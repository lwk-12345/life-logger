"""Safe same-job retry of contextual extraction from durable checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

RETRYABLE_STATUSES = {"degraded", "failed"}
_BATCH_PROGRESS = re.compile(r"^context batch (\d+)/(\d+) (running|completed|failed)$")
_FILE_ID = re.compile(r"^[a-fA-F0-9]{32,128}$")
_RECOVERY_ID = re.compile(r"^[a-fA-F0-9]{32}$")


def _job_path(job_dir: Path, relative: str) -> Path:
    root = Path(job_dir).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("recovery path outside job")
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_signature(root: Path) -> tuple[tuple[str, str, int, int, str], ...]:
    """Fingerprint names, types, metadata, and bytes to detect same-size edits."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("result tree is not a regular directory")
    entries: list[tuple[str, str, int, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("result tree contains a symlink")
        before = path.lstat()
        relative = str(path.relative_to(root))
        if stat.S_ISDIR(before.st_mode):
            entries.append((relative, "dir", 0, before.st_mtime_ns, ""))
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("result tree contains a non-regular file")
        digest = _sha256(path)
        after = path.lstat()
        if (
            before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError("result file changed while it was fingerprinted")
        entries.append((relative, "file", after.st_size, after.st_mtime_ns, digest))
    return tuple(entries)


def _signature_payload(
    signature: tuple[tuple[str, str, int, int, str], ...],
) -> list[list[Any]]:
    return [list(entry) for entry in signature]


def _signature_matches(path: Path, expected: Any) -> bool:
    if not isinstance(expected, (list, tuple)):
        return False
    try:
        return _signature_payload(_tree_signature(path)) == [
            list(entry) for entry in expected
        ]
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def _checkpoint_rows(path: Path) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list) or not all(
        isinstance(row, dict) for row in payload
    ):
        return None
    return payload


def _checkpoint_ids(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        segment_id = str(row.get("segment_id") or "")
        if not segment_id or segment_id in indexed:
            return None
        indexed[segment_id] = row
    return indexed


def _checkpoints_match(
    transcript_path: Path,
    recordings_path: Path,
    attribution_path: Path,
) -> bool:
    transcript = _checkpoint_rows(transcript_path)
    recordings = _checkpoint_rows(recordings_path)
    attribution = _checkpoint_rows(attribution_path)
    if not transcript or not recordings or not attribution:
        return False
    asr_by_id = _checkpoint_ids(transcript)
    attribution_by_id = _checkpoint_ids(attribution)
    if (
        asr_by_id is None
        or attribution_by_id is None
        or set(asr_by_id) != set(attribution_by_id)
    ):
        return False
    recording_ids = {
        str(recording.get("recording_id") or "")
        for recording in recordings
        if recording.get("recording_id")
    }
    if not recording_ids:
        return False
    identity_fields = (
        "recording_id",
        "start_offset_seconds",
        "end_offset_seconds",
        "absolute_started_at",
        "absolute_ended_at",
        "text",
    )
    for segment_id, asr_row in asr_by_id.items():
        attributed_row = attribution_by_id[segment_id]
        if str(asr_row.get("recording_id") or "") not in recording_ids:
            return False
        if any(
            asr_row.get(field) != attributed_row.get(field) for field in identity_fields
        ):
            return False
        if attributed_row.get("source_label") not in {"self", "non_self", "unknown"}:
            return False
    return True


def cleanup_context_recovery_staging(
    job_dir: Path, recovery_id: str
) -> tuple[int, int]:
    """Remove only this recovery attempt's private, unpublished staging trees."""
    if not _RECOVERY_ID.fullmatch(str(recovery_id)):
        return 0, 0
    root = Path(job_dir).resolve()
    files_root = root / "files"
    if not files_root.is_dir() or files_root.is_symlink():
        return 0, 0
    removed = 0
    failed = 0
    for candidate in files_root.glob(f"*/.result.context-recovery-{recovery_id}"):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if files_root.resolve() not in resolved.parents:
            continue
        try:
            shutil.rmtree(resolved)
            removed += 1
        except OSError:
            failed += 1
    return removed, failed


def find_context_recovery_targets(
    job_dir: Path,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Find only files with retryable extraction output and matching checkpoints."""
    root = Path(job_dir).resolve()
    files = state.get("files")
    if not isinstance(files, list):
        return [], 0
    targets: list[dict[str, Any]] = []
    skipped = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("sha256") or "")
        if not _FILE_ID.fullmatch(file_id):
            continue
        file_root = root / "files" / file_id
        result_dir = file_root / "result"
        extraction_path = result_dir / "context-extraction.json"
        if not extraction_path.is_file():
            continue
        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(extraction, dict)
            or extraction.get("status") not in RETRYABLE_STATUSES
        ):
            continue
        attribution_path = result_dir / "source-attribution.json"
        transcript_matches = sorted((file_root / "asr").rglob("transcript.json"))
        recording_matches = sorted((file_root / "asr").rglob("recordings.json"))
        if (
            not result_dir.is_dir()
            or result_dir.is_symlink()
            or file_root.is_symlink()
            or not attribution_path.is_file()
            or not transcript_matches
            or not recording_matches
        ):
            skipped += 1
            continue
        checkpoint_dirs = sorted(
            {
                transcript.parent
                for transcript in transcript_matches
                if any(
                    recording.parent == transcript.parent
                    for recording in recording_matches
                )
            }
        )
        if len(checkpoint_dirs) != 1:
            skipped += 1
            continue
        checkpoint_dir = checkpoint_dirs[0]
        transcript_path = checkpoint_dir / "transcript.json"
        recordings_path = checkpoint_dir / "recordings.json"
        try:
            for path in (
                result_dir,
                attribution_path,
                transcript_path,
                recordings_path,
            ):
                resolved = path.resolve()
                if resolved != root and root not in resolved.parents:
                    raise ValueError("recovery input outside job")
            if any(path.is_symlink() for path in result_dir.rglob("*")):
                skipped += 1
                continue
            if not _checkpoints_match(
                transcript_path, recordings_path, attribution_path
            ):
                skipped += 1
                continue
            targets.append(
                {
                    "file_id": file_id,
                    "result_dir": result_dir,
                    "attribution_path": attribution_path,
                    "transcript_path": transcript_path,
                    "recordings_path": recordings_path,
                    "extraction_status": extraction["status"],
                }
            )
        except (OSError, ValueError):
            skipped += 1
    return targets, skipped


# 上下文抽取子进程的硬超时与看门狗：该进程 stdout 逐行读取，
# 若只挂死而不产出任何输出，读循环会永久阻塞，故用定时器强杀。
_CONTEXT_EXTRACTION_TIMEOUT_SECONDS = 3600


def _kill_process_quietly(process: subprocess.Popen) -> None:
    """超时看门狗用：强杀子进程；进程已退出时忽略。"""
    try:
        process.kill()
    except Exception:
        pass


def prepare_context_recovery_target(
    *,
    job_dir: Path,
    target: dict[str, Any],
    recovery_id: str,
    config: dict[str, Any],
    env: dict[str, str],
    timezone_name: str,
    dictation_windows: Path | None,
    progress_callback: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Run extraction in a sibling staging tree without touching published output."""
    root = Path(job_dir).resolve()
    result_dir = Path(target["result_dir"]).resolve()
    if root not in result_dir.parents or not result_dir.is_dir():
        raise ValueError("recovery result directory is unavailable")
    file_id = str(target["file_id"])
    if not _FILE_ID.fullmatch(file_id) or not _RECOVERY_ID.fullmatch(recovery_id):
        raise ValueError("recovery identity is invalid")
    staging_dir = result_dir.parent / f".result.context-recovery-{recovery_id}"
    if staging_dir.exists():
        raise ValueError("recovery staging path already exists")

    original_signature = _tree_signature(result_dir)
    input_hashes = {
        key: _sha256(Path(target[key]))
        for key in ("attribution_path", "transcript_path", "recordings_path")
    }
    try:
        shutil.copytree(result_dir, staging_dir, copy_function=shutil.copy2)
        staged_attribution = staging_dir / "source-attribution.json"
        command = [
            str(config.get("python") or sys.executable),
            str(
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "attribute_funasr_transcript.py"
            ),
            "--context-only",
            "--deepseek-enabled",
            "--transcript",
            str(target["transcript_path"]),
            "--recordings",
            str(target["recordings_path"]),
            "--source-attribution",
            str(staged_attribution),
            "--context-source-namespace",
            str(result_dir),
            "--output-dir",
            str(staging_dir),
            "--timezone",
            str(timezone_name or "Asia/Shanghai"),
        ]
        if dictation_windows is not None and Path(dictation_windows).is_file():
            command.extend(["--dictation-windows", str(dictation_windows)])
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        timed_out: list[bool] = []

        def _kill_on_timeout() -> None:
            timed_out.append(True)
            _kill_process_quietly(process)

        watchdog = threading.Timer(_CONTEXT_EXTRACTION_TIMEOUT_SECONDS, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in process.stdout:
                match = _BATCH_PROGRESS.match(line.strip())
                if match:
                    progress_callback(
                        {
                            "batch": int(match.group(1)),
                            "total_batches": int(match.group(2)),
                            "status": match.group(3),
                        }
                    )
            return_code = process.wait()
        finally:
            watchdog.cancel()
        if return_code != 0:
            if timed_out:
                raise RuntimeError(
                    f"context extraction 超时（>{_CONTEXT_EXTRACTION_TIMEOUT_SECONDS}s），已终止子进程"
                )
            raise RuntimeError("context extraction command failed")

        extraction_path = staging_dir / "context-extraction.json"
        try:
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "context extraction result is missing or invalid"
            ) from error
        if not isinstance(extraction, dict) or extraction.get("status") not in {
            "completed",
            "degraded",
        }:
            raise RuntimeError("context extraction did not produce publishable output")
        if _sha256(staged_attribution) != input_hashes["attribution_path"]:
            raise RuntimeError("source-attribution checkpoint changed during retry")
        for key in ("transcript_path", "recordings_path"):
            if _sha256(Path(target[key])) != input_hashes[key]:
                raise RuntimeError("ASR checkpoint changed during retry")
        if _tree_signature(result_dir) != original_signature:
            raise RuntimeError(
                "published results changed during retry; refusing to replace them"
            )
        staged_signature = _tree_signature(staging_dir)

        archive_dir = (
            root / "recovery" / "context-extraction" / recovery_id / file_id / "result"
        )
        old_artifacts = []
        for path in sorted(result_dir.rglob("*")):
            if path.is_file() and path.name in {
                "context-extraction.json",
                "events.json",
                "daily_summary.json",
                "daily.md",
            }:
                old_artifacts.append(
                    str((archive_dir / path.relative_to(result_dir)).relative_to(root))
                )
        return {
            "file_id": file_id,
            "recovery_id": recovery_id,
            "result_dir": result_dir,
            "staging_dir": staging_dir,
            "archive_dir": archive_dir,
            "extraction_status": extraction["status"],
            "old_artifacts": old_artifacts,
            "input_hashes": input_hashes,
            "original_signature": original_signature,
            "staged_signature": staged_signature,
        }
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def publish_context_recovery_target(
    job_dir: Path,
    prepared: dict[str, Any],
) -> list[str]:
    """Atomically swap one result directory, retaining the prior tree in recovery/."""
    root = Path(job_dir).resolve()
    result_dir = Path(prepared["result_dir"]).resolve()
    staging_dir = Path(prepared["staging_dir"]).resolve()
    archive_dir = Path(prepared["archive_dir"]).resolve()
    file_id = str(prepared.get("file_id") or "")
    recovery_id = str(prepared.get("recovery_id") or "")
    if not _FILE_ID.fullmatch(file_id) or not _RECOVERY_ID.fullmatch(recovery_id):
        raise ValueError("recovery publish identity is invalid")
    expected_result = root / "files" / file_id / "result"
    expected_staging = (
        root / "files" / file_id / f".result.context-recovery-{recovery_id}"
    )
    expected_archive = (
        root / "recovery" / "context-extraction" / recovery_id / file_id / "result"
    )
    if (result_dir, staging_dir, archive_dir) != (
        expected_result.resolve(),
        expected_staging.resolve(),
        expected_archive.resolve(),
    ):
        raise ValueError("recovery publish path does not match its identity")
    if _tree_signature(result_dir) != prepared["original_signature"]:
        raise RuntimeError("published results changed before recovery publish")
    if (
        archive_dir.exists()
        or not staging_dir.is_dir()
        or _tree_signature(staging_dir) != prepared["staged_signature"]
    ):
        raise RuntimeError("recovery publish paths are not ready")
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(result_dir, archive_dir)
    try:
        os.replace(staging_dir, result_dir)
    except Exception:
        os.replace(archive_dir, result_dir)
        raise
    return prepared["old_artifacts"]


def recover_context_recovery_publication(
    job_dir: Path,
    publication: dict[str, Any],
) -> tuple[str, list[str]]:
    """Finish or roll back a directory swap interrupted by a service restart."""
    root = Path(job_dir).resolve()
    file_id = str(publication.get("file_id") or "")
    recovery_id = str(publication.get("recovery_id") or "")
    if not _FILE_ID.fullmatch(file_id) or not _RECOVERY_ID.fullmatch(recovery_id):
        return "unsafe", []
    result_dir = root / "files" / file_id / "result"
    try:
        archive_dir = _job_path(root, str(publication.get("archive_path") or ""))
        staging_dir = _job_path(root, str(publication.get("staging_path") or ""))
    except ValueError:
        return "unsafe", []
    expected_archive = (
        root / "recovery" / "context-extraction" / recovery_id / file_id / "result"
    )
    expected_staging = (
        root / "files" / file_id / f".result.context-recovery-{recovery_id}"
    )
    if (archive_dir, staging_dir) != (
        expected_archive.resolve(),
        expected_staging.resolve(),
    ):
        return "unsafe", []
    if any(path.is_symlink() for path in (result_dir, archive_dir, staging_dir)):
        return "unsafe", []
    original_signature = publication.get("original_signature")
    staged_signature = publication.get("staged_signature")
    if archive_dir.exists():
        if not _signature_matches(archive_dir, original_signature):
            outcome = "unsafe"
        elif result_dir.exists():
            outcome = (
                "committed"
                if _signature_matches(result_dir, staged_signature)
                else "unsafe"
            )
        elif staging_dir.exists() and _signature_matches(staging_dir, staged_signature):
            os.replace(staging_dir, result_dir)
            outcome = "committed"
        else:
            os.replace(archive_dir, result_dir)
            outcome = "restored"
    else:
        if _signature_matches(result_dir, original_signature):
            outcome = "unchanged"
        elif result_dir.exists():
            outcome = "unsafe"
        else:
            outcome = "missing"
    archived = publication.get("old_artifacts")
    return outcome, (
        [path for path in archived if isinstance(path, str)]
        if isinstance(archived, list)
        else []
    )
