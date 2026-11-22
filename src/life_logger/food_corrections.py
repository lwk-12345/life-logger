"""首页饮食条目「改名字」读写、跨重跑回退匹配与本地营养复查。

设计要点：
- 不修改 `activity-timeline/jev/*.json` 原文件；用户改名独立存于 `<root>/food-renames.json`
  （schema `food-renames-v1`），采用 `file_lock` + 临时文件 + `os.replace` 原子落盘。
- 写入时同时记录 `original_name`（去除「（转写名称待核）」后的原名）、
  `raw_original_name`（原始未清洗名称）、`quote`、`quotes`、`time`、`clock`、`meal`、`source_ids`。
- 读取时优先按 `event_id`（及人工补重事件的 `source_event_id`）精确匹配；
  当 v3 重跑同一天导致 `activity_id` 或 `item_id` 变化时，按 `(原名称 + 原话 + 时间)` 找回；
  找不回时绝不静默丢弃，挂入 `nutrition_ledger["unmatched_renames"]` 供前端提示与撤销。
- 改名后查热量只走本地四层库（`allow_off=False`，与 `enter_weight` 同源的本地索引），
  绝不发起联网请求；查不到则保持「热量待定」。
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from life_logger.atomic import file_lock

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
RENAME_FILENAME = "food-renames.json"
RENAME_SCHEMA = "food-renames-v1"
RENAME_BASIS = "首页改饮食名"

_PENDING_SUFFIX_RE = re.compile(
    r"(?:\s*[（(]\s*(?:转写名称待核|名称待核|转写待核)\s*[）)])+\s*$"
)


class FoodRenameError(ValueError):
    """非法日期、条目 ID 或食物名称参数。"""


def strip_pending_suffix(name: Any) -> str:
    """去掉食物名称末尾的「（转写名称待核）」/「（名称待核）」后缀。"""
    text = str(name or "").strip()
    if not text:
        return ""
    cleaned = _PENDING_SUFFIX_RE.sub("", text).strip()
    return cleaned or text


def validate_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as error:
        raise FoodRenameError("date 必须为 YYYY-MM-DD 格式") from error
    if parsed.isoformat() != text:
        raise FoodRenameError("date 必须为 YYYY-MM-DD 格式")
    return text


def validate_food_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise FoodRenameError("name 不能为空")
    cleaned = strip_pending_suffix(raw)
    if not cleaned:
        raise FoodRenameError("name 不能为空")
    if len(cleaned) > 80:
        raise FoodRenameError("name 长度不能超过 80 个字符")
    return cleaned


def _norm_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(
        r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】]+",
        "",
        text,
    ).lower()


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


class _LocalOnlyFoodLookup:
    """只查本地库（层①品牌/配方 + 层②中国食物成分表/foodwake + 层③USDA），禁止任何联网请求。"""

    def __init__(self, base_lookup: Any, extra_brand_library: Any = None) -> None:
        self._base = base_lookup
        self._extra_brand = extra_brand_library

    def lookup(self, name: str, grams: float | None = None, **_kwargs: Any) -> Any:
        query = str(name or "").strip()
        if not query:
            return None
        if self._extra_brand is not None:
            try:
                hit = self._extra_brand.lookup_strict(query)
                if hit is None:
                    hit = self._extra_brand.lookup_weak(query)
                if hit is not None:
                    finalize = getattr(self._base, "_finalize", None)
                    return finalize(hit) if callable(finalize) else hit
            except Exception:
                pass
        try:
            return self._base.lookup(query, grams=grams, allow_off=False)
        except TypeError:
            return self._base.lookup(query, grams=grams)


def local_only_food_lookup(
    root: Path | str | None = None,
    base_lookup: Any = None,
) -> _LocalOnlyFoodLookup:
    """构造不发起任何网络请求的本地查库适配器。"""
    from life_logger.day_facts import data_root
    from life_logger.food_lookup import BrandFoodLibrary, default_food_lookup

    engine = base_lookup if base_lookup is not None else default_food_lookup()
    extra_brand = None
    if root is not None:
        try:
            resolved_root = data_root(root)
            brand_path = resolved_root / "food-data" / "brand-library.json"
            engine_dir = getattr(engine, "data_dir", None)
            if brand_path.is_file() and (
                engine_dir is None or brand_path.resolve() != (Path(engine_dir) / "brand-library.json").resolve()
            ):
                extra_brand = BrandFoodLibrary(brand_path)
        except Exception:
            extra_brand = None
    return _LocalOnlyFoodLookup(engine, extra_brand_library=extra_brand)


def load_raw_renames(root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """读取 `<data_root>/food-renames.json` 中的 decisions 字典。"""
    from life_logger.day_facts import data_root

    path = data_root(root) / RENAME_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        decisions = payload.get("decisions")
        if not isinstance(decisions, Mapping):
            return {}
        return {
            str(key): dict(val)
            for key, val in decisions.items()
            if isinstance(val, Mapping) and str(val.get("action") or "rename") == "rename"
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def extract_food_candidates(
    events: Sequence[Mapping[str, Any]] | None,
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    day: str | None = None,
) -> list[dict[str, Any]]:
    """从当天饮食领域事件（含活动层与人工补重事件）提取可匹配候选。"""
    from life_logger.day_facts import domain_events

    candidates: list[dict[str, Any]] = []
    for event in domain_events(events or [], activities, day, "food"):
        if not isinstance(event, Mapping):
            continue
        ev_id = str(event.get("event_id") or "").strip()
        if not ev_id:
            continue
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        food_data = data.get("food") or data.get("diet") or {}
        if not isinstance(food_data, Mapping):
            food_data = {}
        routine_data = data.get("routine") if isinstance(data.get("routine"), Mapping) else {}
        title = str(event.get("title") or "").strip()
        raw_name = str(food_data.get("item") or routine_data.get("item") or title or "").strip()
        clean_name = strip_pending_suffix(raw_name)
        evidence = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
        quote = str(evidence.get("quote") or "").strip()
        quotes = [q.strip() for q in quote.splitlines() if q.strip()] if quote else []
        if quote and quote not in quotes:
            quotes.insert(0, quote)

        prov = event.get("_activity_source") if isinstance(event.get("_activity_source"), Mapping) else {}
        time_block = event.get("time") if isinstance(event.get("time"), Mapping) else {}
        raw_time = str(
            time_block.get("absolute")
            or prov.get("occurred_at")
            or prov.get("spoken_at")
            or event.get("reported_at")
            or evidence.get("reported_at")
            or ""
        ).strip()
        day_str, clock_str = _shanghai_parts(raw_time)
        if not clock_str and time_block.get("clock"):
            clock_str = str(time_block.get("clock") or "").strip()
        if not clock_str and prov.get("spoken_at"):
            _, clock_str = _shanghai_parts(prov.get("spoken_at"))

        source_ids = [
            str(sid).strip()
            for sid in (prov.get("source_ids") or [])
            if str(sid or "").strip()
        ]
        source_event_id = str(food_data.get("source_event_id") or "").strip()
        meal = str(food_data.get("meal") or "").strip().lower()

        candidates.append(
            {
                "event_id": ev_id,
                "source_event_id": source_event_id,
                "raw_name": raw_name,
                "name": clean_name,
                "title": title,
                "quote": quote,
                "quotes": quotes,
                "time": raw_time,
                "clock": clock_str,
                "date": day_str or str(day or ""),
                "meal": meal,
                "source_ids": source_ids,
                "origin": "activity" if prov else "event",
            }
        )
    return candidates


def match_food_candidate_fallback(
    decision_key: str,
    decision: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    used_keys: set[str],
) -> dict[str, Any] | None:
    """当 `decision_key` 因 v3 重跑变化时，按 (原名称 + 原话 + 时间) 找回对应饮食候选。"""
    dec_date = str(decision.get("date") or "").strip()
    orig_clean = _norm_text(strip_pending_suffix(decision.get("original_name") or decision.get("raw_original_name")))
    orig_raw = _norm_text(decision.get("raw_original_name") or decision.get("original_name"))
    target_name = _norm_text(decision.get("name"))
    dec_names = {n for n in (orig_clean, orig_raw) if n}

    dec_quotes = {
        _norm_text(q)
        for q in ([decision.get("quote")] + list(decision.get("quotes") or []))
        if _norm_text(q)
    }
    dec_time = str(decision.get("time") or "").strip()
    _, dec_time_clock = _shanghai_parts(dec_time)
    dec_clock = str(decision.get("clock") or dec_time_clock or "").strip()
    dec_source_ids = {
        str(sid).strip()
        for sid in (decision.get("source_ids") or [])
        if str(sid or "").strip()
    }
    dec_meal = str(decision.get("meal") or "").strip().lower()

    best_cand: dict[str, Any] | None = None
    best_score = -1

    for cand in candidates:
        ev_id = str(cand.get("event_id") or "").strip()
        if not ev_id or ev_id in used_keys:
            continue
        cand_date = str(cand.get("date") or "").strip()
        if dec_date and cand_date and dec_date != cand_date:
            continue

        cand_clean = _norm_text(cand.get("name"))
        cand_raw = _norm_text(cand.get("raw_name"))
        cand_title = _norm_text(cand.get("title"))
        cand_names = {n for n in (cand_clean, cand_raw) if n}

        name_match = False
        if dec_names and cand_names:
            if dec_names & cand_names:
                name_match = True
            elif any(
                len(dn) >= 2 and len(cn) >= 2 and (dn in cn or cn in dn)
                for dn in dec_names
                for cn in cand_names
            ):
                name_match = True
            elif cand_title and any(len(dn) >= 2 and dn in cand_title for dn in dec_names):
                name_match = True
        elif target_name and target_name in cand_names:
            name_match = True

        # 同一句录音可能包含多项食物（如「吃了煮了米饭，吃了热力宝」），原名称必须对得上才能防止误绑到同句的米饭
        if dec_names and not name_match:
            continue

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

        cand_source_ids = {
            str(sid).strip()
            for sid in (cand.get("source_ids") or [])
            if str(sid or "").strip()
        }
        source_id_match = bool(dec_source_ids and cand_source_ids and (dec_source_ids & cand_source_ids))
        meal_match = bool(dec_meal and cand.get("meal") and dec_meal == str(cand.get("meal")).lower())

        context_checks = [
            flag
            for present, flag in (
                (bool(dec_quotes), quote_match),
                (bool(dec_time or dec_clock), time_match),
                (bool(dec_source_ids), source_id_match),
            )
            if present
        ]
        if context_checks and not any(context_checks):
            continue

        score = (
            (5 if name_match else 0)
            + (4 if quote_match else 0)
            + (3 if source_id_match else 0)
            + (2 if exact_time_match else (1 if clock_match else 0))
            + (1 if meal_match else 0)
        )
        if score > best_score:
            best_score = score
            best_cand = dict(cand)

    return best_cand


def resolve_food_renames(
    raw_decisions: Mapping[str, Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]] | None,
    activities: Sequence[Mapping[str, Any]] | None,
    *,
    day: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """解析当天饮食改名决策，支持 v3 重跑后的回退找回。

    返回 `(matched_by_event_id, unmatched_renames)`：
      - `matched_by_event_id`: `{current_event_id: rename_meta}`（同时覆盖被人工补重关联的 `manual_*` 条目）
      - `unmatched_renames`: 当天未能匹配上任何候选的改名记录
    """
    matched_by_event_id: dict[str, dict[str, Any]] = {}
    unmatched_renames: list[dict[str, Any]] = []
    if not raw_decisions:
        return matched_by_event_id, unmatched_renames

    candidates = extract_food_candidates(events, activities, day=day)
    candidate_by_id = {str(c["event_id"]): c for c in candidates if c.get("event_id")}
    # source_event_id -> [manual_event_id, ...]
    manual_by_source_id: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        src_id = str(cand.get("source_event_id") or "").strip()
        if src_id:
            manual_by_source_id.setdefault(src_id, []).append(cand)

    used_candidate_ids: set[str] = set()

    def _in_day(dec_key: str, dec_val: Mapping[str, Any]) -> bool:
        if not day:
            return True
        dec_date = str(dec_val.get("date") or "").strip()
        if dec_date:
            return dec_date == day
        return dec_key.startswith(f"activity:{day}:")

    def _build_meta(
        dec_key: str,
        dec_val: Mapping[str, Any],
        cand: Mapping[str, Any],
        *,
        matched_by: str,
    ) -> dict[str, Any]:
        new_name = strip_pending_suffix(dec_val.get("name"))
        raw_orig = str(
            dec_val.get("raw_original_name")
            or dec_val.get("original_name")
            or cand.get("raw_name")
            or cand.get("name")
            or ""
        ).strip()
        clean_orig = strip_pending_suffix(
            dec_val.get("original_name")
            or raw_orig
            or cand.get("name")
            or ""
        )
        return {
            "rename_key": dec_key,
            "current_event_id": str(cand.get("event_id") or dec_key),
            "name": new_name,
            "original_name": clean_orig,
            "raw_original_name": raw_orig,
            "quote": str(dec_val.get("quote") or cand.get("quote") or ""),
            "quotes": list(dec_val.get("quotes") or cand.get("quotes") or []),
            "time": str(dec_val.get("time") or cand.get("time") or ""),
            "clock": str(dec_val.get("clock") or cand.get("clock") or ""),
            "date": str(dec_val.get("date") or cand.get("date") or day or ""),
            "meal": str(dec_val.get("meal") or cand.get("meal") or ""),
            "decided_at": str(dec_val.get("decided_at") or ""),
            "basis": str(dec_val.get("basis") or RENAME_BASIS),
            "matched_by": matched_by,
        }

    pending_fallback: list[tuple[str, Mapping[str, Any]]] = []

    # 第一遍：精确 event_id 或 manual source_event_id 命中
    for dec_key, dec_val in raw_decisions.items():
        if not isinstance(dec_val, Mapping) or not _in_day(dec_key, dec_val):
            continue
        new_name = strip_pending_suffix(dec_val.get("name"))
        if not new_name:
            continue
        cand = candidate_by_id.get(dec_key)
        linked_manuals = manual_by_source_id.get(dec_key, [])
        if cand is not None or linked_manuals:
            primary_cand = cand or linked_manuals[0]
            meta = _build_meta(dec_key, dec_val, primary_cand, matched_by="event_id")
            if cand is not None:
                used_candidate_ids.add(dec_key)
                matched_by_event_id[dec_key] = meta
            for m_cand in linked_manuals:
                m_id = str(m_cand["event_id"])
                used_candidate_ids.add(m_id)
                matched_by_event_id[m_id] = {**meta, "current_event_id": m_id}
            continue
        pending_fallback.append((dec_key, dec_val))

    # 第二遍：按 (原名称 + 原话 + 时间) 回退找回
    for dec_key, dec_val in pending_fallback:
        matched_cand = match_food_candidate_fallback(
            dec_key, dec_val, candidates, used_keys=used_candidate_ids
        )
        if matched_cand is not None:
            cand_id = str(matched_cand["event_id"])
            used_candidate_ids.add(cand_id)
            meta = _build_meta(dec_key, dec_val, matched_cand, matched_by="fallback")
            matched_by_event_id[cand_id] = meta
            for m_cand in manual_by_source_id.get(cand_id, []):
                m_id = str(m_cand["event_id"])
                used_candidate_ids.add(m_id)
                matched_by_event_id[m_id] = {**meta, "current_event_id": m_id}
        else:
            unmatched_renames.append(format_unmatched_rename(dec_key, dec_val, default_date=day or ""))

    return matched_by_event_id, unmatched_renames


def format_unmatched_rename(
    dec_key: str,
    dec_val: Mapping[str, Any],
    *,
    default_date: str = "",
) -> dict[str, Any]:
    raw_time = str(dec_val.get("time") or "").strip()
    _, time_clock = _shanghai_parts(raw_time)
    clock = str(dec_val.get("clock") or time_clock or "").strip()
    new_name = strip_pending_suffix(dec_val.get("name"))
    raw_orig = str(dec_val.get("raw_original_name") or dec_val.get("original_name") or "").strip()
    clean_orig = strip_pending_suffix(dec_val.get("original_name") or raw_orig)
    return {
        "event_id": dec_key,
        "rename_key": dec_key,
        "date": str(dec_val.get("date") or default_date),
        "name": new_name,
        "original_name": clean_orig,
        "raw_original_name": raw_orig,
        "quote": str(dec_val.get("quote") or "").strip(),
        "quotes": list(dec_val.get("quotes") or []),
        "time": raw_time,
        "clock": clock,
        "meal": str(dec_val.get("meal") or ""),
        "decided_at": str(dec_val.get("decided_at") or ""),
        "basis": str(dec_val.get("basis") or RENAME_BASIS),
    }


def _find_day_food_candidates_for_root(root: Path, date: str) -> list[dict[str, Any]]:
    from life_logger.day_facts import (
        activities_for_day,
        data_root,
        filter_events_for_activity_cutover,
    )

    resolved_root = data_root(root)
    activities = activities_for_day(date, root=resolved_root)
    events: list[Mapping[str, Any]] = []
    try:
        from life_logger.event_view import build_event_view

        payload = build_event_view(resolved_root, date_from=date, date_to=date, use_cache=False)
        raw_events = [e for e in (payload.get("events") or []) if isinstance(e, Mapping)]
        events = filter_events_for_activity_cutover(raw_events, day=date, activities=activities)
    except Exception:
        events = []
    return extract_food_candidates(events, activities, day=date)


def rename_food(
    root: Path | str,
    *,
    date: str,
    event_id: str,
    name: Any,
) -> dict[str, Any]:
    """在 `<data_root>/food-renames.json` 记录用户改名（原子落盘 + 清读缓存）。"""
    from life_logger.day_facts import data_root

    resolved_root = data_root(root)
    valid_date = validate_date(date)
    clean_id = str(event_id or "").strip()
    if not clean_id:
        raise FoodRenameError("event_id 不能为空")
    valid_name = validate_food_name(name)

    candidates = _find_day_food_candidates_for_root(resolved_root, valid_date)
    candidate_by_id = {str(c["event_id"]): c for c in candidates if c.get("event_id")}
    cand = candidate_by_id.get(clean_id)

    rename_path = resolved_root / RENAME_FILENAME
    rename_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(tz=SHANGHAI_TZ).isoformat()

    with file_lock(rename_path):
        try:
            payload = json.loads(rename_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {"schema": RENAME_SCHEMA, "decisions": {}}
        except (OSError, json.JSONDecodeError):
            payload = {"schema": RENAME_SCHEMA, "decisions": {}}
        payload.setdefault("schema", RENAME_SCHEMA)
        decisions = payload.setdefault("decisions", {})
        if not isinstance(decisions, dict):
            decisions = {}
            payload["decisions"] = decisions

        # 若点的是人工补重后的 manual_* 事件，优先归口到其原始 source_event_id（如果已有改名或原活动存在）
        source_ev_id = str(cand.get("source_event_id") or "").strip() if cand is not None else ""
        if cand is not None and source_ev_id and source_ev_id in candidate_by_id:
            base_cand = candidate_by_id[source_ev_id]
        else:
            base_cand = cand

        existing = decisions.get(clean_id) if isinstance(decisions.get(clean_id), Mapping) else None
        replaced_key: str | None = None
        if existing is None and source_ev_id and isinstance(decisions.get(source_ev_id), Mapping):
            existing = decisions.get(source_ev_id)
            replaced_key = source_ev_id

        if existing is None and base_cand is not None:
            for old_k, old_v in list(decisions.items()):
                if not isinstance(old_v, Mapping) or str(old_v.get("date") or "") != valid_date:
                    continue
                if old_k not in candidate_by_id:
                    m = match_food_candidate_fallback(old_k, old_v, [base_cand], used_keys=set())
                    if m is not None:
                        existing = old_v
                        replaced_key = old_k
                        break

        if base_cand is None and existing is None:
            raise FoodRenameError(f"未找到 {valid_date} 对应饮食条目: {clean_id}")

        raw_orig_name = str(
            (existing.get("raw_original_name") if existing is not None else None)
            or (existing.get("original_name") if existing is not None else None)
            or (base_cand.get("raw_name") if base_cand is not None else None)
            or (cand.get("raw_name") if cand is not None else None)
            or ""
        ).strip()
        clean_orig_name = strip_pending_suffix(
            (existing.get("original_name") if existing is not None else None)
            or raw_orig_name
        )
        resolved_quote = str(
            (existing.get("quote") if existing is not None else None)
            or (base_cand.get("quote") if base_cand is not None else None)
            or (cand.get("quote") if cand is not None else None)
            or ""
        ).strip()
        resolved_quotes = list(
            (existing.get("quotes") if existing is not None and existing.get("quotes") else None)
            or (base_cand.get("quotes") if base_cand is not None and base_cand.get("quotes") else None)
            or ([resolved_quote] if resolved_quote else [])
        )
        resolved_time = str(
            (existing.get("time") if existing is not None else None)
            or (base_cand.get("time") if base_cand is not None else None)
            or (cand.get("time") if cand is not None else None)
            or ""
        ).strip()
        _, time_clock = _shanghai_parts(resolved_time)
        resolved_clock = str(
            (existing.get("clock") if existing is not None else None)
            or (base_cand.get("clock") if base_cand is not None else None)
            or time_clock
            or ""
        ).strip()
        resolved_meal = str(
            (existing.get("meal") if existing is not None else None)
            or (base_cand.get("meal") if base_cand is not None else None)
            or ""
        ).strip()
        resolved_source_ids = list(
            (existing.get("source_ids") if existing is not None and existing.get("source_ids") else None)
            or (base_cand.get("source_ids") if base_cand is not None and base_cand.get("source_ids") else None)
            or []
        )

        target_key = replaced_key or (source_ev_id if source_ev_id else clean_id)
        decision_entry: dict[str, Any] = {
            "date": valid_date,
            "event_id": target_key,
            "action": "rename",
            "name": valid_name,
            "original_name": clean_orig_name,
            "raw_original_name": raw_orig_name,
            "quote": resolved_quote,
            "quotes": resolved_quotes,
            "time": resolved_time,
            "clock": resolved_clock,
            "meal": resolved_meal,
            "source_ids": resolved_source_ids,
            "decided_by": "user",
            "decided_at": now_iso,
            "basis": RENAME_BASIS,
        }
        decisions[target_key] = decision_entry
        _atomic_write_json(rename_path, payload)

    _invalidate_all_caches(resolved_root, "rename_food")
    ledger = _current_day_nutrition_ledger(resolved_root, valid_date)
    return {
        "ok": True,
        "date": valid_date,
        "event_id": target_key,
        "name": valid_name,
        "decision": decision_entry,
        "unmatched_renames": ledger.get("unmatched_renames") or [],
        "nutrition_ledger": ledger,
    }


def undo_food_rename(
    root: Path | str,
    *,
    date: str,
    event_id: str,
) -> dict[str, Any]:
    """撤销 `<data_root>/food-renames.json` 中的一笔饮食改名（原子落盘 + 清读缓存）。"""
    from life_logger.day_facts import data_root

    resolved_root = data_root(root)
    valid_date = validate_date(date)
    clean_id = str(event_id or "").strip()
    if not clean_id:
        raise FoodRenameError("event_id 不能为空")

    rename_path = resolved_root / RENAME_FILENAME
    removed_key = clean_id
    undone = False

    if rename_path.exists():
        candidates = _find_day_food_candidates_for_root(resolved_root, valid_date)
        candidate_by_id = {str(c["event_id"]): c for c in candidates if c.get("event_id")}
        cand = candidate_by_id.get(clean_id)
        source_ev_id = str(cand.get("source_event_id") or "").strip() if cand is not None else ""

        with file_lock(rename_path):
            try:
                payload = json.loads(rename_path.read_text(encoding="utf-8"))
                decisions = payload.get("decisions") if isinstance(payload, dict) else None
            except (OSError, json.JSONDecodeError):
                payload = None
                decisions = None

            if isinstance(payload, dict) and isinstance(decisions, dict):
                if clean_id in decisions:
                    decisions.pop(clean_id, None)
                    undone = True
                elif source_ev_id and source_ev_id in decisions:
                    decisions.pop(source_ev_id, None)
                    removed_key = source_ev_id
                    undone = True
                elif cand is not None:
                    base_cand = candidate_by_id.get(source_ev_id) if source_ev_id in candidate_by_id else cand
                    for old_k, old_v in list(decisions.items()):
                        if not isinstance(old_v, Mapping) or str(old_v.get("date") or "") != valid_date:
                            continue
                        if old_k not in candidate_by_id:
                            m = match_food_candidate_fallback(old_k, old_v, [base_cand], used_keys=set())
                            if m is not None:
                                decisions.pop(old_k, None)
                                removed_key = old_k
                                undone = True
                                break
                if undone:
                    _atomic_write_json(rename_path, payload)

    _invalidate_all_caches(resolved_root, "undo_food_rename")
    ledger = _current_day_nutrition_ledger(resolved_root, valid_date)
    return {
        "ok": True,
        "date": valid_date,
        "event_id": removed_key,
        "undone": undone,
        "unmatched_renames": ledger.get("unmatched_renames") or [],
        "nutrition_ledger": ledger,
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


def _current_day_nutrition_ledger(root: Path, date: str) -> dict[str, Any]:
    from life_logger.day_facts import domain_ledgers

    events: list[Mapping[str, Any]] = []
    try:
        from life_logger.event_view import build_event_view

        payload = build_event_view(root, date_from=date, date_to=date, use_cache=False)
        events = [e for e in (payload.get("events") or []) if isinstance(e, Mapping)]
    except Exception:
        events = []
    return domain_ledgers(events, date, root=root, only={"nutrition"})["nutrition"]
