"""Resident MOSS: in-process payload parity and watch_formal_asr wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


from life_logger import asr_resident
from life_logger.domain import TranscriptSegment
from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WatchSessionStore


def test_output_payload_keeps_sentence_speaker_shape():
    output = SimpleNamespace(
        text="[S01]你好 [S02]再见",
        sentences=[SimpleNamespace(text="[S01]你好", start=0.0, end=1.2, speaker_id="S01"),
                   SimpleNamespace(text="[S02]再见", start=1.2, end=2.0, speaker_id="S02")],
    )
    payload = asr_resident._output_payload(output)
    assert payload["text"].startswith("[S01]你好")
    assert [s["speaker_id"] for s in payload["sentences"]] == ["S01", "S02"]
    assert payload["sentences"][0]["end"] == 1.2


def test_default_transcribe_uses_4096_limit_and_same_prompt():
    calls = {}

    class FakeMoss:
        def generate(self, audio, *, max_tokens, prompt):
            calls.update(audio=audio, max_tokens=max_tokens, prompt=prompt)
            return SimpleNamespace(text="文本", sentences=[])

    result = asr_resident._default_transcribe(wav=Path("/tmp/a.wav"), outdir=Path("/tmp"),
                                              hotwords="卧推、上斜卧推", moss=FakeMoss(), campplus=None)
    assert calls["max_tokens"] == 4096
    assert "热词提示：卧推、上斜卧推" in calls["prompt"]
    assert result["text"] == "文本"


def _segment(seq=0):
    return TranscriptSegment(
        segment_id=f"rec-{seq}-moss-00000", recording_id=f"rec-{seq}",
        start_offset_seconds=0.25, end_offset_seconds=1.5, absolute_started_at=None,
        absolute_ended_at=None, text="这一段来自常驻 MOSS", source="moss-transcribe-diarize",
        language="zh", transcription_confidence=None, speaker="说话人一",
    )


def test_watch_processor_writes_artifacts_that_reader_accepts(tmp_path, monkeypatch):
    store = WatchSessionStore(tmp_path / "watch-sessions")
    store.start_session(device_id="watch-x", session_id="session-x", started_at="2026-11-07T10:00:00+08:00")
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")
    processor = WatchFormalASRProcessor(store, tmp_path / "config.json", __import__("threading").Event())

    import life_logger.moss_transcribe as moss

    def fake_transcribe(path, *, recording_id, hotwords=None):
        assert hotwords == "卧推、飞鸟"
        return [_segment()], {"attributions": {"rec-0-moss-00000": {
            "source_label": "self", "voice_similarity": 0.71, "acoustic_proximity": "near",
            "review_required": False, "decision_reason": "campplus", "attribution_granularity": "speaker_group",
        }}}

    monkeypatch.setattr(moss, "transcribe_file", fake_transcribe)
    output = tmp_path / "out"
    output.mkdir()
    assert processor._run_resident_moss_segment(
        audio=audio, output=output, start_at="2026-11-07T10:00:00+08:00",
        timezone_name="Asia/Shanghai", duration_ms=60000, config={"moss_hotwords": "卧推、飞鸟"},
    )
    rows = processor._read_segment_result(output, 0, 0, "2026-11-07T10:00:00+08:00")
    assert len(rows) == 1
    assert rows[0]["text"] == "这一段来自常驻 MOSS"
    assert rows[0]["source_label"] == "self"
    assert rows[0]["voice_similarity"] == 0.71
    assert rows[0]["attribution_granularity"] == "speaker_group"
    assert rows[0]["speaker"] == "说话人一"


def test_resident_failure_falls_back_to_command(tmp_path, monkeypatch):
    import hashlib
    import threading

    import life_logger.watch_formal_asr as module

    store = WatchSessionStore(tmp_path / "watch-sessions")
    store.start_session(device_id="watch-y", session_id="session-y", started_at="2026-11-07T10:00:00+08:00")
    body = b"segment-bytes"
    store.receive_segment(device_id="watch-y", session_id="session-y", sequence=0,
                          start_offset_ms=0, duration_ms=2000,
                          checksum="sha256:" + hashlib.sha256(body).hexdigest(), body=body)
    processor = WatchFormalASRProcessor(store, tmp_path / "config.json", threading.Event(),
                                        slot_acquirer=lambda owner: {"pid": None}, slot_releaser=lambda slot: None)
    monkeypatch.setattr(module, "load_local_config", lambda _path: {"asr_provider": "moss"})
    monkeypatch.setattr(module, "validate_local_pipeline_config", lambda _config: None)
    monkeypatch.setattr(module, "local_pipeline_env", lambda _config: __import__("os").environ.copy())
    monkeypatch.setattr(module, "is_segment_silence", lambda *a, **k: False)
    monkeypatch.setattr("life_logger.asr_resident.resident_enabled", lambda *a, **k: True)
    monkeypatch.setattr(processor, "_run_resident_moss_segment", lambda **kwargs: False)

    called = []
    def fake_command(**kwargs):
        called.append(kwargs)
        return [sys.executable, "-c", "import sys;sys.exit(3)"]
    monkeypatch.setattr(module, "build_asr_command", fake_command)
    monkeypatch.setattr(module, "_DEFAULT_BUILD_ASR_COMMAND", fake_command)

    manifest = store.load_session("watch-y", "session-y")
    assert processor._process_segment("watch-y", "session-y", manifest, manifest["segments"][0])
    assert called, "resident 失败后应回退到旧命令路径"


def test_resident_success_skips_legacy_command(tmp_path, monkeypatch):
    import hashlib
    import threading

    import life_logger.watch_formal_asr as module

    store = WatchSessionStore(tmp_path / "watch-sessions")
    store.start_session(device_id="watch-z", session_id="session-z", started_at="2026-11-07T10:00:00+08:00")
    body = b"segment-bytes-z"
    store.receive_segment(device_id="watch-z", session_id="session-z", sequence=0,
                          start_offset_ms=0, duration_ms=2000,
                          checksum="sha256:" + hashlib.sha256(body).hexdigest(), body=body)
    processor = WatchFormalASRProcessor(store, tmp_path / "config.json", threading.Event(),
                                        slot_acquirer=lambda owner: {"pid": None}, slot_releaser=lambda slot: None)
    monkeypatch.setattr(module, "load_local_config", lambda _path: {"asr_provider": "moss"})
    monkeypatch.setattr(module, "validate_local_pipeline_config", lambda _config: None)
    monkeypatch.setattr(module, "local_pipeline_env", lambda _config: __import__("os").environ.copy())
    monkeypatch.setattr(module, "is_segment_silence", lambda *a, **k: False)
    monkeypatch.setattr("life_logger.asr_resident.resident_enabled", lambda *a, **k: True)

    def fake_resident(**kwargs):
        out = kwargs["output"]
        out.mkdir(parents=True, exist_ok=True)
        rows = [{"start_offset_seconds": 0.1, "end_offset_seconds": 1.0, "text": "常驻结果",
                 "source": "moss-transcribe-diarize", "language": "zh", "speaker": "说话人一"}]
        (out / "transcript.json").write_text(json.dumps(rows, ensure_ascii=False))
        (out / "recordings.json").write_text(json.dumps([{"recording_id": "x"}]))
        (out / "run-summary.json").write_text(json.dumps({"status": "completed", "transcript_count": 1}))
        return True

    monkeypatch.setattr(processor, "_run_resident_moss_segment", fake_resident)
    called = []
    fake_command = lambda **k: called.append(k) or ["false"]
    monkeypatch.setattr(module, "build_asr_command", fake_command)
    monkeypatch.setattr(module, "_DEFAULT_BUILD_ASR_COMMAND", fake_command)
    manifest = store.load_session("watch-z", "session-z")
    assert processor._process_segment("watch-z", "session-z", manifest, manifest["segments"][0])
    assert called == []
    state = json.loads((store.session_directory("watch-z", "session-z") /
                        "formal-asr" / "segments" / "000000" / "state.json").read_text())
    assert state["status"] == "completed" and state["rows"][0]["text"] == "常驻结果"
