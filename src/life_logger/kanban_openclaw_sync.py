"""看板操作 → OpenClaw task-store 同步（含持久重试队列）。

为什么要有它（用户 11-06 的要求）
--------------------------------
看板上的每个操作（完成 / 恢复 / 删除 / 拖动改状态 / 新增）都要同步进 OpenClaw，
否则两边各说各话：看板点了"完成"，OpenClaw 上还挂着。

之前只有**手动**触发的 `/api/openclaw/sync`（扫作业产物把 todo 推过去），
看板自己的状态改动一行都没进 task-store。本模块补上这一段。

三条硬约束（都是踩过的坑）
--------------------------
1. **本地写永远先成功**：OpenClaw 挂了不能让看板点不动。
   所以同步失败**不抛异常**，进持久重试队列，页面显示"未同步到 OpenClaw"，恢复后自动补。
2. **同一个任务只建一次**：没有映射时用 `task_key` 当 `event_id` 去 `create_task_from_life_event`，
   它内部把 Idempotency-Key 写成 `life-logger:event:<event_id>` —— 重试/并发都不会建出两条。
3. **版本冲突按最新重试一次**：复用 `web_openclaw.patch_task_status()`（409 → 读最新 → 再 PATCH 一次）。
   已经是目标状态就直接算成功，不再打一次。

映射与队列都存在 `output/local-web/kanban-openclaw-sync.json`：
    {"schema_version": "1", "tasks": {task_key: {...}}, "queue": [{...}]}
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

BJ_TZ = timezone(timedelta(hours=8))
SYNC_FILE = "kanban-openclaw-sync.json"
SCHEMA_VERSION = "1"

#: 看板状态 → OpenClaw 状态（同名词，显式写出来是为了让改动有迹可循）
STATUS_MAP = {
    "pending": "pending",
    "in_progress": "in_progress",
    "completed": "completed",
    "dismissed": "dismissed",
}

#: 这些状态不值得建任务（删除一个从未同步过的任务）
LOCAL_ONLY_STATUSES = {"dismissed"}

#: 自动补建时一次最多建几条，避免看板一打开就对着 bridge 打一串
AUTO_CREATE_LIMIT = 5


def _iso() -> str:
    return datetime.now(BJ_TZ).isoformat(timespec="seconds")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class KanbanOpenClawSync:
    """把看板的任务状态改动落到 OpenClaw task-store；失败进持久队列。"""

    def __init__(
        self,
        root: Path | str,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / SYNC_FILE
        self._client_factory = client_factory

    # ────────────────────────────────────────────────── 存取
    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": SCHEMA_VERSION, "tasks": {}, "queue": []}
        if not isinstance(payload, dict):
            return {"schema_version": SCHEMA_VERSION, "tasks": {}, "queue": []}
        payload.setdefault("schema_version", SCHEMA_VERSION)
        if not isinstance(payload.get("tasks"), dict):
            payload["tasks"] = {}
        if not isinstance(payload.get("queue"), list):
            payload["queue"] = []
        return payload

    def _save(self, payload: dict[str, Any]) -> None:
        try:
            _atomic_write(self.path, payload)
        except OSError as error:  # 磁盘坏了也不能把看板操作带崩
            sys.stderr.write(f"[kanban-sync] 写映射文件失败：{type(error).__name__}\n")
            sys.stderr.flush()

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from life_logger.openclaw_bridge import OpenClawBridgeClient

        return OpenClawBridgeClient(timeout=5.0)

    # ────────────────────────────────────────────────── 读状态（给页面用）
    def state(self, task_key: str) -> dict[str, Any]:
        payload = self._load()
        record = payload["tasks"].get(str(task_key) or "")
        if not isinstance(record, dict):
            return {"synced": False, "pending": False, "known": False}
        return {
            "synced": bool(record.get("task_id")) and not record.get("pending"),
            "pending": bool(record.get("pending")),
            "known": True,
            "task_id": record.get("task_id"),
            "last_error": record.get("last_error"),
            "synced_at": record.get("synced_at"),
        }

    def pending_count(self) -> int:
        return len(self._load()["queue"])

    # ────────────────────────────────────────────────── 同步一次改动
    def sync_change(
        self,
        *,
        task_key: str,
        status: str,
        title: str | None = None,
        event_id: str | None = None,
        artifact_path: str | None = None,
    ) -> dict[str, Any]:
        """把一次看板改动同步到 OpenClaw。**永不抛异常**。

        返回 {"synced": bool, "queued": bool, "task_id": str|None, "error": str|None}
        """
        task_key = str(task_key or "").strip()
        status = str(status or "").strip()
        if not task_key:
            return {"synced": False, "queued": False, "task_id": None, "error": "empty_task_key"}
        remote_status = STATUS_MAP.get(status)
        if remote_status is None:
            return {"synced": False, "queued": False, "task_id": None,
                    "error": f"unknown_status:{status}"}

        payload = self._load()
        record = payload["tasks"].get(task_key)
        record = record if isinstance(record, dict) else {}

        # 从没同步过又被删除：本地记 dismissed 就够，不去 OpenClaw 建一条随即删掉
        if not record.get("task_id") and status in LOCAL_ONLY_STATUSES:
            payload["tasks"][task_key] = {
                **record, "task_key": task_key, "status": status,
                "title": str(title or record.get("title") or "")[:400] or None,
                "skipped": "dismissed_before_create", "pending": False, "updated_at": _iso(),
            }
            self._save(payload)
            return {"synced": True, "queued": False, "task_id": None,
                    "error": None, "skipped": "dismissed_before_create"}

        try:
            result = self._apply(record, task_key=task_key, remote_status=remote_status,
                                 title=title, event_id=event_id)
        except Exception as error:  # bridge 不可用 / 409 二次冲突 / 其它
            message = f"{type(error).__name__}: {error}"[:300]
            payload = self._load()  # 重新读，避免覆盖期间别的改动
            record = payload["tasks"].get(task_key)
            record = dict(record) if isinstance(record, dict) else {}
            record.update({
                "task_key": task_key, "status": status,
                "title": str(title or record.get("title") or "")[:400] or None,
                "pending": True, "last_error": message, "updated_at": _iso(),
            })
            payload["tasks"][task_key] = record
            payload["queue"] = [item for item in payload["queue"]
                                if not (isinstance(item, dict) and item.get("task_key") == task_key)]
            payload["queue"].append({
                "task_key": task_key, "status": status,
                "title": str(title or "")[:400] or None,
                "event_id": event_id, "artifact_path": artifact_path,
                "queued_at": _iso(), "last_error": message,
            })
            self._save(payload)
            return {"synced": False, "queued": True, "task_id": record.get("task_id"),
                    "error": message}

        payload = self._load()
        record = dict(payload["tasks"].get(task_key) or {})
        record.update({
            "task_key": task_key, "task_id": result.get("task_id"),
            "revision": result.get("revision"), "status": status,
            "title": str(title or record.get("title") or "")[:400] or None,
            "event_id": event_id or record.get("event_id"),
            "pending": False, "last_error": None, "synced_at": _iso(), "updated_at": _iso(),
        })
        payload["tasks"][task_key] = record
        payload["queue"] = [item for item in payload["queue"]
                            if not (isinstance(item, dict) and item.get("task_key") == task_key)]
        self._save(payload)
        return {"synced": True, "queued": False, "task_id": result.get("task_id"), "error": None}

    def _apply(self, record: dict[str, Any], *, task_key: str, remote_status: str,
               title: str | None, event_id: str | None) -> dict[str, Any]:
        """真正打 bridge：有映射就 PATCH，没映射就 create。返回 {task_id, revision}。"""
        from life_logger.web_openclaw import patch_task_status

        client = self._client()
        task_id = str(record.get("task_id") or "").strip()
        if task_id:
            task = patch_task_status(client, task_id, remote_status, record.get("revision"))
            return {"task_id": task_id, "revision": (task or {}).get("revision")}

        created = client.create_task_from_life_event(
            {
                # external_id 用稳定的 task_key：重试用同一个幂等键，不会建出两条
                "event_id": str(event_id or task_key),
                "title": str(title or record.get("title") or task_key),
                "evidence": {"quote": None},
            }
        )
        task = (created or {}).get("task") if isinstance(created, dict) else None
        task = task if isinstance(task, dict) else {}
        new_id = str(task.get("id") or "").strip()
        if not new_id:
            raise RuntimeError(f"create 没有返回 task.id：{str(created)[:120]}")
        # 新任务默认 pending，需要的话再打一次状态（比如"直接建成 completed"）
        revision = task.get("revision")
        if str(task.get("status") or "") != remote_status:
            patched = patch_task_status(client, new_id, remote_status, revision)
            revision = (patched or {}).get("revision", revision)
        return {"task_id": new_id, "revision": revision}

    # ────────────────────────────────────────────────── 补队列
    def flush_queue(self, *, limit: int = 20) -> dict[str, Any]:
        """重试队列里的同步。返回 {"flushed": n, "remaining": m, "errors": [...]}"""
        payload = self._load()
        queue = [item for item in payload["queue"] if isinstance(item, dict)]
        if not queue:
            return {"flushed": 0, "remaining": 0, "errors": []}
        remaining: list[dict[str, Any]] = []
        flushed = 0
        errors: list[str] = []
        for index, item in enumerate(queue):
            if index >= limit:
                remaining.append(item)
                continue
            task_key = str(item.get("task_key") or "")
            result = self.sync_change(
                task_key=task_key,
                status=str(item.get("status") or "pending"),
                title=item.get("title"),
                event_id=item.get("event_id"),
                artifact_path=item.get("artifact_path"),
            )
            if result.get("synced"):
                flushed += 1
            else:
                errors.append(f"{task_key}: {result.get('error')}")
                remaining.append(item)
        payload = self._load()
        # sync_change 已经维护过队列，这里以本轮结果为准（避免重复累积）
        payload["queue"] = remaining
        self._save(payload)
        return {"flushed": flushed, "remaining": len(remaining), "errors": errors[:5]}

    # ────────────────────────────────────────────────── 新语音待办自动进
    def sync_new_todos(self, events: Iterable[dict[str, Any]], *,
                       limit: int = AUTO_CREATE_LIMIT) -> list[dict[str, Any]]:
        """把还没映射过的 todo 事件建成 OpenClaw 任务（幂等）。

        判据与 `OpenClawBridgeClient.sync_todo_events` 保持一致：
        `categories` 含 `todo`，或 `data.openclaw.candidate_type == "todo_candidate"`；
        `data.openclaw.ignored is True` 的跳过（已被判为历史垃圾）。
        """
        payload = self._load()
        known = payload["tasks"]
        outcomes: list[dict[str, Any]] = []
        for event in events:
            if len(outcomes) >= limit:
                break
            if not isinstance(event, dict):
                continue
            categories = event.get("categories") or []
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            openclaw_meta = data.get("openclaw") if isinstance(data.get("openclaw"), dict) else {}
            if openclaw_meta.get("ignored") is True:
                continue
            is_todo = ("todo" in categories) or (openclaw_meta.get("candidate_type") == "todo_candidate")
            if not is_todo:
                continue
            task_key = self.task_key_for_event(event)
            if not task_key or task_key in known:
                continue
            outcomes.append({
                "task_key": task_key,
                **self.sync_change(
                    task_key=task_key, status="pending",
                    title=str(event.get("title") or ""),
                    event_id=str(event.get("event_id") or ""),
                ),
            })
        return outcomes

    # ────────────────────────────────────────────────── 看板读取时自动补
    def auto_sync(self, events_provider: Callable[[], Iterable[dict[str, Any]]], *,
                  ttl: float = 60.0, limit: int = AUTO_CREATE_LIMIT) -> dict[str, Any]:
        """看板读取时顺手做两件事：补重试队列、把还没建的语音待办建出来。

        * 有 TTL（默认 60 秒）—— 看板一刷新就对着 bridge 打一串是不可接受的；
        * 全过程 try/except —— 自动同步坏了绝不能影响看板本身能不能打开。
        """
        state = self._load()
        last = str(state.get("last_auto_at") or "")
        if last:
            try:
                if (datetime.now(BJ_TZ) - datetime.fromisoformat(last)).total_seconds() < ttl:
                    return {"skipped": "ttl", "queue": self.pending_count()}
            except ValueError:
                pass
        try:
            flushed = self.flush_queue()
            created = self.sync_new_todos(events_provider(), limit=limit)
        except Exception as error:  # noqa: BLE001 - 自动同步不允许把看板带崩
            return {"error": f"{type(error).__name__}: {error}"[:200]}
        state = self._load()
        state["last_auto_at"] = _iso()
        self._save(state)
        return {
            "flushed": flushed.get("flushed", 0),
            "created": sum(1 for item in created if item.get("synced")),
            "queued": sum(1 for item in created if item.get("queued")),
            "remaining": self.pending_count(),
        }

    @staticmethod
    def task_key_for_event(event: dict[str, Any]) -> str:
        """事件 → 稳定 task_key。优先用 event_id（与 OpenClaw 的幂等键同源）。"""
        event_id = str(event.get("event_id") or "").strip()
        if event_id:
            return f"event:{event_id}"
        return ""
