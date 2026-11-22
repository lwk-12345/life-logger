"""CAM++（funasr）声纹后端 + 「按 (chunk, MOSS 说话人) 合并再打分」粒度。

背景（2026-10-14 三组真实录音实测，脚本：``scripts/probe_campplus_*.py``）：

* 48 维手工频谱包络把 AI 技术视频判成本人：段 000024 给 0.650 判 self，
  CAM++ 给 -0.024。逐 span 粒度下两者的分离间隔：48 维 0.153 / CAM++ 0.285。
* CAM++ 逐 span 仍噪（本人最低 0.133，都是短句）；按 MOSS 说话人把 span 合并成
  长段后再算：视频中位 0.195、本人中位 0.635，**分离间隔 0.440**（逐 span 0.244）。
* 8765 的 worker venv（``local-config.json`` 的 ``python``）里就有 funasr 1.3.30，
  CAM++ 可以**同进程**加载：模型加载 ~1s，60 秒音频单次 forward 0.45~0.80s。

这些测试锁住新主路径：

1. CAM++ 与 48 维各自带一套阈值（量纲不同，0.45/0.25 不能跨后端复用），
   阈值可配（local-config.json 的 ``moss_voiceprint`` / 环境变量）；
2. funasr 或权重不可用 → 静默降级到 48 维，**不抛异常**，原因可见；
3. 同一 (chunk, 说话人) 的 span 合并成一个窗口只算一次，分数回填给该组每个 span；
4. 不同 chunk 的同名说话人不许跨 chunk 合并（chunk 是分组键的一部分）。

注意：本仓库的 ``.venv`` 没有 funasr（测试必须跑在 ``.venv/bin/python``），
所以 CAM++ 侧的自动化测试用「假 AutoModel + 真 ``CampplusEmbedder``」的组合，
真模型的端到端证据由 ``scripts/probe_moss_campplus_e2e.py``（.venv-funasr）提供。
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from life_logger import campplus_voiceprint, moss_transcribe
from life_logger.campplus_voiceprint import (
    CampplusEmbedder,
    CampplusUnavailable,
    resolve_campplus_model_dir,
)
from life_logger.moss_transcribe import (
    ATTRIBUTION_KEYS,
    MossSpeakerAttributor,
    resolve_moss_voiceprint_settings,
    score_segments,
    transcribe_file,
)
from life_logger.source_attribution import (
    CAMPPLUS_VOICEPRINT_POLICY,
    NUMPY48_VOICEPRINT_POLICY,
    AttributionConfig,
    build_voiceprint_policy,
    voiceprint_policy,
)

SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# 工具：合成「像人声」的音频 + 标准库解码 + 假 AutoModel
# ---------------------------------------------------------------------------


def _voice_like(
    f0: float,
    seconds: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    formants: tuple[float, ...] = (200.0, 700.0, 1200.0, 2600.0),
    seed: int = 1,
    amplitude: float = 0.3,
) -> np.ndarray:
    """合成一段谐波梳 + 共振峰包络的「类人声」（不同 f0/共振峰 ≈ 不同说话人）。"""
    n = int(seconds * sample_rate)
    rng = np.random.default_rng(seed)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    spec = np.zeros(len(freqs), dtype=np.complex128)
    for k in range(1, int(sample_rate / 2.0 / f0)):
        idx = int(round(f0 * k * n / sample_rate))
        if idx >= len(spec):
            break
        spec[idx] += (1.0 / k) * (1.0 + 0.25 * rng.standard_normal())
    envelope = np.full(len(freqs), 0.02)
    for center in formants:
        envelope += np.exp(-0.5 * ((freqs - center) / 220.0) ** 2)
    spec *= envelope
    signal = np.fft.irfft(spec, n=n)
    peak = float(np.max(np.abs(signal))) or 1.0
    signal = signal / peak * amplitude
    signal = signal + rng.standard_normal(n) * 0.001
    return signal.astype("float32")


def _write_wav(path: Path, samples) -> Path:
    arr = np.asarray(samples, dtype="float32")
    pcm = np.clip(arr * 32767.0, -32767.0, 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def _decode_wav(path: str, *, sampling_rate: int = SAMPLE_RATE):
    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def _template_file(tmp_path: Path) -> Path:
    return _write_wav(tmp_path / "template-self.wav", _voice_like(120.0, 6.0, seed=11))


class FakeAutomodel:
    """假 funasr AutoModel：走真 ``CampplusEmbedder``（真写临时 wav、真读回）。

    ``spk_embedding`` 用 48 维声纹派生补齐到 192 维，所以它对「同一个合成声音」
    稳定高相似、对不同声音低相似 —— 足以让测试判定路径与阈值，而不是在测假模型。
    """

    def __init__(self, embedding_dim: int = 192) -> None:
        self.embedding_dim = int(embedding_dim)
        self.inputs: list[str] = []
        self.window_samples: list[int] = []

    def generate(self, *, input, extract_embedding=True, **kwargs):  # noqa: ANN001 - 假签名
        del extract_embedding, kwargs
        self.inputs.append(str(input))
        arr = _decode_wav(str(input))
        self.window_samples.append(int(arr.size))
        base = moss_transcribe._extract_numpy_voiceprint(arr)
        if base is None:
            raise RuntimeError("fake automodel got too-short audio")
        vector = np.zeros(self.embedding_dim, dtype=np.float32)
        vector[: len(base)] = np.asarray(base, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return [{"spk_embedding": vector / norm if norm else vector}]


def _install_fake_campplus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    embedding_dim: int = 192,
) -> FakeAutomodel:
    """把 CAM++ 模型目录与 AutoModel 都换成可用的假货（权重目录仍然要存在）。"""
    model_dir = tmp_path / "campplus-model"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "configuration.json").write_text("{}", encoding="utf-8")
    fake = FakeAutomodel(embedding_dim=embedding_dim)
    monkeypatch.setenv("LIFE_LOGGER_CAMPPLUS_MODEL", str(model_dir))
    monkeypatch.setattr(campplus_voiceprint, "_import_automodel", lambda path: fake)
    return fake


# ---------------------------------------------------------------------------
# 1) 阈值：每个后端一套，且可配置
# ---------------------------------------------------------------------------


def test_numpy48_policy_keeps_legacy_thresholds() -> None:
    """48 维的判据不许被这次改动顺手改掉（老行为原样保留）。"""
    policy = NUMPY48_VOICEPRINT_POLICY

    assert policy.name == "numpy48"
    assert policy.attribution.self_threshold == 0.50
    assert policy.attribution.unknown_threshold == 0.25
    assert policy.near_field_floor > 0.0  # 48 维保留近场信道补偿
    assert policy.embedding_dim == 48


def test_campplus_policy_has_its_own_scale() -> None:
    """CAM++ 量纲不同：自阈值 0.40、未知带下限 0.30（实测非本人最高 0.26 / 本人最低 0.59）。"""
    policy = CAMPPLUS_VOICEPRINT_POLICY

    assert policy.name == "campplus"
    assert policy.embedding_dim == 192
    assert policy.attribution.self_threshold == 0.40
    assert policy.attribution.unknown_threshold == 0.30
    # 近场抬升按 48 维信道敏感度标定：CAM++ 上会把 0.31 的非本人抬过 0.40 → 必须关掉
    assert policy.near_field_floor <= 0.0
    assert policy.near_clear_similarity_bonus == 0.0
    # 短窗在 CAM++ 上不可靠（0.46s 短句实测 0.181），门槛比 48 维高
    assert policy.min_window_seconds >= 0.8


def test_voiceprint_policy_lookup_rejects_unknown_backend() -> None:
    assert voiceprint_policy("campplus") is CAMPPLUS_VOICEPRINT_POLICY
    assert voiceprint_policy("numpy48") is NUMPY48_VOICEPRINT_POLICY
    with pytest.raises(ValueError):
        voiceprint_policy("whisper-large")


def test_build_voiceprint_policy_overrides_only_thresholds() -> None:
    policy = build_voiceprint_policy(
        "campplus", self_threshold=0.55, unknown_threshold=0.35
    )

    assert policy.attribution == AttributionConfig(
        self_threshold=0.55, unknown_threshold=0.35
    )
    # 后端标定过的常数不受影响
    assert policy.embedding_dim == 192
    assert policy.near_field_floor == CAMPPLUS_VOICEPRINT_POLICY.near_field_floor


def test_build_voiceprint_policy_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError):
        build_voiceprint_policy(
            "campplus", self_threshold=0.30, unknown_threshold=0.40
        )


# ---------------------------------------------------------------------------
# 2) CAM++ 嵌入器：真把 wav 写盘再喂给模型；不可用时如实报因
# ---------------------------------------------------------------------------


def test_campplus_embedder_returns_normalized_192d_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_campplus(monkeypatch, tmp_path)
    embedder = CampplusEmbedder(model_dir=tmp_path / "campplus-model")

    embedding = embedder.embed(_voice_like(120.0, 3.0, seed=5))

    assert embedding is not None
    assert len(embedding) == 192
    assert float(np.linalg.norm(np.asarray(embedding))) == pytest.approx(1.0, abs=1e-5)
    # 传文件路径是唯一实测可行的调用形态（dict 形式会炸 Kaldi.fbank）
    assert fake.inputs and Path(fake.inputs[0]).is_file() is False  # 临时文件已清理
    assert fake.window_samples == [int(3.0 * SAMPLE_RATE)]


def test_campplus_embedder_same_voice_scores_higher_than_other_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_campplus(monkeypatch, tmp_path)
    embedder = CampplusEmbedder(model_dir=tmp_path / "campplus-model")

    template = np.asarray(embedder.embed(_voice_like(120.0, 6.0, seed=11)))
    same = np.asarray(embedder.embed(_voice_like(120.0, 5.0, seed=99)))
    other = np.asarray(
        embedder.embed(_voice_like(210.0, 5.0, formants=(350.0, 2200.0, 3400.0), seed=5))
    )

    assert float(template @ same) > float(template @ other)


def test_campplus_embedder_rejects_too_short_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_campplus(monkeypatch, tmp_path)
    embedder = CampplusEmbedder(model_dir=tmp_path / "campplus-model")

    assert embedder.embed(np.zeros(1600, dtype="float32")) is None


def test_campplus_embedder_reports_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = tmp_path / "campplus-model"
    model_dir.mkdir()

    def _boom(path):  # noqa: ANN001 - 假故障
        raise CampplusUnavailable("funasr_unavailable:ModuleNotFoundError")

    monkeypatch.setattr(campplus_voiceprint, "_import_automodel", _boom)
    embedder = CampplusEmbedder(model_dir=model_dir)

    assert embedder.available is False
    assert "funasr_unavailable" in str(embedder.load_error)
    assert embedder.embed(_voice_like(120.0, 3.0)) is None


def test_resolve_campplus_model_dir_prefers_env_then_config_then_default(
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / "from-env"
    env_dir.mkdir()
    cfg_dir = tmp_path / "from-config"
    cfg_dir.mkdir()
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"campplus": str(cfg_dir)}), encoding="utf-8")

    # 环境变量 > local-config.json
    resolved, source = resolve_campplus_model_dir(config_path=config)
    assert (resolved, source) == (cfg_dir, "local-config")
    resolved, source = resolve_campplus_model_dir(
        config_path=config, env={"LIFE_LOGGER_CAMPPLUS_MODEL": str(env_dir)}
    )
    assert (resolved, source) == (env_dir, "env")

    # 显式参数缺失 → 如实报因，不偷偷换目录
    resolved, source = resolve_campplus_model_dir(
        tmp_path / "gone", config_path=config, env={}
    )
    assert resolved is None
    assert source.startswith("campplus_model_missing:")


def test_real_campplus_weight_dir_resolves_on_this_machine() -> None:
    """本机缓存里就有 CAM++ 权重（probe 脚本实测），解析必须成功。"""
    resolved, source = resolve_campplus_model_dir(env={})

    assert resolved is not None, source
    assert resolved.is_dir()
    assert (resolved / "configuration.json").is_file()


# ---------------------------------------------------------------------------
# 3) 后端选择：CAM++ 可用走新路径；不可用静默降级 48 维（不抛异常）
# ---------------------------------------------------------------------------


class _StubPool:
    """固定 raw 相似度的模板池桩：让阈值/门控单独受测。"""

    def __init__(self, raw_similarity: float) -> None:
        self.raw_similarity = float(raw_similarity)
        self.updates: list[tuple[float, float | None]] = []

    def score_candidate(self, candidate) -> float:  # noqa: ANN001 - 测试桩
        return self.raw_similarity

    def maybe_update(self, candidate, similarity, *, proximity_ratio_db=None) -> bool:  # noqa: ANN001
        self.updates.append((float(similarity), proximity_ratio_db))
        return False


def _patch_proximity(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    *,
    ratio_db: float = 0.0,
    score: float = 0.5,
) -> None:
    """把近场/远场证据固定成指定标签（判场算法另有测试）。"""
    from life_logger import source_attribution

    def _forced(**kwargs):  # noqa: ANN003 - 与真实签名同形
        del kwargs
        return {
            "acoustic_proximity": label,
            "proximity_score": score,
            "proximity_confidence": 1.0,
            "snr_proxy_db": 6.0,
            "proximity_ratio_db": ratio_db,
        }

    monkeypatch.setattr(source_attribution, "classify_acoustic_proximity", _forced)


def _install_broken_campplus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str
) -> None:
    """权重目录在（能被解析到），但加载必然失败 —— 模拟「没装 funasr」。"""
    model_dir = tmp_path / "campplus-model"
    model_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("LIFE_LOGGER_CAMPPLUS_MODEL", str(model_dir))

    def _boom(path):  # noqa: ANN001 - 假故障
        raise CampplusUnavailable(reason)

    monkeypatch.setattr(campplus_voiceprint, "_import_automodel", _boom)


def test_settings_default_to_campplus_with_its_own_thresholds(tmp_path: Path) -> None:
    settings = resolve_moss_voiceprint_settings(
        config_path=tmp_path / "no-config.json", env={}
    )

    assert settings.backend == "campplus"
    assert settings.policy.attribution.self_threshold == 0.40
    assert settings.policy.attribution.unknown_threshold == 0.30
    assert settings.threshold_source == "default"
    assert settings.summary()["backend"] == "campplus"


def test_settings_read_thresholds_from_local_config(tmp_path: Path) -> None:
    config = tmp_path / "local-config.json"
    config.write_text(
        json.dumps(
            {
                "moss_voiceprint": {
                    "backend": "campplus",
                    "self_threshold": 0.55,
                    "unknown_threshold": 0.35,
                }
            }
        ),
        encoding="utf-8",
    )

    settings = resolve_moss_voiceprint_settings(config_path=config, env={})

    assert (settings.backend, settings.threshold_source) == ("campplus", "config")
    assert settings.policy.attribution.self_threshold == 0.55
    assert settings.policy.attribution.unknown_threshold == 0.35


def test_settings_env_overrides_config_for_one_threshold_only(tmp_path: Path) -> None:
    """只覆盖一个阈值时，另一个仍按**该后端**的量纲取值（不许串到 48 维的 0.25）。"""
    config = tmp_path / "local-config.json"
    config.write_text(
        json.dumps({"moss_voiceprint": {"self_threshold": 0.55, "unknown_threshold": 0.35}}),
        encoding="utf-8",
    )

    settings = resolve_moss_voiceprint_settings(
        config_path=config, env={"LIFE_LOGGER_MOSS_SELF_THRESHOLD": "0.62"}
    )

    assert (settings.threshold_source, settings.policy.attribution.self_threshold) == (
        "env",
        0.62,
    )
    assert settings.policy.attribution.unknown_threshold == 0.35


def test_settings_can_force_legacy_numpy48_backend(tmp_path: Path) -> None:
    settings = resolve_moss_voiceprint_settings(
        backend="numpy48", config_path=tmp_path / "no-config.json", env={}
    )

    assert settings.backend == "numpy48"
    assert settings.policy.attribution.self_threshold == 0.50
    assert settings.policy.attribution.unknown_threshold == 0.25


def test_settings_bad_values_fall_back_with_warning(tmp_path: Path) -> None:
    config = tmp_path / "local-config.json"
    config.write_text(
        json.dumps(
            {
                "moss_voiceprint": {
                    "backend": "whisper-large",
                    "self_threshold": "很高",
                    "unknown_threshold": 0.30,
                }
            }
        ),
        encoding="utf-8",
    )

    settings = resolve_moss_voiceprint_settings(config_path=config, env={})

    # 坏后端/坏阈值绝不抛给 ASR：退回默认后端 + 留 warning
    assert settings.backend == "campplus"
    assert settings.policy.attribution.self_threshold == 0.40
    assert settings.policy.attribution.unknown_threshold == 0.30
    joined = " ".join(settings.warnings)
    assert "unknown_voiceprint_backend:whisper-large" in joined
    assert "invalid_moss_voiceprint_threshold" in joined


def test_campplus_backend_is_used_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_campplus(monkeypatch, tmp_path)
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
        decode=_decode_wav,
    )

    assert attributor.available, attributor.degraded_reason
    summary = attributor.summary()
    assert summary["voiceprint_backend"] == "campplus"
    assert summary["embedding_dim"] == 192
    assert summary["self_threshold"] == 0.40
    assert summary["unknown_threshold"] == 0.30
    assert summary["backend_fallback_reason"] is None
    # 模板走的是 CAM++ 嵌入器（不是 48 维），所以假模型一定被调用过
    assert len(fake.inputs) == 1


def test_campplus_thresholds_are_in_effect_for_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个 raw 分数在 CAM++ 量纲下是 self，在 48 维量纲下是 unknown。"""
    _install_fake_campplus(monkeypatch, tmp_path)
    _patch_proximity(monkeypatch, "uncertain")
    signal = _voice_like(120.0, 4.0, seed=13)

    campplus = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav
    )
    campplus.pool = _StubPool(0.45)
    campplus_record = score_segments(campplus, signal, [(0.0, 4.0)], fuse=False)[0]

    legacy = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav, backend="numpy48"
    )
    legacy.pool = _StubPool(0.45)
    legacy_record = score_segments(legacy, signal, [(0.0, 4.0)], fuse=False)[0]

    assert campplus_record["voice_similarity"] == pytest.approx(0.45)
    assert campplus_record["source_label"] == "self"
    assert legacy_record["source_label"] == "unknown"  # 0.25 <= 0.45 < 0.50
    assert legacy_record["review_required"] is True


def test_campplus_does_not_get_near_field_channel_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """近场抬升（0.55）是 48 维的补偿：CAM++ 上会把 0.35 的非本人抬成 self。"""
    _install_fake_campplus(monkeypatch, tmp_path)
    _patch_proximity(monkeypatch, "near", ratio_db=5.0)
    signal = _voice_like(120.0, 4.0, seed=17)

    campplus = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav
    )
    campplus.pool = _StubPool(0.35)
    record = score_segments(campplus, signal, [(0.0, 4.0)], fuse=False)[0]

    assert record["voice_similarity"] == pytest.approx(0.35)
    assert record["source_label"] == "unknown"


def test_missing_funasr_falls_back_to_48d_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_broken_campplus(
        monkeypatch, tmp_path, "funasr_unavailable:ModuleNotFoundError"
    )

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav
    )

    assert attributor.available, attributor.degraded_reason
    summary = attributor.summary()
    assert summary["voiceprint_backend"] == "numpy48"
    assert "funasr_unavailable" in str(summary["backend_fallback_reason"])
    # 降级后必须换回 48 维自己的阈值（0.50/0.25），不能用 CAM++ 的 0.40/0.30
    assert summary["self_threshold"] == 0.50
    assert summary["unknown_threshold"] == 0.25

    records = score_segments(attributor, _voice_like(120.0, 5.0, seed=99), [(0.0, 5.0)])
    assert records[0]["source_label"] == "self"
    assert records[0]["voice_similarity"] >= 0.5


def test_campplus_failure_while_embedding_the_template_also_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型能加载但算不出模板 → 仍然降级 48 维，而不是整体不可用。"""

    class _DeafAutomodel:
        def generate(self, *, input, extract_embedding=True, **kwargs):  # noqa: ANN001
            raise RuntimeError("boom")

    model_dir = tmp_path / "campplus-model"
    model_dir.mkdir()
    monkeypatch.setenv("LIFE_LOGGER_CAMPPLUS_MODEL", str(model_dir))
    monkeypatch.setattr(
        campplus_voiceprint, "_import_automodel", lambda path: _DeafAutomodel()
    )

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav
    )

    assert attributor.available, attributor.degraded_reason
    summary = attributor.summary()
    assert summary["voiceprint_backend"] == "numpy48"
    assert "campplus" in str(summary["backend_fallback_reason"])
    assert summary["self_threshold"] == 0.50


def test_explicit_numpy48_backend_never_touches_campplus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_campplus(monkeypatch, tmp_path)

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav, backend="numpy48"
    )

    assert attributor.available
    assert attributor.summary()["voiceprint_backend"] == "numpy48"
    records = score_segments(attributor, _voice_like(120.0, 5.0, seed=8), [(0.0, 5.0)])
    assert records[0]["source_label"] == "self"
    assert fake.inputs == []  # 完全没碰 CAM++


def test_real_env_without_funasr_degrades_gracefully(tmp_path: Path) -> None:
    """本仓库 .venv 没有 funasr：默认配置下必须自动退回 48 维而不是整体失效。"""
    import importlib.util

    if importlib.util.find_spec("funasr") is not None:  # pragma: no cover - 有 funasr 的环境
        pytest.skip("该环境装了 funasr，降级路径由其它用例覆盖")

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav
    )

    assert attributor.available, attributor.degraded_reason
    assert attributor.summary()["voiceprint_backend"] == "numpy48"
    assert attributor.summary()["self_threshold"] == 0.50


# ---------------------------------------------------------------------------
# 4) 打分粒度：同一 (chunk, 说话人) 合并成长段只算一次，分数回填给该组每个 span
# ---------------------------------------------------------------------------


def test_speaker_group_is_scored_once_and_backfilled_to_every_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并粒度：模板 + 每个 (chunk, 说话人) 组各算一次，分数回填给组内每个 span。"""
    model = _install_fake_campplus(monkeypatch, tmp_path)
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
        decode=_decode_wav,
    )
    assert attributor.available, attributor.degraded_reason
    template_calls = len(model.window_samples)

    signal = np.concatenate(
        [_voice_like(120.0, 2.0, seed=21), _voice_like(120.0, 2.0, seed=22),
         _voice_like(210.0, 2.0, formants=(350.0, 2200.0, 3400.0), seed=23)]
    ).astype("float32")

    records = score_segments(
        attributor,
        signal,
        [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)],
        speaker_groups=["S01", "S01", "S02"],
        fuse=False,
    )

    assert len(records) == 3
    # CAM++ 只算了「模板 + S01 合并窗口(4s) + S02 窗口(2s)」= 3 次
    assert model.window_samples[template_calls:] == [
        4 * SAMPLE_RATE,
        2 * SAMPLE_RATE,
    ]
    # 同组每个 span 拿到同一个分数（回填），并且是合并长段算出来的那一个
    assert records[0]["voice_similarity"] == records[1]["voice_similarity"]
    assert records[0]["source_label"] == records[1]["source_label"]
    assert records[0]["decision_reason"] == records[1]["decision_reason"]
    assert set(ATTRIBUTION_KEYS) <= set(records[0])
    for record in records:
        assert record["attribution_granularity"] == "speaker_group"
    assert records[0]["speaker_group_key"] == "S01"
    assert records[0]["speaker_group_spans"] == 2
    assert records[0]["speaker_group_seconds"] == pytest.approx(4.0)
    assert records[2]["speaker_group_key"] == "S02"
    assert records[2]["speaker_group_spans"] == 1


def test_spans_without_speaker_label_are_scored_one_by_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 MOSS 说话人标签的 span 不许和别的 span 合并 —— 宁可慢，不可错。"""
    model = _install_fake_campplus(monkeypatch, tmp_path)
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
        decode=_decode_wav,
    )
    template_calls = len(model.window_samples)
    signal = np.concatenate(
        [_voice_like(120.0, 2.0, seed=31), _voice_like(120.0, 2.0, seed=32),
         _voice_like(120.0, 2.0, seed=33)]
    ).astype("float32")

    records = score_segments(
        attributor,
        signal,
        [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)],
        speaker_groups=["S01", None, "S01"],
        fuse=False,
    )

    # S01 的两个 span 合并（4s 窗），中间没有标签的那个单独算（2s 窗）
    assert model.window_samples[template_calls:] == [
        4 * SAMPLE_RATE,
        2 * SAMPLE_RATE,
    ]
    assert records[1]["speaker_group_key"] is None
    assert records[1]["attribution_granularity"] == "per_span"
    assert records[0]["speaker_group_key"] == records[2]["speaker_group_key"] == "S01"
    assert records[0]["voice_similarity"] == records[2]["voice_similarity"]


def test_no_speaker_groups_keeps_the_legacy_per_span_granularity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_fake_campplus(monkeypatch, tmp_path)
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
        decode=_decode_wav,
    )
    template_calls = len(model.window_samples)
    signal = np.concatenate(
        [_voice_like(120.0, 2.0, seed=41), _voice_like(120.0, 2.0, seed=42)]
    ).astype("float32")

    records = score_segments(attributor, signal, [(0.0, 2.0), (2.0, 4.0)], fuse=False)

    assert model.window_samples[template_calls:] == [2 * SAMPLE_RATE, 2 * SAMPLE_RATE]
    for record in records:
        assert record["attribution_granularity"] == "per_span"
        assert "speaker_group_key" not in record


def test_same_speaker_label_in_two_chunks_is_not_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分组键里含 chunk：第二个 chunk 的 S01 不能复用第一个 chunk 的分数/窗口。"""
    model = _install_fake_campplus(monkeypatch, tmp_path)
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
        decode=_decode_wav,
    )
    template_calls = len(model.window_samples)
    chunk0 = np.concatenate(
        [_voice_like(120.0, 2.0, seed=51), _voice_like(120.0, 2.0, seed=52)]
    ).astype("float32")
    chunk1 = np.concatenate(
        [_voice_like(120.0, 2.0, seed=53), _voice_like(120.0, 2.0, seed=54)]
    ).astype("float32")

    first = score_segments(
        attributor, chunk0, [(0.0, 2.0), (2.0, 4.0)], speaker_groups=["S01", "S01"],
        fuse=False,
    )
    second = score_segments(
        attributor,
        chunk1,
        [(1200.0, 1202.0), (1202.0, 1204.0)],
        span_offset=1200.0,
        speaker_groups=["S01", "S01"],
        fuse=False,
    )

    # 两个 chunk 各自算了一次 4 秒的合并窗口（没有跨 chunk 复用）
    assert model.window_samples[template_calls:] == [
        4 * SAMPLE_RATE,
        4 * SAMPLE_RATE,
    ]
    assert first[0]["speaker_group_key"] == "S01"
    assert second[0]["speaker_group_key"] == "S01"


def test_campplus_min_window_is_stricter_than_48d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.5 秒的孤立段在 CAM++ 下判「太短」待复核（实测短句 embedding 不可靠）。"""
    _install_fake_campplus(monkeypatch, tmp_path)
    signal = _voice_like(120.0, 1.0, seed=61)

    campplus = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav
    )
    campplus_record = score_segments(campplus, signal, [(0.0, 0.5)], fuse=False)[0]

    legacy = MossSpeakerAttributor(
        template_path=_template_file(tmp_path), decode=_decode_wav, backend="numpy48"
    )
    legacy_record = score_segments(legacy, signal, [(0.0, 0.5)], fuse=False)[0]

    assert campplus_record["decision_reason"] == "segment_too_short"
    assert campplus_record["source_label"] == "unknown"
    assert campplus_record["review_required"] is True
    assert legacy_record["decision_reason"] != "segment_too_short"


def test_merged_group_is_scored_as_one_long_window_not_short_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三个都不到 CAM++ 短窗门槛的同说话人 span，合并后照样能判（组内总长够）。"""
    model = _install_fake_campplus(monkeypatch, tmp_path)
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
        decode=_decode_wav,
    )
    template_calls = len(model.window_samples)
    signal = _voice_like(120.0, 6.0, seed=71)

    records = score_segments(
        attributor,
        signal,
        [(0.0, 0.5), (1.0, 1.5), (2.0, 2.5)],
        speaker_groups=["S01", "S01", "S01"],
        fuse=False,
    )

    assert model.window_samples[template_calls:] == [int(1.5 * SAMPLE_RATE)]
    for record in records:
        assert record["decision_reason"] != "segment_too_short"
        assert record["voice_similarity"] is not None
    assert records[0]["voice_similarity"] == records[2]["voice_similarity"]


# ---------------------------------------------------------------------------
# 5) 管道接线：transcribe_file 真的按 MOSS 说话人分组打分并写进 stats
# ---------------------------------------------------------------------------


def _inject_test_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    real_cls = moss_transcribe.MossSpeakerAttributor
    monkeypatch.setattr(
        moss_transcribe,
        "MossSpeakerAttributor",
        lambda **kwargs: real_cls(decode=_decode_wav, **kwargs),
    )


def _fake_moss_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict,
    duration: float = 6.0,
) -> Path:
    audio = _write_wav(tmp_path / "recording.wav", _voice_like(120.0, duration, seed=81))
    monkeypatch.setattr(moss_transcribe, "moss_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        moss_transcribe,
        "_to_wav16",
        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()), True)[1],
    )
    monkeypatch.setattr(moss_transcribe, "_duration", lambda path: duration)
    monkeypatch.setattr(moss_transcribe, "_rms_of", lambda path: 0.02)
    monkeypatch.setattr(
        moss_transcribe, "_run_moss", lambda chunk, outp, hotwords=None: payload
    )
    _inject_test_decoder(monkeypatch)
    return audio


def test_transcribe_file_backfills_group_scores_to_every_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_fake_campplus(monkeypatch, tmp_path)
    payload = {
        "language": "zh",
        "segments": [
            {"text": "[S01] 第一句。", "start": 0.0, "end": 1.5, "speaker_id": "S01"},
            {"text": "[S01] 第二句。", "start": 1.7, "end": 3.2, "speaker_id": "S01"},
            {"text": "[S02] 别人的话。", "start": 3.4, "end": 5.0, "speaker_id": "S02"},
        ],
    }
    audio = _fake_moss_run(tmp_path, monkeypatch, payload=payload)

    segments, stats = transcribe_file(
        audio,
        recording_id="rec-group",
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
    )

    attributions = stats["attributions"]
    assert len(attributions) == len(segments) == 3
    rows = [attributions[segment.segment_id] for segment in segments]
    assert rows[0]["voice_similarity"] == rows[1]["voice_similarity"]
    assert rows[0]["decision_reason"] == rows[1]["decision_reason"]
    assert rows[0]["speaker_group_key"] == "S01"
    assert rows[0]["speaker_group_spans"] == 2
    assert rows[2]["speaker_group_key"] == "S02"
    assert rows[2]["speaker_group_spans"] == 1
    assert set(ATTRIBUTION_KEYS) <= set(rows[0])
    # 后端与粒度写进 stats（看板/排查要能看出来跑的是哪条路径）
    assert stats["speaker_attribution"]["voiceprint_backend"] == "campplus"
    assert stats["speaker_attribution"]["granularity"] == "speaker_group"
    assert stats["speaker_attribution"]["self_threshold"] == 0.40
    # 模板 1 次 + 两个说话人各 1 次
    assert len(model.window_samples) == 3


def test_transcribe_file_keeps_working_when_campplus_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没 funasr 的机器上整条 MOSS 链路照跑（降级 48 维），后端写进 stats。"""
    _install_broken_campplus(
        monkeypatch, tmp_path, "funasr_unavailable:ModuleNotFoundError"
    )
    payload = {
        "language": "zh",
        "segments": [
            {"text": "[S01] 一句自己的话。", "start": 0.0, "end": 3.0, "speaker_id": "S01"},
        ],
    }
    audio = _fake_moss_run(
        tmp_path, monkeypatch, payload=payload, duration=4.0
    )

    segments, stats = transcribe_file(
        audio,
        recording_id="rec-fallback",
        template_path=_template_file(tmp_path),
        config_path=tmp_path / "no-config.json",
    )

    assert len(segments) == 1
    row = stats["attributions"][segments[0].segment_id]
    assert row["voice_similarity"] is not None
    assert stats["speaker_attribution"]["voiceprint_backend"] == "numpy48"
    assert stats["speaker_attribution"]["self_threshold"] == 0.50
    assert "funasr_unavailable" in str(
        stats["speaker_attribution"]["backend_fallback_reason"]
    )
