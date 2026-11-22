"""Metadata date_basis_counts must be present for all scope/view combinations."""

from __future__ import annotations

from pathlib import Path

import pytest

from life_logger.daily_brief import build_daily_brief
from life_logger.event_view import build_event_view

REPO_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_EVENTS = {
    "evt_session_5ec9c59053d34a94",
    "evt_session_5f98eab7a61cbff4",
    "evt_session_52845a91c6b36e5c",
    "evt_session_d3142d2000a4f436",
}


def _view(scope: str, view: str) -> dict:
    return build_event_view(
        REPO_ROOT / "output" / "local-web",
        date_from="2026-10-22",
        date_to="2026-10-22",
        scope=scope,
        view=view,
    )


def test_all_scope_view_combinations_have_date_basis_counts() -> None:
    for scope in ("settled", "provisional"):
        for view in ("display", "all"):
            payload = _view(scope, view)
            metadata = payload["metadata"]
            assert metadata.get("date_basis_counts") is not None, (scope, view)
            assert metadata["counts"]["all"]["date_basis_counts"] == metadata["date_basis_counts"]
            assert metadata["counts"]["all"]["event_count"] == len(payload["events"])
            assert sum(metadata["date_basis_counts"].values()) == len(payload["events"])
            assert metadata["selected_counts"]["date_basis_counts"] == metadata["date_basis_counts"]

def test_2025_sentinel_is_artifact_directory_not_occurrence() -> None:
    payload = _view("provisional", "all")
    found = {event["event_id"]: event for event in payload["events"]}
    missing = SENTINEL_EVENTS - found.keys()
    if missing:
        pytest.skip(f"sentinel samples absent: {sorted(missing)}")
    for event_id in SENTINEL_EVENTS:
        assert found[event_id]["date_basis"] == "artifact_directory"
        assert found[event_id]["occurred_at"] is None
    counts = payload["metadata"]["date_basis_counts"]
    occurrence_ids = {
        event["event_id"] for event in payload["events"]
        if event.get("date_basis") == "occurrence"
    }
    assert not (SENTINEL_EVENTS & occurrence_ids)
    assert counts.get("occurrence", 0) == len(occurrence_ids)
    assert counts.get("artifact_directory", 0) >= len(SENTINEL_EVENTS)

def test_view_all_metadata_not_none_and_matches_selected() -> None:
    payload = _view("provisional", "all")
    metadata = payload["metadata"]
    assert metadata["view"] == "all"
    assert metadata["date_basis_counts"] is not None
    assert metadata["counts"]["all"]["date_basis_counts"] is not None


def test_daily_brief_event_view_metadata_has_counts() -> None:
    for view in ("display", "all"):
        brief = build_daily_brief(
            REPO_ROOT / "output" / "local-web",
            date="2026-10-22",
            scope="provisional",
            view=view,
        )
        metadata = brief["event_view"]
        assert metadata["date_basis_counts"] is not None
        assert metadata["counts"]["all"]["date_basis_counts"] is not None
        assert metadata["counts"]["all"]["event_count"] == len(brief["events"])
