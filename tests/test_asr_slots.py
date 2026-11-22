from pathlib import Path

import life_logger.asr_slots as asr_slots


def test_shared_default_limit_caps_watch_previews_and_full_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(asr_slots, "available_memory_gb", lambda: 16.0)
    state_path = tmp_path / "slots.json"

    first = asr_slots.acquire_asr_slot("web-1", state_path=state_path)
    second = asr_slots.acquire_asr_slot("watch-preview", state_path=state_path)
    third = asr_slots.acquire_asr_slot("web-2", state_path=state_path)

    assert first is not None
    assert second is not None
    assert third is None
    asr_slots.release_asr_slot(first, state_path=state_path)
    asr_slots.release_asr_slot(second, state_path=state_path)


def test_shared_slot_denies_admission_when_measured_headroom_is_too_low(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(asr_slots, "available_memory_gb", lambda: 4.5)

    token = asr_slots.acquire_asr_slot(
        "watch-preview", state_path=tmp_path / "slots.json"
    )

    assert token is None
