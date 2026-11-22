"""活动事实结构 v3 —— 唯一定义处（不接线上）。

设计稿：`docs/architecture/fact-schema-v3.md`。本模块只做三件事：
1. 用 Python 常量描述结构与枚举（提示词、校验、JSON Schema 都从这里生成）；
2. `json_schema()` 产出可直接放进 `response_format` 的 schema；
3. `validate_payload()` 严格校验：枚举外、类型错、必填缺失都返回结构化错误，**不抛异常**，
   由调用方决定记录还是拒绝。

约定：
* 只校验结构，不判断业务（金额是否可信、证据是否逐字，由上层做）。
* `unknown` 是合法值；不在枚举里的值一律报 `enum_out_of_range`。
* 未声明的额外字段 → `unknown_field`（v3 明确删掉了 unit / location / amount_kind 等平铺字段）。
"""
from __future__ import annotations

import copy
import math
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 3
ROOT_FIELDS = {"schema_version": "integer", "activities": "array"}
DATE_LENGTH = 10
STRING_MAX_LENGTHS = {"activity": {"title": 20}, "item": {"action": 40, "occurred_date": DATE_LENGTH}}
CONFIDENCE_RANGE = (0.0, 1.0)
MIN_ACTIVITY_ITEMS = 1
MIN_EVIDENCE_ITEMS = 1
LINE_REFERENCE_FIELD = "line_id"
LINE_ID_PREFIX = "L"
LINE_ID_WIDTH = 3

STATUS_ENUM = ("planned", "ongoing", "completed", "cancelled", "unknown")
LIFECYCLE_ENUM = ("live", "media", "unknown")
CATEGORY_ENUM = (
    "工作·项目", "运动·健身", "饮食", "消费·购物", "出行",
    "社交·通话", "健康·作息", "家务·琐事", "闲聊·其他", "媒体内容",
)
FACT_TYPE_ENUM = ("expense", "food", "exercise", "smoke", "todo", "idea", "other")
MEAL_ENUM = ("breakfast", "lunch", "dinner", "snack", "unknown")
PAYMENT_STATUS_ENUM = ("paid", "unpaid", "unknown")
WEIGHT_UNIT_ENUM = ("kg", "lb")
SIDE_ENUM = ("per_side", "total", "unknown")
SMOKE_KIND_ENUM = ("smoked", "bought", "urge")

#: 活动层允许的键（多一个都算 unknown_field）
ACTIVITY_FIELDS = {
    "activity_id": "string",
    "title": "string",
    "summary": "string",
    "status": "enum:status",
    "lifecycle": "enum:lifecycle",
    "category": "enum:category",
    "start_at": "iso|null",
    "end_at": "iso|null",
    "items": "array",
}

ITEM_FIELDS = {
    "item_id": "string",
    "action": "string",
    "fact_type": "enum:fact_type",
    "status": "enum:status",
    "spoken_at": "iso|null",
    "occurred_at": "iso|null",
    "occurred_date": "date|null",
    "correction_of": "string|null",
    "restatement_of": "string|null",
    "confidence": "number",
    "reason": "string",
    "evidence": "array",
    "expense": "object|null",
    "food": "object|null",
    "exercise": "object|null",
    "smoke": "object|null",
    "todo": "object|null",
}

EXPENSE_FIELDS = {
    # [2026-11-08] 允许 null：本人现场点单/拿货、但录音没录清金额时必须能产出
    # `fact_type="expense"` + `amount: null`（由用户在待确认区补填），
    # 否则模型只能丢弃或错标成 other，真消费就整条消失。
    "amount": "number|null",
    "currency": "string",
    "item": "string",
    "payment_status": "enum:payment_status",
}
FOOD_FIELDS = {
    "name": "string",
    "meal": "enum:meal",
    "grams": "number|null",
    "portion": "string|null",
}
EXERCISE_FIELDS = {
    "name": "string",
    "sets": "integer|null",
    "reps": "integer|null",
    "weight_value": "number|null",
    "weight_unit": "enum:weight_unit|null",
    "side": "enum:side",
    "duration_seconds": "number|null",
}
SMOKE_FIELDS = {
    "count": "integer|null",
    "brand": "string|null",
    "kind": "enum:smoke_kind",
}
TODO_FIELDS = {"text": "string", "due": "string|null"}
EVIDENCE_FIELDS = {"source_id": "string", "quote": "string"}

_ENUM_BY_NAME = {
    "status": STATUS_ENUM,
    "lifecycle": LIFECYCLE_ENUM,
    "category": CATEGORY_ENUM,
    "fact_type": FACT_TYPE_ENUM,
    "meal": MEAL_ENUM,
    "payment_status": PAYMENT_STATUS_ENUM,
    "weight_unit": WEIGHT_UNIT_ENUM,
    "side": SIDE_ENUM,
    "smoke_kind": SMOKE_KIND_ENUM,
}

_FIELD_SETS = {
    "expense": EXPENSE_FIELDS,
    "food": FOOD_FIELDS,
    "exercise": EXERCISE_FIELDS,
    "smoke": SMOKE_FIELDS,
    "todo": TODO_FIELDS,
}


def enum_values(name: str) -> tuple[str, ...]:
    return _ENUM_BY_NAME[name]


# --------------------------------------------------------------------------- JSON Schema


def _type_schema(kind: str) -> dict[str, Any]:
    kind = kind.replace("|null", "") if kind.startswith("enum:") else kind
    if kind.startswith("enum:"):
        return {"type": "string", "enum": list(enum_values(kind.split(":", 1)[1]))}
    return {
        "string": {"type": "string"},
        "number": {"type": "number"},
        "integer": {"type": "integer"},
        "array": {"type": "array"},
        "object": {"type": "object"},
        "iso|null": {"type": ["string", "null"]},
        "date|null": {"type": ["string", "null"], "minLength": DATE_LENGTH, "maxLength": DATE_LENGTH},
        "string|null": {"type": ["string", "null"]},
        "number|null": {"type": ["number", "null"]},
        "integer|null": {"type": ["integer", "null"]},
        "object|null": {"type": ["object", "null"]},
    }[kind]


def _object_schema(fields: Mapping[str, str], *, strict: bool = True, scope: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {name: _type_schema(kind) for name, kind in fields.items()},
        "required": [name for name in fields],
    }
    for name, limit in STRING_MAX_LENGTHS.get(scope, {}).items():
        schema["properties"][name]["maxLength"] = limit
    if scope == "item":
        schema["properties"]["confidence"].update(minimum=CONFIDENCE_RANGE[0], maximum=CONFIDENCE_RANGE[1])
    if strict:
        schema["additionalProperties"] = False
    return schema


def json_schema() -> dict[str, Any]:
    """可直接用于 `response_format={"type":"json_schema","json_schema":{...}}`。"""
    item_schema = _object_schema(ITEM_FIELDS, scope="item")
    item_schema["properties"]["evidence"] = {
        "type": "array", "minItems": MIN_EVIDENCE_ITEMS,
        "items": _object_schema(EVIDENCE_FIELDS),
    }
    for key, fields in _FIELD_SETS.items():
        item_schema["properties"][key] = {"anyOf": [_object_schema(fields), {"type": "null"}]}
    activity_schema = _object_schema(ACTIVITY_FIELDS, scope="activity")
    activity_schema["properties"]["items"] = {"type": "array", "minItems": MIN_ACTIVITY_ITEMS, "items": item_schema}
    return _object_schema(ROOT_FIELDS) | {
        "properties": {
            "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
            "activities": {"type": "array", "items": activity_schema},
        }
    }


def line_id(index: int) -> str:
    """One-based request-local reference; never a persistent activity identity."""
    return f"{LINE_ID_PREFIX}{index:0{LINE_ID_WIDTH}d}"


def request_json_schema(line_ids: Iterable[str]) -> dict[str, Any]:
    """Model wire format; archived canonical facts still use source_id + quote."""
    schema = json_schema()
    evidence = schema["properties"]["activities"]["items"]["properties"]["items"]["items"]["properties"]["evidence"]
    evidence["items"] = {
        "type": "object", "additionalProperties": False,
        "required": [LINE_REFERENCE_FIELD],
        "properties": {LINE_REFERENCE_FIELD: {"type": "string", "enum": list(line_ids)}},
    }
    return schema


# --------------------------------------------------------------------------- 校验


def _is_iso(value: str) -> bool:
    from datetime import datetime

    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and (
        isinstance(value, int) or isinstance(value, float) and math.isfinite(value)
    )


def _check_field(where: str, name: str, kind: str, value: Any, errors: list[dict[str, Any]]) -> None:
    if kind.endswith("|null") and value is None:
        return
    base = kind.split("|", 1)[0]
    if base.startswith("enum:"):
        allowed = enum_values(base.split(":", 1)[1])
        if value not in allowed:
            errors.append({"where": where, "field": name, "error": "enum_out_of_range",
                           "value": value, "allowed": list(allowed)})
        return
    if base == "iso" and isinstance(value, str) and not _is_iso(value):
        errors.append({"where": where, "field": name, "error": "time_format", "value": value})
        return
    ok = {
        "string": lambda v: isinstance(v, str),
        "number": _finite_number,
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, Mapping),
        "iso": lambda v: isinstance(v, str) and _is_iso(v),
        "date": lambda v: isinstance(v, str) and len(v) == DATE_LENGTH and v[4] == "-",
    }.get(base)
    if ok is None:
        return
    if not ok(value):
        errors.append({"where": where, "field": name, "error": "type_error",
                       "expected": kind, "value": value})


def _check_object(where: str, fields: Mapping[str, str], value: Any, errors: list[dict[str, Any]], *, scope: str = "") -> None:
    if not isinstance(value, Mapping):
        errors.append({"where": where, "field": "", "error": "type_error",
                       "expected": "object", "value": value})
        return
    for name, kind in fields.items():
        if name not in value:
            errors.append({"where": where, "field": name, "error": "missing_field"})
            continue
        limit = STRING_MAX_LENGTHS.get(scope, {}).get(name)
        if limit is not None and isinstance(value[name], str) and len(value[name]) > limit:
            errors.append({"where": where, "field": name, "error": "field_too_long",
                           "maximum": limit, "actual_length": len(value[name])})
            continue
        _check_field(where, name, kind, value[name], errors)
        if scope == "item" and name == "confidence" and _finite_number(value[name]):
            if not CONFIDENCE_RANGE[0] <= value[name] <= CONFIDENCE_RANGE[1]:
                errors.append({"where": where, "field": name, "error": "out_of_range", "value": value[name]})
    for extra in value:
        if extra not in fields:
            errors.append({"where": where, "field": extra, "error": "unknown_field",
                           "value": value[extra]})


def validate_payload(payload: Any, *, confidence_threshold: float | None = None) -> dict[str, Any]:
    """校验 v3 payload，返回 ``{"ok": bool, "errors": [...], "counts": {...}}``（不抛异常）。"""
    errors: list[dict[str, Any]] = []
    counts = {"activities": 0, "items": 0, "errors": 0}
    if not isinstance(payload, Mapping):
        errors.append({"where": "$", "field": "", "error": "type_error", "expected": "object"})
        return {"ok": False, "errors": errors, "counts": {**counts, "errors": len(errors)}}
    _check_object("$", ROOT_FIELDS, payload, errors)
    if confidence_threshold is not None and (
        not _finite_number(confidence_threshold) or not 0 <= confidence_threshold <= 1
    ):
        errors.append({"where": "$config", "field": "confidence_threshold", "error": "out_of_range"})
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append({"where": "$", "field": "schema_version", "error": "version_mismatch",
                       "value": payload.get("schema_version")})
    activities = payload.get("activities")
    if not isinstance(activities, list):
        errors.append({"where": "$", "field": "activities", "error": "type_error", "expected": "array"})
        counts["errors"] = len(errors)
        return {"ok": False, "errors": errors, "counts": counts}

    counts["activities"] = len(activities)
    for ai, activity in enumerate(activities):
        where = f"$.activities[{ai}]"
        _check_object(where, ACTIVITY_FIELDS, activity, errors, scope="activity")
        if not isinstance(activity, Mapping):
            continue
        items = activity.get("items")
        if not isinstance(items, list):
            continue
        counts["items"] += len(items)
        for ii, item in enumerate(items):
            iwhere = f"{where}.items[{ii}]"
            _check_object(iwhere, ITEM_FIELDS, item, errors, scope="item")
            if not isinstance(item, Mapping):
                continue
            for key, fields in _FIELD_SETS.items():
                value = item.get(key)
                if value is None:
                    continue
                _check_object(f"{iwhere}.{key}", fields, value, errors)
            evidence = item.get("evidence")
            if isinstance(evidence, list):
                for ei, ev in enumerate(evidence):
                    _check_object(f"{iwhere}.evidence[{ei}]", EVIDENCE_FIELDS, ev, errors)
                if not evidence:
                    errors.append({"where": iwhere, "field": "evidence", "error": "empty_evidence"})
    soft = ("field_too_long", "empty_evidence", "time_format")
    warnings = [error for error in errors if error["error"] in soft]
    errors = [error for error in errors if error["error"] not in soft]
    confirmed, non_confirmed = _partition(payload, warnings, confidence_threshold if not errors else None)
    counts.update(errors=len(errors), non_confirmed=len(non_confirmed),
                  accepted_items=sum(len(a["items"]) for a in confirmed["activities"]))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts,
            "confirmed_payload": confirmed if not errors else None, "non_confirmed": non_confirmed,
            "confidence_gate": {"configured": confidence_threshold is not None, "threshold": confidence_threshold}}


def _partition(payload, warnings, threshold):
    """Hold whole original items without truncation; never alter the raw response."""
    confirmed = {"schema_version": SCHEMA_VERSION, "activities": []}
    pending = []
    for ai, activity in enumerate(payload["activities"]):
        if not isinstance(activity, Mapping) or not isinstance(activity.get("items"), list):
            continue
        awhere = f"$.activities[{ai}]"
        header = [w for w in warnings if w["where"] == awhere]
        if not activity["items"]:
            pending.append({"reason": "no_evidence", "fields": copy.deepcopy(header),
                            "activity_id": activity.get("activity_id"), "activity": copy.deepcopy(activity), "item": None})
            continue
        accepted = []
        for ii, item in enumerate(activity["items"]):
            if not isinstance(item, Mapping):
                continue
            fields = header + [w for w in warnings if w["where"] == f"{awhere}.items[{ii}]"]
            if isinstance(item.get("evidence"), list) and not item["evidence"]:
                pending.append({"reason": "no_evidence", "fields": copy.deepcopy(fields),
                                "activity_id": activity.get("activity_id"),
                                "activity": copy.deepcopy({k: v for k, v in activity.items() if k != "items"}),
                                "item": copy.deepcopy(item)})
                continue
            score = item.get("confidence")
            low = threshold is not None and _finite_number(score) and score < threshold
            unknown_expense = activity.get("lifecycle") == "unknown" and item.get("fact_type") == "expense" and bool(item.get("expense"))
            if fields or low or unknown_expense:
                reason = ("time_format" if any(f["error"] == "time_format" for f in fields) else
                          "field_too_long" if fields else "unknown_lifecycle" if unknown_expense else "low_confidence")
                pending.append({"reason": reason,
                    "fields": copy.deepcopy(fields), "activity_id": activity.get("activity_id"),
                    "activity": copy.deepcopy({k: v for k, v in activity.items() if k != "items"}),
                    "item": copy.deepcopy(item)})
            else:
                accepted.append(copy.deepcopy(item))
        if accepted:
            confirmed["activities"].append({**copy.deepcopy(activity), "items": accepted})
    return confirmed, pending


def summarize_errors(errors: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """按 error 类型计数，写报告用。"""
    out: dict[str, int] = {}
    for item in errors:
        key = str(item.get("error") or "?")
        out[key] = out.get(key, 0) + 1
    return out
