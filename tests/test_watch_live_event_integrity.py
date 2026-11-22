"""Live publication must not invent occurrence times or speaker identity."""

import hashlib
import json
import threading

from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WatchSessionStore


def setup(tmp_path, label, text):
    store = WatchSessionStore(tmp_path / "watch-sessions")
    store.start_session(
        device_id="watch", session_id="session", started_at="2026-10-16T12:00:00+08:00"
    )
    audio = b"sealed-test-audio"
    store.receive_segment(
        device_id="watch",
        session_id="session",
        sequence=0,
        start_offset_ms=0,
        duration_ms=5000,
        checksum="sha256:" + hashlib.sha256(audio).hexdigest(),
        body=audio,
    )
    directory = store.session_directory("watch", "session")
    state = directory / "formal-asr/segments/000000/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "status": "completed",
                "rows": [
                    {
                        "segment_id": "s1",
                        "source_label": label,
                        "voice_similarity": 0.4,
                        "text": text,
                        "absolute_started_at": "2026-10-16T12:00:00+08:00",
                        "start_offset_seconds": 0,
                        "end_offset_seconds": 5,
                    }
                ],
            }
        )
    )
    processor = WatchFormalASRProcessor(
        store, tmp_path / "config.json", threading.Event()
    )
    return processor, directory


def test_unknown_event_time_is_not_replaced_with_recording_time(tmp_path, monkeypatch):
    processor, directory = setup(tmp_path, "self", "之前买了个杯子。")
    monkeypatch.setattr(
        "life_logger.contextual_extraction.run_contextual_extraction",
        lambda *_args, **_kwargs: {
            "formal_events": [
                {
                    "event_id": "e1",
                    "title": "买杯子",
                    "categories": ["expense"],
                    "time": {"absolute": None, "precision": "unknown"},
                    "evidence": {
                        "reported_at": "2026-10-16T12:00:00+08:00",
                        "quote": "之前买了个杯子。",
                    },
                }
            ],
        },
    )
    processor._sync_live_session_events(
        "watch", "session", {"deepseek": {"api_key": "offline"}}
    )
    events = json.loads((directory / "events.json").read_text())
    assert events[0]["time"]["absolute"] is None
    assert events[0]["time"]["precision"] == "unknown"
    assert events[0]["evidence"]["reported_at"] == "2026-10-16T12:00:00+08:00"


def test_memo_fallback_cannot_promote_unknown_speaker_to_self(tmp_path, monkeypatch):
    processor, directory = setup(tmp_path, "unknown", "记录一下我昨天买了包烟。")
    monkeypatch.setattr(
        "life_logger.contextual_extraction.run_contextual_extraction",
        lambda *_args, **_kwargs: {"formal_events": []},
    )
    processor._sync_live_session_events(
        "watch", "session", {"deepseek": {"api_key": "offline"}}
    )
    assert json.loads((directory / "events.json").read_text()) == []
