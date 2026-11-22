"""时间线口径统一：`/api/daily`、`/api/stats`、`/api/daily-brief` 必须同数同钱。

背景（2026-10-19 用户发火）：
同一个日期在三个页面上是三个数 —— `/api/daily` 与 `/api/stats` 走
`event_view.build_event_view()`，而 `/api/daily-brief` 走
`external_agent_brief()` 的**置信度投影**（会再丢事件），金额干脆不返回。
实测 2026-10-14：daily/stats 44 条，brief 30 条；2026-10-18：24 vs 18。

本文件锁三条不变量：
1. 同 scope/view 下三接口事件数相等、金额相等（0 差值）；
2. brief 的 `event_count` 是权威数，投影数单独叫 `projection_event_count`；
3. 区间结果里某一天的切片 == 该日单日结果（不许因为预过滤漏跨日事件）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from life_logger.local_web import JobStore, LocalWebServer, create_server
from life_logger.statistics import build_range_statistics

DATE = "2026-10-13"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def _event(identifier: str, title: str, amount: float | None, *, when: str, confidence: float = 0.95):
    payload = {
        "event_id": identifier,
        "categories": ["expense"] if amount is not None else ["routine"],
        "title": title,
        "confidence": confidence,
        "time": {"absolute": when, "precision": "exact"},
        "evidence": {"quote": title, "reported_at": when},
        "data": {},
    }
    if amount is not None:
        payload["data"] = {"expense": {"item": title, "amount": amount, "currency": "CNY"}}
    return payload


def _build_store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "web", recover=False)
    formal = [
        _event("formal-1", "正式链消费A", 10.0, when=f"{DATE}T08:00:00+08:00"),
        _event("formal-2", "正式链低置信事件", None, when=f"{DATE}T09:00:00+08:00", confidence=0.2),
    ]
    _write(
        store.root / "jobs/job-a/files/h1/result" / DATE / "daily_summary.json",
        {"timeline": formal, "display_timeline": formal, "warnings": []},
    )
    live = [
        _event("live-1", "实时链消费B", 5.5, when=f"{DATE}T10:00:00+08:00"),
        _event("live-2", "实时链低置信事件", None, when=f"{DATE}T11:00:00+08:00", confidence=0.2),
    ]
    _write(store.root / "watch-sessions/dev/sess/events.json", live)
    _write(
        store.root / "watch-sessions/dev/sess/manifest.json",
        {
            "state": "complete",
            "completion_job_id": "job-a",
            "segments": [],
            "started_at": f"{DATE}T07:00:00+08:00",
        },
    )
    return store


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    """起一个只读测试服务；后台写线程全部禁用，避免污染。"""
    for method in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                   "_warm_vip_transcriber", "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, method):
            monkeypatch.setattr(LocalWebServer, method, lambda self, *a, **k: None)
    config = tmp_path / "config.json"
    config.write_text('{"hide_dictation_from_dashboard": false}')
    store = _build_store(tmp_path)
    srv = create_server(host="127.0.0.1", port=0, store=store, config_path=config)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, store, config
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("scope", ["provisional", "settled"])
def test_three_endpoints_report_identical_counts_and_money(server, scope):
    srv, _store, _config = server
    brief = srv.build_daily_brief(date=DATE, allow_empty=True, scope=scope, view="display")
    filtered, events = srv.apply_dashboard_dictation_filter(brief, list(brief.get("events", [])))
    stats = build_range_statistics(
        srv.store.root, date_from=DATE, date_to=DATE, scope=scope, view="display",
        event_filter=srv.dashboard_dictation_filter().filter_events,
    )
    agent = srv.build_agent_brief(date=DATE, allow_empty=True, hide_dictation=True, scope=scope, view="display")

    from life_logger.expense_policy import build_expense_ledger

    daily_count = len(events)
    daily_money = round(float(build_expense_ledger(list(events)).get("total_amount") or 0), 2)

    assert stats["event_count"] == daily_count == agent["event_count"], (
        f"{scope}: stats={stats['event_count']} daily={daily_count} brief={agent['event_count']}"
    )
    assert round(float(stats["expense_total_cny"] or 0), 2) == daily_money
    assert round(float(agent["expense_total_cny"] or 0), 2) == daily_money
    assert agent["expense_total"]["total_amount"] == daily_money
    assert agent["event_count_basis"] == "canonical_event_view"


def test_brief_keeps_projection_separate_from_canonical_count(server):
    """低置信事件不能算进投影，但也不能让权威数变小。"""
    srv, _store, _config = server
    agent = srv.build_agent_brief(date=DATE, allow_empty=True, hide_dictation=True,
                                  scope="provisional", view="display")
    assert agent["event_count"] > agent["projection_event_count"], "本 fixture 里应有低置信事件被投影过滤"
    assert agent["confidence_filter"]["source_event_count"] == agent["event_count"]
    assert (
        agent["event_count"]
        == agent["projection_event_count"] + agent["confidence_filter"]["filtered_event_count"]
    )


def test_range_slice_equals_single_day(server):
    """区间里某一天的切片必须等于该日单日查询（防跨日预过滤漏事件）。"""
    srv, _store, _config = server
    flt = srv.dashboard_dictation_filter().filter_events
    wide = build_range_statistics(
        srv.store.root, date_from=DATE, date_to="2026-10-15", scope="provisional",
        view="display", event_filter=flt,
    )
    one = build_range_statistics(
        srv.store.root, date_from=DATE, date_to=DATE, scope="provisional",
        view="display", event_filter=flt,
    )
    wide_counts = {row["date"]: row["count"] for row in (wide.get("daily_event_counts") or [])}
    wide_money = {row["date"]: row["amount"] for row in (wide.get("daily_expenses") or [])}
    assert wide_counts.get(DATE) == one["event_count"]
    assert round(float(wide_money.get(DATE) or 0), 2) == round(float(one["expense_total_cny"] or 0), 2)


def test_invalid_scope_still_rejected_by_all_three(server):
    """非法 scope 必须显式报错，不能静默退回默认口径。"""
    srv, _store, _config = server
    with pytest.raises(ValueError):
        srv.build_daily_brief(date=DATE, allow_empty=True, scope="bogus", view="display")
    with pytest.raises(ValueError):
        srv.build_agent_brief(date=DATE, allow_empty=True, hide_dictation=True, scope="bogus", view="display")
    with pytest.raises(ValueError):
        build_range_statistics(srv.store.root, date_from=DATE, date_to=DATE, scope="bogus", view="display")
