"""D1/D2：账本 helper 必须用调用方数据根；宽区间与逐日合计一致。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger import day_facts as df


def _write_day(root: Path, day: str, amount: float, title: str = "面") -> None:
    path = root / "activity-timeline" / "jev" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "activity-timeline-jev-v1", "date": day,
        "activities": [{
            "activity_id": f"act_{day}", "title": title, "category": "消费·购物",
            "lifecycle": "live", "status": "completed",
            "occurrence_start": f"{day}T10:00:00+08:00",
            "items": [{
                "item_id": f"{day}-1", "action": f"买{title}", "fact_type": "expense",
                "status": "completed", "occurrence_start": f"{day}T10:00:00+08:00",
                "amount": amount,
                "data": {"expense": {"amount": amount, "currency": "CNY", "item": title}},
                "evidence": [{"source_id": "s1", "quote": "买了", "source_label": "self"}],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")


def test_empty_root_returns_zero(tmp_path):
    ledger = df.range_expense_ledger([], "2026-11-01", "2026-11-01", root=tmp_path)
    assert ledger.get("total_amount") == 0
    assert df.activities_for_day("2026-11-01", root=tmp_path) is None


def test_two_roots_do_not_leak(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_day(a, "2026-11-01", 16.9)
    _write_day(b, "2026-11-01", 5.0)

    assert df.range_expense_ledger([], "2026-11-01", "2026-11-01", root=a)["total_amount"] == pytest.approx(16.9)
    assert df.range_expense_ledger([], "2026-11-01", "2026-11-01", root=b)["total_amount"] == pytest.approx(5.0)
    # 缓存也必须按根区分：先读 a 再读 b 后，a 不能变
    assert df.range_expense_ledger([], "2026-11-01", "2026-11-01", root=a)["total_amount"] == pytest.approx(16.9)


def test_wide_range_matches_per_day_sum(tmp_path):
    from datetime import date, timedelta
    start = date(2026, 9, 20)                               # 33 个连续日期，跨过旧的 31 天上限
    days = [(start + timedelta(days=i)).isoformat() for i in range(33)]
    for i, day in enumerate(days):
        _write_day(tmp_path, day, 1.0 + i * 0.0)          # 每天 1 元

    wide = df.range_expense_ledger([], days[0], days[-1], root=tmp_path)["total_amount"]
    per_day = sum(
        df.range_expense_ledger([], day, day, root=tmp_path)["total_amount"] for day in days
    )
    assert len(df.activities_for_range(days[0], days[-1], root=tmp_path)) == len(days)
    assert wide == pytest.approx(per_day)
    assert wide == pytest.approx(33.0)


def test_no_date_search_range_is_not_empty(tmp_path):
    """无日期搜索用 2000–2099：不能因为「区间太宽」直接返回空活动。"""
    _write_day(tmp_path, "2026-11-05", 7.5)
    acts = df.activities_for_range("2000-01-01", "2099-12-31", root=tmp_path)
    assert len(acts) == 1
    ledger = df.range_expense_ledger([], "2026-11-05", "2026-11-05", root=tmp_path)
    assert ledger["total_amount"] == pytest.approx(7.5)


def test_default_root_still_production(tmp_path, monkeypatch):
    monkeypatch.setattr(df, "PROJECT_ROOT", tmp_path)
    _write_day(tmp_path / "output" / "local-web", "2026-11-02", 3.0)
    assert df.activities_for_day("2026-11-02") is not None
    assert df.day_expense_ledger([], "2026-11-02")["total_amount"] == pytest.approx(3.0)
