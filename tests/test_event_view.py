"""Regression tests for the shared canonical event read view.

These tests intentionally exercise real artifact layouts (directory date vs
occurrence date, display_timeline, live Watch sessions, variant branches)
without touching any production data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger.event_view import build_event_view


def _event(
    event_id: str,
    quote: str,
    *,
    absolute: str | None,
    reported_at: str | None = None,
    categories: list[str] | None = None,
    data: dict | None = None,
    title: str | None = None,
    relative_seconds: float | None = None,
    temporal_relation: str | None = None,
    precision: str = "exact",
    source_type: str = "audio",
    review_status: str = "unreviewed",
) -> dict:
    categories = categories or ["routine"]
    evidence: dict = {
        "quote": quote,
        "reported_at": reported_at,
    }
    if relative_seconds is not None:
        evidence["start_offset_seconds"] = relative_seconds
    if temporal_relation is not None:
        evidence["temporal_relation"] = temporal_relation
    return {
        "event_id": event_id,
        "categories": categories,
        "title": title or quote[:20],
        "time": {
            "absolute": absolute,
            "relative_seconds": relative_seconds,
            "precision": precision,
        },
        "data": data or {},
        "confidence": 0.9,
        "evidence": evidence,
        "source_type": source_type,
        "review_status": review_status,
    }


def _write_summary(
    root: Path,
    *,
    job_id: str,
    digest: str,
    directory_date: str,
    events: list[dict],
    display_timeline: list[dict] | None = None,
    warnings: list[str] | None = None,
) -> Path:
    target = (
        root
        / "jobs"
        / job_id
        / "files"
        / digest
        / "result"
        / directory_date
        / "daily_summary.json"
    )
    target.parent.mkdir(parents=True)
    payload = {
        "schema_version": "0.1",
        "date": directory_date,
        "timezone": "Asia/Shanghai",
        "event_count": len(events),
        "timeline": events,
        "category_summary": {},
        "warnings": warnings or [],
    }
    if display_timeline is not None:
        payload["display_timeline"] = display_timeline
        payload["display_timeline_count"] = len(display_timeline)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _write_session(
    root: Path,
    *,
    device: str,
    session: str,
    events: list[dict],
    job_id: str | None = None,
    started_at_local: str | None = None,
) -> Path:
    target = root / "watch-sessions" / device / session
    target.mkdir(parents=True)
    (target / "events.json").write_text(
        json.dumps(events, ensure_ascii=False), encoding="utf-8"
    )
    manifest = {
        "schema_version": "watch-session-v1",
        "device_id": device,
        "job_id": job_id,
        "completion_job_id": job_id,
        "segments": [],
    }
    if started_at_local:
        manifest["segments"].append(
            {
                "sequence": 0,
                "file": "segment-000000.m4a",
                "started_at_local": started_at_local,
                "start_offset_ms": 0,
            }
        )
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return target


def _ids(view: dict) -> list[str]:
    return [str(event.get("event_id")) for event in view["events"]]


def test_cross_midnight_summary_directory_does_not_hide_occurrence_date(
    tmp_path: Path,
) -> None:
    event = _event(
        "evt-cross-midnight",
        "晚上买了点东西",
        absolute="2026-10-13T03:29:00+08:00",
        reported_at="2026-10-13T03:29:00+08:00",
        categories=["expense"],
    )
    _write_summary(
        tmp_path,
        job_id="job-midnight",
        digest="d1",
        directory_date="2026-10-12",
        events=[event],
    )

    sameday = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="all",
    )
    previous = build_event_view(
        tmp_path,
        date_from="2026-10-12",
        date_to="2026-10-12",
        scope="settled",
        view="all",
    )

    assert _ids(sameday) == ["evt-cross-midnight"]
    assert sameday["events"][0]["date_basis"] == "occurrence"
    assert sameday["metadata"]["formal_summary_count"] == 1
    assert _ids(previous) == []


def test_single_day_equals_wide_query_slice(tmp_path: Path) -> None:
    _write_summary(
        tmp_path,
        job_id="job-a",
        digest="d1",
        directory_date="2026-10-12",
        events=[
            _event(
                "evt-day-12",
                "十二号事件",
                absolute="2026-10-12T22:00:00+08:00",
                reported_at="2026-10-12T22:00:00+08:00",
            )
        ],
    )
    _write_summary(
        tmp_path,
        job_id="job-b",
        digest="d2",
        directory_date="2026-10-12",
        events=[
            _event(
                "evt-day-13",
                "跨午夜事件",
                absolute="2026-10-13T01:30:00+08:00",
                reported_at="2026-10-13T01:30:00+08:00",
            )
        ],
    )
    wide = build_event_view(
        tmp_path,
        date_from="2026-10-11",
        date_to="2026-10-14",
        scope="settled",
        view="all",
    )
    single = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="all",
    )

    assert _ids(single) == ["evt-day-13"]
    assert _ids(single) == [
        event_id
        for event_id in _ids(wide)
        if event_id == "evt-day-13"
    ]


def test_display_timeline_empty_is_legitimate_and_all_view_sees_timeline(
    tmp_path: Path,
) -> None:
    event = _event(
        "evt-idea",
        "一条被展示筛选器排除的想法",
        absolute="2026-10-13T10:00:00+08:00",
        reported_at="2026-10-13T10:00:00+08:00",
        categories=["idea"],
    )
    _write_summary(
        tmp_path,
        job_id="job-empty-display",
        digest="d1",
        directory_date="2026-10-13",
        events=[event],
        display_timeline=[],
    )

    display = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="display",
    )
    everything = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="all",
    )

    assert display["events"] == []
    assert display["metadata"]["view_info"]["empty_display_summary_count"] == 1
    assert display["metadata"]["counts"]["settled"]["event_count"] == 0
    assert _ids(everything) == ["evt-idea"]
    assert everything["metadata"]["counts"]["settled"]["event_count"] == 1


def test_scope_settled_excludes_live_and_provisional_merges_with_provenance(
    tmp_path: Path,
) -> None:
    formal = _event(
        "evt-formal",
        "我买了一杯咖啡",
        absolute="2026-10-13T09:00:00+08:00",
        reported_at="2026-10-13T01:00:00+00:00",
        categories=["expense"],
        data={"expense": {"amount": 18.0, "currency": "CNY"}},
    )
    live_duplicate = _event(
        "evt-live-dup",
        "我买了一杯咖啡",
        absolute="2026-10-13T01:00:00+00:00",
        reported_at="2026-10-13T01:00:00+00:00",
        categories=["expense"],
        data={"expense": {"amount": 18.0, "currency": "CNY"}},
    )
    live_only = _event(
        "evt-live-only",
        "现在去健身",
        absolute="2026-10-13T18:00:00+08:00",
        reported_at="2026-10-13T10:00:00+00:00",
        categories=["exercise"],
    )
    _write_summary(
        tmp_path,
        job_id="job-linked",
        digest="d1",
        directory_date="2026-10-13",
        events=[formal],
    )
    _write_session(
        tmp_path,
        device="watch-abc",
        session="SESSION1",
        events=[live_duplicate, live_only],
        job_id="job-linked",
        started_at_local="2026-10-13T08:55:00+08:00",
    )

    settled = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="all",
    )
    provisional = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="provisional",
        view="all",
    )

    assert _ids(settled) == ["evt-formal"]
    assert settled["metadata"]["counts"]["settled"]["event_count"] == 1
    assert settled["metadata"]["counts"]["provisional"]["event_count"] == 2

    assert len(provisional["events"]) == 2
    canonical = next(
        event
        for event in provisional["events"]
        if event["event_id"] == "evt-formal"
    )
    assert provisional["metadata"]["dedup"]["cross_source_duplicate_count"] == 1
    assert [alias["event_id"] for alias in canonical["aliases"]] == ["evt-live-dup"]
    assert {item["source_kind"] for item in canonical["provenance"]} == {
        "formal",
        "live",
    }
    assert canonical["provenance"][1]["match_basis"] == "manifest_completion_job"
    assert "evt-live-only" in _ids(provisional)


def test_same_quote_at_different_occurrence_times_stays_separate(
    tmp_path: Path,
) -> None:
    formal = _event(
        "evt-formal-repeat",
        "买了一瓶水",
        absolute="2026-10-13T10:00:00+08:00",
        reported_at="2026-10-13T02:00:00+00:00",
        categories=["expense"],
    )
    live = _event(
        "evt-live-repeat",
        "买了一瓶水",
        absolute="2026-10-13T10:00:05+08:00",
        reported_at="2026-10-13T02:00:05+00:00",
        categories=["expense"],
    )
    _write_summary(
        tmp_path,
        job_id="job-repeat",
        digest="d1",
        directory_date="2026-10-13",
        events=[formal],
    )
    _write_session(
        tmp_path,
        device="watch-abc",
        session="SESSION2",
        events=[live],
        job_id="job-repeat",
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="provisional",
        view="all",
    )

    assert {event["event_id"] for event in view["events"]} == {
        "evt-formal-repeat",
        "evt-live-repeat",
    }
    assert view["metadata"]["dedup"]["cross_source_duplicate_count"] == 0


def test_variant_branch_directories_are_not_counted(tmp_path: Path) -> None:
    formal = _event(
        "evt-root",
        "根分支事件",
        absolute="2026-10-17T10:00:00+08:00",
        reported_at="2026-10-17T02:00:00+00:00",
    )
    summary = _write_summary(
        tmp_path,
        job_id="job-variant",
        digest="d1",
        directory_date="2026-10-17",
        events=[formal],
    )
    variant_dir = summary.parents[1] / "decision-variants"
    for branch in ("jev", "clef"):
        branch_dir = variant_dir / branch
        branch_dir.mkdir(parents=True)
        (branch_dir / "events.json").write_text(
            json.dumps(
                [
                    _event(
                        f"evt-{branch}",
                        f"{branch} 分支事件",
                        absolute="2026-10-17T11:00:00+08:00",
                        reported_at="2026-10-17T03:00:00+00:00",
                    )
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-17",
        date_to="2026-10-17",
        scope="provisional",
        view="all",
    )

    assert _ids(view) == ["evt-root"]
    assert view["metadata"]["model_branches"]["excluded_variant_artifacts"] == 2
    assert view["metadata"]["model_branches"]["root_only"] is True


def test_explicit_past_occurrence_is_not_overwritten_by_recording_offset(
    tmp_path: Path,
) -> None:
    past = _event(
        "evt-past",
        "昨天去爬山了",
        absolute="2026-09-20T10:00:00+08:00",
        reported_at="2026-09-22T08:00:00+08:00",
        relative_seconds=60,
        temporal_relation="past",
    )
    summary = _write_summary(
        tmp_path,
        job_id="job-past",
        digest="d1",
        directory_date="2026-09-22",
        events=[past],
    )
    status = {
        "files": [
            {
                "file_id": "d1",
                "original_name": "2026年9月22日 08_00_00.m4a",
                "effective_start_at": "2026-09-22T08:00:00+08:00",
            }
        ]
    }
    (summary.parents[4] / "status.json").write_text(
        json.dumps(status, ensure_ascii=False), encoding="utf-8"
    )

    on_occurrence_day = build_event_view(
        tmp_path,
        date_from="2026-09-20",
        date_to="2026-09-20",
        scope="settled",
        view="all",
    )
    on_report_day = build_event_view(
        tmp_path,
        date_from="2026-09-22",
        date_to="2026-09-22",
        scope="settled",
        view="all",
    )

    assert _ids(on_occurrence_day) == ["evt-past"]
    assert on_occurrence_day["events"][0]["occurred_at"] == "2026-09-20T10:00:00+08:00"
    assert on_occurrence_day["events"][0]["time"]["absolute"] == (
        "2026-09-20T10:00:00+08:00"
    )
    assert _ids(on_report_day) == []


def test_undated_fact_uses_reported_local_date_with_marker(tmp_path: Path) -> None:
    undated = _event(
        "evt-undated",
        "早上被闹钟吵醒",
        absolute=None,
        reported_at="2026-10-13T01:00:00+08:00",
        categories=["routine"],
        precision="unknown",
    )
    _write_summary(
        tmp_path,
        job_id="job-undated",
        digest="d1",
        directory_date="2026-10-12",
        events=[undated],
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-13",
        date_to="2026-10-13",
        scope="settled",
        view="all",
    )

    assert _ids(view) == ["evt-undated"]
    event = view["events"][0]
    assert event["occurrence_date"] is None
    assert event["occurred_at"] is None
    assert event["reported_date"] == "2026-10-13"
    assert event["date_basis"] == "reported_local"


def test_corrected_amount_is_not_double_counted_and_conflict_is_preserved(
    tmp_path: Path,
) -> None:
    formal = _event(
        "evt-formal-amount",
        "买了两份桂圆",
        absolute="2026-10-14T23:28:26+08:00",
        reported_at="2026-10-14T15:28:26+00:00",
        categories=["expense"],
        data={
            "expense": {
                "amount": 26.6,
                "currency": "CNY",
                "item": "桂圆",
            }
        },
    )
    live = _event(
        "evt-live-amount",
        "买了两份桂圆",
        absolute="2026-10-14T15:28:26+00:00",
        reported_at="2026-10-14T15:28:26+00:00",
        categories=["expense"],
        data={
            "expense": {
                "amount": 24.0,
                "currency": "CNY",
                "item": "桂圆",
            }
        },
    )
    _write_summary(
        tmp_path,
        job_id="job-amount",
        digest="d1",
        directory_date="2026-10-15",
        events=[formal],
    )
    _write_session(
        tmp_path,
        device="watch-abc",
        session="SESSION3",
        events=[live],
        job_id="job-amount",
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="all",
    )

    assert len(view["events"]) == 1
    event = view["events"][0]
    assert event["event_id"] == "evt-formal-amount"
    assert event["data"]["expense"]["amount"] == 26.6
    assert event["aliases"][0]["event"]["data"]["expense"]["amount"] == 24.0
    amount_conflict = next(
        item for item in event["conflicts"] if item["field"] == "expense.amount"
    )
    assert amount_conflict["formal"] == 26.6
    assert amount_conflict["live"] == 24.0


def test_unknown_nutrition_stays_unknown_and_missing_formal_is_enriched(
    tmp_path: Path,
) -> None:
    formal_unknown = _event(
        "evt-formal-food",
        "吃了午饭",
        absolute="2026-10-14T12:00:00+08:00",
        reported_at="2026-10-14T04:00:00+00:00",
        categories=["food"],
        data={"food": {"item": "午饭", "estimated_calories": None}},
    )
    live_unknown = _event(
        "evt-live-food",
        "吃了午饭",
        absolute="2026-10-14T04:00:00+00:00",
        reported_at="2026-10-14T04:00:00+00:00",
        categories=["food"],
        data={"food": {"item": "午饭", "estimated_calories": None}},
    )
    _write_summary(
        tmp_path,
        job_id="job-food",
        digest="d1",
        directory_date="2026-10-14",
        events=[formal_unknown],
    )
    _write_session(
        tmp_path,
        device="watch-abc",
        session="SESSION4",
        events=[live_unknown],
        job_id="job-food",
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="all",
    )
    event = view["events"][0]
    assert event["data"]["food"]["estimated_calories"] is None

    # If the live branch knows a value and formal left it empty, fill only the
    # missing non-null field. Formal non-null values always win.
    live_known = dict(live_unknown)
    live_known["event_id"] = "evt-live-food-known"
    live_known["data"] = {
        "food": {"item": "午饭", "estimated_calories": 620}
    }
    session_dir = tmp_path / "watch-sessions" / "watch-abc" / "SESSION4"
    (session_dir / "events.json").write_text(
        json.dumps([live_known], ensure_ascii=False), encoding="utf-8"
    )
    enriched = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="all",
    )
    assert enriched["events"][0]["data"]["food"]["estimated_calories"] == 620
    assert enriched["events"][0]["event_id"] == "evt-formal-food"


@pytest.mark.skipif(
    not (
        Path("output/local-web/jobs").is_dir()
        and Path("output/local-web/watch-sessions").is_dir()
    ),
    reason="real local-web artifacts are not present",
)
def test_real_output_has_at_least_31_cross_source_duplicate_groups() -> None:
    view = build_event_view(
        Path("output/local-web"),
        date_from="2026-10-11",
        date_to="2026-10-15",
        scope="provisional",
        view="all",
    )

    assert view["metadata"]["dedup"]["cross_source_group_count"] >= 31
    assert view["metadata"]["dedup"]["cross_source_duplicate_count"] >= 31


def test_realistic_guoyuan_entity_key_survives_formal_arrival(
    tmp_path: Path,
) -> None:
    fixture_path = Path(
        "tests/fixtures/audit_expenses_20261017/2026-10-14.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    candidates = [
        event
        for event in fixture
        if "桂圆" in str((event.get("evidence") or {}).get("quote") or "")
        and "合计" not in str(event.get("title") or "")
    ]
    formal_event = next(
        event
        for event in candidates
        if str((event.get("data") or {}).get("expense", {}).get("item") or "")
        == "桂圆一包"
    )
    live_event = next(
        event
        for event in candidates
        if str((event.get("data") or {}).get("expense", {}).get("item") or "")
        == "桂圆"
    )

    _write_session(
        tmp_path,
        device="dev",
        session="session",
        events=[live_event],
        job_id="job-one",
    )
    before = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="display",
    )
    assert len(before["events"]) == 1
    assert (
        before["events"][0]["data"]["expense"]["item"] == "桂圆"
    )

    _write_summary(
        tmp_path,
        job_id="job-one",
        digest="hash",
        directory_date="2026-10-14",
        events=[formal_event],
    )
    after = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="display",
    )

    assert len(after["events"]) == 1
    event = after["events"][0]
    assert event["event_key"] == before["events"][0]["event_key"]
    assert event["event_id"] == formal_event["event_id"]
    assert {item["event_id"] for item in event["provenance"]} == {
        formal_event["event_id"],
        live_event["event_id"],
    }
    assert len(event["aliases"]) == 1


def test_nested_nutrition_is_filled_only_when_missing_and_never_zeroed(
    tmp_path: Path,
) -> None:
    formal = _event(
        "evt-formal-food",
        "午饭吃了牛肉面",
        absolute="2026-10-14T12:00:00+08:00",
        reported_at="2026-10-14T04:00:00+00:00",
        categories=["food"],
        data={
            "food": {
                "item": "牛肉面",
                "nutrients": {
                    "energy_kcal": 0,
                    "protein_g": None,
                    "fat_g": 12.0,
                },
            }
        },
    )
    live = _event(
        "evt-live-food",
        "午饭吃了牛肉面",
        absolute="2026-10-14T04:00:00+00:00",
        reported_at="2026-10-14T04:00:00+00:00",
        categories=["food"],
        data={
            "food": {
                "item": "牛肉面",
                "nutrients": {
                    "energy_kcal": 320,
                    "protein_g": 20,
                    "fat_g": 99.0,
                },
            }
        },
    )
    _write_summary(
        tmp_path,
        job_id="job-food",
        digest="d1",
        directory_date="2026-10-14",
        events=[formal],
    )
    _write_session(
        tmp_path,
        device="watch-abc",
        session="SESSION-FOOD",
        events=[live],
        job_id="job-food",
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-14",
        date_to="2026-10-14",
        scope="provisional",
        view="all",
    )

    nutrients = view["events"][0]["data"]["food"]["nutrients"]
    assert nutrients["energy_kcal"] == 0  # explicit real zero is preserved
    assert nutrients["protein_g"] == 20  # missing formal value is filled
    assert nutrients["fat_g"] == 12.0  # formal non-null value wins
    assert view["events"][0]["enrichment_provenance"][0]["fields"] == [
        "food.nutrients.protein_g"
    ]
