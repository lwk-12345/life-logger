"""活动层 v3 消费识别补强（2026-11-08）：店内买烟、跨店结账、未录清金额。

全部离线 fixture：不联网、不调模型。覆盖三件事——
① `expense.amount` 允许 `null`（依然拒绝字符串/NaN/布尔）；
② 11-07 凌晨三笔真实消费经 `project_live` → `build_expense_ledger` 全部进待确认，
   补填金额后合计 36.4 元、3 笔、待确认 0；
③ 提示词里写明了进店拿货、未录清金额填 null、不同门店拆分三条规则。
"""
from __future__ import annotations

import math

import pytest

from life_logger import activity_prompt_v3, fact_schema
from life_logger.activity_prompt_v3 import project_live
from life_logger.expense_policy import build_expense_ledger

# 复用 v3 影子测试的 runtime fixture（同一套 config 装载方式，不起网络）
from test_activity_v3_shadow import setup as setup  # noqa: F401

PAYMENT_UNKNOWN = "unknown"


def _item(index: int, action: str, expense: dict, quote: str, spoken_at: str) -> dict:
    """v3 item（字段严格按 fact_schema.ITEM_FIELDS，不多不少）。"""
    return {
        "item_id": f"it{index}",
        "action": action,
        "fact_type": "expense",
        "status": "completed",
        "spoken_at": spoken_at,
        "occurred_at": None,
        "occurred_date": None,
        "correction_of": None,
        "restatement_of": None,
        "confidence": 0.9,
        "reason": "现场购买/点单",
        "evidence": [{"source_id": f"s{index}", "quote": quote}],
        "expense": expense,
        "food": None,
        "exercise": None,
        "smoke": None,
        "todo": None,
    }


def _activity(activity_id: str, title: str, category: str, items: list[dict], reported_at: str) -> dict:
    return {
        "activity_id": activity_id,
        "title": title,
        "summary": title,
        "status": "completed",
        "lifecycle": "live",
        "category": category,
        "start_at": reported_at,
        "end_at": reported_at,
        "items": items,
    }


def _three_purchases() -> list[dict]:
    """11-07 03:39–03:53 的三笔真实消费（凭 2026-11-07 复盘原话构造）。"""
    return [
        # ① 03:42 买烟 15 元：进店拿货 + 本人确认价格「十五的吧我记得是，啊，对」
        _activity(
            "act-20261107-buy-cigarettes", "买烟", "消费·购物",
            [_item(1, "买烟",
                   {"item": "烟", "amount": 15, "currency": "CNY", "payment_status": PAYMENT_UNKNOWN},
                   "十五的吧我记得是，啊，对", "2026-11-07T03:42:08+08:00")],
            "2026-11-07T03:42:08+08:00",
        ),
        # ② 03:51 钟点零食 13.4 元：店员报出结账总价「十三块四」
        _activity(
            "act-20261107-snack", "钟点零食", "消费·购物",
            [_item(1, "买零食",
                   {"item": "零食", "amount": 13.4, "currency": "CNY", "payment_status": PAYMENT_UNKNOWN},
                   "十三块四", "2026-11-07T03:51:18+08:00")],
            "2026-11-07T03:51:18+08:00",
        ),
        # ③ 03:52 粉摊点炒河粉：本人点单 + 店家应答「好的」，但价格没录清 → amount null
        _activity(
            "act-20261107-fried-rice-noodle", "炒粉", "饮食",
            [_item(1, "点炒河粉",
                   {"item": "炒河粉", "amount": None, "currency": "CNY", "payment_status": PAYMENT_UNKNOWN},
                   "一份炒河粉", "2026-11-07T03:52:00+08:00")],
            "2026-11-07T03:53:06+08:00",
        ),
    ]


def _records() -> list[dict]:
    return [
        {"source_id": "s1", "segment_id": "seg1", "row_id": "r1", "source_label": "self",
         "timestamp": "2026-11-07T03:42:08+08:00"},
        {"source_id": "s2", "segment_id": "seg2", "row_id": "r2", "source_label": "self",
         "timestamp": "2026-11-07T03:51:18+08:00"},
        {"source_id": "s3", "segment_id": "seg3", "row_id": "r3", "source_label": "self",
         "timestamp": "2026-11-07T03:52:00+08:00"},
    ]


def _project(activities: list[dict]) -> dict:
    report = {
        "status": "valid",
        "confirmed_result": {"activities": activities},
        "non_confirmed": [],
        "input_apps": {},
    }
    return project_live(report, _records())


# --------------------------------------------------------------------------- ① schema


def test_expense_amount_accepts_null() -> None:
    payload = {"schema_version": 3, "activities": _three_purchases()}
    result = fact_schema.validate_payload(payload)
    assert result["ok"] is True, result["errors"]
    schema = fact_schema.json_schema()
    expense = (schema["properties"]["activities"]["items"]["properties"]["items"]["items"]
               ["properties"]["expense"])
    amount = expense["anyOf"][0]["properties"]["amount"]
    assert amount["type"] == ["number", "null"], "JSON Schema 必须同时允许 number 与 null"


@pytest.mark.parametrize("bad_amount", ["十六元", float("nan"), True, float("inf")])
def test_expense_amount_still_rejects_non_numbers(bad_amount) -> None:
    activities = _three_purchases()
    activities[0]["items"][0]["expense"]["amount"] = bad_amount
    errors = fact_schema.validate_payload({"schema_version": 3, "activities": activities})["errors"]
    assert any(e["error"] == "type_error" and e["field"] == "amount" for e in errors), errors


def test_expense_amount_null_without_status_0() -> None:
    """null 不是 0：结构合法，但**绝不能**被当成 0 元入账。"""
    activities = _three_purchases()
    assert activities[2]["items"][0]["expense"]["amount"] is None
    ledger = build_expense_ledger([], activities=_project(activities)["activities"])
    assert ledger["total_amount"] == 0.0
    assert all(tx.get("amount") is None for tx in ledger["pending_transactions"])


# --------------------------------------------------------------------------- ② 账本


def test_three_purchases_all_land_in_pending() -> None:
    activities = _three_purchases()
    projected = _project(activities)
    # project_live 必须把 expense 摊到顶层，且不因 amount=None 报错/丢弃
    flat = [(i["action"], i.get("amount"), i.get("payment_status"))
            for a in projected["activities"] for i in a["items"]]
    assert flat == [("买烟", 15, "unknown"), ("买零食", 13.4, "unknown"), ("点炒河粉", None, "unknown")]

    ledger = build_expense_ledger([], activities=projected["activities"])
    pending = ledger["pending_transactions"]
    assert ledger["total_amount"] == 0.0, "未确认的消费不得混进已付总额"
    assert ledger["transactions"] == []
    assert ledger["pending_count"] == len(pending) == 3, "三笔都要在待确认里，一笔都不能丢"

    by_event = {row["event_id"]: row for row in pending}
    cigarettes = by_event["act-20261107-buy-cigarettes#it1"]
    snack = by_event["act-20261107-snack#it1"]
    noodle = by_event["act-20261107-fried-rice-noodle#it1"]

    # 买烟：价格说清了，只是没录到付款播报 → 待确认但保留候选金额
    assert cigarettes["proposed_amount"] == pytest.approx(15.0)
    assert cigarettes["amount"] is None
    assert cigarettes["reason"] == "v3_payment_unconfirmed"
    # 钟点零食：店员报出结账总价
    assert snack["proposed_amount"] == pytest.approx(13.4)
    assert snack["reason"] == "v3_payment_unconfirmed"
    # 炒河粉：金额没录清 → 不是"付款未确认"，而是"金额未核实/待补填"
    assert noodle["amount"] is None
    assert noodle["proposed_amount"] is None
    assert noodle["reason"] == "v3_amount_unverified", "缺金额要说缺金额，别笼统说付款未确认"


def test_confirming_three_amounts_settles_ledger() -> None:
    activities = _three_purchases()
    projected = _project(activities)
    confirmed = {
        "act-20261107-buy-cigarettes#it1": 15.0,
        "act-20261107-snack#it1": 13.4,
        "act-20261107-fried-rice-noodle#it1": 8.0,   # 用户事后补填的炒河粉价格
    }
    ledger = build_expense_ledger([], activities=projected["activities"], confirmed=confirmed)
    assert ledger["total_amount"] == pytest.approx(36.4)
    assert len(ledger["transactions"]) == 3
    assert ledger["pending_count"] == 0
    assert ledger["pending_transactions"] == []
    # 三笔各自的金额都要对得上，不能串到别人头上
    amounts = {tx["title"]: tx["amount"] for tx in ledger["transactions"]}
    assert amounts == {"烟": pytest.approx(15.0), "零食": pytest.approx(13.4), "炒河粉": pytest.approx(8.0)}


# --------------------------------------------------------------------------- ③ 提示词

PROMPT_RULES = [
    "店内拿货并确认价格",          # 规则 1
    "已点单或已拿货但金额没录清",  # 规则 2
    "不同门店或摊位分开记",         # 规则 3
]


def test_prompt_documents_store_purchase_rules() -> None:
    text = open("config/activity-prompt-v3.3.md", encoding="utf-8").read()
    for anchor in PROMPT_RULES:
        assert anchor in text, f"提示词缺少规则：{anchor}"
    # 三条规则的关键约束都要写清楚
    assert "payment_status=unknown" in text
    assert "expense.amount填null" in text
    assert "不要把amount写成0" in text
    assert "严禁混成一条" in text
    # 原有锚点句不能被破坏（test_activity_v3_gates 断言它们）
    assert "标题≤{title_max_length}" in text and "动作≤{action_max_length}" in text


def test_rendered_prompt_keeps_anchors_and_new_rules(setup) -> None:
    from test_activity_v3_shadow import CUTOFF, LOOKBACK, RECORD

    runtime = setup({"version": "v3", "mode": "shadow", "user_profile": ""})
    prompt = activity_prompt_v3.build_prompt(runtime.load_config(), CUTOFF, LOOKBACK, [RECORD], [])
    assert "标题≤20 字、动作≤40 字" in prompt
    for anchor in PROMPT_RULES:
        assert anchor in prompt, f"渲染后的提示词缺少：{anchor}"


def test_nan_is_still_rejected_by_schema_after_null_support() -> None:
    """显式再确认一次：放宽 null 没有顺带放过 NaN/Inf。"""
    activities = _three_purchases()
    activities[1]["items"][0]["expense"]["amount"] = math.nan
    errors = fact_schema.validate_payload({"schema_version": 3, "activities": activities})["errors"]
    assert [e for e in errors if e["field"] == "amount" and e["error"] == "type_error"]


def test_same_day_price_supplement_backfills_unpriced_cola_and_deduplicates() -> None:
    """11-08 真实案例：上午买可乐未录清金额，后续补报“可乐是一块八”自动回填并入账，再次复述不重复记两份。"""
    activities = [
        {
            "activity_id": "act_cola_purchase_1108_L038",
            "title": "想买冰杯嫌远放弃，就近买一瓶可乐并支付",
            "summary": "原提到想买冰杯，因觉得太远不想跑过去而放弃，改为就近买了一瓶可乐并操作支付（金额未录清）。",
            "status": "completed",
            "lifecycle": "live",
            "category": "消费·购物",
            "fact_schema_version": 3,
            "items": [
                {
                    "item_id": "item_cola_purchase_1108_01",
                    "action": "就近购买一瓶可乐并操作支付（金额未录清）",
                    "fact_type": "expense",
                    "status": "completed",
                    "expense": {"amount": None, "currency": "CNY", "item": "可乐", "payment_status": "unknown"},
                    "evidence": [
                        {"source_id": "s_cola_buy", "quote": "想买B杯，但太远了，不想往那边跑，然后就就去买了一瓶可乐。", "source_label": "self"},
                        {"source_id": "s_pos_unlock", "quote": "请解锁手机", "source_label": "non_self"},
                    ],
                }
            ],
        },
        {
            "activity_id": "act_event_review_1108_L034",
            "title": "复盘录错事件与可乐价格",
            "summary": "在工作复盘中补充可乐一块八。",
            "status": "ongoing",
            "lifecycle": "live",
            "category": "工作·项目",
            "fact_schema_version": 3,
            "items": [
                {
                    "item_id": "item_event_cola_price_1108_L119",
                    "action": "在事件复盘中说明可乐价格为一块八",
                    "fact_type": "other",
                    "status": "completed",
                    "expense": None,
                    "evidence": [
                        {"source_id": "s_cola_price_1", "quote": "可乐是一块八。", "source_label": "self", "v3_work_dictation": True},
                    ],
                }
            ],
        },
        {
            "activity_id": "act_cola_restatement_1108_L150",
            "title": "补充说明上午可乐价格",
            "summary": "再次说明上午买的可乐是一块八。",
            "status": "completed",
            "lifecycle": "live",
            "category": "消费·购物",
            "fact_schema_version": 3,
            "items": [
                {
                    "item_id": "item_cola_price_restated",
                    "action": "说明上午买的无糖可乐是一块八",
                    "fact_type": "expense",
                    "status": "completed",
                    "restatement_of": "item_cola_purchase_1108_01",
                    "expense": {"amount": 1.8, "currency": "CNY", "item": "无糖可乐", "payment_status": "paid"},
                    "evidence": [
                        {"source_id": "s_cola_price_2", "quote": "上午买的无糖可乐是一块八。", "source_label": "self"},
                    ],
                }
            ],
        },
    ]
    merged = activity_prompt_v3.merge_live_v3_activities(activities)
    cola_act = next(a for a in merged if a["activity_id"] == "act_cola_purchase_1108_L038")
    cola_item = cola_act["items"][0]
    assert cola_item["expense"]["amount"] == 1.8
    assert cola_item["expense"]["payment_status"] == "paid"
    assert "（1.8元）" in cola_item["action"]
    assert "（1.8元）" in cola_act["summary"]
    assert all(a["activity_id"] != "act_cola_restatement_1108_L150" for a in merged)

    ledger = build_expense_ledger([], activities=merged, activity_rules_from="2026-10-26")
    assert ledger["total_amount"] == 1.8
    assert ledger["pending_count"] == 0
    assert len(ledger["transactions"]) == 1
    assert ledger["transactions"][0]["item"] == "可乐"
    assert ledger["transactions"][0]["amount"] == 1.8
