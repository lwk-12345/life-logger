import hashlib
import json

import pytest

from scripts.publish_activity_preview import build_payload


def replay(tmp_path):
    row = {"source_id": "s", "segment_id": "seg", "row_id": 1, "text": "original words"}
    raw = (json.dumps(row) + "\n").encode()
    (tmp_path / "input.jsonl").write_bytes(raw)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "date": "2026-10-26", "route": "jev", "snapshot_cutoff": "2026-10-26T10:00:00+08:00",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
    }))
    evidence = {k: row[k] for k in ("source_id", "segment_id", "row_id")}
    evidence["quote"] = "original words"
    item = {"item_id": "unassociated", "action": "original words", "evidence": [evidence]}
    result = {"activities": [], "steps": [{"response": {"unassociated_items": [item]}}]}
    (tmp_path / "results.json").write_text(json.dumps(result))
    (tmp_path / "summary.json").write_text(json.dumps({"status": "complete", "failed": 0}))
    return result


def test_publish_recovers_unassociated_outputs_and_does_not_guess_amount(tmp_path):
    result = replay(tmp_path)
    result["steps"].append(result["steps"][0])
    (tmp_path / "results.json").write_text(json.dumps(result))
    payload = build_payload(tmp_path)
    assert len(payload["unassociated_items"]) == 1
    assert payload["unassociated_items"][0].get("amount") is None
    assert payload["coverage"]["source_count"] == 1


def test_publish_rejects_wrong_coordinate_even_when_quote_exists(tmp_path):
    result = replay(tmp_path)
    result["steps"][0]["response"]["unassociated_items"][0]["evidence"][0]["row_id"] = 2
    (tmp_path / "results.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="Invalid source reference"):
        build_payload(tmp_path)


def test_publish_rejects_changed_frozen_input(tmp_path):
    replay(tmp_path)
    (tmp_path / "input.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_payload(tmp_path)


def test_unknown_item_time_cannot_gain_occurrence_from_activity_header(tmp_path):
    result = replay(tmp_path)
    item = result["steps"][0]["response"]["unassociated_items"][0]
    result["activities"] = [{"activity_id": "a", "occurrence_start": "2026-10-26T08:00:00+08:00",
                             "occurrence_end": "2026-10-26T08:10:00+08:00", "items": [item]}]
    (tmp_path / "results.json").write_text(json.dumps(result))
    activity = build_payload(tmp_path)["activities"][0]
    assert activity["occurrence_start"] is None
    assert activity["occurrence_end"] is None
