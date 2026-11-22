import json
import hashlib
from life_logger.watch_stream import WatchSessionStore

def test_watch_stream_pins_and_capsules_sync_and_idempotency(tmp_path):
    store = WatchSessionStore(tmp_path / "watch-sessions")
    device_id = "test-device-s5"
    session_id = "test-session-pin-sync"

    # 1. Start session
    reg = store.start_session(
        device_id=device_id,
        session_id=session_id,
        started_at="2026-10-17T10:00:00+08:00",
        codec="m4a-aac",
    )
    assert not reg.get("duplicate")

    # 2. Upload segment with multi-pins in X-LifeLogger-Pins header
    dummy_audio = b"fake-aac-audio-bytes-for-pin-test" * 10
    checksum = f"sha256:{hashlib.sha256(dummy_audio).hexdigest()}"
    pins = [
        {"pin_id": "pin_001", "type": "double_pinch", "offset_ms": 5000, "timestamp": "2026-10-17T10:00:05+08:00"},
        {"pin_id": "pin_002", "type": "double_pinch", "offset_ms": 18200, "timestamp": "2026-10-17T10:00:18+08:00"}
    ]
    headers = {
        "X-LifeLogger-Checksum": checksum,
        "X-LifeLogger-Pins": json.dumps(pins),
    }

    res_seg = store.receive_segment(
        device_id=device_id,
        session_id=session_id,
        sequence=0,
        start_offset_ms=0,
        duration_ms=30000,
        checksum=checksum,
        body=dummy_audio,
        headers=headers,
    )
    assert res_seg["ok"] is True

    # Verify pins are read back from session manifest
    saved_pins = store.get_session_pins(device_id, session_id)
    assert len(saved_pins) == 2
    assert saved_pins[0]["pin_id"] == "pin_001"
    assert saved_pins[1]["pin_id"] == "pin_002"

    # Idempotent re-upload of same segment does not duplicate pins
    store.receive_segment(
        device_id=device_id,
        session_id=session_id,
        sequence=0,
        start_offset_ms=0,
        duration_ms=30000,
        checksum=checksum,
        body=dummy_audio,
        headers=headers,
    )
    saved_pins_after_retry = store.get_session_pins(device_id, session_id)
    assert len(saved_pins_after_retry) == 2

    # 3. Complete session carrying capsules
    capsules = [
        {
            "start_offset_ms": 12000,
            "end_offset_ms": 28500,
            "started_at": "2026-10-17T10:00:12+08:00",
            "ended_at": "2026-10-17T10:00:28+08:00"
        }
    ]
    comp = store.complete_session(
        device_id=device_id,
        session_id=session_id,
        expected_segments=1,
        ended_at="2026-10-17T10:00:30+08:00",
        capsules=capsules,
    )
    assert comp["complete"] is True

    # Verify capsules read back accurately
    saved_capsules = store.get_session_capsules(device_id, session_id)
    assert len(saved_capsules) == 1
    assert saved_capsules[0]["start_offset_ms"] == 12000
    assert saved_capsules[0]["end_offset_ms"] == 28500
