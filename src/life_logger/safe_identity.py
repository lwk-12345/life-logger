"""证据身份 · 保守切片（identity 一律 unknown；禁止可伪造 origin 升级）

设计前提（源于 v1.2/v1.3 独验结论）：
  * 带外 provenance 没有可信写入/签名，digest 可伪造；
  * 因此**任何** in-band 字段（origin / source / source_label / user_confirmed / digest /
    voice_similarity / identity）都**不得**把"我"升格为已确认；
  * 用户明确圈选的 9 条「这是我在看直播」只做**归类**（媒体），不推本人。

本模块能够保证的三件事：
  1. `identity` 恒为 `"unknown"`，没有任何分支会输出 self/非 self；
  2. 原始 ASR 标记只作为**候选**记录在 `candidate_markers` 里，带 `candidate_only: true`；
  3. 原话 / 交易 / 全部条目**逐字保留**，只新增键。

失败可见：`annotate_payload` 对非法输入抛 `SafeIdentityError`；`safe_annotate` 不吞异常，
由调用方决定记录 warning（见 wiring.patch）。
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

SCHEMA = "evidence-safe-identity-v1"
IDENTITY_UNKNOWN = "unknown"

#: 这些字段**只能**当候选，永远不能作为身份结论。
CANDIDATE_FIELDS: tuple[str, ...] = (
    "origin", "source", "source_label", "identity", "user_confirmed",
    "self_confirmed", "voice_similarity", "digest", "provenance",
)

#: 即使出现这些值，也只记录为 candidate，不改变 identity。
_STRONG_LOOKING_VALUES = {
    "user_text", "typed", "user_authored", "manual", "user_confirmed",
    "self", "self_confirmed", "verified",
}


class SafeIdentityError(ValueError):
    """非法载荷 / 切片被误用时的显式错误（绝不静默降级）。"""


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _candidate_markers(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """只收集、不判断：把 in-band 的身份类声明登记为候选。"""
    out: list[dict[str, Any]] = []
    for field in CANDIDATE_FIELDS:
        if field not in record:
            continue
        raw = record.get(field)
        if raw is None or raw == "":
            continue
        value = raw if isinstance(raw, (str, int, float, bool)) else "<non-scalar>"
        text = _as_text(raw).lower()
        out.append({
            "field": field,
            "value": value,
            "looks_authoritative": bool(text in _STRONG_LOOKING_VALUES) or raw is True,
            "usable": False,          # ← 永远不可用
        })
    return out


def classify_identity(record: Any) -> dict[str, Any]:
    """单条证据的身份结论：恒为 unknown。"""
    if not isinstance(record, Mapping):
        raise SafeIdentityError(f"evidence record must be a mapping, got {type(record).__name__}")
    markers = _candidate_markers(record)
    return {
        "schema": SCHEMA,
        "identity": IDENTITY_UNKNOWN,
        "candidate_only": True,
        "candidate_markers": markers,
        "reason": "in_band_markers_are_untrusted" if markers else "no_identity_marker",
    }


def _iter_items(payload: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for activity in payload.get("activities") or []:
        if not isinstance(activity, Mapping):
            continue
        aid = _as_text(activity.get("activity_id"))
        for item in activity.get("items") or []:
            if isinstance(item, Mapping):
                yield aid, item
    for item in payload.get("unassociated_items") or []:
        if isinstance(item, Mapping):
            yield "", item


def _corrections_by_id(corrections: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for key, value in (corrections or {}).items():
        if isinstance(value, Mapping):
            out[str(key)] = value
    return out


def annotate_payload(payload: Any, *, corrections: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """就地注解：只加键，不改任何既有字段。返回统计。"""
    if not isinstance(payload, dict):
        raise SafeIdentityError(f"payload must be a dict, got {type(payload).__name__}")

    corr = _corrections_by_id(corrections)
    stats = {"items": 0, "refs": 0, "identity_unknown": 0, "identity_other": 0,
             "candidate_markers": 0, "media_reclassified": 0, "activities": 0}

    for activity in payload.get("activities") or []:
        if not isinstance(activity, Mapping):
            continue
        stats["activities"] += 1
        aid = _as_text(activity.get("activity_id"))
        media_reclassified = aid in corr and _as_text((corr[aid] or {}).get("speech_target")).lower().find("媒体") >= 0

    for aid, item in _iter_items(payload):
        stats["items"] += 1
        item_media = aid in corr and "媒体" in _as_text((corr[aid] or {}).get("speech_target"))
        if item_media:
            stats["media_reclassified"] += 1

        refs_out: list[dict[str, Any]] = []
        for ref in item.get("evidence") or []:
            if not isinstance(ref, Mapping):
                continue
            stats["refs"] += 1
            verdict = classify_identity(ref)
            stats["candidate_markers"] += len(verdict["candidate_markers"])
            stats["identity_unknown"] += 1
            refs_out.append({
                "identity": verdict["identity"],
                "candidate_only": True,
                "candidate_markers": verdict["candidate_markers"],
                "reason": verdict["reason"],
            })

        # 子项级：identity 同样恒 unknown；媒体归类只作 scope 记录，不推本人
        item["identity_safe"] = {
            "schema": SCHEMA,
            "identity": IDENTITY_UNKNOWN,
            "candidate_only": True,
            "media_reclassified_only": bool(item_media),
            "refs": refs_out,
        }

    payload["safe_identity_schema"] = SCHEMA
    payload["safe_identity_summary"] = {
        "identity_policy": "always_unknown",
        "activities": stats["activities"],
        "items": stats["items"],
        "refs": stats["refs"],
        "identity_unknown": stats["identity_unknown"],
        "candidate_markers": stats["candidate_markers"],
        "media_reclassified_only": stats["media_reclassified"],
    }
    return stats


def find_forbidden_assertions(node: Any, path: str = "") -> list[str]:
    """守卫：若任何层出现“已确认本人/非本人”的结论，返回违规路径。

    只检查**结论性字段**（identity / self_confirmed / is_self / confirmed_self），
    不检查候选字段（origin/source_label 等，它们本就被允许存在）。
    """
    hits: list[str] = []
    conclusion_keys = {"identity", "self_confirmed", "is_self", "confirmed_self", "identity_status"}
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in conclusion_keys and isinstance(value, str):
                # 允许 unknown / empty；其余一律视为违规结论（除 candidate_markers 内部）
                if "/candidate_markers/" in here:
                    continue
                if value.strip().lower() not in {"unknown", "", "none"}:
                    hits.append(f"{here}={value!r}")
            hits.extend(find_forbidden_assertions(value, here))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(find_forbidden_assertions(value, f"{path}[{i}]"))
    return hits


def verify_conservation(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, bool]:
    """原话 / 交易 / 条目守恒（逐条比对）。"""
    def quotes(pl: Mapping[str, Any]) -> list[str]:
        return [str(e.get("quote"))
                for a in pl.get("activities") or [] for i in a.get("items") or []
                for e in i.get("evidence") or []]

    def txns(pl: Mapping[str, Any]) -> list[str]:
        import json
        return [f"{a.get('activity_id')}::{json.dumps(a.get('transactions') or [], sort_keys=True, ensure_ascii=False)}"
                for a in pl.get("activities") or []]

    def ids(pl: Mapping[str, Any]) -> tuple:
        return (
            [a.get("activity_id") for a in pl.get("activities") or []],
            [[i.get("item_id") for i in a.get("items") or []] for a in pl.get("activities") or []],
            len(pl.get("unassociated_items") or []),
        )

    return {
        "quotes_preserved": quotes(before) == quotes(after),
        "transactions_preserved": txns(before) == txns(after),
        "ids_preserved": ids(before) == ids(after),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 展示投影：旧身份字段一律降级为 unknown（原始数据不动，只改“给用户看的副本”）
# ─────────────────────────────────────────────────────────────────────────────
LEGACY_IDENTITY_FIELDS: tuple[str, ...] = (
    "identity", "identity_status", "self_confirmed", "is_self", "confirmed_self",
)

_NEUTRAL_VALUE = {
    "identity": IDENTITY_UNKNOWN,
    "identity_status": IDENTITY_UNKNOWN,
    "self_confirmed": False,
    "is_self": False,
    "confirmed_self": False,
}

_FALSY_OK = (None, "", False, 0, "unknown", "none", "null")


def _neutralise_tree(node: Any, path: str, claims: list[dict[str, Any]]) -> int:
    """就地清理**投影副本**里的旧身份字段；返回被降级的字段数。"""
    if not isinstance(node, dict):
        return 0
    changed = 0
    for field_name in LEGACY_IDENTITY_FIELDS:
        if field_name not in node:
            continue
        value = node[field_name]
        suspicious = value not in _FALSY_OK
        if suspicious:
            claims.append({"path": path, "field": field_name, "value": value})
        if node[field_name] != _NEUTRAL_VALUE[field_name]:
            node[field_name] = _NEUTRAL_VALUE[field_name]
            changed += 1
    return changed


def _guard_view(node: Any) -> dict[str, Any]:
    """守卫：异常必须显式变成 unknown，绝不静默通过。"""
    try:
        violations = find_forbidden_assertions(node)
    except Exception as exc:  # noqa: BLE001 - 守卫自身失败也必须可见
        return {"status": IDENTITY_UNKNOWN, "violations": [],
                "error": f"{type(exc).__name__}: {exc}"}
    if violations:
        return {"status": IDENTITY_UNKNOWN, "violations": violations[:10], "error": None}
    return {"status": "ok", "violations": [], "error": None}


def project_for_display(payload: Any, *, corrections: Mapping[str, Any] | None = None,
                        max_claims: int = 20) -> dict[str, Any]:
    """生成**给用户看的投影副本**。

    - 不修改入参（deepcopy）；证据原话 / 交易 / 用户纠正**逐字保留**；
    - 旧 `identity / self_confirmed / is_self / identity_status` 一律降级为 unknown，
      原值记录到 `legacy_identity_claims` 供审计；
    - 守卫执行失败或发现违规 → `status="unknown"`（显式，不静默）。
    """
    if not isinstance(payload, dict):
        raise SafeIdentityError(f"payload must be a dict, got {type(payload).__name__}")

    view = copy.deepcopy(payload)
    annotate_payload(view, corrections=corrections)

    claims: list[dict[str, Any]] = []
    neutralised = _neutralise_tree(view, "$", claims)
    for index, activity in enumerate(view.get("activities") or []):
        if not isinstance(activity, dict):
            continue
        aid = str(activity.get("activity_id") or f"#{index}")
        neutralised += _neutralise_tree(activity, f"activities/{aid}", claims)
        for item_index, item in enumerate(activity.get("items") or []):
            if not isinstance(item, dict):
                continue
            iid = str(item.get("item_id") or f"#{item_index}")
            neutralised += _neutralise_tree(item, f"activities/{aid}/items/{iid}", claims)
            for ref_index, ref in enumerate(item.get("evidence") or []):
                if isinstance(ref, dict):
                    neutralised += _neutralise_tree(
                        ref, f"activities/{aid}/items/{iid}/evidence[{ref_index}]", claims)
    for index, item in enumerate(view.get("unassociated_items") or []):
        if isinstance(item, dict):
            iid = str(item.get("item_id") or f"#{index}")
            neutralised += _neutralise_tree(item, f"unassociated_items/{iid}", claims)
            for ref_index, ref in enumerate(item.get("evidence") or []):
                if isinstance(ref, dict):
                    neutralised += _neutralise_tree(
                        ref, f"unassociated_items/{iid}/evidence[{ref_index}]", claims)

    guard = _guard_view(view)
    view["safe_identity_projection"] = {
        "schema": SCHEMA,
        "identity_policy": "unknown_unless_explicitly_verified",
        "status": guard["status"],
        "guard": guard,
        "legacy_fields_neutralised": neutralised,
        "legacy_identity_claims": claims[:max_claims],
        "legacy_identity_claims_total": len(claims),
        "note": "原始数据未删除；本对象是展示投影，旧身份声明已降级为 unknown",
    }
    return view
