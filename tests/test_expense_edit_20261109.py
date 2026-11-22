"""首页消费明细“改金额”功能与键稳定性/兜底找回测试（2026-11-09）。"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from life_logger import day_facts
from life_logger import expense_corrections as ec
from life_logger import judgment_queue as jq
from life_logger.web_http import _Handler as LocalWebHandler


def _jev_20261109_activities():
    """基于 2026-11-09 真实凌晨口述消费（jev/2026-11-09.json）构造的 v3 活动样本。"""
    return [
        {
            "activity_id": "act_20261109_expense_report_01",
            "title": "补记食品等多笔消费",
            "category": "消费·购物",
            "lifecycle": "live",
            "status": "completed",
            "fact_schema_version": 3,
            "spoken_at": "2026-11-09T00:25:09.624000+08:00",
            "spoken_end": "2026-11-09T00:25:30.114000+08:00",
            "items": [
                {
                    "item_id": "item_expense_chicken_01",
                    "action": "补记鸡胸肉丸消费105元",
                    "fact_type": "expense",
                    "status": "completed",
                    "amount": 105,
                    "currency": "CNY",
                    "payment_status": "paid",
                    "amount_kind": "money",
                    "expense": {
                        "amount": 105,
                        "currency": "CNY",
                        "item": "鸡胸肉丸",
                        "payment_status": "paid",
                    },
                    "spoken_at": "2026-11-09T00:25:09.624000+08:00",
                    "evidence": [
                        {"quote": "补充一下两个消费。", "source_label": "self"},
                        {"quote": "呃，三个消费，一个是鸡胸，鸡胸肉丸，一共是一百零五。", "source_label": "self"},
                    ],
                },
                {
                    "item_id": "item_expense_beef_01",
                    "action": "补记十包香辣土豆牛肉51元",
                    "fact_type": "expense",
                    "status": "completed",
                    "amount": 51,
                    "currency": "CNY",
                    "payment_status": "paid",
                    "amount_kind": "money",
                    "expense": {
                        "amount": 51,
                        "currency": "CNY",
                        "item": "香辣土豆牛肉十包",
                        "payment_status": "paid",
                    },
                    "spoken_at": "2026-11-09T00:25:09.624000+08:00",
                    "evidence": [
                        {"quote": "补充一下两个消费。", "source_label": "self"},
                        {"quote": "香辣土豆牛肉。", "source_label": "self"},
                        {"quote": "十包，这是五十一。", "source_label": "self"},
                    ],
                },
                {
                    "item_id": "item_expense_noodles_02",
                    "action": "将面条消费修正为5.2元",
                    "fact_type": "expense",
                    "status": "completed",
                    "amount": 5.2,
                    "currency": "CNY",
                    "payment_status": "paid",
                    "amount_kind": "money",
                    "expense": {
                        "amount": 5.2,
                        "currency": "CNY",
                        "item": "面条",
                        "payment_status": "paid",
                    },
                    "spoken_at": "2026-11-09T00:25:30.114000+08:00",
                    "evidence": [
                        {"quote": "面条是十五。", "source_label": "self"},
                        {"quote": "五块二", "source_label": "self"},
                    ],
                },
            ],
        }
    ]


def _write_jev_day(root: Path, day: str, activities: list[dict]) -> None:
    jev_dir = root / "activity-timeline" / "jev"
    jev_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "activity-timeline-v1",
        "date": day,
        "route": "jev",
        "status": "ready",
        "activities": activities,
    }
    (jev_dir / f"{day}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(day_facts, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "output" / "local-web"
    root.mkdir(parents=True, exist_ok=True)
    _write_jev_day(root, "2026-11-09", _jev_20261109_activities())
    return root


def test_every_transaction_exposes_stable_entry_key(temp_root):
    ledger = day_facts.day_expense_ledger([], "2026-11-09", root=temp_root)
    assert ledger["total_amount"] == 161.2
    tx_by_key = {tx["entry_key"]: tx for tx in ledger["payment_transactions"]}
    assert set(tx_by_key.keys()) == {
        "act_20261109_expense_report_01#it1",
        "act_20261109_expense_report_01#it2",
        "act_20261109_expense_report_01#it3",
    }
    noodle_tx = tx_by_key["act_20261109_expense_report_01#it3"]
    assert noodle_tx["item"] == "面条"
    assert noodle_tx["amount"] == 5.2
    assert noodle_tx["event_id"] == "act_20261109_expense_report_01#it3"


def test_edit_amount_and_undo_updates_total_and_metadata(temp_root):
    entry_key = "act_20261109_expense_report_01#it3"
    res = ec.edit_expense(temp_root, date="2026-11-09", entry_key=entry_key, amount=15.2)
    assert res["ok"] is True
    assert res["decision"]["original_amount"] == 5.2
    assert res["decision"]["amount"] == 15.2
    assert res["total_amount"] == 171.2
    assert res["expense_ledger"]["total_amount"] == 171.2

    # 检查落盘的 ledger-review.json 字段
    review_doc = json.loads((temp_root / "ledger-review.json").read_text(encoding="utf-8"))
    dec = review_doc["decisions"][entry_key]
    assert dec["action"] == "confirm"
    assert dec["amount"] == 15.2
    assert dec["original_amount"] == 5.2
    assert dec["original_label"] == "面条"
    assert dec["quote"] == "面条是十五。"
    assert dec["quotes"] == ["面条是十五。", "五块二"]
    assert dec["clock"] == "00:25"
    assert dec["decided_by"] == "user"
    assert dec["basis"] == "首页改金额"
    assert dec["decided_at"]

    # 重新读账本：合计 +10（161.2 -> 171.2），且该笔带 edited=True 与 original_amount=5.2
    ledger = day_facts.day_expense_ledger([], "2026-11-09", root=temp_root)
    assert ledger["total_amount"] == 171.2
    noodle_tx = next(t for t in ledger["payment_transactions"] if t["entry_key"] == entry_key)
    assert noodle_tx["amount"] == 15.2
    assert noodle_tx["edited"] is True
    assert noodle_tx["original_amount"] == 5.2
    assert ledger["unmatched_edits"] == []

    # 撤销后恢复 5.2 与原合计 161.2
    undo_res = ec.undo_expense_edit(temp_root, date="2026-11-09", entry_key=entry_key)
    assert undo_res["ok"] is True
    assert undo_res["total_amount"] == 161.2
    noodle_after = next(t for t in undo_res["expense_ledger"]["payment_transactions"] if t["entry_key"] == entry_key)
    assert noodle_after["amount"] == 5.2
    assert not noodle_after.get("edited")


def test_v3_rerun_id_and_index_shift_still_matches_by_fallback(temp_root):
    old_key = "act_20261109_expense_report_01#it3"
    ec.edit_expense(temp_root, date="2026-11-09", entry_key=old_key, amount=15.2)

    # 模拟 v3 重跑同一天：activity_id 变了，且面条在 items 里的序号从 #it3 变成了 #it1
    base_items = _jev_20261109_activities()[0]["items"]
    rerun_activities = [
        {
            "activity_id": "act_20261109_rerun_new_id_99",
            "title": "凌晨记账重跑后新活动ID",
            "category": "消费·购物",
            "lifecycle": "live",
            "status": "completed",
            "fact_schema_version": 3,
            "spoken_at": "2026-11-09T00:25:09.624000+08:00",
            "items": [base_items[2], base_items[0], base_items[1]],
        }
    ]
    _write_jev_day(temp_root, "2026-11-09", rerun_activities)

    ledger = day_facts.day_expense_ledger([], "2026-11-09", root=temp_root)
    assert ledger["total_amount"] == 171.2
    assert ledger["unmatched_edits"] == []
    new_key = "act_20261109_rerun_new_id_99#it1"
    noodle_tx = next(t for t in ledger["payment_transactions"] if t["event_id"] == new_key)
    assert noodle_tx["entry_key"] == old_key
    assert noodle_tx["amount"] == 15.2
    assert noodle_tx["edited"] is True
    assert noodle_tx["original_amount"] == 5.2
    assert noodle_tx["matched_by"] == "fallback"
    assert noodle_tx["stored_entry_key"] == old_key

    # 前端带着新 entry_key 请求撤销，也必须能把旧键对应的修改撤销掉
    undo_res = ec.undo_expense_edit(temp_root, date="2026-11-09", entry_key=new_key)
    assert undo_res["ok"] is True
    assert undo_res["entry_key"] == old_key
    assert undo_res["total_amount"] == 161.2


def test_unmatched_edit_is_never_silently_dropped_and_can_be_undone(temp_root):
    old_key = "act_20261109_expense_report_01#it3"
    ec.edit_expense(temp_root, date="2026-11-09", entry_key=old_key, amount=15.2)

    # 模拟 v3 重跑后该笔面条完全消失（只剩前两笔 105 + 51 = 156）
    base_items = _jev_20261109_activities()[0]["items"]
    without_noodles = [
        {
            "activity_id": "act_20261109_without_noodles",
            "title": "只剩前两笔",
            "category": "消费·购物",
            "lifecycle": "live",
            "status": "completed",
            "fact_schema_version": 3,
            "spoken_at": "2026-11-09T00:25:09.624000+08:00",
            "items": [base_items[0], base_items[1]],
        }
    ]
    _write_jev_day(temp_root, "2026-11-09", without_noodles)

    ledger = day_facts.day_expense_ledger([], "2026-11-09", root=temp_root)
    assert ledger["total_amount"] == 156.0
    assert len(ledger["unmatched_edits"]) == 1
    unmatched = ledger["unmatched_edits"][0]
    assert unmatched["entry_key"] == old_key
    assert unmatched["amount"] == 15.2
    assert unmatched["original_amount"] == 5.2
    assert unmatched["label"] == "面条"
    assert unmatched["quote"] == "面条是十五。"
    assert unmatched["clock"] == "00:25"

    # 用户在卡片上点“撤销”可清理这笔未匹配修改
    undo_res = ec.undo_expense_edit(temp_root, date="2026-11-09", entry_key=old_key)
    assert undo_res["ok"] is True
    assert undo_res["unmatched_edits"] == []


def test_does_not_affect_exclude_pending_or_duplicate_decisions(temp_root):
    # 先把第一笔设为 pending，第二笔设为 exclude，再改第三笔面条金额，再测 decide_duplicate
    review_path = temp_root / "ledger-review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema": "ledger-review-v1",
                "decisions": {
                    "act_20261109_expense_report_01#it1": {
                        "action": "pending",
                        "reason": "待核实鸡胸肉丸金额",
                    },
                    "act_20261109_expense_report_01#it2": {
                        "action": "exclude",
                        "reason": "排除测试",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ec.edit_expense(
        temp_root,
        date="2026-11-09",
        entry_key="act_20261109_expense_report_01#it3",
        amount=15.2,
    )
    jq.decide_duplicate(temp_root, event_id="manual_dup_1", action="same", date="2026-11-09")

    ledger = day_facts.day_expense_ledger([], "2026-11-09", root=temp_root)
    # 鸡胸肉丸 105 在 pending，牛肉 51 被 exclude，面条改成 15.2 在已付合计
    assert ledger["total_amount"] == 15.2
    assert any(
        p["event_id"] == "act_20261109_expense_report_01#it1"
        for p in ledger["pending_transactions"]
    )
    assert any(
        e["event_id"] == "act_20261109_expense_report_01#it2"
        for e in ledger["excluded_events"]
    )
    review_doc = json.loads(review_path.read_text(encoding="utf-8"))
    assert review_doc["decisions"]["manual_dup_1"]["action"] == "exclude"


def _invoke_post(root: Path, path: str, payload: dict) -> tuple[int, dict]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sent: dict = {}
    handler = LocalWebHandler.__new__(LocalWebHandler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(store=SimpleNamespace(root=root))
    handler._require_api_auth = lambda write=False: True
    handler._send_json = lambda data, status=200: sent.update(status=status, data=data)
    handler.do_POST()
    return sent["status"], sent["data"]


def test_http_post_expense_edit_and_undo_routes(temp_root):
    status, data = _invoke_post(
        temp_root,
        "/api/expense/edit",
        {
            "date": "2026-11-09",
            "entry_key": "act_20261109_expense_report_01#it3",
            "amount": 15.2,
        },
    )
    assert status == 200
    assert data["ok"] is True
    assert data["total_amount"] == 171.2
    assert data["expense_ledger"]["total_amount"] == 171.2

    status_u, data_u = _invoke_post(
        temp_root,
        "/api/expense/edit/undo",
        {
            "date": "2026-11-09",
            "entry_key": "act_20261109_expense_report_01#it3",
        },
    )
    assert status_u == 200
    assert data_u["ok"] is True
    assert data_u["total_amount"] == 161.2
    assert data_u["expense_ledger"]["total_amount"] == 161.2

    status_bad, data_bad = _invoke_post(
        temp_root,
        "/api/expense/edit",
        {
            "date": "2026-11-09",
            "entry_key": "act_20261109_expense_report_01#it3",
            "amount": -5,
        },
    )
    assert status_bad == 400
    assert data_bad["ok"] is False
