"""上游音频分类接线测试：复用上游声纹的 attribute 分支不再产出空窗口。

背景（2026-10-14 实测）：job-20261014-031125-74bc30d9 的
``result/audio-classification.json`` 里 ``windows`` 恒为 ``[]``，run-summary 也是
``audio_classifier.window_count = 0``，而看板场景统计读的就是这些窗口。

根因（本文件锁住它）：音频分类只在 ``attribute_funasr_transcript`` 的「重算分支」
里做，而实时切片 / MOSS 链路（row 里已经带声纹，走「复用分支」）从不写
``audio_content_*`` 字段 —— 复用分支没有可复用的数据，于是恒空。

修法（方向 B）：上游在算声纹时顺手分类。MOSS（``moss_transcribe``）与 Whisper
（``transcribe``）本来就把整段/chunk 音频解码在内存里，切 10 秒窗做纯 NumPy
特征不需要额外解码；字段随 transcript.json 的 row 流到 attribute 的复用分支，
那里既有的 ``_audio_classification_windows`` 重建逻辑原样可用。
"""

from __future__ import annotations

import json
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from life_logger import moss_transcribe
from life_logger.audio_classifier import (
    AUDIO_CLASSIFIER_RECORD_FIELDS,
    annotate_record_with_window_classification,
    annotate_records_with_audio_classification,
    annotate_records_with_window_classification,
    classify_audio_window,
    shift_window_classification,
)
from life_logger.moss_transcribe import transcribe_file

SAMPLE_RATE = 16_000


# ---------------------------------------------------------------------------
# 工具：与 tests/test_audio_classifier.py 同一套可判定的合成信号
# ---------------------------------------------------------------------------


def _signals():
    t = np.arange(SAMPLE_RATE * 10, dtype=np.float32) / SAMPLE_RATE
    music = (
        0.25 * np.sin(2 * np.pi * 440 * t)
        + 0.18 * np.sin(2 * np.pi * 660 * t)
        + 0.12 * np.sin(2 * np.pi * 880 * t)
    ).astype(np.float32)
    envelope = (
        ((t % 0.55) < 0.24).astype(np.float32)
        * (0.55 + 0.45 * np.sin(2 * np.pi * 1.3 * t) ** 2)
    )
    rng = np.random.default_rng(7)
    speech = (
        (
            0.28
            * np.sin(2 * np.pi * (170 + 25 * np.sin(2 * np.pi * 2.2 * t)) * t)
            + 0.08 * rng.normal(size=t.size)
        )
        * envelope
    ).astype(np.float32)
    return speech, music


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
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if rate != sampling_rate:
        raise ValueError(f"unexpected sample rate: {rate}")
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def _span(segment_id: str, start: float, end: float) -> dict:
    """10 秒窗定位用的最小记录（只有时间戳 + id）。"""
    return {
        "segment_id": segment_id,
        "start_offset_seconds": start,
        "end_offset_seconds": end,
    }


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _inject_test_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    real_cls = moss_transcribe.MossSpeakerAttributor
    monkeypatch.setattr(
        moss_transcribe,
        "MossSpeakerAttributor",
        lambda **kwargs: real_cls(decode=_decode_wav, **kwargs),
    )


# ---------------------------------------------------------------------------
# 1) 分类字段名 / 新旧 API 行为一致（重构不许改变既有语义）
# ---------------------------------------------------------------------------


def test_upstream_field_names_match_both_annotators() -> None:
    speech, music = _signals()
    record = _span("w0", 1.0, 2.0)

    legacy, legacy_windows = annotate_records_with_audio_classification(
        [record], music, window_start_seconds=0.0, sample_rate=SAMPLE_RATE
    )
    upstream, upstream_windows = annotate_records_with_window_classification(
        [record], music, spans=[(1.0, 2.0)], signal_start_seconds=0.0
    )

    assert set(AUDIO_CLASSIFIER_RECORD_FIELDS) <= set(legacy[0])
    assert set(AUDIO_CLASSIFIER_RECORD_FIELDS) <= set(upstream[0])
    assert legacy[0]["audio_content_class"] == "music"
    assert upstream[0]["audio_content_class"] == "music"
    assert upstream[0]["audio_content_scores"] == legacy[0]["audio_content_scores"]
    assert upstream[0]["audio_priority_weight"] == legacy[0]["audio_priority_weight"]
    assert (
        upstream[0]["audio_processing_priority"]
        == legacy[0]["audio_processing_priority"]
    )
    assert upstream_windows[0]["window_index"] == legacy_windows[0]["window_index"] == 0
    # 源记录本身不被就地改写（返回副本）
    assert "audio_content_class" not in record


# ---------------------------------------------------------------------------
# 2) 10 秒窗网格 / 信号起点 / 边界交集
# ---------------------------------------------------------------------------


def test_window_classification_uses_absolute_window_grid() -> None:
    speech, music = _signals()
    signal = np.concatenate([music, speech])
    records = [
        _span("music", 1.0, 2.0),
        _span("speech", 12.0, 13.0),
    ]

    annotated, windows = annotate_records_with_window_classification(records, signal)

    assert [w["window_index"] for w in windows] == [0, 1]
    assert windows[0]["window_start_seconds"] == 0.0
    assert windows[0]["window_end_seconds"] == 10.0
    assert windows[1]["window_start_seconds"] == 10.0
    assert annotated[0]["audio_content_class"] == "music"
    assert annotated[1]["audio_content_class"] == "speech"

    # 同一个窗里的多条记录共用一次判定（不重复计算）
    same_window = [
        _span("a", 12.0, 13.0),
        _span("b", 18.0, 19.0),
    ]
    annotated_same, windows_same = annotate_records_with_window_classification(
        same_window, signal
    )
    assert len(windows_same) == 1
    assert (
        annotated_same[0]["audio_content_scores"]
        == annotated_same[1]["audio_content_scores"]
    )


def test_window_classification_honours_signal_start_seconds() -> None:
    """chunk 音频只覆盖整段录音的一段：窗口要按整段录音时间轴定位。"""
    speech, music = _signals()
    signal = np.concatenate([music, speech])  # 覆盖 [100s, 120s)
    records = [
        _span("music", 101.0, 102.0),
        _span("speech", 112.0, 113.0),
    ]

    annotated, windows = annotate_records_with_window_classification(
        records, signal, signal_start_seconds=100.0
    )

    assert [w["window_index"] for w in windows] == [10, 11]
    assert windows[0]["window_start_seconds"] == 100.0
    assert windows[1]["window_start_seconds"] == 110.0
    assert annotated[0]["audio_content_class"] == "music"
    assert annotated[1]["audio_content_class"] == "speech"


def test_window_classification_slices_edge_windows_to_the_intersection() -> None:
    """窗口只被信号覆盖一部分时，用交集判定；交集过短则什么都不写。"""
    speech, music = _signals()
    signal = np.concatenate([music[: 5 * SAMPLE_RATE], speech])  # 覆盖 [5s, 25s)
    records = [
        _span("edge", 6.0, 7.0),
        _span("inside", 15.0, 16.0),
    ]

    annotated, windows = annotate_records_with_window_classification(
        records, signal, signal_start_seconds=5.0
    )

    assert [w["window_index"] for w in windows] == [0, 1]
    # 窗口 0 只有 [5s,10s) 这 5 秒在信号里 —— 用交集判定（音乐），而不是错位取 10 秒
    assert annotated[0]["audio_content_class"] == "music"
    assert annotated[0]["audio_classifier_window_start_seconds"] == 0.0
    assert annotated[1]["audio_content_class"] == "speech"


def test_window_classification_skips_windows_without_enough_audio() -> None:
    speech, music = _signals()
    signal = np.concatenate([music, speech])  # 覆盖 [0s, 20s)
    records = [
        # 完全没有时间戳，且没给 spans → 不许瞎猜
        {"segment_id": "no-time"},
        # 窗口整个落在信号之外（100s 处的 10 秒窗）
        _span("outside", 100.0, 101.0),
        # 交集只有 0.1 秒（窗口 1 与信号 [0,20.1s) 的交集）
        _span("sliver", 20.05, 20.09),
    ]

    annotated, windows = annotate_records_with_window_classification(records, signal)

    assert windows == []
    for record in annotated:
        assert not set(AUDIO_CLASSIFIER_RECORD_FIELDS) & set(record)


def test_single_record_wrapper_matches_batch_api() -> None:
    speech, music = _signals()
    signal = np.concatenate([music, speech])

    single = annotate_record_with_window_classification(
        {"segment_id": "one"}, signal, start_seconds=12.0, end_seconds=13.0
    )

    assert single["audio_content_class"] == "speech"
    assert single["audio_classifier_window_index"] == 1
    assert single["segment_id"] == "one"


# ---------------------------------------------------------------------------
# 3) MOSS 上游：段级归属记录带上分类字段（复用分支的数据来源）
# ---------------------------------------------------------------------------


def _run_fake_moss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    chunks: list[tuple[Path, float]] | None = None,
    payload: dict | None = None,
    payload_for: dict[str, dict] | None = None,
) -> tuple[list, dict]:
    template = _write_wav(tmp_path / "template-self.wav", _signals()[1])
    monkeypatch.setattr(moss_transcribe, "moss_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        moss_transcribe,
        "_to_wav16",
        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()), True)[1],
    )
    monkeypatch.setattr(moss_transcribe, "_duration", lambda path: 10.0)
    monkeypatch.setattr(moss_transcribe, "_rms_of", lambda path: 0.02)
    if chunks is not None:
        monkeypatch.setattr(moss_transcribe, "_split", lambda path, outdir, dur: chunks)
    monkeypatch.setattr(
        moss_transcribe,
        "_run_moss",
        lambda chunk, outp, hotwords=None: (
            payload_for.get(chunk.name) if payload_for is not None else payload
        ),
    )
    _inject_test_decoder(monkeypatch)
    return transcribe_file(
        tmp_path / "recording.wav",
        recording_id="rec-audio-class",
        template_path=template,
        config_path=tmp_path / "no-config.json",
    )


def test_moss_attribution_rows_carry_audio_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speech, music = _signals()
    _write_wav(tmp_path / "recording.wav", music)
    payload = {
        "language": "zh",
        "segments": [
            {"text": "[S01] 第一条。", "start": 0.0, "end": 2.0, "speaker_id": "S01"},
            {"text": "[S02] 第二条。", "start": 2.4, "end": 4.4, "speaker_id": "S02"},
        ],
    }

    segments, stats = _run_fake_moss(
        monkeypatch, tmp_path, payload=payload
    )

    assert len(segments) == 2
    attributions = stats["attributions"]
    for segment in segments:
        record = attributions[segment.segment_id]
        assert set(AUDIO_CLASSIFIER_RECORD_FIELDS) <= set(record)
        assert record["audio_content_class"] == "music"
        assert record["audio_classifier_window_index"] == 0
        assert record["audio_classifier_window_start_seconds"] == 0.0
        assert record["audio_classifier_window_end_seconds"] == 10.0
        assert record["audio_classifier_policy"] == "audio-content-v1"
    assert stats["audio_classification"]["window_count"] == 1
    assert stats["audio_classification"]["policy_version"] == "audio-content-v1"


def test_moss_second_chunk_windows_use_the_file_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多 chunk（长录音）时，第二个 chunk 的窗口必须落在整段录音时间轴上。"""
    speech, music = _signals()
    chunk0 = _write_wav(tmp_path / "chunk-000.wav", music)
    chunk1 = _write_wav(tmp_path / "chunk-001.wav", speech)
    _write_wav(tmp_path / "recording.wav", music[: SAMPLE_RATE])
    payload = {
        "chunk-000.wav": {
            "language": "zh",
            "segments": [
                {
                    "text": "[S01] 音乐段。",
                    "start": 0.5,
                    "end": 3.5,
                    "speaker_id": "S01",
                }
            ],
        },
        "chunk-001.wav": {
            "language": "zh",
            "segments": [
                {
                    "text": "[S01] 说话段。",
                    "start": 0.5,
                    "end": 3.5,
                    "speaker_id": "S01",
                }
            ],
        },
    }

    segments, stats = _run_fake_moss(
        monkeypatch,
        tmp_path,
        chunks=[(chunk0, 0.0), (chunk1, 1500.0)],
        payload_for=payload,
    )

    assert len(segments) == 2
    first, second = (stats["attributions"][s.segment_id] for s in segments)
    assert first["audio_classifier_window_index"] == 0
    assert first["audio_content_class"] == "music"
    # 第二个 chunk 起点 1500s → 段中点 1502s → 窗口 150（整段录音时间轴）
    assert second["audio_classifier_window_index"] == 150
    assert second["audio_classifier_window_start_seconds"] == 1500.0
    assert second["audio_content_class"] == "speech"
    assert stats["audio_classification"]["window_count"] == 2


def test_moss_degrades_without_touching_classification_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声纹降级（拿不到音频）时不许瞎写分类字段。"""
    speech, music = _signals()
    input_wav = _write_wav(tmp_path / "recording.wav", music)
    payload = {
        "language": "zh",
        "segments": [
            {
                "text": "[S01] 降级段。",
                "start": 0.0,
                "end": 2.0,
                "speaker_id": "S01",
            }
        ],
    }
    monkeypatch.setattr(moss_transcribe, "moss_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        moss_transcribe,
        "_to_wav16",
        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()), True)[1],
    )
    monkeypatch.setattr(moss_transcribe, "_duration", lambda path: 10.0)
    monkeypatch.setattr(moss_transcribe, "_rms_of", lambda path: 0.02)
    monkeypatch.setattr(
        moss_transcribe, "_run_moss", lambda chunk, outp, hotwords=None: payload
    )
    # decode 恒失败 → 归属器不可用、chunk 音频拿不到
    class _BrokenDecode:
        def __call__(self, path: str, *, sampling_rate: int = SAMPLE_RATE):
            raise RuntimeError("boom")

    real_cls = moss_transcribe.MossSpeakerAttributor
    monkeypatch.setattr(
        moss_transcribe,
        "MossSpeakerAttributor",
        lambda **kwargs: real_cls(decode=_BrokenDecode(), **kwargs),
    )

    segments, stats = transcribe_file(
        input_wav,
        recording_id="rec-degraded",
        template_path=tmp_path / "missing-template.wav",
        config_path=tmp_path / "no-config.json",
    )

    assert len(segments) == 1
    record = stats["attributions"][segments[0].segment_id]
    assert record["source_label"] == "unknown"
    assert not set(AUDIO_CLASSIFIER_RECORD_FIELDS) & set(record)
    assert stats["audio_classification"]["window_count"] == 0


# ---------------------------------------------------------------------------
# 4) 行落盘：合并白名单必须放行分类字段
# ---------------------------------------------------------------------------


def test_asr_pipeline_merge_keeps_audio_classification_fields(tmp_path: Path) -> None:
    from scripts.run_cloud_asr_pipeline import _merge_attribution_into_transcript

    transcript = tmp_path / "transcript.json"
    transcript.write_text(
        json.dumps(
            [{"segment_id": "rec-1-moss-00000", "text": "第一段"}], ensure_ascii=False
        ),
        encoding="utf-8",
    )
    attribution = {
        "source_label": "self",
        "voice_similarity": 0.8,
        "audio_content_class": "speech",
        "audio_content_scores": {
            "speech": 0.9,
            "music": 0.03,
            "noise": 0.03,
            "stream": 0.04,
        },
        "audio_classifier_features": {"rms": 0.05},
        "audio_priority_weight": 0.93,
        "audio_processing_priority": "high",
        "audio_classifier_window_index": 12,
        "audio_classifier_window_start_seconds": 120.0,
        "audio_classifier_window_end_seconds": 130.0,
        "audio_classifier_policy": "audio-content-v1",
    }

    merged = _merge_attribution_into_transcript(
        transcript, {"rec-1-moss-00000": attribution}
    )

    assert merged == 1
    row = json.loads(transcript.read_text(encoding="utf-8"))[0]
    for field in AUDIO_CLASSIFIER_RECORD_FIELDS:
        assert row[field] == attribution[field]


# ---------------------------------------------------------------------------
# 5) 实时切片：段内时间轴 → 整场会话时间轴的平移
# ---------------------------------------------------------------------------


def test_shift_window_classification_moves_onto_session_timeline() -> None:
    record = {
        "segment_id": "watch_000000_000001",
        "start_offset_seconds": 0.25,
        "audio_classifier_window_index": 0,
        "audio_classifier_window_start_seconds": 0.0,
        "audio_classifier_window_end_seconds": 10.0,
        "audio_content_class": "speech",
    }

    shifted = shift_window_classification(record, 120.0)

    assert shifted["audio_classifier_window_index"] == 12
    assert shifted["audio_classifier_window_start_seconds"] == 120.0
    assert shifted["audio_classifier_window_end_seconds"] == 130.0
    assert shifted["audio_content_class"] == "speech"
    # 原始记录不动
    assert record["audio_classifier_window_start_seconds"] == 0.0
    # 没分类字段的记录不受影响
    assert shift_window_classification({"segment_id": "x"}, 5.0) == {"segment_id": "x"}


def test_live_segment_rows_rebase_classification_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实时切片（整段 60 秒）→ 会话时间轴：窗口索引不许每段都从 0 重来。"""
    import sys

    import life_logger.watch_formal_asr as formal_asr_module
    from life_logger.watch_formal_asr import WatchFormalASRProcessor
    from life_logger.watch_stream import WatchSessionStore

    session_store = WatchSessionStore(tmp_path / "watch-sessions")
    session_store.start_session(
        device_id="watch-audio",
        session_id="session-audio",
        started_at="2026-10-14T03:00:00+08:00",
    )
    import hashlib

    for sequence, offset_ms in ((0, 0), (1, 120_000)):
        body = f"segment audio {sequence}".encode()
        session_store.receive_segment(
            device_id="watch-audio",
            session_id="session-audio",
            sequence=sequence,
            start_offset_ms=offset_ms,
            duration_ms=60_000,
            checksum="sha256:" + hashlib.sha256(body).hexdigest(),
            body=body,
        )

    rows = [
        {
            "segment_id": "seg-1",
            "start_offset_seconds": 0.25,
            "end_offset_seconds": 3.5,
            "text": "这一段实时切片已经落盘",
            "source": "moss-transcribe-diarize",
            "language": "zh",
            "source_label": "self",
            "voice_similarity": 0.8,
            "audio_content_class": "speech",
            "audio_content_scores": {
                "speech": 0.9,
                "music": 0.03,
                "noise": 0.03,
                "stream": 0.04,
            },
            "audio_priority_weight": 0.93,
            "audio_processing_priority": "high",
            "audio_classifier_window_index": 0,
            "audio_classifier_window_start_seconds": 0.0,
            "audio_classifier_window_end_seconds": 10.0,
            "audio_classifier_policy": "audio-content-v1",
        }
    ]
    asr_script = (
        "import json,pathlib,sys; "
        "root=pathlib.Path(sys.argv[1]); root.mkdir(parents=True,exist_ok=True); "
        f"rows={rows!r}; "
        "(root/'run-summary.json').write_text(json.dumps({'status':'completed',"
        "'transcript_count':len(rows)})); "
        "(root/'transcript.json').write_text(json.dumps(rows,ensure_ascii=False)); "
        "(root/'recordings.json').write_text(json.dumps([{'recording_id':'segment'}]))"
    )
    monkeypatch.setattr(formal_asr_module, "load_local_config", lambda _path: {})
    monkeypatch.setattr(
        formal_asr_module, "validate_local_pipeline_config", lambda _config: None
    )
    monkeypatch.setattr(formal_asr_module, "local_pipeline_env", lambda _config: {})
    monkeypatch.setattr(
        formal_asr_module,
        "build_asr_command",
        lambda **kwargs: [sys.executable, "-c", asr_script, str(kwargs["output_dir"])],
    )
    processor = WatchFormalASRProcessor(
        session_store,
        tmp_path / "config.json",
        threading.Event(),
        slot_acquirer=lambda owner: {"owner": owner},
        slot_releaser=lambda _slot: None,
    )

    assert processor.scan_once() is True
    assert processor.scan_once() is True

    segment_root = (
        session_store.session_directory("watch-audio", "session-audio")
        / "formal-asr/segments"
    )
    first = _read_json(segment_root / "000000/state.json")
    second = _read_json(segment_root / "000001/state.json")
    first_row = first["rows"][0]
    # 第一段（起点 0s）：窗口仍在段内时间轴
    assert first_row["audio_classifier_window_index"] == 0
    assert first_row["audio_classifier_window_start_seconds"] == pytest.approx(0.0)
    row = second["rows"][0]
    # 第二段（起点 120s）：时间戳与分类窗口一起平移到会话时间轴
    assert row["start_offset_seconds"] == pytest.approx(120.25)
    assert row["audio_classifier_window_start_seconds"] == pytest.approx(120.0)
    assert row["audio_classifier_window_end_seconds"] == pytest.approx(130.0)
    assert row["audio_classifier_window_index"] == 12
    assert row["audio_content_class"] == "speech"


# ---------------------------------------------------------------------------
# 6) Whisper（local_turbo）路径：VAD 窗归属记录同样带上分类
# ---------------------------------------------------------------------------


def test_whisper_window_attribution_carries_audio_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    from life_logger import transcribe as transcribe_module
    from life_logger.domain import RecordingSegment

    speech, music = _signals()
    quiet = np.random.default_rng(5).normal(0, 0.002, SAMPLE_RATE * 2)
    quiet = quiet.astype(np.float32)
    signal = np.concatenate([quiet, speech, music]).astype(np.float32)  # 22 秒

    fake_audio = types.ModuleType("faster_whisper.audio")
    fake_audio.decode_audio = lambda path, sampling_rate=SAMPLE_RATE: signal
    fake_vad = types.ModuleType("faster_whisper.vad")
    fake_vad.VadOptions = lambda **kwargs: kwargs
    fake_vad.get_speech_timestamps = lambda wav, options: [
        {"start": 2 * SAMPLE_RATE, "end": 12 * SAMPLE_RATE}
    ]
    fake_faster_whisper = types.ModuleType("faster_whisper")
    fake_faster_whisper.audio = fake_audio
    fake_faster_whisper.vad = fake_vad
    fake_mlx = types.ModuleType("mlx_whisper")
    fake_mlx.transcribe = lambda chunk, **kwargs: {
        "segments": [
            {
                "text": "我在健身房练腿。",
                "no_speech_prob": 0.0,
                "avg_logprob": -0.2,
                "compression_ratio": 1.0,
            }
        ],
        "language": "zh",
    }
    for name, module in (
        ("faster_whisper", fake_faster_whisper),
        ("faster_whisper.audio", fake_audio),
        ("faster_whisper.vad", fake_vad),
        ("mlx_whisper", fake_mlx),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    audio = tmp_path / "recording.m4a"
    audio.write_bytes(b"audio")
    transcriber = transcribe_module.LocalTurboCompositeTranscriber()
    recording = RecordingSegment(
        recording_id="rec-whisper",
        source_file=str(audio),
        started_at="2026-10-14T09:00:00+08:00",
        ended_at=None,
        duration_seconds=22.0,
        sequence=1,
        time_source="user",
        time_precision="exact",
        ingestion_warnings=[],
    )

    segments = transcriber.transcribe(recording)

    assert segments, "假 MLX 必须产出至少一条转写"
    attribution = transcriber._last_segment_attributions[segments[0].segment_id]
    assert set(AUDIO_CLASSIFIER_RECORD_FIELDS) <= set(attribution)
    # VAD 窗 [2s,12s] → 中点 7s → 10 秒窗 0（[0s,10s)）—— 与直接调用分类器一致
    expected = classify_audio_window(signal[: 10 * SAMPLE_RATE])
    assert attribution["audio_classifier_window_index"] == 0
    assert attribution["audio_classifier_window_start_seconds"] == 0.0
    assert attribution["audio_classifier_window_end_seconds"] == 10.0
    assert attribution["audio_content_class"] == expected["primary_class"] == "speech"
    assert attribution["audio_content_scores"] == expected["probabilities"]


# ---------------------------------------------------------------------------
# 7) 端到端：attribute 的「复用分支」真的写出非空窗口
# ---------------------------------------------------------------------------


def _upstream_rows(*, with_classification: bool) -> list[dict]:
    rows = [
        {
            "segment_id": "seg-000001",
            "recording_id": "rec-e2e",
            "start_offset_seconds": 6.75,
            "end_offset_seconds": 11.48,
            "absolute_started_at": "2026-10-14T03:11:36.750000+08:00",
            "absolute_ended_at": "2026-10-14T03:11:41.480000+08:00",
            "text": "我在健身房练腿。",
            "source": "moss-transcribe-diarize",
            "language": "zh",
            "source_label": "self",
            "voice_similarity": 0.667866,
            "acoustic_proximity": "near",
            "review_required": False,
            "decision_reason": "above_self_threshold",
        },
        {
            "segment_id": "seg-000002",
            "recording_id": "rec-e2e",
            "start_offset_seconds": 34.2,
            "end_offset_seconds": 38.9,
            "absolute_started_at": "2026-10-14T03:12:04.200000+08:00",
            "absolute_ended_at": "2026-10-14T03:12:08.900000+08:00",
            "text": "背景里在放歌。",
            "source": "moss-transcribe-diarize",
            "language": "zh",
            "source_label": "unknown",
            "voice_similarity": 0.31,
            "acoustic_proximity": "far",
            "review_required": True,
            "decision_reason": "short_segment_low_similarity_review",
        },
    ]
    if with_classification:
        rows[0].update(
            {
                "audio_classifier_policy": "audio-content-v1",
                "audio_content_class": "speech",
                "audio_content_scores": {
                    "speech": 0.91,
                    "music": 0.03,
                    "noise": 0.02,
                    "stream": 0.04,
                },
                "audio_classifier_features": {"rms": 0.062, "tonality": 0.2},
                "audio_priority_weight": 0.94,
                "audio_processing_priority": "high",
                "audio_classifier_window_index": 0,
                "audio_classifier_window_start_seconds": 0.0,
                "audio_classifier_window_end_seconds": 10.0,
            }
        )
        rows[1].update(
            {
                "audio_classifier_policy": "audio-content-v1",
                "audio_content_class": "music",
                "audio_content_scores": {
                    "speech": 0.11,
                    "music": 0.58,
                    "noise": 0.01,
                    "stream": 0.3,
                },
                "audio_classifier_features": {"rms": 0.2, "tonality": 0.5},
                "audio_priority_weight": 0.35,
                "audio_processing_priority": "low",
                "audio_classifier_window_index": 3,
                "audio_classifier_window_start_seconds": 30.0,
                "audio_classifier_window_end_seconds": 40.0,
            }
        )
    return rows


def _run_attribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict]):
    from life_logger import contextual_extraction
    from scripts import attribute_funasr_transcript

    audio = tmp_path / "recording.m4a"
    audio.write_bytes(b"audio")
    template = _write_wav(
        tmp_path / "template.wav", np.zeros(SAMPLE_RATE, dtype=np.float32)
    )
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    recordings = tmp_path / "recordings.json"
    recordings.write_text(
        json.dumps(
            [
                {
                    "recording_id": "rec-e2e",
                    "source_file": str(audio),
                    "started_at": "2026-10-14T03:11:30+08:00",
                    "ended_at": "2026-10-14T03:13:00+08:00",
                    "duration_seconds": 90.0,
                    "sequence": 0,
                    "time_source": "metadata",
                    "time_precision": "exact",
                    "ingestion_warnings": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "result"

    def fake_extraction(records, **kwargs):
        report = {
            "schema_version": "context-extraction-v1",
            "status": "completed",
            "counts": {"formal_events": 0, "pending_events": 0, "pending_sources": 0},
            "kev": {"status": "completed"},
            "batches": [],
            "formal_events": [],
            "pending_events": [],
            "pending_sources": [],
        }
        (Path(kwargs["output_dir"]) / "context-extraction.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        kwargs["progress_callback"](
            {"batch": 1, "total_batches": 1, "status": "completed"}
        )
        return report

    monkeypatch.setattr(
        contextual_extraction, "run_contextual_extraction", fake_extraction
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    args = attribute_funasr_transcript._parser().parse_args(
        [
            "--deepseek-enabled",
            "--audio",
            str(audio),
            "--transcript",
            str(transcript),
            "--recordings",
            str(recordings),
            "--template",
            str(template),
            "--speaker-model",
            "cam++",
            "--output-dir",
            str(output_dir),
        ]
    )
    summary = attribute_funasr_transcript._run(args)
    return summary, output_dir


def test_reuse_branch_publishes_upstream_audio_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary, output_dir = _run_attribution(
        tmp_path, monkeypatch, _upstream_rows(with_classification=True)
    )

    stdout = capsys.readouterr().out
    assert "直接复用实时切片级声纹" in stdout  # 走的是复用分支，不是重算分支
    payload = _read_json(output_dir / "audio-classification.json")
    assert [w["window_index"] for w in payload["windows"]] == [0, 3]
    assert payload["windows"][0]["primary_class"] == "speech"
    assert payload["windows"][1]["primary_class"] == "music"
    assert payload["windows"][1]["window_start_seconds"] == 30.0
    assert payload["windows"][0]["probabilities"]["speech"] == 0.91
    assert payload["panns_events"] is None  # PANNs 仍默认关闭
    run_summary = _read_json(output_dir / "run-summary.json")
    assert run_summary["audio_classifier"]["window_count"] == 2
    assert run_summary["audio_classifier"]["priority_counts"]["high"] == 1
    assert run_summary["audio_classifier"]["priority_counts"]["low"] == 1
    assert summary["status"] == "completed"


def test_reuse_branch_stays_empty_without_upstream_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """老产物（没有分类字段的 row）仍然是空窗口 —— 修复点在「上游写入」。"""
    _summary, output_dir = _run_attribution(
        tmp_path, monkeypatch, _upstream_rows(with_classification=False)
    )

    payload = _read_json(output_dir / "audio-classification.json")
    assert payload["windows"] == []
    run_summary = _read_json(output_dir / "run-summary.json")
    assert run_summary["audio_classifier"]["window_count"] == 0
