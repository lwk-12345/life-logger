"""Single-path extraction artifacts remain visible in pending review (offline)."""
from __future__ import annotations

import json
from pathlib import Path

from life_logger.pending_review import collect_pending_sources


def _write_single_path_artifact(tmp_path: Path, reason: str) -> None:
    """Single-path artifacts have no comparison/variant directory."""
    result_dir = tmp_path / "jobs" / "job-20261104-000000-deadbeef" / "files" / "abc123" / "result"
    result_dir.mkdir(parents=True)
    payload = {
        "schema_version": "context-extraction-v1",
        "model": "auto/deepseek-v4.1-flash",
        "status": "degraded",
        "pending_sources": [
            {
                "source_id": "src_singlepath0001",
                "segment_id": "rec-test_seg_000001",
                "reason": reason,
                "text": "买了红塔山，一包十块。",
                "source_label": "self",
            }
        ],
        "pending_events": [],
        "formal_events": [],
    }
    (result_dir / "context-extraction.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _collected_source_ids(collected: dict) -> set[str]:
    ids: set[str] = set()
    for group in collected.get("groups") or []:
        for item in group.get("items") or []:
            ids.add(str(item.get("source_id") or ""))
    return ids


def test_single_path_artifact_pending_review_is_collected(tmp_path: Path):
    """Ambiguous identity remains in the default pending view."""
    _write_single_path_artifact(tmp_path, "ambiguous_identity")
    collected = collect_pending_sources(tmp_path)
    assert collected["totals"]["all"] == 1, collected["totals"]
    assert "src_singlepath0001" in _collected_source_ids(collected)


def test_single_path_artifact_batch_failure_is_collected_when_expanded(tmp_path: Path):
    """Batch failures are collapsed by default and visible when expanded."""
    _write_single_path_artifact(tmp_path, "deepseek_batch_failed")
    hidden = collect_pending_sources(tmp_path)
    assert hidden["totals"]["all"] == 1
    assert hidden["totals"]["open_batch_failure"] == 1
    assert "src_singlepath0001" not in _collected_source_ids(hidden), "整批失败默认应折叠"

    shown = collect_pending_sources(tmp_path, include_collapsed=True)
    assert "src_singlepath0001" in _collected_source_ids(shown)
