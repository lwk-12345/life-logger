"""OpenClaw Task Bridge integration for Life Logger.

Bridges Life Logger todo events, candidates, and daily review with OpenClaw's
canonical Task Service and unified-task-v1 schema on port 8791.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_OPENCLAW_BRIDGE_URL = "http://127.0.0.1:8791"
DEFAULT_OPENCLAW_WORKSPACE = Path.home() / ".openclaw" / "workspace"
TOKEN_STATE_FILE = DEFAULT_OPENCLAW_WORKSPACE / ".state" / "task-bridge-tokens.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def build_evening_alignment_payload(
    date_str: str,
    confirmed_events: list[dict[str, Any]],
    candidate_events: list[dict[str, Any]],
    active_intents: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build structured evening-bill-alignment-v1 schema payload."""
    confirmed_total = sum(
        float(e.get("data", {}).get("expense", {}).get("amount") or 0.0)
        for e in confirmed_events
        if isinstance(e, dict)
    )
    unresolved_count = len(
        [
            i
            for i in active_intents
            if isinstance(i, dict) and i.get("status") == "pending"
        ]
    )
    return {
        "schema_version": "evening-bill-alignment-v1",
        "date": date_str,
        "summary": {
            "confirmed_expense_count": len(confirmed_events),
            "total_confirmed_amount_cny": round(confirmed_total, 2),
            "candidate_expense_count": len(candidate_events),
            "unresolved_intent_count": unresolved_count,
        },
        "confirmed_events": list(confirmed_events),
        "candidate_events": list(candidate_events),
        "active_intents": list(active_intents),
    }


def resolve_openclaw_token(explicit_token: str | None = None) -> str | None:
    """Find a valid OpenClaw Task Bridge Bearer token.

    Order of precedence:
    1. Explicit argument
    2. Environment variable OPENCLAW_TASK_BRIDGE_TOKEN
    3. Stored token in local-config.json
    4. ~/.openclaw/workspace/.state/task-bridge-tokens.json (if accessible)
    """
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()

    env_token = os.environ.get("OPENCLAW_TASK_BRIDGE_TOKEN", "").strip()
    if env_token:
        return env_token

    for candidate in (
        Path(os.environ.get("LIFE_LOGGER_LOCAL_CONFIG", "")),
        PROJECT_ROOT / "local-config.json",
        Path("local-config.json"),
    ):
        if candidate.is_file():
            try:
                cfg = json.loads(candidate.read_text(encoding="utf-8"))
                token = cfg.get("openclaw", {}).get("token", "").strip()
                if token:
                    return token
            except Exception:
                pass

    # Look for saved plain tokens or check token file
    plain_token_file = DEFAULT_OPENCLAW_WORKSPACE / ".state" / "task-bridge-token.plain"
    if plain_token_file.is_file():
        try:
            val = plain_token_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass

    return None


class OpenClawBridgeClient:
    """Client for OpenClaw Task Bridge running on 127.0.0.1:8791."""

    def __init__(
        self,
        base_url: str = DEFAULT_OPENCLAW_BRIDGE_URL,
        token: str | None = None,
        bearer_token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_OPENCLAW_BRIDGE_URL).rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError(
                "Localhost daemon: OpenClaw Task Bridge endpoint must use localhost/127.0.0.1"
            )
        resolved = bearer_token if bearer_token is not None else token
        self.token = resolve_openclaw_token(resolved)
        self.timeout = float(timeout)

    def dispatch_evening_alignment(
        self,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch evening bill alignment payload to OpenClaw daemon for WeChat forwarding."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/reconciliation/evening-alignment",
            data=data,
            headers=self._headers(idempotency_key=idempotency_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                res = json.loads(body) if body else {}
                if isinstance(res, dict):
                    if "card_id" not in res:
                        res["card_id"] = f"card_{uuid.uuid4().hex[:12]}"
                    if "status" not in res:
                        res["status"] = "success"
                    return res
                return {"status": "success", "card_id": f"card_{uuid.uuid4().hex[:12]}"}
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8")
            try:
                err_data = json.loads(body)
            except Exception:
                err_data = {"error": body}
            raise RuntimeError(
                f"OpenClaw evening alignment error ({err.code}): {err_data}"
            ) from err
        except Exception as error:
            logger.error("Failed to dispatch evening alignment to OpenClaw: %s", error)
            raise

    def _headers(self, idempotency_key: str | None = None, expected_revision: int | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        if expected_revision is not None:
            headers["If-Match"] = str(expected_revision)
        return headers

    def is_healthy(self) -> bool:
        """Probe whether OpenClaw Task Bridge is alive and authorized."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/v1/tasks?status=pending",
                headers=self._headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_tasks(
        self,
        status: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks conforming to unified-task-v1."""
        query_params: dict[str, str] = {}
        if status:
            query_params["status"] = status
        if project_id:
            query_params["project_id"] = project_id

        url = f"{self.base_url}/v1/tasks"
        if query_params:
            url += "?" + urllib.parse.urlencode(query_params)

        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("tasks", [])
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            logger.warning("OpenClaw list_tasks HTTP %d: %s", error.code, detail[:200])
            raise RuntimeError(f"OpenClaw HTTP {error.code}: {detail[:200]}") from error
        except Exception as error:
            logger.warning("OpenClaw list_tasks error: %s", error)
            raise RuntimeError(f"cannot reach OpenClaw bridge at {self.base_url}: {error}") from error

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Fetch a single task by ID."""
        req = urllib.request.Request(
            f"{self.base_url}/v1/tasks/{urllib.parse.quote(task_id)}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(f"OpenClaw HTTP {error.code}: {detail[:200]}") from error
        except Exception as error:
            raise RuntimeError(f"cannot reach OpenClaw bridge: {error}") from error

    def create_task(
        self,
        title: str,
        priority: str = "P2",
        project_id: str | None = None,
        tags: list[str] | None = None,
        source: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a new unified task with idempotency protection."""
        payload: dict[str, Any] = {
            "title": str(title).strip(),
            "priority": priority if priority in ("P1", "P2", "P3") else "P2",
            "project_id": project_id,
            "tags": list(tags or []),
            "source": source or {"system": "life_logger"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/tasks",
            data=body,
            headers=self._headers(idempotency_key=idempotency_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(f"OpenClaw create_task HTTP {error.code}: {detail[:200]}") from error
        except Exception as error:
            raise RuntimeError(f"cannot reach OpenClaw bridge: {error}") from error

    def patch_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Patch a task's status, priority, or title with revision protection."""
        body = json.dumps(updates, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/tasks/{urllib.parse.quote(task_id)}",
            data=body,
            headers=self._headers(expected_revision=expected_revision),
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise RuntimeError(f"OpenClaw patch_task HTTP {error.code}: {detail[:200]}") from error
        except Exception as error:
            raise RuntimeError(f"cannot reach OpenClaw bridge: {error}") from error

    def create_task_from_life_event(
        self,
        event: dict[str, Any] | Any,
        priority: str = "P2",
        project_id: str = "life-logger",
    ) -> dict[str, Any]:
        """Convert a Life Logger todo event/candidate into an OpenClaw canonical task.

        Sets Idempotency-Key to `life-logger:event:<event_id>` to ensure exact-once
        task creation across re-runs.
        """
        data = event if isinstance(event, dict) else (getattr(event, "to_dict", None)() if callable(getattr(event, "to_dict", None)) else {})
        event_id = str(data.get("event_id") or getattr(event, "event_id", "") or "")
        title = str(data.get("title") or getattr(event, "title", "") or "待办事项")
        evidence = data.get("evidence") or {}

        source = {
            "system": "life_logger",
            "event_id": event_id,
            "recording_id": evidence.get("recording_id"),
            "segment_id": evidence.get("segment_id"),
            "quote": evidence.get("quote"),
        }

        idempotency_key = f"life-logger:event:{event_id}" if event_id else None

        tags = ["life-logger"]
        categories = data.get("categories") or []
        for cat in categories:
            if cat and cat not in tags:
                tags.append(str(cat))

        return self.create_task(
            title=title,
            priority=priority,
            project_id=project_id,
            tags=tags,
            source=source,
            idempotency_key=idempotency_key,
        )

    def sync_todo_events(
        self,
        events: list[dict[str, Any] | Any],
        job_id: str | None = None,
        artifact_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Scan a list of LifeEvents, extract todo candidates, and sync to OpenClaw.

        Returns list of sync outcomes with external_task_id.
        """
        results: list[dict[str, Any]] = []
        for item in events:
            ev_dict = item if isinstance(item, dict) else (item.to_dict() if hasattr(item, "to_dict") else {})
            categories = ev_dict.get("categories") or []
            data_field = ev_dict.get("data") or {}
            openclaw_field = data_field.get("openclaw") or {}
            candidate_type = openclaw_field.get("candidate_type")

            # 【2026-10-15】已被判定为历史垃圾并清理的候选，不再重复推送。
            # 只标记不删除，保持产物可追溯（用户要求「数据可追溯」）。
            if openclaw_field.get("ignored") is True:
                continue

            # Check if this is a todo event or candidate
            if "todo" in categories or candidate_type == "todo_candidate":
                try:
                    res = self.create_task_from_life_event(ev_dict)
                    task = res.get("task", {})
                    task_id = task.get("id")
                    results.append({
                        "event_id": ev_dict.get("event_id"),
                        "external_task_id": task_id,
                        "created": res.get("created", False),
                        "status": task.get("status"),
                        "title": task.get("title"),
                    })
                except Exception as error:
                    logger.warning("Failed to sync event %s to OpenClaw: %s", ev_dict.get("event_id"), error)
                    results.append({
                        "event_id": ev_dict.get("event_id"),
                        "error": str(error),
                    })
        return results

    def get_unified_dashboard_todos(self) -> dict[str, Any]:
        """Fetch all open tasks from OpenClaw and categorize for daily review & briefing.

        Groups tasks into:
        - wechat_matters: WeChat tasks ([id:wx-...] or source.system == 'wechat')
        - life_logger_todos: Audio-extracted tasks (source.system == 'life_logger')
        - general_todos: Other OpenClaw todos
        """
        all_tasks = self.list_tasks()
        open_tasks = [t for t in all_tasks if t.get("status") in ("pending", "in_progress", "blocked")]

        wechat_matters: list[dict[str, Any]] = []
        life_logger_todos: list[dict[str, Any]] = []
        general_todos: list[dict[str, Any]] = []

        for task in open_tasks:
            src = task.get("source") or {}
            tags = task.get("tags") or []
            sys_name = src.get("system", "")

            if sys_name == "wechat" or "微信" in tags:
                wechat_matters.append(task)
            elif sys_name == "life_logger" or "life-logger" in tags:
                life_logger_todos.append(task)
            else:
                general_todos.append(task)

        return {
            "total_open": len(open_tasks),
            "wechat_matters": wechat_matters,
            "life_logger_todos": life_logger_todos,
            "general_todos": general_todos,
            "raw_count": len(all_tasks),
        }
