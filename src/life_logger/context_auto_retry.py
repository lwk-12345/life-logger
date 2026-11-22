"""Read-only eligibility checks for safe contextual-extraction auto-retries."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from life_logger.context_extraction_recovery import find_context_recovery_targets

_FILE_ID = re.compile(r"^[a-fA-F0-9]{32,128}$")
_ALLOWED_FAILURE_KINDS = {"http_error", "timeout", "connection_error"}


def failure_is_retryable(failure_kind: Any, http_status: Any) -> bool:
    """Single source of truth for transient-failure retryability.

    The serialized ``retryable_transient`` field is only a projection of this
    function.  Never let a stale/contradictory boolean make a 401 or a schema
    failure retryable, and never let ``False`` suppress a genuine timeout.
    """
    if isinstance(http_status, bool):
        return False
    kind = str(failure_kind or "")
    if kind == "http_error":
        return (
            isinstance(http_status, int)
            and (http_status in {408, 429} or 500 <= http_status <= 599)
        )
    if kind in {"timeout", "connection_error"}:
        return http_status is None
    return False


#: legacy 产物只有布尔 `retryable`、没有 failure_kind/http_status 时给出的告警文案。
LEGACY_RETRYABLE_WARNING = (
    "legacy retryable=true 缺少 failure_kind/http_status，证据不足，按 fail-closed 视为不可重试"
)


def legacy_retryable_is_trusted(value: Any) -> bool:
    """legacy 布尔本身**永远不构成重试依据**（Q165）。

    旧产物可能只写 `retryable: true`，没有任何可解释证据（failure_kind / http_status）。
    盲信它会把 401/402 甚至 schema 失败也变成"可重试"，从而反复烧模型。
    因此这里恒为 False；需要重试时必须有结构化元数据走 `failure_is_retryable()`。
    """
    return False


def legacy_retryable_warning(value: Any) -> str | None:
    """只有 `retryable=true` 时才需要告警；`false` 与 fail-closed 一致，无需提示。"""
    return LEGACY_RETRYABLE_WARNING if bool(value) else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_job_regular_file(root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        if root != resolved and root not in resolved.parents:
            return False
        current = path
        while current != root:
            if current.is_symlink():
                return False
            current = current.parent
        return path.is_file()
    except OSError:
        return False


def _failure_kinds(report: Any) -> list[str] | None:
    if not isinstance(report, dict) or report.get("status") != "failed":
        return None
    batches = report.get("batches")
    if not isinstance(batches, list) or not batches:
        return None
    kinds: list[str] = []
    for batch in batches:
        if not isinstance(batch, dict) or batch.get("status") != "failed":
            return None
        kind = batch.get("failure_kind")
        if (
            not isinstance(kind, str)
            or kind not in _ALLOWED_FAILURE_KINDS
            or not isinstance(batch.get("retryable_transient"), bool)
        ):
            # The flag must exist as evidence, but its value is never the
            # authority (legacy artifacts contain contradictory booleans).
            return None
        http_status = batch.get("http_status")
        # ``failure_is_retryable`` is authoritative; a stale ``False`` cannot
        # suppress a genuine timeout, and a stale ``True`` cannot retry a 401.
        if not failure_is_retryable(kind, http_status):
            return None
        kinds.append(kind)
    return kinds


def inspect_retry_candidate(job_dir: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    """Return sanitized retry evidence only for wholly transient failed extraction.

    This function performs filesystem reads only. It never reads transcript text
    into its return value and never starts a recovery or model request.
    """
    if not isinstance(state, dict) or state.get("status") != "completed":
        return None
    job_id = state.get("job_id")
    files = state.get("files")
    if not isinstance(job_id, str) or not job_id.strip() or not isinstance(files, list) or not files:
        return None

    root = Path(job_dir).resolve()
    failed_ids: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            return None
        file_id = str(item.get("sha256") or "")
        if not _FILE_ID.fullmatch(file_id):
            return None
        extraction_path = root / "files" / file_id / "result" / "context-extraction.json"
        try:
            if not _inside_job_regular_file(root, extraction_path):
                return None
            report = json.loads(extraction_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(report, dict):
            return None
        status = report.get("status")
        if status == "completed":
            continue
        if status != "failed":
            # Includes degraded, in-progress, partial, and unknown statuses.
            return None
        if _failure_kinds(report) is None:
            return None
        failed_ids.append(file_id)

    if not failed_ids:
        return None

    targets, skipped = find_context_recovery_targets(root, state)
    target_by_id = {str(target.get("file_id") or ""): target for target in targets}
    if skipped or len(target_by_id) != len(targets) or set(target_by_id) != set(failed_ids):
        return None

    candidate_rows = []
    all_kinds: list[str] = []
    try:
        for file_id in sorted(failed_ids):
            target = target_by_id[file_id]
            extraction_path = Path(target["result_dir"]) / "context-extraction.json"
            if not _inside_job_regular_file(root, extraction_path):
                return None
            report_bytes_hash = _sha256(extraction_path)
            kinds = _failure_kinds(json.loads(extraction_path.read_text(encoding="utf-8")))
            if kinds is None:
                return None
            asr_path = Path(target["transcript_path"])
            recordings_path = Path(target["recordings_path"])
            attribution_path = Path(target["attribution_path"])
            if not all(
                _inside_job_regular_file(root, path)
                for path in (asr_path, recordings_path, attribution_path)
            ):
                return None
            checkpoint_hashes = {
                "asr_transcript_sha256": _sha256(asr_path),
                "asr_recordings_sha256": _sha256(recordings_path),
                "attribution_sha256": _sha256(attribution_path),
            }
            candidate_rows.append({
                "file_id": file_id,
                "report_sha256": report_bytes_hash,
                **checkpoint_hashes,
            })
            all_kinds.extend(kinds)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None

    fingerprint_payload = {
        "job_id": job_id,
        "files": candidate_rows,
    }
    encoded = json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "file_ids": [row["file_id"] for row in candidate_rows],
        "input_fingerprint": hashlib.sha256(encoded).hexdigest(),
        "failure_kinds": all_kinds,
    }
