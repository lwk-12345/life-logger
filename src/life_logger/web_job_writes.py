"""JobStore 的方法，从 local_web.py 机械搬出，方法体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

from life_logger.domain import ALLOWED_CATEGORIES
from life_logger.timeutil import LOCAL_TZ
from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WATCH_CAPTURE_SOURCE
from life_logger.web_common import PROJECT_ROOT
from life_logger.web_common import SUPPORTED_ASR_MODELS
from life_logger.web_common import SUPPORTED_SPEAKER_MODELS
from life_logger.web_common import _atomic_json
from life_logger.web_common import logger
from life_logger.web_common import stage_severity
from life_logger.web_preferences import _read_preferences
from pathlib import Path
from typing import Any
import io
import json
import os
import sys
import uuid
import zipfile


class _JobWritesMixin:
    def save_context_relation_review(
        self,
        job_id: str,
        *,
        artifact_path: str,
        candidate_id: str,
        decision: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _iso,
        )

        candidate_id = str(candidate_id or "").strip()
        if not candidate_id or len(candidate_id) > 200:
            raise ValueError("关系候选编号无效")
        if not isinstance(decision, str) or decision not in {"accepted", "rejected"}:
            raise ValueError("关系复核结果必须是 accepted 或 rejected")
        if note is not None:
            if not isinstance(note, str):
                raise ValueError("关系复核备注必须是文本")
            note = note.strip()
            if len(note) > 2000:
                raise ValueError("关系复核备注过长")

        with self._lock:
            artifact_path, candidates = self._load_relation_candidates_unlocked(
                job_id, artifact_path
            )
            if candidate_id not in candidates:
                raise ValueError("关系候选不存在")
            path = self.context_relation_reviews_path(job_id)
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("关系复核记录格式无效") from error
            else:
                payload = {"schema_version": "0.1", "reviews": {}}
            if not isinstance(payload, dict):
                raise ValueError("关系复核记录格式无效")
            reviews = payload.setdefault("reviews", {})
            if not isinstance(reviews, dict):
                raise ValueError("关系复核记录格式无效")
            key = f"{artifact_path}::{candidate_id}"
            current = reviews.get(key, {})
            if not isinstance(current, dict):
                current = {}
            reviewed_at = _iso()
            record: dict[str, Any] = {
                "job_id": job_id,
                "artifact_path": artifact_path,
                "candidate_id": candidate_id,
                "decision": decision,
                "reviewed_at": reviewed_at,
                "history": [
                    *(
                        current.get("history", [])
                        if isinstance(current.get("history"), list)
                        else []
                    ),
                    {
                        "decision": decision,
                        "reviewed_at": reviewed_at,
                        **({"note": note} if note else {}),
                    },
                ],
            }
            if note:
                record["note"] = note
            reviews[key] = record
            _atomic_json(path, payload)
            return dict(record)

    def configure_auto_ingest(
        self,
        *,
        enabled: bool,
        folders: list[str],
        stability_seconds: int | None = None,
        scan_interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _iso,
        )

        normalized_folders: list[str] = []
        for raw in folders:
            value = str(raw or "").strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("自动导入目录必须使用绝对路径")
            resolved = path.resolve()
            blocked_roots = (self.root, PROJECT_ROOT)
            if any(
                resolved == blocked or blocked in resolved.parents
                for blocked in blocked_roots
            ):
                raise ValueError("不能监控 Life Logger 自己的项目或输出目录")
            text = str(resolved)
            if text not in normalized_folders:
                normalized_folders.append(text)
        if enabled and not normalized_folders:
            raise ValueError("开启自动导入前至少配置一个本机目录")
        stability = 20 if stability_seconds is None else int(stability_seconds)
        interval = 15 if scan_interval_seconds is None else int(scan_interval_seconds)
        if not 5 <= stability <= 3600:
            raise ValueError("文件稳定等待时间必须在 5 到 3600 秒之间")
        if not 5 <= interval <= 3600:
            raise ValueError("自动扫描间隔必须在 5 到 3600 秒之间")
        with self._lock:
            state = self._read_auto_ingest_unlocked()
            state["enabled"] = bool(enabled)
            state["folders"] = normalized_folders
            state["stability_seconds"] = stability
            state["scan_interval_seconds"] = interval
            valid_prefixes = tuple(f"{folder}{os.sep}" for folder in normalized_folders)
            state["candidates"] = {
                path: item
                for path, item in state.get("candidates", {}).items()
                if path in normalized_folders
                or any(path.startswith(prefix) for prefix in valid_prefixes)
            }
            self._append_auto_ingest_history_unlocked(
                state,
                {
                    "kind": "configuration",
                    "at": _iso(),
                    "enabled": bool(enabled),
                    "folders": normalized_folders,
                },
            )
            _atomic_json(self.auto_ingest_path(), state)
        return self.auto_ingest_status()

    def choose_auto_ingest_folder(self) -> str | None:
        from life_logger.local_web import (
            subprocess,
        )

        if sys.platform != "darwin":
            raise ValueError("当前系统暂不支持原生文件夹选择，请手动填写绝对路径")
        script = (
            'POSIX path of (choose folder with prompt '
            '"选择 Life Logger 自动导入目录")'
        )
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("无法打开 Finder 文件夹选择器") from error
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            if "-128" in detail or "User canceled" in detail:
                return None
            raise ValueError(f"文件夹选择失败：{detail or completed.returncode}")
        selected = completed.stdout.strip()
        if not selected:
            return None
        path = Path(selected).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("选择的目录不存在")
        return str(path)

    def build_state_backup(self) -> bytes:
        """Export durable user state without raw audio or local secrets."""
        from life_logger.local_web import (
            SERVER_BUILD_ID,
            _iso,
        )


        root_files = (
            "preferences.json",
            "personal-training-samples.json",
            "personal-model.json",
            "task-states.json",
            "reminders.json",
            "habits.json",
            "auto-ingest.json",
        )
        job_files = (
            "status.json",
            "review-decisions.json",
            "manual-events.json",
            "review-derived-events.json",
            "event-corrections.json",
        )
        with self._lock:
            buffer = io.BytesIO()
            included: list[str] = []
            with zipfile.ZipFile(
                buffer, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name in root_files:
                    path = self.root / name
                    if not path.is_file():
                        continue
                    archive.write(path, arcname=name)
                    included.append(name)
                for job_dir in sorted(self.jobs_dir.glob("job-*")):
                    if not job_dir.is_dir():
                        continue
                    for name in job_files:
                        path = job_dir / name
                        if not path.is_file():
                            continue
                        arcname = f"jobs/{job_dir.name}/{name}"
                        archive.write(path, arcname=arcname)
                        included.append(arcname)
                manifest = {
                    "schema_version": "0.1",
                    "created_at": _iso(),
                    "server_build_id": SERVER_BUILD_ID,
                    "included_files": included,
                    "excluded": [
                        "secrets.json",
                        "raw audio/content",
                        "ASR/intermediate artifacts",
                    ],
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            return buffer.getvalue()

    def save_task_state(
        self,
        *,
        task_key: str,
        status: str,
        title: str | None = None,
        event_id: str | None = None,
        job_id: str | None = None,
        artifact_path: str | None = None,
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _iso,
        )

        task_key = str(task_key or "").strip()
        status = str(status or "").strip()
        if not task_key or len(task_key) > 2048:
            raise ValueError("任务标识无效")
        if status not in {"pending", "in_progress", "completed", "dismissed"}:
            raise ValueError("任务状态无效")
        if job_id:
            # [2026-10-14] job_id 是「作业定位」用的可选元数据，不是必需字段。
            # 客户端可能把**显示用**标识塞进来（典型：Watch 实时会话事件用
            # `watch_<session_id>` 当 _jobId，它并不是可定位的作业编号）。
            # 此前对这种值直接 raise「任务编号无效」→ 前端行动候选整排按钮点下去 400，
            # 用户完全无法操作待办。现在：只对能定位的 `job-` 编号做路径校验，
            # 非作业编号降级为「无作业定位」保存（路径穿越防护不变，因为一律不拼路径）。
            candidate = str(job_id)
            if candidate.startswith("job-"):
                self.job_path(candidate)
            elif Path(candidate).name != candidate or ".." in Path(candidate).parts:
                # 保留路径穿越防护：含分隔符 / 上跳段的输入仍一律拒绝
                raise ValueError("任务编号无效")
            else:
                # 纯显示用标识（如 watch_<session_id>）：降级为「无作业定位」
                job_id = None

        with self._lock:
            path = self.task_states_path()
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = {"schema_version": "0.1", "tasks": {}}
            tasks = payload.setdefault("tasks", {})
            if not isinstance(tasks, dict):
                tasks = {}
                payload["tasks"] = tasks
            current = tasks.get(task_key, {})
            if not isinstance(current, dict):
                current = {}
            record = {
                **current,
                "task_key": task_key,
                "status": status,
                "title": str(title or current.get("title") or "")[:400] or None,
                "event_id": str(event_id or current.get("event_id") or "") or None,
                "job_id": str(job_id or current.get("job_id") or "") or None,
                "artifact_path": (
                    str(artifact_path or current.get("artifact_path") or "") or None
                ),
                "updated_at": _iso(),
            }
            if "created_at" not in record:
                record["created_at"] = record["updated_at"]
            tasks[task_key] = record
            _atomic_json(path, payload)
            return record

    def save_event_correction(
        self,
        job_id: str,
        *,
        artifact_path: str,
        event_id: str,
        changes: dict[str, Any] | None = None,
        rejected: bool | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _iso,
            datetime,
            invalidate_read_caches,
        )

        artifact_path = str(artifact_path or "").strip()
        event_id = str(event_id or "").strip()
        if not artifact_path or not event_id:
            raise ValueError("事件修正缺少文件路径或 event_id")
        changes = dict(changes or {})
        if note and "note" not in changes:
            changes["note"] = note
        allowed_change_keys = {
            "title",
            "categories",
            "activity_type",
            "time",
            "data",
            "note",
        }
        unknown_keys = set(changes).difference(allowed_change_keys)
        if unknown_keys:
            raise ValueError(f"事件修正包含无效字段：{sorted(unknown_keys)}")
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise ValueError("事件标题不能为空")
            changes["title"] = title
        if "categories" in changes:
            categories = [
                str(item).strip()
                for item in changes.get("categories", [])
                if str(item).strip()
            ]
            if not categories:
                raise ValueError("事件至少需要一个分类")
            invalid = set(categories).difference(ALLOWED_CATEGORIES)
            if invalid:
                raise ValueError(f"事件包含无效分类：{sorted(invalid)}")
            changes["categories"] = categories
        if "time" in changes:
            time_patch = changes["time"]
            if not isinstance(time_patch, dict):
                raise ValueError("事件时间修正格式无效")
            if time_patch.get("absolute"):
                try:
                    datetime.fromisoformat(str(time_patch["absolute"]))
                except ValueError as error:
                    raise ValueError("事件时间格式无效") from error
            if time_patch.get("relative_seconds") is not None:
                relative_seconds = float(time_patch["relative_seconds"])
                if relative_seconds < 0:
                    raise ValueError("事件相对时间无效")
                time_patch["relative_seconds"] = relative_seconds
            if (
                time_patch.get("precision") is not None
                and time_patch.get("precision")
                not in {"exact", "inferred", "relative", "unknown"}
            ):
                raise ValueError("事件时间精度无效")
        if "data" in changes and not isinstance(changes["data"], dict):
            raise ValueError("事件结构化数据格式无效")
        if "note" in changes:
            changes["note"] = str(changes["note"] or "")[:4000]

        with self._lock:
            job_dir = self.job_path(job_id).resolve()
            target = (job_dir / artifact_path).resolve()
            if target != job_dir and job_dir not in target.parents:
                raise ValueError("事件文件路径越界")
            if not target.is_file() or target.name != "events.json":
                raise ValueError("事件修正必须关联现有 events.json")
            payload = json.loads(target.read_text(encoding="utf-8"))
            generated_events = (
                payload
                if isinstance(payload, list)
                else payload.get("events", [])
                if isinstance(payload, dict)
                else []
            )
            manual_events = [
                *self._read_event_file_unlocked(self.manual_events_path(job_id)),
                *self._read_event_file_unlocked(
                    self.review_derived_events_path(job_id)
                ),
            ]
            known = any(
                isinstance(item, dict)
                and item.get("event_id") == event_id
                and (
                    item in generated_events
                    or item.get("artifact_path") == artifact_path
                )
                for item in [*generated_events, *manual_events]
            )
            if not known:
                raise ValueError("要修正的事件不存在")

            path = self.event_corrections_path(job_id)
            if path.exists():
                correction_payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                correction_payload = {
                    "schema_version": "0.1",
                    "corrections": {},
                }
            corrections = correction_payload.setdefault("corrections", {})
            if not isinstance(corrections, dict):
                corrections = {}
                correction_payload["corrections"] = corrections
            key = f"{artifact_path}::{event_id}"
            current = corrections.get(key, {})
            if not isinstance(current, dict):
                current = {}
            record = {
                **current,
                "job_id": job_id,
                "artifact_path": artifact_path,
                "event_id": event_id,
                "changes": {
                    **(
                        current.get("changes", {})
                        if isinstance(current.get("changes"), dict)
                        else {}
                    ),
                    **changes,
                },
                "rejected": (
                    bool(rejected)
                    if rejected is not None
                    else bool(current.get("rejected", False))
                ),
                "updated_at": _iso(),
            }
            if "created_at" not in record:
                record["created_at"] = record["updated_at"]
            corrections[key] = record
            _atomic_json(path, correction_payload)
            invalidate_read_caches(self.root, "save_event_correction")
            return record

    def save_manual_event(
        self,
        job_id: str,
        *,
        artifact_path: str,
        title: str,
        categories: list[str],
        absolute_time: str | None = None,
        relative_seconds: float | None = None,
        time_precision: str = "unknown",
        activity_type: str | None = None,
        data: dict[str, Any] | None = None,
        note: str | None = None,
        source_type: str = "manual",
        evidence: dict[str, Any] | None = None,
        capture_mode: str = "active",
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _iso,
            datetime,
            invalidate_read_caches,
        )

        title = title.strip()
        if not title:
            raise ValueError("人工事件缺少标题")
        if not categories:
            raise ValueError("人工事件至少需要一个分类")
        invalid = set(categories).difference(ALLOWED_CATEGORIES)
        if invalid:
            raise ValueError(f"人工事件包含无效分类：{sorted(invalid)}")
        if time_precision not in {"exact", "inferred", "relative", "unknown"}:
            raise ValueError("人工事件时间精度无效")
        if absolute_time:
            try:
                datetime.fromisoformat(absolute_time)
            except ValueError as error:
                raise ValueError("人工事件时间格式无效") from error
        if relative_seconds is not None:
            relative_seconds = float(relative_seconds)
            if relative_seconds < 0:
                raise ValueError("人工事件相对时间无效")

        with self._lock:
            job_dir = self.job_path(job_id).resolve()
            target = (job_dir / artifact_path).resolve()
            if target != job_dir and job_dir not in target.parents:
                raise ValueError("人工事件文件路径越界")
            if target.name != "events.json":
                raise ValueError("人工事件必须关联现有 events.json")
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_json(target, {"schema_version": "0.1", "events": []})

            path = self.manual_events_path(job_id)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = {"schema_version": "0.1", "events": []}
            evidence_record = {
                "source": (
                    "user_supplement"
                    if source_type == "user_supplement"
                    else "user_confirmation"
                ),
                "quote": note,
                "note": note or "用户人工确认",
            }
            if evidence and isinstance(evidence, dict):
                evidence_record.update(evidence)
            event = {
                "event_id": f"manual_evt_{uuid.uuid4().hex[:12]}",
                "artifact_path": artifact_path,
                "categories": categories,
                "activity_type": activity_type,
                "title": title,
                "time": {
                    "absolute": absolute_time,
                    "relative_seconds": relative_seconds,
                    "precision": time_precision,
                },
                "data": data or {},
                "capture_mode": (
                    capture_mode
                    if capture_mode in {"active", "active_hotword", "passive"}
                    else "active"
                ),
                "source_type": (
                    source_type
                    if source_type in {"manual", "user_supplement"}
                    else "manual"
                ),
                "confidence": 1.0,
                "evidence": evidence_record,
                "note": note,
                "review_status": "confirmed",
                "created_at": _iso(),
            }
            payload.setdefault("events", []).append(event)
            _atomic_json(path, payload)
            invalidate_read_caches(self.root, "save_manual_event")
            return event

    def save_review_decision(
        self,
        job_id: str,
        *,
        artifact_path: str,
        segment_id: str,
        source_decision: str | None = None,
        transcript_decision: str | None = None,
        corrected_text: str | None = None,
        interlocutor_relation: str | None = None,
        interlocutor_name: str | None = None,
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _apply_interlocutor_fields,
            _iso,
            invalidate_read_caches,
        )

        allowed_sources = {
            "self",
            "non_self",
            "other_person",
            "media",
            "non_speech",
            "unclear",
        }
        allowed_transcripts = {"correct", "incorrect", "unclear"}
        if source_decision is not None and source_decision not in allowed_sources:
            raise ValueError("无效的声源复核结果")
        if (
            transcript_decision is not None
            and transcript_decision not in allowed_transcripts
        ):
            raise ValueError("无效的转写复核结果")
        if (
            source_decision is None
            and transcript_decision is None
            and corrected_text is None
            and interlocutor_relation is None
            and interlocutor_name is None
        ):
            raise ValueError("至少需要提交一项复核结果")
        if not segment_id:
            raise ValueError("缺少 segment_id")

        with self._lock:
            job_dir = self.job_path(job_id).resolve()
            target = (job_dir / artifact_path).resolve()
            if target != job_dir and job_dir not in target.parents:
                raise ValueError("复核文件路径越界")
            if not target.is_file() or target.suffix.lower() != ".json":
                raise ValueError("复核文件不存在")
            source_records = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(source_records, list):
                raise ValueError("复核文件格式无效")
            if not any(item.get("segment_id") == segment_id for item in source_records):
                raise ValueError("复核片段不存在")

            path = self.review_path(job_id)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = {"schema_version": "0.1", "decisions": {}}
            decisions = payload.setdefault("decisions", {})
            key = f"{artifact_path}::{segment_id}"
            item = dict(decisions.get(key, {}))
            item.update(
                artifact_path=artifact_path,
                segment_id=segment_id,
                reviewed_at=_iso(),
                job_id=job_id,
            )
            if source_decision is not None:
                item["source_decision"] = source_decision
            if transcript_decision is not None:
                item["transcript_decision"] = transcript_decision
                if transcript_decision != "incorrect":
                    item.pop("corrected_text", None)
            if corrected_text is not None:
                corrected_text = corrected_text.strip()
                if not corrected_text:
                    item.pop("corrected_text", None)
                else:
                    if len(corrected_text) > 10000:
                        raise ValueError("修正后的转写文本过长")
                    item["corrected_text"] = corrected_text
            _apply_interlocutor_fields(item, interlocutor_relation, interlocutor_name)
            decisions[key] = item
            _atomic_json(path, payload)
            invalidate_read_caches(self.root, "save_review_decision")
            self._rebuild_review_derived_events_unlocked(job_id)
            try:
                from life_logger.flywheel import feed_batch_review_decisions

                feed_batch_review_decisions(
                    job_id,
                    artifact_path,
                    [item],
                    source_records,
                    root=self.root / "flywheel",
                )
            except Exception as e:
                logger.warning("Flywheel review decision ingestion error: %s", e)
            return item

    def save_review_decisions_batch(
        self,
        job_id: str,
        *,
        artifact_path: str,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        from life_logger.local_web import (
            _apply_interlocutor_fields,
            _iso,
            invalidate_read_caches,
        )

        allowed_sources = {
            "self",
            "non_self",
            "other_person",
            "media",
            "non_speech",
            "unclear",
        }
        allowed_transcripts = {"correct", "incorrect", "unclear"}
        if not items:
            return []

        with self._lock:
            job_dir = self.job_path(job_id).resolve()
            target = (job_dir / artifact_path).resolve()
            if target != job_dir and job_dir not in target.parents:
                raise ValueError("复核文件路径越界")
            if not target.is_file() or target.suffix.lower() != ".json":
                raise ValueError("复核文件不存在")
            source_records = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(source_records, list):
                raise ValueError("复核文件格式无效")
            existing_segment_ids = {
                item.get("segment_id")
                for item in source_records
                if isinstance(item, dict)
            }

            path = self.review_path(job_id)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload = {"schema_version": "0.1", "decisions": {}}
            decisions = payload.setdefault("decisions", {})
            results: list[dict[str, Any]] = []
            now_iso = _iso()

            for entry in items:
                segment_id = str(entry.get("segment_id") or "")
                if not segment_id or segment_id not in existing_segment_ids:
                    continue
                source_decision = entry.get("source_decision")
                transcript_decision = entry.get("transcript_decision")
                corrected_text = entry.get("corrected_text")
                interlocutor_relation = entry.get("interlocutor_relation")
                interlocutor_name = entry.get("interlocutor_name")
                if source_decision is not None and source_decision not in allowed_sources:
                    raise ValueError("无效的声源复核结果")
                if transcript_decision is not None and transcript_decision not in allowed_transcripts:
                    raise ValueError("无效的转写复核结果")
                if (
                    source_decision is None
                    and transcript_decision is None
                    and corrected_text is None
                    and interlocutor_relation is None
                    and interlocutor_name is None
                ):
                    continue

                key = f"{artifact_path}::{segment_id}"
                item = dict(decisions.get(key, {}))
                item.update(
                    artifact_path=artifact_path,
                    segment_id=segment_id,
                    reviewed_at=now_iso,
                    job_id=job_id,
                )
                if source_decision is not None:
                    item["source_decision"] = source_decision
                if transcript_decision is not None:
                    item["transcript_decision"] = transcript_decision
                    if transcript_decision != "incorrect":
                        item.pop("corrected_text", None)
                if corrected_text is not None:
                    corrected_text = corrected_text.strip()
                    if not corrected_text:
                        item.pop("corrected_text", None)
                    else:
                        if len(corrected_text) > 10000:
                            raise ValueError("修正后的转写文本过长")
                        item["corrected_text"] = corrected_text
                _apply_interlocutor_fields(item, interlocutor_relation, interlocutor_name)
                decisions[key] = item
                results.append(item)

            _atomic_json(path, payload)
            invalidate_read_caches(self.root, "save_review_decisions_batch")
            self._rebuild_review_derived_events_unlocked(job_id)
            try:
                from life_logger.flywheel import feed_batch_review_decisions

                feed_batch_review_decisions(
                    job_id,
                    artifact_path,
                    results,
                    source_records,
                    root=self.root / "flywheel",
                )
            except Exception as e:
                logger.warning("Flywheel batch review ingestion error: %s", e)
            return results

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        from life_logger.local_web import (
            _iso,
        )

        state["updated_at"] = _iso()
        _atomic_json(self.status_path(state["job_id"]), state)

    def create_job(
        self,
        *,
        start_at: str | None = None,
        timezone_name: str = "Asia/Shanghai",
        processing_options: dict[str, Any] | None = None,
    ) -> str:
        from life_logger.local_web import (
            _iso,
            _parse_client_time,
            datetime,
        )

        start_at = _parse_client_time(start_at, timezone_name)
        # Archive policy is independent of client/browser/source timezone.
        timezone_name = LOCAL_TZ.key
        preferences = _read_preferences(self.root)
        raw_options = processing_options or {}
        asr_model = str(
            raw_options.get("asr_model")
            or preferences.get("asr_model")
            or "mlx-whisper-turbo"
        )
        if asr_model == "fun-asr-nano":
            asr_model = "mlx-whisper-turbo"
        speaker_model = str(
            raw_options.get("speaker_model")
            or preferences.get("speaker_model")
            or "cam++"
        )
        if asr_model not in SUPPORTED_ASR_MODELS:
            raise ValueError("当前主链路暂不支持这个 ASR 模型")
        if speaker_model not in SUPPORTED_SPEAKER_MODELS:
            raise ValueError("当前主链路暂不支持这个声源模型")
        stamp = datetime.now(tz=LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
        job_id = f"job-{stamp}-{uuid.uuid4().hex[:8]}"
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        for name in ("input", "files", "uploads"):
            (job_dir / name).mkdir()
        state = {
            "job_id": job_id,
            "status": "draft",
            "created_at": _iso(),
            "updated_at": _iso(),
            "started_at": None,
            "completed_at": None,
            "elapsed_seconds": 0.0,
            "current_stage": "waiting_for_files",
            "progress": {"completed": 0, "total": 0, "unit": "files"},
            "timezone": timezone_name,
            "manual_start_at": start_at,
            "processing_options": {
                "asr_model": asr_model,
                "speaker_model": speaker_model,
            },
            "files": [],
            "logs": [],
            "error": None,
            "results": [],
        }
        with self._lock:
            self._save_unlocked(state)
        return job_id

    def _seed_watch_formal_asr_unlocked(
        self,
        job_id: str,
        device_id: str,
        session_id: str,
        timezone_name: str,
        processor: WatchFormalASRProcessor,
    ) -> bool:
        """Attach persisted per-segment ASR to the full-session Job, without rerunning ASR."""
        from life_logger.local_web import (
            _iso,
        )

        state = self._read_unlocked(job_id)
        item = next(
            (
                candidate
                for candidate in state.get("files", [])
                if isinstance(candidate, dict)
                and candidate.get("capture_source") == WATCH_CAPTURE_SOURCE
                and candidate.get("source_device") == device_id
                and candidate.get("source_session") == session_id
            ),
            None,
        )
        if item is None:
            return False
        asr_stage = item.get("stages", {}).get("asr", {})
        if isinstance(asr_stage, dict) and asr_stage.get("status") == "completed":
            if state.pop("watch_pending_session", None) is not None:
                self._save_unlocked(state)
            return True
        digest = str(item.get("sha256") or "")
        relative_audio = str(item.get("stored_path") or "")
        if not digest or not relative_audio:
            return False
        job_audio = self.job_path(job_id) / relative_audio
        target_asr_dir = self.job_path(job_id) / "files" / digest / "asr"
        if not processor.materialize_for_job(
            device_id,
            session_id,
            job_audio,
            target_asr_dir,
            timezone_name,
        ):
            return False
        item.setdefault("stages", {})["asr"] = {
            "status": "completed",
            "elapsed_seconds": 0.0,
            "completed_at": _iso(),
            "source": "watch_segment_checkpoints",
        }
        segment_count = len(
            processor.store.load_session(device_id, session_id).get("segments") or []
        )
        item["progress"] = {
            "completed": segment_count,
            "total": segment_count,
            "unit": "segments",
        }
        state.pop("watch_pending_session", None)
        self._save_unlocked(state)
        return True

    def update_from_worker(self, job_id: str, event: dict[str, Any]) -> None:
        from life_logger.local_web import (
            _is_retryable_asr_slot_error,
            _iso,
            _now,
            apply_file_outcomes,
        )

        schedule_context_retry = False
        with self._lock:
            state = self._read_unlocked(job_id)
            kind = event.get("type")
            now = _now()
            if kind == "job_started":
                state["status"] = "running"
                state["started_at"] = state.get("started_at") or now
                state["current_stage"] = "starting"
                retry = state.get("automatic_retry")
                if isinstance(retry, dict):
                    state["automatic_retry"] = {
                        "attempts": max(0, int(retry.get("attempts", 0))),
                        "next_attempt_at": None,
                    }
            elif kind == "stage_started":
                item = state["files"][event["file_index"]]
                stage = event["stage"]
                item["status"] = "running"
                item["current_stage"] = stage
                # 重跑同一阶段时保留上一轮的失败记录：否则「曾经失败过」这件事会被
                # 重跑成功整段抹掉，任务又变成看不出问题的 completed（假成功）。
                previous = item["stages"].get(stage)
                carried: dict[str, Any] = {}
                if isinstance(previous, dict):
                    for key in ("error", "failures"):
                        if previous.get(key):
                            carried[key] = previous[key]
                item["stages"][stage] = {
                    "status": "running",
                    "started_at": now,
                    "progress": {"completed": 0, "total": 0, "unit": "windows"},
                    **carried,
                }
                state["current_stage"] = stage
            elif kind == "progress":
                item = state["files"][event["file_index"]]
                item["progress"] = {
                    "completed": event.get("completed", 0),
                    "total": event.get("total", 0),
                    "unit": event.get("unit", "windows"),
                }
                stage_state = item["stages"].get(item["current_stage"])
                if stage_state is not None:
                    stage_state["progress"] = item["progress"]
            elif kind == "log":
                line = str(event.get("line", "")).rstrip()
                if line:
                    item = state["files"][event["file_index"]]
                    item["logs"] = (item.get("logs", []) + [line])[-80:]
                    state["logs"] = (state.get("logs", []) + [line])[-120:]
            elif kind == "stage_finished":
                item = state["files"][event["file_index"]]
                stage_state = item["stages"].setdefault(event["stage"], {})
                stage_state.update(
                    status=event["status"],
                    elapsed_seconds=event.get("elapsed_seconds", 0.0),
                )
                if event["status"] == "failed":
                    item["status"] = "failed"
                    item["error"] = event.get("error")
                    # 失败要落成结构化记录：滚动 logs 会被冲掉，而阶段状态会被后面
                    # 重跑成功的 completed 覆盖，只剩 stage_errors 能留住证据。
                    stage_state["error"] = event.get("error")
                    if event.get("severity"):
                        stage_state["severity"] = event["severity"]
                    history = stage_state.get("failures")
                    if not isinstance(history, list):
                        history = []
                    history.append(
                        {
                            "error": event.get("error"),
                            "stage": event["stage"],
                            "severity": event.get("severity"),
                            "at": _iso(now),
                        }
                    )
                    stage_state["failures"] = history[-10:]
                else:
                    recovered = str(event.get("recovered_from") or "").strip()
                    if recovered:
                        # 重跑成功：失败证据保留在 failures/error 里，任务状态降为
                        # degraded（结果可用，但过程不是一次跑干净的）。
                        stage_state.setdefault("error", recovered)
                        history = stage_state.get("failures")
                        if not isinstance(history, list) or not history:
                            stage_state["failures"] = [
                                {
                                    "error": recovered,
                                    "stage": event["stage"],
                                    "severity": stage_severity(event["stage"]),
                                    "at": _iso(now),
                                }
                            ]
            elif kind == "file_finished":
                item = state["files"][event["file_index"]]
                item["status"] = event["status"]
                item["current_stage"] = (
                    "completed"
                    if event["status"] == "completed"
                    else item["current_stage"]
                )
                item["results"] = event.get("results", [])
                item["error"] = event.get("error")
                if event.get("outcome") == "no_speech":
                    item["outcome"] = "no_speech"
                    item["outcome_reason"] = str(
                        event.get("outcome_reason")
                        or "ASR 已完成且无语音；原音频已保留。"
                    )
                state["progress"]["completed"] = sum(
                    value["status"] == "completed" for value in state["files"]
                )
            elif kind == "job_finished":
                if state.get("started_at"):
                    state["elapsed_seconds"] = round(
                        float(state.get("elapsed_seconds", 0.0))
                        + max(0.0, now - float(state["started_at"])),
                        2,
                    )
                state["started_at"] = None
                state["status"] = event["status"]
                state["completed_at"] = _iso(now)
                state["error"] = event.get("error")
                retryable_slot_error = (
                    event["status"] == "failed"
                    and _is_retryable_asr_slot_error(event.get("error"))
                )
                if retryable_slot_error:
                    previous_retry = state.get("automatic_retry")
                    attempts = (
                        max(0, int(previous_retry.get("attempts", 0)))
                        if isinstance(previous_retry, dict)
                        else 0
                    ) + 1
                    delay = min(30 * (2 ** min(attempts - 1, 4)), 300)
                    state["automatic_retry"] = {
                        "attempts": attempts,
                        "next_attempt_at": _iso(now + delay),
                    }
                else:
                    state.pop("automatic_retry", None)
                state["current_stage"] = (
                    event["status"]
                    if event["status"] in {"completed", "degraded", "partial"}
                    else "failed"
                )
                # 子步骤失败不再被 completed 掩盖：结果照常保留，但任务级状态按严重性
                # 降为 partial（致命）/ degraded（可降级），并把失败写进 stage_errors。
                # 传 job_dir 是为了核对阶段落盘日志与产物 —— 状态说完成、日志其实是崩的
                # 那种假成功只有对着证据才看得出来。
                apply_file_outcomes(state, job_dir=self.job_path(job_id))
                state["results"] = sorted(
                    {
                        path
                        for item in state["files"]
                        for path in item.get("results", [])
                    }
                )
                schedule_context_retry = event.get("status") == "completed"
            self._save_unlocked(state)
        if schedule_context_retry:
            self._schedule_context_auto_retry(job_id)

    def _update_context_recovery(
        self,
        job_id: str,
        recovery_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        from life_logger.local_web import (
            _iso,
        )

        with self._lock:
            state = self._read_unlocked(job_id)
            recovery = state.get("context_recovery")
            if not isinstance(recovery, dict) or recovery.get("recovery_id") != recovery_id:
                return None
            recovery.update(updates)
            if recovery.get("origin") == "automatic" and updates.get("status") == "completed":
                auto_retry = state.get("context_auto_retry")
                if isinstance(auto_retry, dict):
                    extraction_status = recovery.get("extraction_status")
                    auto_retry.update(
                        status=(
                            "completed"
                            if extraction_status == "completed"
                            else "needs_review"
                            if extraction_status == "degraded"
                            else "stopped"
                        ),
                        finished_at=updates.get("finished_at") or _iso(),
                    )
                    if extraction_status not in {"completed", "degraded"}:
                        auto_retry["stopped_reason"] = "attempt_failed"
                    auto_retry.pop("next_attempt_at", None)
            self._save_unlocked(state)
            return recovery
