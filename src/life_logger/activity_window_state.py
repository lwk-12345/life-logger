"""活动窗口持久增量状态管理（Activity Window State）。

离线候选模块（不接生产，不修改原始输入，不加金额合计，不跨ID猜关联）。
用于在滑动窗口（如 10 分钟步长、15 分钟 lookback）推进时，维护全局累积活动状态，
解决"模型上轮摘要无法代替全局状态"以及"窗口滑出导致既往活动遗失"的问题。

设计契约：
1. **稳定主键 Upsert 与冲突隔离**：
   - 活动以明确的 `activity_id` 为主键，但若出现跨种类（kind/activity_type）或跨主体（source_label/actor）的严重冲突，
     拒绝盲目合并，保留独立候选分支并记入冲突；
   - 子项以 `item_id` 为主键；跨支付强冲突不覆盖；无证据新金额/新时间不得覆盖已有确凿事实；
   - 引用证据以 `(source_id, segment_id, row_id, quote)` 严格排重并集。
2. **滑出不删除**：
   - 窗口滑出视野后，既有活动依然完整保留在全局状态中；
   - 保留首次创建与最后更新窗口信息及完整修订历史（`revisions`）。
3. **版本冲突与 Provenance 保留**：
   - 相同 ID 增量更新时，记录字段变更与更新理由；
   - 子项与活动状态变更全留审计记录；
   - 明确取消/否定允许纠正，不永久保护旧 `completed`；但 `planned`/`unknown` 反向回退严格拦截并记入冲突。
4. **updates 契约完整性**：
   - 解析出的 `related_transaction_ids_added` 绝不丢弃，完整追加至关联交易列表；
   - `new_item_ids` 中未携带子项详情的未知引用留存为 `pending_item_references`，绝不凭空捏造假子项。
5. **禁止盲并与金额求和**：
   - 绝不因相似标题、时间邻近或同额将不同活动猜测关联；
   - 绝不计算或新增金额合计字段（不汇总 `amount`）；
   - 绝不合并订单点单与支付广播，不自动将推测购买升级为已支付；
   - 关联交易严格以 `related_transaction_ids` 列表独立保留。
6. **保真与字段隔离**：
   - 不修改传入的原始字典对象（无副作用）；
   - `spoken_at`（发声讲述时间）与 `occurred_at`（真实发生时间）严格分开；未说明发生时间的保持 `None`。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Mapping, Optional

SCHEMA = "activity-window-state-v1"


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _dedup_evidence(evidence_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """以 (source_id, segment_id, row_id, quote) 为主键去重，同时保留次序。"""
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for ev in evidence_list:
        if not isinstance(ev, Mapping):
            continue
        key = (
            str(ev.get("source_id") or ""),
            str(ev.get("segment_id") or ""),
            str(ev.get("row_id") or ""),
            str(ev.get("quote") or ""),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(copy.deepcopy(dict(ev)))
    return deduped


def _parse_update_spec(update_entry: Any) -> tuple[Optional[str], str, str, list[str], list[str]]:
    """解析 update 条目，支持结构化字典与字符串标注两种常见格式。

    返回: (activity_id, operation, reason, new_item_ids, related_tx_added)
    """
    if isinstance(update_entry, Mapping):
        act_id = _clean_str(update_entry.get("activity_id"))
        op = str(update_entry.get("operation") or update_entry.get("op") or "upsert").strip()
        reason = str(update_entry.get("reason") or "").strip()
        raw_items = update_entry.get("new_item_ids") or []
        new_items = [str(x) for x in raw_items if x] if isinstance(raw_items, (list, tuple)) else []
        raw_tx = update_entry.get("related_transaction_ids_added") or []
        tx_added = [str(x) for x in raw_tx if x] if isinstance(raw_tx, (list, tuple)) else []
        return act_id, op, reason, new_items, tx_added
    elif isinstance(update_entry, str):
        s = update_entry.strip()
        op = "upsert"
        if s.startswith("新增 "):
            op = "create"
            s = s[3:].strip()
        act_id = None
        if "：" in s:
            prefix, reason = s.split("：", 1)
            act_id = _clean_str(prefix)
        elif ":" in s:
            prefix, reason = s.split(":", 1)
            act_id = _clean_str(prefix)
        else:
            reason = s
        return act_id, op, reason.strip(), [], []
    return None, "upsert", "", [], []


class ActivityWindowState:
    """活动窗口持久增量状态机。纯离线无副作用。"""

    def __init__(self, initial_state: Optional[Mapping[str, Any]] = None) -> None:
        self._schema = SCHEMA
        self._activities: dict[str, dict[str, Any]] = {}
        self._unassociated_items: list[dict[str, Any]] = []
        self._conflicts: list[dict[str, Any]] = []
        self._window_history: list[dict[str, Any]] = []

        if initial_state and isinstance(initial_state, Mapping):
            self._load_from_dict(initial_state)

    def _load_from_dict(self, data: Mapping[str, Any]) -> None:
        self._schema = str(data.get("schema_version") or SCHEMA)
        raw_acts = data.get("activities") or {}
        if isinstance(raw_acts, Mapping):
            self._activities = {k: copy.deepcopy(v) for k, v in raw_acts.items() if isinstance(v, Mapping)}
        elif isinstance(raw_acts, list):
            for a in raw_acts:
                if isinstance(a, Mapping) and a.get("activity_id"):
                    self._activities[str(a["activity_id"])] = copy.deepcopy(dict(a))

        raw_unassoc = data.get("unassociated_items") or []
        if isinstance(raw_unassoc, list):
            self._unassociated_items = [copy.deepcopy(dict(x)) for x in raw_unassoc if isinstance(x, Mapping)]

        raw_conflicts = data.get("conflicts") or []
        if isinstance(raw_conflicts, list):
            self._conflicts = [copy.deepcopy(dict(x)) if isinstance(x, Mapping) else {"text": str(x)} for x in raw_conflicts]

        raw_hist = data.get("window_history") or []
        if isinstance(raw_hist, list):
            self._window_history = [copy.deepcopy(dict(x)) for x in raw_hist if isinstance(x, Mapping)]

    @property
    def activities(self) -> dict[str, dict[str, Any]]:
        """返回当前所有已知活动（深拷贝保证外部不篡改状态）。"""
        return copy.deepcopy(self._activities)

    @property
    def unassociated_items(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._unassociated_items)

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._conflicts)

    @property
    def window_history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._window_history)

    def get_activity(self, activity_id: str) -> Optional[dict[str, Any]]:
        act = self._activities.get(activity_id)
        return copy.deepcopy(act) if act is not None else None

    def list_activities(self, *, sort_by: str = "reported_at") -> list[dict[str, Any]]:
        """返回已排序的活动列表。"""
        acts = list(self._activities.values())
        if sort_by == "activity_id":
            acts.sort(key=lambda a: str(a.get("activity_id") or ""))
        elif sort_by == "reported_at":
            acts.sort(key=lambda a: (str(a.get("reported_at") or ""), str(a.get("activity_id") or "")))
        return copy.deepcopy(acts)

    def to_dict(self) -> dict[str, Any]:
        """序列化为完整持久状态字典。"""
        total_items = sum(len(a.get("items") or []) for a in self._activities.values())
        return {
            "schema_version": self._schema,
            "activities": copy.deepcopy(self._activities),
            "unassociated_items": copy.deepcopy(self._unassociated_items),
            "conflicts": copy.deepcopy(self._conflicts),
            "window_history": copy.deepcopy(self._window_history),
            "metadata": {
                "total_windows_applied": len(self._window_history),
                "total_activities": len(self._activities),
                "total_items": total_items,
                "total_conflicts": len(self._conflicts),
            },
        }

    def apply_window(
        self,
        window_result: Mapping[str, Any],
        *,
        window_meta: Optional[Mapping[str, Any]] = None,
    ) -> ActivityWindowState:
        """处理一轮滑动窗口结果，返回新的 ActivityWindowState 实例。

        不修改传入的任何输入。
        """
        new_state = ActivityWindowState(self.to_dict())
        new_state._apply_window_in_place(window_result, window_meta=window_meta)
        return new_state

    def _apply_window_in_place(
        self,
        window_result: Mapping[str, Any],
        *,
        window_meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if "response" in window_result and isinstance(window_result["response"], Mapping):
            resp = window_result["response"]
            meta = {
                "index": window_result.get("index"),
                "cutoff": window_result.get("cutoff"),
                "lookback": window_result.get("lookback"),
                "record_count": window_result.get("record_count"),
            }
        else:
            resp = window_result
            meta = {}

        if window_meta and isinstance(window_meta, Mapping):
            meta.update(window_meta)

        window_index = meta.get("index")
        window_cutoff = meta.get("cutoff")

        # 1. 解析 updates
        updates_by_act: dict[str, list[dict[str, Any]]] = {}
        for raw_u in resp.get("updates") or []:
            act_id, op, reason, new_items, tx_added = _parse_update_spec(raw_u)
            if act_id:
                updates_by_act.setdefault(act_id, []).append({
                    "op": op,
                    "reason": reason,
                    "new_items": new_items,
                    "tx_added": tx_added,
                })

        # 2. 消费 activities
        incoming_activities = resp.get("activities") or []
        processed_act_ids: set[str] = set()

        for raw_act in incoming_activities:
            if not isinstance(raw_act, Mapping):
                continue
            act_id = _clean_str(raw_act.get("activity_id"))
            if not act_id:
                continue

            processed_act_ids.add(act_id)
            act_updates = updates_by_act.get(act_id, [])
            update_reason = "; ".join(u["reason"] for u in act_updates if u.get("reason")) if act_updates else None
            update_op = act_updates[0]["op"] if act_updates else "upsert"

            if act_id not in self._activities:
                # ---------------- 新增活动 (Create) ----------------
                act_copy = copy.deepcopy(dict(raw_act))
                act_copy["activity_id"] = act_id
                act_copy["status"] = _clean_str(act_copy.get("status")) or "unknown"
                act_copy.setdefault("related_transaction_ids", [])
                act_copy.setdefault("uncertainties", [])

                # 消费 updates 中的 tx_added
                for u in act_updates:
                    for tx in u.get("tx_added") or []:
                        if tx and tx not in act_copy["related_transaction_ids"]:
                            act_copy["related_transaction_ids"].append(tx)

                norm_items = []
                for raw_item in act_copy.get("items") or []:
                    if isinstance(raw_item, Mapping) and raw_item.get("item_id"):
                        it = copy.deepcopy(dict(raw_item))
                        it["evidence"] = _dedup_evidence(it.get("evidence") or [])
                        norm_items.append(it)
                act_copy["items"] = norm_items

                # updates 中未知引用留存 pending，不可凭空造假子项
                existing_item_ids = {it["item_id"] for it in norm_items if it.get("item_id")}
                pending_refs = []
                for u in act_updates:
                    for nid in u.get("new_items") or []:
                        if nid and nid not in existing_item_ids and nid not in pending_refs:
                            pending_refs.append(nid)
                if pending_refs:
                    act_copy["pending_item_references"] = pending_refs

                act_copy["provenance"] = {
                    "created_in_window": window_index,
                    "created_at_cutoff": window_cutoff,
                    "last_updated_in_window": window_index,
                    "last_updated_at_cutoff": window_cutoff,
                    "revisions": [
                        {
                            "revision_id": 1,
                            "window_index": window_index,
                            "cutoff": window_cutoff,
                            "operation": "create",
                            "reason": update_reason,
                            "added_item_ids": [it["item_id"] for it in norm_items if it.get("item_id")],
                            "timestamp": window_cutoff or "",
                        }
                    ],
                    "conflict_history": [],
                }
                self._activities[act_id] = act_copy
            else:
                # ---------------- 既有活动更新 (Upsert) ----------------
                existing = self._activities[act_id]

                # (0) 跨类别/跨主体冲突检测：同模型 activity_id 但不同 kind 或 actor 不得盲并！
                old_kind = _clean_str(existing.get("activity_type") or existing.get("kind"))
                new_kind = _clean_str(raw_act.get("activity_type") or raw_act.get("kind"))
                old_actor = _clean_str(existing.get("source_label") or existing.get("actor"))
                new_actor = _clean_str(raw_act.get("source_label") or raw_act.get("actor"))

                is_kind_conflict = bool(old_kind and new_kind and old_kind.lower() != new_kind.lower())
                is_actor_conflict = bool(old_actor and new_actor and old_actor.lower() != new_actor.lower())

                if is_kind_conflict or is_actor_conflict:
                    conflict_field = "activity_type" if is_kind_conflict else "source_label"
                    old_val = old_kind if is_kind_conflict else old_actor
                    new_val = new_kind if is_kind_conflict else new_actor
                    conflict_entry = {
                        "conflict_id": f"cnf-{act_id}-{conflict_field}-w{window_index}",
                        "activity_id": act_id,
                        "field": conflict_field,
                        "old_value": old_val,
                        "new_value": new_val,
                        "window_index": window_index,
                        "reason": f"相同 activity_id 发生跨类别/主体冲突 ({old_val} vs {new_val})，拒绝盲并",
                    }
                    existing["provenance"]["conflict_history"].append(conflict_entry)
                    self._conflicts.append(conflict_entry)

                    # 保留输入候选及冲突分支，不盲并污染旧活动
                    cand_id = f"{act_id}#conflict_w{window_index}"
                    cand_copy = copy.deepcopy(dict(raw_act))
                    cand_copy["activity_id"] = cand_id
                    cand_copy["conflict_of"] = act_id
                    cand_copy["provenance"] = {
                        "created_in_window": window_index,
                        "created_at_cutoff": window_cutoff,
                        "last_updated_in_window": window_index,
                        "last_updated_at_cutoff": window_cutoff,
                        "revisions": [{
                            "revision_id": 1,
                            "window_index": window_index,
                            "cutoff": window_cutoff,
                            "operation": "conflict_branch",
                            "reason": f"因 {conflict_field} 冲突拒绝合并，保留为独立候选",
                            "added_item_ids": [it.get("item_id") for it in cand_copy.get("items", []) if it.get("item_id")],
                            "timestamp": window_cutoff or "",
                        }],
                        "conflict_history": [conflict_entry],
                    }
                    self._activities[cand_id] = cand_copy
                    existing["provenance"].setdefault("conflicted_candidates", []).append(cand_id)
                    continue

                changed_fields: list[str] = []
                added_item_ids: list[str] = []

                # (1) Title 更新
                new_title = _clean_str(raw_act.get("title"))
                old_title = _clean_str(existing.get("title"))
                if new_title and new_title != old_title:
                    existing["title"] = new_title
                    changed_fields.append("title")

                # (2) Summary 更新
                new_summary = _clean_str(raw_act.get("summary"))
                old_summary = _clean_str(existing.get("summary"))
                if new_summary and new_summary != old_summary:
                    existing["summary"] = new_summary
                    changed_fields.append("summary")

                # (3) 状态转移：明确取消/否定允许纠正不永久护旧 completed；planned/unknown 反向变化记冲突
                new_status = _clean_str(raw_act.get("status"))
                old_status = _clean_str(existing.get("status"))
                if new_status and new_status != old_status:
                    is_cancellation = (
                        new_status in ("cancelled", "canceled") or
                        any("取消" in u.get("reason", "") or "否定" in u.get("reason", "") for u in act_updates)
                    )
                    if old_status == "completed":
                        if is_cancellation:
                            existing["status"] = "cancelled"
                            changed_fields.append(f"status:{old_status}->cancelled")
                        elif new_status in ("planned", "unknown"):
                            conflict_entry = {
                                "conflict_id": f"cnf-{act_id}-status-w{window_index}",
                                "activity_id": act_id,
                                "field": "status",
                                "old_value": old_status,
                                "new_value": new_status,
                                "window_index": window_index,
                                "reason": f"状态从已完成回退为 {new_status}，旧事实保持保护",
                            }
                            existing["provenance"]["conflict_history"].append(conflict_entry)
                            self._conflicts.append(conflict_entry)
                        else:
                            existing["status"] = new_status
                            changed_fields.append(f"status:{old_status}->{new_status}")
                    else:
                        existing["status"] = new_status
                        changed_fields.append(f"status:{old_status}->{new_status}")

                # (4) 发生时间：不以无证据新事实覆盖既有已知发生时间
                for time_key in ("occurrence_start", "occurrence_end"):
                    new_val = _clean_str(raw_act.get(time_key))
                    old_val = _clean_str(existing.get(time_key))
                    if old_val and not new_val:
                        pass
                    elif new_val and not old_val:
                        existing[time_key] = new_val
                        changed_fields.append(time_key)
                    elif new_val and old_val and new_val != old_val:
                        conflict_entry = {
                            "conflict_id": f"cnf-{act_id}-{time_key}-w{window_index}",
                            "activity_id": act_id,
                            "field": time_key,
                            "old_value": old_val,
                            "new_value": new_val,
                            "window_index": window_index,
                            "reason": f"{time_key} 时间戳变更冲突",
                        }
                        existing["provenance"]["conflict_history"].append(conflict_entry)
                        self._conflicts.append(conflict_entry)

                # (5) reported_at 保持非空
                new_rep = _clean_str(raw_act.get("reported_at"))
                if new_rep and not existing.get("reported_at"):
                    existing["reported_at"] = new_rep

                # (6) related_transaction_ids：独立去重合并，且绝不丢失 updates 中追加的交易！
                existing_tx = list(existing.get("related_transaction_ids") or [])
                incoming_tx = [str(x) for x in (raw_act.get("related_transaction_ids") or []) if x]
                for tx_id in incoming_tx:
                    if tx_id not in existing_tx:
                        existing_tx.append(tx_id)
                        changed_fields.append(f"tx_added:{tx_id}")

                for u in act_updates:
                    for tx_id in u.get("tx_added") or []:
                        if tx_id and tx_id not in existing_tx:
                            existing_tx.append(tx_id)
                            changed_fields.append(f"tx_added_from_update:{tx_id}")
                existing["related_transaction_ids"] = existing_tx

                # (7) uncertainties 去重合并
                existing_unc = list(existing.get("uncertainties") or [])
                for unc in raw_act.get("uncertainties") or []:
                    s_unc = _clean_str(unc)
                    if s_unc and s_unc not in existing_unc:
                        existing_unc.append(s_unc)
                existing["uncertainties"] = existing_unc

                # (8) 子项（items）合并
                existing_items_map = {
                    it["item_id"]: it for it in (existing.get("items") or []) if isinstance(it, Mapping) and it.get("item_id")
                }
                for inc_item in raw_act.get("items") or []:
                    if not isinstance(inc_item, Mapping):
                        continue
                    it_id = _clean_str(inc_item.get("item_id"))
                    if not it_id:
                        continue

                    if it_id not in existing_items_map:
                        it_copy = copy.deepcopy(dict(inc_item))
                        it_copy["evidence"] = _dedup_evidence(it_copy.get("evidence") or [])
                        existing.setdefault("items", []).append(it_copy)
                        existing_items_map[it_id] = it_copy
                        added_item_ids.append(it_id)
                        changed_fields.append(f"item_added:{it_id}")
                    else:
                        target_it = existing_items_map[it_id]

                        # 子项 status 变更留修订，明确取消允许纠正，planned/unknown 记冲突
                        new_it_status = _clean_str(inc_item.get("status"))
                        old_it_status = _clean_str(target_it.get("status"))
                        if new_it_status and new_it_status != old_it_status:
                            is_it_cancellation = (
                                new_it_status in ("cancelled", "canceled") or
                                any("取消" in u.get("reason", "") or "否定" in u.get("reason", "") for u in act_updates)
                            )
                            if old_it_status == "completed":
                                if is_it_cancellation:
                                    target_it["status"] = "cancelled"
                                    changed_fields.append(f"item_status:{it_id}:{old_it_status}->cancelled")
                                    target_it.setdefault("revisions", []).append({
                                        "from": old_it_status, "to": "cancelled", "window_index": window_index, "reason": "明确取消/纠正"
                                    })
                                elif new_it_status in ("planned", "unknown"):
                                    conflict_entry = {
                                        "conflict_id": f"cnf-{act_id}-{it_id}-status-w{window_index}",
                                        "activity_id": act_id,
                                        "item_id": it_id,
                                        "field": "status",
                                        "old_value": old_it_status,
                                        "new_value": new_it_status,
                                        "window_index": window_index,
                                        "reason": f"子项状态从 completed 反向变化为 {new_it_status}，旧事实受保护",
                                    }
                                    existing["provenance"]["conflict_history"].append(conflict_entry)
                                    self._conflicts.append(conflict_entry)
                                else:
                                    target_it["status"] = new_it_status
                                    changed_fields.append(f"item_status:{it_id}:{old_it_status}->{new_it_status}")
                                    target_it.setdefault("revisions", []).append({
                                        "from": old_it_status, "to": new_it_status, "window_index": window_index
                                    })
                            else:
                                target_it["status"] = new_it_status
                                changed_fields.append(f"item_status:{it_id}:{old_it_status}->{new_it_status}")
                                target_it.setdefault("revisions", []).append({
                                    "from": old_it_status, "to": new_it_status, "window_index": window_index
                                })

                        # action
                        if _clean_str(inc_item.get("action")):
                            target_it["action"] = inc_item["action"]

                        # 新无证据 amount 不得替旧事实；同 item_id 跨支付强冲突不覆盖
                        if inc_item.get("amount") is not None:
                            new_amt = inc_item.get("amount")
                            old_amt = target_it.get("amount")
                            if old_amt is not None:
                                try:
                                    diff = abs(float(new_amt) - float(old_amt))
                                except (ValueError, TypeError):
                                    diff = 1.0 if new_amt != old_amt else 0.0

                                if diff > 0.001:
                                    has_inc_ev = bool(inc_item.get("evidence"))
                                    has_old_ev = bool(target_it.get("evidence"))
                                    if not has_inc_ev and has_old_ev:
                                        conflict_entry = {
                                            "conflict_id": f"cnf-{act_id}-{it_id}-amount-w{window_index}",
                                            "activity_id": act_id,
                                            "item_id": it_id,
                                            "field": "amount",
                                            "old_value": old_amt,
                                            "new_value": new_amt,
                                            "window_index": window_index,
                                            "reason": "新金额缺少引用证据，旧金额保持保护",
                                        }
                                        existing["provenance"]["conflict_history"].append(conflict_entry)
                                        self._conflicts.append(conflict_entry)
                                    else:
                                        conflict_entry = {
                                            "conflict_id": f"cnf-{act_id}-{it_id}-amount-conflict-w{window_index}",
                                            "activity_id": act_id,
                                            "item_id": it_id,
                                            "field": "amount",
                                            "old_value": old_amt,
                                            "new_value": new_amt,
                                            "window_index": window_index,
                                            "reason": "同 item_id 发生金额强冲突，拒绝覆盖，保持旧事实",
                                        }
                                        existing["provenance"]["conflict_history"].append(conflict_entry)
                                        self._conflicts.append(conflict_entry)
                            else:
                                target_it["amount"] = new_amt
                                if isinstance(target_it.get("expense"), dict) and target_it["expense"].get("amount") is None:
                                    target_it["expense"]["amount"] = new_amt
                                changed_fields.append(f"item_amount:{it_id}")

                        # V3 嵌套 expense 字典增量补全（不覆盖已有非空金额）
                        inc_exp = inc_item.get("expense")
                        if isinstance(inc_exp, Mapping):
                            if not isinstance(target_it.get("expense"), dict):
                                target_it["expense"] = copy.deepcopy(dict(inc_exp))
                            else:
                                tgt_exp = target_it["expense"]
                                if tgt_exp.get("amount") is None and inc_exp.get("amount") is not None:
                                    tgt_exp["amount"] = inc_exp["amount"]
                                    if target_it.get("amount") is None:
                                        target_it["amount"] = inc_exp["amount"]
                                        changed_fields.append(f"item_amount:{it_id}")
                                if not tgt_exp.get("currency") and inc_exp.get("currency"):
                                    tgt_exp["currency"] = inc_exp["currency"]
                                if not tgt_exp.get("item") and inc_exp.get("item"):
                                    tgt_exp["item"] = inc_exp["item"]
                                if tgt_exp.get("payment_status") in (None, "", "unknown") and inc_exp.get("payment_status") in ("paid", "unpaid"):
                                    tgt_exp["payment_status"] = inc_exp["payment_status"]
                                    target_it["payment_status"] = inc_exp["payment_status"]

                        # 新无证据 time 不得替旧事实
                        if inc_item.get("occurred_at") is not None:
                            new_occ = _clean_str(inc_item.get("occurred_at"))
                            old_occ = _clean_str(target_it.get("occurred_at"))
                            if old_occ and new_occ and old_occ != new_occ:
                                has_inc_ev = bool(inc_item.get("evidence"))
                                has_old_ev = bool(target_it.get("evidence"))
                                if not has_inc_ev and has_old_ev:
                                    conflict_entry = {
                                        "conflict_id": f"cnf-{act_id}-{it_id}-occurred_at-w{window_index}",
                                        "activity_id": act_id,
                                        "item_id": it_id,
                                        "field": "occurred_at",
                                        "old_value": old_occ,
                                        "new_value": new_occ,
                                        "window_index": window_index,
                                        "reason": "新发生时间缺少引用证据，旧事实保持保护",
                                    }
                                    existing["provenance"]["conflict_history"].append(conflict_entry)
                                    self._conflicts.append(conflict_entry)
                                else:
                                    conflict_entry = {
                                        "conflict_id": f"cnf-{act_id}-{it_id}-occurred_at-conflict-w{window_index}",
                                        "activity_id": act_id,
                                        "item_id": it_id,
                                        "field": "occurred_at",
                                        "old_value": old_occ,
                                        "new_value": new_occ,
                                        "window_index": window_index,
                                        "reason": "同 item_id 发生发生时间冲突，拒绝覆盖",
                                    }
                                    existing["provenance"]["conflict_history"].append(conflict_entry)
                                    self._conflicts.append(conflict_entry)
                            elif not old_occ and new_occ:
                                target_it["occurred_at"] = new_occ
                                changed_fields.append(f"item_occurred_at:{it_id}")

                        # spoken_at
                        if inc_item.get("spoken_at"):
                            target_it["spoken_at"] = inc_item["spoken_at"]

                        # 属性保真
                        for attr in ("currency", "unit", "location", "duration_seconds"):
                            if inc_item.get(attr) is not None:
                                target_it[attr] = inc_item[attr]

                        # evidence 严格去重并集
                        inc_ev = inc_item.get("evidence") or []
                        target_it["evidence"] = _dedup_evidence(list(target_it.get("evidence") or []) + list(inc_ev))

                        # uncertainties
                        it_unc = list(target_it.get("uncertainties") or [])
                        for u in inc_item.get("uncertainties") or []:
                            su = _clean_str(u)
                            if su and su not in it_unc:
                                it_unc.append(su)
                        target_it["uncertainties"] = it_unc

                # (9) updates 中的未知 new_item_ids 留 pending，绝不凭空捏造假子项
                all_current_item_ids = {it["item_id"] for it in existing.get("items", []) if it.get("item_id")}
                for u in act_updates:
                    for nid in u.get("new_items") or []:
                        if nid and nid not in all_current_item_ids:
                            pending = existing.setdefault("pending_item_references", [])
                            if nid not in pending:
                                pending.append(nid)
                                changed_fields.append(f"pending_item_ref:{nid}")

                # 记录 revision
                rev_id = len(existing["provenance"]["revisions"]) + 1
                existing["provenance"]["revisions"].append({
                    "revision_id": rev_id,
                    "window_index": window_index,
                    "cutoff": window_cutoff,
                    "operation": update_op,
                    "reason": update_reason,
                    "changed_fields": changed_fields,
                    "added_item_ids": added_item_ids,
                    "timestamp": window_cutoff or "",
                })
                existing["provenance"]["last_updated_in_window"] = window_index
                existing["provenance"]["last_updated_at_cutoff"] = window_cutoff

        # 3. 消费针对未在当前 activities 列表中出现但存在 updates 的活动
        for act_id, act_updates in updates_by_act.items():
            if act_id in self._activities and act_id not in processed_act_ids:
                existing = self._activities[act_id]
                changed_fields = []
                for u in act_updates:
                    for tx in u.get("tx_added") or []:
                        existing_tx = existing.setdefault("related_transaction_ids", [])
                        if tx and tx not in existing_tx:
                            existing_tx.append(tx)
                            changed_fields.append(f"tx_added_from_update:{tx}")

                    all_current_item_ids = {it["item_id"] for it in existing.get("items", []) if it.get("item_id")}
                    for nid in u.get("new_items") or []:
                        if nid and nid not in all_current_item_ids:
                            pending = existing.setdefault("pending_item_references", [])
                            if nid not in pending:
                                pending.append(nid)
                                changed_fields.append(f"pending_item_ref:{nid}")

                    if "取消" in u.get("reason", "") or "否定" in u.get("reason", ""):
                        if existing.get("status") != "cancelled":
                            existing["status"] = "cancelled"
                            changed_fields.append("status:cancelled_from_update")

                if changed_fields:
                    update_reason = "; ".join(u["reason"] for u in act_updates if u.get("reason"))
                    rev_id = len(existing["provenance"]["revisions"]) + 1
                    existing["provenance"]["revisions"].append({
                        "revision_id": rev_id,
                        "window_index": window_index,
                        "cutoff": window_cutoff,
                        "operation": "upsert_from_update",
                        "reason": update_reason,
                        "changed_fields": changed_fields,
                        "added_item_ids": [],
                        "timestamp": window_cutoff or "",
                    })
                    existing["provenance"]["last_updated_in_window"] = window_index
                    existing["provenance"]["last_updated_at_cutoff"] = window_cutoff

        # 4. 消费 unassociated_items
        for unass in resp.get("unassociated_items") or []:
            if not isinstance(unass, Mapping):
                continue
            unass_copy = copy.deepcopy(dict(unass))
            unass_id = _clean_str(unass_copy.get("item_id"))
            is_associated = any(
                any(it.get("item_id") == unass_id for it in act.get("items") or [])
                for act in self._activities.values()
            )
            if not is_associated:
                existing_unass = next((x for x in self._unassociated_items if x.get("item_id") == unass_id), None)
                if not existing_unass:
                    unass_copy["evidence"] = _dedup_evidence(unass_copy.get("evidence") or [])
                    self._unassociated_items.append(unass_copy)
                else:
                    existing_unass["evidence"] = _dedup_evidence(
                        list(existing_unass.get("evidence") or []) + list(unass_copy.get("evidence") or [])
                    )

        # 5. 消费 conflicts
        for conf in resp.get("conflicts") or []:
            if isinstance(conf, Mapping):
                self._conflicts.append(copy.deepcopy(dict(conf)))
            elif isinstance(conf, str) and conf.strip():
                self._conflicts.append({"window_index": window_index, "cutoff": window_cutoff, "note": conf.strip()})

        # 6. 记录 window_history
        self._window_history.append({
            "index": window_index,
            "cutoff": window_cutoff,
            "lookback": meta.get("lookback"),
            "record_count": meta.get("record_count"),
            "activities_in_window": len(incoming_activities),
            "unassociated_in_window": len(resp.get("unassociated_items") or []),
            "total_accumulated_activities": len(self._activities),
            "provider": meta.get("provider"),
        })


def apply_window_update(
    state: Optional[Mapping[str, Any]],
    window_result: Mapping[str, Any],
    *,
    window_meta: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """纯函数接口：输入上轮状态字典与本轮窗口结果，返回新状态字典。

    绝不修改输入字典。
    """
    engine = ActivityWindowState(state)
    new_engine = engine.apply_window(window_result, window_meta=window_meta)
    return new_engine.to_dict()


def replay_window_sequence(
    calls: Iterable[Mapping[str, Any]],
    initial_state: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """回放一系列已保存的滑动窗口结果，生成最终持久化累积状态字典。"""
    engine = ActivityWindowState(initial_state)
    for call in calls:
        engine = engine.apply_window(call)
    return engine.to_dict()
