"""从 local_web.py 机械搬出的 web_openclaw 部分，行为不变；分组依据见 work/split-prep-localweb-20261104/。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Mapping
from life_logger.web_common import _timestamp_key


def _openclaw_task_items(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return task objects already present in the bridge payload.

    This intentionally does not mutate or reclassify the task groups; it only
    reads them to derive timestamp metadata.
    """
    if not isinstance(data, Mapping):
        return []
    items: list[dict[str, Any]] = []
    for key in ("wechat_matters", "general_todos", "life_logger_todos", "tasks"):
        rows = data.get(key)
        if isinstance(rows, list):
            items.extend(row for row in rows if isinstance(row, dict))
    return items


def _openclaw_task_read_metadata(
    data: Mapping[str, Any] | None,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Describe timestamps without inventing a request-time clock.

    OpenClaw's ``unified-task-list-v1`` has no top-level sync/generation time.
    The only real time available is each task's ``updated_at``; expose its max
    as ``source_updated_at`` and keep ``last_sync``/``generated_at`` null with
    explicit reasons.  ``last_sync_at`` is a compatibility alias consumed by
    the existing UI, sourced from the real upstream task update when no true
    sync timestamp exists.
    """
    payload = data if isinstance(data, Mapping) else {}
    tasks = _openclaw_task_items(payload)
    source_updated_at = payload.get("source_updated_at")
    if not isinstance(source_updated_at, str) or not _timestamp_key(source_updated_at):
        source_updated_at = None
        newest_key: datetime | None = None
        for task in tasks:
            raw = task.get("updated_at")
            parsed = _timestamp_key(raw)
            if parsed is not None and (newest_key is None or parsed > newest_key):
                newest_key = parsed
                source_updated_at = str(raw)
    if source_updated_at and not _timestamp_key(source_updated_at):
        source_updated_at = None

    last_sync = payload.get("last_sync")
    if not isinstance(last_sync, str) or not _timestamp_key(last_sync):
        last_sync = None
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not _timestamp_key(generated_at):
        generated_at = None

    last_sync_reason = None
    if last_sync is None:
        last_sync_reason = (
            "upstream unified-task-list-v1 未提供 last_sync；"
            "不拿请求时间冒充同步时间"
        )
        if error:
            last_sync_reason = f"{last_sync_reason}；{str(error)[:160]}"
    generated_at_reason = None
    if generated_at is None:
        generated_at_reason = (
            "upstream unified-task-list-v1 未提供 generated_at；"
            "不拿请求时间冒充生成时间"
        )
    source_updated_at_reason = None
    if source_updated_at is None:
        source_updated_at_reason = (
            "上游任务没有可用的 updated_at（或当前任务池为空），"
            "source_updated_at 保持 null"
        )

    # Keep the frontend's existing last_sync_at branch honest and useful: it
    # consumes the real newest task update, never the current request clock.
    compatibility_last_sync = last_sync or source_updated_at
    read_contract = {
        "schema_version": "openclaw-task-read-contract-v1",
        "scope": "openclaw_tasks",
        "view": "dashboard",
        "canonical": False,
        "source": "openclaw-task-bridge",
        "upstream_schema": "unified-task-list-v1",
        "task_schema": "unified-task-v1",
        "scope_semantics": "所有日期的任务池；dashboard 分组只展示 open 状态",
        "timestamps": {
            "last_sync": "upstream field; null when absent",
            "source_updated_at": "max(task.updated_at) among returned tasks; null when absent",
            "generated_at": "upstream field; null when absent",
        },
        "last_sync_available": last_sync is not None,
        "source_updated_at_available": source_updated_at is not None,
        "generated_at_available": generated_at is not None,
        "last_sync_at_semantics": "compatibility alias = last_sync or source_updated_at",
    }
    return {
        "last_sync": last_sync,
        "last_sync_at": compatibility_last_sync,
        "last_sync_basis": "last_sync" if last_sync else ("source_updated_at" if source_updated_at else None),
        "last_sync_reason": last_sync_reason,
        "source_updated_at": source_updated_at,
        "source_updated_at_reason": source_updated_at_reason,
        "generated_at": generated_at,
        "generated_at_reason": generated_at_reason,
        "read_contract": read_contract,
    }


def patch_task_status(client: Any, task_id: str, status: str, revision: Any) -> dict[str, Any]:
    """改任务状态。版本冲突时看最新状态：已是目标状态就算成功，否则按最新版本再试一次。

    冲突常见于超时重发（第一次其实已写入）和页面数据过期。这里只改状态，用户又明确点了
    操作，所以可以按最新版本重试；任务已不存在或第二次仍冲突才报错。
    """
    expected = int(revision) if revision is not None else None
    try:
        return client.patch_task(task_id, {"status": status}, expected_revision=expected)
    except RuntimeError as error:
        if "HTTP 409" not in str(error):
            raise
    current = client.get_task(task_id)
    if not isinstance(current, dict):
        raise RuntimeError(f"OpenClaw patch_task HTTP 409: task {task_id} not readable")
    if current.get("status") == status:
        return current
    return client.patch_task(task_id, {"status": status}, expected_revision=current.get("revision"))

