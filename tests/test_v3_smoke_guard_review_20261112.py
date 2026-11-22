"""总控审阅补丁：排查指令守卫收窄 + 内部标记校验前剥离（2026-11-12）。"""
from life_logger import activity_prompt_v3 as v3
from life_logger import fact_schema


def _item(iid, fact_type, quote, sid, **extra):
    base = {
        "item_id": iid, "fact_type": fact_type, "action": quote, "status": "completed",
        "confidence": 0.9, "reason": "r",
        "evidence": [{"source_id": sid, "quote": quote}],
        "expense": None, "smoke": None, "food": None, "exercise": None,
    }
    base.update(extra)
    return base


def test_cashier_recheck_does_not_erase_real_purchase():
    body = {"activities": [{
        "activity_id": "a1", "category": "消费·购物",
        "items": [
            _item("i1", "other", "老板你再检查一下", "s1"),
            _item("i2", "expense", "拿包紫云", "s2", expense={"amount": None, "item": "一包紫云香烟"}),
        ],
    }]}
    out = v3._suppress_ai_inspection_false_facts(body)
    kinds = [it["fact_type"] for a in out["activities"] for it in a["items"]]
    assert "expense" in kinds


def test_inspection_quote_reused_by_work_activity_is_demoted():
    sid = "s9"
    q = "然后再检查一下，今天上午是有买一包烟的。"
    body = {"activities": [
        {"activity_id": "w", "category": "工作·项目", "items": [_item("w1", "other", q, sid)]},
        {"activity_id": "b", "category": "消费·购物", "items": [
            _item("b1", "expense", q, sid, expense={"amount": None, "item": "一包烟"})]},
    ]}
    out = v3._suppress_ai_inspection_false_facts(body)
    life = [it for a in out["activities"] for it in a["items"] if it["fact_type"] in ("expense", "smoke")]
    assert life == []


def test_internal_flags_stripped_before_schema():
    body = {"activities": [{"activity_id": "a", "items": [
        {"x": 1, v3.SMOKE_DEMOTED_FLAG: True, v3.AI_INSPECTION_DEMOTED_FLAG: True}]}],
        "unassociated_items": [{"item": {v3.SMOKE_DEMOTED_FLAG: True}}]}
    v3._strip_internal_item_flags(body)
    assert v3.SMOKE_DEMOTED_FLAG not in body["activities"][0]["items"][0]
    assert v3.AI_INSPECTION_DEMOTED_FLAG not in body["activities"][0]["items"][0]
    assert v3.SMOKE_DEMOTED_FLAG not in body["unassociated_items"][0]["item"]
    r = fact_schema.validate_payload(body)
    assert not [e for e in r["errors"] if str(e.get("field", "")).startswith("_v3_")]
