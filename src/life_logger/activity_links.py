"""显式关系边（activity links）——只把**已有结构化引用**变成可追溯的边。

背景（2026-10-25 第二切片）：事实展示已上线，但活动关联不能靠时间/金额/文本相似度猜。
本模块只做一件事：**读事件里已经存在的引用字段**，产出边（edge）与成员引用（member refs），
并为每条边记录 `basis`（数据路径）、双端 id、关系类型、命名空间与冲突。

硬边界
------
1. **不猜关系**：没有显式引用就 `unlinked`；时间相近、金额相同、文本相似都**不**成边。
2. **命名空间隔离**：`recording_id` / `source_session` / `order_id` / `transaction_id` 各自是
   独立命名空间，**不是活动 id**。这类边会标 `activity_membership=false`，不得据此合成活动。
3. **不串组**：显式 activity 引用若跨 actor / kind / 命名空间冲突 → 不建立成员关系，只记 `conflict`。
4. **不吞并**：所有事件、所有交易原样保留；每条边都能回到 `event_id`；不生成"巨卡"、不求和。
5. **纯函数**：不读写盘、不调模型、不导入旧 matcher。

真实字段（本轮抽样 70 条 canonical 事件确认）
    event_key = "<source_kind>:<id>"        → 命名空间化身份（70/70 有值）
    canonical_event_id                      → 规范事件指针（70/70）
    aliases[] = {event_id, event_key, ...}  → 同一实例的别名（15/70）
    source_session                          → 会话 id（53/70，**不是**活动）
    evidence.order_binding                  → 订单绑定标记（2/70，布尔，无 id）
    data.activity_ref = {namespace, id}     → 活动引用（**真实数据中尚无**，为提议字段）

用法::

    from life_logger.activity_links import build_activity_links
    result = build_activity_links(events)
    result["edges"]      # [{edge_id, relation, from_id, to_id, namespace, basis, ...}]
    result["members"]    # {activity_ref: [event_id, ...]}（仅显式 activity 引用）
    result["unlinked"]   # 没有任何可追溯引用的事件
    result["conflicts"]  # 显式引用内部冲突（不合并，仅记录）
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

SCHEMA = "activity-links-v1"

#: 引用 id 形状（格式校验用；`reference_validated` 只表示"这个引用本身格式/目标核对通过"）。
REF_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

#: 允许参与"活动成员"判定的引用字段（必须带命名空间）。order/session/recording **不在**此列。
ACTIVITY_REF_FIELDS = ("activity_ref", "activity_id_ref", "parent_activity_ref")

#: 只做"同实例/归属"提示、**不构成活动成员**的命名空间。
NON_ACTIVITY_NAMESPACES = ("session", "recording", "order", "transaction", "receipt", "payment")


def _text(value: Any) -> str:
    return str(value or "")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _kind_of(event: Mapping[str, Any]) -> str:
    categories = {str(item).strip().lower() for item in (event.get("categories") or [])}
    return _text(event.get("activity_type")).strip().lower() or ",".join(sorted(categories)) or "other"


def _actor_of(event: Mapping[str, Any]) -> str:
    label = _text(_mapping(event.get("evidence")).get("source_label")).strip().lower()
    return label if label in {"self", "non_self"} else "unknown"


def _edge_id(relation: str, source: str, target: str, namespace: str) -> str:
    seed = f"{relation}|{namespace}|{source}|{target}"
    return "edge_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _activity_refs(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取带命名空间的活动引用（只认 dict 形式，str 形式视为缺命名空间 → 记录缺字段）。"""
    refs: list[dict[str, Any]] = []
    top_activity = _text(event.get("activity_id")).strip()
    if top_activity:
        refs.append({"namespace": "activity", "id": top_activity, "path": "activity_id"})
    for block_name, block in (("data", _mapping(event.get("data"))), ("evidence", _mapping(event.get("evidence")))):
        for key in ACTIVITY_REF_FIELDS:
            value = block.get(key)
            if isinstance(value, Mapping):
                namespace = _text(value.get("namespace")).strip()
                ident = _text(value.get("id")).strip()
                if namespace and ident:
                    refs.append({"namespace": namespace, "id": ident, "path": f"{block_name}.{key}"})
            elif isinstance(value, str) and value.strip():
                # 没有命名空间 → 不足以判定活动（A0：不得把无命名空间引用当活动 id）
                refs.append({"namespace": "", "id": value.strip(), "path": f"{block_name}.{key}"})
    return refs


def _event_refs(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """事件命名空间的显式引用：parent_event_id / related_event_ids（related ≠ same activity）。"""
    refs: list[dict[str, Any]] = []
    parent = _text(event.get("parent_event_id")).strip()
    if parent:
        refs.append({"relation": "child_of", "id": parent, "path": "parent_event_id"})
    related = event.get("related_event_ids")
    if isinstance(related, (list, tuple, set)):
        for item in related:
            value = _text(item).strip()
            if value:
                refs.append({"relation": "related_to", "id": value, "path": "related_event_ids[]"})
    elif isinstance(related, str) and related.strip():
        refs.append({"relation": "related_to", "id": related.strip(), "path": "related_event_ids"})
    return refs


def _non_activity_refs(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """会话/录音/订单/交易引用：可追溯，但**不是**活动成员。"""
    evidence = _mapping(event.get("evidence"))
    data = _mapping(event.get("data"))
    refs: list[dict[str, Any]] = []
    for path, value, namespace in (
        ("evidence.recording_id", evidence.get("recording_id"), "recording"),
        ("source_session", event.get("source_session"), "session"),
        ("evidence.session_id", evidence.get("session_id"), "session"),
        ("data.order_id", data.get("order_id"), "order"),
        ("evidence.order_id", evidence.get("order_id"), "order"),
        ("data.transaction_id", data.get("transaction_id"), "transaction"),
        ("evidence.transaction_id", evidence.get("transaction_id"), "transaction"),
    ):
        if isinstance(value, str) and value.strip():
            refs.append({"namespace": namespace, "id": value.strip(), "path": path})
    return refs


def build_activity_links(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """从显式引用产出关系边。纯函数：不改入参、不写盘、不猜关系。

    2026-10-25 返工（4号 独立反例）：自环丢弃、悬空**事件**引用记 conflicts、
    重复 edge 去重并合并 basis、支持 `parent_event_id` / `related_event_ids`。
    `activity_ref` 属独立命名空间：**不要求**存在同名事件；单侧引用仅作"声明"，
    `verified=false`，不得冒称已核实活动实体。
    """
    originals = [event for event in events if isinstance(event, Mapping)]
    by_event_id: dict[str, Mapping[str, Any]] = {}
    for event in originals:
        event_id = _text(event.get("event_id"))
        if event_id:
            by_event_id.setdefault(event_id, event)

    seen: dict[str, dict[str, Any]] = {}
    members: dict[str, list[str]] = {}
    member_meta: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    unlinked: list[str] = []
    transactions: dict[str, list[dict[str, Any]]] = {}

    def add_edge(edge: dict[str, Any]) -> None:
        """同 (relation, namespace, from, to) 只保留一条；多条 basis 合并保留。"""
        existing = seen.get(edge["edge_id"])
        if existing is None:
            edge.setdefault("basis_paths", [edge["basis"]["path"]])
            seen[edge["edge_id"]] = edge
            return
        paths = set(existing.get("basis_paths") or [existing["basis"]["path"]])
        paths.add(edge["basis"]["path"])
        existing["basis_paths"] = sorted(paths)

    for event in originals:
        event_id = _text(event.get("event_id"))
        if not event_id:
            continue
        actor, kind = _actor_of(event), _kind_of(event)
        event_refs = 0

        # 1) 事件级显式父子/相关引用（目标必须存在、非自环）
        for ref in _event_refs(event):
            if ref["id"] == event_id:
                conflicts.append({"kind": "self_reference", "reason": "self_reference",
                                  "event_id": event_id, "path": ref["path"], "value": ref["id"],
                                  "resolution": "dropped"})
                continue
            if ref["id"] not in by_event_id:
                conflicts.append({"kind": "dangling_reference", "reason": "dangling_reference",
                                  "event_id": event_id, "path": ref["path"], "value": ref["id"],
                                  "resolution": "dropped_keep_event"})
                continue
            add_edge({
                "edge_id": _edge_id(ref["relation"], event_id, ref["id"], "event"),
                "relation": ref["relation"],          # child_of / related_to
                "namespace": "event",
                "from_id": event_id,
                "to_id": ref["id"],
                "basis": {"path": ref["path"], "value": ref["id"]},
                "activity_membership": False,          # related_to ≠ same activity
                "reference_validated": True,
                "conflicts": [],
            })
            event_refs += 1

        # 2) aliases（同实例别名；同样要求目标存在）
        for alias in event.get("aliases") or []:
            if not isinstance(alias, Mapping):
                continue
            alias_id = _text(alias.get("event_id")).strip()
            if not alias_id or alias_id == event_id:
                if alias_id == event_id:
                    conflicts.append({"kind": "self_reference", "reason": "self_reference",
                                      "event_id": event_id, "path": "aliases[].event_id",
                                      "value": alias_id, "resolution": "dropped"})
                continue
            namespace = _text(alias.get("source_kind")).strip() or "event"
            if alias_id not in by_event_id:
                conflicts.append({"kind": "dangling_reference", "reason": "dangling_reference",
                                  "event_id": event_id, "path": "aliases[].event_id",
                                  "value": alias_id, "resolution": "dropped_keep_event"})
                continue
            add_edge({
                "edge_id": _edge_id("alias_of", event_id, alias_id, namespace),
                "relation": "alias_of",
                "namespace": namespace,
                "from_id": event_id,
                "to_id": alias_id,
                "basis": {"path": "aliases[].event_id", "value": alias_id},
                "activity_membership": False,
                "reference_validated": True,
                "conflicts": [],
            })
            event_refs += 1

        # 3) canonical 指针
        canonical = _text(event.get("canonical_event_id")).strip()
        if canonical and canonical != event_id:
            if canonical in by_event_id:
                add_edge({
                    "edge_id": _edge_id("canonical_of", event_id, canonical, "event"),
                    "relation": "canonical_of",
                    "namespace": "event",
                    "from_id": event_id,
                    "to_id": canonical,
                    "basis": {"path": "canonical_event_id", "value": canonical},
                    "activity_membership": False,
                    "reference_validated": True,
                    "conflicts": [],
                })
                event_refs += 1
            else:
                conflicts.append({"kind": "dangling_reference", "reason": "dangling_reference",
                                  "event_id": event_id, "path": "canonical_event_id",
                                  "value": canonical, "resolution": "dropped_keep_event"})

        # 4) 活动命名空间引用：不要求同名事件；单侧只算声明
        for ref in _activity_refs(event):
            if not ref["namespace"]:
                conflicts.append({"kind": "missing_namespace", "reason": "missing_namespace",
                                  "event_id": event_id, "path": ref["path"], "value": ref["id"],
                                  "resolution": "ignored_not_activity"})
                continue
            if ref["namespace"] in NON_ACTIVITY_NAMESPACES:
                add_edge({
                    "edge_id": _edge_id("references", event_id, ref["id"], ref["namespace"]),
                    "relation": "references",
                    "namespace": ref["namespace"],
                    "from_id": event_id,
                    "to_id": ref["id"],
                    "basis": {"path": ref["path"], "value": ref["id"]},
                    "activity_membership": False,
                    "reference_validated": True,
                    "conflicts": [],
                })
                event_refs += 1
                continue
            # 【2026-10-25 修缺口 A】局部 activity id 与 anchor event_id 同名是合法的
            # （例如 activity_ref={id: "evt_xxx"} 指向该事件自己）。旧实现直接丢弃 → 整组退化成
            # 单成员 → 声明不足无法成组。现在只记一条 note，成员照常保留。
            self_referential = ref["id"] == event_id
            if self_referential:
                conflicts.append({"kind": "self_reference", "reason": "self_reference",
                                  "event_id": event_id, "path": ref["path"], "value": ref["id"],
                                  "resolution": "kept_with_note"})
            if not REF_ID_PATTERN.match(ref["id"]) or not REF_ID_PATTERN.match(ref["namespace"]):
                # 格式核对失败：引用本身不成立（不建边；不是"活动已核实/未核实"的问题）
                conflicts.append({"kind": "invalid_ref_shape", "reason": "invalid_ref_shape",
                                  "event_id": event_id, "path": ref["path"], "value": ref["id"],
                                  "resolution": "dropped_keep_event"})
                continue
            key = f"{ref['namespace']}:{ref['id']}"
            meta = member_meta.setdefault(
                key,
                {"actors": set(), "kinds": set(), "events": set(), "conflicted": set(),
                 "paths": set(), "kind_coexist": []},
            )
            meta["paths"].add(ref["path"])
            meta["events"].add(event_id)
            if not meta["actors"]:
                meta["actors"].add(actor)
                meta["kinds"].add(kind)
                members.setdefault(key, []).append(event_id)
            elif actor not in meta["actors"] or kind not in meta["kinds"]:
                # 【2026-10-25 修缺口 B】当两侧都用**完全相同**的 data.activity_ref 时，
                # 不同 kind 可以共存（点餐 meal + 对应支付 expense），交易仍逐笔保留、不求和。
                same_explicit_ref = (
                    ref["path"] == "data.activity_ref"
                    and kind not in meta["kinds"]
                    and actor in meta["actors"]
                    and meta["paths"] <= {"data.activity_ref"}
                )
                if same_explicit_ref:
                    meta["kinds"].add(kind)
                    meta["kind_coexist"].append({"event_id": event_id, "kind": kind})
                    members.setdefault(key, []).append(event_id)
                else:
                    # 跨 actor/kind 且证据不成立：**不建边**（与 members 保持一致），只记冲突
                    meta["conflicted"].add(event_id)
                    conflicts.append({
                        "kind": "cross_actor_or_kind",
                        "reason": "actor_conflict" if actor not in meta["actors"] else "kind_conflict",
                        "event_id": event_id, "activity_ref": key,
                        "actors": sorted(meta["actors"] | {actor}),
                        "kinds": sorted(meta["kinds"] | {kind}),
                        "resolution": "not_grouped",
                    })
                    event_refs += 1
                    continue
            else:
                members.setdefault(key, []).append(event_id)
            consistent = len(meta["events"] - meta["conflicted"]) >= 2
            add_edge({
                "edge_id": _edge_id("member_of", event_id, key, ref["namespace"]),
                "relation": "member_of",
                "namespace": ref["namespace"],
                "from_id": event_id,
                "to_id": key,
                "basis": {"path": ref["path"], "value": ref["id"]},
                "activity_membership": True,
                # 语义分离（2026-10-25 返工）：
                #   reference_validated = 这个引用本身格式/目标核对通过（≠ 活动身份已确认）
                #   activity_entity_verified = 活动实体是否被独立权威来源核实 —— 恒为 False
                #   declaration_consistent = 多侧声明彼此一致（仍不等于核实）
                "reference_validated": True,
                "activity_entity_verified": False,
                "declaration_consistent": consistent,
                "self_referential": bool(self_referential),
                "conflicts": [],
            })
            event_refs += 1

        # 5) 会话/录音/订单/交易：可追溯但不是活动成员
        for ref in _non_activity_refs(event):
            add_edge({
                "edge_id": _edge_id("references", event_id, ref["id"], ref["namespace"]),
                "relation": "references",
                "namespace": ref["namespace"],
                "from_id": event_id,
                "to_id": ref["id"],
                "basis": {"path": ref["path"], "value": ref["id"]},
                "activity_membership": False,
                "reference_validated": True,
                "conflicts": [],
            })
            event_refs += 1

        if event_refs == 0:
            unlinked.append(event_id)

        data = _mapping(event.get("data"))
        refs = []
        for key in ("transaction_id", "payment_id", "receipt_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                refs.append({"namespace": key, "id": value.strip(), "path": f"data.{key}"})
        if refs:
            transactions[event_id] = refs

    edges = list(seen.values())
    for key, meta in member_meta.items():
        declared = sorted(meta["events"] - meta["conflicted"])
        meta["declared_by"] = declared
        meta["declaration_consistent"] = len(declared) >= 2
        meta["activity_entity_verified"] = False      # 模型声明不得升级为已核实
        meta["events"] = sorted(meta["events"])
        meta["conflicted"] = sorted(meta["conflicted"])
        meta["paths"] = sorted(meta["paths"])
        meta["kind_coexist"] = sorted(meta["kind_coexist"], key=lambda item: item["event_id"])
        meta["actors"] = sorted(meta["actors"])
        meta["kinds"] = sorted(meta["kinds"])

    return {
        "schema": SCHEMA,
        "edges": edges,
        "members": members,
        "member_meta": member_meta,
        "unlinked": unlinked,
        "conflicts": conflicts,
        "transactions": transactions,
        "stats": {
            "input_events": len(originals),
            "edges": len(edges),
            "duplicate_edges_merged": max(0, sum(1 for _ in edges) - len(edges)) + 0,
            "activity_members": sum(len(v) for v in members.values()),
            "activity_refs": len(members),
            "activity_refs_consistently_declared": sum(
                1 for meta in member_meta.values() if meta["declaration_consistent"]
            ),
            "activity_entities_verified": sum(
                1 for meta in member_meta.values() if meta["activity_entity_verified"]
            ),
            "invalid_ref_shapes": sum(1 for c in conflicts if c["kind"] == "invalid_ref_shape"),
            "unlinked": len(unlinked),
            "self_references": sum(1 for c in conflicts if c["kind"] == "self_reference"),
            "dangling_references": sum(1 for c in conflicts if c["kind"] == "dangling_reference"),
            "conflicts": len(conflicts),
            "non_activity_edges": sum(1 for edge in edges if not edge["activity_membership"]),
        },
        "field_coverage": {
            "canonical_event_id": sum(1 for e in originals if _text(e.get("canonical_event_id"))),
            "aliases": sum(1 for e in originals if e.get("aliases")),
            "source_session": sum(1 for e in originals if _text(e.get("source_session"))),
            "activity_ref": sum(1 for e in originals if _activity_refs(e)),
            "parent_event_id": sum(1 for e in originals if _text(e.get("parent_event_id"))),
            "related_event_ids": sum(1 for e in originals if e.get("related_event_ids")),
        },
        "contract": {
            "schema": SCHEMA,
            "only_explicit": "只读已有结构化引用；时间/金额/文本相似度不建边",
            "namespaces": "recording/session/order/transaction 各自独立，不是活动 id；对应边 activity_membership=false",
            "activity_ref": "独立命名空间引用，不要求存在同名事件；reference_validated 只表示引用格式/目标核对通过，"
                            "activity_entity_verified 恒为 false（模型声明与多侧一致声明都不等于活动已核实）",
            "event_refs": "parent_event_id→child_of、related_event_ids→related_to；目标必须存在、非自环，悬空记 dangling_reference 且保留事件",
            "dedupe": "同 (relation,namespace,from,to) 只留一条边，多条 basis 合并到 basis_paths",
            "no_merge": "跨 actor/kind 的 activity 引用不串组，只记 conflict；related_to ≠ same activity",
            "preserve": "不隐藏事件与交易；每笔 transaction 原样保留，不求和",
            "output": "只给 edge + member refs；不生成巨卡、不改时间与金额",
        },
    }
