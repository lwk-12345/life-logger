"""JobStore 的 _JobReadsMixin 方法，从 local_web.py 机械搬出，行为不变。"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json


class _JobReadsMixin:
    def job_path(self, job_id: str) -> Path:
        if Path(job_id).name != job_id or not job_id.startswith("job-"):
            raise ValueError("任务编号无效")
        return self.jobs_dir / job_id

    def read_context_relation_reviews(
        self, job_id: str, *, artifact_path: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            artifact_path, _ = self._load_relation_candidates_unlocked(
                job_id, artifact_path
            )
            path = self.context_relation_reviews_path(job_id)
            if not path.exists():
                return []
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("关系复核记录格式无效") from error
            reviews = payload.get("reviews", {}) if isinstance(payload, dict) else {}
            if not isinstance(reviews, dict):
                return []
            items = [
                dict(item)
                for item in reviews.values()
                if isinstance(item, dict) and item.get("artifact_path") == artifact_path
            ]
            return sorted(items, key=lambda item: str(item.get("candidate_id") or ""))

    def _read_auto_ingest_unlocked(self) -> dict[str, Any]:
        state = self._default_auto_ingest_state()
        path = self.auto_ingest_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            state.update(payload)
        if not isinstance(state.get("folders"), list):
            state["folders"] = []
        if not isinstance(state.get("candidates"), dict):
            state["candidates"] = {}
        if not isinstance(state.get("processed"), dict):
            state["processed"] = {}
        if not isinstance(state.get("history"), list):
            state["history"] = []
        return state

    def read_task_states(self) -> list[dict[str, Any]]:
        # [2026-10-22 P0] 这里原来把 json 读、解析、排序全放在 `self._lock` 里。
        # 后台转写/抽取（典型是 add_file 边收边写、create_watch_pending_job 全量 glob）
        # 会把全局锁按住几十秒，于是 /api/daily、/api/dashboard/daily、/api/stats
        # 这些**纯读**接口排在后面等锁 → 实测 42~76s。
        # 写侧一律走 `_atomic_json`（tmp + os.replace），读者拿到的永远是完整文件，
        # 所以读侧完全不需要持锁。
        path = self.task_states_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        states = payload.get("tasks", {})
        if not isinstance(states, dict):
            return []
        return sorted(
            (
                item
                for item in states.values()
                if isinstance(item, dict) and item.get("task_key")
            ),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def _read_event_file_unlocked(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return list(payload.get("events", []))

    def read_job_events(self, job_id: str) -> list[dict[str, Any]]:
        job_dir = self.job_path(job_id)
        events: list[dict[str, Any]] = []
        for path in job_dir.glob("files/*/result/*/events.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, list):
                artifact_path = str(path.relative_to(job_dir))
                with self._lock:
                    corrected = self._apply_event_corrections_unlocked(
                        job_id, artifact_path, payload
                    )
                events.extend(corrected)
        return events

    def list_job_artifacts(self, job_id: str) -> list[str]:
        """按磁盘实况列出 job 已产出的产物（相对路径，正斜杠）。

        为什么不能只信 status.json 的 results：2026-10-09 实测有 3 个 job
        （例：job-20261009-221347-aba53854 状态 partial、job-20261009-231933-
        50b43361 状态 completed）磁盘上已有 daily_summary.json / events.json，
        但 results 数组要么是空的、要么只记了一个裸日期 —— 前端只认 results
        时这些已产出的事件就被判成「尚未形成正式事件」而永远不显示。
        这里直接扫产物目录（result/ 与 asr/，量级：中位数 19 + 10 个文件），
        给前端一个更宽的、基于实况的判据。
        """
        job_dir = self.job_path(job_id).resolve()
        artifacts: list[str] = []
        for pattern in ("files/*/result/**/*", "files/*/asr/**/*"):
            for path in job_dir.glob(pattern):
                if path.is_file():
                    artifacts.append(path.relative_to(job_dir).as_posix())
        return sorted(set(artifacts))

    def _read_event_corrections_unlocked(
        self, job_id: str
    ) -> dict[str, dict[str, Any]]:
        path = self.event_corrections_path(job_id)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        corrections = (
            payload.get("corrections", {}) if isinstance(payload, dict) else {}
        )
        return corrections if isinstance(corrections, dict) else {}

    def read_event_corrections(
        self, job_id: str, *, artifact_path: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            corrections = self._read_event_corrections_unlocked(job_id)
            values = [
                item
                for item in corrections.values()
                if isinstance(item, dict)
                and (
                    artifact_path is None
                    or item.get("artifact_path") == artifact_path
                )
            ]
            return sorted(
                values,
                key=lambda item: (
                    str(item.get("artifact_path") or ""),
                    str(item.get("event_id") or ""),
                ),
            )

    def read_manual_events(
        self,
        job_id: str,
        *,
        artifact_path: str | None = None,
        include_rejected: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            events = [
                *self._read_event_file_unlocked(self.manual_events_path(job_id)),
                *self._read_event_file_unlocked(
                    self.review_derived_events_path(job_id)
                ),
            ]
            if artifact_path is not None:
                events = [
                    item
                    for item in events
                    if item.get("artifact_path") == artifact_path
                ]
                events = self._apply_event_corrections_unlocked(
                    job_id,
                    artifact_path,
                    events,
                    include_rejected=include_rejected,
                )
            else:
                grouped: dict[str, list[dict[str, Any]]] = {}
                without_path: list[dict[str, Any]] = []
                for item in events:
                    item_path = str(item.get("artifact_path") or "")
                    if item_path:
                        grouped.setdefault(item_path, []).append(item)
                    else:
                        without_path.append(item)
                events = without_path
                for item_path, grouped_events in grouped.items():
                    events.extend(
                        self._apply_event_corrections_unlocked(
                            job_id,
                            item_path,
                            grouped_events,
                            include_rejected=include_rejected,
                        )
                    )
            return sorted(
                events,
                key=lambda item: (
                    item.get("time", {}).get("absolute") or "",
                    item.get("event_id", ""),
                ),
            )

    def read_reviews(
        self, job_id: str, *, artifact_path: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            path = self.review_path(job_id)
            if not path.exists():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
            decisions = list(payload.get("decisions", {}).values())
            if artifact_path is not None:
                decisions = [
                    item
                    for item in decisions
                    if item.get("artifact_path") == artifact_path
                ]
            return sorted(
                decisions,
                key=lambda item: (
                    item.get("artifact_path", ""),
                    item.get("segment_id", ""),
                ),
            )

    def _read_unlocked(self, job_id: str) -> dict[str, Any]:
        path = self.status_path(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise KeyError(job_id) from error

    def _journal_revision_unlocked(
        self,
        job_id: str,
        state: dict[str, Any],
    ) -> str:
        parts = [str(state.get("updated_at") or "")]
        mutable_paths = (
            self.review_path(job_id),
            self.manual_events_path(job_id),
            self.review_derived_events_path(job_id),
            self.event_corrections_path(job_id),
        )
        for path in mutable_paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def list(self) -> list[dict[str, Any]]:
        # [2026-10-22 P0] 全量 glob + 读 status.json 是 170+ 次文件 I/O，
        # 原来整段在 `self._lock` 内 → 与后台任务互相排队。
        # 改为锁外收集：appending 新 job 只是新增目录，不会让已有快照失真。
        states = []
        for path in list(self.jobs_dir.glob("job-*/status.json")):
            try:
                states.append(self.read(path.parent.name))
            except (KeyError, json.JSONDecodeError):
                continue
        return sorted(
            states,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    def interrupted_job_ids(self) -> list[str]:
        interrupted: list[str] = []
        with self._lock:
            for path in sorted(self.jobs_dir.glob("job-*/status.json")):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if state.get("status") == "interrupted":
                    interrupted.append(str(state.get("job_id") or path.parent.name))
        return interrupted
