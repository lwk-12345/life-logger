"""F1: actual fallback constructor, saved artifacts and legacy read views."""
import ast
import copy
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from life_logger import daily_cognitive_session as dcs

ROOT = Path(__file__).resolve().parents[1]

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(rel: str) -> "Path":
    """优先读入库夹具（tests/fixtures/...）；没有就退回仓库内原位置。

    换机器（新克隆）时 `work/`、`output/` 都不存在 —— 那时只有入库夹具可用。
    本机两者都在，走哪个都等价，所以不影响本地行为。
    """
    shipped = FIXTURES / rel
    return shipped if shipped.exists() else ROOT / rel





@pytest.fixture
def plan_contract():
    """Execute the exact approved contract, not the reference implementation."""
    text = _fixture("work/arch-f1-fallback-20261022/PLAN.md").read_text()
    code = re.search(r"```python\n(.*?)\n```", text, re.S).group(1)
    tree = ast.parse(code)
    nodes = [n for n in tree.body if (
        isinstance(n, ast.FunctionDef)
        and n.name in {"get_path", "assert_empty_facts_contract"}
    ) or (isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "UNSUPPORTED" for t in n.targets
    ))]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "approved-plan-contract", "exec"), namespace)
    return namespace["assert_empty_facts_contract"]


def fallback(events=(), expense=None, **kwargs):
    builder = getattr(dcs, "_build_facts_only_fallback", None)
    assert callable(builder), "F1 actual pure constructor is missing"
    return builder("2000-01-01", events, expense=expense,
                   generated_at="2000-01-01T00:00:00+08:00",
                   error=RuntimeError("upstream unavailable"), **kwargs)


def test_actual_constructor_passes_exact_plan_contract(plan_contract):
    ledger = dcs.build_expense_ledger([])
    with patch.object(dcs, "_get_deepseek_settings", side_effect=AssertionError("config I/O")), \
         patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
         patch.object(Path, "write_text", side_effect=AssertionError("disk write")), \
         patch.object(Path, "read_text", side_effect=AssertionError("disk read")):
        result = fallback(expense=ledger)
    plan_contract(result)
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in ("乌冬面", "烤肠", "生脉饮", "腿部训练", "缺觉", "芒果香烟", "82"):
        assert forbidden not in encoded


def test_failed_model_uses_safe_builder_and_persists(tmp_path, plan_contract, monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_PAUSE_ON_MODEL_FAILURE", "0")
    with patch.object(dcs, "_get_deepseek_settings", return_value=(
        "https://unit.invalid/v1", "test-key", "test-model", None, None
    )), patch("urllib.request.urlopen", side_effect=RuntimeError("unavailable")):
        result = dcs.generate_daily_cognitive_synthesis("2000-01-01", [], force=True, cache_dir=tmp_path)
    plan_contract(result)
    assert json.loads((tmp_path / "synthesis-2000-01-01.json").read_text()) == result


@pytest.mark.parametrize("ledger,amount", [
    (None, None),
    ({}, None),
    ({"total_amount": 0, "transactions": [], "pending_transactions": []}, 0),
    ({"total_amount": 26.8, "totals_by_currency": {"CNY": 26.8, "USD": 7},
      "transactions": [{"event_id": "test-expense", "amount": 26.8}],
      "pending_transactions": [{"amount": None}], "opaque_policy_metadata": "preserve"}, 26.8),
])
def test_expense_passthrough_three_states_without_mutating_inputs(ledger, amount):
    before = copy.deepcopy(ledger)
    result = fallback(expense=ledger)
    assert result["facts"]["expense_ledger"] == ledger
    assert result["expense_ledger"] == ledger
    assert result["expense_analysis"]["total_amount_cny"] == amount
    assert ledger == before


@pytest.fixture
def udon_event():
    # 2026-11-07 用户决定：不知重量就不算；本夹具测 F1 fallback 透传，故提供明确克数
    return {"event_id": "udon-source", "title": "早饭吃了200克乌冬面", "categories": ["food"],
            "activity_type": "meal", "data": {"food": {"item": "乌冬面", "weight_g": 200, "estimated_calories": 375}},
            "evidence": {"quote": "早饭吃了200克乌冬面"}}


def test_evidenced_food_survives_without_inventing_analysis(udon_event):
    result = fallback([udon_event], expense=dcs.build_expense_ledger([]))
    assert "乌冬面" in json.dumps(result["facts"], ensure_ascii=False)
    assert result["facts"]["records"][0]["event_id"] == "udon-source"
    assert result["facts"]["records"][0]["quote"] == udon_event["evidence"]["quote"]
    assert result["nutrition_analysis"]["total_calories_kcal"] == 375
    assert result["executive_summary"] is None
    assert result["nutrition_analysis"]["meals_summary"] is None


@pytest.mark.parametrize("calories,expected,ratio", [(None, None, 0), (0, 0, 1), (125, 125, 1)])
def test_unknown_nutrition_is_distinct_from_explicit_zero(udon_event, calories, expected, ratio):
    event = copy.deepcopy(udon_event)
    event["data"]["food"]["estimated_calories"] = calories
    result = fallback([event])
    assert result["nutrition_analysis"]["total_calories_kcal"] == expected
    assert result["facts"]["coverage"]["nutrition"]["ratio"] == ratio
    assert result["nutrition_analysis"]["macronutrients"]["protein_g"] is None


def test_rule_inference_and_unknown_training_time_do_not_reenter_facts():
    events = [{"event_id": "workout", "title": "一次锻炼", "categories": ["exercise"],
               "data": {"exercise": {}}, "evidence": {"quote": "我锻炼了"}},
              {"event_id": "smoke", "title": "一条记录", "data": {"smoking": {"action": "craving_memo"}},
               "evidence": {"quote": "想抽烟"}}]
    result = fallback(events)
    assert result["smoking_ledger"]["psychology"]["cognitive_insight"] is None
    assert result["habit_and_smoking_analysis"]["smoke_count"] is None
    assert result["fitness_ledger"]["workouts"][0]["time_of_day"] is None
    assert "17:00左右" not in json.dumps(result, ensure_ascii=False)


def test_legacy_fallback_is_masked_read_only(tmp_path):
    old = {"date": "2000-01-01", "fallback": True, "executive_summary": "无依据叙述",
           "attention_and_energy": {"health_score": 82}, "expense_ledger": {"total_amount": 0},
           "smoking_ledger": {"psychology": {"cognitive_insight": "错误心理推断"}}}
    path = tmp_path / "synthesis-2000-01-01.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    before = path.read_bytes()
    result = dcs.normalize_daily_synthesis(old)
    assert result["narrative"]["status"] == "unavailable"
    assert result["executive_summary"] is None
    assert result["attention_and_energy"]["health_score"] is None
    assert result["smoking_ledger"]["psychology"]["cognitive_insight"] is None
    assert old["attention_and_energy"]["health_score"] == 82
    assert path.read_bytes() == before


@pytest.mark.parametrize("old", [{}, {"date": "2000-01-01", "executive_summary": "历史分析",
                                      "work_and_cognition_analysis": {"architecture_decisions": None}}])
def test_unmarked_legacy_is_unverified_not_primary(old):
    original = copy.deepcopy(old)
    result = dcs.normalize_daily_synthesis(old)
    assert result["narrative"]["status"] == "legacy_unverified"
    assert result["model_tier"] == "unknown"
    assert result["executive_summary"] == old.get("executive_summary")
    assert result["work_and_cognition_analysis"]["architecture_decisions"] == []
    assert old == original


def test_cache_hit_uses_read_adapter_without_network_or_file_migration(tmp_path):
    path = tmp_path / "synthesis-2000-01-01.json"
    path.write_text(json.dumps({"date": "2000-01-01", "executive_summary": "历史总评"}))
    before = path.read_bytes()
    with patch.object(dcs, "_get_deepseek_settings", return_value=(
        "https://unit.invalid", "test", "test", None, None
    )), patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        result = dcs.generate_daily_cognitive_synthesis("2000-01-01", [], cache_dir=tmp_path)
    assert result["narrative"]["status"] == "legacy_unverified"
    assert path.read_bytes() == before


def test_v2_aliases_are_rebuilt_from_fact_and_narrative_fields():
    result = fallback(expense={"total_amount": 5})
    result["expense_analysis"]["total_amount_cny"] = 999
    result["executive_summary"] = "stale unsafe alias"
    adapted = dcs.normalize_daily_synthesis(result)
    assert adapted["expense_analysis"]["total_amount_cny"] == 5
    assert adapted["executive_summary"] is None


def test_read_reconciliation_updates_facts_and_alias_together():
    result = fallback(expense={"total_amount": 5})
    aligned = dcs.reconcile_synthesis_expense(result, {"total_amount": 12})
    assert aligned["facts"]["expense_ledger"]["total_amount"] == 12
    assert aligned["expense_analysis"]["total_amount_cny"] == 12
    assert result["facts"]["expense_ledger"]["total_amount"] == 5


def test_chat_context_does_not_reintroduce_rule_inferences():
    from unittest.mock import MagicMock
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps({"choices": [{"message": {"content": "unknown"}}]}).encode()
    with patch.object(dcs, "_get_deepseek_settings", return_value=(
        "https://unit.invalid", "test", "test", None, None
    )), patch("urllib.request.urlopen", return_value=response) as transport:
        dcs.ask_daily_cognitive_session("2000-01-01", "有哪些记录？", [])
    request = transport.call_args.args[0]
    body = json.loads(request.data)
    content = body["messages"][-1]["content"]
    assert '"cognitive_insight": null' in content
    assert "吸烟频次平稳记录" not in content


def test_model_success_separates_narrative_from_authoritative_facts(tmp_path):
    reply = {"executive_summary": "模型分析", "expense_analysis": {"total_amount_cny": 999},
             "attention_and_energy": {"health_score": 50}}
    from unittest.mock import MagicMock
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
    with patch.object(dcs, "_get_deepseek_settings", return_value=(
        "https://unit.invalid", "test", "test", None, None
    )), patch("urllib.request.urlopen", return_value=response):
        result = dcs.generate_daily_cognitive_synthesis("2000-01-01", [], force=True, cache_dir=tmp_path)
    assert result["schema_version"] == "daily-synthesis-v2"
    assert result["generation"]["status"] == "complete"
    assert result["narrative"]["status"] == "generated"
    assert result["narrative"]["sections"]["executive_summary"] == "模型分析"
    assert result["facts"]["expense_ledger"]["total_amount"] == 0
    assert result["expense_analysis"]["total_amount_cny"] == 0
    assert result["expense_analysis"]["model_reported_total"] == 999
