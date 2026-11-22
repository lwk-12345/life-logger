"""Tests for activity_window_state:
- 6-round v2 zero-model replay verification (DeepSeek & Gemini) generating final_state
- Fixed adversarial counterexamples:
  1. Counterexample A: Same activity_id but conflicting kind / actor refuses blind merge, logs conflict, preserves candidate
  2. Counterexample B: Item amount without evidence cannot overwrite verified evidence amount, logs conflict
  3. Counterexample C: Item status regression (completed -> planned) is intercepted, protects completed, logs conflict
  4. Counterexample D: Explicit cancellation / correction (completed -> cancelled) updates status and preserves revision reason
  5. Counterexample E: updates contract fields (related_transaction_ids_added & pending_item_references) are never dropped
  6. Counterexample F: Immutability (zero mutation of caller input)
  7. Counterexample G: Slide-out retention (never deletes known activities)
  8. Counterexample H: No association guessing by similar title, adjacent time, or same amount
  9. Counterexample I: No amount summing and no order/payment merge
  10. Counterexample J: Strict separation of spoken_at vs occurred_at (no guessing, null preserved)
  11. Counterexample K: Evidence deduplication by (source_id, segment_id, row_id, quote)
  12. Counterexample L: Unassociated items accumulation and deduplication
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(rel: str) -> "Path":
    """优先读入库夹具（tests/fixtures/...）；没有就退回仓库内原位置。

    换机器（新克隆）时 `work/`、`output/` 都不存在 —— 那时只有入库夹具可用。
    本机两者都在，走哪个都等价，所以不影响本地行为。
    """
    shipped = FIXTURES / rel
    return shipped if shipped.exists() else ROOT / rel




from life_logger.activity_window_state import (  # noqa: E402
    SCHEMA,
    ActivityWindowState,
    apply_window_update,
    replay_window_sequence,
)

DEEPSEEK_CALLS_DIR = _fixture("work/window-cadence-20261026/10min_v2/deepseek/calls")
GEMINI_CALLS_DIR = _fixture("work/window-cadence-20261026/10min_v2/gemini/calls")


# ------------------------------------------------------------------------------
# 1. 6轮真实 v2 零模型回放与 final_state 验证 (DeepSeek)
# ------------------------------------------------------------------------------
def test_deepseek_6_round_replay_generates_final_state():
    """回放已保存的 6 轮 DeepSeek v2 调用，验证 final_state 正确累积并解决滑出遗失。"""
    call_files = sorted(DEEPSEEK_CALLS_DIR.glob("call_*.json"))
    assert len(call_files) == 6, f"应有 6 轮调用，实际找到 {len(call_files)}"
    calls = [json.load(f.open(encoding="utf-8")) for f in call_files]

    final_state = replay_window_sequence(calls)

    # 1. Schema 与元数据验证
    assert final_state["schema_version"] == SCHEMA
    meta = final_state["metadata"]
    assert meta["total_windows_applied"] == 6
    assert meta["total_activities"] == 7, "6 轮累积应包含全部 7 个独立活动"
    assert meta["total_items"] > 0

    acts = final_state["activities"]

    # 2. 验证滑出未删除：第 1 轮创建的失眠讨论在第 6 轮依然存在（尽管第 6 轮输出中没有它）
    insomnia = acts.get("act-20261026-0604-insomnia-rumination")
    assert insomnia is not None, "第 1 轮活动在滑出视野后必须依然完整保留"
    assert insomnia["status"] == "completed"
    assert len(insomnia["items"]) == 4
    assert insomnia["provenance"]["created_in_window"] == 1
    assert insomnia["provenance"]["last_updated_in_window"] == 1

    # 3. 验证既有活动增量合并（第 2 轮点单 -> 第 3 轮完结与追加子项、追加交易）
    breakfast = acts.get("act-20261026-0614-breakfast-shop-order")
    assert breakfast is not None
    assert breakfast["status"] == "completed"
    assert len(breakfast["items"]) == 4, "3 个原项 + 1 个追加项"
    assert len(breakfast["provenance"]["revisions"]) == 2, "第 2 轮创建，第 3 轮增量更新"
    rev2 = breakfast["provenance"]["revisions"][1]
    assert rev2["operation"] == "upsert"
    assert "item-20261026-0628-01" in rev2["added_item_ids"]
    assert "txn-bg-20261026-0615-alipay-10" in breakfast["related_transaction_ids"]
    assert "txn-bg-20261026-0621-wechat-12" in breakfast["related_transaction_ids"]
    assert "amount_total" not in breakfast and "total_amount" not in breakfast

    # 4. 验证时间隔离：回顾性活动中 spoken_at 与 occurred_at 严格分离
    leg_training = acts.get("act-20261026-0634-leg-training-retrospective")
    assert leg_training is not None
    item_01 = next(it for it in leg_training["items"] if it["item_id"] == "item-20261026-0634-01")
    assert "2026-10-26" in item_01["spoken_at"]
    assert "2026-10-25" in item_01["occurred_at"]
    assert item_01["spoken_at"] != item_01["occurred_at"]

    item_02 = next(it for it in leg_training["items"] if it["item_id"] == "item-20261026-0634-02")
    assert item_02["occurred_at"] is None, "未说明具体钟点的口头询问，occurred_at 必须保持 None"

    # 5. 验证冲突跟踪
    assert len(final_state["conflicts"]) > 0


# ------------------------------------------------------------------------------
# 2. 6轮真实 v2 零模型回放与 final_state 验证 (Gemini)
# ------------------------------------------------------------------------------
def test_gemini_6_round_replay_generates_final_state():
    """回放已保存的 6 轮 Gemini v2 调用，验证多模型格式兼容性。"""
    call_files = sorted(GEMINI_CALLS_DIR.glob("call_*.json"))
    assert len(call_files) == 6
    calls = [json.load(f.open(encoding="utf-8")) for f in call_files]

    final_state = replay_window_sequence(calls)
    assert final_state["metadata"]["total_windows_applied"] == 6
    assert final_state["metadata"]["total_activities"] == 2
    acts = final_state["activities"]
    assert "act_20261026_060417_001" in acts
    assert "act_20261026_061435_001" in acts
    assert acts["act_20261026_061435_001"]["status"] == "completed"


# ------------------------------------------------------------------------------
# 3. 反例 A：同模型 activity_id 但不同 kind/主体不得盲并 (Kind/Actor Isolation)
# ------------------------------------------------------------------------------
def test_counterexample_a_same_id_diff_kind_or_actor_refuses_blind_merge():
    """反例 A：不同窗口中相同 activity_id 但 activity_type/source_label 发生冲突（餐饮/本人 vs 深蹲/他人），
    拒绝盲目合并，原活动不被污染，记录冲突并保留独立候选分支。
    """
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:15:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-shared-01",
                "title": "早餐点单",
                "activity_type": "meal",
                "source_label": "self",
                "status": "completed",
                "items": [{"item_id": "it-meal-1", "action": "点粉", "evidence": [{"quote": "来碗粉"}]}],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:25:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-shared-01",
                "title": "深蹲训练",
                "activity_type": "exercise",
                "source_label": "other",
                "status": "completed",
                "items": [{"item_id": "it-squat-1", "action": "深蹲", "evidence": [{"quote": "深蹲四十公斤"}]}],
            }],
        },
    }
    state = replay_window_sequence([w1, w2])
    act = state["activities"]["act-shared-01"]

    # 1. 原活动 items 必须保持纯净，绝不混入深蹲训练子项
    assert len(act["items"]) == 1
    assert act["items"][0]["item_id"] == "it-meal-1"

    # 2. 冲突被精确捕获
    assert len(state["conflicts"]) > 0
    assert len(act["provenance"]["conflict_history"]) > 0
    conf = act["provenance"]["conflict_history"][0]
    assert conf["field"] in ("activity_type", "source_label")
    assert "拒绝盲并" in conf["reason"]

    # 3. 保留输入候选及冲突分支，不丢失新信息
    cand_id = "act-shared-01#conflict_w2"
    assert cand_id in state["activities"]
    assert state["activities"][cand_id]["items"][0]["item_id"] == "it-squat-1"


# ------------------------------------------------------------------------------
# 4. 反例 B：新无证据 amount 不得替旧事实；同 item_id 跨支付强冲突不覆盖
# ------------------------------------------------------------------------------
def test_counterexample_b_item_amount_without_evidence_protected():
    """反例 B：子项已有的 10.0 元（带原文 quote 证据）被后续窗口中无证据的 50.0 元更新时，
    旧金额保持保护不被覆盖，且记入冲突历史。
    """
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:15:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-amt",
                "items": [{
                    "item_id": "it-pay-1",
                    "action": "付款",
                    "amount": 10.0,
                    "currency": "CNY",
                    "evidence": [{"quote": "十块钱"}],
                }],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:25:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-amt",
                "items": [{
                    "item_id": "it-pay-1",
                    "amount": 50.0,  # 无证据篡改金额
                    "evidence": [],
                }],
            }],
        },
    }
    state = replay_window_sequence([w1, w2])
    item = state["activities"]["act-amt"]["items"][0]

    # 金额保持保护，不被无证据的 50.0 覆盖
    assert item["amount"] == 10.0

    # 冲突被记录
    assert len(state["conflicts"]) > 0
    act = state["activities"]["act-amt"]
    assert any(c.get("field") == "amount" for c in act["provenance"]["conflict_history"])


# ------------------------------------------------------------------------------
# 5. 反例 C：子项 status 退化审计覆盖 (completed -> planned protected & logged)
# ------------------------------------------------------------------------------
def test_counterexample_c_item_status_regression_protected_and_logged():
    """反例 C：子项从 completed 退化为 planned 时，系统保护 completed 事实，并记入冲突历史。"""
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:15:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-stat",
                "status": "completed",
                "items": [{"item_id": "it-1", "action": "跑步", "status": "completed"}],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:25:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-stat",
                "status": "completed",
                "items": [{"item_id": "it-1", "action": "跑步", "status": "planned"}],
            }],
        },
    }
    state = replay_window_sequence([w1, w2])
    item = state["activities"]["act-stat"]["items"][0]

    # 子项保持 completed 保护
    assert item["status"] == "completed"

    # 冲突被记录在 conflict_history 与全局 conflicts
    assert len(state["conflicts"]) > 0
    act = state["activities"]["act-stat"]
    assert any(c.get("field") == "status" and c.get("item_id") == "it-1" for c in act["provenance"]["conflict_history"])


# ------------------------------------------------------------------------------
# 6. 反例 D：明确取消/否定允许纠正，不永久保护旧 completed
# ------------------------------------------------------------------------------
def test_counterexample_d_completed_to_cancelled_preserves_revision():
    """反例 D：当活动或子项发生明确取消/否定纠正时，允许更新为 cancelled，并记录修订原因。"""
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-meeting",
                "status": "completed",
                "title": "会议室预定",
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:20:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-meeting",
                "status": "cancelled",
                "title": "会议室预定",
            }],
            "updates": ["act-meeting: 用户明确表示已取消会议室预定"],
        },
    }
    state = replay_window_sequence([w1, w2])
    act = state["activities"]["act-meeting"]
    assert act["status"] == "cancelled"
    assert len(act["provenance"]["revisions"]) == 2
    rev2 = act["provenance"]["revisions"][1]
    assert rev2["operation"] == "upsert"
    assert "status:completed->cancelled" in rev2["changed_fields"]
    assert "用户明确表示已取消会议室预定" in rev2["reason"]


# ------------------------------------------------------------------------------
# 7. 反例 E：updates 契约完整性 (related_transaction_ids_added & pending_item_references)
# ------------------------------------------------------------------------------
def test_counterexample_e_updates_contract_preserves_added_tx_and_pending_items():
    """反例 E：updates 中声明的 related_transaction_ids_added 必须追加至活动；
    new_item_ids 中未带子项主体的引用必须留存为 pending_item_references，绝不凭空捏造假子项。
    """
    raw_update = {
        "activity_id": "act-tx-drop",
        "operation": "upsert",
        "reason": "追加关联交易与引用",
        "related_transaction_ids_added": ["txn-secret-999"],
        "new_item_ids": ["it-unknown-ref-888"],
    }
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "activities": [{"activity_id": "act-tx-drop", "status": "completed", "items": []}],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:20:00+08:00",
        "response": {
            "activities": [{"activity_id": "act-tx-drop", "status": "completed"}],
            "updates": [raw_update],
        },
    }
    state = replay_window_sequence([w1, w2])
    act = state["activities"]["act-tx-drop"]

    # 1. 交易 ID 绝不丢弃
    assert "txn-secret-999" in act.get("related_transaction_ids", []), "related_transaction_ids_added 必须完整保存"

    # 2. 未知 item_id 留存 pending，绝不凭空造假子项
    assert "it-unknown-ref-888" in act.get("pending_item_references", [])
    assert len(act.get("items", [])) == 0, "未知引用不可凭空制造假子项！"


# ------------------------------------------------------------------------------
# 8. 反例 F：严格不可变性（Zero Mutation of Caller Input）
# ------------------------------------------------------------------------------
def test_counterexample_f_immutability_zero_mutation_of_input():
    """反例 F：传入的 window 字典与 state 字典，在执行 apply_window_update 后原样保持不变。"""
    raw_window = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-immu",
                "title": "测试",
                "items": [{"item_id": "it-1", "action": "打卡"}],
            }],
        },
    }
    raw_copy = copy.deepcopy(raw_window)

    state = apply_window_update(None, raw_window)
    assert raw_window == raw_copy, "调用方传入的 raw_window 字典被原位篡改！"

    state_copy = copy.deepcopy(state)
    _ = apply_window_update(state, raw_window)
    assert state == state_copy, "调用方传入的 state 字典被原位篡改！"


# ------------------------------------------------------------------------------
# 9. 反例 G：窗口滑出绝不删除已知活动 (Slide-out Retention)
# ------------------------------------------------------------------------------
def test_counterexample_g_window_slide_out_never_deletes_known_activity():
    """反例 G：第 1 轮产生活动 A，第 2/3 轮模型输出完全不包含 A；最终状态必须依然包含 A 且不损坏。"""
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-early-01",
                "title": "晨起洗漱",
                "status": "completed",
                "reported_at": "2026-10-26T06:05:00+08:00",
                "items": [{"item_id": "it-01", "action": "刷牙", "spoken_at": "2026-10-26T06:05:00+08:00"}],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:20:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-mid-02",
                "title": "出门散步",
                "status": "in_progress",
                "items": [{"item_id": "it-02", "action": "走下楼", "spoken_at": "2026-10-26T06:15:00+08:00"}],
            }],
        },
    }
    w3 = {
        "index": 3,
        "cutoff": "2026-10-26T06:30:00+08:00",
        "response": {"activities": []},
    }

    state = replay_window_sequence([w1, w2, w3])
    assert "act-early-01" in state["activities"], "滑出窗口的活动不得被删除"
    assert "act-mid-02" in state["activities"]
    assert state["activities"]["act-early-01"]["provenance"]["created_in_window"] == 1


# ------------------------------------------------------------------------------
# 10. 反例 H：不得靠相似标题、时间临近或同额猜关联 (No Guessing Association)
# ------------------------------------------------------------------------------
def test_counterexample_h_no_association_by_similar_title_adjacent_time_or_same_amount():
    """反例 H：两笔活动即使标题极似、金额完全一致（10元）、时间相近，只要 activity_id 不同，绝不合并。"""
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:15:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-order-alpha",
                "title": "早餐点瘦肉粉",
                "status": "completed",
                "reported_at": "2026-10-26T06:14:00+08:00",
                "items": [{
                    "item_id": "item-alpha",
                    "action": "口头点单瘦肉粉",
                    "amount": 10.0,
                    "currency": "CNY",
                    "spoken_at": "2026-10-26T06:14:00+08:00",
                }],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:25:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-order-beta",
                "title": "早餐吃瘦肉粉",
                "status": "completed",
                "reported_at": "2026-10-26T06:15:00+08:00",
                "items": [{
                    "item_id": "item-beta",
                    "action": "吃瘦肉粉",
                    "amount": 10.0,
                    "currency": "CNY",
                    "spoken_at": "2026-10-26T06:15:00+08:00",
                }],
            }],
        },
    }

    state = replay_window_sequence([w1, w2])
    assert "act-order-alpha" in state["activities"]
    assert "act-order-beta" in state["activities"]
    assert state["activities"]["act-order-alpha"]["items"][0]["item_id"] == "item-alpha"
    assert state["activities"]["act-order-beta"]["items"][0]["item_id"] == "item-beta"


# ------------------------------------------------------------------------------
# 11. 反例 I：不加金额合计，不合并订单与支付
# ------------------------------------------------------------------------------
def test_counterexample_i_no_amount_summing_and_no_order_payment_merge():
    """反例 I：子项中包含点单 10 元，并关联了两笔交易 10 元与 12 元；
    系统绝不得将金额加总，绝不合并订单与交易。
    """
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:20:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-breakfast",
                "title": "早餐点单",
                "status": "in_progress",
                "items": [{
                    "item_id": "it-order",
                    "action": "口头点单",
                    "amount": 10.0,
                    "currency": "CNY",
                }],
                "related_transaction_ids": ["txn-alipay-10", "txn-wechat-12"],
            }],
        },
    }

    state = replay_window_sequence([w1])
    act = state["activities"]["act-breakfast"]
    assert "amount_total" not in act
    assert "total_amount" not in act
    assert "amount_sum" not in act
    assert act["related_transaction_ids"] == ["txn-alipay-10", "txn-wechat-12"]
    assert len(act["items"]) == 1


# ------------------------------------------------------------------------------
# 12. 反例 J：spoken_at 与 occurred_at 严格分离，未知不猜
# ------------------------------------------------------------------------------
def test_counterexample_j_occurred_at_vs_spoken_at_separation():
    """反例 J：在 06:34 讲述'昨天下午六点练腿'：
    item 1 的 spoken_at 是 06:34，occurred_at 是昨天 18:00；
    item 2 的'晚上睡不着怎么办'，occurred_at 必须是 None，绝不得自动填充为 06:34。
    """
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:40:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-retrospective",
                "title": "回顾练腿与失眠",
                "items": [
                    {
                        "item_id": "it-leg",
                        "action": "自述昨天练腿",
                        "spoken_at": "2026-10-26T06:34:00+08:00",
                        "occurred_at": "2026-10-25T18:00:00+08:00",
                    },
                    {
                        "item_id": "it-insomnia",
                        "action": "询问睡不着怎么办",
                        "spoken_at": "2026-10-26T06:34:10+08:00",
                        "occurred_at": None,
                    },
                ],
            }],
        },
    }

    state = replay_window_sequence([w1])
    items = state["activities"]["act-retrospective"]["items"]
    assert items[0]["spoken_at"] == "2026-10-26T06:34:00+08:00"
    assert items[0]["occurred_at"] == "2026-10-25T18:00:00+08:00"
    assert items[1]["spoken_at"] == "2026-10-26T06:34:10+08:00"
    assert items[1]["occurred_at"] is None


# ------------------------------------------------------------------------------
# 13. 反例 K：引用证据严格排重
# ------------------------------------------------------------------------------
def test_counterexample_k_evidence_deduplication():
    """反例 K：同一子项在多轮中反复提供相同的 quote 引用，证据列表必须自动排重。"""
    ev = {"source_id": "src-1", "segment_id": "seg-1", "row_id": 1, "quote": "重复引用"}
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-dup",
                "items": [{"item_id": "it-dup", "action": "测试", "evidence": [ev, ev]}],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:20:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-dup",
                "items": [{"item_id": "it-dup", "action": "测试", "evidence": [ev]}],
            }],
        },
    }

    state = replay_window_sequence([w1, w2])
    item = state["activities"]["act-dup"]["items"][0]
    assert len(item["evidence"]) == 1


# ------------------------------------------------------------------------------
# 14. 反例 L：未关联子项（unassociated_items）累积与排重
# ------------------------------------------------------------------------------
def test_counterexample_l_unassociated_items_preservation_and_deduplication():
    """反例 L：未关联子项持续累积与排重。"""
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "unassociated_items": [{
                "item_id": "unass-01",
                "action": "商户收款语音播报21元",
                "evidence": [{"quote": "收款21元"}],
            }],
        },
    }
    w2 = {
        "index": 2,
        "cutoff": "2026-10-26T06:20:00+08:00",
        "response": {
            "unassociated_items": [
                {
                    "item_id": "unass-01",
                    "action": "商户收款语音播报21元",
                    "evidence": [{"quote": "收款21元"}],
                },
                {
                    "item_id": "unass-02",
                    "action": "自言自语刷手机",
                },
            ],
        },
    }

    state = replay_window_sequence([w1, w2])
    unass = state["unassociated_items"]
    assert len(unass) == 2
    ids = {x["item_id"] for x in unass}
    assert ids == {"unass-01", "unass-02"}


# ------------------------------------------------------------------------------
# 15. 反例 M：window_history 中 provider 字段正确持久化
# ------------------------------------------------------------------------------
def test_counterexample_m_provider_field_persisted_in_window_history():
    """反例 M：验证 window_history 正确保留并持久化 provider 字段。"""
    w1 = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "response": {
            "activities": [{
                "activity_id": "act-prov",
                "title": "测试提供方",
                "items": [{"item_id": "it-prov", "action": "测试", "evidence": []}],
            }],
        },
    }
    meta = {
        "index": 1,
        "cutoff": "2026-10-26T06:10:00+08:00",
        "lookback": "2026-10-26T05:55:00+08:00",
        "record_count": 3,
        "provider": "deepseek",
    }
    state = apply_window_update({}, w1, window_meta=meta)
    assert len(state["window_history"]) == 1
    assert state["window_history"][0]["provider"] == "deepseek"
    assert state["window_history"][0]["cutoff"] == "2026-10-26T06:10:00+08:00"

    # 验证状态反序列化后 provider 依然完整存在
    rehydrated = ActivityWindowState(state)
    assert rehydrated.window_history[0]["provider"] == "deepseek"

