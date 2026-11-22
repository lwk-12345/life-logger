"""统一事实入口：行为与旧路径一致，且任何代码都不能绕过入口自己算账。"""
import ast
import json
from pathlib import Path

import pytest

from life_logger import day_facts
from life_logger.expense_policy import build_expense_ledger

ROOT = Path(__file__).resolve().parents[1]
LEDGER_BUILDERS = {
    "build_expense_ledger",
    "build_nutrition_ledger",
    "build_fitness_ledger",
    "build_smoking_ledger",
}
# 定义处、入口本身。activity_presentation 是判断单条事件的金额，不是算一天的账。
ALLOWED = {"day_facts.py", "expense_policy.py", "daily_cognitive_session.py", "activity_presentation.py"}


def _direct_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in LEDGER_BUILDERS:
                found.append(f"{path.name}:{node.lineno}:{name}")
    return found


def test_only_the_entry_builds_day_ledgers():
    paths = [*(ROOT / "src/life_logger").glob("*.py"), *(ROOT / "scripts").glob("*.py")]
    offenders = [call for p in paths if p.name not in ALLOWED for call in _direct_calls(p)]
    assert offenders == []


def test_cognitive_session_calls_the_entry_too():
    path = ROOT / "src/life_logger/daily_cognitive_session.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name not in LEDGER_BUILDERS:
            bad += [fn.name for node in ast.walk(fn) if isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) in LEDGER_BUILDERS]
    assert bad == []


@pytest.fixture
def activity_root(tmp_path, monkeypatch):
    monkeypatch.setattr(day_facts, "PROJECT_ROOT", tmp_path)
    folder = tmp_path / "output/local-web/activity-timeline/jev"
    folder.mkdir(parents=True)
    return folder


def _purchase(amount=10):
    return {
        "activity_id": "a1",
        "title": "买水",
        "items": [{
            "action": "买了一瓶水",
            "status": "已完成",
            "amount": amount,
            "currency": "CNY",
            "spoken_at": "2026-11-01T19:48:25+08:00",
            "evidence": [{"quote": f"买了一瓶水，{amount}块钱", "source_label": "self"}],
        }],
    }


def test_day_ledger_matches_events_plus_activities(activity_root):
    (activity_root / "2026-11-01.json").write_text(json.dumps({"activities": [_purchase()]}))
    expected = build_expense_ledger([], activities=[_purchase()])
    assert day_facts.day_expense_ledger([], "2026-11-01") == expected
    assert expected["total_amount"] == 10


def test_event_only_ledger_ignores_activities(activity_root):
    (activity_root / "2026-11-01.json").write_text(json.dumps({"activities": [_purchase()]}))
    assert day_facts.event_only_expense_ledger([])["total_amount"] == 0


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"activities": "bad"})])
def test_unreadable_activities_count_as_none(activity_root, content):
    if content is not None:
        (activity_root / "2026-11-02.json").write_text(content)
    assert day_facts.activities_for_day("2026-11-02") is None


def test_range_is_bounded(activity_root):
    (activity_root / "2026-11-01.json").write_text(json.dumps({"activities": [_purchase()]}))
    assert len(day_facts.activities_for_range("2026-11-01", "2026-11-01")) == 1
    # [2026-11-07 D2] 宽区间不再返回空：按天分批读，与逐日合计一致。
    assert len(day_facts.activities_for_range("2026-10-01", "2026-11-30")) == 1
    # 宽区间走目录枚举，不再受「31 天」截断（D2 的核心修复）
    assert len(day_facts.activities_for_range("2026-10-01", "2026-11-30", max_days=30)) == 1
    assert day_facts.activities_for_range("bad", "2026-11-30") == []


def test_activity_summary_never_reports_a_day_without_products_as_zero(activity_root):
    assert day_facts.activity_track_summary("2026-11-03") is None
    (activity_root / "2026-11-01.json").write_text(json.dumps({"activities": [_purchase()]}))
    assert day_facts.activity_track_summary("2026-11-01") == {
        "total_cny": 10.0, "event_count": 0, "source": "activity_track",
    }


def test_cutover_date_is_the_one_the_user_approved():
    assert day_facts.FACT_CUTOVER_DATE == "2026-10-26"


def test_expense_range_counts_a_day_that_only_has_activities(tmp_path, activity_root):
    """11-01 这类事件轨为 0、活动有消费的日子必须进入区间合计。"""
    from life_logger.local_web import build_expense_range

    root = tmp_path / "data"
    (root / "activity-timeline" / "jev").mkdir(parents=True, exist_ok=True)
    # [2026-11-07 D1] 活动必须放在**调用方数据根**下（不再偷读 PROJECT_ROOT）
    (root / "activity-timeline" / "jev" / "2026-11-01.json").write_text(
        json.dumps({"activities": [_purchase()]}))
    payload = build_expense_range(root, date_from="2026-11-01", date_to="2026-11-01")
    assert payload["by_day"][0]["total_cny"] == 10.0
    assert payload["by_day"][0]["source"] == "ledger"


def test_report_activities_keeps_only_self_speech_and_drops_media():
    media = {**_purchase(), "category": "浏览·视频", "title": "看视频"}
    other = _purchase()
    other["items"][0]["evidence"][0]["source_label"] = "other"
    kept = day_facts.report_activities([_purchase(), media, other])
    assert [a["title"] for a in kept] == ["买水"]
    assert kept[0]["items"][0]["quotes"] == ["买了一瓶水，10块钱"]
    assert day_facts.report_activities(None) == []


def test_report_time_is_utc8():
    item = {**_purchase(), "occurrence_start": "2026-11-01T11:48:25+00:00"}
    assert day_facts.report_activities([item])[0]["time"] == "19:48"


def test_synthesis_with_activities_uses_day_ledger_and_keeps_event_count(activity_root, tmp_path, monkeypatch):
    from life_logger import daily_cognitive_session as dcs

    (activity_root / "2026-11-01.json").write_text(json.dumps({"activities": [_purchase()]}))
    sent = {}

    def fake_model(url, api_key, body, *, tier):
        sent["prompt"] = body["messages"][1]["content"]
        sent["system"] = body["messages"][0]["content"]
        return {"date": "2026-11-01", "executive_summary": "x", "expense_analysis": {"total_amount_cny": 999}}, []

    monkeypatch.setattr(dcs, "_get_deepseek_settings", lambda: ("http://fake.invalid", "k", "m1", "m2", None))
    monkeypatch.setattr(dcs, "_call_model", fake_model)
    out = dcs.generate_daily_cognitive_synthesis(
        "2026-11-01", [], force=True, cache_dir=tmp_path / "s",
        activities=day_facts.activities_for_day("2026-11-01"))
    assert "买了一瓶水" in sent["prompt"] and "活动时间线" in sent["system"]
    assert out["expense_analysis"]["total_amount_cny"] == 10
    assert out["event_count"] == 0


def test_scheduler_skips_empty_day_unless_activities_given():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sched", ROOT / "scripts/daily_synthesis_scheduler.py")
    sched = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sched)
    assert sched._decide("2099-01-01", True, [])[0] == "skip-empty"
    assert sched._decide("2099-01-01", True, [], [{"title": "x"}])[0] == "run"
