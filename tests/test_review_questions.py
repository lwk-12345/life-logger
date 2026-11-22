"""来源判断反馈：存取/撤销/幂等/非法来源/证据保真（隔离临时 root）。"""
from __future__ import annotations

import pytest

from life_logger.review_questions import (
    ReviewQuestionError,
    answer_review_question,
    get_review_question,
    undo_review_question,
)

SOURCE = {"source_id": "src_1", "segment_id": "seg_1", "job_id": "job_1",
          "recording_id": "rec_1", "quote": "买咖啡", "extracted_content": "咖啡"}


def lookup(sid):
    return SOURCE if sid == "src_1" else None


def test_answer_persists_state_and_evidence(tmp_path):
    out = answer_review_question(tmp_path, source_id="src_1", operation="query", op_id="op1",
                                 source_lookup=lookup)
    assert out["ok"] and out["state"] == "answered"
    assert out["evidence"] == {"source_id": "src_1", "segment_id": "seg_1", "job_id": "job_1",
                               "recording_id": "rec_1", "quote": "买咖啡", "extracted_content": "咖啡"}
    assert out["learning_applied"] is False and out["accounting_changed"] is False
    assert get_review_question(tmp_path, source_id="src_1")["state"] == "answered"


def test_media_mutes_but_remembers_prior_and_undo_restores(tmp_path):
    answer_review_question(tmp_path, source_id="src_1", operation="personal", op_id="op0", source_lookup=lookup)
    muted = answer_review_question(tmp_path, source_id="src_1", operation="media", op_id="op1", source_lookup=lookup)
    assert muted["state"] == "muted" and muted["effects"]["reminder_muted"] is True
    undone = undo_review_question(tmp_path, source_id="src_1", op_id="op1")
    assert undone["state"] == "answered", "undo 恢复为上一状态而不是删除记录"
    assert undone["effects"]["reminder_restored"]


def test_idempotent_duplicate_op_id(tmp_path):
    first = answer_review_question(tmp_path, source_id="src_1", operation="query", op_id="dup", source_lookup=lookup)
    second = answer_review_question(tmp_path, source_id="src_1", operation="query", op_id="dup", source_lookup=lookup)
    assert second["idempotent"] is True and second["state"] == first["state"]
    assert get_review_question(tmp_path, source_id="src_1")["revision"] == 1


def test_unknown_source_rejected(tmp_path):
    with pytest.raises(ReviewQuestionError):
        answer_review_question(tmp_path, source_id="src_missing", operation="query", source_lookup=lookup)


def test_invalid_operation_rejected(tmp_path):
    with pytest.raises(ReviewQuestionError):
        answer_review_question(tmp_path, source_id="src_1", operation="delete_all", source_lookup=lookup)


def test_xss_reason_is_stored_as_text(tmp_path):
    reason = "<img src=x onerror=alert(1)>"
    answer_review_question(tmp_path, source_id="src_1", operation="uncertain", op_id="x1",
                           reason=reason, source_lookup=lookup)
    stored = get_review_question(tmp_path, source_id="src_1")["reason"]
    assert stored == reason, "原样存储，不解释；渲染层负责转义"


def test_uncertain_keeps_unknown_without_guessing(tmp_path):
    out = answer_review_question(tmp_path, source_id="src_1", operation="uncertain",
                                 op_id="u1", source_lookup=lambda sid: {"source_id": sid})
    assert out["state"] == "uncertain"
    assert out["evidence"] == {"source_id": "src_1"}, "缺证据不推断"


def test_undo_without_history_rejected(tmp_path):
    with pytest.raises(ReviewQuestionError):
        undo_review_question(tmp_path, source_id="src_1")


def test_undo_twice_rejected(tmp_path):
    answer_review_question(tmp_path, source_id="src_1", operation="query", op_id="o1", source_lookup=lookup)
    undo_review_question(tmp_path, source_id="src_1", op_id="o1")
    with pytest.raises(ReviewQuestionError):
        undo_review_question(tmp_path, source_id="src_1", op_id="o1")

# ===== 闭环返工：pending 投影 / GET 只读 / op_id 归属 / 旧 undo =====


def test_muted_source_hidden_from_pending_until_resolved(tmp_path, monkeypatch):
    import life_logger.pending_review as pr

    fake = {"ok": True, "groups": [{"key": "needs_review", "items": [
        {"source_id": "src_1", "state": "open", "date": "2026-10-24", "group": "needs_review"},
        {"source_id": "src_2", "state": "open", "date": "2026-10-24", "group": "needs_review"}]}]}
    monkeypatch.setattr(pr, "_load_pending_items", lambda *a, **k: fake, raising=False)
    answer_review_question(tmp_path, source_id="src_1", operation="media", op_id="m1", source_lookup=lookup)
    from life_logger.review_questions import muted_source_ids
    assert muted_source_ids(tmp_path) == {"src_1"}


def test_apply_question_mute_hides_only_muted_and_keeps_trace():
    from life_logger.pending_review import _apply_question_mute
    items = [
        {"source_id": "src_1", "state": "open", "date": "2026-10-24", "review_question_state": "muted"},
        {"source_id": "src_2", "state": "open", "date": "2026-10-24"},
        {"source_id": "src_3", "state": "ignored", "date": "2026-10-24"},
    ]
    default = [i["source_id"] for i in _apply_question_mute(items, False, None)]
    assert default == ["src_2"], "muted 与 ignored 默认都不显示"
    traced = [i["source_id"] for i in _apply_question_mute(items, True, None)]
    assert traced == ["src_1", "src_2", "src_3"], "include_resolved 时可追溯 muted"


def test_muted_ids_projection_is_readonly(tmp_path):
    from life_logger.review_questions import muted_source_ids
    assert muted_source_ids(tmp_path) == set()
    assert not (tmp_path / "review-questions" / "review-questions.sqlite3").exists(), "只读投影不得建 db"


def test_get_does_not_create_db(tmp_path):
    out = get_review_question(tmp_path, source_id="src_1")
    assert out["state"] == "unanswered"
    assert not (tmp_path / "review-questions").exists()


def test_op_id_cannot_be_reused_across_sources(tmp_path):
    answer_review_question(tmp_path, source_id="src_1", operation="query", op_id="shared", source_lookup=lookup)
    with pytest.raises(ReviewQuestionError):
        answer_review_question(tmp_path, source_id="src_2", operation="query", op_id="shared",
                               source_lookup=lambda sid: {"source_id": sid})


def test_old_undo_cannot_override_newer_op(tmp_path):
    answer_review_question(tmp_path, source_id="src_1", operation="query", op_id="o1", source_lookup=lookup)
    answer_review_question(tmp_path, source_id="src_1", operation="personal", op_id="o2", source_lookup=lookup)
    with pytest.raises(ReviewQuestionError):
        undo_review_question(tmp_path, source_id="src_1", op_id="o1")
    assert get_review_question(tmp_path, source_id="src_1")["state"] == "answered"
