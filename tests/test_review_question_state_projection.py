"""Q408：answer → 待回答列表可见状态 → 重建 reader → undo → 列表恢复（隔离 root）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import pending_review  # noqa: E402
from life_logger.review_questions import (  # noqa: E402
    answer_review_question,
    review_question_states,
    undo_review_question,
)

SOURCE = {"source_id": "src_proj_1", "segment_id": "seg_1", "job_id": "job_1", "quote": "我加个烤肠"}


def _items(sid="src_proj_1"):
    return {
        sid: {
            "source_id": sid, "state": "open", "group": pending_review.GROUP_NEEDS_REVIEW,
            "date": "2026-10-25", "rank": 0, "review_question_state": None,
        }
    }


def _project(tmp_path, items, *, include_resolved=True):
    """复刻 collect_pending_sources 的投影段（同一函数、同一顺序）。"""
    states = review_question_states(tmp_path, [str(i.get("source_id") or "") for i in items.values()])
    for item in items.values():
        state = states.get(str(item.get("source_id") or ""))
        if state:
            item["review_question_state"] = state["state"]
            item["review_question_operation"] = state.get("operation")
            item["review_question_op_id"] = state.get("op_id")
            item["review_question_answered_at"] = state.get("answered_at")
            item["review_question_prior_status"] = state.get("prior_status")
            item["review_question_undoable"] = state.get("undoable", False)
        else:
            if not item.get("review_question_state"):
                item["review_question_state"] = "unknown"
            item.setdefault("review_question_undoable", False)
    return pending_review._apply_question_mute(list(items.values()), include_resolved, None)


@pytest.mark.parametrize("operation,expected_state", [
    ("query", "answered"),
    ("personal", "answered"),
    ("uncertain", "uncertain"),
])
def test_answered_and_uncertain_are_visible_with_state(tmp_path, operation, expected_state):
    resp = answer_review_question(tmp_path, source_id="src_proj_1", operation=operation, source_lookup=lambda sid: SOURCE if sid == "src_proj_1" else None)
    assert resp["state"] == expected_state
    items = _project(tmp_path, _items())
    assert len(items) == 1, "query/personal/uncertain 不得从待回答消失"
    item = items[0]
    assert item["review_question_state"] == expected_state
    assert item["review_question_operation"] == operation
    assert item["review_question_op_id"] == resp["op_id"]
    assert item["review_question_undoable"] is True
    assert item["review_question_answered_at"]


def test_media_muted_hidden_then_undo_restores(tmp_path):
    resp = answer_review_question(tmp_path, source_id="src_proj_1", operation="media", source_lookup=lambda sid: SOURCE if sid == "src_proj_1" else None)
    assert resp["state"] == "muted"
    assert _project(tmp_path, _items(), include_resolved=False) == [], "media 静音后默认隐藏（既有路径）"
    assert _project(tmp_path, _items(), include_resolved=True)[0]["review_question_state"] == "muted", "含已处理时仍可追溯"
    undo = undo_review_question(tmp_path, source_id="src_proj_1")
    assert undo["ok"] is True
    restored = _project(tmp_path, _items())
    assert len(restored) == 1, "undo 后必须重新出现在待回答"
    # undo 恢复的是 prior_status（无历史时为 uncertain），关键是不再是 muted 且条目可见
    assert restored[0]["review_question_state"] != "muted"
    assert restored[0]["review_question_state"] in {"open", "unanswered", "uncertain"}


def test_state_survives_new_reader(tmp_path):
    answer_review_question(tmp_path, source_id="src_proj_1", operation="query", source_lookup=lambda sid: SOURCE if sid == "src_proj_1" else None)
    first = _project(tmp_path, _items())[0]["review_question_state"]
    # 重建 reader（新的 items/新的连接）后仍能读到
    second = _project(tmp_path, _items())[0]["review_question_state"]
    assert first == second == "answered"


def test_missing_source_state_is_unknown_not_inferred(tmp_path):
    answer_review_question(tmp_path, source_id="src_proj_1", operation="query", source_lookup=lambda sid: SOURCE if sid == "src_proj_1" else None)
    items = _items()
    extra = dict(items["src_proj_1"], source_id="src_other", review_question_state=None)
    items["src_other"] = extra
    projected = {i["source_id"]: i for i in _project(tmp_path, items)}
    assert projected["src_other"]["review_question_state"] == "unknown"
    assert projected["src_other"]["review_question_undoable"] is False
    assert projected["src_proj_1"]["review_question_state"] == "answered", "跨来源不得串"


def test_idempotent_answer_keeps_single_state(tmp_path):
    first = answer_review_question(tmp_path, source_id="src_proj_1", operation="query",
                                   op_id="op_fixed", source_lookup=lambda sid: SOURCE if sid == "src_proj_1" else None)
    second = answer_review_question(tmp_path, source_id="src_proj_1", operation="query",
                                    op_id="op_fixed", source_lookup=lambda sid: SOURCE if sid == "src_proj_1" else None)
    assert second.get("idempotent") is True
    states = review_question_states(tmp_path, ["src_proj_1"])
    assert states["src_proj_1"]["state"] == "answered"
    assert states["src_proj_1"]["op_id"] == "op_fixed" == first["op_id"]
