"""daily_brief 跨商品购买摘要：显式追加才并会话；交易逐笔保留；缺商品只报 pending。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import life_logger.event_view as event_view  # noqa: E402
from life_logger.daily_brief import build_daily_brief  # noqa: E402

REC = "watch_F786"


def ev(eid, item, amount, *, offset, quote, label="self", tx=None, seg="000026"):
    data = {"expense": {"amount": amount, "currency": "CNY", "item": item}}
    if tx:
        data["transaction_id"] = tx
    return {
        "event_id": eid, "title": f"购买{item} (¥{amount:.2f})", "categories": ["expense", "food"],
        "activity_type": "meal",
        "time": {"absolute": "2026-10-25T00:19:46+08:00", "precision": "minute"},
        "data": data,
        "evidence": {"quote": quote, "source_label": label, "recording_id": REC,
                     "segment_id": seg, "start_offset_seconds": offset, "claim_type": "order"},
    }


DUCK = ev("evt_duck", "鸭腿", 10.0, offset=10.0, quote="来一份鸭腿 十块", tx="T1")
ADDON = ev("evt_sausage", "烤肠", 3.0, offset=44.0, quote="我加个烤肠 三块", tx="T2")
OTHER = ev("evt_headphones", "耳机", 199.0, offset=900.0, quote="买了个耳机", tx="T3", seg="000040")


def _brief(monkeypatch, events):
    monkeypatch.setattr(event_view, "build_event_view",
                        lambda *a, **k: {"events": copy.deepcopy(events),
                                         "metadata": {"timezone": "Asia/Shanghai", "sources": {}}})
    return build_daily_brief(Path("/tmp/mock-root"), date="2026-10-25")


def test_purchase_sessions_present_and_contract(monkeypatch):
    out = _brief(monkeypatch, [DUCK, ADDON])
    assert "purchase_sessions" in out
    assert out["purchase_session_contract"]["schema"] == "purchase-session-v1"
    assert out["purchase_session_contract"]["only_explicit_addon"] is True
    for session in out["purchase_sessions"]:
        assert session["member_ids"], "必须有 member_ids"
        assert session["state"] in {"unknown", "confirmed"}


def test_addon_items_keep_two_transactions_and_real_times(monkeypatch):
    out = _brief(monkeypatch, [DUCK, ADDON])
    sessions = out["purchase_sessions"]
    assert len(sessions) == 1
    session = sessions[0]
    assert session["member_ids"] == ["evt_duck", "evt_sausage"]
    amounts = [item["amount"] for item in session["items"]]
    assert amounts == [10.0, 3.0], "金额逐项列出，不做合计"
    assert "display_total" not in session
    tx = [ref["id"] for item in session["items"] for ref in item["transaction_refs"]]
    assert tx == ["T1", "T2"], "两笔交易逐笔保留"
    assert all(item["time"]["precision"] == "minute" for item in session["items"])
    assert session["state"] == "unknown", "不判断是否已付款"


def test_non_addon_purchase_is_separate_session(monkeypatch):
    out = _brief(monkeypatch, [DUCK, OTHER])
    assert len(out["purchase_sessions"]) == 2, "无显式追加 → 不同会话"
    for session in out["purchase_sessions"]:
        assert len(session["member_ids"]) == 1


def test_same_recording_close_time_without_addon_is_not_merged(monkeypatch):
    close = ev("evt_x", "可乐", 3.0, offset=20.0, quote="来个可乐", tx="T4")
    out = _brief(monkeypatch, [DUCK, close])
    assert len(out["purchase_sessions"]) == 2, "同录音/时间近不是关联证据"


def test_missing_canonical_item_only_pending(monkeypatch):
    out = _brief(monkeypatch, [DUCK])
    # 没有烤肠 canonical → 不生成烤肠记录，也不虚增交易
    assert [item["item"] for s in out["purchase_sessions"] for item in s["items"]] == ["鸭腿"]
    tx = [ref["id"] for s in out["purchase_sessions"] for item in s["items"]
          for ref in item["transaction_refs"]]
    assert tx == ["T1"]


def test_long_activity_sub_items_keep_positions(monkeypatch):
    """长活动（多子项）不因会话摘要被合并成一条，子项时间保持原位。"""
    items = [ev(f"evt_{i}", f"菜{i}", 10.0 + i, offset=10.0 + i * 5, quote=f"来一份菜{i}",
                tx=f"T{i}", seg=f"0000{30+i}") for i in range(4)]
    out = _brief(monkeypatch, items)
    assert len(out["purchase_sessions"]) == 4
    assert sorted(i["time"]["occurred_at"] for s in out["purchase_sessions"] for i in s["items"])


# ---------------------------------------------------------------- Q410：复用 expense_policy 同源判据

def test_ledger_accepted_order_with_pos_text_still_joins_session(monkeypatch):
    """报价证据被账本认可（claim_type=order）时，即使 quote 混有播报句也进会话、两笔交易。"""
    duck = dict(DUCK)
    duck["evidence"] = dict(DUCK["evidence"], claim_type="order")
    sausage = dict(ADDON)
    sausage["evidence"] = dict(
        ADDON["evidence"], claim_type="order",
        quote="拿一个是吧 对 多少钱 十块 支付宝到账十元 我加个烤肠 三块 支付宝到账三元",
        related_quotes=[
            # 证据合同：子句必须带坐标（segment/absolute），否则不可作为独立订单证据
            {"quote": "我加个烤肠", "role": "order", "source_label": "self",
             "segment_id": "000026", "absolute": "2026-10-25T00:20:02+08:00"},
            {"quote": "三块", "role": "price", "source_label": "non_self",
             "segment_id": "000026", "absolute": "2026-10-25T00:20:04+08:00"},
        ],
    )
    out = _brief(monkeypatch, [duck, sausage])
    sessions = out["purchase_sessions"]
    assert len(sessions) == 1
    session = sessions[0]
    assert session["member_ids"] == ["evt_duck", "evt_sausage"]
    assert [i["amount"] for i in session["items"]] == [10.0, 3.0]
    tx = [r["id"] for i in session["items"] for r in i["transaction_refs"]]
    assert tx == ["T1", "T2"], "订单与 POS 分别保留，交易不去重"


def test_pos_only_event_is_not_an_order(monkeypatch):
    """只有 POS 播报、账本未认可 → 不进会话（不因 addon 前置放松 POS 判据）。"""
    pos = {"event_id": "evt_pos", "title": "支付宝到账三元", "categories": ["expense"],
           "activity_type": "purchase", "data": {"expense": {"amount": 3.0, "currency": "CNY"}},
           "evidence": {"quote": "支付宝到账三元", "source_label": "non_self",
                        "recording_id": REC, "segment_id": "000026", "claim_type": "payment"},
           "time": {"absolute": "2026-10-25T00:20:02+08:00", "precision": "minute"}}
    out = _brief(monkeypatch, [DUCK, pos])
    assert all("evt_pos" not in s["member_ids"] for s in out["purchase_sessions"]), "POS 不得并入订单会话"


def test_other_actor_or_other_recording_not_merged(monkeypatch):
    other_person = dict(ADDON, event_id="evt_other")
    other_person["evidence"] = dict(ADDON["evidence"], source_label="non_self", claim_type="order")
    other_rec = dict(ADDON, event_id="evt_other_rec")
    other_rec["evidence"] = dict(ADDON["evidence"], recording_id="watch_OTHER", claim_type="order")
    out = _brief(monkeypatch, [DUCK, other_person, other_rec])
    duck_sessions = [s for s in out["purchase_sessions"] if "evt_duck" in s["member_ids"]]
    assert len(duck_sessions) == 1, "鸭腿会话必须存在且唯一"
    duck_members = set(duck_sessions[0]["member_ids"])
    assert "evt_other" not in duck_members, "不同人不得并入本人会话"
    assert "evt_other_rec" not in duck_members, "不同录音不得并入本人会话"
    # 异录音的订单允许**独立保留**（不是删除/不是合并）
    assert any("evt_other_rec" in s["member_ids"] and s["member_ids"] != duck_sessions[0]["member_ids"]
               for s in out["purchase_sessions"]), "异录音应作为独立会话保留"


def test_order_clause_without_coordinates_is_not_evidence(monkeypatch):
    """缺坐标的 related_quotes 不得作为独立订单证据（负例）。"""
    duck = dict(DUCK, event_id="evt_duck2")
    sausage = dict(ADDON, event_id="evt_sausage2")
    sausage["evidence"] = dict(
        ADDON["evidence"], claim_type="order",
        quote="我加个烤肠 三块 支付宝到账三元",
        related_quotes=[
            {"quote": "我加个烤肠", "role": "order", "source_label": "self"},   # 无 segment/absolute
            {"quote": "三块", "role": "price", "source_label": "non_self"},
        ],
    )
    out = _brief(monkeypatch, [duck, sausage])
    duck_session = next(s for s in out["purchase_sessions"] if "evt_duck2" in s["member_ids"])
    assert "evt_sausage2" not in duck_session["member_ids"], "缺坐标证据不得合并"
