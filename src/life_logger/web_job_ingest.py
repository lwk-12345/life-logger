"""JobStore 的 _JobIngestMixin 方法，从 local_web.py 机械搬出，行为不变。"""
from __future__ import annotations

from datetime import timezone
from life_logger.ingest import AUDIO_EXTENSIONS
from life_logger.passport_stream import CAPTURE_SOURCE
from life_logger.timecode import infer_start_time
from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WATCH_CAPTURE_SOURCE
from life_logger.web_common import logger
from pathlib import Path
from typing import Any
from typing import BinaryIO
from typing import Mapping
from typing import Sequence
from zoneinfo import ZoneInfo
import json
import math
import os
import shutil
import threading
import uuid
from life_logger.web_job_helpers import _safe_relative_path, _stream_to_file


class _JobIngestMixin:
    def status_path(self, job_id: str) -> Path:
        return self.job_path(job_id) / "status.json"

    def manual_events_path(self, job_id: str) -> Path:
        return self.job_path(job_id) / "manual-events.json"

    def _load_relation_candidates_unlocked(
        self, job_id: str, artifact_path: str
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        artifact_path = str(artifact_path or "").strip()
        if not artifact_path:
            raise ValueError("缺少语境提取文件路径")
        job_dir = self.job_path(job_id).resolve()
        target = (job_dir / artifact_path).resolve()
        if target == job_dir or job_dir not in target.parents:
            raise ValueError("语境提取文件路径越界")
        if target.name != "context-extraction.json" or not target.is_file():
            raise ValueError("语境提取文件不存在")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("语境提取文件格式无效") from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("relation_candidates"), list
        ):
            raise ValueError("语境提取文件缺少关系候选")
        candidates: dict[str, dict[str, Any]] = {}
        for candidate in payload["relation_candidates"]:
            if not isinstance(candidate, dict):
                continue
            candidate_id = candidate.get("candidate_id")
            if isinstance(candidate_id, str) and candidate_id:
                candidates[candidate_id] = candidate
        return artifact_path, candidates

    def task_states_path(self) -> Path:
        return self.root / "task-states.json"

    def auto_ingest_path(self) -> Path:
        return self.root / "auto-ingest.json"

    def _default_auto_ingest_state(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1",
            "enabled": False,
            "folders": [],
            "scan_interval_seconds": 15,
            "stability_seconds": 20,
            "candidates": {},
            "processed": {},
            "last_scan_at": None,
            "last_scan_started_at": None,
            "last_scan_duration_seconds": None,
            "scan_state": "idle",
            "last_discovered_count": 0,
            "last_error": None,
            "last_job_id": None,
            "last_import_count": 0,
            "history": [],
        }

    def _append_auto_ingest_history_unlocked(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
    ) -> None:
        history = state.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            state["history"] = history
        history.append(entry)
        del history[:-30]

    def auto_ingest_status(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_auto_ingest_unlocked()
            folders = []
            for raw in state.get("folders", []):
                path = Path(str(raw)).expanduser()
                folders.append(
                    {
                        "path": str(path),
                        "exists": path.is_dir(),
                    }
                )
            active_job_id = str(state.get("last_job_id") or "")
            active_job_status = None
            if active_job_id:
                try:
                    active_job_status = self._read_unlocked(active_job_id).get("status")
                except (FileNotFoundError, ValueError, json.JSONDecodeError):
                    active_job_status = None
            folder_errors = [
                {"path": item["path"], "error": "目录未挂载或不存在"}
                for item in folders
                if not item["exists"]
            ]
            return {
                "enabled": bool(state.get("enabled")),
                "folders": folders,
                "folder_errors": folder_errors,
                "scan_interval_seconds": int(state.get("scan_interval_seconds") or 15),
                "stability_seconds": int(state.get("stability_seconds") or 20),
                "pending_count": len(state.get("candidates", {})),
                "processed_count": len(state.get("processed", {})),
                "last_scan_at": state.get("last_scan_at"),
                "last_scan_started_at": state.get("last_scan_started_at"),
                "last_scan_duration_seconds": state.get("last_scan_duration_seconds"),
                "scan_state": str(state.get("scan_state") or "idle"),
                "last_discovered_count": int(state.get("last_discovered_count") or 0),
                "last_error": state.get("last_error"),
                "last_job_id": active_job_id or None,
                "last_job_status": active_job_status,
                "last_import_count": int(state.get("last_import_count") or 0),
                "history": list(reversed(state.get("history", [])[-12:])),
            }

    def add_auto_ingest_folder(self, folder: str) -> dict[str, Any]:
        """Persist a picked folder without changing auto-ingest enablement."""

        with self._lock:
            state = self._read_auto_ingest_unlocked()
            folders = [str(item) for item in state.get("folders", [])]
            value = str(folder or "").strip()
            if value and value not in folders:
                folders.append(value)
            enabled = bool(state.get("enabled"))
            stability = int(state.get("stability_seconds") or 20)
            interval = int(state.get("scan_interval_seconds") or 15)
        return self.configure_auto_ingest(
            enabled=enabled,
            folders=folders,
            stability_seconds=stability,
            scan_interval_seconds=interval,
        )

    def _auto_ingest_active_unlocked(self, state: dict[str, Any]) -> bool:
        job_id = str(state.get("last_job_id") or "")
        if not job_id:
            return False
        try:
            job = self._read_unlocked(job_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return False
        return job.get("status") in {"queued", "running"}

    def audio_path_for_artifact(self, job_id: str, artifact_path: str) -> Path:
        """Resolve the uploaded source audio associated with a result artifact."""
        artifact_path = str(artifact_path or "").strip()
        if not artifact_path:
            raise ValueError("缺少结果文件路径")
        parts = Path(artifact_path).parts
        if len(parts) < 3 or parts[0] != "files":
            raise ValueError("结果文件无法定位到原始录音")
        file_hash = parts[1]
        state = self.read(job_id)
        file_state = next(
            (
                item
                for item in state.get("files", [])
                if str(item.get("sha256") or "") == file_hash
                or str(item.get("file_id") or "") == file_hash
            ),
            None,
        )
        if file_state is None:
            raise ValueError("结果文件对应的原始录音不存在")
        job_dir = self.job_path(job_id).resolve()
        source = (job_dir / str(file_state.get("stored_path") or "")).resolve()
        if source != job_dir and job_dir not in source.parents:
            raise ValueError("原始录音路径越界")
        if not source.is_file():
            raise ValueError("原始录音文件不存在")
        return source

    def audio_clip_spans_for_artifact(
        self,
        job_id: str,
        artifact_path: str,
        *,
        start: float,
        end: float,
    ) -> list[dict[str, Any]] | None:
        """Map session-time offsets to retained Watch segment files.

        Watch ASR timestamps use the capture timeline, but the assembled M4A is a
        stream-copy concatenation whose packet durations can drift from that
        timeline. For Watch sources, preview from the original segments instead
        of seeking into the compacted assembled file. Other sources keep using
        the single uploaded file.
        """
        parts = Path(str(artifact_path or "").strip()).parts
        if len(parts) < 3 or parts[0] != "files":
            return None
        file_key = parts[1]
        state = self.read(job_id)
        file_state = next(
            (
                item
                for item in state.get("files", [])
                if str(item.get("sha256") or "") == file_key
                or str(item.get("file_id") or "") == file_key
            ),
            None,
        )
        if not isinstance(file_state, dict):
            return None
        if (
            file_state.get("capture_source") != WATCH_CAPTURE_SOURCE
            or file_state.get("source_transport") != "watch-http-m4a-segments-v1"
        ):
            return None

        source_session_dir = str(file_state.get("source_session_dir") or "").strip()
        if not source_session_dir:
            raise ValueError("Watch 原始分段目录缺失，无法准确试听")
        session_dir = Path(source_session_dir)
        if not session_dir.is_absolute():
            session_dir = self.root / session_dir
        session_dir = session_dir.resolve()
        if session_dir == self.root or self.root not in session_dir.parents:
            raise ValueError("Watch 原始分段路径越界")

        manifest_path = session_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("Watch 原始分段清单不存在，无法准确试听")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("Watch 原始分段清单无效，无法准确试听") from error
        raw_segments = manifest.get("segments") if isinstance(manifest, dict) else None
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError("Watch 原始分段清单为空，无法准确试听")

        segments: list[tuple[int, int, int, Path]] = []
        segment_root = (session_dir / "segments").resolve()
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            try:
                sequence = int(item.get("sequence"))
                offset_ms = int(item.get("start_offset_ms"))
                duration_ms = int(item.get("duration_ms"))
            except (TypeError, ValueError, OverflowError):
                continue
            filename = str(item.get("file") or "")
            if sequence < 0 or offset_ms < 0 or duration_ms <= 0:
                continue
            if not filename or Path(filename).name != filename:
                continue
            audio_path = (segment_root / filename).resolve()
            if segment_root not in audio_path.parents or not audio_path.is_file():
                continue
            segments.append((offset_ms, duration_ms, sequence, audio_path))
        if not segments:
            raise ValueError("Watch 原始音频分段不可用，无法准确试听")
        segments.sort(key=lambda row: (row[0], row[2]))

        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("音频片段时间范围无效")
        clip_start_ms = max(0.0, start * 1000 - 350)
        clip_end_ms = min(
            end * 1000 + 350,
            max(offset_ms + duration_ms for offset_ms, duration_ms, _, _ in segments),
        )
        if clip_end_ms <= clip_start_ms:
            raise ValueError("音频片段超出 Watch 录音范围")

        spans: list[dict[str, Any]] = []
        cursor_ms = clip_start_ms
        for offset_ms, duration_ms, _, audio_path in segments:
            segment_end_ms = offset_ms + duration_ms
            if segment_end_ms <= cursor_ms or offset_ms >= clip_end_ms:
                continue
            if offset_ms > cursor_ms:
                gap_end_ms = min(offset_ms, clip_end_ms)
                spans.append({"silence_seconds": (gap_end_ms - cursor_ms) / 1000})
                cursor_ms = gap_end_ms
            overlap_start_ms = max(cursor_ms, offset_ms)
            overlap_end_ms = min(clip_end_ms, segment_end_ms)
            if overlap_end_ms > overlap_start_ms:
                spans.append(
                    {
                        "path": audio_path,
                        "start_seconds": (overlap_start_ms - offset_ms) / 1000,
                        "end_seconds": (overlap_end_ms - offset_ms) / 1000,
                    }
                )
                cursor_ms = overlap_end_ms
            if cursor_ms >= clip_end_ms:
                break
        if cursor_ms < clip_end_ms:
            spans.append({"silence_seconds": (clip_end_ms - cursor_ms) / 1000})
        return spans

    def create_watch_pending_job(
        self,
        *,
        device_id: str,
        session_id: str,
        start_at: str | None,
        timezone_name: str,
    ) -> str:
        """Reserve one stable Job ID so the Watch can record completion acceptance."""
        device = str(device_id or "")
        session = str(session_id or "")
        with self._lock:
            for status_path in self.jobs_dir.glob("job-*/status.json"):
                try:
                    state = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                pending = state.get("watch_pending_session")
                if (
                    isinstance(pending, dict)
                    and pending.get("device_id") == device
                    and pending.get("session_id") == session
                ):
                    return str(state.get("job_id") or status_path.parent.name)
                if any(
                    isinstance(item, dict)
                    and item.get("capture_source") == WATCH_CAPTURE_SOURCE
                    and item.get("source_device") == device
                    and item.get("source_session") == session
                    for item in state.get("files", [])
                ):
                    return str(state.get("job_id") or status_path.parent.name)
            job_id = self.create_job(
                start_at=start_at,
                timezone_name=timezone_name,
            )
            state = self._read_unlocked(job_id)
            state["watch_pending_session"] = {
                "device_id": device,
                "session_id": session,
            }
            self._save_unlocked(state)
            return job_id

    def add_file(
        self,
        job_id: str,
        *,
        filename: str,
        relative_path: str,
        stream: BinaryIO,
        modified_at: float | None = None,
        max_bytes: int | None = None,
        reuse_existing: bool = False,
        source_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in AUDIO_EXTENSIONS:
            raise ValueError(f"不支持的音频格式：{suffix or '无扩展名'}")
        # [2026-10-22 P0] 收流（可能几十~几百 MB、耗时数十秒）**必须在锁外**做。
        # 原来整段上传都包在 `self._lock` 里，后台一收大文件，所有读接口
        # (/api/daily、/api/dashboard/daily、/api/stats) 就全部排队等锁。
        state = self._read_unlocked(job_id)
        if state["status"] == "running":
            raise ValueError("任务正在运行，不能继续上传")
        job_dir = self.job_path(job_id)
        temporary = job_dir / "uploads" / f"{uuid.uuid4().hex}.partial"
        digest, size = _stream_to_file(
            stream,
            temporary,
            max_bytes=max_bytes,
        )
        with self._lock:
            # 收流期间作业状态可能已变（另一个上传/后台任务），重新读一次
            state = self._read_unlocked(job_id)
            if max_bytes is not None and size != max_bytes:
                temporary.unlink(missing_ok=True)
                raise ValueError(
                    f"上传不完整：应接收 {max_bytes} 字节，实际收到 {size} 字节"
                )
            content_path = self.content_dir / digest
            if not content_path.exists():
                os.replace(temporary, content_path)
                # replace 之后补目录 fsync；失败只告警（内容已可见，不谎称没写成）
                try:
                    from life_logger.atomic import sync_directory

                    durability, warning = sync_directory(self.content_dir)
                    if durability == "unknown":
                        logger.warning("上传内容已替换但持久性未知: %s", content_path)
                except OSError as sync_exc:
                    logger.warning("上传内容目录 fsync 失败 (%s): %s",
                                   type(sync_exc).__name__, sync_exc)
            else:
                temporary.unlink(missing_ok=True)

            existing = next(
                (item for item in state["files"] if item["sha256"] == digest),
                None,
            )
            if existing is not None:
                self._save_unlocked(state)
                return {"duplicate": True, "file": existing}

            if reuse_existing and not state["files"]:
                priority = {
                    "running": 5,
                    "queued": 4,
                    "completed": 3,
                    "partial": 2,
                    "interrupted": 1,
                    "failed": 0,
                }
                candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
                for status_path in self.jobs_dir.glob("job-*/status.json"):
                    other_job_id = status_path.parent.name
                    if other_job_id == job_id:
                        continue
                    try:
                        other = json.loads(status_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    rank = priority.get(str(other.get("status") or ""), 0)
                    if not rank:
                        continue
                    if other.get("timezone") != state.get("timezone"):
                        continue
                    if other.get("manual_start_at") != state.get("manual_start_at"):
                        continue
                    if other.get("processing_options") != state.get(
                        "processing_options"
                    ):
                        continue
                    other_files = other.get("files")
                    if not isinstance(other_files, list) or len(other_files) != 1:
                        continue
                    other_file = other_files[0]
                    if (
                        not isinstance(other_file, dict)
                        or other_file.get("sha256") != digest
                    ):
                        continue
                    candidates.append(
                        (
                            rank,
                            str(other.get("updated_at") or ""),
                            other,
                            other_file,
                        )
                    )
                if candidates:
                    _, _, reusable_state, reusable_file = max(
                        candidates,
                        key=lambda item: (item[0], item[1]),
                    )
                    return {
                        "duplicate": True,
                        "reused_existing_job": True,
                        "reusable_job_id": reusable_state["job_id"],
                        "reusable_job_status": reusable_state["status"],
                        "file": reusable_file,
                    }

            safe_name = Path(filename).name or f"recording{suffix}"
            stored_path = job_dir / "input" / f"{digest}__{safe_name}"
            if not stored_path.exists():
                try:
                    os.link(content_path, stored_path)
                except OSError:
                    import shutil

                    shutil.copyfile(content_path, stored_path)
            if modified_at and modified_at > 0:
                os.utime(stored_path, (modified_at, modified_at))

            inferred, source, precision = infer_start_time(stored_path)
            if inferred is not None:
                timezone = ZoneInfo(state["timezone"])
                inferred = (
                    inferred.replace(tzinfo=timezone)
                    if inferred.tzinfo is None
                    else inferred.astimezone(timezone)
                )
            file_state = {
                "file_id": digest,
                "sha256": digest,
                "original_name": safe_name,
                "relative_path": _safe_relative_path(relative_path, safe_name),
                "stored_path": str(stored_path.relative_to(job_dir)),
                "size_bytes": size,
                "inferred_start_at": inferred.isoformat() if inferred else None,
                "effective_start_at": None,
                "time_source": source,
                "time_precision": precision,
                "status": "queued",
                "current_stage": "waiting",
                "progress": {"completed": 0, "total": 0, "unit": "windows"},
                "stages": {},
                "logs": [],
                "error": None,
                "results": [],
            }
            if source_metadata:
                for key in (
                    "capture_source",
                    "source_device",
                    "source_session",
                    "source_transport",
                    "source_format",
                    "source_integrity",
                    "source_session_dir",
                ):
                    value = source_metadata.get(key)
                    if value not in (None, ""):
                        file_state[key] = value
            state["files"].append(file_state)
            state["progress"]["total"] = len(state["files"])
            self._save_unlocked(state)
            return {"duplicate": False, "file": file_state}

    def import_passport_recording(
        self,
        session_dir: Path,
        summary: Mapping[str, Any],
        config_path: Path,
        *,
        auto_start: bool = True,
    ) -> dict[str, Any]:
        """Import one complete AI Passport session into the normal job pipeline."""

        if not bool(summary.get("complete")):
            raise ValueError("AI Passport 会话不完整，拒绝导入为完整录音")
        device = str(summary.get("device") or "device")
        session = str(summary.get("session") or "unknown")
        recording = Path(session_dir) / "recording.wav"
        if not recording.is_file() or recording.stat().st_size <= 44:
            raise ValueError("AI Passport 完整会话缺少可用 recording.wav")

        for job in self.list():
            for item in job.get("files", []):
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("capture_source") == CAPTURE_SOURCE
                    and item.get("source_device") == device
                    and item.get("source_session") == session
                ):
                    return {
                        "job_id": job["job_id"],
                        "reused": True,
                        "file": item,
                    }

        start_at = str(summary.get("received_started_at") or "") or None
        job_id = self.create_job(
            start_at=start_at,
            timezone_name="Asia/Shanghai",
        )
        try:
            relative_session_dir = str(
                Path(session_dir).resolve().relative_to(self.root)
            )
        except ValueError:
            relative_session_dir = str(Path(session_dir).resolve())
        with recording.open("rb") as stream:
            result = self.add_file(
                job_id,
                filename=f"AI-Passport-{session}.wav",
                relative_path=f"AI Passport/{device}/{session}/recording.wav",
                stream=stream,
                max_bytes=recording.stat().st_size,
                source_metadata={
                    "capture_source": CAPTURE_SOURCE,
                    "source_device": device,
                    "source_session": session,
                    "source_transport": "lan-http-pcm",
                    "source_format": str(summary.get("format") or "s16le"),
                    "source_integrity": "complete-crc32-verified",
                    "source_session_dir": relative_session_dir,
                },
            )
        if auto_start:
            self.start_job(
                job_id,
                config_path,
                secrets_path=self.root / "secrets.json",
            )
        return {"job_id": job_id, "reused": False, **result}

    def import_watch_recording(
        self,
        session_dir: Path,
        summary: Mapping[str, Any],
        config_path: Path,
        *,
        auto_start: bool = True,
        formal_asr_processor: WatchFormalASRProcessor | None = None,
        reserved_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically import one Watch session so concurrent retries reuse one job."""
        with self._lock:
            return self._import_watch_recording_unlocked(
                session_dir,
                summary,
                config_path,
                auto_start=auto_start,
                formal_asr_processor=formal_asr_processor,
                reserved_job_id=reserved_job_id,
            )

    def _import_watch_recording_unlocked(
        self,
        session_dir: Path,
        summary: Mapping[str, Any],
        config_path: Path,
        *,
        auto_start: bool,
        formal_asr_processor: WatchFormalASRProcessor | None,
        reserved_job_id: str | None,
    ) -> dict[str, Any]:
        """Perform the Watch import while the JobStore lock is held."""

        if not bool(summary.get("complete")):
            raise ValueError("Apple Watch 会话不完整，拒绝导入")
        device = str(summary.get("device_id") or "watch")
        session = str(summary.get("session_id") or "unknown")
        recording_name = str(summary.get("recording_file") or "recording.m4a")
        recording = Path(session_dir) / recording_name
        if not recording.is_file() or recording.stat().st_size <= 0:
            raise ValueError("Apple Watch 完整会话缺少 recording.m4a")

        for job in self.list():
            for item in job.get("files", []):
                if (
                    isinstance(item, dict)
                    and item.get("capture_source") == WATCH_CAPTURE_SOURCE
                    and item.get("source_device") == device
                    and item.get("source_session") == session
                ):
                    job_id = str(job["job_id"])
                    prior_status = job.get("status")
                    if formal_asr_processor is not None and not self._seed_watch_formal_asr_unlocked(
                        job_id,
                        device,
                        session,
                        str(job.get("timezone") or summary.get("timezone") or "Asia/Shanghai"),
                        formal_asr_processor,
                    ):
                        raise ValueError("Watch正式分段ASR尚未能汇总到整场任务，任务保留等待重试")
                    job = self._read_unlocked(job_id)
                    if prior_status in {"completed", "failed"}:
                        job["status"] = prior_status
                    if auto_start and job.get("status") not in {"completed", "failed"}:
                        self.start_job(
                            job_id,
                            config_path,
                            secrets_path=self.root / "secrets.json",
                        )
                    return {
                        "job_id": job_id,
                        "reused": True,
                        "file": item,
                    }

        try:
            relative_session_dir = str(
                Path(session_dir).resolve().relative_to(self.root)
            )
        except ValueError:
            relative_session_dir = str(Path(session_dir).resolve())
        if reserved_job_id:
            job_id = str(reserved_job_id)
            if not job_id.startswith("job-") or Path(job_id).name != job_id:
                raise ValueError("invalid reserved Watch job id")
            state = self._read_unlocked(job_id)
            pending = state.get("watch_pending_session")
            if not (
                isinstance(pending, dict)
                and pending.get("device_id") == device
                and pending.get("session_id") == session
                and not state.get("files")
            ):
                raise ValueError("reserved Watch job does not match this session")
        else:
            job_id = self.create_job(
                start_at=str(summary.get("started_at") or "") or None,
                timezone_name=str(summary.get("timezone") or "Asia/Shanghai"),
            )
        with recording.open("rb") as stream:
            result = self.add_file(
                job_id,
                filename=f"Apple-Watch-{device}-{session}.m4a",
                relative_path=f"Apple Watch/{device}/{session}/recording.m4a",
                stream=stream,
                max_bytes=recording.stat().st_size,
                source_metadata={
                    "capture_source": WATCH_CAPTURE_SOURCE,
                    "source_device": device,
                    "source_session": session,
                    "source_transport": "watch-http-m4a-segments-v1",
                    "source_format": str(summary.get("codec") or "m4a-aac"),
                    "source_integrity": "sha256-verified-session-segments",
                    "source_session_dir": relative_session_dir,
                },
            )
        file_state = result.get("file") if isinstance(result, dict) else None
        digest = str(file_state.get("sha256") or "") if isinstance(file_state, dict) else ""
        if digest:
            try:
                content_path = self.content_dir / digest
                if content_path.is_file() and recording.is_file():
                    c_stat = content_path.stat()
                    r_stat = recording.stat()
                    if c_stat.st_ino != r_stat.st_ino and c_stat.st_dev == r_stat.st_dev:
                        temp_link = recording.with_name(f"{recording.name}.link.{uuid.uuid4().hex}")
                        os.link(content_path, temp_link)
                        os.replace(temp_link, recording)
            except Exception:
                pass
        if formal_asr_processor is not None and digest and isinstance(file_state, dict):
            if not self._seed_watch_formal_asr_unlocked(
                job_id,
                device,
                session,
                str(summary.get("timezone") or "Asia/Shanghai"),
                formal_asr_processor,
            ):
                # Keep the reserved draft and uploaded audio intact. The completion loop can
                # retry materialization without dispatching a second, whole-session ASR pass.
                raise ValueError("Watch正式分段ASR尚未能汇总到整场任务，任务保留等待重试")
        state = self._read_unlocked(job_id)
        state.pop("watch_pending_session", None)
        self._save_unlocked(state)
        if auto_start:
            self.start_job(
                job_id,
                config_path,
                secrets_path=self.root / "secrets.json",
            )
        return {"job_id": job_id, "reused": False, **result}

    def prepare_for_start(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._read_unlocked(job_id)
            if not state["files"]:
                raise ValueError("请先选择至少一个音频文件")
            if state["status"] == "running":
                return state
            manual = state.get("manual_start_at")
            for index, item in enumerate(state["files"]):
                if manual and index == 0:
                    item["effective_start_at"] = manual
                    item["time_source"] = "user"
                    item["time_precision"] = "exact"
                elif not item.get("effective_start_at"):
                    item["effective_start_at"] = item.get("inferred_start_at")
                if item["status"] in {"failed", "interrupted", "running"}:
                    item["status"] = "queued"
                    item["error"] = None
                for stage in item.get("stages", {}).values():
                    if stage.get("status") == "running":
                        stage["status"] = "pending"
            state.update(
                status="queued",
                current_stage="queued",
                error=None,
                completed_at=None,
            )
            state.pop("restart_recovery_pending", None)
            state["progress"] = {
                "completed": sum(
                    item["status"] == "completed" for item in state["files"]
                ),
                "total": len(state["files"]),
                "unit": "files",
            }
            self._save_unlocked(state)
            return state

    def _wait_for_worker_slot(self, limit: int) -> None:
        """并发上限控制：在跑的任务数到达 limit 时，等任意一个跑完再返回。

        为什么需要上限而不是「全串行」或「全放行」：
          * 全放行会同时拉起 N 个任务，每个都要独立加载 ASR + 声纹模型
            （实测 2.78 GB / 213% CPU 一份）。一次 11 个直接把 16 GB 机器
            压到 100% 压缩段、内核 watchdog 90 秒抢不到 CPU → kernel panic。
          * 全串行又浪费：8 核机器在 3 个任务并行时负载约 8.5，是稳的。
        所以按资源给一个可调上限，有空位就补，而不是跑完一个才放下一个。
        """
        if limit <= 1:
            with self._lock:
                workers = list(self._active.items())
            for job_id, worker in workers:
                if worker is not threading.current_thread():
                    worker.join()
            return
        while True:
            with self._lock:
                alive = [
                    worker
                    for job_id, worker in self._active.items()
                    if worker.is_alive() and worker is not threading.current_thread()
                ]
            if len(alive) < limit:
                return
            if not alive:
                return
            alive[0].join(timeout=5.0)

    def resume_interrupted_jobs(
        self,
        config_path: Path,
        *,
        secrets_path: Path | None = None,
        job_ids: Sequence[str] | None = None,
        concurrency: int | None = None,
    ) -> list[str]:
        limit = int(
            concurrency
            if concurrency is not None
            else os.environ.get("LIFE_LOGGER_JOB_CONCURRENCY", "2")
        )
        limit = max(1, min(4, limit))
        candidates = (
            list(job_ids) if job_ids is not None else self.interrupted_job_ids()
        )
        resumed: list[str] = []
        for job_id in candidates:
            try:
                state = self.read(job_id)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            if state.get("status") != "interrupted":
                continue
            self._wait_for_worker_slot(limit)
            self.start_job(job_id, config_path, secrets_path=secrets_path)
            resumed.append(job_id)
        with self._lock:
            workers = [
                worker for worker in self._active.values() if worker.is_alive()
            ]
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join()
        return resumed

    def start_job(
        self,
        job_id: str,
        config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_unlocked(job_id)
            recovery = state.get("context_recovery")
            if isinstance(recovery, dict) and recovery.get("status") in {
                "queued",
                "running",
            }:
                raise ValueError("事件提取恢复正在运行，不能同时重启整场任务")
            if self.dispatch_queue is not None:
                previous = state.get("processing_dispatch") or {}
                active_task = self.dispatch_queue.get(str(previous.get("task_id") or ""))
                if active_task and active_task["status"] in {"queued", "running"}:
                    return self.read(job_id)
                self.prepare_for_start(job_id)
                task = self.dispatch_queue.enqueue("pipeline", f"pipeline:{job_id}", {
                    "job_id": job_id, "config_path": str(Path(config_path).resolve()),
                    "secrets_path": str(Path(secrets_path).resolve()) if secrets_path else None,
                })
                queued = self._read_unlocked(job_id)
                queued["processing_dispatch"] = {"task_id": task["id"], "mode": "external"}
                self._save_unlocked(queued)
                return self.read(job_id)
            self.prepare_for_start(job_id)
            active = self._active.get(job_id)
            if active and active.is_alive():
                return self.read(job_id)
            job_dir = self.job_path(job_id)
            thread = threading.Thread(
                target=self._worker,
                args=(job_id, job_dir, Path(config_path), secrets_path),
                name=f"life-logger-{job_id}",
                daemon=True,
            )
            self._active[job_id] = thread
            thread.start()
            return self.read(job_id)

    def _worker(
        self,
        job_id: str,
        job_dir: Path,
        config_path: Path,
        secrets_path: Path | None,
    ) -> None:
        acquired = False
        slot: dict[str, Any] | None = None
        try:
            # The queue is bounded here rather than in start_job so a burst of starts still
            # returns promptly and the HTTP caller never blocks on ASR memory.
            self._job_gates.acquire()
            acquired = True
            # 跨进程槽位：`_job_gates` 只管本进程内的并发，管不了外部脚本
            # （手表长录音转写、声纹模板标定等）。两边各跑各的会叠加爆内存，
            # 实测已被 jetsam 杀过进程。槽位表按文件记账，因此对跨进程有效。
            try:
                from life_logger.asr_slots import (
                    acquire_asr_slot,
                    update_asr_slot_child_pid,
                )

                slot = acquire_asr_slot(
                    f"web-job:{job_id}",
                    limit=self._job_concurrency,
                    wait_seconds=600.0,
                    require_memory_gb=1.5,
                )
            except Exception as error:
                raise RuntimeError(f"ASR resource slot unavailable: {error}") from error
            if slot is None:
                # A failed cross-process admission must stop before model loading. Proceeding
                # without the token silently bypasses the only gate shared with Watch previews.
                raise RuntimeError("ASR resource slot unavailable; job remains retryable")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if secrets_path:
                try:
                    local_secrets = json.loads(
                        Path(secrets_path).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    local_secrets = {}
                if isinstance(local_secrets, dict):
                    deepseek_key = str(local_secrets.get("deepseek_api_key") or "")
                    deepseek_enabled = bool(local_secrets.get("deepseek_enabled"))
                    config["deepseek"] = {"enabled": deepseek_enabled}
                    if deepseek_key:
                        config["_deepseek_api_key"] = deepseek_key
            from scripts.local_jobs import run_web_job, validate_web_config

            validate_web_config(config)
            run_web_job(
                job_dir,
                config,
                lambda event: self.update_from_worker(job_id, event),
                child_pid_callback=lambda child_pid: update_asr_slot_child_pid(
                    slot,
                    child_pid,
                ),
            )
        except Exception as error:  # pragma: no cover - exercised through API
            self.update_from_worker(
                job_id,
                {"type": "job_finished", "status": "failed", "error": str(error)},
            )
        finally:
            if acquired:
                self._job_gates.release()
            if slot is not None:
                try:
                    from life_logger.asr_slots import release_asr_slot

                    release_asr_slot(slot)
                except Exception:
                    pass
            with self._lock:
                self._active.pop(job_id, None)
