"""从 local_web.py 机械搬出的 web_search 部分，行为不变；分组依据见 work/split-prep-localweb-20261104/。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from life_logger.local_web import JobStore

from datetime import datetime
from datetime import timedelta
from life_logger.domain import ALLOWED_CATEGORIES
from life_logger.timeutil import parse_ts
from pathlib import Path
from typing import Any
from typing import Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo
import json
import re
from life_logger.web_common import _SEARCH_CACHE_MAX_ENTRIES, _SEARCH_CATEGORY_ALIASES, _SEARCH_EVENTS_CACHE, _SEARCH_SOURCE_MAP_CACHE, _read_contract
from life_logger.web_expense_range import _expense_range_signature


def _canonical_search_category(value: Any) -> str:
    category = str(value or "").strip()
    return _SEARCH_CATEGORY_ALIASES.get(category, category)


def _normalized_search_event_text(item: dict[str, Any]) -> str:
    value = f"{item.get('text') or ''} {item.get('quote') or ''}".casefold()
    for prefix in ("嗯", "呃", "啊", "这个", "那个", "然后", "就是", "我觉得", "我想"):
        stripped = value.lstrip(" ，,。！？!?；;：:")
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :]
            break
    return "".join(
        char for char in value if char.isalnum() or "一" <= char <= "龥"
    )


def _search_event_ngrams(value: str, size: int = 3) -> set[str]:
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def _semantic_search_event_duplicate(
    left: dict[str, Any], right: dict[str, Any]
) -> bool:
    left_categories = sorted(
        _canonical_search_category(value)
        for value in left.get("categories", [])
        if value
    )
    right_categories = sorted(
        _canonical_search_category(value)
        for value in right.get("categories", [])
        if value
    )
    if left_categories != right_categories:
        return False
    left_text = _normalized_search_event_text(left)
    right_text = _normalized_search_event_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    shorter, longer = sorted((left_text, right_text), key=len)
    if len(shorter) >= 8 and shorter in longer and len(shorter) / len(longer) >= 0.62:
        return True
    left_grams = _search_event_ngrams(left_text)
    right_grams = _search_event_ngrams(right_text)
    union = left_grams | right_grams
    return bool(union) and len(left_grams & right_grams) / len(union) >= 0.64


def _search_result_seconds(item: dict[str, Any]) -> float | None:
    """搜索结果排序/区间过滤用的秒数。

    [Q476] 必须用 `timeutil.parse_ts`：它对**不带 offset** 的写法按北京时间解释。
    原来的 `datetime.fromisoformat(ts).timestamp()` 会按**系统本地时区**解释 naive 值，
    Mac 时区改到 America/Los_Angeles 后同一行会偏移 15 小时（排序错位、区间漏结果）。
    带 offset 的写法不受影响（parse_ts 保留其 instant）。
    """
    timestamp = str(item.get("timestamp") or "")
    if not timestamp:
        return None
    parsed = parse_ts(timestamp)
    return parsed.timestamp() if parsed is not None else None


def _search_date(value: Any, label: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"{label}格式无效") from error
    return text


def _canonical_event_import_source(
    event: Mapping[str, Any],
    source_map: Mapping[tuple[str, str], str] | None = None,
) -> str:
    job_id = str(event.get("source_job_id") or "")
    file_id = str(event.get("source_file_id") or "")
    if source_map and job_id and file_id:
        mapped = source_map.get((job_id, file_id))
        if mapped:
            return mapped
    artifact = str(event.get("source_artifact") or "")
    source_kind = str(
        event.get("canonical_source_kind") or event.get("source_kind") or ""
    )
    if source_kind == "live" or "watch-sessions" in artifact:
        return "apple-watch"
    if "passport" in artifact.casefold():
        return "ai-passport-wifi"
    return "file-upload"


def _canonical_event_link(
    store_root: Path, event: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    artifact_raw = str(event.get("source_artifact") or "").strip()
    if not artifact_raw:
        return None, None
    artifact = Path(artifact_raw)
    job_ids = [str(item) for item in event.get("source_job_ids") or [] if item]
    job_id = str(event.get("source_job_id") or "") or (job_ids[0] if job_ids else "")
    if not job_id:
        return None, None
    try:
        relative = artifact.relative_to(store_root / "jobs" / job_id)
    except ValueError:
        return job_id, None
    return job_id, str(relative)


def _canonical_event_result(
    store_root: Path,
    event: Mapping[str, Any],
    transaction: Mapping[str, Any] | None,
    pending: Mapping[str, Any] | None,
    source_map: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    evidence = (
        event.get("evidence")
        if isinstance(event.get("evidence"), Mapping)
        else {}
    )
    quote = str(evidence.get("quote") or "")
    categories = [
        str(value) for value in event.get("categories") or [] if value
    ]
    job_id, artifact_path = _canonical_event_link(store_root, event)
    amount = transaction.get("amount") if transaction else None
    currency = (
        transaction.get("currency") if transaction else
        pending.get("currency") if pending else None
    )
    amount_known = isinstance(amount, (int, float)) and not isinstance(amount, bool)
    if pending:
        financial_status = "pending"
    elif transaction:
        financial_status = (
            "payment" if transaction.get("kind") in {"consumption", "transfer"}
            else "non_payment"
        )
    else:
        financial_status = "not_financial"
    expense_amount_cny = (
        float(amount)
        if amount_known and str(currency or "").upper() == "CNY"
        else None
    )
    timestamp = (
        str(event.get("occurred_at") or "")
        or str(event.get("reported_at") or "")
        or str((event.get("time") or {}).get("absolute") or "")
        or None
    )
    result = {
        "result_type": "event",
        "job_id": job_id,
        "artifact_path": artifact_path,
        "date": event.get("effective_date"),
        "timestamp": timestamp,
        "text": str(event.get("title") or "").strip(),
        "quote": quote or None,
        "source_file": str(event.get("source_file_id") or "") or None,
        "import_source": _canonical_event_import_source(event, source_map),
        "categories": categories,
        "source_type": event.get("source_type"),
        "review_status": event.get("review_status"),
        "event_id": event.get("event_id"),
        "expense_amount_cny": expense_amount_cny,
        "audio_start_offset_seconds": evidence.get("audio_start_offset_seconds"),
        "audio_end_offset_seconds": evidence.get("audio_end_offset_seconds"),
        "relative_seconds": (event.get("time") or {}).get("relative_seconds")
        if isinstance(event.get("time"), Mapping) else None,
        # Canonical additions. The legacy fields above keep the existing UI link
        # and audio-preview contract.
        "canonical_event": True,
        "canonical_event_id": event.get("canonical_event_id") or event.get("event_id"),
        "event_key": event.get("event_key"),
        "source_kind": event.get("canonical_source_kind") or event.get("source_kind"),
        "date_basis": event.get("date_basis"),
        "occurred_at": event.get("occurred_at"),
        "reported_at": event.get("reported_at"),
        "source_job_id": event.get("source_job_id"),
        "source_job_ids": list(event.get("source_job_ids") or []),
        "source_artifact": event.get("source_artifact"),
        "provenance": list(event.get("provenance") or []),
        "aliases": list(event.get("aliases") or []),
        "expense_amount": float(amount) if amount_known else None,
        "expense_currency": str(currency).upper() if currency else None,
        "expense_kind": transaction.get("kind") if transaction else None,
        "financial_status": financial_status,
        "proposed_expense_amount": pending.get("proposed_amount") if pending else None,
        "financial_policy_version": "expense-policy-v2",
    }
    return result


def _search_events_cached(store: "JobStore", scope: str, view: str) -> list[dict[str, Any]]:
    """全量事件集合（未过 dictation 过滤），按源数据指纹缓存。"""
    from life_logger.event_view import build_event_view

    signature = _expense_range_signature(store.root, "2000-01-01", "2099-12-31", scope, view)
    key = f"{store.root}|{scope}|{view}|{signature}"
    cached = _SEARCH_EVENTS_CACHE.get(key)
    if cached is not None:
        return cached
    payload = build_event_view(
        store.root, date_from="2000-01-01", date_to="2099-12-31", scope=scope, view=view
    )
    events = [dict(event) for event in payload.get("events", []) if isinstance(event, Mapping)]
    if len(_SEARCH_EVENTS_CACHE) >= _SEARCH_CACHE_MAX_ENTRIES:
        _SEARCH_EVENTS_CACHE.clear()
    _SEARCH_EVENTS_CACHE[key] = events
    return events


def _search_source_map_cached(store: "JobStore") -> dict[tuple[str, str], str]:
    """(job_id, file_id) → capture_source。

    只用 status.json 的 stat 指纹判断是否变化，**不去拿 JobStore 的锁**，
    避免搜索请求与后台写任务互相阻塞。
    """
    status_files = []
    newest = 0
    try:
        for path in store.jobs_dir.glob("job-*/status.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            status_files.append((path, stat.st_mtime_ns, stat.st_size))
            newest = max(newest, stat.st_mtime_ns)
    except OSError:
        return {}
    key = f"{store.root}|{len(status_files)}|{newest}"
    cached = _SEARCH_SOURCE_MAP_CACHE.get(key)
    if cached is not None:
        return cached

    source_map: dict[tuple[str, str], str] = {}
    for path, _mtime, _size in status_files:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(state.get("job_id") or path.parent.name)
        for file_state in state.get("files") or []:
            if not isinstance(file_state, Mapping):
                continue
            file_id = str(file_state.get("file_id") or file_state.get("sha256") or "")
            capture_source = str(file_state.get("capture_source") or "")
            if job_id and file_id and capture_source in {
                "apple-watch", "ai-passport-wifi", "file-upload",
            }:
                source_map[(job_id, file_id)] = capture_source
    if len(_SEARCH_SOURCE_MAP_CACHE) >= _SEARCH_CACHE_MAX_ENTRIES:
        _SEARCH_SOURCE_MAP_CACHE.clear()
    _SEARCH_SOURCE_MAP_CACHE[key] = source_map
    return source_map


def _canonical_event_search(
    store: "JobStore",
    *,
    text_query: str,
    result_type: str,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
    category: str | None,
    amount_min: float | None,
    amount_max: float | None,
    currency: str | None,
    import_source: str,
    limit: int,
    scope: str,
    view: str,
    event_filter,
) -> dict[str, Any]:
    if result_type not in {"all", "event", "utterance"}:
        raise ValueError("搜索类型无效")
    if import_source not in {"all", "apple-watch", "ai-passport-wifi", "file-upload"}:
        raise ValueError("导入来源无效")
    query = str(text_query or "").strip()
    date = _search_date(date, "搜索日期")
    date_from = _search_date(date_from, "开始日期")
    date_to = _search_date(date_to, "结束日期")
    if date:
        date_from = date_to = date
    if date_from and date_to and date_from > date_to:
        raise ValueError("搜索开始日期不能晚于结束日期")
    category = str(category or "").strip() or None
    valid_categories = set(ALLOWED_CATEGORIES) | set(_SEARCH_CATEGORY_ALIASES)
    if category and category not in valid_categories:
        raise ValueError("搜索分类无效")
    canonical_category = _canonical_search_category(category) if category else None

    def _amount(value: float | None, label: str) -> float | None:
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label}无效") from error
        if number < 0:
            raise ValueError(f"{label}不能小于 0")
        return number

    amount_min = _amount(amount_min, "最低金额")
    amount_max = _amount(amount_max, "最高金额")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise ValueError("最低金额不能高于最高金额")
    amount_filter = amount_min is not None or amount_max is not None
    currency_filter = str(currency or "").strip().upper() or None
    if currency_filter and not re.fullmatch(r"[A-Z]{3}", currency_filter):
        raise ValueError("币种无效")
    # Legacy amount filters were CNY-only. Keep that safe default so a 7 USD
    # payment cannot satisfy an unqualified CNY 7 filter.
    if amount_filter and not currency_filter:
        currency_filter = "CNY"
    if (
        not query
        and not any(
            (date_from, date_to, category, amount_filter, currency_filter)
        )
        and import_source == "all"
    ):
        return {
            "query": query,
            "results": [],
            "result_count": 0,
            "event_count": 0,
            "utterance_count": 0,
            "read_contract": _read_contract(scope, view),
        }

    try:
        limit_value = max(1, min(int(limit), 100))
    except (TypeError, ValueError) as error:
        raise ValueError("搜索数量无效") from error

    from life_logger.day_facts import range_expense_ledger

    # 【2026-10-19 P-9】全量扫描 + JobStore 锁都改为缓存（见上方说明）
    events = list(_search_events_cached(store, scope, view))
    # [2026-11-04 P01] 下面的 `range_expense_ledger` 无条件用到 start/end；
    # 原来只在 `if date_from or date_to` 里赋值 → 不带日期的搜索直接 UnboundLocalError。
    # 没有日期过滤时就是全域，与原来的默认值一致。
    start = date_from or "2000-01-01"
    end = date_to or "2099-12-31"
    if date_from or date_to:
        events = [
            event
            for event in events
            if start <= str(event.get("effective_date") or event.get("date") or "") <= end
        ]
    if event_filter is not None:
        events = [dict(event) for event in event_filter(events)]
    source_map: dict[tuple[str, str], str] = _search_source_map_cached(store)
    # [2026-11-04 Q809 补位] 明细页的金额同样不能只读事件轨：把本次区间内的活动轨一起喂进去，
    # 否则 11-01 这种「事件 0 条、活动有消费」的日子在明细里仍然是 0。
    ledger = range_expense_ledger(events, start, end, root=store.root)
    transaction_by_id: dict[str, Mapping[str, Any]] = {}
    for key in ("all_transactions", "transactions", "payment_transactions"):
        for item in ledger.get(key) or []:
            if not isinstance(item, Mapping):
                continue
            event_id = str(item.get("event_id") or "")
            if event_id:
                transaction_by_id.setdefault(event_id, item)
            for duplicate in item.get("duplicate_event_ids") or []:
                transaction_by_id.setdefault(str(duplicate), item)
    payment_ids = {
        str(item.get("event_id"))
        for item in ledger.get("payment_transactions") or []
        if isinstance(item, Mapping) and item.get("event_id")
    }
    pending_by_id = {
        str(item.get("event_id")): item
        for item in ledger.get("pending_transactions") or []
        if isinstance(item, Mapping) and item.get("event_id")
    }

    include_events = result_type in {"all", "event"} or bool(
        category or amount_filter or currency_filter
    )
    include_utterances = result_type in {"all", "utterance"} and not (
        category or amount_filter or currency_filter
    )
    results: list[dict[str, Any]] = []
    if include_events:
        needle = query.casefold()
        for event in events:
            categories = [
                _canonical_search_category(value)
                for value in event.get("categories") or []
                if value
            ]
            if (
                canonical_category
                and canonical_category not in categories
            ):
                continue
            if (
                import_source != "all"
                and _canonical_event_import_source(event, source_map) != import_source
            ):
                continue
            evidence = (
        event.get("evidence")
        if isinstance(event.get("evidence"), Mapping)
        else {}
    )
            haystack = " ".join(
                [
                    str(event.get("title") or ""),
                    str(evidence.get("quote") or ""),
                    *[str(value) for value in categories],
                ]
            ).casefold()
            if needle and needle not in haystack:
                continue
            event_id = str(event.get("event_id") or "")
            transaction = transaction_by_id.get(event_id)
            pending = pending_by_id.get(event_id)
            if currency_filter:
                transaction_currency = str(
                    (transaction or {}).get("currency")
                    or (pending or {}).get("currency")
                    or ""
                ).upper()
                if transaction_currency != currency_filter:
                    continue
            if amount_filter:
                if event_id not in payment_ids or not transaction:
                    continue
                numeric = transaction.get("amount")
                if not isinstance(numeric, (int, float)) or isinstance(numeric, bool):
                    continue
                if amount_min is not None and numeric < amount_min:
                    continue
                if amount_max is not None and numeric > amount_max:
                    continue
            result = _canonical_event_result(
                store.root, event, transaction, pending, source_map
            )
            result["read_contract"] = _read_contract(
                scope,
                view,
                notice=(
                    "Canonical event result from the shared read view and "
                    "shared expense policy."
                ),
            )
            results.append(result)

    if include_utterances:
        # Legacy utterance search slices the raw timestamp; widen by one day on
        # each side so a UTC/speech timestamp near local midnight is not lost.
        # The exact Asia/Shanghai speech date is filtered below.
        legacy_range_from = "2000-01-01"
        legacy_range_to = "2099-12-31"
        if date_from:
            legacy_range_from = (
                datetime.strptime(date_from, "%Y-%m-%d").date()
                - timedelta(days=1)
            ).isoformat()
        if date_to:
            legacy_range_to = (
                datetime.strptime(date_to, "%Y-%m-%d").date()
                + timedelta(days=1)
            ).isoformat()
        utterances = store.search(
            query,
            result_type="utterance",
            date_from=legacy_range_from,
            date_to=legacy_range_to,
            import_source=import_source,
            limit=limit_value,
        )
        for item in utterances:
            timestamp = str(item.get("timestamp") or "")
            local_date = None
            if timestamp:
                try:
                    local_date = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                    ).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                except (TypeError, ValueError):
                    local_date = str(item.get("date") or "") or None
            if date_from and (not local_date or local_date < date_from):
                continue
            if date_to and (not local_date or local_date > date_to):
                continue
            utterance = dict(item)
            utterance.update(
                {
                    "canonical_event": False,
                    "source_kind": "transcript",
                    "date_basis": "speech_time",
                    "date": local_date or item.get("date"),
                    "read_contract": _read_contract(
                        "diagnostic_speech",
                        "speech_time",
                        canonical=False,
                        date_basis="speech/transcript time; not event occurrence time",
                        notice=(
                            "Self-utterance evidence. Use canonical event results "
                            "for occurrence/date accounting."
                        ),
                    ),
                }
            )
            results.append(utterance)

    def _sort_key(item: Mapping[str, Any]) -> tuple[str, str]:
        return (
            str(item.get("date") or ""),
            str(item.get("timestamp") or item.get("reported_at") or ""),
        )

    results.sort(key=_sort_key, reverse=True)
    returned = results[:limit_value]
    event_count = sum(item.get("result_type") == "event" for item in results)
    utterance_count = sum(item.get("result_type") == "utterance" for item in results)
    return {
        "query": query,
        "results": returned,
        "result_count": len(results),
        "returned_count": len(returned),
        "truncated": len(results) > len(returned),
        "event_count": event_count,
        "utterance_count": utterance_count,
        "scope": scope,
        "view": view,
        "timezone": "Asia/Shanghai",
        "read_contract": _read_contract(
            scope,
            view,
            canonical=include_events,
            notice=(
                "Event results use the canonical shared view; utterance results "
                "are explicitly speech-time evidence."
            ),
        ),
        "result_kinds": {
            "event": event_count,
            "utterance": utterance_count,
        },
    }
