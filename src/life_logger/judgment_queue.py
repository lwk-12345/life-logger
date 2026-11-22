"""“需要我判断”的用途视图：每条写明为什么问、答完改进什么，答案写进数据飞轮。

只读投影 pending_review，不改抽取产物。真正会改数据的只有用户明确点的两种操作：
账目或待办入账（写一条人工事件），以及恢复历史上被丢的人工记录。都可以撤销。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from life_logger.atomic import file_lock
from life_logger.day_facts import activities_for_day, load_ledger_review
from life_logger.flywheel import get_flywheel_root, record_dismissed_event
from life_logger.natural_correction import parse_spoken_amount
from life_logger.pending_review import (
    GROUP_NEEDS_REVIEW,
    _collect_pending_items,
    load_review_states,
)
from life_logger.review_questions import (
    answer_review_question,
    review_question_states,
    undo_review_question,
)

STATE_FILENAME = "judgment-queue-state.json"
RECENT_DAYS = 7
ITEM_LIMIT = 30
CACHE_SECONDS = 30.0
_SHANGHAI = timezone(timedelta(hours=8))

VOICEPRINT, SCENE, MONEY, TODO, CALORIES = "voiceprint", "scene", "money", "todo", "calories"

PURPOSES: tuple[dict[str, str], ...] = (
    {
        "key": MONEY,
        "label": "账目",
        "why": "这句话提到了钱，但金额或付款没有说清。",
        "effect": "确认后入账，账本更完整。",
    },
    {
        "key": TODO,
        "label": "待办",
        "why": "这句话像是要做的事，模型没敢记成事件。",
        "effect": "确认后进任务看板；选否会记下来，减少同类误判。",
    },
    {
        "key": VOICEPRINT,
        "label": "声纹",
        "why": "系统分不清这句话是不是你说的。",
        "effect": "存为声纹样本。目前只积累，还没有用来自动更新声纹模型。",
    },
    {
        "key": SCENE,
        "label": "现场或媒体",
        "why": "模型拿不准这件事是真实发生的，还是视频里的内容。",
        "effect": "选媒体后，以后抽取时会提示模型避开相似内容。",
    },
    {
        "key": CALORIES,
        "label": "热量待定",
        "why": "这条食物没说重量，按常规份量算出的热量只能当候选。",
        "effect": "采用候选或补上重量后进当天饮食合计；不算则只记下你的选择。",
    },
)

_REASON_PURPOSE = {
    "amount_uncertain": MONEY,
    "amount_missing": MONEY,
    "amount_not_grounded": MONEY,
    "amount_evidence_not_verbatim": MONEY,
    "transaction_evidence_not_verbatim": MONEY,
    "transaction_not_completed": MONEY,
    "model_no_event_but_directive": TODO,
    "unknown_speaker": VOICEPRINT,
    "non_self_review": VOICEPRINT,
    # [2026-11-07] 候选热量：条目不是来自 pending_review，而是来自当天 nutrition_ledger
    # 的 `unweighed_items`（2 号窗提供）。reason 只是为了让 purpose_for_reason 能分类。
    "calories_unweighed": CALORIES,
}

_OPTIONS: dict[str, list[dict[str, str]]] = {
    MONEY: [
        {"op": "enter_expense", "label": "入账"},
        {"op": "media", "label": "不是我的消费"},
        {"op": "uncertain", "label": "不确定"},
    ],
    TODO: [
        {"op": "enter_todo", "label": "加入待办"},
        {"op": "query", "label": "不是待办"},
        {"op": "uncertain", "label": "不确定"},
    ],
    VOICEPRINT: [
        {"op": "personal", "label": "是我说的"},
        {"op": "media", "label": "不是我"},
        {"op": "uncertain", "label": "不确定"},
    ],
    SCENE: [
        {"op": "personal", "label": "真实发生"},
        {"op": "media", "label": "是媒体内容"},
        {"op": "uncertain", "label": "不确定"},
    ],
    CALORIES: [
        {"op": "adopt_candidate", "label": "按常规份量算"},
        {"op": "enter_weight", "label": "我补重量"},
        {"op": "skip", "label": "不算"},
    ],
}

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def purpose_for_reason(reason: Any) -> str:
    return _REASON_PURPOSE.get(str(reason or ""), SCENE)

# ── 候选热量（2026-11-07）────────────────────────────────────────────────────
#
# 用户决定：**不知重量就不算热量**；按常规份量算出的值只能当「候选」，
# 晚上复盘时由用户决定采不采用。所以这里不产生新的事实，只是把台账里
# `nutrition_ledger.unweighed_items` 变成可回答的条目：
#   采用(adopt_candidate) / 补重量(enter_weight) → 写一条人工饮食事件（可撤销）
#   不算(skip)                                    → 只记回答，不写事件
#
# 数据由 2 号窗在 `build_nutrition_ledger()` 里提供；交付前这些字段不存在，
# 本模块必须**安静地返回 0 条**，不能报错（见 calorie_items 的 try/except 与类型判断）。

CALORIE_SOURCE_PREFIX = "cal|"


def _clock(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"T(\d{2}:\d{2})", text)
    return match.group(1) if match else ""


def calorie_source_id(day: str, event_id: str) -> str:
    return f"{CALORIE_SOURCE_PREFIX}{day}|{event_id}"


def parse_calorie_source_id(source_id: str) -> tuple[str, str]:
    """`cal|<day>|<event_id>` → (day, event_id)；不是这种形状就返回 ("", "")。"""
    text = str(source_id or "")
    if not text.startswith(CALORIE_SOURCE_PREFIX):
        return "", ""
    day, _, event_id = text[len(CALORIE_SOURCE_PREFIX):].partition("|")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "") or not event_id:
        return "", ""
    return day, event_id


_ledger_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def day_nutrition_ledger(root: Path | str, day: str) -> dict[str, Any]:
    """当天营养台账（只读投影）。读不出来就返回 {} —— 绝不让队列因此打不开。

    带 30 秒缓存：`build_queue()` 每次刷新都会问一次，台账要读 activity-timeline + 食物库，
    不缓存会把「需要我判断」的打开变慢。`invalidate_cache()` 会一并清掉。
    """
    key = (str(root), str(day))
    cached = _ledger_cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        from life_logger.day_facts import domain_ledgers

        ledger = domain_ledgers([], day, root=Path(root))["nutrition"]
    except Exception:  # noqa: BLE001 - 读层失败不该拖垮「需要我判断」
        ledger = {}
    value = ledger if isinstance(ledger, dict) else {}
    _ledger_cache[key] = (time.monotonic(), value)
    return value


def calorie_items(root: Path | str, day: str) -> list[dict[str, Any]]:
    """把 `nutrition_ledger.unweighed_items` 变成「热量待定」条目。

    只取 `portion_estimate_candidate.calories_kcal` 有值的（没有候选就没有可采纳的东西）。
    字段缺失 / 结构不符 → []，不抛。
    """
    ledger = day_nutrition_ledger(root, day)
    raw = ledger.get("unweighed_items")
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        candidate = row.get("portion_estimate_candidate")
        if not isinstance(candidate, Mapping):
            continue
        try:
            kcal = float(candidate.get("calories_kcal"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(kcal) or kcal <= 0:
            continue
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            continue
        try:
            grams = float(candidate.get("grams"))
        except (TypeError, ValueError):
            grams = None
        nutrients = candidate.get("nutrients")
        spoken_at = str(row.get("spoken_at") or "")
        items.append({
            "source_id": calorie_source_id(day, event_id),
            "purpose": CALORIES,
            "reason": "calories_unweighed",
            "reason_label": "没说重量",
            "quote": str(row.get("quote") or ""),
            "date": spoken_at[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", spoken_at) else day,
            "time": _clock(spoken_at),
            "recorded_at": spoken_at or None,
            "event_id": event_id,
            "item": str(row.get("item") or "").strip() or "未命名食物",
            "meal": str(row.get("meal") or ""),
            "candidate": {
                "grams": grams,
                "calories_kcal": kcal,
                "basis": str(candidate.get("basis") or row.get("estimate_basis") or ""),
                "nutrients": dict(nutrients) if isinstance(nutrients, Mapping) else {},
            },
            "options": _OPTIONS[CALORIES],
            "suggested_amount": None,
        })
    return items


def find_calorie_item(root: Path | str, source_id: str, *, today: str | None = None) -> dict[str, Any] | None:
    """按 source_id 找回某条候选（回答时用；台账读不出来就当找不到）。

    除了有常规份量候选的 ``calorie_items``，也允许定位当天台账里无候选热量或已计入的饮食条目，
    供时间轴饮食卡片内联「补重量 / 填千卡 / 不算」直接操作。
    """
    day, event_id = parse_calorie_source_id(source_id)
    if not day or not event_id:
        return None
    for item in calorie_items(root, day):
        if item.get("event_id") == event_id:
            return item
    ledger = day_nutrition_ledger(root, day)
    if not isinstance(ledger, Mapping):
        return None
    pools: list[ Any ] = []
    for key in ("unweighed_items", "unknown_nutrition_items"):
        seq = ledger.get(key)
        if isinstance(seq, list):
            pools.extend(seq)
    meals = ledger.get("meals")
    if isinstance(meals, Mapping):
        for seq in meals.values():
            if isinstance(seq, list):
                pools.extend(seq)
    for row in pools:
        if not isinstance(row, Mapping) or str(row.get("event_id") or "").strip() != event_id:
            continue
        cand_raw = row.get("portion_estimate_candidate")
        cand = dict(cand_raw) if isinstance(cand_raw, Mapping) else {
            "grams": row.get("weight_g"),
            "calories_kcal": row.get("calories_kcal"),
            "basis": row.get("estimate_basis") or "",
            "nutrients": dict(row.get("nutrients") or {}) if isinstance(row.get("nutrients"), Mapping) else {},
        }
        spoken_at = str(row.get("spoken_at") or row.get("reported_at") or "")
        return {
            "source_id": calorie_source_id(day, event_id),
            "purpose": CALORIES,
            "reason": "calories_unweighed",
            "reason_label": "没说重量",
            "quote": str(row.get("quote") or ""),
            "date": spoken_at[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", spoken_at) else day,
            "time": _clock(spoken_at),
            "recorded_at": spoken_at or None,
            "event_id": event_id,
            "item": str(row.get("item") or "").strip() or "未命名食物",
            "meal": str(row.get("meal") or ""),
            "candidate": cand,
            "options": _OPTIONS[CALORIES],
            "suggested_amount": None,
        }
    return None


def skipped_calorie_event_ids(root: Path | str, day: str) -> set[str]:
    """返回当天已在「热量待定」里被用户点过「不算」(skip) 的原始 event_id 集合。"""
    if not day:
        return set()
    prefix = f"{CALORIE_SOURCE_PREFIX}{day}|%"
    try:
        from life_logger.review_questions import _db

        conn = _db(Path(root), create=False)
        if conn is None:
            return set()
        try:
            rows = conn.execute(
                "SELECT source_id, reason FROM question_state WHERE source_id LIKE ? AND state IN ('answered', 'muted')",
                (prefix,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return set()
    out: set[str] = set()
    for sid, reason in rows:
        if "skip" not in str(reason or ""):
            continue
        _, ev_id = parse_calorie_source_id(str(sid))
        if ev_id:
            out.add(ev_id)
    return out


def invalidate_cache() -> None:
    _ledger_cache.clear()
    with _cache_lock:
        _cache.clear()


def _resolved_media_or_work_evidence(root: Path, days: set[str]) -> tuple[set[str], dict[str, list[str]]]:
    """收集已在 activity-timeline 中明确归类为外放媒体或编程工作的 source_id 与原话片段。"""
    sids: set[str] = set()
    quotes_by_day: dict[str, list[str]] = {}
    tl_dir = root / "activity-timeline" / "jev"
    if not tl_dir.is_dir():
        return sids, quotes_by_day
    for day in days:
        if not day:
            continue
        day_file = tl_dir / f"{day}.json"
        if not day_file.is_file():
            continue
        try:
            payload = json.loads(day_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        day_quotes: list[str] = []
        for act in payload.get("activities") or []:
            if not isinstance(act, Mapping):
                continue
            lc = str(act.get("lifecycle") or "")
            cat = str(act.get("category") or "")
            title = str(act.get("title") or "")
            summary = str(act.get("summary") or "")
            meta_blob = f"{title} {summary}"
            if not (
                lc == "media"
                or cat in {"媒体内容", "浏览·视频", "媒体·科技资讯", "工作·项目", "编程·项目优化"}
                or any(k in meta_blob for k in ("播放", "背景", "媒体", "剧情", "影视", "游戏解说", "外放", "口播"))
            ):
                continue
            for it in act.get("items") or []:
                if not isinstance(it, Mapping):
                    continue
                for ev in it.get("evidence") or []:
                    if not isinstance(ev, Mapping):
                        continue
                    sid = str(ev.get("source_id") or "").strip()
                    if sid:
                        sids.add(sid)
                    q = str(ev.get("quote") or "").strip()
                    if len(q) >= 4:
                        day_quotes.append(q)
        if day_quotes:
            quotes_by_day[day] = day_quotes
    return sids, quotes_by_day


def _open_items(root: Path) -> list[dict[str, Any]]:
    key = str(root)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        records = load_review_states(root)["records"]
        items, _, _ = _collect_pending_items(root, records)
        from collections import Counter
        from life_logger.activity_prompt_v3 import _A123_TOOL, is_agent_or_work_todo_text

        candidates = [
            dict(item) for item in items.values()
            if item.get("group") == GROUP_NEEDS_REVIEW
            and item.get("state") == "open"
        ]
        days = {str(it.get("date") or "") for it in candidates if it.get("date")}
        resolved_sids, resolved_quotes = _resolved_media_or_work_evidence(root, days)
        day_quote_counts = Counter(
            (str(it.get("date") or ""), str(it.get("quote") or "").strip())
            for it in candidates
            if len(str(it.get("quote") or "").strip()) >= 6
        )
        repeated_loop_quotes = {
            dq for dq, cnt in day_quote_counts.items() if cnt >= 3
        }

        rows: list[dict[str, Any]] = []
        for item in candidates:
            purpose = purpose_for_reason(item.get("reason"))
            quote = str(item.get("quote") or "").strip()
            day = str(item.get("date") or "")
            sid = str(item.get("source_id") or "").strip()
            if purpose == TODO and is_agent_or_work_todo_text(quote):
                continue
            if purpose in {SCENE, VOICEPRINT, TODO}:
                if (day, quote) in repeated_loop_quotes:
                    continue
                if is_agent_or_work_todo_text(quote) or bool(_A123_TOOL.search(quote)):
                    continue
                if sid and sid in resolved_sids:
                    continue
                if len(quote) >= 4 and any(
                    mq in quote or quote in mq for mq in resolved_quotes.get(day, ())
                ):
                    continue
            rows.append(item)
        _cache[key] = (time.monotonic(), rows)
        return rows


def _read_state(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"manual": {}}
    return payload if isinstance(payload, dict) and isinstance(payload.get("manual"), dict) else {"manual": {}}


def _write_state(root: Path, state: Mapping[str, Any]) -> None:
    target = root / STATE_FILENAME
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def lost_manual_events(root: Path, before: str) -> list[dict[str, Any]]:
    """历史上没说时间、因此进不了当天的人工记录。不自动恢复，交给用户判断。"""
    decided = _read_state(root)["manual"]
    found: list[dict[str, Any]] = []
    for path in sorted(root.glob("jobs/job-*/manual-events.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = payload.get("events", []) if isinstance(payload, Mapping) else payload
        for event in entries if isinstance(entries, list) else []:
            artifact = str(event.get("artifact_path") or "")
            match = re.match(r"(\d{4}-\d{2}-\d{2})/events\.json$", artifact)
            when = event.get("time") if isinstance(event.get("time"), Mapping) else {}
            event_id = str(event.get("event_id") or "")
            if not match or match.group(1) >= before or when.get("absolute") or event_id in decided:
                continue
            found.append({
                "event_id": event_id,
                "job_id": path.parent.name,
                "date": match.group(1),
                "title": str(event.get("title") or ""),
                "amount": ((event.get("data") or {}).get("expense") or {}).get("amount"),
                "quote": str((event.get("evidence") or {}).get("quote") or ""),
            })
    return found


def decide_lost_manual(root: Path, *, job_id: str, event_id: str, action: str) -> dict[str, Any]:
    """restore：把这条放回原来的日期；dismiss：不恢复。撤销用 undo。"""
    if action not in {"restore", "dismiss", "undo"} or not re.fullmatch(r"job-[\w-]+", job_id):
        raise ValueError("参数无效")
    path = root / "jobs" / job_id / "manual-events.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("events", []) if isinstance(payload, dict) else payload
    event = next((item for item in entries if item.get("event_id") == event_id), None)
    if event is None:
        raise ValueError("记录不存在")
    state = _read_state(root)
    if action == "undo":
        event.pop("restored_date", None)
        state["manual"].pop(event_id, None)
    else:
        if action == "restore":
            event["restored_date"] = str(event.get("artifact_path") or "")[:10]
        state["manual"][event_id] = {"action": action, "at": time.time()}
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    _write_state(root, state)
    return {"ok": True, "event_id": event_id, "action": action}


def _manual_expenses(root: Path, days: set[str]) -> list[dict[str, Any]]:
    found = []
    for path in sorted(root.glob("jobs/job-*/manual-events.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = payload.get("events", []) if isinstance(payload, Mapping) else payload
        for event in entries if isinstance(entries, list) else []:
            amount = ((event.get("data") or {}).get("expense") or {}).get("amount")
            day = str(event.get("artifact_path") or "")[:10]
            value = _as_amount(amount)
            if value is not None and day in days:
                found.append({"event_id": str(event.get("event_id") or ""), "date": day,
                              "title": str(event.get("title") or ""), "amount": value})
    return found


def _as_amount(value: Any) -> float | None:
    """能转成数字的金额才算；"一百八十二亿" 这类文字金额（多为外放新闻）返回 None，不能让整个队列 400。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def possible_duplicates(root: Path, days: set[str]) -> list[dict[str, Any]]:
    """手动记账和活动里同一天同金额的条目。账本不自动去重（同额可能真是两笔），交给用户。"""
    review = load_ledger_review()
    decided = _read_state(root).get("duplicates", {})
    found = []
    for manual in _manual_expenses(root, days):
        if manual["event_id"] in review or manual["event_id"] in decided:
            continue
        for activity in activities_for_day(manual["date"], root=root) or []:
            match = next((item for item in activity.get("items", [])
                          if item.get("fact_type") == "expense"
                          and _as_amount(item.get("amount")) == manual["amount"]), None)
            if match:
                found.append({**manual, "matched_title": str(match.get("action") or ""),
                              "matched_time": str(match.get("spoken_at") or "")[11:16]})
                break
    return found


def decide_duplicate(root: Path, *, event_id: str, action: str, date: str = "") -> dict[str, Any]:
    """same：排除这条手动记录；separate：确认是两笔。undo 撤销。"""
    if action not in {"same", "separate", "undo"} or not event_id:
        raise ValueError("参数无效")
    state = _read_state(root)
    duplicates = state.setdefault("duplicates", {})
    review_path = root / "ledger-review.json"
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        review = {"schema": "ledger-review-v1", "decisions": {}}
    decisions = review.setdefault("decisions", {})
    if action == "undo":
        duplicates.pop(event_id, None)
        decisions.pop(event_id, None)
    else:
        duplicates[event_id] = {"action": action, "at": time.time()}
        if action == "same":
            decisions[event_id] = {"action": "exclude", "date": date, "decided_by": "user",
                                   "basis": "需要我判断：和活动里同一笔"}
    temp = review_path.with_name(f".{review_path.name}.tmp")
    temp.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, review_path)
    _write_state(root, state)
    return {"ok": True, "event_id": event_id, "action": action}


def _answer_dir(flywheel_root: Path | None) -> Path:
    return (flywheel_root or get_flywheel_root()) / "answers"


def record_answer(
    item: Mapping[str, Any] | None,
    operation: str,
    op_id: str,
    *,
    flywheel_root: Path | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path | None:
    """把一次回答写进飞轮。媒体答案同时写负样本，供抽取时提示模型。"""
    if not item or operation == "uncertain" or not op_id:
        return None
    purpose = purpose_for_reason(item.get("reason"))
    quote = str(item.get("quote") or "")
    record: dict[str, Any] = {
        "schema_version": "flywheel-judgment-v1",
        "op_id": op_id,
        "source_id": item.get("source_id"),
        "purpose": purpose,
        "operation": operation,
        "quote": quote,
        "segment_id": item.get("segment_id"),
        "created_at": time.time(),
        **dict(extra or {}),
    }
    if operation == "media":
        negative = record_dismissed_event(
            {"title": quote[:30], "categories": [purpose], "evidence": {"quote": quote}},
            source_text=quote,
            reason=f"judgment_{purpose}_media",
            root=flywheel_root,
        )
        record["negative_path"] = str(negative) if negative else None
    target = _answer_dir(flywheel_root)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{re.sub(r'[^A-Za-z0-9_-]', '_', op_id)}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def undo_answer(op_id: str, *, flywheel_root: Path | None = None) -> dict[str, Any] | None:
    """撤销飞轮记录，返回原记录；调用方据此撤掉它带来的人工事件。"""
    path = _answer_dir(flywheel_root) / f"{re.sub(r'[^A-Za-z0-9_-]', '_', str(op_id))}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    negative = record.get("negative_path")
    if negative:
        Path(negative).unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return record


def remove_manual_event(root: Path, job_id: str, event_id: str) -> bool:
    path = root / "jobs" / job_id / "manual-events.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entries = payload.get("events", []) if isinstance(payload, dict) else payload
    kept = [item for item in entries if item.get("event_id") != event_id]
    if len(kept) == len(entries):
        return False
    if isinstance(payload, dict):
        payload["events"] = kept
    else:
        payload = kept
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return True


def find_open_item(root: Path, source_id: str) -> dict[str, Any] | None:
    return next((row for row in _open_items(root) if str(row.get("source_id") or "") == source_id), None)


CALORIE_OPERATIONS = ("adopt_candidate", "enter_weight", "skip")

#: 用户采纳/补重量后写进 `data.food.estimate_basis` 的两个值（不是抽取层的 basis）
BASIS_USER_ADOPTED = "user_adopted_candidate"
BASIS_USER_WEIGHT = "user_weight"


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _nonnegative_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _parse_user_per_100g(raw: Any) -> dict[str, float] | None:
    """解析前端传来的每 100g 营养成分表（支持 energy_kcal 或 energy_kj）。"""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, float] = {}
    kcal = _positive_float(raw.get("energy_kcal"))
    if kcal is None:
        kj = _positive_float(raw.get("energy_kj"))
        if kj is not None:
            kcal = round(kj / 4.184, 2)
    if kcal is not None:
        out["energy_kcal"] = round(kcal, 2)
    for key in ("protein_g", "fat_g", "carbohydrates_g", "sodium_mg"):
        val = _nonnegative_float(raw.get(key))
        if val is not None:
            out[key] = round(val, 2)
    return out if "energy_kcal" in out else None


def _calorie_food_payload(item: Mapping[str, Any], *, grams: float, kcal: float,
                          basis: str, nutrients: Mapping[str, Any] | None,
                          per_100g: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """写进人工事件 `data.food` 的结构。

    `source_event_id` 是**原台账条目的 event_id** —— 有了它，读层（营养台账）才能把原来那条
    「热量未知」的记录和这条人工事件对上，避免同一顿饭既算未知又算一次候选。
    """
    food: dict[str, Any] = {
        "item": str(item.get("item") or "").strip() or "未命名食物",
        "weight_g": grams,
        "portion": f"{grams:g}克",
        "estimated_calories": kcal,
        "nutrients": dict(nutrients or {}),
        "estimate_basis": basis,
        "estimate_confidence": "high" if per_100g else "medium",
        "meal": str(item.get("meal") or "") or None,
        "nutrition_source": "user_judgment",
        "source_event_id": str(item.get("event_id") or ""),
    }
    if per_100g:
        food["nutrition_per_100g"] = dict(per_100g)
        food["nutrition_source_label"] = "手动营养成分表"
    return {"food": food}


def _apply_calorie_operation(
    store: Any,
    item: Mapping[str, Any],
    operation: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """执行「热量待定」的三种回答，返回 (extra, review_operation)。

    * ``adopt_candidate`` → 写人工饮食事件（basis=user_adopted_candidate）
    * ``enter_weight``    → 按候选的「每克热量」折算后写人工饮食事件（basis=user_weight）
    * ``skip``            → 不写事件，只记回答（review_operation=query → 记为已答）
    """
    if operation == "skip":
        return {"skipped": True}, "query"

    candidate = item.get("candidate") if isinstance(item.get("candidate"), Mapping) else {}
    name = str(item.get("item") or "").strip() or "未命名食物"
    cand_kcal = _positive_float(candidate.get("calories_kcal"))
    cand_grams = _positive_float(candidate.get("grams"))
    user_per_100g = _parse_user_per_100g(payload.get("per_100g"))

    if operation == "adopt_candidate":
        if cand_kcal is None:
            raise ValueError("这条没有候选热量，不能采用")
        grams = cand_grams if cand_grams is not None else 0.0
        kcal = round(cand_kcal, 1)
        basis = BASIS_USER_ADOPTED
        nutrients = candidate.get("nutrients") if isinstance(candidate.get("nutrients"), Mapping) else {}
    else:  # enter_weight
        grams = _positive_float(payload.get("grams"))
        if grams is None:
            raise ValueError("请填写克数")
        if grams > 10_000:
            raise ValueError("克数不合理")
        manual_kcal = _positive_float(payload.get("calories_kcal"))
        cand_nutrients = candidate.get("nutrients") if isinstance(candidate.get("nutrients"), Mapping) else {}
        if user_per_100g is not None:
            factor = grams / 100.0
            kcal = round(float(user_per_100g["energy_kcal"]) * factor, 1)
            nutrients = {
                k: (
                    round(float(user_per_100g[k]) * factor, 0)
                    if k == "sodium_mg"
                    else round(float(user_per_100g[k]) * factor, 1)
                )
                for k in ("carbohydrates_g", "protein_g", "fat_g", "sodium_mg")
                if k in user_per_100g
            }
            # 同步写入自建品牌库（若存在 food-data 目录），下次再吃同款直接复用
            try:
                food_dir = Path(store.root) / "food-data"
                if food_dir.is_dir():
                    from life_logger.food_lookup import BrandFoodLibrary, default_food_lookup

                    BrandFoodLibrary(food_dir / "brand-library.json").remember(
                        {
                            "name": name,
                            "per_100g": user_per_100g,
                            "serving_g": grams,
                            "evidence": "user_spoken",
                            "origin": "user",
                        },
                        note="user_inline_nutrition_table",
                    )
                    default_food_lookup(food_dir).brand._entries = None
            except Exception:
                pass
        elif manual_kcal is not None:
            kcal = round(manual_kcal, 1)
            nutrients = (
                {
                    k: round(float(v) / cand_grams * grams, 1)
                    for k, v in cand_nutrients.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                }
                if cand_grams and cand_grams > 0
                else {}
            )
        elif cand_kcal is not None and cand_grams is not None and cand_grams > 0:
            kcal = round(cand_kcal / cand_grams * grams, 1)
            nutrients = {
                k: round(float(v) / cand_grams * grams, 1)
                for k, v in cand_nutrients.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        else:
            from life_logger.food_nutrition import nutrition_for_item

            looked = nutrition_for_item(name, grams)
            looked_kcal = _positive_float((looked or {}).get("estimated_calories")) if isinstance(looked, Mapping) else None
            if looked_kcal is None:
                raise ValueError("这条候选没有克数，无法按重量折算；可同时填写千卡或每100g营养成分表")
            kcal = round(looked_kcal, 1)
            raw_nutr = looked.get("nutrients") if isinstance(looked.get("nutrients"), Mapping) else {}
            nutrients = {
                k: round(float(raw_nutr[k]), 1)
                for k in ("carbohydrates_g", "protein_g", "fat_g", "sodium_mg")
                if isinstance(raw_nutr.get(k), (int, float)) and not isinstance(raw_nutr.get(k), bool)
            }
        basis = BASIS_USER_WEIGHT

    data = _calorie_food_payload(item, grams=grams, kcal=kcal, basis=basis, nutrients=nutrients, per_100g=user_per_100g)
    extra = _save_entry(store, item, category="food", title=name, data=data)
    extra["amount"] = None
    extra["calories_kcal"] = kcal
    extra["estimate_basis"] = basis
    if cand_grams is not None:
        extra["candidate_grams"] = cand_grams
    return extra, "personal"


def _save_entry(store: Any, item: Mapping[str, Any], *, category: str, title: str, data: dict[str, Any]) -> dict[str, str]:
    date = str(item.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("这条没有日期，不能直接入账")
    quote = str(item.get("quote") or "")
    job_id = store.create_job()
    event = store.save_manual_event(
        job_id,
        artifact_path=f"{date}/events.json",
        title=title,
        categories=[category],
        absolute_time=item.get("recorded_at"),
        time_precision="exact" if item.get("recorded_at") else "unknown",
        activity_type={"expense": "purchase", "todo": "todo", "food": "food"}.get(category, "todo"),
        data=data,
        note=f"用户在需要我判断里确认：{quote}",
        source_type="user_supplement",
        evidence={"source": "user_judgment", "quote": quote, "source_id": item.get("source_id")},
    )
    return {"job_id": job_id, "event_id": str(event.get("event_id") or "")}


def _answer_record_path(op_id: str, flywheel_root: Path | None) -> Path:
    return _answer_dir(flywheel_root) / f"{re.sub(r'[^A-Za-z0-9_-]', '_', str(op_id))}.json"


def _existing_answer(op_id: str, flywheel_root: Path | None) -> dict[str, Any] | None:
    """按 op_id 读第一次的回答记录（record_answer 写的 <op_id>.json）。"""
    try:
        raw = json.loads(_answer_record_path(op_id, flywheel_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def answer(store: Any, payload: Mapping[str, Any], *, flywheel_root: Path | None = None) -> dict[str, Any]:
    """处理一次回答：入账类先写人工事件，再记来源判断，最后写飞轮。"""
    root = Path(store.root)
    source_id = str(payload.get("source_id") or "").strip()
    operation = str(payload.get("operation") or "").strip()
    op_id = str(payload.get("op_id") or "").strip()
    if not op_id:
        raise ValueError("缺少 op_id")
    # [2026-11-06 F1] **任何写入之前**先按 op_id 判幂等：
    # 同 op_id 已经处理过 → 直接返回第一次的结果，不再建第二笔人工事件。
    # 之前这一步排在 _save_entry() 之后，重发会先落第二笔事件，
    # undo() 又只能撤最后一笔 → 账本残留 14 元（审计 F1）。
    with file_lock(_answer_record_path(op_id, flywheel_root)):
        first = _existing_answer(op_id, flywheel_root)
        if first is not None:
            replay = {k: first.get(k) for k in
                      ("op_id", "source_id", "operation", "job_id", "event_id", "amount")}
            return {**{k: v for k, v in replay.items() if v is not None},
                    "operation": str(first.get("operation") or operation),
                    "idempotent": True}
        return _answer_once(store, payload, flywheel_root=flywheel_root,
                            root=root, source_id=source_id,
                            operation=operation, op_id=op_id)


def _answer_once(store: Any, payload: Mapping[str, Any], *,
                 flywheel_root: Path | None, root: Path,
                 source_id: str, operation: str, op_id: str) -> dict[str, Any]:
    """真正执行一次回答（只在持有该 op_id 的锁、且确认未处理过时调用）。"""
    item = find_open_item(root, source_id)
    if item is None:
        # [2026-11-07] 候选热量不在 pending_review 里，来自当天 nutrition_ledger。
        item = find_calorie_item(root, source_id)
    if item is None:
        raise ValueError("这条已经不在待判断列表里")
    extra: dict[str, Any] = {}
    review_op = operation
    if operation == "enter_expense":
        try:
            amount = float(payload.get("amount"))
        except (TypeError, ValueError):
            raise ValueError("请填写金额") from None
        if not 0 < amount < 1_000_000:
            raise ValueError("金额不合理")
        label = str(payload.get("label") or item.get("quote") or "")[:40].strip()
        extra = _save_entry(
            store, item, category="expense", title=label,
            data={"expense": {"amount": amount, "currency": "CNY", "item": label}},
        )
        extra["amount"] = amount
        review_op = "personal"
    elif operation == "enter_todo":
        label = str(payload.get("label") or item.get("quote") or "")[:80].strip()
        extra = _save_entry(store, item, category="todo", title=label, data={"todo": {"task": label}})
        review_op = "personal"
    elif operation in CALORIE_OPERATIONS:
        extra, review_op = _apply_calorie_operation(store, item, operation, payload)
    result = answer_review_question(
        root, source_id=source_id, operation=review_op, op_id=op_id,
        reason=f"需要我判断：{operation}", source_lookup=lambda _sid: item,
    )
    record_answer(item, operation, op_id, flywheel_root=flywheel_root, extra=extra)
    invalidate_cache()
    return {**result, "operation": operation, **extra}


def undo(store: Any, payload: Mapping[str, Any], *, flywheel_root: Path | None = None) -> dict[str, Any]:
    root = Path(store.root)
    source_id = str(payload.get("source_id") or "").strip()
    op_id = str(payload.get("op_id") or "").strip()
    result = undo_review_question(root, source_id=source_id, op_id=op_id)
    record = undo_answer(op_id, flywheel_root=flywheel_root)
    if record and record.get("job_id") and record.get("event_id"):
        remove_manual_event(root, str(record["job_id"]), str(record["event_id"]))
    invalidate_cache()
    return result


def flywheel_stats(flywheel_root: Path | None = None) -> dict[str, int]:
    root = flywheel_root or get_flywheel_root()
    counts = {"negatives": 0, "corrections": 0, "answers": 0, "voice_samples": 0}
    for name, key in (("negatives", "negatives"), ("corrections", "corrections"), ("voiceprints", "voice_samples")):
        folder = root / name
        counts[key] = sum(1 for _ in folder.glob("*.json")) if folder.is_dir() else 0
    answers = _answer_dir(root)
    if answers.is_dir():
        for path in answers.glob("*.json"):
            counts["answers"] += 1
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("purpose") == VOICEPRINT and record.get("operation") == "personal":
                counts["voice_samples"] += 1
    return counts


def build_queue(
    root: Path | str,
    *,
    purpose: str = "",
    include_older: bool = False,
    today: str | None = None,
    flywheel_root: Path | None = None,
) -> dict[str, Any]:
    root_path = Path(root)
    current = today or datetime.now(_SHANGHAI).date().isoformat()
    recent_from = (datetime.fromisoformat(current) - timedelta(days=RECENT_DAYS - 1)).date().isoformat()
    rows = list(_open_items(root_path))
    # [2026-11-07] 「热量待定」条目不在 pending_review 里，来自当天营养台账。
    # 台账字段缺失（2 号窗交付前）时 calorie_items 返回 []，这里自然就是 0 条。
    rows += calorie_items(root_path, current)
    states = review_question_states(root_path, [str(row.get("source_id") or "") for row in rows])
    waiting = [
        row for row in rows
        if (states.get(str(row.get("source_id") or "")) or {}).get("state") not in {"answered", "muted"}
    ]
    selected = purpose if any(item["key"] == purpose for item in PURPOSES) else ""
    summary = []
    for meta in PURPOSES:
        mine = [row for row in waiting if purpose_for_reason(row.get("reason")) == meta["key"]]
        recent = [row for row in mine if str(row.get("date") or "") >= recent_from]
        if meta["key"] == CALORIES and not mine:
            # 没说不重量的食物时，不显示「热量待定」这一类（免得页面上多一个恒为 0 的分区）
            continue
        summary.append({**meta, "total_open": len(mine), "recent_open": len(recent), "older_open": len(mine) - len(recent)})
    picked = [
        row for row in waiting
        if (not selected or purpose_for_reason(row.get("reason")) == selected)
        and (include_older or str(row.get("date") or "") >= recent_from)
    ]
    picked.sort(key=lambda row: str(row.get("recorded_at") or row.get("date") or ""), reverse=True)
    items = []
    for row in picked[:ITEM_LIMIT]:
        kind = purpose_for_reason(row.get("reason"))
        entry = {
            "source_id": row.get("source_id"),
            "purpose": kind,
            "quote": row.get("quote"),
            "date": row.get("date"),
            "time": row.get("time"),
            "recorded_at": row.get("recorded_at"),
            "reason_label": row.get("reason_label"),
            "options": _OPTIONS[kind],
            "suggested_amount": parse_spoken_amount(str(row.get("quote") or "")) if kind == MONEY else None,
        }
        if kind == CALORIES:
            # 前端要显示：食物名、原话、候选克数/热量，以及关联的原始 event_id
            entry.update({
                "item": row.get("item"),
                "meal": row.get("meal"),
                "event_id": row.get("event_id"),
                "candidate": row.get("candidate"),
            })
        items.append(entry)
    return {
        "ok": True,
        "schema": "judgment-queue-v1",
        "recent_from": recent_from,
        "purposes": summary,
        "total_open": len(waiting),
        "matched": len(picked),
        "items": items,
        "lost_manual": lost_manual_events(root_path, "2026-11-05"),
        "possible_duplicates": possible_duplicates(
            root_path,
            {(datetime.fromisoformat(current) - timedelta(days=offset)).date().isoformat() for offset in range(RECENT_DAYS)},
        ),
        "flywheel": flywheel_stats(flywheel_root),
    }
