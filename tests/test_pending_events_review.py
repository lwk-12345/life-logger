"""pending_events 必须进入现有抽取待确认投影（Worker B）。

这里只验证读投影/审核台账，不涉及中文金额解析，也不把模型建议晋升成正式消费。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger.pending_review import (
    GROUP_NEEDS_REVIEW,
    STATES_FILENAME,
    collect_pending_sources,
    reason_meta,
    set_pending_review_state,
    set_pending_review_states_bulk,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _pending_event(
    *,
    event_id: str,
    source_id: str,
    amount: object = 38.8,
    reason: str = "amount_not_grounded",
    reported_at: str | None = "2026-10-17T20:28:06Z",
    quote: str = "然后又买了四斤半成品的黑椒鸡排，是三十八点八。",
    title: str = "购买四斤半成品黑椒鸡排，38.8元",
) -> dict:
    return {
        "event_id": event_id,
        "title": title,
        "time": {"absolute": None, "precision": "unknown"},
        "data": {
            "expense": {
                "amount": amount,
                "currency": "CNY",
                "item": "半成品黑椒鸡排",
            }
        },
        "evidence": {
            "source_id": source_id,
            "segment_id": "watch_000001_000001",
            "source_label": "self",
            "reported_at": reported_at,
            "quote": quote,
            "proposed_expense_amount": amount,
            "proposed_expense_currency": "CNY",
            "pending_reason": reason,
        },
    }


def _watch_root(root: Path, payload: dict) -> Path:
    session = root / "watch-sessions" / "watch-dev" / "SESSION0001"
    _write_json(
        session / "manifest.json",
        {"started_at": "2026-10-12T15:13:15.091000+00:00", "state": "complete"},
    )
    _write_json(
        session / "formal-asr" / "segments" / "000001" / "transcript.json",
        [
            {
                "segment_id": "rec_a_seg_0001",
                "absolute_started_at": "2026-10-12T19:42:00+08:00",
            }
        ],
    )
    artifact = session / "formal-asr" / "live-context" / "context-extraction.json"
    _write_json(artifact, payload)
    return artifact


def _items(payload: dict) -> list[dict]:
    return [
        item
        for group in payload["groups"]
        for item in group.get("items") or []
    ]


def test_pending_events_only_artifact_is_visible_with_model_amount_and_quote(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    event = _pending_event(event_id="evt_38", source_id="src_38")
    _watch_root(
        root,
        {
            "decision_provider": "jev",
            "pending_sources": [],
            "pending_events": [event],
        },
    )

    payload = collect_pending_sources(root, date="2026-10-18")
    items = _items(payload)

    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "pending_event"
    assert item["source_id"].startswith("pending_event:jev:")
    assert item["review_key"] == item["source_id"]
    assert item["original_source_id"] == "src_38"
    assert item["branch"] == "jev"
    assert item["event_id"] == "evt_38"
    assert item["group"] == GROUP_NEEDS_REVIEW
    assert item["proposed_amount"] == 38.8
    assert item["proposed_currency"] == "CNY"
    assert item["proposed_item"] == "半成品黑椒鸡排"
    assert item["quote"] == event["evidence"]["quote"]
    assert item["tentative"] is True
    # reported_at 是 UTC；界面/日期必须按 Asia/Shanghai 落到 10-18 04:28。
    assert item["date"] == "2026-10-18"
    assert item["time"] == "04:28"
    assert item["occurrence_time"] is None
    assert item["time_source"] == "evidence.reported_at"
    assert "金额" in item["reason_label"]


def test_amount_transaction_and_unknown_reasons_are_readable_chinese() -> None:
    for reason in ("amount_not_grounded", "transaction_not_completed", "unknown"):
        meta = reason_meta(reason)
        assert meta["group"] == GROUP_NEEDS_REVIEW
        assert meta["label"] != reason
        assert any("\u4e00" <= char <= "\u9fff" for char in meta["label"])


def test_branch_isolation_sibling_events_and_root_copy_dedupe(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    result = root / "jobs" / "job-20261018-120000-abcd1234" / "files" / "hash1" / "result"
    event_a = _pending_event(event_id="evt_same", source_id="src_same")
    event_b = _pending_event(
        event_id="evt_sibling",
        source_id="src_same",
        amount=12.5,
        reason="transaction_not_completed",
        quote="还有一份十二块五的配菜，不确定付了没有。",
    )
    jev_variant = {"pending_events": [event_a, event_b]}
    clef_variant = {"pending_events": [event_a]}
    comparison = {
        "comparison_mode": True,
        "providers": ["jev", "clef"],
        "variants": {"jev": jev_variant, "clef": clef_variant},
        # 根上的 pending_events 只是选中分支（jev）的拷贝，不应再加一份。
        "pending_events": [event_a],
    }
    _write_json(result / "context-extraction.json", comparison)
    # 分支文件与根内嵌 variants 指向同一事件；按分支+event_id 去重，不膨胀。
    _write_json(result / "decision-variants" / "jev" / "context-extraction.json", jev_variant)
    _write_json(result / "decision-variants" / "clef" / "context-extraction.json", clef_variant)

    payload = collect_pending_sources(root)
    items = _items(payload)

    assert payload["totals"]["all"] == 3
    assert len(items) == 3
    by_branch: dict[str, list[dict]] = {}
    for item in items:
        by_branch.setdefault(item["branch"], []).append(item)
    assert {item["event_id"] for item in by_branch["jev"]} == {
        "evt_same",
        "evt_sibling",
    }
    assert [item["event_id"] for item in by_branch["clef"]] == ["evt_same"]
    assert len({item["source_id"] for item in items}) == 3
    assert all(item["original_source_id"] == "src_same" for item in items)
    # 同源多个事件必须保留兄弟条目，不能按裸 source_id 去重。
    assert len(by_branch["jev"]) == 2


def test_status_and_bulk_triage_are_ledger_only_and_keep_legacy_source_keys(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    event = _pending_event(event_id="evt_38", source_id="src_38")
    artifact = _watch_root(
        root,
        {
            "pending_sources": [
                {
                    "source_id": "src_legacy",
                    "segment_id": "watch_000001_000001",
                    "quote": "记录一下。",
                    "reason": "model_no_event_but_directive",
                }
            ],
            "pending_events": [event],
        },
    )
    before = artifact.read_bytes()
    initial = _items(collect_pending_sources(root, include_resolved=True))
    legacy = [item for item in initial if item["source_id"] == "src_legacy"]
    pending_event = [item for item in initial if item["kind"] == "pending_event"]
    assert len(legacy) == 1 and legacy[0]["kind"] == "pending_source"
    assert len(pending_event) == 1

    event_key = pending_event[0]["source_id"]
    result = set_pending_review_state(root, source_id=event_key, status="ignored")
    assert result["counts"] == {"ignored": 1, "handled": 0}
    ledger = json.loads((root / STATES_FILENAME).read_text(encoding="utf-8"))
    assert ledger["records"][event_key]["status"] == "ignored"
    assert artifact.read_bytes() == before

    updated = {
        item["source_id"]: item
        for item in _items(collect_pending_sources(root, include_resolved=True))
    }
    assert updated[event_key]["state"] == "ignored"
    assert updated["src_legacy"]["state"] == "open"

    bulk = set_pending_review_states_bulk(
        root,
        date="2026-10-12",
        group=GROUP_NEEDS_REVIEW,
        status="handled",
    )
    # 事件（10-18）已经不 open，只剩 10-12 的 legacy 来源进入本次批量处理。
    assert bulk["count"] == 1
    ledger = json.loads((root / STATES_FILENAME).read_text(encoding="utf-8"))
    assert ledger["records"]["src_legacy"]["status"] == "handled"
    assert ledger["records"][event_key]["status"] == "ignored"
    assert artifact.read_bytes() == before


def test_selected_root_without_variants_does_not_duplicate_branch_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    result = root / "jobs" / "job-20261018-130000-abcd1234" / "files" / "hash2" / "result"
    event = _pending_event(event_id="evt_copy", source_id="src_copy")
    # 旧格式根产物没有 variants 字段，但同目录已有独立分支文件。
    _write_json(result / "context-extraction.json", {"pending_events": [event]})
    _write_json(result / "decision-variants" / "jev" / "context-extraction.json", {"pending_events": [event]})
    _write_json(result / "decision-variants" / "clef" / "context-extraction.json", {"pending_events": [event]})

    payload = collect_pending_sources(root)
    items = _items(payload)

    # 根拷贝不另算；Jev/Clef 各一份，键与处理台账保持分支隔离。
    assert payload["totals"]["all"] == 2
    assert {item["branch"] for item in items} == {"jev", "clef"}
    assert len({item["source_id"] for item in items}) == 2


def test_live_and_formal_twins_with_different_ids_dedupe_per_branch_and_keep_job(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    watch = (
        root
        / "watch-sessions"
        / "watch-dev"
        / "SESSION0001"
        / "formal-asr"
        / "live-context"
        / "context-extraction.json"
    )
    job = (
        root
        / "jobs"
        / "job-20261018-095927-9735adbd"
        / "files"
        / "hash1"
        / "result"
        / "context-extraction.json"
    )
    live_jev = _pending_event(event_id="evt_live_jev", source_id="src_live")
    live_clef = _pending_event(event_id="evt_live_clef", source_id="src_live")
    job_jev = _pending_event(event_id="evt_job_jev", source_id="src_job")
    job_clef = _pending_event(event_id="evt_job_clef", source_id="src_job")
    _write_json(
        watch,
        {
            "comparison_mode": True,
            "variants": {
                "jev": {"pending_events": [live_jev]},
                "clef": {"pending_events": [live_clef]},
            },
        },
    )
    _write_json(
        job,
        {
            "comparison_mode": True,
            "variants": {
                "jev": {"pending_events": [job_jev]},
                "clef": {"pending_events": [job_clef]},
            },
        },
    )
    # 旧 live 键已经人工处理过；它换成 formal job 键后不能重新冒出来。
    set_pending_review_state(
        root, source_id="pending_event:jev:evt_live_jev", status="handled"
    )

    items = _items(collect_pending_sources(root, include_resolved=True))
    by_branch = {item["branch"]: item for item in items}
    assert set(by_branch) == {"jev", "clef"}
    assert by_branch["jev"]["event_id"] == "evt_job_jev"
    assert by_branch["clef"]["event_id"] == "evt_job_clef"
    assert by_branch["jev"]["artifact"]["kind"] == "job"
    assert by_branch["clef"]["artifact"]["kind"] == "job"
    assert by_branch["jev"]["state"] == "handled"
    assert "pending_event:jev:evt_live_jev" in by_branch["jev"]["review_aliases"]
    assert "pending_event:jev:evt_job_jev" in by_branch["jev"]["review_aliases"]
    # Jev/Clef 独立；默认 open 视图只剩未被处理的 clef 候选。
    open_items = _items(collect_pending_sources(root))
    assert [item["branch"] for item in open_items] == ["clef"]


def test_same_quote_time_item_with_different_amount_keeps_sibling_proposals(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    first = _pending_event(event_id="evt_388", source_id="src_same", amount=38.8)
    second = _pending_event(event_id="evt_125", source_id="src_same", amount=12.5)
    _write_json(
        root
        / "jobs"
        / "job-20261018-100000-abcd1234"
        / "files"
        / "hash1"
        / "result"
        / "context-extraction.json",
        {"decision_provider": "jev", "pending_events": [first, second]},
    )

    items = _items(collect_pending_sources(root))

    assert len(items) == 2
    assert {item["event_id"] for item in items} == {"evt_388", "evt_125"}
    assert {item["proposed_amount"] for item in items} == {38.8, 12.5}


def test_accepted_formal_twin_suppresses_stale_pending_only_in_its_branch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    accepted = _pending_event(event_id="evt_formal_jev", source_id="src_formal")
    stale_jev = _pending_event(event_id="evt_stale_jev", source_id="src_stale")
    stale_clef = _pending_event(event_id="evt_stale_clef", source_id="src_stale")
    _write_json(
        root
        / "jobs"
        / "job-20261018-110000-abcd1234"
        / "files"
        / "hash1"
        / "result"
        / "context-extraction.json",
        {
            "comparison_mode": True,
            "variants": {
                "jev": {
                    "formal_events": [accepted],
                    "pending_events": [stale_jev],
                },
                "clef": {"formal_events": [], "pending_events": [stale_clef]},
            },
        },
    )

    items = _items(collect_pending_sources(root))

    # Jev 已有匹配的 accepted formal event，旧的 pending twin 不应再显示；
    # Clef 没有 formal 对端，仍是独立待确认候选。
    assert [item["branch"] for item in items] == ["clef"]
    assert items[0]["event_id"] == "evt_stale_clef"


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("amount_missing", "金额缺失"),
        ("amount_invalid_type", "金额类型无效"),
        ("amount_invalid_value", "金额内容无效"),
        ("amount_not_finite", "金额不是有限数值"),
        ("amount_out_of_range", "金额超出范围"),
        ("amount_evidence_missing", "缺少金额原文证据"),
        ("amount_evidence_not_verbatim", "金额证据与原文不一致"),
        ("transaction_evidence_missing", "缺少交易原文证据"),
        ("transaction_evidence_not_verbatim", "交易证据与原文不一致"),
        ("transaction_state_missing", "缺少交易状态"),
        ("transaction_claim_missing", "缺少交易声明"),
        ("transaction_claim_not_positive", "不是已完成的支付声明"),
    ],
)
def test_evidence_contract_reasons_have_short_chinese_pending_labels(
    tmp_path: Path, reason: str, label: str
) -> None:
    root = tmp_path / "local-web"
    _watch_root(
        root,
        {
            "decision_provider": "jev",
            "pending_events": [_pending_event(event_id=f"evt_{reason}", source_id="src_contract", reason=reason)],
        },
    )

    item = _items(collect_pending_sources(root))[0]

    assert item["group"] == GROUP_NEEDS_REVIEW
    assert item["reason"] == reason  # raw enum remains available for diagnostics
    assert item["pending_reason"] == reason
    assert item["reason_label"] == label
    assert "未收录原因码" not in item["reason_label"]


def test_generic_pending_event_unknown_reason_is_pending_while_source_stays_legacy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    _watch_root(
        root,
        {
            "pending_sources": [
                {
                    "source_id": "src_unknown_legacy",
                    "segment_id": "watch_000001_000001",
                    "quote": "记录一下。",
                    "reason": "brand_new_source_reason",
                }
            ],
            "pending_events": [
                _pending_event(
                    event_id="evt_unknown",
                    source_id="src_unknown_event",
                    reason="brand_new_event_reason",
                )
            ],
        },
    )

    items = {item["source_id"]: item for item in _items(collect_pending_sources(root))}
    event = items["pending_event:default:evt_unknown"]
    source = items["src_unknown_legacy"]

    assert event["group"] == GROUP_NEEDS_REVIEW
    assert event["reason"] == "brand_new_event_reason"
    assert event["reason_label"] == "原因待确认（未识别的模型原因）"
    # 旧来源条目保持原行为：未知 reason 仍在 other，原 enum 作为诊断可见。
    assert source["group"] == "other"
    assert source["reason"] == "brand_new_source_reason"
    assert "未收录原因码：brand_new_source_reason" == source["reason_label"]
