"""Regression test asserting the 2026-10-16 20:21 user golden benchmark sample.

Verifies:
1. Voiceprint attribution: quiet home watch recording achieves >=0.50 similarity and self label.
2. Filter integrity: testing speech ('对着手表说话', '场景是在家里') is rejected as non-events.
3. Feeling extraction: user observation ('现在是就是正常，就是受不了，就是受不了这个。') is extracted as routine.
"""

from __future__ import annotations

import json
from pathlib import Path

from life_logger.contextual_extraction import _event_from_model

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden-samples" / "2026-10-16-quiet-watch"


def test_golden_sample_acoustic_voiceprint_is_solidly_self():
    attr_path = FIXTURE_DIR / "source-attribution.json"
    rows = json.loads(attr_path.read_text(encoding="utf-8"))
    assert len(rows) == 20

    # Every sentence spoken directly to watch must be categorized as self
    self_rows = [r for r in rows if r.get("source_label") == "self"]
    assert len(self_rows) == 20

    # Mean voice similarity must be well above the 0.40 threshold
    similarities = [r.get("voice_similarity") for r in rows if r.get("voice_similarity") is not None]
    assert similarities
    assert sum(similarities) / len(similarities) >= 0.50


def test_golden_sample_filters_testing_phrases_and_extracts_discomfort_event():
    attr_path = FIXTURE_DIR / "source-attribution.json"
    rows = json.loads(attr_path.read_text(encoding="utf-8"))

    # Testing remark should not produce an event
    testing_row = dict(
        next(r for r in rows if "对着手表说话" in r["text"]),
        source_id="src_testing",
        recording_id="rec_test",
    )
    fake_event = {
        "category": "routine",
        "event_state": "completed",
        "title": "对着手表说话",
        "quote": "对着手表说话",
        "confidence": 0.95,
        "data": {},
    }
    # Should be rejected by meta debug guard or non-event
    evt, reason = _event_from_model(fake_event, testing_row)
    assert evt is None or reason in {"rejected_meta_debug_or_stream_noise", "not_confirmed_event"}

    # Genuine discomfort remark produces a valid event
    discomfort_row = dict(
        next(r for r in rows if "就是受不了这个" in r["text"]),
        source_id="src_discomfort",
        recording_id="rec_test",
    )
    valid_event = {
        "category": "routine",
        "event_state": "ongoing",
        "title": "忍受不适",
        "quote": "现在是就是正常，就是受不了，就是受不了这个。",
        "confidence": 0.85,
        "data": {"routine": {"description": "忍受不适"}},
    }
    event_obj, reason_valid = _event_from_model(valid_event, discomfort_row)
    assert event_obj is not None
    assert event_obj["title"] == "忍受不适"
    assert event_obj["categories"] == ["routine"]
