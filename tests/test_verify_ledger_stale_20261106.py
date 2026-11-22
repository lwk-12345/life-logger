"""夜间校验：日报过期（STALE）与真失败（FAIL）必须分开。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("vlc", ROOT / "scripts" / "verify_ledger_consistency.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vlc = _load()

SYNTH = {
    "generated_at": "2026-11-06T01:30:43+08:00",
    "expense_analysis": {"total_amount_cny": 36.5},
    "expense_ledger": {"total_amount": 36.5},
    "event_count": 6,
}


def _fake_get(daily_events, daily_total=46.5, range_total=46.5):
    def _get(path, token):
        if "expense-range" in path:
            return ({"by_day": [{"date": "2026-11-05", "total_cny": range_total,
                                 "source": "ledger"}]}, "ok", "")
        return ({"expense_ledger": {"total_amount": daily_total}, "events": daily_events}, "ok", "")
    return _get


def ev(title, amount, created_at):
    return {"title": title, "created_at": created_at,
            "data": {"expense": {"amount": amount}}}


def test_late_events_only_returns_events_after_generation():
    events = [
        ev("买零食", 14, "2026-11-05T15:13:00+08:00"),
        ev("买一包红塔山", 10, "2026-11-06T03:33:28+08:00"),
    ]
    late = vlc._late_events(SYNTH, events)
    assert [e["title"] for e in late] == ["买一包红塔山"]


def test_missing_created_at_is_not_late():
    events = [{"title": "老事件", "data": {"expense": {"amount": 5}}}]
    assert vlc._late_events(SYNTH, events) == []


def test_stale_explains_requires_exact_gap():
    late = [ev("买一包红塔山", 10, "2026-11-06T03:33:28+08:00")]
    assert vlc._stale_explains(late, 10.0, None) is True
    assert vlc._stale_explains(late, 10.0, 1) is True
    assert vlc._stale_explains(late, 12.0, None) is False     # 金额对不上 → 不是过期
    assert vlc._stale_explains(late, 10.0, 2) is False        # 条数对不上 → 不是过期
    assert vlc._stale_explains([], 10.0, 1) is False


def test_summary_mentions_title_and_amount():
    late = [ev("买一包红塔山", 10, "2026-11-06T03:33:28+08:00")]
    text = vlc._late_summary(late)
    assert "买一包红塔山" in text and "10" in text


def test_verify_classifies_stale_not_fail(monkeypatch):
    """11-05 现场：日报 36.5/6 条，之后补录 10 元 → STALE，不算失败。"""
    daily_events = [
        ev("买零食", 14, "2026-11-05T15:13:00+08:00"),
        ev("买一包红塔山", 10, "2026-11-06T03:33:28+08:00"),
    ] + [{"title": f"旧事件{i}", "data": {"expense": {"amount": 1}}} for i in range(5)]

    monkeypatch.setattr(vlc, "_synthesis", lambda day, inject: dict(SYNTH))
    monkeypatch.setattr(vlc, "_get", _fake_get(daily_events))
    monkeypatch.setattr(vlc, "_event_view_counts", lambda days: {"2026-11-05": 7})
    monkeypatch.setattr(vlc, "check_frontend_money", lambda: (vlc.PASS, [], []))
    monkeypatch.setattr(vlc, "check_duplicate_purchase_windows", lambda a, b: {"status": vlc.PASS, "cluster_count": 0, "clusters": []})

    result = vlc.verify("2026-11-05", "2026-11-05")
    row = result["rows"][0]
    assert row["rule2"] == vlc.STALE and row["rule4"] == vlc.STALE
    assert "日报过期" in row["rule2_note"] and "红塔山" in row["rule2_note"]
    assert result["verdict"] == vlc.STALE
    assert result["failed_days"] == []
    assert result["stale_days"] == ["2026-11-05"]


def test_verify_still_fails_when_gap_not_explained(monkeypatch):
    """差额和迟到事件对不上 → 仍判 FAIL（防止把真错洗成过期）。"""
    daily_events = [
        ev("买零食", 14, "2026-11-05T15:13:00+08:00"),
        ev("买一包红塔山", 10, "2026-11-06T03:33:28+08:00"),
    ] + [{"title": f"旧事件{i}", "data": {"expense": {"amount": 1}}} for i in range(5)]

    monkeypatch.setattr(vlc, "_synthesis", lambda day, inject: dict(SYNTH))
    # /api/daily 报 99（比「36.5 + 10」多出来的 52.5 没有任何事件解释）
    monkeypatch.setattr(vlc, "_get", _fake_get(daily_events, daily_total=99.0, range_total=99.0))
    monkeypatch.setattr(vlc, "_event_view_counts", lambda days: {"2026-11-05": 7})
    monkeypatch.setattr(vlc, "check_frontend_money", lambda: (vlc.PASS, [], []))
    monkeypatch.setattr(vlc, "check_duplicate_purchase_windows", lambda a, b: {"status": vlc.PASS, "cluster_count": 0, "clusters": []})

    result = vlc.verify("2026-11-05", "2026-11-05")
    row = result["rows"][0]
    assert row["rule2"] == vlc.FAIL
    assert result["verdict"] == vlc.FAIL
    assert result["failed_days"] == ["2026-11-05"]


def test_stale_is_not_a_failure_for_exit_code(monkeypatch):
    """STALE 时 main 必须返回 0（夜间校验不该因为日报旧了报警）。"""
    monkeypatch.setattr(vlc, "verify", lambda a, b, inject=None: {
        "rows": [], "rule5": {"status": vlc.PASS, "violations": [], "allowed": []},
        "rule6": {"cluster_count": 0, "clusters": []}, "failed_days": [],
        "stale_days": ["2026-11-05"], "slow_days": [], "warn_days": [],
        "interface": {"daily_unreachable_count": 0}, "verdict": vlc.STALE,
    })
    monkeypatch.setattr("sys.argv", ["verify_ledger_consistency.py"])
    assert vlc.main(["--from", "2026-11-05", "--to", "2026-11-05"]) == 0
