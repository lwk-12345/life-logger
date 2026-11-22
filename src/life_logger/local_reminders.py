"""Local-first todo reminders with a pluggable notification backend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from life_logger.timeutil import LOCAL_TZ


class ReminderNotifier(Protocol):
    """Minimal delivery interface so local reminders do not depend on cloud push."""

    name: str

    def notify(self, *, title: str, body: str) -> bool: ...


class LocalDesktopNotifier:
    """Deliver a macOS Notification Center message using the local osascript binary."""

    name = "macos_notification"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def notify(self, *, title: str, body: str) -> bool:
        if sys.platform != "darwin":
            return False
        script = (
            f'display notification "{self._escape(body)}" '
            f'with title "{self._escape(title)}"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        return True


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(tz=LOCAL_TZ)).isoformat()


def _parse_reminder_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("缺少提醒时间")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError("提醒时间必须是 ISO 8601 格式") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Offset-free manual due dates/times are Beijing business wall time.
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed


OPENCLAW_SOURCE = "openclaw"
UPSTREAM_REMOVED_REASON = "upstream_removed"


def _record_belongs_to_source(
    record: Mapping[str, Any], key: str, source: str
) -> bool:
    """只认「硬标记」判断一条本地记录是否来自某个上游来源。

    绝不按标题 / source_type 猜：用户手动添加的条目（source_type=manual 但不是
    OpenClaw 来的）必须永远不可能命中，否则清理逻辑会误删人的东西。
    OpenClaw 的硬标记有两个：同步时落盘的 `openclaw_id`，以及 OpenClaw 专用的
    键前缀（`oc_task_` 是上游任务 id，`oc::` 是上游没给 id 时的兜底键）。
    """
    if source != OPENCLAW_SOURCE:
        return False
    if str(record.get("openclaw_id") or "").strip():
        return True
    text = str(key or "")
    return text.startswith("oc_task_") or text.startswith("oc::")


class LocalReminderStore:
    """Persist todo-derived reminders without changing the event contract."""

    def __init__(self, root: Path, *, notifier: ReminderNotifier | None = None):
        self.root = Path(root)
        self.path = self.root / "reminders.json"
        self.notifier = notifier or LocalDesktopNotifier()
        from life_logger.runtime_queue import ProcessRLock
        self._lock = ProcessRLock(self.root / ".reminders.lock")

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        reminders = payload.get("reminders") if isinstance(payload, dict) else None
        return {
            "schema_version": "local-reminders-v1",
            "backend": "local",
            "reminders": reminders if isinstance(reminders, dict) else {},
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            payload = self._read_unlocked()
            rows = [
                dict(item)
                for item in payload["reminders"].values()
                if isinstance(item, dict) and item.get("task_key")
            ]
        # Deduplicate rows by (event_id or title) so duplicates under different keys do not repeat
        seen = set()
        deduped = []
        for item in rows:
            ident = item.get("event_id") or item.get("title") or item.get("task_key")
            if ident in seen:
                continue
            seen.add(ident)
            deduped.append(item)
        return sorted(
            deduped,
            key=lambda item: (
                str(item.get("status") in {"completed", "dismissed"}),
                str(item.get("remind_at") or "9999"),
                str(item.get("updated_at") or ""),
            ),
        )

    def clean_invalid_reminders(self) -> int:
        """Purge stored reminders whose titles are not genuine actionable todos.

        另外负责把「上游已删除」的条目标记最终清出文件：sync_tasks 会把上游
        消失的 OpenClaw 记录标成 dismissed + dismissed_reason="upstream_removed"
        （过期），这里把它们真正删除。`review_status == "confirmed"` 对此不再
        豁免 —— 走 OpenClaw 路径的【语音自查】条目全是 confirmed，正是这个豁免
        让它们永远清不掉。

        保护规则不变：已设提醒时间（status=scheduled 或 remind_at）的条目跳过，
        用户手动添加（无 OpenClaw 硬标记）的 confirmed 条目照旧跳过。
        """
        from life_logger.extract import _is_actionable_todo

        cleaned_count = 0
        with self._lock:
            payload = self._read_unlocked()
            reminders = payload["reminders"]
            to_delete = []
            for k, v in list(reminders.items()):
                if not isinstance(v, dict):
                    to_delete.append(k)
                    continue
                if v.get("status") == "scheduled" or v.get("remind_at"):
                    continue
                if v.get(
                    "dismissed_reason"
                ) == UPSTREAM_REMOVED_REASON and _record_belongs_to_source(
                    v, k, OPENCLAW_SOURCE
                ):
                    to_delete.append(k)
                    cleaned_count += 1
                    continue
                if v.get("review_status") == "confirmed":
                    continue
                # If an item originated from an audio extraction job (has job_id or artifact_path),
                # check whether its text is genuine actionable todo speech.
                if v.get("job_id") or v.get("artifact_path"):
                    title = str(v.get("title") or "")
                    if not _is_actionable_todo(title):
                        to_delete.append(k)
                        cleaned_count += 1
            for k in to_delete:
                reminders.pop(k, None)
            if cleaned_count:
                self._write_unlocked(payload)
        return cleaned_count

    def set_status(
        self,
        task_key: str,
        status: str,
    ) -> dict[str, Any]:
        task_key = str(task_key or "").strip()
        if not task_key:
            raise ValueError("缺少任务标识")
        if status not in {"unscheduled", "scheduled", "completed", "dismissed"}:
            raise ValueError("状态无效")
        now = _iso()
        with self._lock:
            payload = self._read_unlocked()
            reminders = payload["reminders"]
            target_keys = [
                k
                for k, v in reminders.items()
                if k == task_key
                or (
                    isinstance(v, dict)
                    and (v.get("event_id") == task_key or v.get("task_key") == task_key)
                )
            ]
            if not target_keys:
                target_keys = [task_key]
            updated_record: dict[str, Any] = {}
            for k in target_keys:
                current = reminders.get(k)
                record = dict(current) if isinstance(current, dict) else {}
                record.update(
                    task_key=k,
                    status=status,
                    updated_at=now,
                )
                reminders[k] = record
                updated_record = record
            self._write_unlocked(payload)
            return dict(updated_record)

    def sync_tasks(
        self,
        tasks: Sequence[Mapping[str, Any]],
        *,
        prune_missing_source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ensure every visible todo has a local reminder record.

        New todos are intentionally unscheduled: the system does not invent a
        reminder time.

        `prune_missing_source` 是「本批 tasks 完整代表的上游来源」（目前只有
        "openclaw"）。该来源在本地有记录、但本批里已消失（上游删除 / 归档 /
        完成）的条目会被标成 dismissed + dismissed_reason="upstream_removed"
        —— 即「过期」，随后由 clean_invalid_reminders 清出文件。这里只标不删，
        留一个宽限窗口：上游抖动时条目还在，只是不再算活跃。

        保护规则：已设提醒时间（status=scheduled 或 remind_at）的条目不动；
        用户手动添加的条目没有 OpenClaw 硬标记，永远不会被这里裁掉。
        上游条目重新出现时自动复活为 unscheduled（标记是清掉，不是永久封印）。
        """
        now = _iso()
        incoming_keys: set[str] = set()
        with self._lock:
            payload = self._read_unlocked()
            reminders = payload["reminders"]
            for task in tasks:
                task_key = str(
                    task.get("task_key") or task.get("event_id") or ""
                ).strip()
                if not task_key:
                    continue
                incoming_keys.add(task_key)
                source_status = str(task.get("status") or "pending")
                current = reminders.get(task_key)
                record = dict(current) if isinstance(current, dict) else {}
                record.update(
                    task_key=task_key,
                    title=str(
                        task.get("title") or record.get("title") or "未命名待办"
                    )[:400],
                    event_id=task.get("event_id") or record.get("event_id"),
                    job_id=task.get("job_id") or record.get("job_id"),
                    artifact_path=(
                        task.get("artifact_path") or record.get("artifact_path")
                    ),
                    backend="local",
                    source_status=source_status,
                    updated_at=now,
                    # 上游来源的硬标记：以后判断「这条是不是 OpenClaw 的」
                    # 只看它，不看标题、不看 source_type（两者都可能是人工的）。
                    openclaw_id=task.get("openclaw_id") or record.get("openclaw_id"),
                )
                record.setdefault("created_at", now)
                record.setdefault("remind_at", None)
                # 复核状态与来源必须落盘：下游（ESP32 徽章）只显示「人确认过的」
                # 待办。这里在键存在时无条件同步（含 None），保证上游取消确认后
                # 下游能立刻跟着失效；键缺失时才保留旧值，避免无谓改写。
                for key in ("review_status", "source_type"):
                    if key in task:
                        record[key] = task[key]
                if source_status in {"completed", "dismissed"}:
                    record["status"] = source_status
                elif not record.get("status"):
                    record["status"] = "unscheduled"
                # 上游重新出现的条目要能复活：只有本函数写过的 upstream_removed
                # 标记才清，用户自己 dismissed 的条目不受影响。
                if record.get("dismissed_reason") == UPSTREAM_REMOVED_REASON:
                    record.pop("dismissed_reason", None)
                    record.pop("dismissed_at", None)
                    if record.get("status") == "dismissed":
                        record["status"] = "unscheduled"
                reminders[task_key] = record
            if prune_missing_source:
                for key, raw in list(reminders.items()):
                    if key in incoming_keys or not isinstance(raw, dict):
                        continue
                    if not _record_belongs_to_source(raw, key, prune_missing_source):
                        continue
                    if raw.get("dismissed_reason") == UPSTREAM_REMOVED_REASON:
                        continue
                    if raw.get("status") == "scheduled" or raw.get("remind_at"):
                        continue
                    record = dict(raw)
                    record.update(
                        status="dismissed",
                        dismissed_reason=UPSTREAM_REMOVED_REASON,
                        dismissed_at=now,
                        updated_at=now,
                    )
                    reminders[key] = record
            self._write_unlocked(payload)
        return self.list()

    def schedule(
        self,
        task_key: str,
        *,
        remind_at: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        task_key = str(task_key or "").strip()
        if not task_key:
            raise ValueError("缺少任务标识")
        parsed = _parse_reminder_time(remind_at)
        now = _iso()
        with self._lock:
            payload = self._read_unlocked()
            reminders = payload["reminders"]
            current = reminders.get(task_key)
            record = dict(current) if isinstance(current, dict) else {}
            record.update(
                task_key=task_key,
                title=str(title or record.get("title") or "未命名待办")[:400],
                remind_at=parsed.isoformat(),
                backend="local",
                status="scheduled",
                updated_at=now,
                last_error=None,
            )
            record.setdefault("created_at", now)
            reminders[task_key] = record
            self._write_unlocked(payload)
            return dict(record)

    def dispatch_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current_time = now or datetime.now(tz=LOCAL_TZ)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("当前时间必须包含时区")
        delivered: list[dict[str, Any]] = []
        with self._lock:
            payload = self._read_unlocked()
            reminders = payload["reminders"]
            changed = False
            for key, raw in list(reminders.items()):
                if not isinstance(raw, dict) or raw.get("status") != "scheduled":
                    continue
                try:
                    reminder_time = _parse_reminder_time(
                        str(raw.get("remind_at") or "")
                    )
                except ValueError:
                    continue
                if reminder_time > current_time:
                    continue
                record = dict(raw)
                ok = self.notifier.notify(
                    title="Life Logger 待办提醒",
                    body=str(record.get("title") or "未命名待办"),
                )
                record["last_attempt_at"] = _iso(current_time)
                record["delivery_backend"] = self.notifier.name
                if ok:
                    record["status"] = "sent"
                    record["last_notified_at"] = _iso(current_time)
                    record["last_error"] = None
                    delivered.append(dict(record))
                else:
                    record["last_error"] = "local_notification_failed"
                reminders[key] = record
                changed = True
            if changed:
                self._write_unlocked(payload)
        return delivered
