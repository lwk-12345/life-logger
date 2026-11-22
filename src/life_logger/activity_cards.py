"""原事件事实视图（activity cards）——每条 canonical event 一张卡。

设计边界（2026-10-25 用户要求：先落地"原事件事实视图"，不做跨事件关联）：

1. **一条事件 = 一张卡**。不合并、不推断跨事件归属、不生成活动实体，
   因此**不存在**"活动 ID 稳定性"承诺（那是另一条路线，本轮不做）。
2. **稳定引用原事件**：`source_event_id` 原样保留；`card_id` 只是该事件视图的本地键。
3. **只呈现已有事实**：状态（做了/计划/取消/未知）必须有证据标记；时间/时长/地点
   有则保留、无则 `unknown`；领域字段（健身动作/部位/组次、游戏名、饮食点单与支付）
   只从结构化 `data` 白名单读取。
4. **不新增实体或数字**：不从引文（quote）里解析金额/物品/时长，不做中文正则。
5. **原始事件与交易永不吞并**：一张卡一条事件；交易只做**引用**（不求和、不改账）。
6. **字段来源可追**：每个字段给出 `field_sources[字段] = 数据路径`；无法确定的写进
   `unknown_fields`，未知结构用保真条目（`facts[].path`）呈现，不套饮食模板。

纯函数：不读盘、不写盘、不调模型、无全局状态。用法::

    from life_logger.activity_cards import build_activity_cards
    result = build_activity_cards(events, ledger=expense_ledger_payload)
    result["cards"]           # 每条事件一张
    result["alias"]           # event_id -> card_id（仅本地视图键）
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

SCHEMA = "activity-card-v1"

#: 领域结构化区块白名单：只从这些路径取事实，未知区块按原路径保真呈现。
DOMAIN_SECTIONS: tuple[str, ...] = ("exercise", "game", "food", "meal", "expense", "travel", "work")

#: 结构化状态 → 卡片状态。只认这些**显式字段**，不认中文子串：
#:   `event_state` / `data.<section>.state` / `data.<section>.status`
STRUCTURED_STATE_MAP: dict[str, str] = {
    "completed": "done", "complete": "done", "done": "done", "finished": "done",
    "in_progress": "in_progress", "started": "in_progress", "ongoing": "in_progress",
    "planned": "planned", "plan": "planned", "scheduled": "planned",
    "cancelled": "cancelled", "canceled": "cancelled", "aborted": "cancelled",
}

#: 状态取值（`unknown` 表示"没提到"，不代表"没做"）。
STATES: tuple[str, ...] = ("done", "in_progress", "planned", "cancelled", "unknown")

LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _local_date(value: datetime) -> str | None:
    try:
        return value.astimezone(LOCAL_TZ).date().isoformat()
    except Exception:
        return value.date().isoformat() if value else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _kind_of(event: Mapping[str, Any]) -> str:
    categories = {str(item).strip().lower() for item in (event.get("categories") or [])}
    activity = _text(event.get("activity_type")).strip().lower()
    if categories & {"exercise", "fitness", "workout"} or activity.startswith(("body_", "workout", "exercise")):
        return "fitness"
    if categories & {"game", "gaming"} or activity.startswith("game"):
        return "game"
    if categories & {"food", "meal", "drink"}:
        return "meal"
    if categories & {"expense", "purchase", "shopping"}:
        return "purchase"
    if categories & {"work", "work_communication"}:
        return "work"
    if categories & {"travel", "transport"}:
        return "travel"
    if categories & {"routine", "health"}:
        return "routine"
    return "other"


def _state_of(event: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """状态只由**结构化字段**决定；自由文本再像也不改判。

    依证据优先级（2026-10-25 返工，A0 指定，不再堆否定词窗口）：
      1. `event_state`（canonical 事件状态）
      2. `data.<section>.state` / `data.<section>.status`
      3. `evidence.claim_type == "plan"` → planned
      多处结构化状态**互相冲突** → `unknown` 并把冲突列出；
      只有自由文本（"明天练完…"/"没有取消…"）→ `unknown`，原话保留在 evidence.quote。
    `claim_type in {event, order, purchase, transaction, payment}` **不再**等于已完成。
    """
    evidence = _mapping(event.get("evidence"))
    data = _mapping(event.get("data"))
    found: list[dict[str, Any]] = []

    raw_event_state = _text(event.get("event_state")).strip().lower()
    if raw_event_state:
        mapped = STRUCTURED_STATE_MAP.get(raw_event_state)
        found.append({"path": "event_state", "value": raw_event_state,
                      "state": mapped or "unknown"})

    for section in DOMAIN_SECTIONS:
        block = _mapping(data.get(section))
        for key in ("state", "status"):
            raw = _text(block.get(key)).strip().lower()
            if raw:
                found.append({"path": f"data.{section}.{key}", "value": raw,
                              "state": STRUCTURED_STATE_MAP.get(raw, "unknown")})

    claim = _text(evidence.get("claim_type")).strip().lower()
    if claim == "plan":
        found.append({"path": "evidence.claim_type", "value": claim, "state": "planned"})

    if not found:
        return "unknown", {"reason": "no_structured_state", "conflicts": []}
    states = {item["state"] for item in found}
    if "unknown" in states or len(states) > 1:
        return "unknown", {
            "reason": "conflicting_or_unmapped_structured_state",
            "conflicts": found,
        }
    only = states.pop()
    basis = dict(found[0])
    basis["conflicts"] = []
    return only, basis


def _time_view(event: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    time_info = _mapping(event.get("time"))
    evidence = _mapping(event.get("evidence"))
    occurred = _parse(time_info.get("absolute"))
    reported = _parse(evidence.get("reported_at"))
    precision = _text(time_info.get("precision")) or "unknown"
    basis = "occurrence" if occurred else ("reported" if reported else "unknown")
    view = {
        "occurred_at": occurred.isoformat() if occurred else None,
        "reported_at": reported.isoformat() if reported else None,
        "precision": precision if occurred or reported else "unknown",
        "basis": basis,
        "date": _local_date(occurred) if occurred else (_local_date(reported) if reported else None),
        "clock": occurred.astimezone(LOCAL_TZ).strftime("%H:%M") if occurred else None,
    }
    sources = {"time": "time.absolute" if occurred else ("evidence.reported_at" if reported else None)}
    return view, sources


def _duration_of(event: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """时长只认显式结构化字段；没有就返回 None + 原因。"""
    data = _mapping(event.get("data"))
    for section in DOMAIN_SECTIONS:
        block = _mapping(data.get(section))
        for key, unit in (("duration_minutes", "minutes"), ("duration_min", "minutes"),
                          ("total_duration_min", "minutes"), ("duration_seconds", "seconds")):
            raw = block.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            minutes = value / 60.0 if unit == "seconds" else value
            return ({"minutes": round(minutes, 1), "unit": "minutes", "path": f"data.{section}.{key}"},
                    f"data.{section}.{key}")
    evidence = _mapping(event.get("evidence"))
    if evidence.get("start_offset_seconds") is not None:
        return None, "recording_span_is_not_duration"
    return None, "no_explicit_duration"


def _location_of(event: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    data = _mapping(event.get("data"))
    evidence = _mapping(event.get("evidence"))
    for path, block, key in (
        ("data.location", data.get("location"), "name"),
        ("data.location", data.get("location"), "value"),
        ("evidence.location", evidence.get("location"), "name"),
    ):
        if isinstance(block, Mapping):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                return ({"name": value.strip(), "path": f"{path}.{key}"}, f"{path}.{key}")
        elif isinstance(block, str) and block.strip():
            return ({"name": block.strip(), "path": path}, path)
    return None, None


GENERIC_WORKOUT_TITLES = ("力量训练", "健身", "运动", "训练", "workout", "exercise")
#: 占位/无信息动作名：**不得**覆盖原标题（也不能作为标题来源）
PLACEHOLDER_ACTION_NAMES = frozenset({
    "未说明", "未明确", "未命名", "未识别", "未标明", "未知", "unknown", "n/a", "na", "none", "null", "-", "",
})


_PLACEHOLDER_ACTION_MARKERS = ("未说明", "未明确", "未命名", "未识别", "未标明", "待补充", "unknown")


def _is_placeholder_action(name: str) -> bool:
    text = name.strip().lower()
    if text in PLACEHOLDER_ACTION_NAMES:
        return True
    return any(marker in text for marker in _PLACEHOLDER_ACTION_MARKERS)
_CN_DIGITS_FOR_SUPPORT = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}


def _cn_number_support(value: Any) -> str | None:
    """把 40/60/80/90 这类整数转成中文口语形式（含"四十/六十/八十/九十"），用于引文比对。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0 or number >= 1000:
        return None
    if number < 10:
        return _CN_DIGITS_FOR_SUPPORT[number]
    tens, ones = divmod(number, 10)
    if tens >= 10:
        return None          # 100 以上不在此做口语化（避免误判"一百/一百二"歧义）
    text = ("" if tens == 1 else _CN_DIGITS_FOR_SUPPORT[tens]) + "十"
    if ones:
        text += _CN_DIGITS_FOR_SUPPORT[ones]
    return text


#: 重量单位（必须与数值**紧邻**才算重量证据；"90下/90元"不算）
_WEIGHT_UNITS = ("公斤", "kg", "KG", "Kg", "千克")


def _numeric_supported(quote: str, value: Any) -> bool:
    """该数值是否有**逐字重量证据**：数值 + 单位（公斤/kg/千克）紧邻，且不是其它语义。

    「今天做了90下深蹲，花了90元」→ 90 不是重量（无单位）→ 不确认。
    「第一组四十公斤」→ 确认；「深蹲四十公斤，硬拉六十」→ 只有 40 有单位，60 不借用。
    """
    if not quote:
        return False
    text = str(quote)
    forms = [str(value)]
    cn = _cn_number_support(value)
    if cn:
        forms.append(cn)
    for form in forms:
        if not form:
            continue
        for unit in _WEIGHT_UNITS:
            pattern = re.escape(form) + r"\s*" + re.escape(unit)
            if re.search(pattern, text):
                return True
    return False


def _split_fragments(quote: str) -> list[str]:
    return [part.strip() for part in re.split(r"[，,。；;！!？?\n]", str(quote or "")) if part.strip()]


def _set_marker_in_fragment(fragment: str, set_index: int) -> bool:
    """片段里是否有与 set_index 对应的组号（第N组 / N组）。"""
    forms = [str(set_index)]
    cn = _cn_number_support(set_index)
    if cn:
        forms.append(cn)
    return any(f"第{form}组" in fragment or f"{form}组" in fragment for form in forms)


def _weight_support(event: Mapping[str, Any], exercise_name: str, set_index: int, value: Any) -> str:
    """重量证据强度：必须**动作 + 组**归属，否则一律 `model_only`（保守，不猜）。"""
    evidence = _mapping(event.get("evidence"))
    if _text(evidence.get("extractor")).lower() == "session_aggregator":
        return "model_quote_only"
    exercises = [row for row in _mapping(_mapping(event.get("data")).get("exercise")).get("exercises") or []
                 if isinstance(row, Mapping)]
    multi_action = len([row for row in exercises if _text(row.get("name")).strip()]) > 1

    # 1) 带类型引用的 related_quotes：exercise + set 明确对应才确认
    for item in evidence.get("related_quotes") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("role")) != "weight":
            continue
        name = _text(item.get("exercise")).strip()
        if name and exercise_name and name != exercise_name:
            continue
        try:
            if int(item.get("set")) != int(set_index):
                continue
        except (TypeError, ValueError):
            continue
        if _numeric_supported(_text(item.get("quote")), value):
            return "quote_verbatim"

    # 2) 主 quote：片段需同时含数值+单位 与 该组组号；多动作时还要含动作名
    for fragment in _split_fragments(_text(evidence.get("quote"))):
        if not _numeric_supported(fragment, value):
            continue
        if not _set_marker_in_fragment(fragment, set_index):
            continue
        if multi_action and exercise_name and exercise_name not in fragment:
            continue
        return "quote_verbatim"
    return "model_only"


def _workout_title(event: Mapping[str, Any], kind: str) -> tuple[str, str]:
    """标题契约：有明确动作名时优先用户可读动作名；原标题保留在 title_original。"""
    original = _text(event.get("title")).strip()
    if kind != "fitness":
        return original, "event.title"
    exercise = _mapping(_mapping(event.get("data")).get("exercise"))
    action = ""
    for row in exercise.get("exercises") or []:
        if isinstance(row, Mapping):
            name = _text(row.get("name")).strip()
            if name and not _is_placeholder_action(name) and name != "未命名动作":
                action = name
                break
    generic = (not original) or any(token in original for token in GENERIC_WORKOUT_TITLES)
    if action and (generic or action not in original):
        return action, "data.exercise.exercises[0].name"
    return original, "event.title"


def _fitness_facts(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """健身：动作/部位/组次/次数/重量（每侧与合计都保留原样，不做换算）。"""
    data = _mapping(event.get("data"))
    exercise = _mapping(data.get("exercise"))
    facts: list[dict[str, Any]] = []
    body_part = exercise.get("body_part")
    if isinstance(body_part, str) and body_part.strip():
        facts.append({"path": "data.exercise.body_part", "value": body_part.strip(), "unit": None})
    for index, row in enumerate(exercise.get("exercises") or []):
        if not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if isinstance(name, str) and name.strip():
            facts.append({"path": f"data.exercise.exercises[{index}].name", "value": name.strip(), "unit": None})
        raw_sets = row.get("sets")
        rows = [r for r in raw_sets if isinstance(r, Mapping)] if isinstance(raw_sets, (list, tuple)) else [row]
        quote_text = _text(_mapping(event.get("evidence")).get("quote"))
        for set_index, item in enumerate(rows):
            base = f"data.exercise.exercises[{index}].sets[{set_index}]"
            for key, unit in (("set", None), ("reps", "reps"), ("weight_kg", "kg"),
                              ("note", None), ("weight_note", None), ("rest_seconds", "seconds")):
                value = item.get(key)
                if value is None or isinstance(value, bool):
                    continue
                fact = {"path": f"{base}.{key}", "value": value, "unit": unit}
                if key == "weight_kg":
                    # 归属判定：动作 + 组号 + 单位紧邻，三者齐备才算逐字证据
                    try:
                        declared_set = int(item.get("set") or (set_index + 1))
                    except (TypeError, ValueError):
                        declared_set = set_index + 1
                    fact["support"] = _weight_support(event, name, declared_set, value)
                facts.append(fact)
    return facts


def _game_facts(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = _mapping(event.get("data"))
    game = _mapping(data.get("game"))
    facts: list[dict[str, Any]] = []
    for key in ("name", "mode", "platform", "rounds", "result", "players"):
        value = game.get(key)
        if value is None or isinstance(value, bool):
            continue
        facts.append({"path": f"data.game.{key}", "value": value,
                      "unit": "rounds" if key == "rounds" else None})
    return facts


def _meal_facts(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """饮食：只呈现结构化点单/份量/口味；"是否吃了"必须来自显式字段。"""
    data = _mapping(event.get("data"))
    facts: list[dict[str, Any]] = []
    for section in ("food", "meal"):
        block = _mapping(data.get(section))
        for index, item in enumerate(block.get("items") or []):
            if isinstance(item, Mapping):
                for key in ("name", "portion", "flavor", "quantity", "unit"):
                    if item.get(key) is not None:
                        facts.append({"path": f"data.{section}.items[{index}].{key}",
                                      "value": item.get(key), "unit": item.get("unit") if key == "quantity" else None})
            elif isinstance(item, str) and item.strip():
                facts.append({"path": f"data.{section}.items[{index}]", "value": item.strip(), "unit": None})
        for key in ("flavor", "portion", "consumed", "intake_confirmed"):
            if block.get(key) is not None:
                facts.append({"path": f"data.{section}.{key}", "value": block.get(key),
                              "unit": None, "note": "consumed_requires_explicit_evidence"})
    return facts


def _generic_facts(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """未知结构：按原路径保真呈现（一层键 + 标量），不套任何模板。"""
    data = _mapping(event.get("data"))
    facts: list[dict[str, Any]] = []
    for section, block in data.items():
        if section in DOMAIN_SECTIONS or not isinstance(block, Mapping):
            continue
        for key, value in block.items():
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                facts.append({"path": f"data.{section}.{key}", "value": value, "unit": None})
    return facts


def _domain_facts(event: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "fitness":
        return _fitness_facts(event)
    if kind == "game":
        return _game_facts(event)
    if kind == "meal":
        return _meal_facts(event)
    return _generic_facts(event)


def _instance_refs(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """结构化实例键（订单/交易）——**只引用原文**，不做任何比较或合并。"""
    data = _mapping(event.get("data"))
    evidence = _mapping(event.get("evidence"))
    refs: list[dict[str, Any]] = []
    for block, prefix in ((data, "data"), (evidence, "evidence")):
        for key in ("order_id", "transaction_id", "receipt_id", "payment_id", "instance_id"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                refs.append({"namespace": key, "id": value.strip(), "path": f"{prefix}.{key}"})
    return refs


def _ledger_refs(event: Mapping[str, Any], ledger_index: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    event_id = _text(event.get("event_id"))
    entry = ledger_index.get(event_id)
    if entry is None:
        return []
    try:
        amount = float(entry.get("amount"))
    except (TypeError, ValueError):
        amount = None
    return [{
        "event_id": event_id,
        "amount": amount,
        "currency": _text(entry.get("currency")) or "CNY",
        "role": "ledger",
        "source": "expense_ledger",
        "amount_policy": "reference_only_no_sum",
    }]


def _card_id(event_id: str) -> str:
    return "card_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:12]


def build_activity_cards(
    events: Iterable[Mapping[str, Any]],
    *,
    ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """每条事件一张卡：呈现已有事实，不合并、不推断归属。纯函数。"""
    index: dict[str, Mapping[str, Any]] = {}
    if isinstance(ledger, Mapping):
        for key in ("transactions", "payment_transactions", "all_transactions"):
            rows = ledger.get(key)
            if isinstance(rows, (list, tuple)):
                for row in rows:
                    if isinstance(row, Mapping) and row.get("event_id"):
                        index.setdefault(_text(row.get("event_id")), row)

    cards: list[dict[str, Any]] = []
    alias: dict[str, str] = {}
    for event in events:
        event_id = _text(event.get("event_id"))
        if not event_id:
            continue
        kind = _kind_of(event)
        state, state_basis = _state_of(event)
        time_view, time_sources = _time_view(event)
        duration, duration_source = _duration_of(event)
        location, location_source = _location_of(event)
        facts = _domain_facts(event, kind)
        instances = _instance_refs(event)
        ledger_refs = _ledger_refs(event, index)

        field_sources = {
            "title": "event.title",
            "kind": "categories/activity_type",
            "state": state_basis.get("path"),
            "time": time_sources.get("time"),
            "duration": duration_source,
            "location": location_source,
            "facts": sorted({fact["path"] for fact in facts}) or None,
            "instances": [ref["path"] for ref in instances] or None,
            "ledger": "expense_ledger(event_id)" if ledger_refs else None,
        }
        unknown_fields = [
            name for name, value in (
                ("duration", duration),
                ("location", location),
            ) if value is None
        ]
        if state == "unknown":
            unknown_fields.insert(0, "state")
        if not facts:
            unknown_fields.append("domain_facts")
        if not time_view["occurred_at"] and not time_view["reported_at"]:
            unknown_fields.append("time")

        resolved_title, title_source = _workout_title(event, kind)
        card = {
            "card_id": _card_id(event_id),
            "source_event_id": event_id,
            "kind": kind,
            "title": resolved_title or None,
            "title_original": _text(event.get("title")) or None,
            "title_source": title_source,
            "retrospective": bool(event.get("retrospective")),
            "review_status": _text(event.get("review_status")) or None,
            "pending_reason": _text(event.get("pending_reason")) or None,
            "time_basis": time_view.get("basis") if isinstance(time_view, dict) else None,
            "state": state,
            "state_basis": state_basis,
            "time": time_view,
            "duration": duration,
            "location": location,
            "facts": facts,
            "instances": instances,
            "transactions": ledger_refs,
            "evidence": {
                "event_id": event_id,
                "recording_id": _mapping(event.get("evidence")).get("recording_id"),
                "segment_id": _mapping(event.get("evidence")).get("segment_id"),
                "source_id": _mapping(event.get("evidence")).get("source_id"),
                "source_label": _mapping(event.get("evidence")).get("source_label"),
                "reported_at": time_view["reported_at"],
                "extractor": _mapping(event.get("evidence")).get("extractor"),
                "quote": _mapping(event.get("evidence")).get("quote"),
                "start_offset_seconds": _mapping(event.get("evidence")).get("start_offset_seconds"),
                "end_offset_seconds": _mapping(event.get("evidence")).get("end_offset_seconds"),
            },
            "field_sources": field_sources,
            "unknown_fields": sorted(set(unknown_fields)),
            "identity_scope": "single_event_only",
        }
        cards.append(card)
        alias[event_id] = card["card_id"]

    return {
        "schema": SCHEMA,
        "cards": cards,
        "alias": alias,
        "stats": {
            "input_events": len(alias),
            "cards": len(cards),
            "by_kind": {kind: sum(1 for card in cards if card["kind"] == kind)
                        for kind in sorted({card["kind"] for card in cards})},
            "by_state": {state: sum(1 for card in cards if card["state"] == state)
                         for state in STATES},
            "with_transactions": sum(1 for card in cards if card["transactions"]),
        },
        "contract": {
            "schema": SCHEMA,
            "granularity": "one canonical event per card；不合并、不推断跨事件归属",
            "identity": "source_event_id 原样保留；card_id 只是该事件的本地视图键，不是活动实体 ID",
            "states": "done/in_progress/planned/cancelled/unknown；unknown = 没提到，不是没做",
            "duration": "只认显式结构化字段；录音跨度不算时长",
            "facts": "只从结构化 data 白名单读取；不从引文解析新实体或数字",
            "money": "transactions 仅引用 expense_ledger（reference_only_no_sum），不改账、不求和",
            "unknowns": "无法确定的字段列入 unknown_fields，未知结构按原路径保真呈现",
            "wiring": "接线时直接把事件列表 + expense_ledger 传入；前端只读，不自算",
        },
    }
