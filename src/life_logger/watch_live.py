"""Restartable local previews for acknowledged Watch audio, before session close."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from life_logger.asr_slots import (
    acquire_asr_slot,
    release_asr_slot,
    update_asr_slot_child_pid,
)
from life_logger.audio_vad import should_skip_transcription
from life_logger.local_pipeline import (
    PROJECT_ROOT,
    build_asr_command,
    build_attribution_command,
    find_artifact,
    load_local_config,
    local_pipeline_env,
    validate_local_pipeline_config,
)
from life_logger.watch_stream import WatchSessionStore, _json_bytes, _write_atomic


class _NoSpeechPreview:
    """Successful ASR run with no transcript to attribute."""


class WatchLiveProcessor:
    """One local worker; receipts never wait for models or preview failures."""

    def __init__(
        self,
        store: WatchSessionStore,
        config_path: Path,
        stop: threading.Event,
        processor: Callable | None = None,
        *,
        slot_limit: int = 2,
        slot_acquirer: Callable[[str], dict | None] | None = None,
        slot_releaser: Callable[[dict], None] | None = None,
    ):
        self.store = store
        self.config_path = config_path
        self.stop = stop
        self.processor = processor or self._process
        self.slot_limit = max(1, slot_limit)
        self._uses_shared_slots = slot_acquirer is None
        self._active_slot = threading.local()
        preview_mem = float(os.environ.get("LIFE_LOGGER_PREVIEW_MEMORY_GB", "2.0"))
        self.slot_acquirer = slot_acquirer or (
            lambda owner: acquire_asr_slot(
                owner,
                limit=self.slot_limit,
                wait_seconds=0,
                require_memory_gb=preview_mem,
            )
        )
        self.slot_releaser = slot_releaser or release_asr_slot

    def _run(self, command: list[str], log: Path, config: dict, stage: str) -> None:
        with log.open("ab") as output:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=local_pipeline_env(config),
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            if self._uses_shared_slots:
                try:
                    tracked = update_asr_slot_child_pid(
                        getattr(self._active_slot, "slot", None), process.pid
                    )
                    if not tracked:
                        raise RuntimeError("ASR child PID could not be recorded")
                except Exception:
                    self._terminate_process(process)
                    raise
            deadline = time.monotonic() + 600
            while process.poll() is None:
                if self.stop.wait(0.5) or time.monotonic() > deadline:
                    self._terminate_process(process)
                    raise RuntimeError(f"{stage}阶段已暂停，可自动重试")
            if process.returncode:
                raise RuntimeError(
                    f"{stage}阶段失败（{process.returncode}），请检查本地模型"
                )

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _process(
        self, audio: Path, output: Path, started_at: str
    ) -> list[dict] | _NoSpeechPreview:
        if should_skip_transcription(audio):
            return _NoSpeechPreview()
        config = load_local_config(self.config_path)
        validate_local_pipeline_config(config)
        # Previews use local ASR and voice matching. Full-session processing
        # retains the richer context and optional expensive separation stages.
        config = {
            **config,
            "local_llm": {"enabled": False},
            "speech_separation_model": "",
        }
        asr = output / "asr"
        result = output / "result"
        try:
            self._run(
                build_asr_command(
                    audio=audio, output_dir=asr, config=config, start_at=started_at
                ),
                output / "asr.log",
                config,
                "ASR",
            )
        except Exception as error:
            raise RuntimeError(f"ASR阶段：{error}") from error
        try:
            summary_path = find_artifact(asr, "run-summary.json")
            summary = json.loads(summary_path.read_text())
            if (
                not isinstance(summary, dict)
                or summary.get("status") != "completed"
                or isinstance(summary.get("transcript_count"), bool)
                or not isinstance(summary.get("transcript_count"), int)
                or summary["transcript_count"] < 0
            ):
                raise ValueError("run-summary.json 状态或 transcript_count 无效")
            if summary["transcript_count"] == 0:
                return _NoSpeechPreview()
            transcript = find_artifact(asr, "transcript.json")
            recordings = find_artifact(asr, "recordings.json")
        except Exception as error:
            raise RuntimeError(f"ASR产物校验阶段：{error}") from error
        try:
            self._run(
                build_attribution_command(
                    audio=audio,
                    transcript=transcript,
                    recordings=recordings,
                    output_dir=result,
                    config=config,
                ),
                output / "attribution.log",
                config,
                "声纹归属",
            )
            rows = json.loads(
                find_artifact(result, "source-attribution.json").read_text()
            )
            if not isinstance(rows, list):
                raise ValueError("source-attribution.json 格式无效")
        except Exception as error:
            raise RuntimeError(f"声纹归属阶段：{error}") from error
        return [
            {
                key: row.get(key)
                for key in (
                    "text",
                    "absolute_started_at",
                    "start_offset_seconds",
                    "end_offset_seconds",
                    "source_label",
                    "voice_similarity",
                )
            }
            for row in rows
        ]

    def scan_once(self) -> bool:
        for summary in self.store.list_session_summaries():
            if self.stop.is_set():
                return False
            if summary.get("state") != "receiving":
                continue
            if summary.get("superseded") or summary.get("stalled"):
                continue
            device, session = summary["device_id"], summary["session_id"]
            manifest = self.store.load_session(device, session)
            directory = self.store.session_directory(device, session)
            for segment in sorted(
                manifest["segments"], key=lambda item: item["sequence"]
            ):
                output = directory / "live" / f"{segment['sequence']:06d}"
                state_path = output / "state.json"
                try:
                    state = json.loads(state_path.read_text())
                except (OSError, ValueError):
                    state = {}
                if state.get("sha256") == segment["sha256"]:
                    if state.get("status") in {"completed", "no_speech"}:
                        continue
                    if state.get("retry_at", 0) > time.time():
                        continue
                owner = f"watch-preview:{device}:{session}:{segment['sequence']}"
                try:
                    slot = self.slot_acquirer(owner)
                except Exception:
                    # Never run preview ASR when the shared gate is unavailable.
                    return False
                if slot is None:
                    # Leave it pending; the periodic scan retries after another ASR
                    # task releases its slot; do not advertise work that never started.
                    return False
                try:
                    self._active_slot.slot = slot
                    output.mkdir(parents=True, exist_ok=True)
                    attempt = int(state.get("attempt", 0)) + 1
                    state = {
                        "status": "processing",
                        "attempt": attempt,
                        "sha256": segment["sha256"],
                        "sequence": segment["sequence"],
                    }
                    _write_atomic(state_path, _json_bytes(state))
                    try:
                        # Reconstruct the filename from the validated sequence.
                        audio = (
                            directory
                            / "segments"
                            / f"segment-{segment['sequence']:06d}.m4a"
                        )
                        digest = "sha256:" + hashlib.sha256(
                            audio.read_bytes()
                        ).hexdigest()
                        if digest != segment["sha256"]:
                            raise ValueError("音频校验不一致，原文件已保留")
                        start = datetime.fromisoformat(
                            manifest["started_at"]
                        ) + timedelta(milliseconds=segment["start_offset_ms"])
                        rows = self.processor(audio, output, start.isoformat())
                        if isinstance(rows, _NoSpeechPreview):
                            state.update(status="no_speech", rows=[], error=None)
                        else:
                            state.update(status="completed", rows=rows, error=None)
                    except Exception as error:
                        state.update(
                            status="failed",
                            error=str(error),
                            retry_at=time.time()
                            + min(1800, 15 * 2 ** min(attempt, 7)),
                        )
                    state["updated_at"] = datetime.now().astimezone().isoformat()
                    _write_atomic(state_path, _json_bytes(state))
                finally:
                    self._active_slot.slot = None
                    self.slot_releaser(slot)
                return True
        return False


def preview_summary(directory: Path, manifest: dict) -> dict:
    """Bounded preview payload; no filesystem paths or unconfirmed life events."""
    allowed_statuses = {"completed", "no_speech", "failed", "processing", "pending"}
    row_fields = (
        "text",
        "absolute_started_at",
        "start_offset_seconds",
        "end_offset_seconds",
        "source_label",
        "voice_similarity",
    )
    counts = {status: 0 for status in allowed_statuses}
    previews = []
    for segment in sorted(
        manifest.get("segments", []), key=lambda item: item.get("sequence", -1)
    ):
        path = directory / "live" / f"{segment['sequence']:06d}" / "state.json"
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        valid_state = state.get("sha256") == segment["sha256"]
        status = state.get("status") if valid_state else "pending"
        if status not in allowed_statuses:
            status = "pending"
        counts[status] += 1
        rows = []
        if status == "completed" and isinstance(state.get("rows"), list):
            rows = [
                {key: row.get(key) for key in row_fields if key in row}
                for row in state["rows"][-8:]
                if isinstance(row, dict)
            ]
        try:
            start_at = (
                datetime.fromisoformat(manifest["started_at"])
                + timedelta(milliseconds=segment["start_offset_ms"])
            ).isoformat()
        except (KeyError, TypeError, ValueError):
            start_at = None
        error = state.get("error") if status == "failed" and valid_state else None
        previews.append({
            "sequence": segment["sequence"],
            "start_at": start_at,
            "duration_ms": segment.get("duration_ms"),
            "updated_at": state.get("updated_at") if valid_state else None,
            "status": status,
            "error": str(error)[:240] if error else None,
            "rows": rows,
        })
    latest = previews[-8:]
    rows = [row for segment in latest for row in segment["rows"]][-8:]
    return {
        "completed_segments": counts["completed"],
        "no_speech_segments": counts["no_speech"],
        "failed_segments": counts["failed"],
        "processing_segments": counts["processing"],
        "pending_segments": counts["pending"],
        "processing": counts["processing"] > 0,
        "segments": latest,
        "rows": rows,
        "provisional": True,
    }
