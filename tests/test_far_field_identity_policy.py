"""P1 回归：远场不再「一票否决身份」。

背景（实测）：手表戴在手腕上，嘴到手腕 30~50cm，**用户自己正常说话测出来就是
``far``**。旧逻辑 ``similarity = min(raw_sim, 0.18)`` 把远场段的声纹相似度直接
封顶到 0.18 → 必然掉进 ``non_self``（真实录音里 ``far`` 段 100% 判 non_self）。

新逻辑（用户拍板，两条路径共用 :func:`proximity_adjusted_similarity`）：

* 远场：``raw_sim * FAR_FIELD_SIMILARITY_DAMPING``（0.8）—— 只降权 20%，
  不再封顶；同时把该段标成 ``review_required=True`` 交人工复核；
* 近场 + 低频强：``max(raw_sim, 0.55)`` —— 保留。它不是「近场证明是本人」，
  而是**可信性前提**：模板是近场录的，只有近场录音的分数才与模板条件可比；
* 其余：裸值。

这些测试同时锁住 ``moss_transcribe``（MOSS 主路径）与 ``transcribe``
（Whisper 路径）两侧行为一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from life_logger.moss_transcribe import (
    MossSpeakerAttributor,
    _score_window,
    score_segments,
)
from life_logger.source_attribution import (
    FAR_FIELD_SIMILARITY_DAMPING,
    NEAR_FIELD_SIMILARITY_FLOOR,
    proximity_adjusted_similarity,
)
from life_logger.transcribe import LocalTurboCompositeTranscriber

SAMPLE_RATE = 16000
NOISE_FLOOR_DBFS = -60.0


# ---------------------------------------------------------------------------
# 工具：桩池（锁定 raw 相似度）+ 强制近场/远场证据 + 合成窗口
# ---------------------------------------------------------------------------


class _StubPool:
    """固定返回 raw 相似度的模板池桩：让门控逻辑单独受测。"""

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
    ratio_db: float = -6.0,
    score: float = 0.1,
) -> None:
    """把近场/远场证据固定成指定标签（§门控逻辑受测，判场算法另有测试）。"""
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

    monkeypatch.setattr(
        source_attribution, "classify_acoustic_proximity", _forced
    )


def _window(seconds: float = 3.0, *, freq: float = 150.0, amplitude: float = 0.3):
    """合成窗口：强低频（近讲低频比达标）+ 轻噪声，长度够声纹抽取。"""
    import numpy as np

    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    rng = np.random.default_rng(3)
    signal = amplitude * np.sin(2 * np.pi * freq * t)
    signal = signal + rng.standard_normal(n) * 0.001
    return signal.astype("float32")


def _template_file(tmp_path: Path) -> Path:
    import wave

    import numpy as np

    path = tmp_path / "template-self.wav"
    pcm = np.clip(_window(4.0) * 32767.0, -32767.0, 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def _decode_wav(path: str, *, sampling_rate: int = SAMPLE_RATE):
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def _attributor_with_raw(tmp_path: Path, raw_similarity: float) -> MossSpeakerAttributor:
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available, attributor.degraded_reason
    attributor.pool = _StubPool(raw_similarity)
    return attributor


def _transcriber_with_raw(raw_similarity: float) -> LocalTurboCompositeTranscriber:
    transcriber = LocalTurboCompositeTranscriber(enable_speaker=True)
    transcriber._get_rolling_pool = lambda: _StubPool(raw_similarity)  # type: ignore[method-assign]
    transcriber._extract_numpy_voiceprint = lambda arr: [1.0, 0.0]  # type: ignore[method-assign]
    return transcriber


# ---------------------------------------------------------------------------
# 1) 共用策略函数（两条路径的唯一实现）
# ---------------------------------------------------------------------------


def test_far_field_is_damped_not_capped() -> None:
    similarity, review = proximity_adjusted_similarity(
        0.9, prox_label="far", proximity_ratio_db=-6.0
    )

    assert similarity == pytest.approx(0.9 * FAR_FIELD_SIMILARITY_DAMPING)  # 0.72
    assert similarity > 0.5  # 旧行为会封顶到 0.18 → 必判 non_self
    assert review is True


def test_low_proximity_ratio_alone_is_treated_as_far_field() -> None:
    similarity, review = proximity_adjusted_similarity(
        0.9, prox_label="uncertain", proximity_ratio_db=-2.0
    )

    assert similarity == pytest.approx(0.9 * FAR_FIELD_SIMILARITY_DAMPING)
    assert review is True


def test_near_field_keeps_channel_compensation() -> None:
    # 近场 + 低频强 + raw 已达最低可信度 → 抬到 0.55
    assert proximity_adjusted_similarity(
        0.35, prox_label="near", proximity_ratio_db=5.0
    ) == pytest.approx((NEAR_FIELD_SIMILARITY_FLOOR, 0.0))
    # 已经更高的 raw 不动
    assert proximity_adjusted_similarity(
        0.9, prox_label="near", proximity_ratio_db=5.0
    ) == pytest.approx((0.9, 0.0))
    # raw 太低（<0.30）不享受加分：那只是「声音大」，不是同一人的证据
    assert proximity_adjusted_similarity(
        0.2, prox_label="near", proximity_ratio_db=5.0
    ) == pytest.approx((0.2, 0.0))
    # 低频比不达标不算近场
    assert proximity_adjusted_similarity(
        0.35, prox_label="near", proximity_ratio_db=2.0
    ) == pytest.approx((0.35, 0.0))
    # uncertain 走裸值，不再封顶 0.38
    assert proximity_adjusted_similarity(
        0.9, prox_label="uncertain", proximity_ratio_db=0.0
    ) == pytest.approx((0.9, 0.0))


# ---------------------------------------------------------------------------
# 2) MOSS 主路径（moss_transcribe._score_window / score_segments）
# ---------------------------------------------------------------------------


def test_moss_far_field_high_similarity_is_self_not_non_self(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_proximity(monkeypatch, "far")
    attributor = _attributor_with_raw(tmp_path, 0.9)

    record = _score_window(attributor.pool, _window(), NOISE_FLOOR_DBFS)

    assert record["acoustic_proximity"] == "far"
    assert record["voice_similarity"] == pytest.approx(0.72)
    assert record["source_label"] == "self"
    assert record["review_required"] is True


def test_moss_far_field_low_similarity_is_still_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_proximity(monkeypatch, "far")
    attributor = _attributor_with_raw(tmp_path, 0.2)

    records = score_segments(attributor, _window(), [(0.0, 3.0)])

    assert records[0]["voice_similarity"] == pytest.approx(0.16)
    assert records[0]["source_label"] == "non_self"


def test_moss_far_field_review_flag_survives_multi_evidence_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """融合后写进 stats["attributions"] 的那条记录必须带复核标记（下游读它）。"""
    _patch_proximity(monkeypatch, "far")
    attributor = _attributor_with_raw(tmp_path, 0.9)

    records = score_segments(attributor, _window(), [(0.0, 3.0)])

    assert records[0]["source_label"] == "self"
    assert records[0]["voice_similarity"] == pytest.approx(0.72)
    assert records[0]["review_required"] is True
    assert records[0]["identity_source_label"] == "self"


def test_moss_far_field_marks_review_required_on_every_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_proximity(monkeypatch, "far")

    for raw in (0.9, 0.72, 0.4, 0.2):
        attributor = _attributor_with_raw(tmp_path, raw)
        record = _score_window(attributor.pool, _window(), NOISE_FLOOR_DBFS)
        assert record["review_required"] is True, raw


def test_moss_near_field_low_similarity_still_gets_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_proximity(monkeypatch, "near", ratio_db=5.0)
    attributor = _attributor_with_raw(tmp_path, 0.35)

    record = _score_window(attributor.pool, _window(), NOISE_FLOOR_DBFS)

    assert record["voice_similarity"] == pytest.approx(NEAR_FIELD_SIMILARITY_FLOOR)
    assert record["source_label"] == "self"
    assert record["review_required"] is False


# ---------------------------------------------------------------------------
# 3) Whisper 路径（transcribe.LocalTurboCompositeTranscriber）
# ---------------------------------------------------------------------------


def test_transcribe_far_field_high_similarity_is_self_and_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_proximity(monkeypatch, "far")
    transcriber = _transcriber_with_raw(0.9)

    record = transcriber._score_window_speaker_and_proximity(
        _window(), NOISE_FLOOR_DBFS
    )

    assert record["acoustic_proximity"] == "far"
    assert record["voice_similarity"] == pytest.approx(0.72)
    assert record["source_label"] == "self"
    assert record["review_required"] is True


def test_transcribe_far_field_low_similarity_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_proximity(monkeypatch, "far")
    transcriber = _transcriber_with_raw(0.2)

    record = transcriber._score_window_speaker_and_proximity(
        _window(), NOISE_FLOOR_DBFS
    )

    assert record["voice_similarity"] == pytest.approx(0.16)
    assert record["source_label"] == "non_self"


def test_transcribe_near_field_low_similarity_still_gets_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_proximity(monkeypatch, "near", ratio_db=5.0)
    transcriber = _transcriber_with_raw(0.35)

    record = transcriber._score_window_speaker_and_proximity(
        _window(), NOISE_FLOOR_DBFS
    )

    assert record["voice_similarity"] == pytest.approx(NEAR_FIELD_SIMILARITY_FLOOR)
    assert record["source_label"] == "self"


# ---------------------------------------------------------------------------
# 4) 两条路径必须给出同一结论（同一段逻辑，不许漂移）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "ratio_db", "raw"),
    [
        ("far", -6.0, 0.9),
        ("far", -6.0, 0.2),
        ("near", 5.0, 0.35),
        ("near", 5.0, 0.2),
    ],
)
def test_both_paths_agree_on_proximity_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    ratio_db: float,
    raw: float,
) -> None:
    _patch_proximity(monkeypatch, label, ratio_db=ratio_db)
    expected_similarity, expected_review = proximity_adjusted_similarity(
        raw, prox_label=label, proximity_ratio_db=ratio_db
    )

    attributor = _attributor_with_raw(tmp_path, raw)
    moss_scores = _score_window(attributor.pool, _window(), NOISE_FLOOR_DBFS)
    moss_fused = score_segments(attributor, _window(), [(0.0, 3.0)])[0]
    whisper_fused = _transcriber_with_raw(raw)._score_window_speaker_and_proximity(
        _window(), NOISE_FLOOR_DBFS
    )

    # 打分层：两条路径的调整值必须一模一样（transcribe 侧的融合前标签）
    assert moss_scores["voice_similarity"] == pytest.approx(expected_similarity)
    assert whisper_fused["voice_similarity"] == pytest.approx(expected_similarity)
    assert moss_scores["source_label"] == whisper_fused["identity_source_label"]
    # 远场强制复核必须真的落到返回字段上（其余情形由阈值/短段保护决定）
    if expected_review:
        assert moss_scores["review_required"] is True
        # transcribe 侧在本方法内就做多证据融合：唯一允许把远场复核标记清掉的
        # 路径，是「远场 + 分数低于 unknown 阈值 + 孤立短段」→ 明确判 non_self。
        deliberate_downgrade = (
            whisper_fused["source_label"] == "non_self"
            and whisper_fused["decision_reason"] == "multi_evidence_far_low_similarity"
        )
        assert whisper_fused["review_required"] is True or deliberate_downgrade

    # 融合层：多证据融合后（transcribe 就地融合 / moss 由 score_segments 融合）结论一致
    assert moss_fused["source_label"] == whisper_fused["source_label"]
    assert moss_fused["review_required"] == whisper_fused["review_required"]


def test_real_repo_template_pool_still_unchanged_for_near_speech(
    tmp_path: Path,
) -> None:
    """真声纹（非桩）：抽得出向量、近场段仍能判 self —— 修复没伤到正常路径。"""
    import numpy as np

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available

    same_template_audio = _decode_wav(str(_template_file(tmp_path)))
    records = score_segments(attributor, same_template_audio, [(0.0, 4.0)])

    assert records[0]["source_label"] == "self"
    assert records[0]["voice_similarity"] >= 0.5
    assert np.isfinite(records[0]["voice_similarity"])
