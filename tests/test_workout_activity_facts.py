"""力量训练：动作名优先标题、逐组事实、引文支持标记、跨事件不合并。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_cards import build_activity_cards  # noqa: E402

WATCH_ROOT = ROOT / "output" / "local-web" / "watch-sessions" / "watch-41aa6b87347f4f0fb5238b559f705c4d" / "7DA384D776E24A4CA7F825C65A2A2A24"


def _real_event() -> dict | None:
    path = WATCH_ROOT / "events.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text())
    events = payload if isinstance(payload, list) else payload.get("events") or []
    return next((e for e in events if e.get("event_id") == "evt_session_1c5ef63bf265d219"), None)


def test_real_squat_summary_keeps_action_and_per_set_facts():
    event = _real_event()
    if event is None:
        pytest.skip("真实深蹲聚合事件不在本机")
    card = build_activity_cards([event])["cards"][0]
    assert card["kind"] == "fitness"
    paths = {f["path"]: f for f in card["facts"]}
    assert paths["data.exercise.exercises[0].name"]["value"] == "深蹲"
    weights = [(f["path"], f["value"], f.get("support")) for f in card["facts"]
               if f["path"].endswith(".weight_kg")]
    assert [w[1] for w in weights] == [40, 60, 80, 90], weights
    # 该事件来自 session_aggregator：quote 是模型概述而非 ASR 逐字 → 全部标 model_quote_only
    assert {w[2] for w in weights} == {"model_quote_only"}, weights


def test_verbatim_segment_event_marks_quote_support():
    """现场逐字事件（非聚合器）里出现的重量 → quote_verbatim；未出现的 → model_only。"""
    event = {
        "event_id": "evt_verbatim", "title": "深蹲", "categories": ["exercise"],
        "time": {"precision": "minute", "absolute": "2026-10-25T18:03:50+08:00"},
        "data": {"exercise": {"exercises": [{"name": "深蹲", "sets": [
            {"set": 1, "weight_kg": 40}, {"set": 2, "weight_kg": 90}]}]}},
        "evidence": {"quote": "第一组四十公斤", "source_label": "self",
                     "extractor": "deepseek_context_batch", "segment_id": "000036"},
    }
    card = build_activity_cards([event])["cards"][0]
    support = {f["value"]: f.get("support") for f in card["facts"] if f["path"].endswith(".weight_kg")}
    assert support == {40: "quote_verbatim", 90: "model_only"}, support


def test_generic_title_falls_back_to_action_name():
    event = {
        "event_id": "evt_gym_generic", "title": "力量训练", "categories": ["exercise"],
        "time": {"precision": "unknown", "absolute": None},
        "data": {"exercise": {"exercises": [{"name": "深蹲",
                                             "sets": [{"set": 1, "weight_kg": 40, "reps": 10}]}]}},
        "evidence": {"quote": "第一组四十公斤", "source_label": "self"},
    }
    card = build_activity_cards([event])["cards"][0]
    assert card["title"] == "深蹲", card["title"]
    assert card["title_original"] == "力量训练"
    assert card["title_source"] == "data.exercise.exercises[0].name"
    assert card["time_basis"] == "unknown", "没有发生时刻 → unknown"


def test_sets_array_keeps_weight_reps_rest_and_does_not_fill_unknown():
    event = {
        "event_id": "evt_gym_sets", "title": "深蹲训练", "categories": ["exercise"],
        "data": {"exercise": {"exercises": [{"name": "深蹲", "sets": [
            {"set": 1, "weight_kg": 60, "reps": 8, "rest_seconds": 90},
            {"set": 2, "weight_kg": None, "reps": None},
        ]}]}},
        "evidence": {"quote": "第一组六十公斤八个 第二组", "source_label": "self"},
        "time": {"precision": "minute", "absolute": "2026-10-25T18:06:09+08:00"},
    }
    card = build_activity_cards([event])["cards"][0]
    values = {(f["path"], f["value"]) for f in card["facts"]}
    assert ("data.exercise.exercises[0].sets[0].weight_kg", 60) in values
    assert ("data.exercise.exercises[0].sets[0].reps", 8) in values
    assert ("data.exercise.exercises[0].sets[0].rest_seconds", 90) in values
    # 第二组没填的不补 0 / 不补猜测值
    assert not any(p.endswith("sets[1].weight_kg") for p, _ in values)
    assert card["time_basis"] == "occurrence"


def test_retrospective_flag_is_projected_not_used_as_occurrence():
    event = {
        "event_id": "evt_gym_review", "title": "深蹲（回顾）", "categories": ["exercise"],
        "retrospective": True, "review_status": "pending_review",
        "pending_reason": "retrospective_statement",
        "time": {"precision": "unknown", "absolute": None},
        "data": {"exercise": {"exercises": [{"name": "深蹲", "sets": [{"set": 1, "weight_kg": 40}]}]}},
        "evidence": {"quote": "首先我今天练的深蹲", "source_label": "self",
                     "reported_at": "2026-10-25T20:42:26+08:00"},
    }
    card = build_activity_cards([event])["cards"][0]
    assert card["retrospective"] is True
    assert card["review_status"] == "pending_review"
    assert card["pending_reason"] == "retrospective_statement"
    assert card["time_basis"] == "reported", "只有讲述时间 → reported，不当发生时刻"
    assert card["time"]["occurred_at"] is None


def test_multiple_actions_in_one_event_stay_one_card():
    event = {
        "event_id": "evt_gym_multi", "title": "腿部训练", "categories": ["exercise"],
        "data": {"exercise": {"exercises": [
            {"name": "深蹲", "sets": [{"set": 1, "weight_kg": 60}]},
            {"name": "腿举", "sets": [{"set": 1, "weight_kg": 100}]},
        ]}},
        "evidence": {"quote": "深蹲六十 腿举一百", "source_label": "self"},
        "time": {"precision": "minute", "absolute": "2026-10-25T18:30:00+08:00"},
    }
    cards = build_activity_cards([event])["cards"]
    assert len(cards) == 1, "同一原事件的多个动作保留为同活动子项"
    names = [f["value"] for f in cards[0]["facts"] if f["path"].endswith(".name")]
    assert names == ["深蹲", "腿举"]


def test_two_events_never_merge_by_recording_or_time():
    base = {
        "categories": ["exercise"], "evidence": {"quote": "深蹲", "source_label": "self",
                                                 "recording_id": "watch_X", "segment_id": "000038"},
        "time": {"precision": "minute", "absolute": "2026-10-25T18:06:09+08:00"},
        "data": {"exercise": {"exercises": [{"name": "深蹲", "sets": [{"set": 1, "weight_kg": 80}]}]}},
    }
    a = dict(base, event_id="evt_a", title="深蹲")
    b = dict(base, event_id="evt_b", title="深蹲")
    cards = build_activity_cards([a, b])["cards"]
    assert len(cards) == 2, "跨事件不得因同录音/时间近合并"
    assert {c["source_event_id"] for c in cards} == {"evt_a", "evt_b"}


# ---------------------------------------------------------------- Q415：重量证据必须带单位/上下文

def _weight_support(quote: str, weight: float, *, set_index: int = 1) -> str:
    card = build_activity_cards([{
        "event_id": "evt_unit_case", "title": "深蹲", "categories": ["exercise"],
        "time": {"precision": "minute", "absolute": "2026-10-25T18:00:00+08:00"},
        "data": {"exercise": {"exercises": [{"name": "深蹲",
                                            "sets": [{"set": set_index, "weight_kg": weight}]}]}},
        "evidence": {"quote": quote, "source_label": "self", "extractor": "deepseek_context_batch"},
    }])["cards"][0]
    fact = next(f for f in card["facts"] if f["path"].endswith(".weight_kg"))
    return fact["support"]


def test_reps_and_money_numbers_do_not_confirm_weight():
    """「今天做了90下深蹲，花了90元」里的 90 不是重量 → 必须 model_only。"""
    quote = "今天做了90下深蹲，花了90元"
    assert _weight_support(quote, 90) == "model_only"


def test_weight_with_unit_is_confirmed():
    for quote, weight, set_index in (("第一组四十公斤", 40, 1), ("第二组 60kg", 60, 2),
                                     ("第三组八十千克", 80, 3)):
        assert _weight_support(quote, weight, set_index=set_index) == "quote_verbatim", (quote, weight)


def test_weight_without_unit_is_not_confirmed():
    """只有数字没有单位 → 宁 unknown/model_only，不猜。"""
    assert _weight_support("第一组九十", 90) == "model_only"
    assert _weight_support("深蹲90下", 90) == "model_only"


def test_unit_from_other_number_does_not_leak_across_sets():
    """别的组/别的动作的重量单位不能给本组作证。"""
    quote = "深蹲第一组四十公斤，硬拉第二组六十"
    assert _weight_support(quote, 40, set_index=1) == "quote_verbatim"
    assert _weight_support(quote, 60, set_index=2) == "model_only", "60 没有单位，不能借用 40 的单位"


def test_session_aggregator_stays_model_quote_only():
    card = build_activity_cards([{
        "event_id": "evt_agg_units", "title": "深蹲", "categories": ["exercise"],
        "time": {"precision": "minute", "absolute": "2026-05-29T10:01:16+08:00"},
        "data": {"exercise": {"exercises": [{"name": "深蹲", "sets": [{"set": 1, "weight_kg": 90}]}]}},
        "evidence": {"quote": "深蹲九十公斤", "source_label": "self", "extractor": "session_aggregator"},
    }])["cards"][0]
    fact = next(f for f in card["facts"] if f["path"].endswith(".weight_kg"))
    assert fact["support"] == "model_quote_only", "聚合概述即使带单位也不算逐字"


# ---------------------------------------------------------------- Q416：逐字重量必须有"动作 + 组"归属

def _supports(quote: str, exercises: list[dict], *, extractor="deepseek_context_batch", related=None) -> dict:
    event = {
        "event_id": "evt_attr", "title": "训练", "categories": ["exercise"],
        "time": {"precision": "minute", "absolute": "2026-10-25T18:00:00+08:00"},
        "data": {"exercise": {"exercises": exercises}},
        "evidence": {"quote": quote, "source_label": "self", "extractor": extractor},
    }
    if related:
        event["evidence"]["related_quotes"] = related
    card = build_activity_cards([event])["cards"][0]
    return {f["path"]: f.get("support") for f in card["facts"] if f["path"].endswith(".weight_kg")}


def test_multi_action_number_does_not_leak_to_other_action():
    """深蹲40 / 卧推40，quote 只提深蹲 → 卧推不得 quote_verbatim。"""
    out = _supports("深蹲第一组四十公斤", [
        {"name": "深蹲", "sets": [{"set": 1, "weight_kg": 40}]},
        {"name": "卧推", "sets": [{"set": 1, "weight_kg": 40}]},
    ])
    assert out["data.exercise.exercises[0].sets[0].weight_kg"] == "quote_verbatim"
    assert out["data.exercise.exercises[1].sets[0].weight_kg"] == "model_only"


def test_multi_action_without_action_name_is_not_confirmed():
    out = _supports("第一组四十公斤", [
        {"name": "深蹲", "sets": [{"set": 1, "weight_kg": 40}]},
        {"name": "卧推", "sets": [{"set": 1, "weight_kg": 40}]},
    ])
    assert set(out.values()) == {"model_only"}, out


def test_single_action_still_needs_set_attribution():
    """单动作但没有组归属（无「第N组」）→ 不确认。"""
    out = _supports("深蹲四十公斤", [{"name": "深蹲", "sets": [{"set": 1, "weight_kg": 40}]}])
    assert out["data.exercise.exercises[0].sets[0].weight_kg"] == "model_only"


def test_set_number_must_match():
    out = _supports("深蹲第二组四十公斤", [{"name": "深蹲", "sets": [
        {"set": 1, "weight_kg": 40}, {"set": 2, "weight_kg": 60}]}])
    assert out["data.exercise.exercises[0].sets[0].weight_kg"] == "model_only", "第一组不得借用第二组"
    assert out["data.exercise.exercises[0].sets[1].weight_kg"] == "model_only", "数值 40 与第二组 60 不符"


def test_typed_related_quotes_can_attribute_without_model_guessing():
    """related_quotes 带 exercise/set 引用 → 可确认对应组（不是模型猜）。"""
    out = _supports("训练记录：深蹲与卧推各一组",
                    [{"name": "深蹲", "sets": [{"set": 1, "weight_kg": 40}]},
                     {"name": "卧推", "sets": [{"set": 1, "weight_kg": 40}]}],
                    related=[
                        {"quote": "深蹲一组四十公斤", "role": "weight", "source_label": "self",
                         "exercise": "深蹲", "set": 1, "segment_id": "000036"},
                        {"quote": "卧推一组四十公斤", "role": "weight", "source_label": "self",
                         "exercise": "卧推", "set": 1, "segment_id": "000038"},
                    ])
    assert out["data.exercise.exercises[0].sets[0].weight_kg"] == "quote_verbatim"
    assert out["data.exercise.exercises[1].sets[0].weight_kg"] == "quote_verbatim"
