"""Runtime Lifecycle Management for macOS Application Input Context.

Provides an explicit start/stop/status manager that:
1. Capacity Cap Aligned:
   - Output byte budget DEFAULT_MAX_FILE_BYTES strictly aligned with reader INPUT_CONTEXT_MAX_BYTES = 2_000_000.
   - Any oversize payload (even single window) is strictly truncated/budget-capped to <= max_file_size_bytes.
   - Retains truncation audit flag; never fakes completeness.
2. Bounded Memory on Underlying Collector:
   - collector.windows, front_samples, mic_events and anomalies are bounded and pruned alongside retention.
   - Necessary evidence for in-progress open_window is preserved.
3. Strict Timezone Contract:
   - Aware absolute timestamps (+08:00, Z) are preserved.
   - Naive timestamps (tzinfo is None) are strictly treated as naive/unverified and rejected on export
     (never guessed as local or Shanghai timezone).
4. Non-CWD Safe Bootstrapping:
   - Safely bootstraps collector import regardless of process CWD without polluting global sys.path.
5. Lifecycle & Process Cleanup:
   - Explicit start()/stop() without background auto-launch, launchd or config modification.
   - Stop guarantees termination/cleanup of any spawned mic / collector subprocesses.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

from life_logger.input_context import (  # noqa: E402  归档/可读名纯函数
    ARCHIVE_DIR_RELATIVE,
    BUNDLE_APP_LABELS,
    archive_day_for_window,
    bundle_to_app_label,
    window_archive_key,
)

ARTIFACT_RELATIVE_PATH = "input-context/latest.json"
SCHEMA_VERSION = "mac-input-context-v1"
DEFAULT_RETENTION_MINUTES = 30.0
DEFAULT_MIN_CLOSED_MINUTES = 15.0
#: Aligned strictly with reader INPUT_CONTEXT_MAX_BYTES = 2_000_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
SHANGHAI_TZ = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now().astimezone(SHANGHAI_TZ)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Naive datetime: 绝不猜测上海时区，严格拒绝输出
        return None
    return dt.astimezone(SHANGHAI_TZ).isoformat()


def _parse_iso(raw: Any) -> datetime | None:
    """Parse an ISO datetime string.

    Strict Timezone Contract (2026-10-26):
    - Aware timestamps with explicit offset (+08:00, Z) are preserved and converted to aware.
    - Naive timestamps (tzinfo is None) retain tzinfo=None (never arbitrarily tagged with Shanghai).
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        if raw.endswith("Z") or "+" in raw or (raw.count("-") >= 3 and ":" in raw):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(SHANGHAI_TZ)
        parsed = datetime.fromisoformat(raw)
        return parsed
    except (ValueError, TypeError):
        return None


class InputContextRuntimeManager:
    """Explicit lifecycle manager for input context collection and publishing."""

    def __init__(
        self,
        store_root: Path | str,
        *,
        collector: Any | None = None,
        retention_minutes: float = DEFAULT_RETENTION_MINUTES,
        min_closed_window_minutes: float = DEFAULT_MIN_CLOSED_MINUTES,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_BYTES,
        poll_interval_seconds: float = 0.5,
    ):
        self.store_root = Path(store_root)
        self.artifact_path = self.store_root / ARTIFACT_RELATIVE_PATH
        self.retention_minutes = max(1.0, float(retention_minutes))
        self.min_closed_window_minutes = max(1.0, float(min_closed_window_minutes))
        self.max_file_size_bytes = max(100, int(max_file_size_bytes))
        self.poll_interval = max(0.05, float(poll_interval_seconds))
        # [2026-11-04] 前台应用**连续采样**的合并区间。
        # 为什么单独维护：collector.front_samples 只保留最后 20 条（约 10s），
        # 而用户要问的是"我说这句话时前台是什么应用" —— 需要覆盖整段对话，不是某个瞬间。
        # 这里把连续相同的 bundle 合并成 span，按 retention 裁剪后随快照导出（字段 front_windows）。
        self._front_spans: list[dict[str, Any]] = []

        self.collector = collector
        self._external_collector = collector is not None
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._windows_history: list[dict[str, Any]] = []
        #: 每个归档日已落盘的 archive_key 集合（首次写该日时从文件恢复）。
        self._archive_seen: dict[str, set[str]] = {}
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_publish_at: datetime | None = None
        self._tracked_subprocesses: list[subprocess.Popen] = []

    @property
    def is_running(self) -> bool:
        return self._running and bool(self._thread and self._thread.is_alive())

    def register_subprocess(self, proc: subprocess.Popen) -> None:
        """Track a child process spawned by collectors for cleanup on stop."""
        with self._lock:
            self._tracked_subprocesses.append(proc)

    def _ensure_collector(self) -> Any:
        """Safely import and initialize live collector regardless of process CWD."""
        if self.collector is None:
            repo_root = Path(__file__).resolve().parents[2]
            scripts_dir = repo_root / "scripts"
            added_to_sys = False
            if scripts_dir.is_dir() and str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
                added_to_sys = True
            try:
                from scripts.mac_input_context import (
                    InputContextCollector,
                    LiveFrontSource,
                    LiveMicSource,
                )
                self.collector = InputContextCollector(
                    front_source=LiveFrontSource(),
                    mic_source=LiveMicSource(),
                )
            except Exception as exc:
                logger.warning("Could not initialize live collector: %s", exc)
                self.collector = None
            finally:
                if added_to_sys and str(repo_root) in sys.path:
                    try:
                        sys.path.remove(str(repo_root))
                    except ValueError:
                        pass
        return self.collector

    def start(self) -> None:
        """Explicitly start the polling background worker thread."""
        with self._lock:
            if self._running:
                return
            self._ensure_collector()
            self._running = True
            self._stop_event.clear()
            self._started_at = _now()
            self._stopped_at = None
            self._thread = threading.Thread(
                target=self._run_loop,
                name="InputContextRuntimeWorker",
                daemon=True,
            )
            self._thread.start()
            logger.info("InputContextRuntimeManager started on %s", self.artifact_path)

    def stop(self, timeout_seconds: float = 3.0) -> None:
        """Explicitly stop the runtime, terminate child processes, and flush output."""
        with self._lock:
            if not self._running:
                self._cleanup_subprocesses()
                return
            self._running = False
            self._stop_event.set()
            self._stopped_at = _now()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0.5, timeout_seconds))

        with self._lock:
            self._cleanup_subprocesses()
            now = _now()
            if self.collector is not None:
                self._harvest_collector_windows(now=now)
            self._prune_and_publish(now=now)
            logger.info("InputContextRuntimeManager stopped")

    def _cleanup_subprocesses(self) -> None:
        """Terminate and clean up any tracked child processes."""
        for proc in list(self._tracked_subprocesses):
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=0.5)
            except Exception as error:
                logger.debug("Error cleaning up subprocess %s: %s", getattr(proc, "pid", None), error)
        self._tracked_subprocesses.clear()

        if self.collector and hasattr(self.collector, "mic_source"):
            proc = getattr(self.collector.mic_source, "process", None)
            if isinstance(proc, subprocess.Popen) and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.5)
                except Exception:
                    pass

    def step_once(self, *, at: datetime | None = None) -> dict[str, Any]:
        """Perform one sampling step, harvest windows, and atomically publish."""
        with self._lock:
            now = at or _now()
            coll = self._ensure_collector()
            if coll and hasattr(coll, "sample_once"):
                coll.sample_once(at=now)
                self._harvest_collector_windows(now=now)
                self._record_front_spans(now=now)
            return self._prune_and_publish(now=now)

    def _run_loop(self) -> None:
        """Background thread sampling loop."""
        while not self._stop_event.is_set():
            try:
                self.step_once()
            except Exception as error:
                logger.warning("Error in input context sample step: %s", error)
            self._stop_event.wait(timeout=self.poll_interval)

    def _record_front_spans(self, *, now: datetime) -> None:
        """把连续的前台应用采样合并成区间（同一 bundle 连续 → 一段）。

        只记录 bundle/时间：不读窗口标题、不读剪贴板、不读键盘（沿用采集端策略）。
        """
        if not self.collector:
            return
        samples = getattr(self.collector, "front_samples", None)
        if not isinstance(samples, list) or not samples:
            return
        last_seen = getattr(self, "_front_last_sample_at", None)
        for sample in samples:
            at = getattr(sample, "at", None)
            if at is None or at.tzinfo is None:
                continue
            if last_seen is not None and at <= last_seen:
                continue                      # 已经处理过的采样
            bundle = getattr(sample, "bundle", None) or None
            gap_ok = (
                self._front_spans
                and (at - self._front_spans[-1]["_end"]).total_seconds() <= max(5.0, self.poll_interval * 10)
            )
            if self._front_spans and self._front_spans[-1].get("target_app") == bundle and gap_ok:
                self._front_spans[-1]["_end"] = at
                self._front_spans[-1]["ended_at"] = _iso(at)
                self._front_spans[-1]["duration_seconds"] = round(
                    (at - self._front_spans[-1]["_start"]).total_seconds(), 3)
            else:
                self._front_spans.append({
                    "_start": at,
                    "_end": at,
                    "started_at": _iso(at),
                    "ended_at": _iso(at),
                    "duration_seconds": 0.0,
                    "target_app": bundle,
                    "front_bundles": [bundle] if bundle else [],
                    "target_unknown": not bool(bundle),
                    "provider": {"pid": getattr(sample, "pid", None), "process": None,
                                 "source": "front_sample"},
                    "complete": False,
                    "meaning": "front_app_at_speech_time_not_delivery",
                    "delivery_inferred": False,
                })
            last_seen = at
        self._front_last_sample_at = last_seen
        # 按 retention 裁剪（与 mic 窗口同一口径）
        cutoff = now - timedelta(minutes=self.retention_minutes)
        self._front_spans = [sp for sp in self._front_spans if sp["_end"] >= cutoff][-400:]

    def _harvest_collector_windows(self, *, now: datetime) -> None:
        """Harvest new closed windows into manager and prune underlying collector state."""
        if not self.collector:
            return

        # 1. 收割闭合窗口
        collector_windows = getattr(self.collector, "windows", [])
        if collector_windows:
            seen_starts = {
                w.get("started_at") for w in self._windows_history if w.get("started_at")
            }
            for cw in collector_windows:
                s_at = cw.get("started_at")
                if s_at and s_at not in seen_starts:
                    self._windows_history.append(dict(cw))
                    seen_starts.add(s_at)
            # 清空底层 collector 已收割的历史窗口，彻底消除内存泄漏！
            try:
                self.collector.windows.clear()
            except Exception:
                pass

        # 2. 对底层 collector 的 front_samples 和 mic_events 进行有界清理
        open_win = getattr(self.collector, "open_window", None)
        open_start = open_win.get("start") if isinstance(open_win, Mapping) else None

        if hasattr(self.collector, "front_samples") and isinstance(self.collector.front_samples, list):
            if open_start:
                self.collector.front_samples = [
                    s for s in self.collector.front_samples if getattr(s, "at", None) and s.at >= open_start
                ]
            else:
                self.collector.front_samples = self.collector.front_samples[-20:]

        if hasattr(self.collector, "mic_events") and isinstance(self.collector.mic_events, list):
            if open_start:
                self.collector.mic_events = [
                    m for m in self.collector.mic_events if getattr(m, "at", None) and m.at >= open_start
                ]
            else:
                self.collector.mic_events = self.collector.mic_events[-20:]

        if hasattr(self.collector, "anomalies") and isinstance(self.collector.anomalies, list):
            if len(self.collector.anomalies) > 50:
                self.collector.anomalies = self.collector.anomalies[-50:]

    def _archive_closed_windows(self, windows: list[dict[str, Any]]) -> int:
        """把**已闭合**窗口增量追加到 `input-context/days/YYYY-MM-DD.jsonl`。

        [2026-11-04] 为什么必须有：`latest.json` 只保留 30 分钟，而活动时间线是在
        数小时后才跑的 —— 等它要查"我说这句话时在哪个应用"，窗口早被裁掉，
        历史日永远拿不到前台应用。归档按北京时间日期分文件、append-only、不重写。

        去重：以 `(started_at, ended_at, target_app)` 为键，先读该日已落盘的键集
        （每个日期只读一次，缓存在 `self._archive_seen`），已存在就跳过。
        `ended_at` 为空（录音进行中）**不归档** —— 未闭合窗口下次会带着结束时间再来。

        任何 I/O 失败都只 log 不抛：归档绝不能阻断发布。
        """
        written = 0
        by_day: dict[str, list[dict[str, Any]]] = {}
        for w in windows:
            if not isinstance(w, Mapping):
                continue
            if not w.get("ended_at"):
                continue                      # 未闭合，等下次
            day = archive_day_for_window(w)
            key = window_archive_key(w)
            if not day or not key:
                continue
            by_day.setdefault(day, []).append(
                dict(w, archive_key=key, archived_at=_iso(self._last_publish_at)))
        for day, items in by_day.items():
            seen = self._archive_seen.get(day)
            if seen is None:
                seen = set()
                path = self.store_root / ARCHIVE_DIR_RELATIVE / f"{day}.jsonl"
                try:
                    if path.is_file():
                        with path.open("r", encoding="utf-8") as fh:
                            for line in fh:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    row = json.loads(line)
                                except ValueError:
                                    continue
                                if isinstance(row, dict) and row.get("archive_key"):
                                    seen.add(str(row["archive_key"]))
                except OSError as error:
                    logger.warning("input context archive read skipped (%s): %s", day, error)
                self._archive_seen[day] = seen

            fresh = [w for w in items if w["archive_key"] not in seen]
            if not fresh:
                continue
            path = self.store_root / ARCHIVE_DIR_RELATIVE / f"{day}.jsonl"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for w in fresh:
                        fh.write(json.dumps(w, ensure_ascii=False) + "\n")
                        seen.add(w["archive_key"])
                        written += 1
            except OSError as error:
                logger.warning("input context archive append failed (%s): %s", day, error)
        return written

    def _prune_and_publish(self, *, now: datetime) -> dict[str, Any]:
        """Prune windows older than retention threshold and atomically publish snapshot."""
        cutoff_retention = now - timedelta(minutes=self.retention_minutes)
        cutoff_min_closed = now - timedelta(minutes=self.min_closed_window_minutes)

        # 1. 过滤超过 30 分钟的过期窗口，但确保保留最近 15 分钟内的所有闭合窗口
        retained: list[dict[str, Any]] = []
        for w in self._windows_history:
            end_dt = _parse_iso(w.get("ended_at")) or _parse_iso(w.get("started_at"))
            if end_dt is None or end_dt.tzinfo is None:
                # Naive 或异常时间：拒绝处理
                continue
            if end_dt >= cutoff_retention or end_dt >= cutoff_min_closed:
                retained.append(w)

        self._windows_history = retained

        # 2. 如果存在正在进行的未闭合 open 窗口，附加为一个 unknown open 窗口
        export_windows = [dict(w) for w in self._windows_history]
        open_win = getattr(self.collector, "open_window", None) if self.collector else None
        if isinstance(open_win, Mapping) and open_win.get("start"):
            start_dt = open_win["start"]
            start_iso = _iso(start_dt)
            if start_iso:
                export_windows.append({
                    "started_at": start_iso,
                    "ended_at": None,
                    "duration_seconds": round((now - start_dt).total_seconds(), 3),
                    "provider": {
                        "pid": open_win.get("pid"),
                        "process": None,
                        "source": "mic_event",
                    },
                    "provider_pid": open_win.get("pid"),
                    "front_bundles": [],
                    "target_app": None,
                    "target_unknown": True,
                    "complete": False,
                    "reason": "recording_in_progress",
                    "meaning": "open_window_target_unverified",
                    "delivery_inferred": False,
                })

        export_windows.sort(key=lambda w: str(w.get("started_at") or ""))
        # [2026-11-04] 人类可读应用名：`com.yetone.alma` → `Alma`。
        # 只做映射、不读窗口标题；未知 bundle 退化为最后一段首字母大写。
        for _w in export_windows:
            if not _w.get("app_label"):
                _w["app_label"] = bundle_to_app_label(
                    _w.get("target_app") or _w.get("target_bundle"))

        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(now),
            "window_count": len(export_windows),
            "retention_policy": {
                "retention_minutes": self.retention_minutes,
                "min_closed_window_minutes": self.min_closed_window_minutes,
            },
            "windows": export_windows,
            # [2026-11-04] 前台应用连续采样区间（独立字段，不动 windows 契约）。
            # 消费者（watch_formal_asr.load_input_context_windows）会把它并进候选窗口，
            # 这样"我说这句话时前台是什么应用"才能被解析到 —— 旧实现只有 mic 启停产生的窗口。
            # complete 的语义在这里是「这段区间有足够证据可信」，不是「录音已闭合」：
            #   * 连续采样 >= 2s（至少数个 0.5s 采样点指向同一 bundle）→ complete=True；
            #   * 只有一个采样点（跨度 ~0s）→ complete=False，交回 unknown，不硬猜。
            # `resolve_input_context` 会拒绝 incomplete_window —— 所以单点采样绝不会被当成归因。
            "front_windows": [
                {**{k: v for k, v in sp.items() if not k.startswith("_")},
                 "app_label": bundle_to_app_label(sp.get("target_app") or sp.get("target_bundle")),
                 "complete": bool((sp.get("duration_seconds") or 0) >= 2.0),
                 "reason": None if (sp.get("duration_seconds") or 0) >= 2.0 else "single_sample_span"}
                for sp in self._front_spans
            ],
            "front_window_count": len(self._front_spans),
        }

        # 3. 大小保护与严格截断：序列化后若超过 max_file_size_bytes，弹出最旧窗口
        serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(serialized) > self.max_file_size_bytes:
            while len(serialized) > self.max_file_size_bytes and payload["windows"]:
                payload["windows"].pop(0)
                payload["window_count"] = len(payload["windows"])
                payload["truncated_oversize"] = True
                serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

            # 若单窗口超大或 metadata 超大导致清空后依然超标，构造极简安全错误 payload
            if len(serialized) > self.max_file_size_bytes:
                payload = {
                    "schema_version": SCHEMA_VERSION,
                    "error": "payload_exceeded_byte_limit",
                    "truncated_oversize": True,
                    "window_count": 0,
                    "windows": [],
                }
                serialized = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # 4. 原子写入：临时文件 + os.replace
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = f".latest.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        tmp_path = self.artifact_path.parent / tmp_name
        try:
            tmp_path.write_bytes(serialized)
            os.replace(tmp_path, self.artifact_path)
            self._last_publish_at = now
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        # 5. 按日归档（append-only）：latest.json 只留 30 分钟，历史日靠它回溯。
        #    放在原子写之后 —— latest 与归档不一致时，以 latest 为准。
        #    失败只 log，绝不阻断发布。
        try:
            # [2026-11-05] front_windows 过去只活在 latest.json（30 分钟就被裁），
            # 而手表转写经常更晚 —— 于是历史日永远查不到「当时前台是哪个应用」。
            # 这里把它和 mic 窗口一起归档；front 窗口自带 target_app，键里已含它。
            archive_batch = list(export_windows) + [
                dict(w, record_kind="front_sample")
                for w in payload.get("front_windows") or []
                if isinstance(w, Mapping)
            ]
            archived = self._archive_closed_windows(archive_batch)
            from life_logger.app_context import prune_day_files

            prune_day_files(self.store_root / "input-context" / "days")
            payload["archived_windows"] = archived
        except Exception as error:                     # noqa: BLE001
            logger.warning("input context archive skipped: %s", error)
            payload["archived_windows"] = 0

        return payload

    def status(self) -> dict[str, Any]:
        """Return structured runtime health and lifecycle status."""
        with self._lock:
            return {
                "running": self.is_running,
                "started_at": _iso(self._started_at),
                "stopped_at": _iso(self._stopped_at),
                "last_publish_at": _iso(self._last_publish_at),
                "artifact_path": str(self.artifact_path),
                "artifact_exists": self.artifact_path.is_file(),
                "artifact_size_bytes": self.artifact_path.stat().st_size if self.artifact_path.is_file() else 0,
                "retained_windows_count": len(self._windows_history),
                "tracked_subprocesses_count": len(self._tracked_subprocesses),
                "pid": os.getpid(),
            }
