"""显式关系边：只由结构化引用建边，不猜、不串组、不吞并。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.activity_links import SCHEMA, build_activity_links  # noqa: E402


def ev(eid="e1", *, cats=("other",), actor="self", activity=None, data=None, aliases=None,
       canonical=None, session=None, recording=None) -> dict:
    event = {
        "event_id": eid,
        "title": "事件",
        "categories": list(cats),
        "activity_type": None,
        "evidence": {"source_label": actor, "recording_id": recording, "quote": "原话"},
    }
    if activity is not None:
        event["data"] = {"activity_ref": activity}
    if data:
        event.setdefault("data", {}).update(data)
    if aliases:
        event["aliases"] = aliases
    if canonical:
        event["canonical_event_id"] = canonical
    if session:
        event["source_session"] = session
    return event


def test_events_without_references_are_unlinked() -> None:
    result = build_activity_links([ev("a"), ev("b", cats=["food"])])
    assert result["unlinked"] == ["a", "b"]
    assert result["edges"] == []
    assert result["stats"]["activity_members"] == 0


def test_namespaced_activity_ref_creates_membership_edges() -> None:
    result = build_activity_links([
        ev("a", activity={"namespace": "activity", "id": "A1"}),
        ev("b", activity={"namespace": "activity", "id": "A1"}),
    ])
    assert result["members"] == {"activity:A1": ["a", "b"]}
    members = [edge for edge in result["edges"] if edge["relation"] == "member_of"]
    assert len(members) == 2
    assert all(edge["activity_membership"] for edge in members)
    assert members[0]["basis"]["path"] == "data.activity_ref"


def test_activity_ref_without_namespace_is_ignored_with_conflict() -> None:
    result = build_activity_links([ev("a", activity="A1"), ev("b", activity="A1")])
    assert result["members"] == {}
    assert result["conflicts"][0]["kind"] == "missing_namespace"
    assert result["conflicts"][0]["resolution"] == "ignored_not_activity"


@pytest.mark.parametrize("namespace", ["session", "recording", "order", "transaction"])
def test_non_activity_namespaces_never_become_membership(namespace: str) -> None:
    result = build_activity_links([
        ev("a", activity={"namespace": namespace, "id": "X1"}),
        ev("b", activity={"namespace": namespace, "id": "X1"}),
    ])
    assert result["members"] == {}
    edges = result["edges"]
    assert edges and all(edge["activity_membership"] is False for edge in edges)


def test_session_and_order_are_references_not_activity() -> None:
    result = build_activity_links([
        ev("a", session="SESS-1", data={"order_id": "ORD-1"}),
        ev("b", session="SESS-1", data={"order_id": "ORD-1"}),
    ])
    assert result["members"] == {}
    namespaces = {edge["namespace"] for edge in result["edges"]}
    assert {"session", "order"} <= namespaces
    assert all(edge["activity_membership"] is False for edge in result["edges"])


def test_cross_actor_does_not_group_even_with_same_ref() -> None:
    result = build_activity_links([
        ev("a", actor="self", activity={"namespace": "activity", "id": "A1"}),
        ev("b", actor="non_self", activity={"namespace": "activity", "id": "A1"}),
    ])
    assert result["members"] == {"activity:A1": ["a"]}
    assert result["conflicts"][0]["kind"] == "cross_actor_or_kind"
    assert "non_self" in result["conflicts"][0]["actors"]


def test_cross_kind_without_identical_explicit_ref_does_not_group() -> None:
    """【2026-10-25 缺口 B】跨 kind 是否允许取决于证据：只有**完全相同**的 data.activity_ref 才共存。"""
    via_top = ev("b", cats=["food"])
    via_top["activity_id"] = "A1"        # 证据来自顶层 activity_id，不是同一个 data.activity_ref
    result = build_activity_links([
        ev("a", cats=["exercise"], activity={"namespace": "activity", "id": "A1"}),
        via_top,
    ])
    assert result["members"] == {"activity:A1": ["a"]}
    assert result["conflicts"][0]["kind"] == "cross_actor_or_kind"


def test_aliases_create_traceable_edges_without_membership() -> None:
    result = build_activity_links([
        ev("a", aliases=[{"event_id": "a2", "source_kind": "live"}]),
        ev("a2"),   # 目标必须存在（返工：悬空别名记为 dangling_reference）
    ])
    alias_edges = [edge for edge in result["edges"] if edge["relation"] == "alias_of"]
    assert len(alias_edges) == 1
    assert alias_edges[0]["to_id"] == "a2"
    assert alias_edges[0]["namespace"] == "live"
    assert alias_edges[0]["activity_membership"] is False
    assert result["unlinked"] == ["a2"]     # 目标事件自身没有其它引用


def test_canonical_pointer_is_an_edge_not_membership() -> None:
    result = build_activity_links([ev("a", canonical="canon-1"), ev("canon-1")])
    edge = next(edge for edge in result["edges"] if edge["relation"] == "canonical_of")
    assert edge["to_id"] == "canon-1"
    assert edge["activity_membership"] is False


def test_transactions_are_preserved_individually() -> None:
    result = build_activity_links([
        ev("a", cats=["expense"], activity={"namespace": "activity", "id": "A1"},
           data={"transaction_id": "T1"}),
        ev("b", cats=["expense"], activity={"namespace": "activity", "id": "A1"},
           data={"transaction_id": "T2"}),
    ])
    assert result["transactions"]["a"][0]["id"] == "T1"
    assert result["transactions"]["b"][0]["id"] == "T2"
    assert result["members"] == {"activity:A1": ["a", "b"]}   # 同一活动可以有多个交易，各自保留


def test_no_time_or_amount_or_text_fallback() -> None:
    # 同一分钟、同样金额、标题相同，但没有引用 → 仍然 unlinked
    result = build_activity_links([
        ev("a", cats=["food"]),
        ev("b", cats=["food"]),
    ])
    assert result["members"] == {}
    assert result["unlinked"] == ["a", "b"]


def test_is_pure_and_preserves_all_events() -> None:
    events = [ev("a", activity={"namespace": "activity", "id": "A1"}), ev("b")]
    snapshot = copy.deepcopy(events)
    build_activity_links(events)
    assert events == snapshot


def test_contract_and_stats() -> None:
    result = build_activity_links([ev("a", session="S1")])
    assert result["schema"] == SCHEMA
    assert result["contract"]["only_explicit"].startswith("只读已有结构化引用")
    assert result["stats"]["input_events"] == 1
    assert result["stats"]["non_activity_edges"] >= 1
    json.dumps(result)  # 必须可 JSON 序列化


@pytest.mark.skipif(not (ROOT / "output" / "local-web" / "jobs").is_dir(), reason="需要真实 store")
def test_real_store_field_coverage_is_reported() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from life_logger.event_view import build_event_view

    view = build_event_view(ROOT / "output" / "local-web", date_from="2026-10-25",
                            date_to="2026-10-25", scope="provisional", view="display", use_cache=True)
    result = build_activity_links(view["events"])
    coverage = result["field_coverage"]
    assert coverage["canonical_event_id"] > 0     # 真实有值
    assert coverage["activity_ref"] == 0          # 真实数据尚无 activity_ref（字段缺口）
    assert result["stats"]["activity_members"] == 0


# ---------------------------------------------------------------- 4号 反例返工（L6–L9 + 语义）

def test_self_reference_is_kept_with_note() -> None:
    """【2026-10-25 缺口 A】局部 id 与 anchor event_id 同名是合法的：保留成员，只记 note。"""
    result = build_activity_links([ev("a1", activity={"namespace": "activity", "id": "a1"})])
    assert result["conflicts"][0]["reason"] == "self_reference"
    assert result["conflicts"][0]["resolution"] == "kept_with_note"
    assert result["members"] == {"activity:a1": ["a1"]}
    edge = next(edge for edge in result["edges"] if edge["relation"] == "member_of")
    assert edge["self_referential"] is True
    assert edge["reference_validated"] is True


def test_dangling_event_reference_kept_event_and_reported() -> None:
    result = build_activity_links([ev("a", canonical="ghost", session="S1")])
    assert any(c["reason"] == "dangling_reference" for c in result["conflicts"])
    assert all(edge["to_id"] != "ghost" for edge in result["edges"])
    assert "a" not in result["unlinked"]          # 仍有 session 引用，不算 unlinked
    assert result["stats"]["dangling_references"] == 1


def test_duplicate_edges_are_deduplicated_and_basis_merged() -> None:
    result = build_activity_links([
        ev("a", aliases=[{"event_id": "b", "source_kind": "live"},
                         {"event_id": "b", "source_kind": "live"}]),
        ev("b"),
    ])
    alias_edges = [edge for edge in result["edges"] if edge["relation"] == "alias_of"]
    assert len(alias_edges) == 1
    ids = [edge["edge_id"] for edge in result["edges"]]
    assert len(ids) == len(set(ids)), "edge_id 必须唯一"


def test_parent_event_id_creates_child_of_edge_not_membership() -> None:
    child = ev("c1", cats=["food"])
    child["parent_event_id"] = "p1"
    parent = ev("p1", cats=["meal"])
    result = build_activity_links([child, parent])
    edge = next(edge for edge in result["edges"] if edge["relation"] == "child_of")
    assert (edge["from_id"], edge["to_id"]) == ("c1", "p1")
    assert edge["basis"]["path"] == "parent_event_id"
    assert edge["activity_membership"] is False
    assert result["members"] == {}


def test_related_event_ids_become_related_to_not_same_activity() -> None:
    a = ev("a1", cats=["food"])
    a["related_event_ids"] = ["r1", "r2"]
    result = build_activity_links([a, ev("r1"), ev("r2")])
    related = [edge for edge in result["edges"] if edge["relation"] == "related_to"]
    assert {edge["to_id"] for edge in related} == {"r1", "r2"}
    assert all(edge["activity_membership"] is False for edge in related)
    assert result["members"] == {}


def test_cross_kind_parent_edge_allowed_but_no_membership() -> None:
    child = ev("c2", cats=["food"])
    child["parent_event_id"] = "p2"
    result = build_activity_links([child, ev("p2", cats=["exercise"])])
    assert any(edge["relation"] == "child_of" for edge in result["edges"])
    assert result["members"] == {}


def test_activity_ref_need_not_exist_as_event_and_single_side_is_unverified() -> None:
    result = build_activity_links([ev("a", activity={"namespace": "activity", "id": "act_ghost"})])
    key = "activity:act_ghost"
    assert result["members"] == {key: ["a"]}                 # 保留声明
    meta = result["member_meta"][key]
    assert meta["declaration_consistent"] is False            # 单侧只是声明
    assert meta["activity_entity_verified"] is False          # 绝不冒称已核实
    edge = next(edge for edge in result["edges"] if edge["relation"] == "member_of")
    assert edge["reference_validated"] is True                # 引用格式/目标核对通过
    assert edge["activity_entity_verified"] is False          # ≠ 活动身份已确认
    assert edge["declaration_consistent"] is False
    assert result["stats"]["activity_entities_verified"] == 0


def test_two_sided_declaration_is_consistent_but_not_verified_entity() -> None:
    result = build_activity_links([
        ev("a", activity={"namespace": "activity", "id": "A1"}),
        ev("b", activity={"namespace": "activity", "id": "A1"}),
    ])
    meta = result["member_meta"]["activity:A1"]
    assert meta["declaration_consistent"] is True      # 多侧一致声明
    assert meta["activity_entity_verified"] is False   # 仍不等于活动已核实
    assert meta["declared_by"] == ["a", "b"]
    assert result["stats"]["activity_entities_verified"] == 0


def test_transactions_never_dropped_even_when_refs_conflict() -> None:
    a = ev("a", cats=["expense"], activity={"namespace": "activity", "id": "A1"}, data={"transaction_id": "T1"})
    b = ev("b", cats=["expense"], actor="non_self", activity={"namespace": "activity", "id": "A1"},
           data={"transaction_id": "T2"})
    result = build_activity_links([a, b])
    assert result["transactions"]["a"][0]["id"] == "T1"
    assert result["transactions"]["b"][0]["id"] == "T2"
    assert any(c["kind"] == "cross_actor_or_kind" for c in result["conflicts"])


# ---------------------------------------------------------------- 3号 独立反例：verified 语义 / edges-members 一致性

def test_no_edge_exposes_a_bare_verified_flag() -> None:
    result = build_activity_links([
        ev("a", activity={"namespace": "activity", "id": "A1"}),
        ev("b", activity={"namespace": "activity", "id": "A1"}),
    ])
    for edge in result["edges"]:
        assert "verified" not in edge, "不得保留会被误读的 verified 字段"
        assert "reference_validated" in edge
        if edge["relation"] == "member_of":
            assert edge["activity_entity_verified"] is False


def test_actor_conflict_excluded_event_has_no_membership_edge() -> None:
    result = build_activity_links([
        ev("e1", actor="self", activity={"namespace": "activity", "id": "A4"}),
        ev("e2", actor="non_self", activity={"namespace": "activity", "id": "A4"}),
    ])
    assert result["members"] == {"activity:A4": ["e1"]}
    member_edges = [edge for edge in result["edges"] if edge["relation"] == "member_of"]
    assert {edge["from_id"] for edge in member_edges} == {"e1"}     # e2 不得有 membership 边
    assert result["member_meta"]["activity:A4"]["conflicted"] == ["e2"]
    excluded = [c for c in result["conflicts"] if c["kind"] == "cross_actor_or_kind"]
    assert excluded and excluded[0]["resolution"] == "not_grouped"


def test_kind_conflict_without_shared_explicit_ref_has_no_membership_edge() -> None:
    """证据不同（一侧顶层 activity_id）时，跨 kind 仍不建成员边。"""
    via_top = ev("k2", cats=["food"])
    via_top["activity_id"] = "K1"
    result = build_activity_links([
        ev("k1", cats=["exercise"], activity={"namespace": "activity", "id": "K1"}),
        via_top,
    ])
    member_edges = [edge for edge in result["edges"] if edge["relation"] == "member_of"]
    assert {edge["from_id"] for edge in member_edges} == {"k1"}


def test_invalid_namespace_or_id_shape_is_rejected_at_link_layer() -> None:
    bad_id = build_activity_links([ev("x", activity={"namespace": "activity", "id": "bad id!"})])
    assert bad_id["members"] == {}
    assert any(c["kind"] == "invalid_ref_shape" for c in bad_id["conflicts"])
    assert bad_id["stats"]["invalid_ref_shapes"] == 1

    bad_ns = build_activity_links([ev("y", activity={"namespace": "act ivity", "id": "A1"})])
    assert bad_ns["members"] == {}
    assert any(c["kind"] == "invalid_ref_shape" for c in bad_ns["conflicts"])


def test_edges_and_members_never_disagree() -> None:
    result = build_activity_links([
        ev("m1", actor="self", activity={"namespace": "activity", "id": "A9"}),
        ev("m2", actor="self", activity={"namespace": "activity", "id": "A9"}),
        ev("m3", actor="non_self", activity={"namespace": "activity", "id": "A9"}),
    ])
    edge_members = {edge["from_id"] for edge in result["edges"] if edge["relation"] == "member_of"}
    assert edge_members == set(result["members"]["activity:A9"]) == {"m1", "m2"}


# ---------------------------------------------------------------- Q363 缺口 A/B 回归

def test_gap_a_self_named_activity_id_still_forms_group() -> None:
    """① 两个事件都指向与自己同名的 activity id（局部 id == anchor event_id）→ 仍成组。"""
    result = build_activity_links([
        ev("evt_x", cats=["food"], activity={"namespace": "activity", "id": "evt_x"}),
        ev("evt_y", cats=["food"], activity={"namespace": "activity", "id": "evt_x"}),
    ])
    assert result["members"] == {"activity:evt_x": ["evt_x", "evt_y"]}
    edges = [edge for edge in result["edges"] if edge["relation"] == "member_of"]
    assert {edge["from_id"] for edge in edges} == {"evt_x", "evt_y"}
    assert result["member_meta"]["activity:evt_x"]["declaration_consistent"] is True


def test_gap_b_meal_and_expense_share_explicit_ref() -> None:
    """② 相同 data.activity_ref 下 meal 与 expense 共存；交易各自保留、不求和。"""
    meal = ev("m1", cats=["food"], activity={"namespace": "activity", "id": "A1"},
              data={"transaction_id": "T1"})
    pay = ev("p1", cats=["expense"], activity={"namespace": "activity", "id": "A1"},
             data={"transaction_id": "T2"})
    result = build_activity_links([meal, pay])
    assert result["members"] == {"activity:A1": ["m1", "p1"]}
    assert result["conflicts"] == []
    meta = result["member_meta"]["activity:A1"]
    assert meta["kinds"] == ["expense", "food"]
    assert meta["kind_coexist"] == [{"event_id": "p1", "kind": "expense"}]
    assert result["transactions"]["m1"][0]["id"] == "T1"
    assert result["transactions"]["p1"][0]["id"] == "T2"
