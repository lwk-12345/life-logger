"""Durable local dispatch and cross-process ownership, without HTTP/model imports."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterator


class DispatchConflict(ValueError):
    pass


class ProcessRLock:
    """An RLock for threads plus an advisory file lock for independent processes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._file = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        started = time.monotonic()
        if not self._thread_lock.acquire(blocking, timeout):
            return False
        if self._depth:
            self._depth += 1
            return True
        handle = self.path.open("a+b")
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not blocking or (timeout >= 0 and time.monotonic() - started >= timeout):
                        handle.close()
                        self._thread_lock.release()
                        return False
                    time.sleep(0.01)
            self._file = handle
            self._depth = 1
            return True
        except BaseException:
            handle.close()
            self._thread_lock.release()
            raise

    def release(self) -> None:
        # Let RLock enforce thread ownership before touching the OS lock.
        if not self._thread_lock._is_owned():
            raise RuntimeError("cannot release unowned process lock")
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._file = None
        self._thread_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class WorkerLease:
    """One coordinator per data root. A Web process never holds this lease."""

    def __init__(self, root: Path):
        self.path = Path(root) / "runtime" / "worker.lock"
        self.handle = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self.handle = handle
        return True

    def close(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise DispatchConflict("processing worker already owns this data root")
        return self

    def __exit__(self, *_):
        self.close()


class TaskQueue:
    """Small SQLite outbox. Accepting work never runs it in the caller."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = runtime / "tasks.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, owner TEXT,
                    result TEXT, error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS task_active_key ON tasks(dedupe_key)
                    WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS task_pending ON tasks(status, priority, created);
                CREATE TABLE IF NOT EXISTS request_aliases (
                    request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    owner TEXT NOT NULL, pid INTEGER NOT NULL, heartbeat REAL NOT NULL,
                    build TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def decode(row) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result"] = json.loads(item["result"]) if item["result"] is not None else None
        return item

    def enqueue(self, kind: str, key: str, payload: dict[str, Any], *, priority: int = 20,
                reuse_finished: bool = False, request_id: str = '') -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError("dispatch payload exceeds 1 MiB")
        if not kind or len(kind) > 64 or not key or len(key) > 512:
            raise ValueError("invalid dispatch kind/key")
        now, identifier = time.time(), uuid.uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if request_id:
                    aliased = db.execute('SELECT tasks.* FROM tasks JOIN request_aliases ON tasks.id=request_aliases.task_id WHERE request_id=?', (request_id,)).fetchone()
                    if aliased is not None:
                        if aliased['kind'] != kind or aliased['payload'] != encoded:
                            raise DispatchConflict('request identifier was used with different work')
                        db.commit()
                        return self.decode(aliased)
                query = 'SELECT * FROM tasks WHERE dedupe_key=?'
                if not reuse_finished:
                    query += " AND status IN ('queued','running')"
                previous = db.execute(query + ' ORDER BY created DESC LIMIT 1', (key,)).fetchone()
                if previous is not None:
                    if previous["kind"] != kind or previous["payload"] != encoded:
                        raise DispatchConflict("different work already owns this dispatch key")
                    if previous["status"] == "queued" and int(priority) < previous["priority"]:
                        db.execute('UPDATE tasks SET priority=? WHERE id=?', (int(priority), previous['id']))
                        previous = db.execute('SELECT * FROM tasks WHERE id=?', (previous['id'],)).fetchone()
                    if request_id:
                        db.execute('INSERT INTO request_aliases VALUES(?,?)', (request_id,previous['id']))
                    db.commit()
                    return self.decode(previous)
                db.execute("INSERT INTO tasks(id,kind,dedupe_key,payload,status,priority,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                           (identifier, kind, key, encoded, "queued", int(priority), now, now))
                row = db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone()
                if request_id:
                    db.execute('INSERT INTO request_aliases VALUES(?,?)', (request_id,identifier))
                db.commit()
                return self.decode(row)
            except BaseException:
                db.rollback()
                raise

    def claim(self, owner: str, *, kinds: tuple[str, ...]) -> dict[str, Any] | None:
        if not kinds:
            return None
        marks = ",".join("?" for _ in kinds)
        with self.connection() as db:
            # Idle polling must stay read-only: WAL readers never wait on the write lock.
            if db.execute(f"SELECT 1 FROM tasks WHERE status='queued' AND kind IN ({marks}) LIMIT 1", kinds).fetchone() is None:
                return None
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(f"SELECT * FROM tasks WHERE status='queued' AND kind IN ({marks}) ORDER BY priority,created LIMIT 1", kinds).fetchone()
                if row is None:
                    db.commit()
                    return None
                db.execute("UPDATE tasks SET status='running',owner=?,updated=? WHERE id=? AND status='queued'", (owner, time.time(), row["id"]))
                claimed = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
                db.commit()
                return self.decode(claimed)
            except BaseException:
                db.rollback()
                raise

    def finish(self, identifier: str, owner: str, *, result=None, error: str | None = None) -> bool:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
        with self.connection() as db:
            cursor = db.execute("UPDATE tasks SET status=?,result=?,error=?,updated=? WHERE id=? AND owner=? AND status='running'",
                                ("failed" if error else "succeeded", encoded, error, time.time(), identifier, owner))
            return cursor.rowcount == 1

    def recover(self) -> dict[str, int]:
        """Call only while holding WorkerLease; never blindly repeat model side effects."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            safe = db.execute("UPDATE tasks SET status='queued',owner=NULL,updated=? WHERE status='running' AND kind IN ('projection','optional-projection','refresh-catalog')", (time.time(),)).rowcount
            interrupted = db.execute("UPDATE tasks SET status='interrupted',error='worker exited before acknowledgment; inspect checkpoints before retry',updated=? WHERE status='running'", (time.time(),)).rowcount
            db.commit()
            return {"requeued_reads": safe, "interrupted_processing": interrupted}

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self.connection() as db:
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (identifier,)).fetchone())

    def heartbeat(self, owner: str, build: str) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO worker VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,pid=excluded.pid,heartbeat=excluded.heartbeat,build=excluded.build",
                       (owner, os.getpid(), time.time(), build))

    def offline(self, owner: str) -> None:
        with self.connection() as db:
            db.execute('UPDATE worker SET heartbeat=0 WHERE singleton=1 AND owner=?', (owner,))

    def health(self, *, max_age: float = 15) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM worker WHERE singleton=1").fetchone()
            counts = dict(db.execute("SELECT status,count(*) FROM tasks GROUP BY status").fetchall())
        if row is None:
            return {"online": False, "tasks": counts}
        heartbeat = dict(row)
        alive = True
        try:
            os.kill(int(heartbeat["pid"]), 0)
        except (OSError, ValueError):
            alive = False
        return {"online": alive and 0 <= time.time() - heartbeat["heartbeat"] <= max_age, "tasks": counts,
                "pid": heartbeat["pid"], "build": heartbeat["build"],
                "heartbeat": heartbeat["heartbeat"]}
