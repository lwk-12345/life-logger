"""来源判断问答（可撤销反馈）——首版。

只记录「用户对某条 pending 来源的判断」，不改事件、金额、抽取产物、不执行查询、不训练模型。
- 以明确的 source_id 为准，且该来源必须真实存在于待审核投影中；
- 结构化审计独立原子写（SQLite，WAL），操作按 op_id 幂等、可 undo；
- 「关闭提醒」复用 pending 状态，但保存 prior_state，undo 时恢复；
- 证据（source/segment/job）有则存，未知不猜；缺证据视为 uncertain。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "review-questions-v1"
OPERATIONS = ("query", "media", "personal", "uncertain")
MUTE_OPERATIONS = ("media",)          # 仅“媒体”可暂时关闭提醒；不是永久排除相似文本

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS question_state (
  source_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  prior_status TEXT,
  reason TEXT,
  evidence_json TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS question_audit (
  op_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  previous_state TEXT,
  previous_prior_status TEXT,
  reason TEXT,
  evidence_json TEXT,
  created_at REAL NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0
);
"""


class ReviewQuestionError(ValueError):
    """输入/状态错误（HTTP 400 语义）。"""


def _db(root: Path | str, *, create: bool = True) -> sqlite3.Connection | None:
    path = Path(root) / "review-questions"
    db_path = path / "review-questions.sqlite3"
    if not create and not db_path.exists():
        return None                      # GET/投影不得创建 db
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _evidence_for(source: Mapping[str, Any] | None) -> dict[str, Any]:
    """只复制已有证据字段；未知保持缺省，不推断。"""
    if not isinstance(source, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in ("source_id", "segment_id", "job_id", "recording_id", "source_label", "quote", "extracted_content"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:400]
    return out


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def answer_review_question(
    root: Path | str,
    *,
    source_id: str,
    operation: str,
    op_id: str = "",
    reason: str = "",
    source_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    sid = _clean(source_id, 120)
    if not sid:
        raise ReviewQuestionError("缺少 source_id")
    op = _clean(operation, 20).lower()
    if op not in OPERATIONS:
        raise ReviewQuestionError("不支持的判断：" + op)
    if source_lookup is not None and source_lookup(sid) is None:
        raise ReviewQuestionError("来源不存在或已不在待审核列表：" + sid)
    evidence = _evidence_for(source_lookup(sid) if source_lookup else None)
    stamp = float(now if now is not None else time.time())
    opid = _clean(op_id, 80) or uuid.uuid4().hex

    conn = _db(root)
    try:
        with conn:
            existing = conn.execute(
                "SELECT * FROM question_audit WHERE op_id = ?", (opid,)
            ).fetchone()
            if existing:
                if str(existing[1]) != sid:
                    raise ReviewQuestionError("op_id 已被其他来源使用：" + opid)
                row = conn.execute(
                    "SELECT state, prior_status FROM question_state WHERE source_id = ?", (sid,)
                ).fetchone()
                return _response(sid, op, opid, row[0] if row else "uncertain",
                                 row[1] if row else None, evidence, stamp, True, bool(existing[8]))
            row = conn.execute(
                "SELECT state, prior_status FROM question_state WHERE source_id = ?", (sid,)
            ).fetchone()
            previous_state = row[0] if row else None
            previous_prior = row[1] if row else None
            if op == "uncertain":
                new_state, prior_status = "uncertain", previous_prior
            elif op in MUTE_OPERATIONS:
                new_state, prior_status = "muted", (previous_prior or "open")
            else:
                new_state, prior_status = "answered", previous_prior
            conn.execute(
                "INSERT INTO question_audit (op_id, source_id, operation, previous_state, previous_prior_status,"
                " reason, evidence_json, created_at, undone) VALUES (?,?,?,?,?,?,?,?,0)",
                (opid, sid, op, previous_state, previous_prior, _clean(reason, 200),
                 json.dumps(evidence, ensure_ascii=False), stamp),
            )
            conn.execute(
                "INSERT INTO question_state (source_id, state, prior_status, reason, evidence_json, revision, updated_at)"
                " VALUES (?,?,?,?,?,1,?) ON CONFLICT(source_id) DO UPDATE SET"
                " state=excluded.state, prior_status=excluded.prior_status, reason=excluded.reason,"
                " evidence_json=excluded.evidence_json, revision=revision+1, updated_at=excluded.updated_at",
                (sid, new_state, prior_status, _clean(reason, 200),
                 json.dumps(evidence, ensure_ascii=False), stamp),
            )
    finally:
        conn.close()
    return _response(sid, op, opid, new_state, prior_status, evidence, stamp, False, False)


def undo_review_question(
    root: Path | str,
    *,
    source_id: str,
    op_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    sid = _clean(source_id, 120)
    if not sid:
        raise ReviewQuestionError("缺少 source_id")
    conn = _db(root)
    try:
        with conn:
            if op_id:
                row = conn.execute(
                    "SELECT op_id, operation, previous_state, previous_prior_status, undone FROM question_audit"
                    " WHERE op_id = ? AND source_id = ?", (_clean(op_id, 80), sid)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT op_id, operation, previous_state, previous_prior_status, undone FROM question_audit"
                    " WHERE source_id = ? ORDER BY created_at DESC LIMIT 1", (sid,)
                ).fetchone()
            if not row or row[4]:
                raise ReviewQuestionError("没有可撤销的操作")
            latest = conn.execute(
                "SELECT op_id FROM question_audit WHERE source_id = ? AND undone = 0"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1", (sid,)
            ).fetchone()
            if latest and latest[0] != row[0]:
                raise ReviewQuestionError("只能撤销最近一次操作")
            prev_state = row[2] or "uncertain"
            prev_prior = row[3]
            conn.execute("UPDATE question_audit SET undone = 1 WHERE op_id = ?", (row[0],))
            conn.execute(
                "UPDATE question_state SET state = ?, prior_status = ?, revision = revision + 1, updated_at = ?"
                " WHERE source_id = ?", (prev_state, prev_prior, float(now if now is not None else time.time()), sid)
            )
    finally:
        conn.close()
    return {
        "schema": SCHEMA, "ok": True, "source_id": sid, "undone_op_id": row[0],
        "state": prev_state, "prior_status": prev_prior,
        "effects": {"question_state": prev_state, "reminder_restored": prev_prior or "open"},
        "learning_applied": False, "accounting_changed": False,
    }


def _response(sid, op, opid, state, prior, evidence, stamp, idempotent, undone) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "ok": True, "source_id": sid, "operation": op, "op_id": opid,
        "state": state, "prior_status": prior,
        "effects": {
            "question_state": state,
            "reminder_muted": state == "muted",
            "scope": "this_source_only",
        },
        "evidence": evidence,
        "learning_applied": False,
        "accounting_changed": False,
        "idempotent": bool(idempotent),
        "undone": bool(undone),
        "answered_at": stamp,
    }


def review_question_states(root: Path | str, source_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """批量投影：一次查询多条来源的状态 + 最近一次操作（供 pending 列表用，避免 N+1）。"""
    wanted = [str(sid) for sid in (source_ids or []) if str(sid).strip()]
    if not wanted:
        return {}
    conn = _db(root, create=False)
    if conn is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        placeholders = ",".join("?" for _ in wanted)
        rows = conn.execute(
            f"SELECT source_id, state, prior_status, reason, updated_at FROM question_state"
            f" WHERE source_id IN ({placeholders})", wanted
        ).fetchall()
        for source_id, state, prior, reason, stamp in rows:
            op = conn.execute(
                "SELECT operation, op_id, undone FROM question_audit WHERE source_id = ?"
                " AND undone = 0 ORDER BY created_at DESC LIMIT 1", (source_id,)
            ).fetchone()
            out[str(source_id)] = {
                "state": str(state),
                "prior_status": prior,
                "reason": reason,
                "answered_at": stamp,
                "operation": (op[0] if op else None),
                "op_id": (op[1] if op else None),
                "undoable": bool(op) and str(state) != "unanswered",
            }
    finally:
        conn.close()
    return out


def muted_source_ids(root: Path | str) -> set[str]:
    """只读投影：当前被“媒体”静音的来源（不创建 db）。"""
    conn = _db(root, create=False)
    if conn is None:
        return set()
    try:
        rows = conn.execute("SELECT source_id FROM question_state WHERE state = 'muted'").fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def get_review_question(root: Path | str, *, source_id: str) -> dict[str, Any]:
    sid = _clean(source_id, 120)
    if not sid:
        raise ReviewQuestionError("缺少 source_id")
    conn = _db(root, create=False)
    if conn is None:
        return {"schema": SCHEMA, "ok": True, "source_id": sid, "state": "unanswered",
                "prior_status": None, "evidence": {}, "learning_applied": False,
                "accounting_changed": False}
    try:
        row = conn.execute(
            "SELECT state, prior_status, reason, evidence_json, revision, updated_at"
            " FROM question_state WHERE source_id = ?", (sid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"schema": SCHEMA, "ok": True, "source_id": sid, "state": "unanswered",
                "prior_status": None, "evidence": {}, "learning_applied": False,
                "accounting_changed": False}
    return {"schema": SCHEMA, "ok": True, "source_id": sid, "state": row[0],
            "prior_status": row[1], "reason": row[2],
            "evidence": json.loads(row[3] or "{}"), "revision": row[4],
            "learning_applied": False, "accounting_changed": False}
