"""occurred_at 产出校验：判档正确、清空可回溯、幂等、不误伤现场动作。"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import occurred_at_audit as A  # noqa: E402

TS = "2026-11-02T10:16:20.134000+08:00"


def item(action="", quote="", occurred=TS, spoken=TS):
    it = {"action": action, "occurred_at": occurred, "spoken_at": spoken}
    if quote:
        it["evidence"] = [{"quote": quote}]
    return it


# ── 判档：三档优先级
def test_media_cue_wins_over_live_cue():
    """『今天给大家讲一个故事』同时含"今天"，但媒体体裁优先 → 清空。"""
    st = A.audit_item(item("播报节目开场", "今天给大家讲一个故事"))
    assert st["reason"] == A.REASON_MEDIA and st["cleared"] and st["violation"]


def test_retro_cue_clears():
    st = A.audit_item(item("回顾昨天的事", "昨天一晚上基本没睡"))
    assert st["reason"] == A.REASON_RETRO and st["cleared"] is True


def test_live_cue_kept_and_not_violation():
    st = A.audit_item(item("点餐", "来一份杀猪河粉"))
    assert st["reason"] == A.REASON_LIVE
    assert st["cleared"] is False and st["violation"] is False


def test_no_anchor_clears_by_default():
    st = A.audit_item(item("要求找到角色链路", "像这种角色链路什么东西都要去"))
    assert st["reason"] == A.REASON_NO_ANCHOR and st["cleared"] is True


# ── 可比性：不等/为空/坏值一律不动
def test_differing_timestamps_are_not_touched():
    st = A.audit_item(item(occurred="2026-11-02T09:00:00+08:00", spoken=TS))
    assert st["reason"] == A.REASON_DIFFER and st["cleared"] is False


def test_microsecond_difference_still_counts_as_equal():
    """秒级相等即违规：微秒不同不影响判档（这是规格里明确的"秒级"）。"""
    st = A.audit_item(item(occurred="2026-11-02T10:16:20.000000+08:00", spoken=TS))
    assert st["reason"] != A.REASON_DIFFER


def test_null_and_garbage_are_safe():
    assert A.audit_item(item(occurred=None))["reason"] == A.REASON_NULL
    assert A.audit_item({"occurred_at": "not-a-time", "spoken_at": TS})["reason"] == A.REASON_NULL
    assert A.audit_item({"occurred_at": TS})["reason"] == A.REASON_DIFFER   # 没有 spoken_at


# ── 应用：清空 + 可回溯 + 幂等
def test_apply_audit_clears_and_preserves_original():
    act = {"items": [item("要求找到角色链路", "像这种角色链路")]}
    stats = A.apply_audit(act, clear=True)
    assert stats["cleared"] == 1
    it = act["items"][0]
    assert it["occurred_at"] is None
    assert it["occurred_at_cleared"] == TS          # 原文保留，可回滚
    assert it["occurred_at_audit"]["reason"] == A.REASON_NO_ANCHOR


def test_apply_audit_dry_run_does_not_touch_data():
    act = {"items": [item("要求找到角色链路", "像这种角色链路")]}
    stats = A.apply_audit(act, clear=False)
    assert stats["cleared"] == 1                    # 判定为"应清空"
    assert act["items"][0]["occurred_at"] == TS     # 但数据没动
    assert act["items"][0]["occurred_at_audit"]["cleared"] is True


def test_apply_audit_is_idempotent():
    act = {"items": [item("要求找到角色链路", "像这种角色链路")]}
    A.apply_audit(act, clear=True)
    first = dict(act["items"][0])
    stats = A.apply_audit(act, clear=True)
    assert stats["already_audited"] == 1
    assert act["items"][0]["occurred_at"] is None
    assert act["items"][0]["occurred_at_cleared"] == first["occurred_at_cleared"]


def test_live_item_survives_apply():
    act = {"items": [item("点餐", "来一份杀猪河粉")]}
    A.apply_audit(act, clear=True)
    assert act["items"][0]["occurred_at"] == TS      # 现场动作保留


# ── 卡片级：只标注，不清空
def test_card_level_only_annotates():
    act = {"occurrence_start": TS,
           "items": [item("确认", "对。"), item("回应", "嗯。")]}
    card = A.audit_activity_card(act)
    assert card["comparable"] and card["suspicious"] is True
    assert act["items"][0]["occurred_at"] == TS, "卡片级校验不得改 item 数据"


def test_payload_summary_counts():
    payload = {"activities": [
        {"occurrence_start": TS, "items": [item("要求找到角色链路", "角色链路"),
                                           item("点餐", "来一份杀猪河粉")]},
    ]}
    totals = A.audit_payload(payload, clear=True)
    assert totals["items"] == 2 and totals["cleared"] == 1 and totals["kept"] == 1
    assert totals["by_reason"][A.REASON_NO_ANCHOR] == 1
    assert payload["activities"][0]["items"][1]["occurred_at"] == TS
