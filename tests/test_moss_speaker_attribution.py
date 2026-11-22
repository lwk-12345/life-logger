"""MOSS 声纹归属（speaker attribution）接入测试。

背景：换 ASR 引擎到 MOSS 之后声纹没跟过来，2590 条记录里只有 7.7% 带
``voice_similarity``，其余全靠默认 "self"。这些测试锁住新接线：

1. 有模板时能抽出声纹向量并判定 self；
2. 模板缺失 / 声纹关闭 / 段太短 → 优雅降级（不抛异常），且降级原因可见；
3. 已知不是本人的声音（合成另一个声源的谐波+共振峰）不会被判 self；
4. 模板池整个文件只建一次、音频只解码一次（不是每段重建）；
5. 每个 MOSS 段用自己的时间窗独立打分；
6. ``transcribe_file`` 真的把归属记录挂到每条转写记录上（管道接线）。
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from life_logger import moss_transcribe
from life_logger.moss_transcribe import (
    ATTRIBUTION_KEYS,
    DEFAULT_TEMPLATE_RELATIVE,
    MossSpeakerAttributor,
    attribution_label_counts,
    resolve_template_path,
    score_segments,
    transcribe_file,
)

SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# 工具：合成「像人声」的音频 + 用标准库解码（不依赖 faster_whisper）
# ---------------------------------------------------------------------------


def _voice_like(
    f0: float,
    seconds: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    formants: tuple[float, ...] = (200.0, 700.0, 1200.0, 2600.0),
    seed: int = 1,
    amplitude: float = 0.3,
    pause_seconds: float = 0.6,
    pause_every: float = 1.4,
    silence_level: float = 0.0015,
):
    """合成一段谐波梳 + 共振峰包络的「类人声」（不同 f0/共振峰 ≈ 不同说话人）。

    低位共振峰（200Hz）+ 周期性停顿是刻意加的：真实手表近讲录音有胸腔低频
    和换气停顿，近场模型正是靠这两点判 near；纯连续正弦会被判 far。
    """
    import numpy as np

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
    signal = signal + rng.standard_normal(n) * 0.002
    if pause_seconds > 0 and pause_every > 0:
        period = pause_every + pause_seconds
        phase = np.mod(np.arange(n) / sample_rate, period)
        mute = phase >= pause_every
        if mute.any():
            signal[mute] = (
                np.random.default_rng(seed + 999).standard_normal(int(mute.sum()))
                * silence_level
            )
    # 起止淡入淡出，避免边界爆音
    fade = int(0.01 * sample_rate)
    signal[:fade] *= np.linspace(0.0, 1.0, fade)
    signal[-fade:] *= np.linspace(1.0, 0.0, fade)
    return signal.astype("float32")


def _write_wav(path: Path, samples) -> Path:
    import numpy as np

    arr = np.asarray(samples, dtype="float32")
    pcm = np.clip(arr * 32767.0, -32767.0, 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def _decode_wav(path: str, *, sampling_rate: int = SAMPLE_RATE):
    """测试用解码器：标准库读 PCM，避免依赖 faster_whisper。"""
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if rate != sampling_rate:
        raise ValueError(f"unexpected sample rate: {rate}")
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def _template_file(tmp_path: Path) -> Path:
    return _write_wav(
        tmp_path / "template-self.wav",
        _voice_like(120.0, 6.0, seed=11),
    )


def _inject_test_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 transcribe_file 构造的归属器用标准库解码器（仓库 venv 没有 faster_whisper）。"""
    real_cls = moss_transcribe.MossSpeakerAttributor
    monkeypatch.setattr(
        moss_transcribe,
        "MossSpeakerAttributor",
        lambda **kwargs: real_cls(decode=_decode_wav, **kwargs),
    )


# ---------------------------------------------------------------------------
# 1) 有模板 → 抽出向量 → 判 self
# ---------------------------------------------------------------------------


def test_template_voiceprint_yields_self_for_same_voice(tmp_path: Path) -> None:
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available, attributor.degraded_reason

    same_voice = _voice_like(120.0, 5.0, seed=99)  # 同一个人，另一次说话
    records = score_segments(attributor, same_voice, [(0.0, 5.0)])

    assert len(records) == 1
    record = records[0]
    assert record["voice_similarity"] is not None
    assert record["voice_similarity"] >= 0.5
    assert record["source_label"] == "self"
    assert record["review_required"] is False
    assert set(ATTRIBUTION_KEYS) <= set(record)


def test_voiceprint_vector_is_extracted_and_normalized(tmp_path: Path) -> None:
    import math

    embedding = moss_transcribe._extract_numpy_voiceprint(_voice_like(150.0, 3.0))

    assert embedding is not None
    assert len(embedding) == 48
    norm = math.sqrt(sum(value * value for value in embedding))
    assert norm == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# 2) 降级：模板缺失 / 关闭 / 太短 —— 不报错，原因可见
# ---------------------------------------------------------------------------


def test_missing_template_degrades_without_raising(tmp_path: Path) -> None:
    missing = tmp_path / "not-there.wav"
    attributor = MossSpeakerAttributor(template_path=missing, decode=_decode_wav)

    assert attributor.available is False
    assert attributor.degraded_reason is not None
    assert "template_missing" in attributor.degraded_reason

    signal = _voice_like(120.0, 5.0, seed=3)
    records = score_segments(attributor, signal, [(0.0, 2.0), (2.0, 4.0)])

    assert len(records) == 2
    for record in records:
        assert record["source_label"] == "unknown"
        assert record["voice_similarity"] is None
        assert record["review_required"] is True
        assert record["decision_reason"].startswith("voiceprint_unavailable:")
        assert "template_missing" in record["decision_reason"]


def test_attribution_can_be_disabled_explicitly(tmp_path: Path) -> None:
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
        enabled=False,
    )

    assert attributor.available is False
    records = score_segments(attributor, _voice_like(120.0, 4.0), [(0.0, 4.0)])

    assert records[0]["source_label"] == "unknown"
    assert records[0]["decision_reason"] == (
        "voiceprint_unavailable:speaker_attribution_disabled"
    )
    assert attributor.summary()["degraded_reason"] == "speaker_attribution_disabled"


def test_missing_decoder_degrades_without_raising(tmp_path: Path) -> None:
    class _BrokenDecode:
        def __call__(self, path: str, *, sampling_rate: int = SAMPLE_RATE):
            raise RuntimeError("boom")

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_BrokenDecode(),
    )

    assert attributor.available is False
    assert "template_decode_failed" in attributor.degraded_reason


def test_too_short_segment_is_review_not_self(tmp_path: Path) -> None:
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available

    records = score_segments(attributor, _voice_like(120.0, 5.0), [(0.0, 0.1)])

    assert records[0]["source_label"] == "unknown"
    assert records[0]["voice_similarity"] is None
    assert records[0]["review_required"] is True
    assert records[0]["decision_reason"] == "segment_too_short"


def test_empty_signal_still_returns_aligned_records(tmp_path: Path) -> None:
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )

    records = score_segments(attributor, None, [(0.0, 1.0), (1.0, 2.0)])

    assert len(records) == 2
    assert all(record["source_label"] == "unknown" for record in records)
    assert all(
        record["decision_reason"].startswith("voiceprint_unavailable:")
        for record in records
    )


# ---------------------------------------------------------------------------
# 3) 别人的声音不能被判成 self
# ---------------------------------------------------------------------------


def test_different_voice_is_not_labelled_self(tmp_path: Path) -> None:
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available

    other_voice = _voice_like(
        210.0,
        5.0,
        formants=(350.0, 2200.0, 3400.0),
        seed=5,
    )
    records = score_segments(attributor, other_voice, [(0.0, 5.0)])

    record = records[0]
    assert record["source_label"] != "self"
    assert record["voice_similarity"] is not None
    assert record["voice_similarity"] < 0.5  # self_threshold


def test_white_noise_is_not_labelled_self(tmp_path: Path) -> None:
    import numpy as np

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available

    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(SAMPLE_RATE * 4) * 0.2).astype("float32")
    records = score_segments(attributor, noise, [(0.0, 4.0)])

    assert records[0]["source_label"] != "self"


# ---------------------------------------------------------------------------
# 4) 池只建一次 / 只解码一次 / 分段独立打分
# ---------------------------------------------------------------------------


def test_template_pool_is_built_once_for_whole_file(tmp_path: Path) -> None:
    decode_calls: list[str] = []

    def _counting_decode(path: str, *, sampling_rate: int = SAMPLE_RATE):
        decode_calls.append(path)
        return _decode_wav(path, sampling_rate=sampling_rate)

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_counting_decode,
    )
    pool_before = attributor.pool

    records = score_segments(
        attributor,
        _voice_like(120.0, 8.0, seed=4),
        [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 5.0)],
    )

    assert len(records) == 5
    assert len(decode_calls) == 1  # 只解码模板；段级打分复用内存里的音频
    assert attributor.pool is pool_before  # 池没有被逐段重建
    assert attributor.summary()["rolling_pool_history"] <= 20


def test_each_segment_is_scored_on_its_own_window(tmp_path: Path) -> None:
    import numpy as np

    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available

    self_part = _voice_like(120.0, 4.0, seed=21)
    other_part = _voice_like(210.0, 4.0, formants=(350.0, 2200.0, 3400.0), seed=22)
    signal = np.concatenate([self_part, other_part]).astype("float32")

    records = score_segments(attributor, signal, [(0.0, 4.0), (4.0, 8.0)])

    assert records[0]["source_label"] == "self"
    assert records[1]["source_label"] != "self"
    assert records[0]["voice_similarity"] > records[1]["voice_similarity"]


def test_spans_use_file_timeline_with_chunk_offset(tmp_path: Path) -> None:
    """切了 chunk 的录音：段用整段录音时间轴，切窗要减掉 chunk 起点。"""
    attributor = MossSpeakerAttributor(
        template_path=_template_file(tmp_path),
        decode=_decode_wav,
    )
    assert attributor.available

    # chunk 音频只有 4 秒，但它在整段录音里的起点是第 3000 秒
    chunk_signal = _voice_like(120.0, 4.0, seed=61)
    records = score_segments(
        attributor,
        chunk_signal,
        [(3000.0, 3004.0)],
        span_offset=3000.0,
        fuse=False,
    )

    assert len(records) == 1
    assert records[0]["source_label"] == "self"  # 窗口真的切到了音频，而不是空窗

    # 忘了传 span_offset（等价于第 0 秒）→ 窗口落在音频之外，必须降级而不是误判
    staged = score_segments(attributor, chunk_signal, [(3000.0, 3004.0)], fuse=False)
    assert staged[0]["source_label"] == "unknown"
    assert staged[0]["decision_reason"].startswith("voiceprint_unavailable:")


def test_attribution_label_counts_covers_all_labels() -> None:
    counts = attribution_label_counts(
        [
            {"source_label": "self"},
            {"source_label": "self"},
            {"source_label": "unknown"},
            {"source_label": "non_self"},
            {"source_label": None},
        ]
    )

    assert counts == {"self": 2, "non_self": 1, "unknown": 2}
    assert sum(counts.values()) == 5


# ---------------------------------------------------------------------------
# 5) 模板路径解析（local-config.json 优先，其次仓库默认模板）
# ---------------------------------------------------------------------------


def test_explicit_template_wins_and_missing_is_reported(tmp_path: Path) -> None:
    template = _template_file(tmp_path)
    resolved, source = resolve_template_path(template)
    assert resolved == template
    assert source == "explicit"

    missing, source = resolve_template_path(tmp_path / "nope.wav")
    assert missing is None
    assert source.startswith("template_missing:")


def test_config_template_key_is_used(tmp_path: Path) -> None:
    template = _template_file(tmp_path)
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"template": str(template)}), encoding="utf-8")

    resolved, source = resolve_template_path(None, config)
    assert resolved == template
    assert source == "local-config"


def test_configured_but_missing_template_does_not_fall_back(tmp_path: Path) -> None:
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"template": str(tmp_path / "gone.wav")}), encoding="utf-8")

    resolved, source = resolve_template_path(None, config)
    assert resolved is None
    assert source == f"template_missing:{tmp_path / 'gone.wav'}"


def test_repo_default_template_is_used_when_config_has_no_key(tmp_path: Path) -> None:
    config = tmp_path / "local-config.json"
    config.write_text(json.dumps({"asr_provider": "moss"}), encoding="utf-8")

    resolved, source = resolve_template_path(None, config)
    repo_default = (
        Path(moss_transcribe.__file__).resolve().parents[2] / DEFAULT_TEMPLATE_RELATIVE
    )
    if repo_default.is_file():
        assert resolved == repo_default
        assert source == "repo-default"
    else:
        assert resolved is None
        assert source.startswith("template_missing:")


def test_real_repo_config_resolves_an_existing_template() -> None:
    """仓库当前 local-config.json 指向的模板必须真实存在（否则声纹全程降级）。"""
    resolved, source = resolve_template_path()
    assert resolved is not None, source
    assert resolved.is_file()
    assert source in {"local-config", "repo-default"}


# ---------------------------------------------------------------------------
# 6) transcribe_file 接线：每条转写记录都带上归属字段
# ---------------------------------------------------------------------------


def test_transcribe_file_attaches_attribution_to_every_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _template_file(tmp_path)
    input_wav = _write_wav(
        tmp_path / "recording.wav",
        _voice_like(120.0, 6.0, seed=31),
    )

    monkeypatch.setattr(moss_transcribe, "moss_available", lambda: (True, "ok"))

    def _fake_to_wav16(src: Path, dst: Path) -> bool:
        dst.write_bytes(Path(src).read_bytes())
        return True

    monkeypatch.setattr(moss_transcribe, "_to_wav16", _fake_to_wav16)
    monkeypatch.setattr(moss_transcribe, "_duration", lambda path: 6.0)
    monkeypatch.setattr(moss_transcribe, "_rms_of", lambda path: 0.02)

    payload = {
        "language": "zh",
        "segments": [
            {"text": "[S01] 我先记一下今天的训练。", "start": 0.0, "end": 2.0,
             "speaker_id": "S01"},
            {"text": "[S01] 卧推四组，每组六十公斤。", "start": 2.4, "end": 4.4,
             "speaker_id": "S01"},
            {"text": "[S02] 你好，请问几点关门？", "start": 4.6, "end": 5.9,
             "speaker_id": "S02"},
        ],
    }
    monkeypatch.setattr(moss_transcribe, "_run_moss", lambda wav, outp, hotwords=None: payload)
    _inject_test_decoder(monkeypatch)

    segments, stats = transcribe_file(
        input_wav,
        recording_id="rec-test",
        template_path=template,
        config_path=tmp_path / "nonexistent-config.json",
    )

    assert len(segments) == 3
    attributions = stats["attributions"]
    assert len(attributions) == 3
    for segment in segments:
        record = attributions[segment.segment_id]
        assert set(ATTRIBUTION_KEYS) <= set(record)
        assert record["source_label"] in {"self", "non_self", "unknown"}
    assert stats["speaker_attribution"]["available"] is True
    assert stats["speaker_attribution"]["template"] == str(template)
    assert sum(stats["speaker_attribution"]["label_counts"].values()) == 3


def test_transcribe_file_degrades_when_template_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_wav = _write_wav(tmp_path / "recording.wav", _voice_like(120.0, 3.0, seed=41))

    monkeypatch.setattr(moss_transcribe, "moss_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        moss_transcribe,
        "_to_wav16",
        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()), True)[1],
    )
    monkeypatch.setattr(moss_transcribe, "_duration", lambda path: 3.0)
    monkeypatch.setattr(moss_transcribe, "_rms_of", lambda path: 0.02)
    monkeypatch.setattr(
        moss_transcribe,
        "_run_moss",
        lambda wav, outp, hotwords=None: {
            "language": "zh",
            "segments": [
                {"text": "[S01] 测试降级。", "start": 0.0, "end": 1.5,
                 "speaker_id": "S01"},
            ],
        },
    )
    _inject_test_decoder(monkeypatch)

    segments, stats = transcribe_file(
        input_wav,
        recording_id="rec-degraded",
        template_path=tmp_path / "missing-template.wav",
        config_path=tmp_path / "no-config.json",
    )

    assert len(segments) == 1
    record = stats["attributions"][segments[0].segment_id]
    assert record["source_label"] == "unknown"
    assert record["voice_similarity"] is None
    assert record["decision_reason"].startswith("voiceprint_unavailable:")
    assert stats["speaker_attribution"]["available"] is False
    assert "template_missing" in stats["speaker_attribution"]["degraded_reason"]


def test_transcribe_file_uses_real_decoder_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实模板 + 真实解码器（faster_whisper）自比必须=self。

    【2026-10-17 修】原来断言「模板前 5 秒对模板本身的相似度 > 0.9」。
    实测这个假设是错的：模板 25.9 秒，前 5 秒带起录静音/底噪，CAM++ 自比只有
    0.685，而 5~25 秒各窗口都是 0.91~0.95、整段自比 1.000。
    也就是说**产品行为是对的**（仍判 self，只是把低分窗口标了 review_required），
    错的是测试拿了个边界窗口当成“任意窗口都应 >0.9”。

    现改为锁真正重要的语义：
      1. 任意窗口自比都必须判 self（不把本人认成别人）；
      2. 清楚的语音窗口相似度 > 0.9；
      3. 整段自比 ≈ 1.0（声纹管线没有错位）；
      4. 低分窗口必须进入 review 而不是静默放心。
    """
    audio = pytest.importorskip("faster_whisper.audio")
    resolved, _ = resolve_template_path()
    if resolved is None:
        pytest.skip("仓库没有可用的声纹模板")

    attributor = MossSpeakerAttributor(template_path=resolved, decode=audio.decode_audio)
    assert attributor.available, attributor.degraded_reason

    signal = audio.decode_audio(str(resolved), sampling_rate=SAMPLE_RATE)
    duration = len(signal) / SAMPLE_RATE
    assert duration >= 20, "声纹模板太短，无法做多窗口自比"

    windows = [(0.0, 5.0), (5.0, 10.0), (10.0, 15.0), (15.0, 20.0)]
    records = score_segments(attributor, signal, windows)

    # 1) 本人任意窗口都不能被认成别人
    assert [r["source_label"] for r in records] == ["self"] * len(windows)
    assert all(r["voice_similarity"] is not None for r in records)

    # 2) 避开起录静音的窗口必须高分
    clean_scores = [r["voice_similarity"] for r in records[1:]]
    assert min(clean_scores) > 0.9, clean_scores

    # 3) 整段自比应接近 1.0
    whole = score_segments(attributor, signal, [(0.0, duration)])[0]
    assert whole["voice_similarity"] > 0.98, whole["voice_similarity"]

    # 4) 低分窗口应被标记复核，而不是静默当可信
    for record in records:
        if record["voice_similarity"] <= 0.9:
            assert record["review_required"] is True


# ---------------------------------------------------------------------------
# 7) ASR 产物：归属字段必须真的写进下游会读的 transcript.json
# ---------------------------------------------------------------------------


def test_transcribe_file_scores_multi_chunk_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多个 chunk：每段用自己 chunk 的音频切窗，归属记录仍与段一一对齐。"""
    template = _template_file(tmp_path)
    input_wav = _write_wav(tmp_path / "recording.wav", _voice_like(120.0, 4.0, seed=71))
    chunk_self = _write_wav(tmp_path / "chunk-000.wav", _voice_like(120.0, 4.0, seed=71))
    chunk_other = _write_wav(
        tmp_path / "chunk-001.wav",
        _voice_like(210.0, 4.0, formants=(350.0, 2200.0, 3400.0), seed=72),
    )

    monkeypatch.setattr(moss_transcribe, "moss_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        moss_transcribe,
        "_to_wav16",
        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()), True)[1],
    )
    monkeypatch.setattr(moss_transcribe, "_duration", lambda path: 3600.0)
    monkeypatch.setattr(moss_transcribe, "_rms_of", lambda path: 0.02)
    monkeypatch.setattr(
        moss_transcribe,
        "_split",
        lambda path, outdir, dur: [(chunk_self, 0.0), (chunk_other, 1800.0)],
    )

    def _fake_run_moss(chunk, outp, hotwords=None):
        text = (
            "[S01] 这是第一段。"
            if chunk.name == "chunk-000.wav"
            else "[S02] 这是第二段。"
        )
        return {
            "language": "zh",
            "segments": [{"text": text, "start": 0.5, "end": 3.5, "speaker_id": "S01"}],
        }

    monkeypatch.setattr(moss_transcribe, "_run_moss", _fake_run_moss)
    _inject_test_decoder(monkeypatch)

    segments, stats = transcribe_file(
        input_wav,
        recording_id="rec-multi",
        template_path=template,
        config_path=tmp_path / "none-config.json",
    )

    assert len(segments) == 2
    assert segments[0].start_offset_seconds == pytest.approx(0.5)
    assert segments[1].start_offset_seconds == pytest.approx(1800.5)
    attributions = stats["attributions"]
    assert len(attributions) == 2
    asserts = [
        attributions[segments[0].segment_id]["source_label"],
        attributions[segments[1].segment_id]["source_label"],
    ]
    assert asserts[0] == "self"
    assert asserts[1] != "self"
    # 第二个 chunk 的音频不能被第一个 chunk 的窗口误用
    assert attributions[segments[1].segment_id]["voice_similarity"] is not None


def test_daily_transcript_merge_keeps_attribution_fields(tmp_path: Path) -> None:
    from scripts.run_cloud_asr_pipeline import _merge_attribution_into_transcript

    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        json.dumps(
            [
                {"segment_id": "rec-1-moss-00000", "text": "第一段"},
                {"segment_id": "rec-1-moss-00001", "text": "第二段"},
                {"segment_id": "rec-1-moss-00099", "text": "没归属的段"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    attributions = {
        "rec-1-moss-00000": {
            "source_label": "self",
            "voice_similarity": 0.91,
            "acoustic_proximity": "near",
            "review_required": False,
            "decision_reason": "above_self_threshold",
        },
        "rec-1-moss-00001": {
            "source_label": "unknown",
            "voice_similarity": None,
            "acoustic_proximity": "uncertain",
            "review_required": True,
            "decision_reason": "segment_too_short",
        },
    }

    merged = _merge_attribution_into_transcript(transcript, attributions)

    assert merged == 2
    rows = json.loads(transcript.read_text(encoding="utf-8"))
    assert rows[0]["source_label"] == "self"
    assert rows[0]["voice_similarity"] == 0.91
    assert rows[1]["decision_reason"] == "segment_too_short"
    assert "source_label" not in rows[2]
    assert rows[2]["text"] == "没归属的段"


def test_daily_transcript_merge_tolerates_broken_input(tmp_path: Path) -> None:
    from scripts.run_cloud_asr_pipeline import _merge_attribution_into_transcript

    broken = tmp_path / "transcript.json"
    broken.write_text("{not json", encoding="utf-8")

    assert _merge_attribution_into_transcript(broken, {"x": {"source_label": "self"}}) == 0
    assert (
        _merge_attribution_into_transcript(tmp_path / "missing.json", {}) == 0
    )
