"""JobStore 的方法，从 local_web.py 机械搬出，方法体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

from life_logger.domain import ALLOWED_CATEGORIES
from life_logger.ingest import AUDIO_EXTENSIONS
from life_logger.timeutil import LOCAL_TZ
from life_logger.timeutil import local_date as business_date
from life_logger.watch_stream import WATCH_CAPTURE_SOURCE
from life_logger.web_common import _SEARCH_CATEGORY_ALIASES
from life_logger.web_common import _atomic_json
from life_logger.web_search import _canonical_search_category
from life_logger.web_search import _search_result_seconds
from life_logger.web_search import _semantic_search_event_duplicate
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
import json
import threading
import uuid


class _JobRunnerMixin:
    def scan_auto_ingest(
        self,
        config_path: Path,
        *,
        now: float | None = None,
        secrets_path: Path | None = None,
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _hash_path,
            _iso,
            _now,
        )

        timestamp = _now() if now is None else float(now)
        with self._lock:
            state = self._read_auto_ingest_unlocked()
            state["last_scan_at"] = _iso(timestamp)
            state["last_scan_started_at"] = _iso(timestamp)
            state["last_scan_duration_seconds"] = None
            state["scan_state"] = "scanning"
            state["last_discovered_count"] = 0
            state["last_error"] = None
            state["last_import_count"] = 0
            _atomic_json(self.auto_ingest_path(), state)
            if not state.get("enabled"):
                state["scan_state"] = "paused"
                state["last_scan_duration_seconds"] = 0.0
                _atomic_json(self.auto_ingest_path(), state)
                return self.auto_ingest_status()
            if self._auto_ingest_active_unlocked(state):
                state["scan_state"] = "job_running"
                state["last_scan_duration_seconds"] = 0.0
                _atomic_json(self.auto_ingest_path(), state)
                return self.auto_ingest_status()

            stability_seconds = int(state.get("stability_seconds") or 20)
            candidates = state.setdefault("candidates", {})
            processed = state.setdefault("processed", {})
            discovered_paths: set[str] = set()
            ready: list[tuple[Path, Path]] = []
            try:
                for raw_folder in state.get("folders", []):
                    folder = Path(str(raw_folder)).expanduser().resolve()
                    if not folder.is_dir():
                        continue
                    file_count = 0
                    for path in folder.rglob("*"):
                        if file_count >= 5000:
                            break
                        if (
                            not path.is_file()
                            or path.suffix.lower() not in AUDIO_EXTENSIONS
                        ):
                            continue
                        file_count += 1
                        try:
                            stat = path.stat()
                        except OSError:
                            continue
                        if stat.st_size <= 0:
                            continue
                        absolute = str(path.resolve())
                        discovered_paths.add(absolute)
                        fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
                        already_processed = any(
                            isinstance(item, dict)
                            and item.get("source_path") == absolute
                            and item.get("fingerprint") == fingerprint
                            for item in processed.values()
                        )
                        if already_processed:
                            candidates.pop(absolute, None)
                            continue
                        existing = candidates.get(absolute)
                        if (
                            not isinstance(existing, dict)
                            or existing.get("fingerprint") != fingerprint
                        ):
                            candidates[absolute] = {
                                "fingerprint": fingerprint,
                                "size_bytes": stat.st_size,
                                "mtime": stat.st_mtime,
                                "stable_since": timestamp,
                                "folder": str(folder),
                            }
                            continue
                        stable_since = float(existing.get("stable_since") or timestamp)
                        if timestamp - stable_since >= stability_seconds:
                            ready.append((path, folder))

                state["last_discovered_count"] = len(discovered_paths)
                for path in list(candidates):
                    if path not in discovered_paths and not Path(path).exists():
                        candidates.pop(path, None)

                importable: list[tuple[Path, Path, str, int]] = []
                for path, folder in ready:
                    try:
                        digest, size = _hash_path(path)
                    except OSError:
                        continue
                    if digest in processed:
                        candidates.pop(str(path.resolve()), None)
                        continue
                    importable.append((path, folder, digest, size))

                if not importable:
                    missing = [
                        str(Path(str(raw)).expanduser())
                        for raw in state.get("folders", [])
                        if not Path(str(raw)).expanduser().is_dir()
                    ]
                    state["scan_state"] = "folder_missing" if missing else (
                        "waiting" if candidates else "idle"
                    )
                    state["last_scan_duration_seconds"] = round(
                        max(0.0, (_now() if now is None else timestamp) - timestamp),
                        3,
                    )
                    _atomic_json(self.auto_ingest_path(), state)
                    return self.auto_ingest_status()

                job_id = self.create_job(timezone_name="Asia/Shanghai")
                imported = 0
                imported_files: list[str] = []
                for path, folder, digest, size in importable:
                    try:
                        relative = str(path.resolve().relative_to(folder))
                    except ValueError:
                        relative = path.name
                    with path.open("rb") as stream:
                        result = self.add_file(
                            job_id,
                            filename=path.name,
                            relative_path=relative,
                            stream=stream,
                            modified_at=path.stat().st_mtime,
                            max_bytes=size,
                        )
                    file_state = result.get("file", {})
                    actual_digest = str(file_state.get("sha256") or digest)
                    candidate = candidates.get(str(path.resolve()), {})
                    processed[actual_digest] = {
                        "source_path": str(path.resolve()),
                        "fingerprint": (
                            candidate.get("fingerprint")
                            if isinstance(candidate, dict)
                            else None
                        ),
                        "job_id": job_id,
                        "processed_at": _iso(timestamp),
                    }
                    candidates.pop(str(path.resolve()), None)
                    imported += 1
                    imported_files.append(relative)
                state["last_job_id"] = job_id
                state["last_import_count"] = imported
                if imported:
                    self._append_auto_ingest_history_unlocked(
                        state,
                        {
                            "kind": "import",
                            "at": _iso(timestamp),
                            "job_id": job_id,
                            "count": imported,
                            "files": imported_files[:20],
                        },
                    )
                state["scan_state"] = "imported" if imported else "idle"
                state["last_scan_duration_seconds"] = round(
                    max(0.0, (_now() if now is None else timestamp) - timestamp),
                    3,
                )
                _atomic_json(self.auto_ingest_path(), state)
                if imported:
                    self.start_job(
                        job_id,
                        Path(config_path),
                        secrets_path=secrets_path,
                    )
                return self.auto_ingest_status()
            except Exception as error:
                state["scan_state"] = "error"
                state["last_scan_duration_seconds"] = round(
                    max(0.0, (_now() if now is None else timestamp) - timestamp),
                    3,
                )
                state["last_error"] = str(error)
                self._append_auto_ingest_history_unlocked(
                    state,
                    {
                        "kind": "error",
                        "at": _iso(timestamp),
                        "error": str(error),
                    },
                )
                _atomic_json(self.auto_ingest_path(), state)
                return self.auto_ingest_status()

    def read(self, job_id: str) -> dict[str, Any]:
        from life_logger.local_web import (
            _now,
        )

        # [2026-10-22 P0] 纯读：status.json 由 `_atomic_json` 原子替换，
        # 不需要拿全局锁；否则会跟后台写入者串行化（见 read_task_states 注释）。
        if True:
            state = self._read_unlocked(job_id)
            result = dict(state)
            elapsed = float(result.get("elapsed_seconds", 0.0))
            if result.get("status") == "running" and result.get("started_at"):
                elapsed += max(0.0, _now() - float(result["started_at"]))
            result["elapsed_seconds"] = round(elapsed, 2)
            result["journal_revision"] = self._journal_revision_unlocked(
                job_id,
                state,
            )
            return result

    def search(
        self,
        query: str,
        *,
        result_type: str = "all",
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
        amount_min: float | None = None,
        amount_max: float | None = None,
        import_source: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        from life_logger.local_web import (
            _cached_job_states,
            _job_utterance_items,
            _meaningful_event_payload,
            datetime,
        )

        query = str(query or "").strip()
        if result_type not in {"all", "utterance", "event"}:
            raise ValueError("搜索类型无效")
        if import_source not in {
            "all",
            "apple-watch",
            "ai-passport-wifi",
            "file-upload",
        }:
            raise ValueError("导入来源无效")

        def normalized_date(value: str | None, label: str) -> str | None:
            if value is None:
                return None
            value = str(value).strip()
            if not value:
                return None
            if (
                len(value) != 10
                or value[4] != "-"
                or value[7] != "-"
                or not value.replace("-", "").isdigit()
            ):
                raise ValueError(f"{label}格式无效")
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as error:
                raise ValueError(f"{label}格式无效") from error
            return value

        date = normalized_date(date, "搜索日期")
        date_from = normalized_date(date_from, "开始日期")
        date_to = normalized_date(date_to, "结束日期")
        if date:
            date_from = date
            date_to = date
        if date_from and date_to and date_from > date_to:
            raise ValueError("搜索开始日期不能晚于结束日期")
        category = str(category or "").strip() or None
        valid_search_categories = set(ALLOWED_CATEGORIES) | set(
            _SEARCH_CATEGORY_ALIASES
        )
        if category and category not in valid_search_categories:
            raise ValueError("搜索分类无效")
        canonical_category = _canonical_search_category(category) if category else None
        def normalized_amount(value: float | None, label: str) -> float | None:
            if value is None or value == "":
                return None
            try:
                numeric = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{label}无效") from error
            if numeric < 0:
                raise ValueError(f"{label}不能小于 0")
            return numeric

        amount_min = normalized_amount(amount_min, "最低金额")
        amount_max = normalized_amount(amount_max, "最高金额")
        if (
            amount_min is not None
            and amount_max is not None
            and amount_min > amount_max
        ):
            raise ValueError("最低金额不能高于最高金额")
        amount_filter = amount_min is not None or amount_max is not None
        if (
            not query
            and not any((date_from, date_to, category, amount_filter))
            and import_source == "all"
        ):
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError) as error:
            raise ValueError("搜索数量无效") from error
        limit = max(1, min(limit, 100))
        needle = query.casefold()

        def matches(*values: Any) -> bool:
            if not needle:
                return True
            return needle in " ".join(str(value or "") for value in values).casefold()

        def date_matches(value: str | None) -> bool:
            if not date_from and not date_to:
                return True
            candidate = business_date(value)
            if not candidate:
                return False
            if date_from and candidate < date_from:
                return False
            if date_to and candidate > date_to:
                return False
            return True

        def event_time(item: dict[str, Any]) -> str:
            time_info = item.get("time")
            if isinstance(time_info, dict):
                return str(time_info.get("absolute") or "")
            return ""

        def event_amount(item: dict[str, Any]) -> float | None:
            data = item.get("data")
            if not isinstance(data, dict):
                return None
            expense = data.get("expense")
            if not isinstance(expense, dict):
                return None
            amount = expense.get("amount")
            if not isinstance(amount, (int, float)):
                return None
            return float(amount)

        def amount_matches(amount: float | None) -> bool:
            if not amount_filter:
                return True
            if amount is None:
                return False
            if amount_min is not None and amount < amount_min:
                return False
            if amount_max is not None and amount > amount_max:
                return False
            return True

        results: list[dict[str, Any]] = []
        # 【2026-10-19 P-9】不再持 JobStore 锁、不再每次重读 170+ status.json 与 400+ transcript。
        if True:
            for state in _cached_job_states(self):
                job_id = str(state.get("job_id") or "")
                if not job_id:
                    continue
                job_dir = self.job_path(job_id)
                files_by_id = {
                    str(item.get("file_id") or item.get("sha256") or ""): item
                    for item in state.get("files", [])
                    if isinstance(item, dict)
                }

                def source_for(file_state: dict[str, Any]) -> str:
                    source = file_state.get("capture_source")
                    return (
                        source
                        if source in {"apple-watch", "ai-passport-wifi"}
                        else "file-upload"
                    )

                def source_matches(file_state: dict[str, Any]) -> bool:
                    return (
                        import_source == "all"
                        or source_for(file_state) == import_source
                    )

                if (
                    result_type in {"all", "utterance"}
                    and category is None
                    and not amount_filter
                ):
                    for item in _job_utterance_items(job_dir, job_id, state):
                        if import_source != "all" and item["import_source"] != import_source:
                            continue
                        if not matches(item["text"]):
                            continue
                        if not date_matches(item["timestamp"]):
                            continue
                        results.append(dict(item))

                if result_type in {"all", "event"}:
                    for path in job_dir.glob("files/*/result/*/events.json"):
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            continue
                        if not isinstance(payload, list):
                            continue
                        file_id = path.parents[2].name
                        file_state = files_by_id.get(file_id, {})
                        if not source_matches(file_state):
                            continue
                        artifact_path = str(path.relative_to(job_dir))
                        payload = self._apply_event_corrections_unlocked(
                            job_id, artifact_path, payload
                        )
                        source_file = str(
                            file_state.get("relative_path")
                            or file_state.get("original_name")
                            or ""
                        )
                        for item in payload:
                            if not isinstance(item, dict):
                                continue
                            if not _meaningful_event_payload(item):
                                continue
                            title = str(item.get("title") or "").strip()
                            evidence = item.get("evidence")
                            quote = (
                                str(evidence.get("quote") or "")
                                if isinstance(evidence, dict)
                                else ""
                            )
                            categories = [
                                str(value)
                                for value in item.get("categories", [])
                                if value
                            ]
                            if canonical_category and not any(
                                _canonical_search_category(value) == canonical_category
                                for value in categories
                            ):
                                continue
                            timestamp = event_time(item)
                            amount = event_amount(item)
                            if not amount_matches(amount):
                                continue
                            if not title or not matches(title, quote, *categories):
                                continue
                            if not date_matches(timestamp or path.parent.name):
                                continue
                            time_dict = item.get("time") if isinstance(item.get("time"), dict) else {}
                            evidence_dict = evidence if isinstance(evidence, dict) else {}
                            results.append(
                                {
                                    "result_type": "event",
                                    "job_id": job_id,
                                    "artifact_path": artifact_path,
                                    "date": business_date(timestamp)
                                    if timestamp
                                    else path.parent.name,
                                    "timestamp": timestamp or None,
                                    "text": title,
                                    "quote": quote or None,
                                    "source_file": source_file,
                                    "import_source": source_for(file_state),
                                    "categories": categories,
                                    "source_type": item.get("source_type"),
                                    "review_status": item.get("review_status"),
                                    "event_id": item.get("event_id"),
                                    "expense_amount_cny": amount,
                                    "audio_start_offset_seconds": evidence_dict.get("audio_start_offset_seconds"),
                                    "audio_end_offset_seconds": evidence_dict.get("audio_end_offset_seconds"),
                                    "relative_seconds": time_dict.get("relative_seconds"),
                                }
                            )

                    for item in self.read_manual_events(job_id):
                        title = str(item.get("title") or "").strip()
                        evidence = item.get("evidence")
                        quote = (
                            str(evidence.get("quote") or evidence.get("note") or "")
                            if isinstance(evidence, dict)
                            else ""
                        )
                        categories = [
                            str(value) for value in item.get("categories", []) if value
                        ]
                        if canonical_category and not any(
                            _canonical_search_category(value) == canonical_category
                            for value in categories
                        ):
                            continue
                        timestamp = event_time(item)
                        amount = event_amount(item)
                        if not amount_matches(amount):
                            continue
                        if not title or not matches(title, quote, *categories):
                            continue
                        if not date_matches(timestamp):
                            continue
                        artifact_path = str(item.get("artifact_path") or "")
                        parts = PurePosixPath(artifact_path).parts
                        file_state = (
                            files_by_id.get(parts[1], {})
                            if len(parts) > 2 and parts[0] == "files"
                            else {}
                        )
                        if not source_matches(file_state):
                            continue
                        results.append(
                            {
                                "result_type": "event",
                                "job_id": job_id,
                                "artifact_path": artifact_path,
                                "date": business_date(timestamp) if timestamp else None,
                                "timestamp": timestamp or None,
                                "text": title,
                                "quote": quote or None,
                                "source_file": str(
                                    file_state.get("relative_path")
                                    or file_state.get("original_name")
                                    or ""
                                ),
                                "import_source": source_for(file_state),
                                "categories": categories,
                                "source_type": item.get("source_type") or "manual",
                                "review_status": item.get("review_status")
                                or "confirmed",
                                "event_id": item.get("event_id"),
                                "expense_amount_cny": amount,
                                "audio_start_offset_seconds": evidence.get("audio_start_offset_seconds") if isinstance(evidence, dict) else None,
                                "audio_end_offset_seconds": evidence.get("audio_end_offset_seconds") if isinstance(evidence, dict) else None,
                                "relative_seconds": item.get("time", {}).get("relative_seconds") if isinstance(item.get("time"), dict) else None,
                            }
                        )

        # 实时直连 Watch 会话中的流式生活事件 (无须等待全天 Job，且自动跳过已归档入 Job 的重复 event_id)
        if result_type in {"all", "event"}:
            existing_event_ids = {
                str(r.get("event_id") or "")
                for r in results
                if r.get("event_id")
            }
            watch_sessions_root = self.root / "watch-sessions"
            if watch_sessions_root.exists():
                for w_event_file in watch_sessions_root.glob("*/*/events.json"):
                    try:
                        w_payload = json.loads(w_event_file.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if not isinstance(w_payload, list):
                        continue
                    sess_name = w_event_file.parent.name
                    dev_name = w_event_file.parents[1].name
                    for item in w_payload:
                        if not isinstance(item, dict) or not _meaningful_event_payload(item):
                            continue
                        ev_id = str(item.get("event_id") or "")
                        if ev_id and ev_id in existing_event_ids:
                            continue
                        title = str(item.get("title") or "").strip()
                        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
                        quote = str(evidence.get("quote") or "")
                        categories = [str(val) for val in item.get("categories", []) if val]
                        timestamp = event_time(item)
                        amount = event_amount(item)
                        if not amount_matches(amount) or not matches(title, quote, *categories) or not date_matches(timestamp):
                            continue
                        results.append({
                            "result_type": "event",
                            "job_id": f"watch-{sess_name}",
                            "artifact_path": f"watch-sessions/{dev_name}/{sess_name}/events.json",
                            "date": business_date(timestamp) if timestamp else "",
                            "timestamp": timestamp or None,
                            "text": title,
                            "quote": quote or None,
                            "source_file": f"Apple Watch/{dev_name}/{sess_name}/recording.m4a",
                            "import_source": "apple-watch",
                            "categories": categories,
                            "source_type": item.get("source_type") or "audio",
                            "review_status": item.get("review_status") or "unreviewed",
                            "event_id": item.get("event_id"),
                            "expense_amount_cny": amount,
                            "audio_start_offset_seconds": evidence.get("audio_start_offset_seconds"),
                            "audio_end_offset_seconds": evidence.get("audio_end_offset_seconds"),
                            "relative_seconds": item.get("time", {}).get("relative_seconds") if isinstance(item.get("time"), dict) else None,
                        })

        unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in results:
            key = (
                str(item.get("result_type") or ""),
                str(item.get("job_id") or ""),
                str(item.get("artifact_path") or ""),
                str(
                    item.get("segment_id")
                    or item.get("event_id")
                    or f"{item.get('timestamp')}::{item.get('text')}"
                ),
            )
            unique[key] = item
        chronological = sorted(
            unique.values(),
            key=lambda item: (
                str(item.get("timestamp") or ""),
                str(item.get("date") or ""),
            ),
        )
        curated: list[dict[str, Any]] = []
        for item in chronological:
            if item.get("result_type") != "event":
                curated.append(item)
                continue
            seconds = _search_result_seconds(item)
            duplicate_index: int | None = None
            for index in range(len(curated) - 1, -1, -1):
                previous = curated[index]
                if previous.get("result_type") != "event":
                    continue
                if previous.get("job_id") != item.get("job_id"):
                    continue
                if previous.get("artifact_path") != item.get("artifact_path"):
                    continue
                previous_seconds = _search_result_seconds(previous)
                if seconds is not None and previous_seconds is not None:
                    delta = seconds - previous_seconds
                    if delta > 180:
                        break
                    if delta < 0:
                        continue
                if _semantic_search_event_duplicate(previous, item):
                    duplicate_index = index
                    break
            if duplicate_index is None:
                curated.append(item)
                continue
            previous = curated[duplicate_index]
            current_is_confirmed = (
                item.get("source_type") == "manual"
                or item.get("review_status") == "confirmed"
            )
            previous_is_confirmed = (
                previous.get("source_type") == "manual"
                or previous.get("review_status") == "confirmed"
            )
            if current_is_confirmed and not previous_is_confirmed:
                curated[duplicate_index] = item

        ordered = sorted(
            curated,
            key=lambda item: (
                str(item.get("timestamp") or ""),
                str(item.get("date") or ""),
            ),
            reverse=True,
        )
        return ordered[:limit]

    def _schedule_context_auto_retry(self, job_id: str) -> bool:
        """Enroll only newly completed Watch Jobs with wholly transient failures."""
        from life_logger.local_web import (
            _iso,
            _now,
        )

        from life_logger.context_auto_retry import inspect_retry_candidate

        with self._lock:
            state = self._read_unlocked(job_id)
            if state.get("status") != "completed" or "context_auto_retry" in state:
                return False
            files = state.get("files")
            if not isinstance(files, list) or not files or not all(
                isinstance(item, dict)
                and item.get("capture_source") == WATCH_CAPTURE_SOURCE
                and item.get("source_device")
                and item.get("source_session")
                for item in files
            ):
                return False
            candidate = inspect_retry_candidate(self.job_path(job_id), state)
            if candidate is None:
                return False
            target_prefixes = tuple(
                f"files/{file_id}/result/" for file_id in candidate["file_ids"]
            )
            corrections = self._read_event_corrections_unlocked(job_id)
            if any(
                isinstance(correction, dict)
                and str(correction.get("artifact_path") or "").startswith(
                    target_prefixes
                )
                for correction in corrections.values()
            ):
                return False
            now = _now()
            state["context_auto_retry"] = {
                "status": "scheduled",
                "attempts": 0,
                "max_attempts": 1,
                "resume_count": 0,
                "max_resumes": 1,
                "scheduled_at": _iso(now),
                "next_attempt_at": _iso(now + 30),
                "input_fingerprint": candidate["input_fingerprint"],
                "file_ids": candidate["file_ids"],
                "failure_kinds": candidate["failure_kinds"],
            }
            self._save_unlocked(state)
            return True

    def run_due_context_auto_retry(
        self,
        job_id: str,
        config_path: Path,
        *,
        secrets_path: Path | None = None,
    ) -> bool:
        """Start one due Watch-only retry, after rechecking its saved fingerprint."""
        from life_logger.local_web import (
            _iso,
            _nonnegative_int,
            _now,
            datetime,
        )

        with self._lock:
            state = self._read_unlocked(job_id)
            retry = state.get("context_auto_retry")
            if not isinstance(retry, dict) or retry.get("status") != "scheduled":
                return False
            try:
                due_at = datetime.fromisoformat(str(retry.get("next_attempt_at")))
                if due_at.tzinfo is None:
                    # [Q476] 不带 offset 的重试时间按北京时间解释，不跟宿主时区漂移
                    due_at = due_at.replace(tzinfo=LOCAL_TZ)
                due = due_at.timestamp() <= _now()
            except (TypeError, ValueError, OverflowError):
                retry.update(status="stopped", stopped_reason="invalid_schedule", finished_at=_iso())
                retry.pop("next_attempt_at", None)
                self._save_unlocked(state)
                return False
            if not due:
                return False
            if any(thread.is_alive() for thread in self._context_recovery_active.values()):
                return False
            if _nonnegative_int(retry.get("attempts")) >= _nonnegative_int(
                retry.get("max_attempts")
            ):
                retry.update(status="stopped", stopped_reason="attempt_limit", finished_at=_iso())
                retry.pop("next_attempt_at", None)
                self._save_unlocked(state)
                return False
            return self.retry_context_extraction(
                job_id,
                config_path,
                secrets_path=secrets_path,
                automatic=True,
                expected_fingerprint=str(retry.get("input_fingerprint") or ""),
            ).get("context_auto_retry", {}).get("status") == "running"

    def recover_interrupted(self, *, exclude_job_ids=()) -> list[str]:
        from life_logger.local_web import (
            RESTART_INTERRUPTED_ERROR,
            _iso,
            _nonnegative_int,
            _now,
        )

        recovered: list[str] = []
        with self._lock:
            for path in sorted(self.jobs_dir.glob("job-*/status.json")):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if str(state.get("job_id") or path.parent.name) in exclude_job_ids:
                    continue
                job_was_active = state.get("status") in {"running", "queued"}
                recovery = state.get("context_recovery")
                recovery_was_active = (
                    isinstance(recovery, dict)
                    and recovery.get("status") in {"queued", "running"}
                )
                if not job_was_active and not recovery_was_active:
                    continue
                now = _now()
                if job_was_active and state.get("started_at"):
                    state["elapsed_seconds"] = round(
                        float(state.get("elapsed_seconds", 0.0))
                        + max(0.0, now - float(state["started_at"])),
                        2,
                    )
                if job_was_active:
                    state["started_at"] = None
                    state["status"] = "interrupted"
                    state["current_stage"] = "interrupted"
                    state["error"] = RESTART_INTERRUPTED_ERROR
                    state["restart_recovery_pending"] = True
                if recovery_was_active:
                    from life_logger.context_extraction_recovery import (
                        cleanup_context_recovery_staging,
                        recover_context_recovery_publication,
                    )

                    publication = recovery.get("publication")
                    publication_outcome: str | None = None
                    committed_recovery = False
                    if isinstance(publication, dict):
                        try:
                            publication_outcome, archived = recover_context_recovery_publication(
                                path.parent,
                                publication,
                            )
                        except OSError:
                            publication_outcome, archived = "unsafe", []
                        if publication_outcome == "committed":
                            try:
                                target_files = max(0, int(recovery.get("target_files") or 0))
                                completed_files = max(0, int(recovery.get("completed_files") or 0))
                            except (TypeError, ValueError):
                                target_files = completed_files = 0
                            recovery["completed_files"] = min(
                                target_files,
                                completed_files + 1,
                            )
                            published_status = publication.get("extraction_status")
                            current_status = recovery.get("extraction_status")
                            statuses = [
                                status
                                for status in (current_status, published_status)
                                if status in {"completed", "degraded"}
                            ]
                            if statuses:
                                recovery["extraction_status"] = max(
                                    statuses,
                                    key=lambda status: {"completed": 0, "degraded": 1}[status],
                                )
                            current_archives = recovery.get("archived_artifacts")
                            if not isinstance(current_archives, list):
                                current_archives = []
                            recovery["archived_artifacts"] = list(
                                dict.fromkeys([*current_archives, *archived])
                            )
                            committed_recovery = True
                        elif publication_outcome in {"restored", "unsafe", "missing"}:
                            recovery["error"] = (
                                "服务重启时恢复中断；旧结果已保留，请重新尝试"
                            )
                        if publication_outcome != "unsafe":
                            recovery.pop("publication", None)
                    recovery_id = str(recovery.get("recovery_id") or "")
                    if publication_outcome != "unsafe":
                        _, cleanup_failures = cleanup_context_recovery_staging(
                            path.parent, recovery_id
                        )
                        if cleanup_failures:
                            recovery["staging_cleanup_failures"] = cleanup_failures
                        else:
                            recovery.pop("staging_cleanup_failures", None)
                    recovery["finished_at"] = _iso(now)
                    recovery["current_file_id"] = None
                    try:
                        target_count = max(0, int(recovery.get("target_files") or 0))
                        completed_count = max(
                            0, int(recovery.get("completed_files") or 0)
                        )
                    except (TypeError, ValueError):
                        target_count = completed_count = 0
                    if (
                        publication_outcome in {None, "committed"}
                        and target_count > 0
                        and completed_count >= target_count
                    ):
                        recovery["status"] = "completed"
                        recovery["current_stage"] = "completed"
                        recovery["error"] = None
                    else:
                        recovery["status"] = "interrupted"
                        recovery["current_stage"] = "interrupted"
                        recovery["error"] = (
                            "上次恢复的结果位置无法确认；旧结果仍保留，自动重试已暂停"
                            if publication_outcome == "unsafe"
                            else recovery.get("error")
                            or "服务重启时恢复中断；已完成结果保留，未完成部分可重试"
                        )
                    auto_retry = state.get("context_auto_retry")
                    if (
                        recovery.get("origin") == "automatic"
                        and isinstance(auto_retry, dict)
                        and auto_retry.get("status") == "running"
                    ):
                        if recovery.get("status") == "completed":
                            extraction_status = recovery.get("extraction_status")
                            auto_retry.update(
                                status=(
                                    "completed"
                                    if extraction_status == "completed"
                                    else "needs_review"
                                    if extraction_status == "degraded"
                                    else "stopped"
                                ),
                                finished_at=_iso(now),
                            )
                            if extraction_status not in {"completed", "degraded"}:
                                auto_retry["stopped_reason"] = "attempt_failed"
                            auto_retry.pop("next_attempt_at", None)
                        else:
                            from life_logger.context_auto_retry import inspect_retry_candidate

                            candidate = inspect_retry_candidate(path.parent, state)
                            expected_fingerprint = recovery.get("input_fingerprint")
                            expected_file_ids = auto_retry.get("file_ids")
                            correction_prefixes = tuple(
                                f"files/{file_id}/result/"
                                for file_id in (candidate or {}).get("file_ids", [])
                            )
                            corrections = self._read_event_corrections_unlocked(
                                str(state.get("job_id") or path.parent.name)
                            )
                            has_corrections = any(
                                isinstance(correction, dict)
                                and str(correction.get("artifact_path") or "").startswith(
                                    correction_prefixes
                                )
                                for correction in corrections.values()
                            )
                            resume_count = _nonnegative_int(auto_retry.get("resume_count"))
                            max_resumes = _nonnegative_int(auto_retry.get("max_resumes"))
                            can_resume = (
                                publication_outcome in {None, "restored"}
                                and candidate is not None
                                and candidate.get("input_fingerprint") == expected_fingerprint
                                and candidate.get("file_ids") == expected_file_ids
                                and not has_corrections
                                and resume_count < max_resumes
                            )
                            if can_resume:
                                auto_retry.update(
                                    status="scheduled",
                                    resume_count=resume_count + 1,
                                    resumed_at=_iso(now),
                                    next_attempt_at=_iso(now),
                                )
                            else:
                                auto_retry.update(
                                    status="stopped",
                                    stopped_reason=(
                                        "publication_uncertain"
                                        if publication_outcome == "unsafe"
                                        else "resume_limit_or_inputs_changed"
                                    ),
                                    finished_at=_iso(now),
                                )
                                auto_retry.pop("next_attempt_at", None)
                state["updated_at"] = _iso(now)
                if job_was_active:
                    for item in state.get("files", []):
                        if item.get("status") == "running":
                            item["status"] = "interrupted"
                        for stage in item.get("stages", {}).values():
                            if stage.get("status") == "running":
                                stage["status"] = "interrupted"
                _atomic_json(path, state)
                if job_was_active:
                    recovered.append(str(state.get("job_id") or path.parent.name))
        return recovered

    def retry_context_extraction(
        self,
        job_id: str,
        config_path: Path,
        *,
        secrets_path: Path | None = None,
        automatic: bool = False,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Queue an isolated downstream retry while leaving the ASR Job untouched."""
        from life_logger.local_web import (
            _iso,
            _nonnegative_int,
        )

        from life_logger.context_extraction_recovery import (
            find_context_recovery_targets,
        )

        with self._lock:
            state = self._read_unlocked(job_id)
            auto_retry = state.get("context_auto_retry")
            if automatic:
                from life_logger.context_auto_retry import inspect_retry_candidate

                if (
                    state.get("status") != "completed"
                    or not isinstance(auto_retry, dict)
                    or auto_retry.get("status") != "scheduled"
                ):
                    return self.read(job_id)
                files = state.get("files")
                if not isinstance(files, list) or not files or not all(
                    isinstance(item, dict)
                    and item.get("capture_source") == WATCH_CAPTURE_SOURCE
                    and item.get("source_device")
                    and item.get("source_session")
                    for item in files
                ):
                    auto_retry.update(
                        status="stopped",
                        stopped_reason="not_complete_watch_job",
                        finished_at=_iso(),
                    )
                    auto_retry.pop("next_attempt_at", None)
                    self._save_unlocked(state)
                    return self.read(job_id)
                candidate = inspect_retry_candidate(self.job_path(job_id), state)
                expected_file_ids = auto_retry.get("file_ids")
                if (
                    candidate is None
                    or not expected_fingerprint
                    or candidate.get("input_fingerprint") != expected_fingerprint
                    or auto_retry.get("input_fingerprint") != expected_fingerprint
                    or candidate.get("file_ids") != expected_file_ids
                ):
                    auto_retry.update(
                        status="stopped",
                        stopped_reason="inputs_changed",
                        finished_at=_iso(),
                    )
                    auto_retry.pop("next_attempt_at", None)
                    self._save_unlocked(state)
                    return self.read(job_id)
                try:
                    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
                    if not isinstance(config, dict):
                        raise ValueError("invalid config")
                    if secrets_path is not None:
                        try:
                            local_secrets = json.loads(
                                Path(secrets_path).read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            local_secrets = {}
                        if isinstance(local_secrets, dict):
                            deepseek_key = str(
                                local_secrets.get("deepseek_api_key") or ""
                            )
                            if deepseek_key:
                                config["_deepseek_api_key"] = deepseek_key
                                config["deepseek"] = {"enabled": True}
                    from life_logger.local_pipeline import local_pipeline_env

                    credentials_available = bool(
                        local_pipeline_env(config).get("DEEPSEEK_API_KEY")
                    )
                except Exception:
                    credentials_available = False
                if not credentials_available:
                    auto_retry.update(
                        status="stopped",
                        stopped_reason="credentials_unavailable",
                        finished_at=_iso(),
                    )
                    auto_retry.pop("next_attempt_at", None)
                    self._save_unlocked(state)
                    return self.read(job_id)
            main_worker = self._active.get(job_id)
            if main_worker and main_worker.is_alive():
                raise ValueError("任务仍在处理中，暂不能重试事件提取")
            if state.get("status") in {"queued", "running", "draft"}:
                raise ValueError("任务尚未完成，暂不能重试事件提取")
            recovery = state.get("context_recovery")
            if isinstance(recovery, dict) and recovery.get("status") in {
                "queued",
                "running",
            }:
                return self.read(job_id)
            if isinstance(recovery, dict) and isinstance(
                recovery.get("publication"), dict
            ):
                raise ValueError("上次恢复结果位置无法确认；先检查任务结果再重试")
            active = self._context_recovery_active.get(job_id)
            if active and active.is_alive():
                return self.read(job_id)
            targets, skipped = find_context_recovery_targets(
                self.job_path(job_id), state
            )
            if not targets:
                raise ValueError("没有具备完整检查点的失败或降级提取结果")
            target_prefixes = tuple(
                f"files/{target['file_id']}/result/" for target in targets
            )
            corrections = self._read_event_corrections_unlocked(job_id)
            if any(
                isinstance(correction, dict)
                and str(correction.get("artifact_path") or "").startswith(
                    target_prefixes
                )
                for correction in corrections.values()
            ):
                raise ValueError("该任务已有人工事件修正；先保留当前结果再处理修正")

            recovery_id = uuid.uuid4().hex
            previous_attempts = (
                _nonnegative_int(recovery.get("attempts"))
                if isinstance(recovery, dict)
                else 0
            )
            state["context_recovery"] = {
                "recovery_id": recovery_id,
                "origin": "automatic" if automatic else "manual",
                "status": "queued",
                "attempts": previous_attempts + 1,
                "requested_at": _iso(),
                "started_at": None,
                "finished_at": None,
                "target_files": len(targets),
                "completed_files": 0,
                "skipped_files": skipped,
                "current_file_id": None,
                "current_file_index": None,
                "current_stage": "queued",
                "batch_progress": {"completed": 0, "total": 0},
                "extraction_status": None,
                "archived_artifacts": [],
                "error": None,
            }
            if automatic:
                state["context_recovery"]["input_fingerprint"] = expected_fingerprint
                auto_retry.update(
                    status="running",
                    attempts=_nonnegative_int(auto_retry.get("attempts")) + 1,
                    started_at=_iso(),
                )
                auto_retry.pop("next_attempt_at", None)
            elif isinstance(auto_retry, dict) and auto_retry.get("status") in {
                "scheduled", "stopped", "needs_review"
            }:
                auto_retry.update(status="manual_override", manual_started_at=_iso())
                auto_retry.pop("next_attempt_at", None)
            if self.dispatch_queue is not None:
                task = self.dispatch_queue.enqueue("context", f"context:{job_id}", {
                    "job_id": job_id, "recovery_id": recovery_id, "targets": targets,
                    "config_path": str(Path(config_path).resolve()),
                    "secrets_path": str(Path(secrets_path).resolve()) if secrets_path else None,
                    "recovery": state["context_recovery"],
                    "auto_retry": state.get("context_auto_retry"),
                })
                state["context_recovery"]["task_id"] = task["id"]
                self._save_unlocked(state)
                return self.read(job_id)
            self._save_unlocked(state)
            thread = threading.Thread(
                target=self._context_recovery_worker,
                args=(
                    job_id,
                    recovery_id,
                    targets,
                    Path(config_path),
                    Path(secrets_path) if secrets_path is not None else None,
                ),
                name=f"life-logger-context-recovery-{job_id}",
                daemon=True,
            )
            self._context_recovery_active[job_id] = thread
            thread.start()
            return self.read(job_id)

    def _context_recovery_worker(
        self,
        job_id: str,
        recovery_id: str,
        targets: list[dict[str, Any]],
        config_path: Path,
        secrets_path: Path | None,
    ) -> None:
        from life_logger.local_web import (
            _iso,
        )

        from life_logger.context_extraction_recovery import (
            prepare_context_recovery_target,
            publish_context_recovery_target,
            recover_context_recovery_publication,
        )

        job_dir = self.job_path(job_id)
        acquired = False
        prepared: dict[str, Any] | None = None
        try:
            self._context_recovery_gate.acquire()
            acquired = True
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("invalid processing config")
            if secrets_path:
                try:
                    local_secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    local_secrets = {}
                if isinstance(local_secrets, dict):
                    deepseek_key = str(local_secrets.get("deepseek_api_key") or "")
                    if deepseek_key:
                        config["_deepseek_api_key"] = deepseek_key
                        config["deepseek"] = {"enabled": True}
            from life_logger.local_pipeline import local_pipeline_env

            env = local_pipeline_env(config)
            if not env.get("DEEPSEEK_API_KEY"):
                raise ValueError("DeepSeek API key is unavailable")
            raw_dictation_windows = config.get("dictation_windows")
            dictation_windows = (
                Path(str(raw_dictation_windows)).expanduser()
                if raw_dictation_windows
                else self.root / "dictation-windows.jsonl"
            )
            self._update_context_recovery(
                job_id,
                recovery_id,
                {"status": "running", "started_at": _iso(), "current_stage": "preparing"},
            )
            statuses: list[str] = []
            for index, target in enumerate(targets, start=1):
                self._update_context_recovery(
                    job_id,
                    recovery_id,
                    {
                        "current_file_id": target["file_id"],
                        "current_file_index": index,
                        "current_stage": "context_extraction",
                        "batch_progress": {"completed": 0, "total": 0},
                    },
                )

                def on_progress(progress: dict[str, Any]) -> None:
                    batch = int(progress.get("batch") or 0)
                    total = int(progress.get("total_batches") or 0)
                    status = str(progress.get("status") or "running")
                    completed = batch if status in {"completed", "failed"} else max(0, batch - 1)
                    self._update_context_recovery(
                        job_id,
                        recovery_id,
                        {
                            "batch_progress": {
                                "completed": completed,
                                "total": total,
                            }
                        },
                    )

                prepared = prepare_context_recovery_target(
                    job_dir=job_dir,
                    target=target,
                    recovery_id=recovery_id,
                    config=config,
                    env=env,
                    timezone_name=str(
                        self.read(job_id).get("timezone") or "Asia/Shanghai"
                    ),
                    dictation_windows=dictation_windows,
                    progress_callback=on_progress,
                )
                archive_relative = str(
                    Path(prepared["archive_dir"]).relative_to(job_dir)
                )
                staging_relative = str(
                    Path(prepared["staging_dir"]).relative_to(job_dir)
                )
                publication = {
                    "recovery_id": recovery_id,
                    "file_id": target["file_id"],
                    "archive_path": archive_relative,
                    "staging_path": staging_relative,
                    "original_signature": [
                        list(entry) for entry in prepared["original_signature"]
                    ],
                    "staged_signature": [
                        list(entry) for entry in prepared["staged_signature"]
                    ],
                    "extraction_status": prepared["extraction_status"],
                    "old_artifacts": prepared["old_artifacts"],
                }
                self._update_context_recovery(
                    job_id,
                    recovery_id,
                    {"publication": publication, "current_stage": "publishing"},
                )
                with self._lock:
                    state = self._read_unlocked(job_id)
                    recovery = state.get("context_recovery")
                    if (
                        not isinstance(recovery, dict)
                        or recovery.get("recovery_id") != recovery_id
                    ):
                        raise RuntimeError("context recovery state changed")
                    target_prefix = f"files/{target['file_id']}/result/"
                    corrections = self._read_event_corrections_unlocked(job_id)
                    if any(
                        isinstance(correction, dict)
                        and str(correction.get("artifact_path") or "").startswith(
                            target_prefix
                        )
                        for correction in corrections.values()
                    ):
                        raise RuntimeError(
                            "人工事件修正已更新；原有结果保留，请重新检查后再试"
                        )
                    archived = publish_context_recovery_target(job_dir, prepared)
                    previous_archives = recovery.get("archived_artifacts")
                    if not isinstance(previous_archives, list):
                        previous_archives = []
                    recovery["archived_artifacts"] = list(
                        dict.fromkeys([*previous_archives, *archived])
                    )
                    recovery["completed_files"] = index
                    statuses.append(str(prepared["extraction_status"]))
                    recovery["extraction_status"] = max(
                        statuses,
                        key=lambda value: {"completed": 0, "degraded": 1}.get(value, 2),
                    )
                    recovery.pop("publication", None)
                    recovery["current_stage"] = "context_extraction"
                    self._save_unlocked(state)
                    prepared = None

            self._update_context_recovery(
                job_id,
                recovery_id,
                {
                    "status": "completed",
                    "finished_at": _iso(),
                    "current_file_id": None,
                    "current_file_index": None,
                    "current_stage": "completed",
                    "error": None,
                },
            )
        except Exception as error:
            with self._lock:
                try:
                    state = self._read_unlocked(job_id)
                    recovery = state.get("context_recovery")
                    publication_outcome: str | None = None
                    if isinstance(recovery, dict) and recovery.get("recovery_id") == recovery_id:
                        publication = recovery.get("publication")
                        if isinstance(publication, dict):
                            publication_outcome, archived = recover_context_recovery_publication(
                                job_dir, publication
                            )
                            if publication_outcome == "committed":
                                previous_archives = recovery.get("archived_artifacts")
                                if not isinstance(previous_archives, list):
                                    previous_archives = []
                                recovery["archived_artifacts"] = list(
                                    dict.fromkeys([*previous_archives, *archived])
                                )
                                recovery["completed_files"] = min(
                                    len(targets),
                                    int(recovery.get("completed_files") or 0) + 1,
                                )
                            if publication_outcome != "unsafe":
                                recovery.pop("publication", None)
                        recovery.update(
                            {
                                "status": "failed",
                                "finished_at": _iso(),
                                "current_file_id": None,
                                "current_stage": "failed",
                                "error": (
                                    "请先在设置中配置DeepSeek密钥，再重新尝试"
                                    if isinstance(error, ValueError)
                                    and str(error) == "DeepSeek API key is unavailable"
                                    else (
                                        "恢复结果位置无法确认；旧结果仍保留，自动重试已暂停"
                                        if publication_outcome == "unsafe"
                                        else "事件提取恢复未完成；之前的正式结果已保留"
                                    )
                                ),
                            }
                        )
                        if recovery.get("origin") == "automatic":
                            auto_retry = state.get("context_auto_retry")
                            if isinstance(auto_retry, dict):
                                auto_retry.update(
                                    status="stopped",
                                    stopped_reason=(
                                        "publication_uncertain"
                                        if publication_outcome == "unsafe"
                                        else "attempt_failed"
                                    ),
                                    finished_at=_iso(),
                                )
                                auto_retry.pop("next_attempt_at", None)
                    if publication_outcome != "unsafe":
                        from life_logger.context_extraction_recovery import (
                            cleanup_context_recovery_staging,
                        )

                        _, cleanup_failures = cleanup_context_recovery_staging(
                            job_dir, recovery_id
                        )
                        if cleanup_failures and isinstance(recovery, dict):
                            recovery["staging_cleanup_failures"] = cleanup_failures
                    self._save_unlocked(state)
                except Exception:
                    pass
        finally:
            with self._lock:
                self._context_recovery_active.pop(job_id, None)
            if acquired:
                self._context_recovery_gate.release()
