"""首页消费明细「改金额」读写与跨重跑回退匹配。

复用 `<root>/ledger-review.json`（schema `ledger-review-v1`）的 `confirm` 决策：
- 写入时用 `file_lock` + 临时文件 + `os.replace` 原子落盘，并同时记录
  `original_amount`、`original_label`、`quote`、`quotes`、`time`、`clock`；
- 读取时优先按 `entry_key`（如 `act_xxx#it3`）精确匹配；
  当 v3 重跑同一天导致 `activity_id` 或 `#itN` 序号变化时，按
  `(原金额 + 原名称 + 原话 + 时间)` 在当天候选中找回；
- 找不回时绝不静默丢弃，挂入 `expense_ledger["unmatched_edits"]` 供前端展示与撤销。
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from life_logger.atomic import file_lock

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REVIEW_FILENAME = "ledger-review.json"
REVIEW_SCHEMA = "ledger-review-v1"
EDIT_BASIS = "首页改金额"


class ExpenseEditError(ValueError):
    """非法日期、键或金额参数。"""


def validate_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as error:
        raise ExpenseEditError("date 必须为 YYYY-MM-DD 格式") from error
    if parsed.isoformat() != text:
        raise ExpenseEditError("date 必须为 YYYY-MM-DD 格式")
    return text


def validate_amount(value: Any) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise ExpenseEditError("amount 必须为大于 0 的数字")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ExpenseEditError("amount 必须为大于 0 的数字") from error
    if not math.isfinite(numeric) or numeric <= 0:
        raise ExpenseEditError("amount 必须为大于 0 的数字")
    return round(numeric, 2)


def _norm_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[\s，。！？、；：,.!?;: consumer\"'“”‘’（）()\[\]【】]+", "", text).lower()


def _shanghai_parts(raw_time: Any) -> tuple[str, str]:
    text = str(raw_time or "").strip()
    if not text:
        return "", ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        else:
            dt = dt.astimezone(SHANGHAI_TZ)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        if len(text) >= 16 and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text):
            return text[:10], text[11:16]
        if len(text) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return text[:10], ""
        return "", ""


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return round(num, 2) if math.isfinite(num) else None


def is_edit_decision(decision: Any) -> bool:
    """判断一条 ledger-review 决策是否属于「改金额」记录。"""
    if not isinstance(decision, Mapping):
        return False
    if str(decision.get("action") or "") != "confirm":
        return False
    return (
        str(decision.get("basis") or "") == EDIT_BASIS
        or decision.get("original_amount") is not None
    )


def load_raw_decisions(root: Path) -> dict[str, dict[str, Any]]:
    """读取 `<root>/ledger-review.json` 中的完整 decisions 字典。"""
    path = Path(root) / REVIEW_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        decisions = payload.get("decisions")
        if not isinstance(decisions, Mapping):
            return {}
        return {
            str(key): dict(val)
            for key, val in decisions.items()
            if isinstance(val, Mapping)
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def extract_candidates(
    events: Sequence[Mapping[str, Any]] | None,
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    default_date: str = "",
) -> list[dict[str, Any]]:
    """从活动子项与事件列表中提取当天/区间内所有可匹配消费候选。"""
    candidates: list[dict[str, Any]] = []
    for activity in activities or ():
        if not isinstance(activity, Mapping) or activity.get("suppressed"):
            continue
        act_id = str(activity.get("activity_id") or activity.get("activityId") or "").strip()
        if not act_id:
            continue
        act_title = str(activity.get("title") or "").strip()
        merged_ids = [
            str(mid).strip()
            for mid in (activity.get("merged_activity_ids") or [])
            if str(mid or "").strip()
        ]
        fallback_time = (
            activity.get("occurrence_start")
            or activity.get("spoken_at")
            or activity.get("reported_at")
            or ""
        )
        for index, item in enumerate(activity.get("items") or ()):
            if not isinstance(item, Mapping):
                continue
            entry_key = f"{act_id}#it{index + 1}"
            exp = item.get("expense") if isinstance(item.get("expense"), Mapping) else {}
            raw_amount = exp.get("amount") if exp.get("amount") is not None else item.get("amount")
            amount = _safe_float(raw_amount)
            label = str(
                exp.get("item")
                or item.get("action")
                or act_title
                or ""
            ).strip()
            evidence = [e for e in (item.get("evidence") or ()) if isinstance(e, Mapping)]
            quotes = [
                str(e.get("quote") or "").strip()
                for e in evidence
                if str(e.get("quote") or "").strip()
            ]
            primary_quote = max(quotes, key=len) if quotes else ""
            raw_time = str(
                item.get("occurred_at")
                or item.get("spoken_at")
                or fallback_time
                or ""
            ).strip()
            day_str, clock_str = _shanghai_parts(raw_time)
            candidates.append(
                {
                    "entry_key": entry_key,
                    "merged_keys": [f"{mid}#it{index + 1}" for mid in merged_ids if mid != act_id],
                    "amount": amount,
                    "label": label,
                    "title": str(item.get("action") or act_title or label).strip(),
                    "quote": primary_quote,
                    "quotes": quotes,
                    "time": raw_time,
                    "clock": clock_str,
                    "date": day_str or default_date,
                    "fact_type": str(item.get("fact_type") or ""),
                }
            )

    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        ev_id = str(event.get("event_id") or "").strip()
        if not ev_id:
            continue
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        exp = data.get("expense") if isinstance(data.get("expense"), Mapping) else {}
        amount = _safe_float(exp.get("amount"))
        label = str(exp.get("item") or event.get("title") or "").strip()
        ev_obj = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
        quotes: list[str] = []
        for q in (ev_obj.get("quote"), event.get("raw_quote"), event.get("quote")):
            q_str = str(q or "").strip()
            if q_str and q_str not in quotes:
                quotes.append(q_str)
        for q in ev_obj.get("quotes") or ():
            q_str = str(q or "").strip()
            if q_str and q_str not in quotes:
                quotes.append(q_str)
        time_obj = event.get("time") if isinstance(event.get("time"), Mapping) else {}
        raw_time = str(
            time_obj.get("absolute")
            or event.get("reported_at")
            or ""
        ).strip()
        day_str, clock_str = _shanghai_parts(raw_time)
        candidates.append(
            {
                "entry_key": ev_id,
                "merged_keys": [],
                "amount": amount,
                "label": label,
                "title": str(event.get("title") or label).strip(),
                "quote": quotes[0] if quotes else "",
                "quotes": quotes,
                "time": raw_time,
                "clock": clock_str,
                "date": day_str or default_date,
                "fact_type": "expense",
            }
        )
    return candidates


def match_candidate_fallback(
    decision_key: str,
    decision: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    used_keys: set[str],
) -> dict[str, Any] | None:
    """当 `decision_key` 因 v3 重跑失效时，按 (原金额 + 原名称 + 原话 + 时间) 找回候选。"""
    orig_amount = _safe_float(decision.get("original_amount"))
    if orig_amount is None:
        return None

    dec_date = str(decision.get("date") or "").strip()
    orig_label = _norm_text(decision.get("original_label") or decision.get("label"))
    dec_quotes = {
        _norm_text(q)
        for q in ([decision.get("quote")] + list(decision.get("quotes") or []))
        if _norm_text(q)
    }
    dec_time = str(decision.get("time") or "").strip()
    _, dec_time_clock = _shanghai_parts(dec_time)
    dec_clock = str(decision.get("clock") or dec_time_clock or "").strip()

    best_cand: dict[str, Any] | None = None
    best_score = -1

    for cand in candidates:
        ckey = str(cand.get("entry_key") or "")
        if not ckey or ckey in used_keys:
            continue
        cand_date = str(cand.get("date") or "").strip()
        if dec_date and cand_date and dec_date != cand_date:
            continue
        cand_amount = _safe_float(cand.get("amount"))
        if cand_amount is None or abs(cand_amount - orig_amount) >= 0.005:
            continue

        cand_label = _norm_text(cand.get("label"))
        cand_title = _norm_text(cand.get("title"))
        label_match = bool(
            orig_label
            and (
                orig_label == cand_label
                or orig_label == cand_title
                or (len(orig_label) >= 2 and (orig_label in cand_label or cand_label in orig_label or orig_label in cand_title))
            )
        )

        cand_quotes = {
            _norm_text(q)
            for q in ([cand.get("quote")] + list(cand.get("quotes") or []))
            if _norm_text(q)
        }
        quote_match = bool(
            dec_quotes
            and cand_quotes
            and (
                bool(dec_quotes & cand_quotes)
                or any(
                    len(dq) >= 3 and len(cq) >= 3 and (dq in cq or cq in dq)
                    for dq in dec_quotes
                    for cq in cand_quotes
                )
            )
        )

        cand_time = str(cand.get("time") or "").strip()
        cand_clock = str(cand.get("clock") or "").strip()
        exact_time_match = bool(dec_time and cand_time and dec_time == cand_time)
        clock_match = bool(dec_clock and cand_clock and dec_clock == cand_clock)
        time_match = exact_time_match or clock_match
        merged_id_match = decision_key in (cand.get("merged_keys") or [])

        available_checks = [
            flag
            for present, flag in (
                (bool(orig_label), label_match),
                (bool(dec_quotes), quote_match),
                (bool(dec_time or dec_clock), time_match),
            )
            if present
        ]
        matched_count = sum(1 for flag in (label_match, quote_match, time_match) if flag)
        if not available_checks:
            continue
        if len(available_checks) >= 2 and matched_count < 2 and not (merged_id_match and matched_count >= 1):
            continue
        if len(available_checks) == 1 and not available_checks[0]:
            continue

        score = (
            (8 if merged_id_match else 0)
            + (4 if quote_match else 0)
            + (3 if label_match else 0)
            + (2 if exact_time_match else (1 if clock_match else 0))
        )
        if score > best_score:
            best_score = score
            best_cand = cand

    return best_cand


def resolve_confirmed_and_edits(
    raw_decisions: Mapping[str, Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]] | None,
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    day: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """解析 `ledger-review.json` 的 confirm 决策，支持 v3 重跑后的回退找回。

    返回 `(effective_confirmed, matched_edit_by_event_id, unmatched_edits)`：
      - `effective_confirmed`: 传给 `build_expense_ledger(..., confirmed=...)` 的 `{current_event_id: {amount, label}}`
      - `matched_edit_by_event_id`: `{current_event_id: edit_meta}`，用于在交易列表上标注 `edited` / `original_amount` / `entry_key`
      - `unmatched_edits`: 当天/区间内未能匹配上任何候选的改金额记录
    """
    effective_confirmed: dict[str, dict[str, Any]] = {}
    matched_edit_by_event_id: dict[str, dict[str, Any]] = {}
    unmatched_edits: list[dict[str, Any]] = []

    if not raw_decisions:
        return effective_confirmed, matched_edit_by_event_id, unmatched_edits

    candidates = extract_candidates(events, activities, default_date=day or "")
    candidate_by_key = {str(c["entry_key"]): c for c in candidates if c.get("entry_key")}
    used_candidate_keys: set[str] = set()

    def _in_scope(dec_date: str) -> bool:
        if day:
            return dec_date == day
        if start and end and dec_date:
            return start <= dec_date <= end
        return True

    # 第一遍：精确 entry_key 命中
    pending_fallback: list[tuple[str, Mapping[str, Any]]] = []
    for dec_key, dec_val in raw_decisions.items():
        if not isinstance(dec_val, Mapping) or str(dec_val.get("action") or "confirm") != "confirm":
            continue
        amt = _safe_float(dec_val.get("amount"))
        if amt is None:
            continue
        if dec_key in candidate_by_key or not is_edit_decision(dec_val):
            effective_confirmed[dec_key] = {
                "amount": amt,
                "label": dec_val.get("label"),
            }
            if dec_key in candidate_by_key:
                used_candidate_keys.add(dec_key)
                if is_edit_decision(dec_val):
                    cand = candidate_by_key[dec_key]
                    orig_amt = _safe_float(dec_val.get("original_amount"))
                    if orig_amt is None:
                        orig_amt = _safe_float(cand.get("amount"))
                    matched_edit_by_event_id[dec_key] = {
                        "entry_key": dec_key,
                        "current_event_id": dec_key,
                        "amount": amt,
                        "original_amount": orig_amt,
                        "original_label": str(dec_val.get("original_label") or cand.get("label") or ""),
                        "quote": str(dec_val.get("quote") or cand.get("quote") or ""),
                        "time": str(dec_val.get("time") or cand.get("time") or ""),
                        "clock": str(dec_val.get("clock") or cand.get("clock") or ""),
                        "date": str(dec_val.get("date") or cand.get("date") or day or ""),
                        "decided_at": str(dec_val.get("decided_at") or ""),
                        "basis": str(dec_val.get("basis") or EDIT_BASIS),
                        "matched_by": "entry_key",
                    }
                continue
        # 精确键未命中且是改金额记录 → 进入第二遍按 (原金额+原名称+原话+时间) 回退找回
        dec_date = str(dec_val.get("date") or "").strip()
        if is_edit_decision(dec_val) and _in_scope(dec_date):
            pending_fallback.append((dec_key, dec_val))

    # 第二遍：回退匹配
    for dec_key, dec_val in pending_fallback:
        amt = _safe_float(dec_val.get("amount"))
        if amt is None:
            continue
        matched_cand = match_candidate_fallback(
            dec_key, dec_val, candidates, used_keys=used_candidate_keys
        )
        if matched_cand is not None:
            cand_key = str(matched_cand["entry_key"])
            used_candidate_keys.add(cand_key)
            effective_confirmed[cand_key] = {
                "amount": amt,
                "label": dec_val.get("label") or matched_cand.get("label"),
            }
            orig_amt = _safe_float(dec_val.get("original_amount"))
            if orig_amt is None:
                orig_amt = _safe_float(matched_cand.get("amount"))
            matched_edit_by_event_id[cand_key] = {
                "entry_key": dec_key,
                "current_event_id": cand_key,
                "amount": amt,
                "original_amount": orig_amt,
                "original_label": str(dec_val.get("original_label") or matched_cand.get("label") or ""),
                "quote": str(dec_val.get("quote") or matched_cand.get("quote") or ""),
                "time": str(dec_val.get("time") or matched_cand.get("time") or ""),
                "clock": str(dec_val.get("clock") or matched_cand.get("clock") or ""),
                "date": str(dec_val.get("date") or matched_cand.get("date") or day or ""),
                "decided_at": str(dec_val.get("decided_at") or ""),
                "basis": str(dec_val.get("basis") or EDIT_BASIS),
                "matched_by": "fallback",
            }
        else:
            unmatched_edits.append(_format_unmatched_edit(dec_key, dec_val, default_date=day or ""))

    return effective_confirmed, matched_edit_by_event_id, unmatched_edits


def _format_unmatched_edit(
    dec_key: str,
    dec_val: Mapping[str, Any],
    *,
    default_date: str = "",
) -> dict[str, Any]:
    raw_time = str(dec_val.get("time") or "").strip()
    _, time_clock = _shanghai_parts(raw_time)
    clock = str(dec_val.get("clock") or time_clock or "").strip()
    amt = _safe_float(dec_val.get("amount"))
    orig_amt = _safe_float(dec_val.get("original_amount"))
    label = str(dec_val.get("label") or dec_val.get("original_label") or "").strip()
    orig_label = str(dec_val.get("original_label") or label).strip()
    return {
        "entry_key": dec_key,
        "date": str(dec_val.get("date") or default_date),
        "amount": amt,
        "label": label,
        "original_amount": orig_amt,
        "original_label": orig_label,
        "quote": str(dec_val.get("quote") or "").strip(),
        "quotes": list(dec_val.get("quotes") or []),
        "time": raw_time,
        "clock": clock,
        "decided_at": str(dec_val.get("decided_at") or ""),
        "basis": str(dec_val.get("basis") or EDIT_BASIS),
    }


def enrich_ledger_with_edits(
    ledger: dict[str, Any],
    *,
    matched_edit_by_event_id: Mapping[str, Mapping[str, Any]],
    unmatched_edits: Sequence[Mapping[str, Any]],
    has_any_decisions: bool = False,
) -> dict[str, Any]:
    """在生成的消费账本上补齐 `entry_key`、`edited`/`original_amount` 标注及 `unmatched_edits`。"""
    seen_in_active_tx: set[str] = set()
    for list_key in ("transactions", "payment_transactions", "all_transactions"):
        tx_list = ledger.get(list_key)
        if not isinstance(tx_list, list):
            continue
        for tx in tx_list:
            if not isinstance(tx, dict):
                continue
            ev_id = str(tx.get("event_id") or "").strip()
            if ev_id and not tx.get("entry_key"):
                tx["entry_key"] = ev_id
            edit_meta = matched_edit_by_event_id.get(ev_id)
            if edit_meta is not None:
                if list_key in ("transactions", "payment_transactions"):
                    seen_in_active_tx.add(ev_id)
                tx["entry_key"] = str(edit_meta.get("entry_key") or ev_id)
                tx["stored_entry_key"] = str(edit_meta.get("entry_key") or ev_id)
                tx["edited"] = True
                tx["original_amount"] = edit_meta.get("original_amount")
                tx["original_label"] = edit_meta.get("original_label")
                tx["decided_at"] = edit_meta.get("decided_at")
                tx["edit_basis"] = edit_meta.get("basis")
                tx["matched_by"] = edit_meta.get("matched_by")

    final_unmatched = [dict(item) for item in unmatched_edits]
    for ev_id, edit_meta in matched_edit_by_event_id.items():
        if ev_id not in seen_in_active_tx:
            final_unmatched.append(
                _format_unmatched_edit(
                    str(edit_meta.get("entry_key") or ev_id),
                    edit_meta,
                    default_date=str(edit_meta.get("date") or ""),
                )
            )

    ledger["unmatched_edits"] = final_unmatched
    return ledger


def _find_day_candidates_for_root(root: Path, date: str) -> list[dict[str, Any]]:
    from life_logger.day_facts import (
        activities_for_day,
        filter_events_for_activity_cutover,
    )

    activities = activities_for_day(date, root=root)
    events: list[Mapping[str, Any]] = []
    try:
        from life_logger.event_view import build_event_view

        payload = build_event_view(root, date_from=date, date_to=date, use_cache=False)
        raw_events = [e for e in (payload.get("events") or []) if isinstance(e, Mapping)]
        events = filter_events_for_activity_cutover(raw_events, day=date, activities=activities)
    except Exception:
        events = []
    return extract_candidates(events, activities, default_date=date)


def edit_expense(
    root: Path | str,
    *,
    date: str,
    entry_key: str,
    amount: Any,
    label: str | None = None,
    original_amount: Any = None,
    original_label: str | None = None,
    quote: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    """在 `<root>/ledger-review.json` 写入一笔「首页改金额」confirm 决策（原子落盘 + 清缓存）。"""
    root_path = Path(root)
    valid_date = validate_date(date)
    clean_key = str(entry_key or "").strip()
    if not clean_key:
        raise ExpenseEditError("entry_key 不能为空")
    valid_amount = validate_amount(amount)

    candidates = _find_day_candidates_for_root(root_path, valid_date)
    candidate_by_key = {str(c["entry_key"]): c for c in candidates if c.get("entry_key")}
    cand = candidate_by_key.get(clean_key)

    review_path = root_path / REVIEW_FILENAME
    review_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(tz=SHANGHAI_TZ).isoformat()

    with file_lock(review_path):
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {"schema": REVIEW_SCHEMA, "decisions": {}}
        except (OSError, json.JSONDecodeError):
            payload = {"schema": REVIEW_SCHEMA, "decisions": {}}
        payload.setdefault("schema", REVIEW_SCHEMA)
        decisions = payload.setdefault("decisions", {})
        if not isinstance(decisions, dict):
            decisions = {}
            payload["decisions"] = decisions

        existing = decisions.get(clean_key) if isinstance(decisions.get(clean_key), Mapping) else None
        replaced_fallback_key: str | None = None
        if existing is None and cand is not None:
            # 若传入的是重跑后的新 event_id，而旧 key 仍在 decisions 中，承接旧 key 的原金额信息并迁移
            for old_k, old_v in list(decisions.items()):
                if not is_edit_decision(old_v) or str(old_v.get("date") or "") != valid_date:
                    continue
                if old_k not in candidate_by_key:
                    m = match_candidate_fallback(old_k, old_v, [cand], used_keys=set())
                    if m is not None:
                        existing = old_v
                        replaced_fallback_key = old_k
                        break

        if cand is None and existing is None:
            raise ExpenseEditError(f"未找到 {valid_date} 对应消费条目: {clean_key}")

        # 原金额、原名称、原话、时间：优先保留首次改金额前记下的原始值，否则从候选提取
        resolved_orig_amount = (
            _safe_float(existing.get("original_amount"))
            if existing is not None and existing.get("original_amount") is not None
            else (
                _safe_float(cand.get("amount"))
                if cand is not None and cand.get("amount") is not None
                else _safe_float(original_amount)
            )
        )
        resolved_orig_label = str(
            (existing.get("original_label") if existing is not None else None)
            or (cand.get("label") if cand is not None else None)
            or original_label
            or label
            or ""
        ).strip()
        resolved_quote = str(
            (existing.get("quote") if existing is not None else None)
            or (cand.get("quote") if cand is not None else None)
            or quote
            or ""
        ).strip()
        resolved_quotes = list(
            (existing.get("quotes") if existing is not None and existing.get("quotes") else None)
            or (cand.get("quotes") if cand is not None and cand.get("quotes") else None)
            or ([resolved_quote] if resolved_quote else [])
        )
        resolved_time = str(
            (existing.get("time") if existing is not None else None)
            or (cand.get("time") if cand is not None else None)
            or time
            or ""
        ).strip()
        _, time_clock = _shanghai_parts(resolved_time)
        resolved_clock = str(
            (existing.get("clock") if existing is not None else None)
            or (cand.get("clock") if cand is not None else None)
            or time_clock
            or ""
        ).strip()
        resolved_label = str(
            label
            if (label is not None and str(label).strip())
            else (
                (existing.get("label") if existing is not None else None)
                or resolved_orig_label
                or clean_key
            )
        ).strip()

        target_key = replaced_fallback_key or clean_key
        decision_entry: dict[str, Any] = {
            "date": valid_date,
            "action": "confirm",
            "amount": valid_amount,
            "label": resolved_label,
            "decided_by": "user",
            "decided_at": now_iso,
            "basis": EDIT_BASIS,
            "original_amount": resolved_orig_amount,
            "original_label": resolved_orig_label,
            "quote": resolved_quote,
            "quotes": resolved_quotes,
            "time": resolved_time,
            "clock": resolved_clock,
        }
        decisions[target_key] = decision_entry
        _atomic_write_json(review_path, payload)

    _invalidate_all_caches(root_path, "edit_expense")
    ledger = _current_day_ledger(root_path, valid_date)
    return {
        "ok": True,
        "date": valid_date,
        "entry_key": target_key,
        "decision": decision_entry,
        "total_amount": ledger.get("total_amount"),
        "unmatched_edits": ledger.get("unmatched_edits") or [],
        "expense_ledger": ledger,
    }


def undo_expense_edit(
    root: Path | str,
    *,
    date: str,
    entry_key: str,
) -> dict[str, Any]:
    """撤销 `<root>/ledger-review.json` 中的一笔改金额决策（原子落盘 + 清缓存）。"""
    root_path = Path(root)
    valid_date = validate_date(date)
    clean_key = str(entry_key or "").strip()
    if not clean_key:
        raise ExpenseEditError("entry_key 不能为空")

    review_path = root_path / REVIEW_FILENAME
    removed_key = clean_key
    undone = False

    if review_path.exists():
        candidates = _find_day_candidates_for_root(root_path, valid_date)
        candidate_by_key = {str(c["entry_key"]): c for c in candidates if c.get("entry_key")}
        cand = candidate_by_key.get(clean_key)

        with file_lock(review_path):
            try:
                payload = json.loads(review_path.read_text(encoding="utf-8"))
                decisions = payload.get("decisions") if isinstance(payload, dict) else None
            except (OSError, json.JSONDecodeError):
                payload = None
                decisions = None

            if isinstance(payload, dict) and isinstance(decisions, dict):
                if clean_key in decisions:
                    decisions.pop(clean_key, None)
                    undone = True
                elif cand is not None:
                    for old_k, old_v in list(decisions.items()):
                        if not is_edit_decision(old_v) or str(old_v.get("date") or "") != valid_date:
                            continue
                        if old_k not in candidate_by_key:
                            m = match_candidate_fallback(old_k, old_v, [cand], used_keys=set())
                            if m is not None:
                                decisions.pop(old_k, None)
                                removed_key = old_k
                                undone = True
                                break
                if undone:
                    _atomic_write_json(review_path, payload)

    _invalidate_all_caches(root_path, "undo_expense_edit")
    ledger = _current_day_ledger(root_path, valid_date)
    return {
        "ok": True,
        "date": valid_date,
        "entry_key": removed_key,
        "undone": undone,
        "total_amount": ledger.get("total_amount"),
        "unmatched_edits": ledger.get("unmatched_edits") or [],
        "expense_ledger": ledger,
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        encoding="utf-8",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def _invalidate_all_caches(root: Path, reason: str) -> None:
    try:
        from life_logger.judgment_queue import invalidate_cache as invalidate_jq_cache

        invalidate_jq_cache()
    except Exception:
        pass
    try:
        from life_logger.web_support import invalidate_read_caches

        invalidate_read_caches(root, reason)
    except Exception:
        pass


def _current_day_ledger(root: Path, date: str) -> dict[str, Any]:
    from life_logger.day_facts import day_expense_ledger

    events: list[Mapping[str, Any]] = []
    try:
        from life_logger.event_view import build_event_view

        payload = build_event_view(root, date_from=date, date_to=date, use_cache=False)
        events = [e for e in (payload.get("events") or []) if isinstance(e, Mapping)]
    except Exception:
        events = []
    return day_expense_ledger(events, date, root=root)
