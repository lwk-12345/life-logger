import json
from pathlib import Path

from life_logger.daily_brief import (
    AGENT_BRIEF_SCHEMA,
    BRIEF_CATEGORIES,
    build_daily_brief,
    default_outbox_dir,
    deliver_daily_brief,
    external_agent_brief,
    outbound_options,
)


def _write_summary(
    job_root: Path,
    *,
    job_id: str,
    digest: str,
    date: str,
    events: list[dict],
    warnings: list[str] | None = None,
) -> Path:
    target = (
        job_root
        / "jobs"
        / job_id
        / "files"
        / digest
        / "result"
        / date
        / "daily_summary.json"
    )
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "date": date,
                "timezone": "Asia/Shanghai",
                "event_count": len(events),
                "timeline": events,
                "category_summary": {},
                "warnings": warnings or [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target


def _event(event_id: str, category: str, title: str, second: int) -> dict:
    return {
        "event_id": event_id,
        "categories": [category],
        "title": title,
        "time": {
            "absolute": f"2026-09-21T08:{second:02d}:00+08:00",
            "relative_seconds": second * 60,
            "precision": "exact",
        },
        "confidence": 0.9,
        "evidence": {"segment_id": f"seg-{event_id}"},
    }


def test_daily_brief_merges_events_seven_categories_todos_and_warnings(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "local-web"
    routine = _event("evt-routine", "routine", "起床", 1)
    todo = _event("evt-todo", "todo", "检查录音结果", 2)
    expense = _event("evt-expense", "expense", "早餐 12 元", 3)
    _write_summary(
        job_root,
        job_id="job-a",
        digest="a",
        date="2026-09-21",
        events=[routine, todo],
        warnings=["one warning"],
    )
    _write_summary(
        job_root,
        job_id="job-b",
        digest="b",
        date="2026-09-21",
        events=[routine, expense],
        warnings=["one warning", "another warning"],
    )
    task_states = [
        {
            "task_key": "event:evt-todo",
            "event_id": "evt-todo",
            "title": "检查录音结果",
            "status": "in_progress",
        },
        {
            "task_key": "manual:followup",
            "event_id": None,
            "title": "联系设备供应商",
            "status": "pending",
        },
        {
            "task_key": "manual:done",
            "title": "已完成事项",
            "status": "completed",
        },
        {
            "task_key": "manual:dismissed",
            "title": "已忽略事项",
            "status": "dismissed",
        },
    ]

    brief = build_daily_brief(
        job_root,
        date="2026-09-21",
        task_states=task_states,
    )

    assert brief["event_count"] == 3
    assert tuple(brief["category_summary"]) == BRIEF_CATEGORIES
    assert brief["category_summary"]["routine"]["count"] == 1
    assert brief["category_summary"]["expense"]["count"] == 1
    assert brief["category_summary"]["food"] == {"count": 0, "highlights": []}
    assert [item["title"] for item in brief["todos"]] == [
        "检查录音结果",
        "联系设备供应商",
    ]
    assert brief["todos"][0]["status"] == "in_progress"
    assert brief["warnings"] == ["one warning", "another warning"]
    assert len(brief["source_daily_summaries"]) == 2
    assert [item["event_id"] for item in brief["events"]] == [
        "evt-routine",
        "evt-todo",
        "evt-expense",
    ]


def test_external_agent_brief_is_allowlisted_and_high_confidence_only() -> None:
    high = _event("evt-high", "expense", "早餐 12 元", 3)
    high["data"] = {"expense": {"amount": 12.0, "currency": "CNY"}}
    high["evidence"]["quote"] = "我花了十二块钱吃早餐"
    low = _event("evt-low", "idea", "半截低置信想法", 4)
    low["confidence"] = 0.72
    raw = {
        "schema_version": "daily-brief-v1",
        "date": "2026-09-21",
        "timezone": "Asia/Shanghai",
        "generated_at": "2026-09-21T23:00:00+08:00",
        "event_count": 2,
        "category_summary": {},
        "todos": [
            {
                "task_key": "event:evt-high",
                "event_id": "evt-high",
                "title": "买早餐",
                "status": "pending",
            },
            {
                "task_key": "event:evt-low",
                "event_id": "evt-low",
                "title": "低置信待办",
                "status": "pending",
            },
            {
                "task_key": "manual:1",
                "event_id": None,
                "title": "手工待办",
                "status": "pending",
            },
        ],
        "warnings": ["只发送高置信点"],
        "events": [high, low],
        "source_daily_summaries": ["/private/output/job/daily_summary.json"],
    }

    outbound = external_agent_brief(raw)

    assert outbound["schema_version"] == AGENT_BRIEF_SCHEMA
    assert outbound["event_count"] == 1
    assert [item["event_id"] for item in outbound["high_confidence_points"]] == [
        "evt-high"
    ]
    assert outbound["high_confidence_points"][0]["facts"] == {
        "expense": {"amount": 12.0, "currency": "CNY"}
    }
    assert [item["title"] for item in outbound["todos"]] == ["买早餐", "手工待办"]
    assert "早餐 12 元" in outbound["searchable_summary"]
    serialized = json.dumps(outbound, ensure_ascii=False)
    for forbidden in (
        "evidence",
        "source_daily_summaries",
        "recording_id",
        "segment_id",
        "quote",
        "/private/output",
    ):
        assert forbidden not in serialized


def test_daily_brief_writes_outbox_and_posts_optional_webhook(tmp_path: Path) -> None:
    high = _event("evt-high", "routine", "起床", 1)
    brief = {
        "schema_version": "daily-brief-v1",
        "date": "2026-09-21",
        "timezone": "Asia/Shanghai",
        "event_count": 1,
        "category_summary": {},
        "todos": [],
        "warnings": [],
        "events": [high],
        "source_daily_summaries": ["/private/raw/daily_summary.json"],
    }
    expected = external_agent_brief(brief)
    captured: dict[str, object] = {}

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    delivery = deliver_daily_brief(
        brief,
        outbox_dir=tmp_path / "outbox",
        webhook_url="http://127.0.0.1:9999/life-logger",
        opener=opener,
    )

    outbox_path = Path(delivery["outbox_path"])
    assert outbox_path.name == "life-logger-daily-brief-2026-09-21.json"
    assert json.loads(outbox_path.read_text(encoding="utf-8")) == expected
    assert outbox_path.stat().st_mode & 0o777 == 0o600
    assert captured["url"] == "http://127.0.0.1:9999/life-logger"
    assert captured["body"] == expected
    assert delivery["webhook"] == {"ok": True, "status": 204, "error": None}


def test_default_outbox_is_one_stable_directory(tmp_path: Path) -> None:
    assert default_outbox_dir(tmp_path / "local-web") == (
        tmp_path / "local-web" / "outbox" / "daily-brief"
    ).resolve()


def test_daily_brief_outbound_options_are_optional() -> None:
    outbox, webhook = outbound_options(
        {
            "daily_brief": {
                "outbox_dir": "~/life-logger-outbox",
                "webhook_url": "http://127.0.0.1:8080/hook",
            }
        }
    )

    assert outbox == Path("~/life-logger-outbox").expanduser()
    assert webhook == "http://127.0.0.1:8080/hook"
    assert outbound_options({}) == (None, None)


def test_daily_brief_does_not_silently_fall_back_when_canonical_view_fails(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest
    import life_logger.event_view as event_view_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("canonical loader failed")

    monkeypatch.setattr(event_view_module, "build_event_view", _boom)
    with pytest.raises(RuntimeError, match="canonical loader failed"):
        build_daily_brief(tmp_path, date="2026-09-21")


def test_daily_brief_reports_canonical_metadata_and_empty_display(
    tmp_path: Path,
) -> None:
    event = _event("evt-hidden", "idea", "被展示筛选器排除", 1)
    _write_summary(
        tmp_path,
        job_id="job-empty",
        digest="d1",
        date="2026-09-21",
        events=[event],
    )
    summary_path = (
        tmp_path
        / "jobs"
        / "job-empty"
        / "files"
        / "d1"
        / "result"
        / "2026-09-21"
        / "daily_summary.json"
    )
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    payload["display_timeline"] = []
    summary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    brief = build_daily_brief(tmp_path, date="2026-09-21", scope="provisional", view="display")

    assert brief["event_count"] == 0
    assert brief["events"] == []
    assert brief["accounting_scope"] == "provisional"
    assert brief["scope"] == "provisional"
    assert brief["view"] == "display"
    assert isinstance(brief["event_view"], dict)
    assert brief["event_view"]["view_info"]["empty_display_summary_count"] == 1
    assert brief["event_view"]["counts"]["settled"]["event_count"] == 0
    assert brief["counts"]["provisional"]["event_count"] == 0
    assert brief["source_daily_summaries"]


def test_daily_brief_sees_cross_midnight_event_from_previous_directory(
    tmp_path: Path,
) -> None:
    event = _event("evt-cross", "routine", "跨午夜", 1)
    event["time"]["absolute"] = "2026-09-22T01:30:00+08:00"
    event["evidence"]["reported_at"] = "2026-09-22T01:30:00+08:00"
    _write_summary(
        tmp_path,
        job_id="job-cross",
        digest="d1",
        date="2026-09-21",
        events=[event],
    )

    brief = build_daily_brief(tmp_path, date="2026-09-22")

    assert [item["event_id"] for item in brief["events"]] == ["evt-cross"]
    assert brief["events"][0]["date_basis"] == "occurrence"
    assert brief["source_daily_summaries"]
