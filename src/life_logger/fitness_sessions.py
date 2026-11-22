"""Fitness sessions: group one gym visit, aggregate actions, verify reviews.

Division of labour (user 2026-11-07, same as food):

* the model only *understands* each quote (which action, how many sets, weight,
  duration, and whether it is live or an after-the-fact review);
* this module does all grouping/counting/comparison.  Reviews never add to the
  totals: they either confirm the live summary or produce an explicit difference.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Mapping, Sequence

UTC8 = dt.timezone(dt.timedelta(hours=8))

#: 健身结构化默认开思考；活动整理仍用全局 deepseek.reasoning_effort（不改它）。
FITNESS_REASONING_EFFORT_DEFAULT = "medium"
REASONING_EFFORTS = ("none", "low", "medium", "high")


def fitness_reasoning_effort(config: Mapping[str, Any] | None) -> str:
    raw = config.get("fitness") if isinstance(config, Mapping) else None
    value = str((raw or {}).get("reasoning_effort") or "").strip().lower() if isinstance(raw, Mapping) else ""
    return value if value in REASONING_EFFORTS else FITNESS_REASONING_EFFORT_DEFAULT

#: 同一次健身的最大相邻间隔。依据：11-05 现场条目从 18:33 到 19:30（57 分钟），
#: 历史健身通常也在一小时内；90 分钟留出换衣/排队余量，又不会把晚上的回顾并进来。
SESSION_GAP_MINUTES = 90

#: 明确离开健身房的证据：出现即结束当前会话，即使下一条还在 90 分钟内。
EXIT_PATTERNS = ("离开健身房", "离开健身", "回家了", "回家路上", "走了", "出门了", "不练了", "练完了走")

#: 没有模型时的保守回顾判据；只用于 v2 兜底/离线无模型，模型给出的 phase 优先。
REVIEW_PATTERNS = (
    "回顾", "称去健身房", "称今天", "称做", "然后做了", "之后又", "运动了好久",
    "做了一个半小时", "做了有", "做了半个小时", "半小时的爬坡",
)

#: 「反重力」在本用户语境指 Antigravity 软件；与这些词同现时不是器材。
SOFTWARE_HINTS = ("登不上", "登录", "登陆", "设置", "地区", "谷歌", "电脑", "软件", "账号",
                  "打开", "网站", "单词", "端口", "网络", "报错", "界面", "应用", "浏览器")

#: 泛泛的能力/习惯/比较陈述：不能覆盖当天数值，只作备注。
GENERIC_REVIEW_PATTERNS = ("以前", "平时", "平常", "顶多", "一般", "通常", "过去", "原来")

_ACTION_PREFIX = re.compile(r"^(?:回顾称|回顾|又称|然后|之后又|又|再|做完了?|做了|进行|完成)")
_TRAILING = " \t，,。.;；：:"


def _parse_time(entry: Mapping[str, Any]) -> dt.datetime | None:
    for key in ("spoken_at", "occurred_at", "reported_at", "time", "timestamp"):
        value = entry.get(key)
        if isinstance(value, Mapping):
            value = value.get("absolute")
        text = str(value or "").strip()
        if not text:
            continue
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC8)
        return moment.astimezone(UTC8)
    return None


def _text(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('title') or ''} {entry.get('quote') or ''}"


def _looks_retrospective(entry: Mapping[str, Any]) -> bool:
    text = _text(entry)
    return any(pattern in text for pattern in REVIEW_PATTERNS)


def is_software_talk(text: str, neighbours: Sequence[str] = ()) -> bool:
    """「反重力…」是否在说 Antigravity 软件，而不是健身器材。

    只有**本条**提到反重力才判定（邻句只提供软件语境，不能把相邻的训练句
    也一起误删）。
    """
    own = str(text or "")
    if "反重力" not in own and "antigravity" not in own.lower():
        return False
    blob = f"{own} " + " ".join(str(n) for n in neighbours)
    return any(hint in blob for hint in SOFTWARE_HINTS)


def filter_exercise_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    ordered = [r for r in records if isinstance(r, Mapping)]
    kept = []
    for index, record in enumerate(ordered):
        text = str(record.get("text") or record.get("quote") or "")
        if str(record.get("source_label") or "self") != "self":
            continue
        neighbours = [str(r.get("text") or r.get("quote") or "") for r in ordered[max(0, index - 3):index + 4]]
        if is_software_talk(text, neighbours):
            continue
        kept.append(record)
    return kept


def _clean_name(value: Any) -> str:
    name = str(value or "").strip().strip(_TRAILING)
    name = _ACTION_PREFIX.sub("", name).strip()
    name = re.sub(r"^(?:了|的)+", "", name).strip()
    return name[:40]


def _fallback_name(title: Any) -> str:
    """模型没给动作名时的短标签；不把整句标题搬成动作名。"""
    text = str(title or "")
    for keyword, label in (("力量训练", "力量训练（未指明动作）"), ("练胸", "胸部训练"),
                           ("卧推", "卧推"), ("深蹲", "深蹲"), ("硬拉", "硬拉"),
                           ("推肩", "推肩"), ("飞鸟", "飞鸟"), ("有氧", "有氧")):
        if keyword in text:
            return label
    return _clean_name(text) or "未命名动作"


def _has_structured(entry: Mapping[str, Any]) -> bool:
    if entry.get("fact_schema_version") == 3:
        return True
    exercises = entry.get("exercises")
    if not isinstance(exercises, list) or not exercises:
        return False
    keys = {"sets", "reps", "weight_value", "weight_unit", "side", "duration_seconds"}
    return any(isinstance(ex, Mapping) and keys & set(ex) for ex in exercises)


def _default_items(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = []
    for entry in session["entries"]:
        phase = "review" if _looks_retrospective(entry) else "live"
        exercises = entry.get("exercises") if isinstance(entry.get("exercises"), list) else []
        if not exercises:
            exercises = [{"name": entry.get("title")}]
        for exercise in exercises:
            if not isinstance(exercise, Mapping):
                continue
            item = {"event_id": entry.get("event_id"), "phase": phase,
                    "name": exercise.get("name") or entry.get("title")}
            for key in ("sets", "set_number", "set", "reps", "weight_value", "weight_unit", "side", "duration_seconds"):
                if exercise.get(key) is not None:
                    item[key] = exercise[key]
            items.append(item)
    return items


def _group(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(enumerate(entries), key=lambda pair: (_parse_time(pair[1]) or dt.datetime.max.replace(tzinfo=UTC8), pair[0]))
    sessions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for _index, entry in ordered:
        moment = _parse_time(entry)
        if moment is None:
            sessions.append({"entries": [entry], "start": None, "end": None})
            current = None
            continue
        if current is not None:
            gap = (moment - current["end"]).total_seconds() / 60.0
            exited = any(p in _text(current["entries"][-1]) for p in EXIT_PATTERNS)
            if gap > SESSION_GAP_MINUTES or exited:
                current = None
        if current is None:
            current = {"entries": [], "start": moment, "end": moment}
            sessions.append(current)
        current["entries"].append(entry)
        current["end"] = max(current["end"], moment)
    return sessions


def _same_action(left: str, right: str) -> bool:
    a, b = left.replace(" ", ""), right.replace(" ", "")
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5


def _aggregate(items: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    live: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    for item in items:
        phase = str(item.get("phase") or "live")
        target = reviews if phase == "review" else live
        target.append(dict(item))
    actions: list[dict[str, Any]] = []
    for item in live:
        name = _clean_name(item.get("name")) or "未命名动作"
        match = next((a for a in actions if _same_action(a["name"], name)), None)
        if match is None:
            match = {"name": name, "sets": None, "reps": None, "weight_value": None,
                     "weight_unit": None, "side": None, "duration_seconds": None,
                     "evidence_event_ids": [], "set_details": [],
                     "source": "live", "review_fields": [], "live_fields": [], "_conflicts": []}
            actions.append(match)
        match["evidence_event_ids"].append(item.get("event_id"))
        number = item.get("set_number", item.get("set"))
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            match.setdefault("set_numbers", set()).add(number)
            match.setdefault("declared_counts", set()).add(number)
        sets = item.get("sets")
        if isinstance(sets, int) and not isinstance(sets, bool) and sets > 0:
            match.setdefault("declared_counts", set()).add(sets)
            match["set_details"].append({"event_id": item.get("event_id"), "sets": sets,
                                         "weight_value": item.get("weight_value")})
        for key in ("reps",):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                match[key] = max(match.get(key) or 0, value)
        weight = item.get("weight_value")
        if isinstance(weight, (int, float)) and not isinstance(weight, bool):
            if match["weight_value"] is None or weight > match["weight_value"]:
                match["weight_value"] = float(weight)
                match["weight_unit"] = item.get("weight_unit")
                match["side"] = item.get("side")
        duration = item.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            match["duration_seconds"] = (match["duration_seconds"] or 0) + float(duration)
    for match in actions:
        numbers = sorted(match.pop("set_numbers", set()))
        declared = sorted(match.pop("declared_counts", set()))
        # 明确「第 N 组」取最大 N；总结句与逐组冲突时保留逐组数字并记差异。
        if numbers:
            match["sets"] = max(numbers)
            match["set_numbers"] = numbers
            conflicts = [n for n in declared if n != max(numbers)]
            if conflicts:
                match["set_conflict"] = f"逐组到第 {max(numbers)} 组，另有总结说 {conflicts} 组"
        elif declared:
            match["sets"] = sum(declared) if len(declared) > 1 else declared[0]
    for action in actions:
        action["live_fields"] = [key for key in
                                 ("sets", "reps", "weight_value", "weight_unit", "side", "duration_seconds")
                                 if action.get(key) is not None]
    notes = _apply_reviews(actions, reviews)
    return actions, reviews, notes


def _review_sets(item: Mapping[str, Any]) -> int | None:
    number = item.get("set_number", item.get("set"))
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return number
    sets = item.get("sets")
    return sets if isinstance(sets, int) and not isinstance(sets, bool) and sets > 0 else None


def _review_value(item: Mapping[str, Any], key: str):
    if key == "sets":
        return _review_sets(item)
    return item.get(key)


def _is_generic_review(item: Mapping[str, Any]) -> bool:
    text = f"{item.get('name') or ''} {item.get('_quote') or ''}"
    return any(pattern in text for pattern in GENERIC_REVIEW_PATTERNS)


def _is_cardio_total(name: str) -> bool:
    return any(word in name for word in ("有氧", "爬坡慢走")) and "爬坡" not in name.replace("爬坡慢走", "")


def _apply_reviews(actions: list[dict[str, Any]], reviews: Sequence[Mapping[str, Any]]) -> list[str]:
    """回顾优先，但区分“今天这次”与“泛泛能力/习惯”。

    * 只有明确指向本次训练的回顾才覆盖数值；
    * “以前/平时/顶多/一般”等比较句只进 review_notes，不改任何字段；
    * 有氧总数只算一次，分解项（爬坡 30 分钟等）挂在 breakdown 下，不再相加。
    """
    notes: list[str] = []
    fields = ("sets", "reps", "weight_value", "weight_unit", "side", "duration_seconds")
    cardio_total = None
    components: list[dict[str, Any]] = []
    ordinary: list[Mapping[str, Any]] = []
    for review in reviews:
        name = _clean_name(review.get("name")) or "未命名动作"
        if _is_generic_review(review):
            quote = str(review.get("_quote") or review.get("name") or "").strip()
            if quote:
                notes.append(quote[:120])
            continue
        if _is_cardio_total(name):
            duration = review.get("duration_seconds")
            if isinstance(duration, (int, float)) and duration > 0:
                cardio_total = max(cardio_total or 0, float(duration))
            continue
        if "爬坡" in name and review.get("duration_seconds"):
            components.append({"name": name, "duration_seconds": float(review["duration_seconds"])})
            continue
        ordinary.append(review)

    def apply(review: Mapping[str, Any], *, target: dict[str, Any] | None = None) -> dict[str, Any] | None:
        name = _clean_name(review.get("name")) or "未命名动作"
        values = {key: _review_value(review, key) for key in fields}
        match = target or next((a for a in actions if _same_action(a["name"], name)), None)
        if match is None:
            present = [key for key, value in values.items() if value is not None]
            if not present:
                return None
            match = {"name": name, **values, "evidence_event_ids": [review.get("event_id")],
                     "set_details": [], "source": "review", "review_fields": present,
                     "live_fields": [], "_conflicts": []}
            actions.append(match)
            return match
        for key in fields:
            value = values[key]
            if value is None:
                continue
            previous = match.get(key)
            if previous is not None and previous != value:
                conflicts = match.setdefault("_conflicts", [])
                existing = next((c for c in conflicts if c["field"] == key), None)
                if existing is not None:
                    existing["review"] = value
                else:
                    conflicts.append({"field": key, "name": match["name"],
                                      "live": previous, "review": value})
            match[key] = value
            match["source"] = "review"
            if key not in match["review_fields"]:
                match["review_fields"].append(key)
            if key in match["live_fields"]:
                match["live_fields"].remove(key)
        return match

    for review in ordinary:
        apply(review)

    if cardio_total is not None:
        cardio = next((a for a in actions if _is_cardio_total(a["name"])), None)
        if cardio is None:
            cardio = {"name": "有氧", "sets": None, "reps": None, "weight_value": None,
                      "weight_unit": None, "side": None, "duration_seconds": None,
                      "evidence_event_ids": [], "set_details": [], "source": "live",
                      "review_fields": [], "live_fields": [], "_conflicts": []}
            actions.append(cardio)
        previous = cardio.get("duration_seconds")
        if previous is not None and previous != cardio_total:
            cardio.setdefault("_conflicts", []).append(
                {"field": "duration_seconds", "name": cardio["name"],
                 "live": previous, "review": cardio_total})
        cardio["duration_seconds"] = cardio_total
        cardio["source"] = "review"
        if "duration_seconds" not in cardio["review_fields"]:
            cardio["review_fields"].append("duration_seconds")
        remainder = cardio_total - sum(c["duration_seconds"] for c in components)
        breakdown = list(components)
        if remainder > 0 and components:
            # 总数有、分解不全：把差额标成“其余有氧”，不把它再算一遍总数。
            breakdown.append({"name": "其余有氧", "duration_seconds": remainder, "derived": True})
        cardio["breakdown"] = breakdown
    else:
        for component in components:
            apply({"name": component["name"], "duration_seconds": component["duration_seconds"],
                   "phase": "review"})
    return notes


def _verify(actions: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]],
            total_duration: float | None, *, has_live: bool = True) -> dict[str, Any]:
    if not reviews:
        return {"status": "confirmed" if actions else "empty", "differences": [], "source": "live"}
    if not has_live:
        return {"status": "review_only", "differences": [], "source": "review"}
    differences: list[str] = []
    for action in actions:
        for conflict in action.get("_conflicts") or []:
            field = conflict["field"]
            differences.append(
                f"「{conflict['name']}」{field}: 现场 {conflict['live']} / 回顾 {conflict['review']}（以回顾为准）")
    return {"status": "differences" if differences else "confirmed",
            "differences": list(dict.fromkeys(differences)), "source": "live+review"}


def structure_sessions(
    workouts: Sequence[Mapping[str, Any]],
    *,
    structurer: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    day: str | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把运动条目合成会话；模型只提供逐条理解，计数比较全在这里。

    ``records`` 给了就用会话时间段内的**全部本人原话**喂模型（v2 漏分类的现场句
    也在内），否则退回用 workouts 条目。非本人/媒体/软件话题不送。
    """
    sessions = _group([w for w in workouts if isinstance(w, Mapping)])
    # 回顾句跟随它要核对的现场会话；没有现场记录时单独成会话并标「来自回顾」。
    live_sessions = [s for s in sessions if any(not _looks_retrospective(e) for e in s["entries"])]
    merged: list[dict[str, Any]] = []
    for session in sessions:
        retro_only = all(_looks_retrospective(e) for e in session["entries"])
        if not retro_only:
            merged.append(session)
            continue
        target = live_sessions[-1] if live_sessions else None
        if target is not None and target in merged:
            target["entries"].extend(session["entries"])
        else:
            merged.append(session)
    output = []
    for index, session in enumerate(merged):
        public = {"index": index, "entries": list(session["entries"]),
                  "start": session["start"].isoformat() if session["start"] else None,
                  "end": session["end"].isoformat() if session["end"] else None}
        structured = None
        input_records = None
        if records is not None and session["start"]:
            start = session["start"] - dt.timedelta(minutes=1)
            last_moment = max((_parse_time(e) for e in session["entries"] if _parse_time(e)),
                              default=session["end"])
            # 已并入的回顾条目也要作为“回顾”输入，用于核对；按时间标记 phase。
            end = (last_moment or session["end"]) + dt.timedelta(minutes=2)
            live_cut = (session["start"] + dt.timedelta(minutes=SESSION_GAP_MINUTES)) if session["start"] else None
            # 只有**超出健身窗口**（90 分钟）的条目才算事后回顾；19:12 这种
            # “回顾刚做完的平板卧推”仍在现场，按用户 11-07 规则计入现场。
            retro_times = [_parse_time(e) for e in session["entries"]
                           if _looks_retrospective(e) and _parse_time(e)
                           and session["start"] and _parse_time(e) > session["start"] + dt.timedelta(minutes=SESSION_GAP_MINUTES)]
            retro_start = min(retro_times) - dt.timedelta(minutes=1) if retro_times else None
            candidates = []
            for r in filter_exercise_records(records):
                moment = _parse_time(r)
                if moment is None or moment < start or moment > end:
                    continue
                # 现场窗口 + 真正的事后回顾段；中间的普通聊天不送模型，避免噪声变成“回顾声明”。
                if (live_cut and moment <= live_cut) or (retro_start and moment >= retro_start):
                    candidates.append(r)
            input_records = candidates
        if structurer is not None and not all(_has_structured(e) for e in session["entries"]):
            payload_session = {**public, "day": day}
            if input_records is not None and input_records:
                payload_session["entries"] = [
                    {"event_id": r.get("source_id") or r.get("event_id"), "title": str(r.get("text"))[:32],
                     "quote": r.get("text"), "spoken_at": r.get("timestamp") or r.get("spoken_at")}
                    for r in input_records]
            structured = structurer(payload_session)
        if isinstance(structured, Mapping) and isinstance(structured.get("items"), list):
            items = [dict(item) for item in structured["items"] if isinstance(item, Mapping)]
            live_end = (session["start"] + dt.timedelta(minutes=SESSION_GAP_MINUTES)
                        if session["start"] else None)
            if input_records is not None:
                by_id = {str(r.get("source_id")): r for r in input_records}
                for item in items:
                    moment = _parse_time(by_id.get(str(item.get("event_id")), {}))
                    if live_end and moment and moment > live_end + dt.timedelta(minutes=2):
                        item["phase"] = "review"
            else:
                by_id = {e.get("event_id"): e for e in session["entries"]}
            for item in items:
                if not item.get("name") and item.get("event_id") in by_id:
                    item["name"] = _fallback_name(by_id[item["event_id"]].get("title"))
        else:
            items = _default_items(session)
        has_live = any(str(i.get("phase")) != "review" for i in items)
        entry_text = {e.get("event_id"): (e.get("quote") or e.get("title") or e.get("_quote") or "")
                      for e in session["entries"]}
        for item in items:
            if not item.get("_quote"):
                item["_quote"] = str(entry_text.get(item.get("event_id")) or "")
        if input_records is not None:
            texts = {str(r.get("source_id") or r.get("event_id")): str(r.get("text") or "")
                     for r in input_records}
            for item in items:
                if not item.get("_quote"):
                    item["_quote"] = texts.get(str(item.get("event_id")), "")
        actions, reviews, review_notes = _aggregate(items)
        # 现场时长只汇总有现场成分的动作；纯回顾动作的时长在 action 上标 source=review。
        durations = [a["duration_seconds"] for a in actions
                     if a.get("duration_seconds") and a.get("source") != "review"]
        # 只有回顾时，回顾里的时长是唯一来源（仍只算一次，不叠加）。
        review_durations = [r["duration_seconds"] for r in reviews if r.get("duration_seconds")]
        # 只有完全没有现场记录时，回顾时长才是整次会话的唯一来源。
        cardio = (sum(durations) if durations else
                  (sum(review_durations) if (review_durations and not has_live) else None))
        cardio_review_total = sum(float(a["duration_seconds"]) for a in actions
                                  if a.get("source") == "review" and a.get("duration_seconds")
                                  and _is_cardio_total(str(a.get("name"))))
        span = None
        if session["start"] and session["end"]:
            span = (session["end"] - session["start"]).total_seconds()
        total = span if (has_live and span) else (max(span, cardio or 0) if span else cardio)
        output.append({
            "session_id": f"{day or 'day'}-{index + 1}",
            "start": public["start"], "end": public["end"],
            "action_count": len(actions),
            "actions": [{k: v for k, v in action.items() if k != "_conflicts"} for action in actions],
            "cardio_duration_seconds": cardio, "total_duration_seconds": total,
            "review_duration_seconds": (cardio_review_total or
                                        (sum(review_durations) if review_durations else None)),
            "review_notes": list(dict.fromkeys(review_notes)),
            "duration_source": "live" if durations else ("review" if review_durations and not actions else None),
            "live_item_count": sum(1 for i in items if str(i.get("phase")) != "review"),
            "review_item_count": sum(1 for i in items if str(i.get("phase")) == "review"),
            "source": ("review" if not has_live else
                       "live+review" if any(a.get("review_fields") for a in actions) else "live"),
            "review_prioritized_action_count": sum(1 for a in actions if a.get("source") == "review"),
            "review_prioritized_field_count": sum(len(a.get("review_fields") or []) for a in actions),
            "verification": _verify(actions, reviews, total, has_live=has_live),
            "evidence": [{"event_id": e.get("event_id"), "quote": e.get("quote") or e.get("title"),
                          "phase": "review" if _looks_retrospective(e) else "live"}
                         for e in session["entries"]] if input_records is None else
                        [{"event_id": str(r.get("source_id") or r.get("event_id")),
                          "quote": r.get("text") or r.get("quote"),
                          "phase": "review" if live_end and _parse_time(r) and _parse_time(r) > live_end else "live"}
                         for r in input_records],
        })
    return output
