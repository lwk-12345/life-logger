"""待人工审核（``pending_sources``）的只读投影与人工处理台账。

背景：``contextual_extraction`` 在「模型判 ``is_event=false``，但文本像用户的主动
记录/查询指令」（以及低置信、批次失败等）情况下，会把来源写进
``context-extraction.json`` 的 ``pending_sources``，而不是静默丢弃 —— 设计意图是
「别让用户的主动记录/查询指令静默丢失」。但这些条目此前**没有任何读取方**，
用户在前端看不到，等于丢失。

两条必须守住的口径（否则 UI 会把真条目淹掉）：

1. ``deepseek_batch_failed`` 是整批 LLM 调用失败时的**全批倾倒**（实测占全库 98%+），
   必须按 ``reason`` 分类并默认折叠，绝不能与真带语义的条目平铺在一起。
2. 真正需要人判断的只有 ``needs_review`` 组（指令句、口述未确认、低置信）；
   抽取过程杂音（``missing_source_response`` / ``invalid_*``）单独成组供抽查，
   不要求逐条处理。

本模块只做只读投影；唯一的写操作是人工处理台账（``ignored`` / ``handled``），
落在 ``<store root>/pending-review-states.json``，与 ``local_habits`` 相同的
「临时文件 + ``os.replace``」原子写法。

投影层把「人工决定」与「历史整批失败归档」分开：台账里的
``ignored`` 若属于 ``batch_failure`` 组，输出为 ``failed_archived``，
不计入 ``ignored_by_user``。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from life_logger.context_auto_retry import (
    failure_is_retryable,
    legacy_retryable_is_trusted,
    legacy_retryable_warning,
)

SCHEMA = "pending-sources-v1"
STATES_SCHEMA = "pending-review-states-v1"
STATES_FILENAME = "pending-review-states.json"

BEIJING = timezone(timedelta(hours=8))

GROUP_NEEDS_REVIEW = "needs_review"
GROUP_EXTRACTION_GAP = "extraction_gap"
GROUP_BATCH_FAILURE = "batch_failure"
GROUP_OTHER = "other"

# Resolution is deliberately distinct from the raw ledger status.  A batch
# failure archived by the cleanup script was written as status=ignored for
# backward compatibility, but it is NOT a human decision and must not inflate
# "ignored by user".
RESOLUTION_IGNORED_BY_USER = "ignored_by_user"
RESOLUTION_HANDLED_BY_USER = "handled_by_user"
RESOLUTION_FAILED_ARCHIVED = "failed_archived"

GROUP_ORDER: tuple[str, ...] = (
    GROUP_NEEDS_REVIEW,
    GROUP_EXTRACTION_GAP,
    GROUP_OTHER,
    GROUP_BATCH_FAILURE,
)

GROUP_LABELS = {
    GROUP_NEEDS_REVIEW: "需要你确认",
    GROUP_EXTRACTION_GAP: "抽取异常（可抽查）",
    GROUP_OTHER: "其他待确认",
    GROUP_BATCH_FAILURE: "整批 API 失败兜底",
}

GROUP_HINTS = {
    GROUP_NEEDS_REVIEW: (
        "模型判「无事件」但文本像你的主动指令/口述记录。分诊先看原文；"
        "当前不会从来源条目自动重新抽取或晋升为正式事件。可复制原文、标记已处理或忽略；"
        "如需形成正式事件，请重新导入相关录音或手动补录。"
    ),
    GROUP_EXTRACTION_GAP: (
        "抽取过程中缺响应或字段非法，原始转写仍在，通常无需逐条处理。"
    ),
    GROUP_OTHER: "未收录的原因码，宁可多露，不可静默丢失。",
    GROUP_BATCH_FAILURE: (
        "整批调用失败时的全批倾倒（API 挂掉，不是模型判不出来）。默认折叠，不占版面。"
    ),
}

# 默认折叠的组：数量巨大且不需要人工逐条处理。
COLLAPSED_GROUPS = {GROUP_BATCH_FAILURE}

# reason → 分组/文案。rank 越小越靠前、越需要人先看。
REASON_META: dict[str, dict[str, Any]] = {
    "model_no_event_but_directive": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 10,
        "label": "指令句：模型判无事件，但文本像你的主动指令",
        "hint": (
            "匹配到「检查/记录/汇报/整理…」这类指令词"
            "（contextual_extraction.py 的兜底分支）。"
        ),
    },
    "dictation_unconfirmed": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 20,
        "label": "口述记录未确认",
        "hint": "被判定为口述（dictation），但模型没确认成事件。",
    },
    "low_confidence": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 30,
        "label": "低置信事件",
        "hint": "模型给了事件但置信度不足，留给人工判断。",
    },
    "not_confirmed_event": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 40,
        "label": "未确认事件",
        "hint": "事件未被确认（非口述时也会进待确认）。",
    },
    "amount_uncertain": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 34,
        "label": "金额不确定：模型建议保留待确认",
        "hint": "金额可能是估计/口误/听不清；在人工确认前不当作已入账消费。",
    },
    "amount_not_grounded": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 35,
        "label": "金额证据待确认：模型建议了金额，但原文/来源证据不足",
        "hint": (
            "这是模型的暂定建议，不是已确认消费或已入账金额。"
            "标记已处理/忽略只写审核台账，不会授权记账，也不会重跑模型。"
        ),
    },
    "transaction_not_completed": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 36,
        "label": "交易完成状态待确认",
        "hint": (
            "模型认为可能只是报价、计划或未完成交易；"
            "在人工确认前不会当作已完成消费。"
        ),
    },
    "unknown": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 37,
        "label": "原因未知（保留待确认）",
        "hint": "模型建议缺少可识别的原因码；宁可保留给人看，不静默丢弃。",
    },
    "unknown_speaker": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 38,
        "label": "说话人未知（保留待确认）",
        "hint": "无法确认这句话是谁说的；不据此自动记到任何人的账上。",
    },
    "non_self_review": {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 39,
        "label": "非本人内容待确认",
        "hint": "模型建议可能来自他人/环境；人工确认前不当作本人事件或消费。",
    },
    "invalid_record_response": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 50,
        "label": "模型返回结构非法",
        "hint": "这一批的返回不符合记录格式，模型没给可用结构。",
    },
    "invalid_event_response": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 51,
        "label": "事件条目非法",
        "hint": "事件条目不是对象，被丢弃。",
    },
    "invalid_category": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 52,
        "label": "分类非法",
        "hint": "模型给的分类不在允许列表里。",
    },
    "invalid_event": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 53,
        "label": "事件非法",
        "hint": "事件未通过校验。",
    },
    "empty_event_list": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 54,
        "label": "事件列表为空",
        "hint": "模型声明是事件却没给条目。",
    },
    "missing_source_response": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 55,
        "label": "模型整批返回里缺这一条",
        "hint": "模型漏答了这条来源，不是判定为无事件。",
    },
    "non_confirmed_state": {
        "group": GROUP_EXTRACTION_GAP,
        "rank": 56,
        "label": "状态未确认",
        "hint": "事件状态未确认（如 proposed）。",
    },
    "deepseek_batch_failed": {
        "group": GROUP_BATCH_FAILURE,
        "rank": 900,
        "label": "整批 API 失败（全批倾倒）",
        "hint": "这一批 LLM 调用整体失败，整批来源被原样倾倒到这里 —— 与模型判断无关。",
    },
}

# Worker A 的金额/交易证据契约原因码：普通卡片必须给短中文标签，不能把
# enum 原样显示成「未收录原因码」。原始 reason 仍保留在 record["reason"] /
# record["pending_reason"] 里供诊断。
EVIDENCE_CONTRACT_REASON_LABELS: dict[str, str] = {
    "amount_missing": "金额缺失",
    "amount_invalid_type": "金额类型无效",
    "amount_invalid_value": "金额内容无效",
    "amount_not_finite": "金额不是有限数值",
    "amount_out_of_range": "金额超出范围",
    "amount_evidence_missing": "缺少金额原文证据",
    "amount_evidence_not_verbatim": "金额证据与原文不一致",
    "transaction_evidence_missing": "缺少交易原文证据",
    "transaction_evidence_not_verbatim": "交易证据与原文不一致",
    "transaction_state_missing": "缺少交易状态",
    "transaction_claim_missing": "缺少交易声明",
    "transaction_claim_not_positive": "不是已完成的支付声明",
}
for _contract_reason, _contract_label in EVIDENCE_CONTRACT_REASON_LABELS.items():
    REASON_META[_contract_reason] = {
        "group": GROUP_NEEDS_REVIEW,
        "rank": 32,
        "label": _contract_label,
        "hint": (
            "模型建议未满足金额/交易证据契约；这是待确认建议，"
            "不会自动入账，也不会因为标记处理而重跑模型。"
        ),
    }

DEFAULT_REASON_META: dict[str, Any] = {
    "group": GROUP_OTHER,
    "rank": 500,
    "label": "未收录的原因码",
    "hint": "未知 reason，默认按「需要确认」展示，避免静默丢失。",
}

DEFAULT_ITEM_LIMIT = 40
MAX_ITEM_LIMIT = 200

# 抽取产物的相对位置模式。用定向 glob 而不是 ``**``：``**`` 需要遍历 jobs/
# watch-sessions 下 5.8G 的目录树（实测 2.5s），定向 glob 只要 0.025s。
ARTIFACT_PATTERNS: tuple[str, ...] = (
    "watch-sessions/*/*/formal-asr/live-context/context-extraction.json",
    "watch-sessions/*/*/live-context-*/context-extraction.json",
    "watch-sessions/*/*/live/*/result/context-extraction.json",
    "jobs/*/files/*/result/context-extraction.json",
    "replays/*/context-extraction.json",
    "*/context-extraction.json",
    # 双分支产物只取每个分支的一层目录；不用 ``**``，避免遍历整棵录音树。
    "watch-sessions/*/*/formal-asr/live-context/decision-variants/*/context-extraction.json",
    "watch-sessions/*/*/live-context-*/decision-variants/*/context-extraction.json",
    "watch-sessions/*/*/live/*/result/decision-variants/*/context-extraction.json",
    "jobs/*/files/*/result/decision-variants/*/context-extraction.json",
    "replays/*/decision-variants/*/context-extraction.json",
)


class PendingReviewError(ValueError):
    """参数非法（如 status 取值不在允许集合内）。"""


def reason_meta(reason: str | None) -> dict[str, Any]:
    """返回 reason 的分类信息（未知 reason 归入 other）。"""

    key = str(reason or "").strip()
    meta = REASON_META.get(key)
    if meta is None:
        merged = dict(DEFAULT_REASON_META)
        merged["label"] = (
            f"未收录原因码：{key}" if key else DEFAULT_REASON_META["label"]
        )
    else:
        merged = dict(meta)
    merged["reason"] = key or None
    merged["group"] = merged.get("group") or GROUP_OTHER
    return merged


def _pending_event_reason_meta(reason: str) -> dict[str, Any]:
    """pending_events 的未知 reason 仍进入待确认，但不泄露未收录 enum 文案。"""

    meta = reason_meta(reason)
    key = str(reason or "").strip()
    if meta.get("group") == GROUP_OTHER and key not in REASON_META:
        meta = dict(meta)
        meta["group"] = GROUP_NEEDS_REVIEW
        meta["rank"] = 450
        meta["label"] = "原因待确认（未识别的模型原因）"
        meta["hint"] = "这是未识别的待确认原因；保留给人判断，不静默丢弃。"
    return meta


def group_meta(group: str) -> dict[str, Any]:
    return {
        "key": group,
        "label": GROUP_LABELS.get(group, group),
        "hint": GROUP_HINTS.get(group, ""),
        "collapsed": group in COLLAPSED_GROUPS,
    }


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed


def _beijing_date(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.astimezone(BEIJING).date().isoformat() if parsed else None


def _beijing_clock(value: Any) -> str | None:
    parsed = _parse_iso(value)
    return parsed.astimezone(BEIJING).strftime("%H:%M") if parsed else None


def _first_value(*values: Any) -> Any:
    """返回第一个非空值（保留 0 等有效数值）。"""

    for value in values:
        if value is None or value == "":
            continue
        return value
    return None


def _finite_number(value: Any) -> float | None:
    """只接受有限数值；布尔、NaN、Infinity 和非法字符串都不算金额。"""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _branch_from_path(path: Path) -> str | None:
    parts = path.parts
    if "decision-variants" not in parts:
        return None
    index = parts.index("decision-variants")
    if index + 1 < len(parts):
        branch = str(parts[index + 1] or "").strip()
        if branch:
            return branch
    return None


def _artifact_branch(path: Path, payload: Mapping[str, Any]) -> str:
    """稳定分支名：优先产物路径，其次产物里的 provider 字段。"""

    candidate = _branch_from_path(path) or _first_value(
        payload.get("decision_provider"),
        payload.get("provider"),
        payload.get("branch"),
    )
    return str(candidate or "default").strip() or "default"


def _source_outcomes(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    outcomes = payload.get("source_outcomes")
    if not isinstance(outcomes, list):
        return {}
    return {
        str(row.get("source_id")): row
        for row in outcomes
        if isinstance(row, Mapping) and row.get("source_id")
    }


def _pending_event_key(
    *, branch: str, event_id: str, original_source_id: str, segment_id: str,
    quote: str, title: str, occurrence_time: Any,
) -> str:
    """事件级人工处理键：同分支同事件只保留一份，不同分支绝不合并。"""

    identity = event_id.strip()
    if not identity:
        basis = json.dumps(
            [
                original_source_id,
                segment_id,
                quote,
                title,
                str(occurrence_time or ""),
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        identity = "sha256:" + hashlib.sha256(basis).hexdigest()[:16]
    return f"pending_event:{branch}:{identity}"


def _pending_candidate_identity(
    event: Mapping[str, Any], proposed_amount: Any = None
) -> str | None:
    """跨 live/formal 的事件候选身份（接 event_view 的既有去重规则）。

    身份 = 原文 + reported/occurrence 时间 + 既有 entity/category discriminator
    + 模型给出的数值金额。金额只做有限数值比较，不解析中文金额语法；不同金额
    的 sibling 提议因此仍是两条，而同一提议的 live/formal 不同 event_id 会合并。
    """

    from life_logger.event_view import _canonical_entity_key, _event_amount

    base = _canonical_entity_key(event)
    if not base:
        return None
    amount = _finite_number(proposed_amount)
    if amount is None:
        amount = _event_amount(event)
    amount_key = "none" if amount is None else format(amount, ".6f")
    return f"{base}|amount:{amount_key}"


def _prefer_pending_event(
    candidate: Mapping[str, Any], existing: Mapping[str, Any]
) -> bool:
    """同一候选出现 live/formal 两份时优先 formal job 表示，分支已经分开。"""

    order = {"job": 0, "watch-session": 1, "replay": 2, "other": 3}
    candidate_kind = order.get(str((candidate.get("artifact") or {}).get("kind")), 3)
    existing_kind = order.get(str((existing.get("artifact") or {}).get("kind")), 3)
    if candidate_kind != existing_kind:
        return candidate_kind < existing_kind
    if bool(candidate.get("date")) != bool(existing.get("date")):
        return bool(candidate.get("date"))
    candidate_at = str(candidate.get("recorded_at") or "")
    existing_at = str(existing.get("recorded_at") or "")
    if candidate_at != existing_at:
        return candidate_at > existing_at
    return _prefer(candidate, existing)


def _review_resolution(group: str, ledger_state: str) -> str | None:
    state = str(ledger_state or "open")
    if state == "open":
        return None
    if group == GROUP_BATCH_FAILURE:
        return RESOLUTION_FAILED_ARCHIVED
    if state == "ignored":
        return RESOLUTION_IGNORED_BY_USER
    if state == "handled":
        return RESOLUTION_HANDLED_BY_USER
    return None


def _set_review_resolution(
    record: dict[str, Any], ledger_state: Any, updated_at: Any
) -> None:
    state = str(ledger_state or "open")
    resolution = _review_resolution(str(record.get("group") or ""), state)
    record["ledger_state"] = state
    record["resolution"] = resolution
    record["state"] = (
        RESOLUTION_FAILED_ARCHIVED
        if resolution == RESOLUTION_FAILED_ARCHIVED
        else state
    )
    record["state_updated_at"] = updated_at


def _merge_event_review_state(
    record: dict[str, Any],
    aliases: Iterable[str],
    records: Mapping[str, Any],
) -> None:
    """把旧 event_id 的 handled/ignored 状态迁到当前胜出的候选键上。"""

    chosen: tuple[str, str, Any] | None = None
    for alias in aliases:
        state = records.get(alias)
        if not isinstance(state, Mapping):
            continue
        status = str(state.get("status") or "")
        if status not in {"ignored", "handled"}:
            continue
        updated_at = state.get("updated_at")
        stamp = str(updated_at or "")
        if chosen is None or stamp >= chosen[0]:
            chosen = (stamp, status, updated_at)
    if chosen is None:
        _set_review_resolution(record, "open", None)
    else:
        _set_review_resolution(record, chosen[1], chosen[2])


def _pending_event_record(
    raw: Mapping[str, Any],
    *,
    branch: str,
    path: Path,
    root_path: Path,
    info: Mapping[str, Any],
    resolve_time: Any,
    outcomes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """把一个 genuine ``pending_events`` 事件投影成待确认卡片。

    金额只展示模型给出的数值建议；这里不做中文金额解析，也不把建议当成
    已确认消费。事件身份用 ``pending_event:<branch>:<event_id>``，原始
    ``source_id`` 只作为元数据保留，避免 Jev/Clef 同源同 ID 被合并。
    """

    evidence = raw.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    data = raw.get("data")
    data = data if isinstance(data, Mapping) else {}
    expense = data.get("expense")
    expense = expense if isinstance(expense, Mapping) else {}
    time_payload = raw.get("time")
    time_payload = time_payload if isinstance(time_payload, Mapping) else {}

    original_source_id = str(
        _first_value(evidence.get("source_id"), raw.get("source_id")) or ""
    ).strip()
    segment_id = _first_value(evidence.get("segment_id"), raw.get("segment_id"))
    event_id = str(_first_value(raw.get("event_id"), evidence.get("event_id")) or "").strip()
    quote = str(
        _first_value(evidence.get("quote"), raw.get("quote")) or ""
    ).strip()
    title = str(raw.get("title") or "").strip()

    reason = str(
        _first_value(
            raw.get("pending_reason"),
            evidence.get("pending_reason"),
            raw.get("pending_reason_code"),
            raw.get("reason"),
        )
        or "unknown"
    ).strip()
    meta = _pending_event_reason_meta(reason)

    proposed_amount = _finite_number(
        _first_value(
            evidence.get("proposed_expense_amount"),
            raw.get("proposed_expense_amount"),
            evidence.get("proposed_amount"),
            raw.get("proposed_amount"),
            expense.get("amount"),
        )
    )
    proposed_currency = str(
        _first_value(
            evidence.get("proposed_expense_currency"),
            raw.get("proposed_expense_currency"),
            expense.get("currency"),
        )
        or ""
    ).strip()
    proposed_item = str(
        _first_value(
            evidence.get("proposed_expense_item"),
            raw.get("proposed_expense_item"),
            expense.get("item"),
        )
        or ""
    ).strip()

    occurrence_time = _first_value(time_payload.get("absolute"), raw.get("occurrence_time"))
    reported_at = _first_value(
        evidence.get("reported_at"),
        raw.get("reported_at"),
        evidence.get("recorded_at"),
        raw.get("recorded_at"),
    )
    resolved = resolve_time(segment_id) if segment_id else None
    resolved_time = resolved.get("recorded_at") if resolved else None
    timestamp = _first_value(reported_at, resolved_time, occurrence_time)
    if reported_at:
        time_source = "evidence.reported_at" if evidence.get("reported_at") else "event.reported_at"
    elif resolved_time:
        time_source = resolved.get("time_source") if resolved else None
    elif occurrence_time:
        time_source = "event.time.absolute"
    else:
        time_source = None

    review_key = _pending_event_key(
        branch=branch,
        event_id=event_id,
        original_source_id=original_source_id,
        segment_id=str(segment_id or ""),
        quote=quote,
        title=title,
        occurrence_time=occurrence_time,
    )
    candidate_identity = _pending_candidate_identity(raw, proposed_amount)
    outcome = outcomes.get(original_source_id) or {}
    record = {
        "source_id": review_key,
        "review_key": review_key,
        "candidate_identity": candidate_identity,
        "kind": "pending_event",
        "event_id": event_id or None,
        "branch": branch,
        "original_source_id": original_source_id or None,
        "segment_id": segment_id,
        "quote": quote,
        "source_label": _first_value(evidence.get("source_label"), raw.get("source_label")),
        "reason": meta["reason"],
        "reason_label": meta["label"],
        "reason_hint": meta["hint"],
        "group": meta["group"],
        "rank": meta["rank"],
        "pending_reason": reason or None,
        "proposal_title": title or None,
        "proposed_amount": proposed_amount,
        "proposed_currency": proposed_currency or None,
        "proposed_item": proposed_item or None,
        "tentative": True,
        "occurrence_time": occurrence_time,
        "occurrence_precision": time_payload.get("precision"),
        "retryable": False,
        "artifact": {
            "kind": info["kind"],
            "label": info["label"],
            "date": info.get("date"),
            "path": _relative(path, root_path),
            "job_id": info.get("job_id"),
            "session_id": info.get("session_id"),
            "device_id": info.get("device_id"),
            "branch": branch,
        },
        "outcome": (
            {
                "semantic_status": outcome.get("semantic_status"),
                "reason": outcome.get("reason"),
                "event_count": (
                    len(outcome.get("event_refs") or [])
                    if isinstance(outcome.get("event_refs"), list)
                    else 0
                ),
            }
            if outcome
            else None
        ),
        "recorded_at": timestamp,
        "time_source": time_source,
        # 找不到 reported_at/segment/occurrence 时保留未知，不用产物日期顶替。
        "date": _beijing_date(timestamp),
        "time": _beijing_clock(timestamp),
    }
    return record


#: 【2026-10-24 性能】读路径三层缓存（不改变任何返回内容，只避免重复读盘/重复计算）
#: 背景：`/api/pending-sources` 单请求实测 5.2~48s，其中 389 次 `json.loads` 占 1.86s、
#: 7492 次 `Path.relative_to` 占 0.53s —— 同一份 `pending-review-states.json`（1.3MB）
#: 被每个产物各读一遍。以下缓存全部以「文件 mtime_ns+size」为键，写后自动失效。
_JSON_CACHE: dict[tuple[str, int, int], Any] = {}
_JSON_CACHE_LOCK = threading.Lock()
_RELATIVE_CACHE: dict[tuple[str, str], str] = {}
_RELATIVE_CACHE_LOCK = threading.Lock()

#: 整份投影结果的短时缓存 + single-flight：并发刷新时只算一次。
_RESULT_CACHE: dict[tuple, tuple[float, dict]] = {}
_RESULT_INFLIGHT: dict[tuple, threading.Event] = {}
_RESULT_LOCK = threading.Lock()
_RESULT_GENERATION = 0
RESULT_CACHE_TTL_SECONDS = 10.0


def invalidate_pending_cache() -> int:
    """写操作后立刻让待审核投影失效（写后下一次读必须看到新状态）。

    同时把 generation +1：失效前启动、失效后才算完的并发计算不会把旧结果写回缓存。
    """
    global _RESULT_GENERATION
    with _RESULT_LOCK:
        cleared = len(_RESULT_CACHE)
        _RESULT_CACHE.clear()
        _RESULT_GENERATION += 1
    return cleared


def _read_json(path: Path) -> Any:
    """读 JSON，按 (mtime_ns, size) 缓存；文件一变键就变，绝不返回旧内容。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _JSON_CACHE_LOCK:
        if key in _JSON_CACHE:
            return _JSON_CACHE[key]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    with _JSON_CACHE_LOCK:
        if len(_JSON_CACHE) > 4096:
            _JSON_CACHE.clear()
        _JSON_CACHE[key] = value
    return value


def _artifact_patterns(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in ARTIFACT_PATTERNS:
        try:
            for path in root.glob(pattern):
                if path.is_file():
                    found.add(path)
        except OSError:
            continue
    return sorted(found)


def _relative(path: Path, root: Path) -> str:
    key = (str(path), str(root))
    with _RELATIVE_CACHE_LOCK:
        cached = _RELATIVE_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        value = path.relative_to(root).as_posix()
    except ValueError:
        value = path.as_posix()
    with _RELATIVE_CACHE_LOCK:
        if len(_RELATIVE_CACHE) > 20000:
            _RELATIVE_CACHE.clear()
        _RELATIVE_CACHE[key] = value
    return value


def _artifact_kind_and_labels(path: Path) -> dict[str, Any]:
    """从产物路径推断来源种类、日期与展示标签。"""

    parts = path.parts
    info: dict[str, Any] = {
        "kind": "other",
        "date": None,
        "session_id": None,
        "device_id": None,
        "label": path.parent.parent.name,
    }
    if "jobs" in parts:
        info["kind"] = "job"
        index = parts.index("jobs")
        job_dir = parts[index + 1] if len(parts) > index + 1 else ""
        info["job_id"] = job_dir
        # job-YYYYMMDD-HHMMSS-xxxx → 处理日期
        stamp = job_dir.split("-")
        if len(stamp) >= 2 and len(stamp[1]) == 8 and stamp[1].isdigit():
            info["date"] = f"{stamp[1][:4]}-{stamp[1][4:6]}-{stamp[1][6:]}"
        info["label"] = f"任务 {job_dir}"
    elif "watch-sessions" in parts:
        info["kind"] = "watch-session"
        index = parts.index("watch-sessions")
        device = parts[index + 1] if len(parts) > index + 1 else None
        session = parts[index + 2] if len(parts) > index + 2 else None
        info["device_id"] = device
        info["session_id"] = session
        info["label"] = f"Apple Watch 会话 {session[:8] if session else ''}"
        if device and session:
            manifest = _read_json(Path(*parts[: index + 3]) / "manifest.json")
            if isinstance(manifest, Mapping):
                info["date"] = _beijing_date(
                    manifest.get("started_at") or manifest.get("ended_at")
                )
    elif "replays" in parts or "contextual-replay" in path.as_posix():
        info["kind"] = "replay"
        for part in parts:
            if part.startswith("contextual-replay-"):
                info["date"] = part[len("contextual-replay-") :][:10] or None
        info["label"] = f"重放产线 {path.parent.name}"
    return info


def _segment_time_lookup(artifact_path: Path, info: Mapping[str, Any]):
    """构造「来源条目 → 真实录音时间」的解析器。

    索引按**产物**懒建一次（而不是按条目），否则 3600+ 条整批失败条目会各自
    去 glob + 读一遍 transcript.json（实测把接口拖到 12s）。
    """

    rows_by_seq: dict[str, list[Any] | None] = {}
    job_index: dict[str, str] | None = None

    def watch_rows(seq: str) -> list[Any] | None:
        if seq in rows_by_seq:
            return rows_by_seq[seq]
        parts = artifact_path.parts
        try:
            index = parts.index("formal-asr")
        except ValueError:
            rows_by_seq[seq] = None
            return None
        rows = _read_json(
            Path(*parts[: index + 1]) / "segments" / seq / "transcript.json"
        )
        rows_by_seq[seq] = rows if isinstance(rows, list) else None
        return rows_by_seq[seq]

    def build_job_index() -> dict[str, str]:
        # job 产物：<hash>/result/<date>/transcript.json 里 segment_id 与来源一一对应
        index: dict[str, str] = {}
        artifact_dir = artifact_path.parent.parent
        for pattern in ("result/*/transcript.json", "asr/*/transcript.json"):
            for candidate in sorted(artifact_dir.glob(pattern)):
                rows = _read_json(candidate)
                if isinstance(rows, Mapping):
                    rows = rows.get("transcripts") or rows.get("rows") or []
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    key = str(row.get("segment_id") or "")
                    value = row.get("absolute_started_at") or row.get("started_at")
                    if key and value and key not in index:
                        index[key] = str(value)
        return index

    def resolve(segment_id: Any) -> dict[str, str] | None:
        nonlocal job_index
        text = str(segment_id or "")
        if not text:
            return None
        if text.startswith("watch_"):
            pieces = text.split("_")
            if len(pieces) != 3 or not pieces[1].isdigit() or not pieces[2].isdigit():
                return None
            rows = watch_rows(pieces[1])
            if not rows:
                return None
            position = max(0, int(pieces[2]) - 1)
            if position >= len(rows) or not isinstance(rows[position], Mapping):
                return None
            value = rows[position].get("absolute_started_at") or rows[position].get(
                "started_at"
            )
            if not value:
                return None
            return {
                "recorded_at": str(value),
                "time_source": (
                    f"formal-asr/segments/{pieces[1]}/transcript.json"
                    f"#row{position + 1}"
                ),
            }
        if info.get("kind") == "job":
            if job_index is None:
                job_index = build_job_index()
            value = job_index.get(text)
            if not value:
                return None
            return {
                "recorded_at": value,
                "time_source": f"result/<date>/transcript.json#{text}",
            }
        return None

    return resolve


def load_review_states(root: Path | str) -> dict[str, Any]:
    """读取人工处理台账（不存在时返回空结构）。"""

    root_path = Path(root)
    payload = _read_json(root_path / STATES_FILENAME)
    if not isinstance(payload, Mapping):
        return {"schema": STATES_SCHEMA, "updated_at": None, "records": {}}
    records = payload.get("records")
    if not isinstance(records, Mapping):
        records = {}
    return {
        "schema": str(payload.get("schema") or STATES_SCHEMA),
        "updated_at": payload.get("updated_at"),
        "records": {
            str(key): dict(value)
            for key, value in records.items()
            if isinstance(value, Mapping)
        },
    }


def _write_review_states(root: Path, states: Mapping[str, Any]) -> None:
    target = root / STATES_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(states, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    # 【2026-10-24】写后立刻失效：审核操作完成后的下一次 GET 必须看到新状态，
    # 不能等 10s TTL（用户点了「处理」还看到旧列表 = 假成功）。
    invalidate_pending_cache()


def set_pending_review_state(
    root: Path | str,
    *,
    source_id: str,
    status: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """写入/清除一条人工处理记录。``status="open"`` 表示撤销处理。"""

    source = str(source_id or "").strip()
    if not source:
        raise PendingReviewError("缺少 source_id")
    normalized = str(status or "").strip().lower()
    if normalized not in {"ignored", "handled", "open"}:
        raise PendingReviewError("status 必须是 ignored / handled / open 之一")

    root_path = Path(root)
    states = load_review_states(root_path)
    records = dict(states["records"])
    stamp = (now or datetime.now(tz=BEIJING)).astimezone(BEIJING).isoformat()
    if normalized == "open":
        records.pop(source, None)
    else:
        records[source] = {
            "status": normalized,
            "note": str(note or "")[:500],
            "updated_at": stamp,
        }
    updated = {"schema": STATES_SCHEMA, "updated_at": stamp, "records": records}
    _write_review_states(root_path, updated)
    return {
        "ok": True,
        "source_id": source,
        "status": normalized,
        "updated_at": stamp,
        "counts": _state_counts(records),
        "total": len(records),
    }


def set_pending_review_states_bulk(
    root: Path | str,
    *,
    date: str,
    group: str = GROUP_NEEDS_REVIEW,
    status: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """一次写入某一日期/分组的全部**当前 open** 条目的处理状态。

    安全边界：
    - ``date`` 必填且必须是 ``YYYY-MM-DD``；
    - 当前只允许 ``needs_review``（整批失败等大范围组不允许批量误杀）；
    - 只接受 ``ignored`` / ``handled``，不接受把记录改回 ``open``；
    - 只处理台账里尚未处理的条目，已 ignored/handled 的不重复计数；
    - 只写 ``pending-review-states.json``，不修改任何抽取产物。

    全量扫描用内部 ``_collect_pending_items``，不受展示 ``limit`` 截断，
    因此 ``count`` 是真实命中数，不会静默丢掉第 201 条以后的记录。
    """

    target_date = str(date or "").strip()
    if not target_date:
        raise PendingReviewError("缺少 date")
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError as error:
        raise PendingReviewError("date 需要 YYYY-MM-DD") from error

    normalized_group = str(group or GROUP_NEEDS_REVIEW).strip()
    if normalized_group != GROUP_NEEDS_REVIEW:
        raise PendingReviewError("批量处理仅支持 group=needs_review")

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"ignored", "handled"}:
        raise PendingReviewError("批量 status 必须是 ignored / handled 之一")

    root_path = Path(root)
    states = load_review_states(root_path)
    records = dict(states["records"])
    items, _, _ = _collect_pending_items(root_path, records)
    targets = [
        item
        for item in items.values()
        if item["group"] == normalized_group
        and item["date"] == target_date
        and item["state"] == "open"
    ]

    if targets:
        stamp = (now or datetime.now(tz=BEIJING)).astimezone(BEIJING).isoformat()
        note_text = str(note or "")[:500]
        for item in targets:
            records[item["source_id"]] = {
                "status": normalized_status,
                "note": note_text,
                "updated_at": stamp,
            }
        updated = {"schema": STATES_SCHEMA, "updated_at": stamp, "records": records}
        _write_review_states(root_path, updated)

    return {
        "ok": True,
        "count": len(targets),
        "date": target_date,
        "group": normalized_group,
        "status": normalized_status,
    }


def _state_counts(records: Mapping[str, Any]) -> dict[str, int]:
    counts = {"ignored": 0, "handled": 0}
    for value in records.values():
        status = str((value or {}).get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def _normalize_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_ITEM_LIMIT
    if value <= 0:
        return DEFAULT_ITEM_LIMIT
    return min(value, MAX_ITEM_LIMIT)


def _published_review_rows(root_path: Path):
    """Read only published live holds; isolated shadow archives are never scanned."""
    import hashlib

    for path in sorted((root_path / "activity-timeline" / "jev").glob("*.json")):
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            continue
        held_items = payload.get("unassociated_items")
        if not isinstance(held_items, list):
            continue
        for item in held_items:
            if not isinstance(item, Mapping) or item.get("review_status") != "non_confirmed":
                continue
            reason = item.get("pending_reason")
            if reason not in {"field_too_long", "low_confidence", "unknown_lifecycle", "time_format"} or not item.get("v3_activity_id"):
                continue
            evidence = item.get("evidence")
            if not isinstance(evidence, list) or not evidence or any(
                not isinstance(ev, Mapping) or not ev.get("quote") for ev in evidence
            ):
                continue
            identity = json.dumps([path.stem, item], ensure_ascii=False, sort_keys=True)
            key = "v3-review:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
            meta = reason_meta(reason)
            stamp = item.get("spoken_at")
            yield path, {
                "source_id": key, "kind": "pending_source", "reason": reason,
                "reason_label": {"field_too_long": "字段过长，需确认", "low_confidence": "把握不足，需确认",
                                 "unknown_lifecycle": "现场归属未知，需确认", "time_format": "发生时间格式需确认"}[reason],
                "reason_hint": "完整候选与证据已保留，未进入正式活动。",
                "group": GROUP_NEEDS_REVIEW, "rank": meta["rank"], "retryable": False,
                "quote": "\n".join(str(ev["quote"]) for ev in evidence),
                "source_label": evidence[0].get("source_label"),
                "segment_id": evidence[0].get("segment_id"),
                "recorded_at": stamp, "time_source": "spoken_at" if stamp else None,
                "date": _beijing_date(stamp) or path.stem, "time": _beijing_clock(stamp),
                "extracted_content": item.get("action"), "proposed_item": dict(item),
                "artifact": {"kind": "activity-v3", "label": "活动候选", "date": path.stem,
                             "path": _relative(path, root_path)},
            }


def _collect_pending_items(
    root_path: Path, records: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], int, list[str]]:
    """扫描全部抽取产物，返回去重后的全部待确认条目（不受展示 limit 截断）。

    同一入口同时投影两类真实待确认内容：

    * ``pending_sources``：沿用原始 ``source_id`` 作为处理键（保持旧行为）；
    * ``pending_events``：模型已给出事件建议、但语义/金额证据未通过确认的
      genuine 事件。处理键必须带分支（``pending_event:<branch>:<event_id>``），
      否则 Jev/Clef 同源同 event_id 会被错误合并。

    对照产物 ``context-extraction.json`` 同时包含根上的「主分支拷贝」和
    ``variants``。这里只在存在 variants 时投影各分支，跳过根拷贝，避免
    同一事件被根拷贝 + 分支各算一次；独立分支的同一 event_id 仍各留一行。

    live/formal 是同一句话的不同产物、event_id 往往不同。对 pending_events
    再按证据候选身份（quote + time + entity/category + 数值金额）在每个分支内
    合并，优先保留 formal job；已有人工处理状态通过别名键继承，避免 candidate
    换个 event_id 后重新冒出来。若同分支已有匹配的 accepted formal event，
    则直接隐藏仍 pending 的旧 twin。
    """

    items: dict[str, dict[str, Any]] = {}
    artifact_count = 0
    artifact_errors: list[str] = []
    artifact_paths = _artifact_patterns(root_path)
    branch_copy_dirs: set[Path] = set()
    for candidate in artifact_paths:
        if "decision-variants" in candidate.parts:
            index = candidate.parts.index("decision-variants")
            branch_copy_dirs.add(Path(*candidate.parts[:index]))

    candidate_index: dict[tuple[str, str], str] = {}
    candidate_aliases: dict[str, set[str]] = {}
    accepted_candidates: set[tuple[str, str]] = set()

    def store(record: Mapping[str, Any], key: str) -> None:
        state = records.get(key) or {}
        _set_review_resolution(
            record, state.get("status") or "open", state.get("updated_at")
        )
        existing = items.get(key)
        if existing is None or _prefer(record, existing):
            items[key] = dict(record)

    def store_pending_event(record: Mapping[str, Any]) -> None:
        key = str(record["source_id"])
        identity = str(record.get("candidate_identity") or "")
        if not identity:
            store(record, key)
            return
        registry_key = (str(record.get("branch") or "default"), identity)
        existing_key = candidate_index.get(registry_key)
        if not existing_key or existing_key not in items:
            aliases = set(candidate_aliases.get(key, set()))
            aliases.add(key)
            chosen = dict(record)
            _merge_event_review_state(chosen, aliases, records)
            chosen["review_aliases"] = sorted(aliases)
            items[key] = chosen
            candidate_index[registry_key] = key
            candidate_aliases[key] = aliases
            return

        existing = items[existing_key]
        aliases = set(candidate_aliases.pop(existing_key, set()))
        aliases.update({existing_key, key})
        if _prefer_pending_event(record, existing):
            chosen = dict(record)
        else:
            chosen = dict(existing)
        _merge_event_review_state(chosen, aliases, records)
        chosen["review_aliases"] = sorted(aliases)
        winner_key = str(chosen["source_id"])
        items.pop(existing_key, None)
        items[winner_key] = chosen
        candidate_index[registry_key] = winner_key
        candidate_aliases[winner_key] = aliases

    for path in artifact_paths:
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            artifact_errors.append(_relative(path, root_path))
            continue

        info = _artifact_kind_and_labels(path)
        resolve_time = _segment_time_lookup(path, info)
        is_variant_artifact = "decision-variants" in path.parts
        variants = payload.get("variants")
        has_variants = isinstance(variants, Mapping) and any(
            isinstance(variant, Mapping) for variant in variants.values()
        )
        # 根上的 pending_events 只是「选中分支」的拷贝，不能与 variants 再算一遍。
        comparison_root = has_variants and not is_variant_artifact
        # 有的旧产物根上没有 variants 字段，但同目录已有 decision-variants/*。
        # 这时根事件仍只是选中分支拷贝，交给分支产物投影，避免多算一份。
        selected_root_copy = (
            not is_variant_artifact
            and not has_variants
            and path.parent in branch_copy_dirs
        )
        found = False

        pending_sources = payload.get("pending_sources")
        if not is_variant_artifact and isinstance(pending_sources, list):
            outcome_by_id = _source_outcomes(payload)
            batch_failure_metadata: dict[str, dict[str, Any]] = {}
            raw_batches = payload.get("batches")
            if isinstance(raw_batches, list):
                for batch in raw_batches:
                    if not isinstance(batch, Mapping) or batch.get("status") != "failed":
                        continue
                    for source_id in batch.get("source_ids") or []:
                        if isinstance(source_id, str) and source_id:
                            batch_failure_metadata[source_id] = {
                                "failure_kind": batch.get("failure_kind"),
                                "http_status": batch.get("http_status"),
                            }
            for raw in pending_sources:
                if not isinstance(raw, Mapping):
                    continue
                source_id = str(raw.get("source_id") or "").strip()
                if not source_id:
                    continue
                found = True
                meta = reason_meta(raw.get("reason"))
                segment_id = raw.get("segment_id")
                failure_kind = raw.get("failure_kind")
                http_status = raw.get("http_status")
                batch_meta = batch_failure_metadata.get(source_id) or {}
                if failure_kind is None:
                    failure_kind = batch_meta.get("failure_kind")
                if http_status is None:
                    http_status = batch_meta.get("http_status")
                if meta["group"] == GROUP_BATCH_FAILURE and (
                    failure_kind is not None or http_status is not None
                ):
                    retryable = failure_is_retryable(failure_kind, http_status)
                    retryable_source = "failure_metadata"
                    retryable_warning = None
                else:
                    # 【2026-10-24 P1 · Q165】legacy 产物只有布尔 `retryable`，
                    # 没有任何可解释证据（failure_kind / http_status / batches）。
                    # 盲信 `retryable=true` 会把 401/402/schema 失败也变成"可重试"，
                    # 反复烧模型。这里改为 **fail-closed**：布尔值不构成重试依据；
                    # 原始值和告警一起留在记录里，方便 UI/审计看见"它曾自称可重试"。
                    legacy_value = bool(raw.get("retryable"))
                    retryable = legacy_retryable_is_trusted(legacy_value)
                    retryable_source = (
                        "legacy_unverified" if legacy_value else "legacy"
                    )
                    retryable_warning = legacy_retryable_warning(legacy_value)
                # 折叠组（整批 API 失败）不需要逐条时钟：它们默认不展示，
                # 日期退回产物级别，避免为了 3600+ 条垃圾去读转写文件。
                resolved = (
                    None
                    if meta["group"] in COLLAPSED_GROUPS
                    else resolve_time(segment_id)
                )
                resolved_time = resolved.get("recorded_at") if resolved else None
                outcome = outcome_by_id.get(source_id) or {}
                record = {
                    "source_id": source_id,
                    "kind": "pending_source",
                    "segment_id": segment_id,
                    "quote": str(raw.get("quote") or "").strip(),
                    "source_label": raw.get("source_label"),
                    "reason": meta["reason"],
                    "reason_label": meta["label"],
                    "reason_hint": meta["hint"],
                    "group": meta["group"],
                    "rank": meta["rank"],
                    "failure_kind": failure_kind,
                    "http_status": http_status,
                    "retryable": retryable,
                    "retryable_source": retryable_source,
                    # 【Q165】legacy=true 被 fail-closed 拒绝时留痕：
                    # legacy 原值 + 人类可读告警（结构化证据时为 None）
                    "retryable_legacy_value": bool(raw.get("retryable")),
                    "retryable_warning": retryable_warning,
                    "artifact": {
                        "kind": info["kind"],
                        "label": info["label"],
                        "date": info.get("date"),
                        "path": _relative(path, root_path),
                        "job_id": info.get("job_id"),
                        "session_id": info.get("session_id"),
                        "device_id": info.get("device_id"),
                    },
                    "outcome": (
                        {
                            "semantic_status": outcome.get("semantic_status"),
                            "reason": outcome.get("reason"),
                            "event_count": (
                                len(outcome.get("event_refs") or [])
                                if isinstance(outcome.get("event_refs"), list)
                                else 0
                            ),
                        }
                        if outcome
                        else None
                    ),
                    "recorded_at": resolved_time,
                    "time_source": resolved.get("time_source") if resolved else None,
                    "date": _beijing_date(resolved_time) or info.get("date"),
                    "time": _beijing_clock(resolved_time),
                }
                store(record, source_id)

        def add_accepted_events(
            event_payload: Mapping[str, Any], branch: str
        ) -> None:
            project = event_payload.get("formal_events")
            if not isinstance(project, list):
                return
            for raw_event in project:
                if not isinstance(raw_event, Mapping):
                    continue
                identity = _pending_candidate_identity(raw_event)
                if identity:
                    accepted_candidates.add((branch, identity))

        def add_pending_events(
            event_payload: Mapping[str, Any], branch: str
        ) -> bool:
            project = event_payload.get("pending_events")
            if not isinstance(project, list) or not project:
                return False
            outcomes = _source_outcomes(event_payload)
            found_any = False
            for raw_event in project:
                if not isinstance(raw_event, Mapping):
                    continue
                record = _pending_event_record(
                    raw_event,
                    branch=branch,
                    path=path,
                    root_path=root_path,
                    info=info,
                    resolve_time=resolve_time,
                    outcomes=outcomes,
                )
                if record is None:
                    continue
                found_any = True
                store_pending_event(record)
            return found_any

        if comparison_root:
            for provider, variant_payload in variants.items():
                if not isinstance(variant_payload, Mapping):
                    continue
                branch = str(provider)
                add_accepted_events(variant_payload, branch)
                if add_pending_events(variant_payload, branch):
                    found = True
        elif not is_variant_artifact:
            if not selected_root_copy:
                branch = _artifact_branch(path, payload)
                add_accepted_events(payload, branch)
                if add_pending_events(payload, branch):
                    found = True
        else:
            # 分支产物只投影 pending_events；pending_sources 仍由根产物按
            # legacy source_id 投影，避免同一来源借分支再长出一份新键。
            branch = _artifact_branch(path, payload)
            add_accepted_events(payload, branch)
            if add_pending_events(payload, branch):
                found = True

        if found:
            artifact_count += 1

    activity_pending_paths = set()
    for path, record in _published_review_rows(root_path):
        store(record, record["source_id"])
        activity_pending_paths.add(path)
    artifact_count += len(activity_pending_paths)

    if accepted_candidates:
        for key, item in list(items.items()):
            if item.get("kind") != "pending_event":
                continue
            identity = str(item.get("candidate_identity") or "")
            if identity and (str(item.get("branch") or "default"), identity) in accepted_candidates:
                items.pop(key, None)

    return items, artifact_count, artifact_errors


def _apply_question_mute(items, include_resolved: bool, target_date: str | None) -> list[dict[str, Any]]:
    """单一投影：默认隐藏被“媒体”静音的来源；include_resolved 时仍可追溯。"""
    return [
        item
        for item in items
        if (include_resolved or (item["state"] == "open" and item.get("review_question_state") != "muted"))
        and (target_date is None or item["date"] == target_date)
    ]


def collect_pending_sources(
    root: Path | str,
    *,
    date: str | None = None,
    include_collapsed: bool = False,
    include_resolved: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """扫描全部抽取产物，按 ``reason`` 分组投影 ``pending_sources``。

    - ``date``：只保留该北京日期（按条目真实录音时间，取不到则退回产物日期）
    - ``include_collapsed``：是否返回默认折叠组（整批 API 失败）的条目
    - ``include_resolved``：是否返回已被人工忽略/处理的条目
    - 同名 ``source_id`` 跨产物去重（watch-session 产物优先）
    """

    root_path = Path(root)
    limit_value = _normalize_limit(limit)
    target_date = str(date or "").strip() or None
    if target_date:
        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError as error:
            raise PendingReviewError("date 需要 YYYY-MM-DD") from error
    states = load_review_states(root_path)
    records = states["records"]
    items, artifact_count, artifact_errors = _collect_pending_items(
        root_path, records
    )

    # 统计一律基于去重后的集合，避免与「跨产物重复」耦合。
    totals = {
        "all": len(items),
        "needs_review": 0,
        "batch_failure": 0,
        "resolved": 0,
        # ``resolved`` stays as the backward-compatible total.  These split it
        # so an archived batch failure cannot masquerade as a human decision.
        "resolved_ignored_by_user": 0,
        "resolved_handled_by_user": 0,
        "resolved_failed_archived": 0,
        "batch_failure_open": 0,
        "batch_failure_archived": 0,
        "undated": 0,
        # Historical group totals above remain backward compatible. These are
        # the actual open queues, independent of include_resolved or date tabs.
        "open_actionable": 0,
        "open_extraction_gap": 0,
        "open_batch_failure": 0,
    }
    reason_counts: dict[str, int] = {}
    for record in items.values():
        key = record["reason"] or "<none>"
        reason_counts[key] = reason_counts.get(key, 0) + 1
        if record["group"] == GROUP_BATCH_FAILURE:
            totals["batch_failure"] += 1
            if record["state"] != "open":
                totals["batch_failure_archived"] += 1
            else:
                totals["batch_failure_open"] += 1
        elif record["group"] == GROUP_NEEDS_REVIEW:
            totals["needs_review"] += 1
        if not record["date"]:
            totals["undated"] += 1
        if record["state"] != "open":
            totals["resolved"] += 1
            resolution = record.get("resolution")
            if resolution == RESOLUTION_FAILED_ARCHIVED:
                totals["resolved_failed_archived"] += 1
            elif resolution == RESOLUTION_IGNORED_BY_USER:
                totals["resolved_ignored_by_user"] += 1
            elif resolution == RESOLUTION_HANDLED_BY_USER:
                totals["resolved_handled_by_user"] += 1
        else:
            open_key = {
                GROUP_NEEDS_REVIEW: "open_actionable",
                GROUP_EXTRACTION_GAP: "open_extraction_gap",
                GROUP_BATCH_FAILURE: "open_batch_failure",
            }.get(record["group"])
            if open_key:
                totals[open_key] += 1

    # 【2026-10-25 Q408】把 review-questions 的持久状态**批量**投影进待回答列表：
    # answered / uncertain / muted 都要透传（query/personal 不关提醒、不转事件、不记账；
    # uncertain 继续待定；muted 走既有隐藏路径）。一次查询，避免 N+1。
    try:
        from life_logger.review_questions import review_question_states
        question_states = review_question_states(
            root_path, [str(i.get("source_id") or "") for i in items.values()]
        )
    except Exception:
        question_states = {}
    for item in items.values():
        source_id = str(item.get("source_id") or "")
        state = question_states.get(source_id)
        if state:
            item["review_question_state"] = state["state"]
            item["review_question_operation"] = state.get("operation")
            item["review_question_op_id"] = state.get("op_id")
            item["review_question_answered_at"] = state.get("answered_at")
            item["review_question_prior_status"] = state.get("prior_status")
            item["review_question_undoable"] = state.get("undoable", False)
        else:
            # 缺状态（缺 source / 从未回答）→ 保持 unknown，不推断
            if not item.get("review_question_state"):
                item["review_question_state"] = "unknown"
            item.setdefault("review_question_undoable", False)

    scoped = _apply_question_mute(items.values(), include_resolved, target_date)
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in GROUP_ORDER}
    for item in scoped:
        grouped.setdefault(item["group"], []).append(item)

    groups = []
    for key in GROUP_ORDER:
        entries = grouped.get(key) or []
        if not entries:
            continue
        entries.sort(
            key=lambda entry: (
                entry["rank"],
                entry["date"] or "",
                entry["time"] or "",
                entry["source_id"],
            )
        )
        collapsed = key in COLLAPSED_GROUPS
        group = group_meta(key)
        group["count"] = len(entries)
        group["items"] = (
            [] if (collapsed and not include_collapsed) else entries[:limit_value]
        )
        group["items_truncated"] = max(0, len(entries) - len(group["items"]))
        groups.append(group)

    reasons = [
        {
            "reason": reason,
            "count": count,
            "group": reason_meta(reason).get("group"),
            "label": reason_meta(reason).get("label"),
        }
        for reason, count in sorted(
            reason_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )
    ]
    shown = sum(len(group["items"]) for group in groups)
    stamp = (now or datetime.now(tz=BEIJING)).astimezone(BEIJING).isoformat()
    return {
        "ok": True,
        "schema": SCHEMA,
        "generated_at": stamp,
        "scope": {
            "date": target_date,
            "include_collapsed": bool(include_collapsed),
            "include_resolved": bool(include_resolved),
            "limit": limit_value,
        },
        "totals": {
            **totals,
            "scoped": len(scoped),
            "scoped_open_actionable": sum(
                item["state"] == "open" and item["group"] == GROUP_NEEDS_REVIEW
                for item in scoped
            ),
            "scoped_open_extraction_gap": sum(
                item["state"] == "open" and item["group"] == GROUP_EXTRACTION_GAP
                for item in scoped
            ),
            "shown": shown,
            "artifacts": artifact_count,
        },
        "groups": groups,
        "reasons": reasons,
        "review_states": {
            "schema": states["schema"],
            "updated_at": states["updated_at"],
            # Projected resolutions, not the raw ledger statuses: archived
            # batch failures must never appear as human "ignored" counts.
            "counts": {
                "ignored": totals["resolved_ignored_by_user"],
                "handled": totals["resolved_handled_by_user"],
                "failed_archived": totals["resolved_failed_archived"],
            },
            "ledger_counts": _state_counts(records),
            "resolution_counts": {
                "ignored_by_user": totals["resolved_ignored_by_user"],
                "handled_by_user": totals["resolved_handled_by_user"],
                "failed_archived": totals["resolved_failed_archived"],
            },
            "total": len(records),
        },
        "artifact_errors": artifact_errors,
    }


def collect_pending_sources_cached(
    root: Path | str,
    *,
    date: str | None = None,
    include_collapsed: bool = False,
    include_resolved: bool = False,
    limit: int | None = None,
    ttl: float = RESULT_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    """`collect_pending_sources` 的短时缓存 + single-flight 包装（2026-10-24）。

    为什么：单次投影要读 389 个产物 JSON（含 1.3MB 的审核台账）并做 7000+ 次路径运算，
    实测本机 3.9s、线上高负载 5~48s；页面「审核后刷新」会并发打多次，直接把审核操作拖死。

    语义保证：
      * **写后立刻可见** —— 所有写路径经 `_write_review_states()` → `invalidate_pending_cache()`；
      * **不吞错** —— 生产函数抛异常时原样抛出，不缓存失败；
      * **并发只算一次** —— 同 key 的并发请求等待首个计算完成（single-flight）；
      * **不会写回旧结果** —— generation 变化时，失效前启动的计算结果直接丢弃。
    """
    key = (
        str(Path(root).resolve()),
        date or '',
        bool(include_collapsed),
        bool(include_resolved),
        limit if limit is not None else -1,
    )
    now = time.monotonic()
    with _RESULT_LOCK:
        hit = _RESULT_CACHE.get(key)
        if hit is not None and now - hit[0] <= ttl:
            return hit[1]
        flight = _RESULT_INFLIGHT.get(key)
        owner = flight is None
        if owner:
            flight = threading.Event()
            _RESULT_INFLIGHT[key] = flight
    if not owner:
        flight.wait(timeout=60.0)
        with _RESULT_LOCK:
            hit = _RESULT_CACHE.get(key)
        if hit is not None:
            return hit[1]
        # 首个计算失败/被丢弃时退化为自行计算（绝不返回空壳假成功）
        return collect_pending_sources(
            root, date=date, include_collapsed=include_collapsed,
            include_resolved=include_resolved, limit=limit,
        )
    generation = _RESULT_GENERATION
    try:
        value = collect_pending_sources(
            root, date=date, include_collapsed=include_collapsed,
            include_resolved=include_resolved, limit=limit,
        )
    finally:
        with _RESULT_LOCK:
            _RESULT_INFLIGHT.pop(key, None)
        flight.set()
    with _RESULT_LOCK:
        if generation == _RESULT_GENERATION:
            _RESULT_CACHE[key] = (time.monotonic(), value)
    return value


def _prefer(candidate: Mapping[str, Any], existing: Mapping[str, Any]) -> bool:
    """去重时保留更有信息量的一份：watch-session 产物优先，其次有日期的。"""

    order = {"watch-session": 0, "job": 1, "replay": 2, "other": 3}
    candidate_kind = order.get(str((candidate.get("artifact") or {}).get("kind")), 3)
    existing_kind = order.get(str((existing.get("artifact") or {}).get("kind")), 3)
    if candidate_kind != existing_kind:
        return candidate_kind < existing_kind
    if bool(candidate.get("date")) != bool(existing.get("date")):
        return bool(candidate.get("date"))
    candidate_at = str(candidate.get("recorded_at") or "")
    existing_at = str(existing.get("recorded_at") or "")
    return candidate_at > existing_at


def summarize_for_ui(payload: Mapping[str, Any]) -> dict[str, Any]:
    """给前端一行摘要用的极简投影（供测试与徽章类消费方复用）。"""

    totals = payload.get("totals") or {}
    return {
        "actionable": int(totals.get("open_actionable", totals.get("needs_review")) or 0),
        "batch_failure": int(totals.get("batch_failure") or 0),
        "all": int(totals.get("all") or 0),
        "resolved": int(totals.get("resolved") or 0),
        "ignored_by_user": int(totals.get("resolved_ignored_by_user") or 0),
        "failed_archived": int(totals.get("resolved_failed_archived") or 0),
    }


def iter_open_quotes(payload: Mapping[str, Any]) -> Iterable[str]:
    """按展示顺序产出「需要你确认」组的原文（供导出/重听等消费方复用）。"""

    for group in payload.get("groups") or []:
        if group.get("key") != GROUP_NEEDS_REVIEW:
            continue
        for item in group.get("items") or []:
            quote = str(item.get("quote") or "")
            if quote:
                yield quote


__all__: Sequence[str] = (
    "BEIJING",
    "COLLAPSED_GROUPS",
    "DEFAULT_ITEM_LIMIT",
    "GROUP_BATCH_FAILURE",
    "GROUP_EXTRACTION_GAP",
    "GROUP_LABELS",
    "GROUP_NEEDS_REVIEW",
    "GROUP_ORDER",
    "GROUP_OTHER",
    "PendingReviewError",
    "REASON_META",
    "RESOLUTION_FAILED_ARCHIVED",
    "RESOLUTION_HANDLED_BY_USER",
    "RESOLUTION_IGNORED_BY_USER",
    "SCHEMA",
    "STATES_FILENAME",
    "STATES_SCHEMA",
    "collect_pending_sources",
    "group_meta",
    "iter_open_quotes",
    "load_review_states",
    "reason_meta",
    "set_pending_review_state",
    "set_pending_review_states_bulk",
    "summarize_for_ui",
)
