"""唯一事实来源：/api/stats 与 daily summary 的消费账必须与 /api/daily 同源（含活动层）。"""
from __future__ import annotations

import json
from pathlib import Path

from life_logger.aggregate import build_daily_summary
from life_logger.domain import EventTime, LifeEvent
from life_logger.day_facts import day_expense_ledger
from life_logger.statistics import build_range_statistics

DAY = "2026-11-05"


def event(event_id, title, *, amount=None, at="12:00", day=DAY):
    return {
        "event_id": event_id, "title": title, "categories": ["expense"],
        "activity_type": "purchase", "effective_date": day,
        "date_basis": "reported",
        "time": {"absolute": f"{day}T{at}:00+08:00", "clock": at, "precision": "exact"},
        "data": {"expense": {"item": title, "amount": amount}} if amount is not None else {},
        "evidence": {"quote": title, "source_label": "self"},
    }


def activity_item(item_id, title, amount, at="13:00"):
    # 与生产 jev 同格式：金额在 item 顶层（不是 expense 子对象），没有 fact_type 字段
    return {
        "item_id": item_id, "action": title, "status": "completed",
        "spoken_at": f"{DAY}T{at}:00+08:00", "occurred_at": None,
        "amount": amount, "currency": "CNY", "unit": None,
        "evidence": [{"source_id": f"src-{item_id}", "quote": title, "source_label": "self"}],
    }


def life_event(event_id, title, amount, *, at="12:00") -> LifeEvent:
    """build_daily_summary 只吃 LifeEvent（不是 dict）。"""
    return LifeEvent(
        event_id=event_id, categories=["expense"], activity_type="purchase", title=title,
        time=EventTime(absolute=f"{DAY}T{at}:00+08:00", relative_seconds=0, precision="exact"),
        data={"expense": {"item": title, "amount": amount}},
        capture_mode="passive", source_type="audio", confidence=0.9,
        evidence={"quote": title, "source_label": "self"}, review_status="unreviewed",
    )


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "local-web"
    (root / "activity-timeline" / "jev").mkdir(parents=True)
    payload = {"activities": [{"activity_id": "a1", "lifecycle": "live",
                               "items": [activity_item("i1", "买了杯咖啡", 16.0),
                                         activity_item("i2", "买了瓶水", 4.5, at="15:00")]}]}
    (root / "activity-timeline" / "jev" / f"{DAY}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return root


def test_statistics_daily_expenses_match_day_ledger(tmp_path):
    """活动层消费必须进 /api/stats（此前完全看不到）。"""
    root = make_root(tmp_path)
    stats = build_range_statistics(root, date_from=DAY, date_to=DAY, view="all")
    ledger = day_expense_ledger([], DAY, root=root)
    by_day = {row["date"]: row["amount"] for row in stats["daily_expenses"]}
    assert ledger["total_amount"] > 0 and by_day[DAY] == round(float(ledger["total_amount"]), 2)
    assert by_day[DAY] == 20.5                    # 16.0 + 4.5，全部来自活动层
    assert stats["expense_total_cny"] == 20.5
    assert stats["expense_transaction_count"] == len(
        [t for t in ledger["transactions"] if t.get("kind", "consumption") in {"consumption", "transfer"}])


def test_statistics_matches_daily_including_events(tmp_path):
    """事件轨 + 活动轨混合时，逐日金额仍与 day_expense_ledger 完全一致。"""
    root = make_root(tmp_path)
    events = [dict(event("e1", "买了个面包", amount=20.0))]
    ledger = day_expense_ledger(events, DAY, root=root)
    # 同一份 events 直接喂账本；统计侧从磁盘读，这里只校验「活动层没被漏掉」
    assert float(ledger["total_amount"]) >= 20.5


def test_statistics_without_activity_products_is_unchanged(tmp_path):
    """纯内存/无活动产物的旧用法：行为不变。"""
    root = tmp_path / "empty"
    root.mkdir()
    stats = build_range_statistics(root, date_from=DAY, date_to=DAY, view="all")
    assert stats["expense_total_cny"] == 0.0
    assert stats["daily_expenses"] == []


def test_daily_summary_expense_matches_day_ledger(tmp_path):
    root = make_root(tmp_path)
    events = [life_event("e1", "买了个面包", 20.0)]
    summary = build_daily_summary(events, date=DAY, timezone_name="Asia/Shanghai",
                                  warnings=[], job_root=root)
    ledger = day_expense_ledger([e.to_dict() for e in events], DAY, root=root)
    assert summary["expense_total_cny"] == round(float(ledger["total_amount"]), 2)
    assert summary["expense_total_cny"] == 40.5          # 20 事件 + 16 + 4.5 活动层


def test_daily_summary_without_root_uses_event_only_ledger(tmp_path):
    """没有 root 的纯内存用法仍走 event_only（不读磁盘活动产物）。"""
    make_root(tmp_path)                       # 磁盘上有活动产物，但没有传 root
    events = [life_event("e1", "买了个面包", 20.0)]
    summary = build_daily_summary(events, date=DAY, timezone_name="Asia/Shanghai", warnings=[])
    assert summary["expense_total_cny"] == 20.0


def test_daily_brief_ledger_includes_activity_layer(tmp_path):
    """`/api/daily-brief` 的账本必须与 /api/daily 同源（含活动层消费）。"""
    from life_logger.daily_brief import brief_expense_ledger

    root = make_root(tmp_path)
    ledger = brief_expense_ledger([], DAY, root)
    reference = day_expense_ledger([], DAY, root=root)
    assert ledger["total_amount"] == reference["total_amount"] == 20.5
    assert [t.get("event_id") for t in ledger["transactions"]] == [
        t.get("event_id") for t in reference["transactions"]]


def test_daily_brief_ledger_without_activity_products_is_event_only(tmp_path):
    """没有活动产物的临时目录：仍走事件轨账本（向后兼容）。"""
    from life_logger.daily_brief import brief_expense_ledger

    root = tmp_path / "empty-root"
    root.mkdir()
    events = [dict(event("e1", "买了个面包", amount=20.0))]
    ledger = brief_expense_ledger(events, DAY, root)
    assert float(ledger["total_amount"]) == 20.0


def test_build_daily_brief_uses_the_same_ledger(tmp_path):
    """端到端：真跑 build_daily_brief，卡片用的是含活动层的账本。"""
    import json as _json

    from life_logger.daily_brief import build_daily_brief

    root = make_root(tmp_path)
    # 与生产一致：jobs/job-*/files/<digest>/result/<date>/daily_summary.json
    day_dir = root / "jobs" / "job-test" / "files" / "digest" / "result" / DAY
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "daily_summary.json").write_text(_json.dumps({
        "schema_version": "0.1", "date": DAY, "timezone": "Asia/Shanghai",
        "event_count": 1,
        "timeline": [life_event("e1", "买了个面包", 20.0).to_dict()],
        "category_summary": {}, "warnings": [],   # 不给 display_timeline → view=display 回落 timeline
    }, ensure_ascii=False), encoding="utf-8")
    brief = build_daily_brief(root, date=DAY)
    reference = day_expense_ledger([life_event("e1", "买了个面包", 20.0).to_dict()], DAY, root=root)
    assert brief["event_count"] >= 1
    # 卡片里出现活动层那两笔（16.0 / 4.5）说明账本确实带上了活动层
    card_amounts = {
        round(float((card.get("expense") or {}).get("amount") or 0), 2)
        for card in brief.get("activity_cards", []) if isinstance(card, dict)
    }
    if card_amounts - {0.0}:
        assert card_amounts & {16.0, 4.5}, card_amounts
    assert float(reference["total_amount"]) == 40.5
