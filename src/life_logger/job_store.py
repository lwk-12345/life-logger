"""JobStore 与任务状态映射（S2 从 local_web.py 原样搬出）。

依赖通过模块引用（`_support`/`_watch`/`_auth`）取用，保证门面 patch 生效。
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

from life_logger.timeutil import local_date as business_date
from life_logger.web_common import PROJECT_ROOT
from life_logger.web_common import logger
from life_logger.web_job_context import _JobContextMixin
from life_logger.web_job_ingest import _JobIngestMixin
from life_logger.web_job_reads import _JobReadsMixin
from life_logger.web_job_runner import _JobRunnerMixin
from life_logger.web_job_writes import _JobWritesMixin

from . import web_support as _support

DEFAULT_JOB_ROOT = PROJECT_ROOT / "output" / "local-web"

TASK_TO_REMINDER_STATUS: dict[str, str] = {
    "pending": "unscheduled",
    "in_progress": "scheduled",
    "blocked": "scheduled",
    "completed": "completed",
    "dismissed": "dismissed",
}

REMINDER_TO_TASK_STATUS: dict[str, str] = {
    "unscheduled": "pending",
    "scheduled": "in_progress",
    "completed": "completed",
    "dismissed": "dismissed",
    # 提醒本出现的历史值直通（这几个任务本都接受，不会二次翻译失败）。
    # 注意：不要在这里放 "blocked" —— 任务本 save_task_state 不接受该值，
    # 加了等于制造一条永远走不通的映射（本文件测试会直接抓到）。
    "pending": "pending",
    "in_progress": "in_progress",
}

_JOB_UTTERANCE_ITEMS_CACHE: dict[str, tuple[str, list[dict[str, Any]]]] = {}

def to_reminder_status(status: str) -> str:
    """任务本状态 -> 提醒本状态。未知值原样返回（交给下游报错，不静默吞）。"""
    return TASK_TO_REMINDER_STATUS.get(str(status or "").strip(), str(status or "").strip())

def apply_task_status(app: Any, payload: Mapping[str, Any], *, sync_openclaw: Any) -> dict[str, Any]:
    """[2026-11-06 F4] /api/tasks/status 的核心逻辑，抽出来便于测试。

    双存储：先写任务本；提醒本失败时**不吞**，在返回体里标 reminders_synced=False。
    """
    task_key = str(payload.get("task_key") or "")
    status = str(payload.get("status") or "")
    result = app.store.save_task_state(
        task_key=task_key,
        status=status,
        title=payload.get("title"),
        event_id=payload.get("event_id"),
        job_id=payload.get("job_id"),
        artifact_path=payload.get("artifact_path"),
    )
    reminders_synced = True
    try:
        app.reminders.set_status(task_key, to_reminder_status(status))
    except Exception as error:  # noqa: BLE001
        reminders_synced = False
        sys.stderr.write(
            f"[life-logger] reminders 同步失败 task_key={task_key}: {type(error).__name__} {error}\n"
        )
        sys.stderr.flush()
    openclaw_sync = sync_openclaw(
        task_key=task_key,
        status=status,
        title=payload.get("title") or result.get("title"),
        event_id=payload.get("event_id") or result.get("event_id"),
        artifact_path=payload.get("artifact_path"),
    )
    out = {"task": result, "openclaw": openclaw_sync, "reminders_synced": reminders_synced}
    if not reminders_synced:
        out["warning"] = "本地已写入，提醒本未同步；可稍后重试或忽略（不影响看板）"
    return out

def to_task_status(status: str) -> str:
    """提醒本状态 -> 任务本状态。未知值原样返回。"""
    return REMINDER_TO_TASK_STATUS.get(str(status or "").strip(), str(status or "").strip())

def _job_utterance_items(
    job_dir: Path, job_id: str, state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """把一个 job 的全部转写行拍平成"可比对的发言项"，按 job 的 updated_at 缓存。"""
    signature = str(state.get("updated_at") or "") + f"|{len(state.get('files') or [])}"
    cached = _JOB_UTTERANCE_ITEMS_CACHE.get(job_id)
    if cached is not None and cached[0] == signature:
        return cached[1]

    files_by_id = {
        str(item.get("file_id") or item.get("sha256") or ""): item
        for item in state.get("files", [])
        if isinstance(item, dict)
    }

    def source_for(file_state: Mapping[str, Any]) -> str:
        source = file_state.get("capture_source")
        return source if source in {"apple-watch", "ai-passport-wifi"} else "file-upload"

    transcript_paths = sorted(job_dir.glob("files/*/asr/*/transcript.json"))
    if not transcript_paths:
        transcript_paths = sorted(job_dir.glob("files/*/result/*/transcript.json"))
    items: list[dict[str, Any]] = []
    for path in transcript_paths:
        payload = _support._cached_transcript_payload(path)
        if not isinstance(payload, list):
            continue
        relative_parts = path.relative_to(job_dir).parts
        file_id = relative_parts[1] if len(relative_parts) > 1 else ""
        file_state = files_by_id.get(file_id, {})
        artifact_path = str(path.relative_to(job_dir))
        source_file = str(
            file_state.get("relative_path") or file_state.get("original_name") or ""
        )
        import_source = source_for(file_state)
        for item in payload:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            timestamp = str(item.get("absolute_started_at") or "")
            items.append(
                {
                    "result_type": "utterance",
                    "job_id": job_id,
                    "artifact_path": artifact_path,
                    "date": business_date(timestamp) if timestamp else None,
                    "timestamp": timestamp or None,
                    "text": text,
                    "source_file": source_file,
                    "import_source": import_source,
                    "recording_id": item.get("recording_id"),
                    "segment_id": item.get("segment_id"),
                    "start_offset_seconds": item.get("start_offset_seconds"),
                    "end_offset_seconds": item.get("end_offset_seconds"),
                    "source_label": "full_transcript",
                    "review_status": "archived",
                }
            )
    if len(_JOB_UTTERANCE_ITEMS_CACHE) >= 512:
        _JOB_UTTERANCE_ITEMS_CACHE.clear()
    _JOB_UTTERANCE_ITEMS_CACHE[job_id] = (signature, items)
    return items

class JobStore(_JobWritesMixin, _JobRunnerMixin, _JobReadsMixin, _JobIngestMixin, _JobContextMixin):
    """JSON-backed job state with content-addressed retained inputs."""

    def __init__(self, root: Path | None = None, *, recover: bool = True,
                 dispatch_queue=None, process_safe: bool = False):
        # 运行时读取模块属性：local_web 门面 patch DEFAULT_JOB_ROOT 后 JobStore() 也要跟着变。
        self.root = Path(root if root is not None else DEFAULT_JOB_ROOT).resolve()
        self.jobs_dir = self.root / "jobs"
        self.content_dir = self.root / "content"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        # [2026-10-29 batch3] 启动时清理超龄孤立临时文件（SIGKILL 残留）。
        # 只删 30 分钟前的 *.tmp / *.partial（绝不删 .bak），任何异常都不影响启动。
        try:
            from life_logger.atomic import cleanup_stale_tmp

            cleanup_targets = [self.root / "cache"]
            cleanup_targets.extend(p for p in self.jobs_dir.glob("*/uploads") if p.is_dir())
            cleanup_stale_tmp(cleanup_targets)
        except Exception as cleanup_exc:  # noqa: BLE001 - 清理失败不影响启动
            logger.warning("孤立临时文件清理跳过: %s: %s", type(cleanup_exc).__name__, cleanup_exc)
        self.dispatch_queue = dispatch_queue
        if process_safe or dispatch_queue is not None:
            from life_logger.runtime_queue import ProcessRLock
            self._lock = ProcessRLock(self.root / ".job-store.lock")
        else:
            self._lock = threading.RLock()
        self._active: dict[str, threading.Thread] = {}
        self._context_recovery_active: dict[str, threading.Thread] = {}
        self._context_recovery_gate = threading.BoundedSemaphore(1)
        # Each running job loads the 2.0 GB Fun-ASR checkpoint into ~4.6 GB of resident
        # memory (measured on this machine). The number of gates is therefore bounded by
        # memory, not by cores: an unbounded burst swaps the host to death and then every
        # job in the burst is marked "interrupted" when the server is restarted.
        # 2 gates = ~9.2 GB of the 16 GB here, leaving the rest to the system.
        self._job_concurrency = 2
        self._job_gates = threading.BoundedSemaphore(self._job_concurrency)
        self._startup_recovery_ids: list[str] = []
        if recover and dispatch_queue is None:
            self._startup_recovery_ids = self.recover_interrupted()


    def data_health(self) -> dict[str, Any]:
        """Inspect durable local state without mutating user data."""

        with self._lock:
            issues: list[dict[str, Any]] = []
            checked_files = 0

            def read_json(path: Path, scope: str, job_id: str | None = None) -> Any:
                nonlocal checked_files
                if not path.exists():
                    return None
                checked_files += 1
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    issues.append(
                        {
                            "kind": "malformed_json",
                            "file": str(path.relative_to(self.root)),
                            "scope": scope,
                            "job_id": job_id,
                            "detail": str(error),
                        }
                    )
                    return None

            for path in (
                self.root / "preferences.json",
                self.root / "personal-training-samples.json",
                self.root / "personal-model.json",
                self.task_states_path(),
            ):
                read_json(path, "root_state")

            auto_ingest = read_json(self.auto_ingest_path(), "auto_ingest")
            auto_ingest_summary = {
                "configured": self.auto_ingest_path().is_file(),
                "enabled": False,
                "folder_count": 0,
                "missing_folder_count": 0,
                "pending_count": 0,
                "processed_count": 0,
                "history_count": 0,
            }
            if isinstance(auto_ingest, dict):
                folders = auto_ingest.get("folders", [])
                candidates = auto_ingest.get("candidates", {})
                processed = auto_ingest.get("processed", {})
                history = auto_ingest.get("history", [])
                if not isinstance(folders, list):
                    issues.append(
                        {
                            "kind": "auto_ingest_invalid_state",
                            "file": "auto-ingest.json",
                            "detail": "folders 不是列表",
                        }
                    )
                    folders = []
                if not isinstance(candidates, dict):
                    issues.append(
                        {
                            "kind": "auto_ingest_invalid_state",
                            "file": "auto-ingest.json",
                            "detail": "candidates 不是对象",
                        }
                    )
                    candidates = {}
                if not isinstance(processed, dict):
                    issues.append(
                        {
                            "kind": "auto_ingest_invalid_state",
                            "file": "auto-ingest.json",
                            "detail": "processed 不是对象",
                        }
                    )
                    processed = {}
                if not isinstance(history, list):
                    issues.append(
                        {
                            "kind": "auto_ingest_invalid_state",
                            "file": "auto-ingest.json",
                            "detail": "history 不是列表",
                        }
                    )
                    history = []

                missing_folders = 0
                for raw_folder in folders:
                    folder = Path(str(raw_folder)).expanduser()
                    if not folder.is_dir():
                        missing_folders += 1
                        issues.append(
                            {
                                "kind": "auto_ingest_missing_folder",
                                "file": "auto-ingest.json",
                                "path": str(folder),
                            }
                        )

                stability_seconds = int(auto_ingest.get("stability_seconds") or 20)
                stale_after = max(3600, stability_seconds * 20)
                now = _support._now()
                for source_path, candidate in candidates.items():
                    if not isinstance(candidate, dict):
                        continue
                    try:
                        stable_since = float(candidate.get("stable_since"))
                    except (TypeError, ValueError):
                        continue
                    if now - stable_since > stale_after:
                        issues.append(
                            {
                                "kind": "auto_ingest_stale_candidate",
                                "file": "auto-ingest.json",
                                "path": str(source_path),
                                "age_seconds": int(now - stable_since),
                            }
                        )

                for digest, item in processed.items():
                    if not isinstance(item, dict):
                        continue
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        continue
                    valid_name = (
                        Path(job_id).name == job_id and job_id.startswith("job-")
                    )
                    if not valid_name or not (self.jobs_dir / job_id).is_dir():
                        issues.append(
                            {
                                "kind": "auto_ingest_orphan_job",
                                "file": "auto-ingest.json",
                                "sha256": str(digest),
                                "job_id": job_id,
                            }
                        )

                auto_ingest_summary = {
                    "configured": True,
                    "enabled": bool(auto_ingest.get("enabled")),
                    "folder_count": len(folders),
                    "missing_folder_count": missing_folders,
                    "pending_count": len(candidates),
                    "processed_count": len(processed),
                    "history_count": len(history),
                }

            for job_dir in sorted(self.jobs_dir.glob("job-*")):
                if not job_dir.is_dir():
                    continue
                job_id = job_dir.name
                read_json(self.status_path(job_id), "job_status", job_id)
                reviews = read_json(self.review_path(job_id), "reviews", job_id)
                manual = read_json(
                    self.manual_events_path(job_id), "manual_events", job_id
                )
                derived = read_json(
                    self.review_derived_events_path(job_id),
                    "review_derived_events",
                    job_id,
                )
                corrections = read_json(
                    self.event_corrections_path(job_id),
                    "event_corrections",
                    job_id,
                )

                artifact_cache: dict[str, list[dict[str, Any]]] = {}

                def artifact_records(relative: str) -> list[dict[str, Any]]:
                    if relative in artifact_cache:
                        return artifact_cache[relative]
                    target = (job_dir / relative).resolve()
                    job_root = job_dir.resolve()
                    if target != job_root and job_root not in target.parents:
                        issues.append(
                            {
                                "kind": "invalid_reference",
                                "job_id": job_id,
                                "artifact_path": relative,
                                "detail": "路径越界",
                            }
                        )
                        artifact_cache[relative] = []
                        return []
                    if not target.is_file():
                        issues.append(
                            {
                                "kind": "missing_artifact",
                                "job_id": job_id,
                                "artifact_path": relative,
                            }
                        )
                        artifact_cache[relative] = []
                        return []
                    payload = read_json(target, "referenced_artifact", job_id)
                    if isinstance(payload, list):
                        records = [item for item in payload if isinstance(item, dict)]
                    elif isinstance(payload, dict):
                        records = [
                            item
                            for item in payload.get("events", [])
                            if isinstance(item, dict)
                        ]
                    else:
                        records = []
                    artifact_cache[relative] = records
                    return records

                manual_items: list[dict[str, Any]] = []
                for payload in (manual, derived):
                    if not isinstance(payload, dict):
                        continue
                    manual_items.extend(
                        item
                        for item in payload.get("events", [])
                        if isinstance(item, dict)
                    )
                manual_ids_by_artifact: dict[str, set[str]] = {}
                for item in manual_items:
                    relative = str(item.get("artifact_path") or "")
                    if not relative:
                        continue
                    manual_ids_by_artifact.setdefault(relative, set()).add(
                        str(item.get("event_id") or "")
                    )
                    if not (job_dir / relative).is_file():
                        issues.append(
                            {
                                "kind": "missing_artifact",
                                "job_id": job_id,
                                "artifact_path": relative,
                                "record_type": "manual_event",
                            }
                        )

                correction_map = (
                    corrections.get("corrections", {})
                    if isinstance(corrections, dict)
                    else {}
                )
                if isinstance(correction_map, dict):
                    for correction in correction_map.values():
                        if not isinstance(correction, dict):
                            continue
                        relative = str(correction.get("artifact_path") or "")
                        event_id = str(correction.get("event_id") or "")
                        if not relative or not event_id:
                            issues.append(
                                {
                                    "kind": "orphan_overlay",
                                    "job_id": job_id,
                                    "record_type": "event_correction",
                                    "detail": "缺少 artifact_path 或 event_id",
                                }
                            )
                            continue
                        records = artifact_records(relative)
                        generated_ids = {
                            str(item.get("event_id") or "")
                            for item in records
                            if item.get("event_id")
                        }
                        known = event_id in generated_ids
                        known = known or event_id in manual_ids_by_artifact.get(
                            relative, set()
                        )
                        if not known:
                            issues.append(
                                {
                                    "kind": "orphan_overlay",
                                    "job_id": job_id,
                                    "record_type": "event_correction",
                                    "artifact_path": relative,
                                    "event_id": event_id,
                                }
                            )

                decision_map = (
                    reviews.get("decisions", {}) if isinstance(reviews, dict) else {}
                )
                if isinstance(decision_map, dict):
                    for decision in decision_map.values():
                        if not isinstance(decision, dict):
                            continue
                        relative = str(decision.get("artifact_path") or "")
                        segment_id = str(decision.get("segment_id") or "")
                        if not relative or not segment_id:
                            issues.append(
                                {
                                    "kind": "orphan_overlay",
                                    "job_id": job_id,
                                    "record_type": "review_decision",
                                    "detail": "缺少 artifact_path 或 segment_id",
                                }
                            )
                            continue
                        records = artifact_records(relative)
                        segment_ids = {
                            str(item.get("segment_id") or "")
                            for item in records
                            if item.get("segment_id")
                        }
                        if segment_id not in segment_ids:
                            issues.append(
                                {
                                    "kind": "orphan_overlay",
                                    "job_id": job_id,
                                    "record_type": "review_decision",
                                    "artifact_path": relative,
                                    "segment_id": segment_id,
                                }
                            )

            from life_logger.data_integrity import operational_issues

            issues.extend(operational_issues(self.root))
            for issue in issues:
                issue.setdefault("severity", "error" if issue.get("kind") in {
                    "malformed_json", "invalid_reference"
                } else "warning")
            error_count = sum(issue["severity"] == "error" for issue in issues)
            return {
                "schema_version": "0.2",
                "checked_at": _support._iso(),
                "status": "error" if error_count else ("warning" if issues else "ok"),
                "error_count": error_count,
                "warning_count": len(issues) - error_count,
                "checked_files": checked_files,
                "issue_count": len(issues),
                "issues": issues[:200],
                "auto_ingest": auto_ingest_summary,
                "notes": [],
            }

