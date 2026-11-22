"""Exact Projection Identity Matcher and Read-Layer Audit for Life Logger.

Conflict-First, Coordinate-Prioritized & Explicit-Incompleteness Architecture (Q422/Q424/Q429/Q430):
1. Veto Gates:
   - Speaker conflict (source_label distinct) -> different / retain_both
   - Utterance conflict (utterance_id distinct) -> different / retain_both
   - Cross-namespace collision (capture_id == utterance_id) -> different / retain_both
   - Business entity barrier (order vs transaction vs activity) -> different / retain_both
   - Amount conflict (different amounts) -> different / report_conflict
   - Physical coordinates distinct (recording/segment/offset mismatch) -> different / retain_both
2. Admission Gates for 'same':
   - Physical coordinates ALONE CANNOT prove same event projection.
   - Must have explicit stable original event ID in same namespace (canonical or utterance).
   - Both sides MUST have complete and matching physical source coordinates.
   - Missing coordinates -> cannot prove identity without verification -> unknown / retain_both.
3. Priority Sub-Bucketing & O(N) Bounded Indexing:
   - Candidate pairs are indexed ONLY by (namespace, stable_original_id).
   - Within an ID bucket, pairs are prioritized by matching recording/segment coordinates
     so that real duplicate pairs are evaluated first before any budget cap.
   - Hard budget caps on massive duplicate buckets to guarantee sub-second execution.
4. Explicit Incompleteness & Honest Counting:
   - Truncated buckets are counted EXACTLY ONCE per bucket key.
   - When a bucket exceeds budget, audit_incomplete is set to True, and truncated_bucket_keys
     are explicitly exported so callers never misinterpret alias=[] as complete audit.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any, Mapping, Sequence

ALLOWED_PROJECTION_NAMESPACES = {"canonical", "utterance"}
BUSINESS_ENTITY_NAMESPACES = {"order", "transaction", "activity", "capture"}

#: Hard budget caps for multi-projection buckets
MAX_PAIRS_PER_BUCKET = 50
MAX_TOTAL_EVALUATED_PAIRS = 500


def extract_source_coordinates(event: Mapping[str, Any]) -> dict[str, Any]:
    """Extract physical source coordinates from event evidence or metadata."""
    evidence = event.get("evidence") if isinstance(event.get("evidence"), Mapping) else {}
    prov = evidence.get("source_provenance") if isinstance(evidence.get("source_provenance"), Mapping) else {}

    rec_id = (
        prov.get("recording_id")
        or evidence.get("recording_id")
        or event.get("recording_id")
    )
    seg_id = (
        prov.get("segment_id")
        or evidence.get("segment_id")
        or event.get("segment_id")
    )
    src_id = (
        prov.get("source_id")
        or evidence.get("source_id")
        or event.get("source_id")
    )

    start_s = (
        prov.get("start_offset_seconds")
        if prov.get("start_offset_seconds") is not None
        else evidence.get("start_offset_seconds")
    )
    if start_s is None:
        start_s = event.get("start_offset_seconds")

    end_s = (
        prov.get("end_offset_seconds")
        if prov.get("end_offset_seconds") is not None
        else evidence.get("end_offset_seconds")
    )
    if end_s is None:
        end_s = event.get("end_offset_seconds")

    return {
        "recording_id": str(rec_id).strip() if rec_id else None,
        "segment_id": str(seg_id).strip() if seg_id else None,
        "source_id": str(src_id).strip() if src_id else None,
        "start_offset_seconds": float(start_s) if start_s is not None else None,
        "end_offset_seconds": float(end_s) if end_s is not None else None,
    }


def extract_identity_refs(event: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Extract (namespace, id) tuples from canonical_id, identity_refs, etc."""
    refs: list[tuple[str, str]] = []

    # 1. canonical_event_id
    canon = event.get("canonical_event_id") or (event.get("evidence") or {}).get("canonical_event_id")
    if canon and isinstance(canon, str) and canon.strip():
        refs.append(("canonical", canon.strip()))

    # 2. explicit identity_ref
    id_ref = event.get("identity_ref") or (event.get("data") or {}).get("activity_ref")
    if isinstance(id_ref, Mapping):
        ns = str(id_ref.get("namespace") or "activity").strip()
        _id = str(id_ref.get("id") or id_ref.get("ref") or "").strip()
        if _id:
            refs.append((ns, _id))

    # 3. typed specific IDs with their fixed namespace
    for key, ns in [
        ("utterance_id", "utterance"),
        ("capture_id", "capture"),
        ("order_id", "order"),
        ("activity_id", "activity"),
        ("transaction_id", "transaction"),
    ]:
        val = event.get(key) or (event.get("evidence") or {}).get(key)
        if val and isinstance(val, str) and val.strip():
            refs.append((ns, val.strip()))

    return refs


def evaluate_projection_identity(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate whether event A and event B are the exact same event projection.

    Conflict-First / Veto-First Architecture.
    """
    coord_a = extract_source_coordinates(a)
    coord_b = extract_source_coordinates(b)
    refs_a = extract_identity_refs(a)
    refs_b = extract_identity_refs(b)

    # --- 否决门 1：跨命名空间假碰撞 ---
    cross_ns_collision = False
    for ns_a, id_a in refs_a:
        for ns_b, id_b in refs_b:
            if id_a == id_b and ns_a != ns_b:
                cross_ns_collision = True
                break
        if cross_ns_collision:
            break

    has_same_ns_hit = False
    same_ns_pair = None
    for ns_a, id_a in refs_a:
        for ns_b, id_b in refs_b:
            if ns_a == ns_b and id_a == id_b:
                has_same_ns_hit = True
                same_ns_pair = (ns_a, id_a)
                break
        if has_same_ns_hit:
            break

    if cross_ns_collision and not has_same_ns_hit:
        return {
            "verdict": "different",
            "action": "retain_both",
            "dimension": "same_event_projection",
            "matched_anchor": None,
            "reason": "cross_namespace_id_collision_rejected",
            "missing_fields": [],
        }

    # --- 否决门 2：业务实体跨层次隔离 ---
    ns_set_a = {ns for ns, _ in refs_a}
    ns_set_b = {ns for ns, _ in refs_b}
    biz_entities_a = ns_set_a & BUSINESS_ENTITY_NAMESPACES
    biz_entities_b = ns_set_b & BUSINESS_ENTITY_NAMESPACES
    if biz_entities_a and biz_entities_b and biz_entities_a != biz_entities_b:
        return {
            "verdict": "different",
            "action": "retain_both",
            "dimension": "same_event_projection",
            "matched_anchor": None,
            "reason": "distinct_business_entities",
            "missing_fields": [],
        }

    # --- 否决门 3：说话人冲突 ---
    spk_a = str((a.get("evidence") or {}).get("source_label") or a.get("source_label") or "").strip().lower()
    spk_b = str((b.get("evidence") or {}).get("source_label") or b.get("source_label") or "").strip().lower()
    if spk_a and spk_b and spk_a != spk_b:
        return {
            "verdict": "different",
            "action": "retain_both",
            "dimension": "same_event_projection",
            "matched_anchor": None,
            "reason": "speaker_conflict",
            "missing_fields": [],
        }

    # --- 否决门 4：话语实例冲突 ---
    uid_a = a.get("utterance_id") or (a.get("evidence") or {}).get("utterance_id")
    uid_b = b.get("utterance_id") or (b.get("evidence") or {}).get("utterance_id")
    if uid_a and uid_b and str(uid_a).strip() != str(uid_b).strip():
        return {
            "verdict": "different",
            "action": "retain_both",
            "dimension": "same_event_projection",
            "matched_anchor": None,
            "reason": "utterance_id_conflict",
            "missing_fields": [],
        }

    # --- 否决门 5：同类 typed ID 冲突 ---
    for key, ns in [("order_id", "order"), ("transaction_id", "transaction"), ("activity_id", "activity"), ("capture_id", "capture")]:
        val_a = a.get(key) or (a.get("evidence") or {}).get(key)
        val_b = b.get(key) or (b.get("evidence") or {}).get(key)
        if val_a and val_b and str(val_a).strip() != str(val_b).strip():
            return {
                "verdict": "different",
                "action": "retain_both",
                "dimension": "same_event_projection",
                "matched_anchor": None,
                "reason": f"{ns}_id_conflict",
                "missing_fields": [],
            }

    # --- 否决门 6：金额细粒度冲突 ---
    amt_a = ((a.get("data") or {}).get("expense") or {}).get("amount")
    amt_b = ((b.get("data") or {}).get("expense") or {}).get("amount")
    if amt_a is not None and amt_b is not None:
        try:
            if abs(float(amt_a) - float(amt_b)) > 0.001:
                return {
                    "verdict": "different",
                    "action": "report_conflict",
                    "dimension": "same_event_projection",
                    "matched_anchor": None,
                    "reason": "amount_conflict",
                    "missing_fields": [],
                }
        except (TypeError, ValueError):
            pass

    # --- 否决门 7：物理坐标明确冲突 ---
    coords_complete_a = bool(coord_a["recording_id"] and coord_a["segment_id"] and coord_a["start_offset_seconds"] is not None)
    coords_complete_b = bool(coord_b["recording_id"] and coord_b["segment_id"] and coord_b["start_offset_seconds"] is not None)

    if coords_complete_a and coords_complete_b:
        if coord_a["recording_id"] != coord_b["recording_id"]:
            return {
                "verdict": "different",
                "action": "retain_both",
                "dimension": "same_event_projection",
                "matched_anchor": None,
                "reason": "physical_source_recording_distinct",
                "missing_fields": [],
            }
        if coord_a["segment_id"] != coord_b["segment_id"]:
            return {
                "verdict": "different",
                "action": "retain_both",
                "dimension": "same_event_projection",
                "matched_anchor": None,
                "reason": "physical_source_offsets_distinct",
                "missing_fields": [],
            }
        delta_start = abs(coord_a["start_offset_seconds"] - coord_b["start_offset_seconds"])
        delta_end = 0.0
        if coord_a["end_offset_seconds"] is not None and coord_b["end_offset_seconds"] is not None:
            delta_end = abs(coord_a["end_offset_seconds"] - coord_b["end_offset_seconds"])
        if delta_start > 0.5 or delta_end > 0.5:
            return {
                "verdict": "different",
                "action": "retain_both",
                "dimension": "same_event_projection",
                "matched_anchor": None,
                "reason": "physical_source_offsets_distinct",
                "missing_fields": [],
            }

    # --- 准入通道 (Admission Gate) ---
    if not (coords_complete_a and coords_complete_b):
        missing = []
        if not coords_complete_a:
            missing.append("source_coordinates_incomplete_in_a")
        if not coords_complete_b:
            missing.append("source_coordinates_incomplete_in_b")
        return {
            "verdict": "unknown",
            "action": "retain_both",
            "dimension": "same_event_projection",
            "matched_anchor": None,
            "reason": "unverified_without_coordinates",
            "missing_fields": missing,
        }

    if has_same_ns_hit and same_ns_pair[0] in ALLOWED_PROJECTION_NAMESPACES:
        anchor = f"{same_ns_pair[0]}:{same_ns_pair[1]}"
        return {
            "verdict": "same",
            "action": "merge_projection_retain_alias",
            "dimension": "same_event_projection",
            "matched_anchor": anchor,
            "reason": "exact_identity_anchor_verified",
            "missing_fields": [],
        }

    return {
        "verdict": "unknown",
        "action": "retain_both",
        "dimension": "same_event_projection",
        "matched_anchor": None,
        "reason": "coordinates_match_alone_insufficient_for_identity",
        "missing_fields": ["canonical_event_id_or_utterance_id_missing"],
    }


def _pair_coordinate_priority(i: int, j: int, events: Sequence[Mapping[str, Any]]) -> int:
    """Sub-bucket priority within the same ID bucket.

    Priority 0 (highest): matching recording_id and segment_id (real duplicate candidates)
    Priority 1: matching recording_id only
    Priority 2: other
    """
    c_a = extract_source_coordinates(events[i])
    c_b = extract_source_coordinates(events[j])
    if c_a["recording_id"] and c_a["recording_id"] == c_b["recording_id"]:
        if c_a["segment_id"] and c_a["segment_id"] == c_b["segment_id"]:
            return 0
        return 1
    return 2


def build_projection_identity_audit(
    selected_events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build an additive projection identity audit for the current selected events.

    O(N) Near-Linear Architecture with Coordinate Prioritization:
    - Candidate pairs are indexed ONLY by (namespace, stable_original_id).
    - Pairs within an ID bucket are sorted by coordinate proximity so true duplicates
      are evaluated first even if budget is reached.
    - Truncated buckets are counted EXACTLY ONCE per bucket key.
    - Explicitly sets audit_incomplete=True and records truncated_bucket_keys.
    """
    events = list(selected_events)
    n = len(events)
    total_possible_pairs = n * (n - 1) // 2

    # 1. 唯一合法候选索引：仅按 (namespace, stable_id) 分桶！
    by_anchor: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, ev in enumerate(events):
        canon = ev.get("canonical_event_id") or (ev.get("evidence") or {}).get("canonical_event_id")
        if canon and isinstance(canon, str) and canon.strip():
            by_anchor[("canonical", canon.strip())].append(idx)
        uid = ev.get("utterance_id") or (ev.get("evidence") or {}).get("utterance_id")
        if uid and isinstance(uid, str) and uid.strip():
            by_anchor[("utterance", uid.strip())].append(idx)

    # 2. 收集候选对：同 ID 桶内按坐标相近优先排序，受单桶预算与全局预算保护
    candidate_pairs: list[tuple[int, int]] = []
    truncated_bucket_keys: list[str] = []
    bucket_completeness: dict[str, bool] = {}

    for anchor, bucket in by_anchor.items():
        if len(bucket) <= 1:
            continue

        anchor_key = f"{anchor[0]}:{anchor[1]}"
        all_bucket_pairs: list[tuple[int, int]] = []
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                all_bucket_pairs.append((min(bucket[i], bucket[j]), max(bucket[i], bucket[j])))

        # 按坐标重合优先级排序（优先级 0 最先比对，确保末尾对也能被优先捕获）
        all_bucket_pairs.sort(key=lambda p: _pair_coordinate_priority(p[0], p[1], events))

        # 检查是否超出单桶预算
        if len(all_bucket_pairs) > MAX_PAIRS_PER_BUCKET:
            truncated_bucket_keys.append(anchor_key)
            bucket_completeness[anchor_key] = False
            selected_pairs = all_bucket_pairs[:MAX_PAIRS_PER_BUCKET]
        else:
            bucket_completeness[anchor_key] = True
            selected_pairs = all_bucket_pairs

        for p in selected_pairs:
            if len(candidate_pairs) >= MAX_TOTAL_EVALUATED_PAIRS:
                if anchor_key not in truncated_bucket_keys:
                    truncated_bucket_keys.append(anchor_key)
                    bucket_completeness[anchor_key] = False
                break
            if p not in candidate_pairs:
                candidate_pairs.append(p)

    # 3. 真实评估收集到的候选对
    same_pairs: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    alias_suggestions: list[dict[str, Any]] = []
    unknown_reasons_counter: dict[str, int] = defaultdict(int)

    same_count = 0
    different_count = 0
    unknown_count = 0

    for i, j in candidate_pairs:
        ev_a = events[i]
        ev_b = events[j]
        res = evaluate_projection_identity(ev_a, ev_b)
        v = res["verdict"]

        if v == "same":
            same_count += 1
            pair_info = {
                "event_id_a": ev_a.get("event_id"),
                "event_id_b": ev_b.get("event_id"),
                "matched_anchor": res.get("matched_anchor"),
                "reason": res.get("reason"),
                "coordinates_a": extract_source_coordinates(ev_a),
                "coordinates_b": extract_source_coordinates(ev_b),
            }
            same_pairs.append(pair_info)
            pri_id = ev_a.get("event_id")
            alias_id = ev_b.get("event_id")
            if str(ev_b.get("source_kind")) == "formal" and str(ev_a.get("source_kind")) != "formal":
                pri_id, alias_id = alias_id, pri_id
            alias_suggestions.append({
                "primary_event_id": pri_id,
                "suggested_alias_id": alias_id,
                "matched_anchor": res.get("matched_anchor"),
            })
        elif v == "different":
            different_count += 1
            if res.get("action") == "report_conflict":
                conflicts.append({
                    "event_id_a": ev_a.get("event_id"),
                    "event_id_b": ev_b.get("event_id"),
                    "reason": res.get("reason"),
                })
        else:
            unknown_count += 1
            reason_key = res.get("reason") or "unknown"
            unknown_reasons_counter[reason_key] += 1

    actual_evaluated_pairs = len(candidate_pairs)
    not_evaluated_pairs = max(0, total_possible_pairs - actual_evaluated_pairs)
    audit_incomplete = bool(truncated_bucket_keys or (actual_evaluated_pairs >= MAX_TOTAL_EVALUATED_PAIRS))

    return {
        "schema_version": "projection-identity-audit-v1",
        "evaluated_event_count": n,
        "total_possible_pairs_count": total_possible_pairs,
        "actual_evaluated_pairs_count": actual_evaluated_pairs,
        "not_evaluated_pairs_count": not_evaluated_pairs,
        "audit_incomplete": audit_incomplete,
        "bucket_complete": not audit_incomplete,
        "truncated_buckets_count": len(truncated_bucket_keys),
        "truncated_bucket_keys": truncated_bucket_keys,
        "truncated_reasons": {
            k: f"bucket pairs exceeded budget {MAX_PAIRS_PER_BUCKET}"
            for k in truncated_bucket_keys
        },
        "summary": {
            "same_positive_count": same_count,
            "different_count": different_count,
            "unknown_count": unknown_count,
            "conflict_count": len(conflicts),
        },
        "same_pairs": same_pairs,
        "conflicts": conflicts,
        "alias_suggestions": alias_suggestions,
        "unknown_reasons": dict(unknown_reasons_counter),
        "note": (
            "Incomplete audit flagged explicitly if any ID bucket exceeded budget. "
            "Coordinate prioritization ensures true duplicate candidates are checked first."
        ),
    }
