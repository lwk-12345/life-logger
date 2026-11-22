"""Q459: 验证金融自动补字段路径默认冻结禁用（FINANCIAL_ENRICHMENT_ENABLED = False）。

核心原则（A0 冻结指令）：
1. 生产默认零晋升：即便 formal 与 live 处于同一 typed 候选，也绝不自动增账、不补金额；
2. 同 order 不同 payment、order/payment 跨阶段均不增账，不隐藏交易、不变金额；
3. 保留原实验候选专测为显式隔离测试（通过 monkeypatch FINANCIAL_ENRICHMENT_ENABLED = True 运行），
   明确标注其为实验性质，绝不冒充生产门禁；
4. 保证原 event_view 读层稳定性。
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

import life_logger.event_view as ev_mod
from life_logger.event_view import build_event_view
from life_logger.expense_policy import build_expense_ledger


def _write_fixture(
    root: Path,
    *,
    job_id: str,
    formal_event: dict,
    live_event: dict,
    date: str = "2026-10-26",
    session_id: str = "SESSION_TEST",
) -> None:
    job_dir = root / "jobs" / job_id / "files" / "test_hash" / "result" / date
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "events.json").write_text(
        json.dumps([formal_event], ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "daily_summary.json").write_text(
        json.dumps(
            {"display_timeline": [formal_event], "schema_version": "0.1"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    sess_dir = root / "watch-sessions" / "test_device" / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "events.json").write_text(
        json.dumps([live_event], ensure_ascii=False), encoding="utf-8"
    )
    (sess_dir / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "started_at": "2026-10-25T22:04:17.390000+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _typed_events():
    formal = {
        "event_id": "formal_evt_duck",
        "categories": ["food"],
        "activity_type": "meal",
        "title": "购买鸭腿",
        "time": {"absolute": "2026-10-26T18:00:00+08:00", "precision": "exact"},
        "data": {"food": {"meal": "meal", "item": "鸭腿"}},
        "evidence": {
            "source_id": "src_01",
            "recording_id": "watch_rec_01",
            "segment_id": "watch_0001_0001",
            "quote": "买了一个鸭腿十块钱",
            "reported_at": "2026-10-26T18:00:00+08:00",
            "source_label": "self",
        },
        "event_state": "completed",
        "review_status": "unreviewed",
    }
    live = {
        "event_id": "live_evt_duck",
        "categories": ["expense", "food"],
        "activity_type": "purchase",
        "title": "购买鸭腿",
        "time": {"absolute": "2026-10-26T18:00:00+08:00", "precision": "exact"},
        "data": {
            "expense": {
                "amount": 10.0,
                "currency": "CNY",
                "item": "鸭腿",
                "amount_evidence": "十块钱",
                "transaction_evidence": "买了一个鸭腿",
            }
        },
        "evidence": {
            "source_id": "src_01",
            "recording_id": "watch_rec_01",
            "segment_id": "watch_0001_0001",
            "quote": "买了一个鸭腿十块钱",
            "reported_at": "2026-10-26T18:00:00+08:00",
            "source_label": "self",
            "expense_model_contract": {
                "status": "accepted",
                "amount": 10.0,
                "amount_evidence": "十块钱",
            },
        },
        "event_state": "completed",
        "review_status": "unreviewed",
    }
    return formal, live


# =========================================================================
# 一、生产默认行为：零晋升、零补账、不变金额
# =========================================================================

def test_production_default_flag_is_false():
    """生产硬性契约：FINANCIAL_ENRICHMENT_ENABLED 默认必须为 False。"""
    assert ev_mod.FINANCIAL_ENRICHMENT_ENABLED is False


def test_production_default_typed_candidate_does_not_enrich_amount(tmp_path: Path):
    """生产默认行为：即使双侧是完全同源的 typed 候选，默认也不自动补金额、不增账。"""
    formal, live = _typed_events()
    _write_fixture(
        tmp_path,
        job_id="job-20261026-000000-default",
        formal_event=formal,
        live_event=live,
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-26",
        date_to="2026-10-26",
        scope="provisional",
        view="display",
    )
    assert len(view["events"]) == 1
    event = view["events"][0]

    # 金额保持缺失，绝不跨源自动填补
    assert event["data"].get("expense", {}).get("amount") is None

    # 原 live 事件如实保留在 alias 中，原交易不隐藏
    assert len(event.get("aliases", [])) == 1
    alias_live = event["aliases"][0]
    assert alias_live.get("event", {}).get("data", {}).get("expense", {}).get("amount") == 10.0

    # 默认零晋升：enrichment_provenance 中绝不包含任何 financial=True 字段
    assert not any(
        p.get("financial") is True
        for p in event.get("enrichment_provenance", [])
    )


def test_production_default_order_and_payment_cross_stages_do_not_enrich(tmp_path: Path):
    """同 order 不同 payment、order/payment 跨阶段：不增账、不混淆、不隐藏。"""
    formal_order = {
        "event_id": "formal_order_01",
        "categories": ["food"],
        "activity_type": "meal",
        "title": "点餐鸭腿",
        "order_id": "ord_999",
        "time": {"absolute": "2026-10-26T18:00:00+08:00", "precision": "exact"},
        "data": {"food": {"item": "鸭腿"}},
        "evidence": {
            "source_id": "src_order_01",
            "recording_id": "rec_order_01",
            "segment_id": "seg_order_001",
            "quote": "要一份鸭腿",
            "reported_at": "2026-10-26T18:00:00+08:00",
            "source_label": "self",
        },
    }
    live_payment = {
        "event_id": "live_pay_01",
        "categories": ["expense"],
        "activity_type": "purchase",
        "title": "支付鸭腿",
        "order_id": "ord_999",
        "transaction_id": "tx_pay_888",
        "time": {"absolute": "2026-10-26T18:00:00+08:00", "precision": "exact"},
        "data": {"expense": {"amount": 10.0, "currency": "CNY", "item": "鸭腿"}},
        "evidence": {
            "source_id": "src_pay_01",
            "recording_id": "rec_order_01",
            "segment_id": "seg_order_001",
            "quote": "要一份鸭腿",
            "reported_at": "2026-10-26T18:00:00+08:00",
            "source_label": "self",
        },
    }

    _write_fixture(
        tmp_path,
        job_id="job-20261026-000000-cross",
        formal_event=formal_order,
        live_event=live_payment,
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-26",
        date_to="2026-10-26",
        scope="provisional",
        view="display",
    )
    event = view["events"][0]
    # formal 不增账，金额保持 None
    assert event["data"].get("expense", {}).get("amount") is None
    ledger = build_expense_ledger(view["events"])
    assert ledger["total_amount"] == 0.0


# =========================================================================
# 二、隔离实验分支：monkeypatch 显式开启（明确不冒充生产门禁）
# =========================================================================

def test_experimental_flag_enabled_enriches_when_typed_coordinates_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """【隔离实验】仅在 monkeypatch 开启 flag 时，已证实强身份 typed 候选才进入补值路径。"""
    monkeypatch.setattr(ev_mod, "FINANCIAL_ENRICHMENT_ENABLED", True)

    formal, live = _typed_events()
    _write_fixture(
        tmp_path,
        job_id="job-20261026-000000-exp",
        formal_event=formal,
        live_event=live,
    )

    view = build_event_view(
        tmp_path,
        date_from="2026-10-26",
        date_to="2026-10-26",
        scope="provisional",
        view="display",
    )
    event = view["events"][0]
    assert event["data"]["expense"]["amount"] == 10.0
    assert event["data"]["expense"]["currency"] == "CNY"

    # provenance 审计线索确认
    prov = event.get("enrichment_provenance", [])
    assert len(prov) > 0 and prov[0].get("financial") is True
    assert prov[0].get("amount") == 10.0


def test_experimental_flag_still_blocks_differing_physical_coordinates(
    monkeypatch: pytest.MonkeyPatch
):
    """【隔离实验】即使开启 flag，跨 recording/segment 的候选依然被物理坐标门禁严密阻断。"""
    monkeypatch.setattr(ev_mod, "FINANCIAL_ENRICHMENT_ENABLED", True)

    formal_a = {
        "event_id": "f_a",
        "title": "购买水",
        "activity_type": "purchase",
        "evidence": {
            "recording_id": "rec_A",
            "segment_id": "seg_001",
            "quote": "买水两元",
            "reported_at": "2026-10-26T18:00:00+08:00",
            "source_label": "self",
        },
        "time": {"absolute": "2026-10-26T18:00:00+08:00", "precision": "exact"},
    }
    live_b = {
        "event_id": "l_b",
        "title": "购买水",
        "activity_type": "purchase",
        "evidence": {
            "recording_id": "rec_B",  # 坐标不同
            "segment_id": "seg_002",
            "quote": "买水两元",
            "reported_at": "2026-10-26T18:00:00+08:00",
            "source_label": "self",
        },
        "time": {"absolute": "2026-10-26T18:00:00+08:00", "precision": "exact"},
        "data": {"expense": {"amount": 2.0, "currency": "CNY"}},
    }

    can_enrich, reason = ev_mod._can_enrich_financial_evidence(formal_a, live_b, 2.0)
    assert not can_enrich
    assert "physical_source_recording_distinct" in str(reason)
