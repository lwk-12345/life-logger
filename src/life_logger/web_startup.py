"""服务启动与 LocalWebServer 组装（S6 从 local_web.py 原样搬出）。

可被 monkeypatch 的本地名字一律通过 ``_lw.<name>`` 运行时读取（函数内惰性导入
``local_web``，避免模块级循环导入），保证门面 patch 真正生效。
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import quote
from zoneinfo import ZoneInfo
from typing import Any, Mapping

from life_logger.local_reminders import ReminderNotifier
from life_logger.passport_stream import (
    CAPTURE_SOURCE,
    PASSPORT_DISCOVERY_PORT,
    PASSPORT_STREAM_PORT,
    PassportReceiverRuntime,
)
from life_logger.local_habits import LocalHabitStore
from life_logger.local_reminders import LocalReminderStore
from life_logger.watch_formal_asr import WatchFormalASRProcessor
from life_logger.watch_stream import WatchSessionStore
from life_logger.web_preferences import _normalize_trusted_network_bind
from life_logger.web_server_reads import _ServerReadsMixin
from life_logger.web_server_services import _ServerServicesMixin
from life_logger.web_common import PROJECT_ROOT
from life_logger.web_common import logger
from life_logger.web_preferences import _desired_mobile_bind, _read_preferences

from . import job_store as _jobs
from . import web_auth as _auth
from . import web_support as _support
from . import watch_upload as _watch

API_FEATURES = (
    "health_v1",
    "event_corrections_v1",
    "corrected_transcript_v1",
    "state_backup_v1",
    "data_health_v1",
    "auto_ingest_v1",
    "passport_stream_v1",
    "watch_session_stream_v1",
    "watch_local_access_v1",
    "doctor_v1",
    "daily_brief_v1",
    "agent_brief_v1",
    "agent_daily_v1",
    "statistics_v1",
    "pattern_observations_v1",
    "mobile_access_v1",
    "mobile_capture_v1",
    "local_reminders_v1",
    "pattern_alert_candidates_v1",
    "search_amount_filter_v1",
    "event_read_contract_v1",
    "expense_policy_v2",
    "canonical_event_search_v1",
    "context_auto_retry_v1",
    "context_relation_review_v1",
    "health_sync_v1",
    "memory_rag_v1",
    "daily_dashboard_v1",
    "dashboard_dictation_filter_v1",
    # 【2026-10-14 摘除 "scene_classifier_v1"】该能力对外自述 ≠ 实际接通：
    # src/life_logger/scene_classifier.py 是完整的声学场景分类器（ASC/SED，6 类场景
    # office/transit/dining/outdoor_street/home_quiet/general），但全仓生产路径对它
    # 引用 0 次（仅本行字符串 + 测试文件）。看板显示的场景实际来自
    # daily_dashboard.build_timeline_scenes() 的另一套关键词/事件实现。
    # 对外宣称未接通的能力 = 信任问题，先摘除；接通 scene_classifier 后再改回并加回归测试。
    "voice_triggers_v1",
    "local_habits_v1",
    "pending_sources_v1",
) + (("native_folder_picker_v1",) if sys.platform == "darwin" else ())

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_WATCH_LAST_SEG_CACHE: dict[
    tuple[str, int, int, int, int, int],
    tuple[
        dict[str, tuple[float, datetime, dict[str, Any]]],
        dict[str, tuple[datetime, dict[str, Any]]],
    ],
] = {}
_WATCH_LAST_SEG_CACHE_MAX = 512


def _session_last_segment_by_date(
    manifest_path: Path,
) -> tuple[
    dict[str, tuple[float, datetime, dict[str, Any]]],
    dict[str, tuple[datetime, dict[str, Any]]],
]:
    try:
        m_stat = manifest_path.stat()
    except OSError:
        return {}, {}
    segments_dir = manifest_path.parent / "segments"
    try:
        s_stat = segments_dir.stat()
        s_mtime_ns, s_ctime_ns = int(s_stat.st_mtime_ns), int(s_stat.st_ctime_ns)
    except OSError:
        s_mtime_ns, s_ctime_ns = -1, -1

    cache_key = (
        str(manifest_path),
        int(m_stat.st_mtime_ns),
        int(m_stat.st_ctime_ns),
        int(m_stat.st_size),
        s_mtime_ns,
        s_ctime_ns,
    )
    hit = _WATCH_LAST_SEG_CACHE.get(cache_key)
    if hit is not None:
        return hit

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}, {}
    if not isinstance(manifest, dict):
        return {}, {}
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        return {}, {}
    try:
        started = datetime.fromisoformat(str(manifest.get("started_at") or ""))
    except (TypeError, ValueError):
        started = None
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)

    seg_mtimes: dict[str, float] = {}
    if s_mtime_ns >= 0:
        try:
            with os.scandir(segments_dir) as it:
                for entry in it:
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            continue
                        seg_mtimes[entry.name] = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
        except OSError:
            pass

    by_mtime: dict[str, tuple[float, datetime, dict[str, Any]]] = {}
    by_cov: dict[str, tuple[datetime, dict[str, Any]]] = {}
    device_id = manifest.get("device_id")
    session_id = manifest.get("session_id")

    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            sequence = int(segment.get("sequence"))
            offset = int(segment.get("start_offset_ms"))
            duration = int(segment.get("duration_ms"))
        except (TypeError, ValueError):
            continue
        if sequence < 0 or offset < 0 or duration <= 0:
            continue
        file_name = segment.get("file")
        if not isinstance(file_name, str) or not file_name or Path(file_name).name != file_name:
            file_name = f"segment-{sequence:06d}.m4a"
        mtime = seg_mtimes.get(file_name)
        if mtime is not None:
            try:
                received = datetime.fromtimestamp(mtime, tz=_SHANGHAI_TZ)
            except (OSError, OverflowError, ValueError):
                received = None
            if received is not None:
                day_str = received.date().isoformat()
                prev = by_mtime.get(day_str)
                if prev is None or mtime > prev[0]:
                    by_mtime[day_str] = (
                        mtime,
                        received,
                        {
                            "device_id": device_id,
                            "session_id": session_id,
                            "sequence": sequence,
                            "file": file_name,
                        },
                    )
        if started is None:
            continue
        try:
            end_at = started + timedelta(milliseconds=offset + duration)
        except (OverflowError, ValueError):
            continue
        local_end = end_at.astimezone(_SHANGHAI_TZ)
        day_str = local_end.date().isoformat()
        prev_cov = by_cov.get(day_str)
        if prev_cov is None or local_end > prev_cov[0]:
            by_cov[day_str] = (
                local_end,
                {
                    "device_id": device_id,
                    "session_id": session_id,
                    "sequence": sequence,
                },
            )

    result = (by_mtime, by_cov)
    if len(_WATCH_LAST_SEG_CACHE) >= _WATCH_LAST_SEG_CACHE_MAX:
        _WATCH_LAST_SEG_CACHE.pop(next(iter(_WATCH_LAST_SEG_CACHE)))
    _WATCH_LAST_SEG_CACHE[cache_key] = result
    return result


def _watch_last_segment_received(
    watch_root: Path | str, target_date: str
) -> dict[str, Any]:
    """Return the real last received Watch segment time for one local date.

    Primary evidence is the segment file's filesystem mtime (the actual local
    receipt time).  If no segment file exists, the real coverage tail
    (manifest start + segment offset/duration) is used as a labelled fallback.
    Processing watermarks, request time, and guessed timestamps are never used.
    """
    root = Path(watch_root)
    target = str(target_date or "").strip()
    best_mtime: tuple[float, datetime] | None = None
    best_segment: dict[str, Any] | None = None
    coverage_end: tuple[datetime, dict[str, Any]] | None = None

    for manifest_path in root.glob("*/*/manifest.json"):
        by_mtime, by_cov = _session_last_segment_by_date(manifest_path)
        cand_m = by_mtime.get(target)
        if cand_m is not None and (best_mtime is None or cand_m[0] > best_mtime[0]):
            best_mtime = (cand_m[0], cand_m[1])
            best_segment = dict(cand_m[2])
        cand_c = by_cov.get(target)
        if cand_c is not None and (coverage_end is None or cand_c[0] > coverage_end[0]):
            coverage_end = (cand_c[0], dict(cand_c[1]))

    if best_mtime is not None and best_segment is not None:
        received = best_mtime[1]
        return {
            "last_segment_received_at": received.isoformat(timespec="seconds"),
            "last_segment_received_source": "segment_file_mtime",
            "last_segment_received_reason": None,
            "last_segment": best_segment,
            "last_recording_end_at": (
                coverage_end[0].isoformat(timespec="seconds")
                if coverage_end is not None else None
            ),
            "last_recording_end_source": (
                "coverage_tail" if coverage_end is not None else None
            ),
        }
    if coverage_end is not None:
        return {
            "last_segment_received_at": coverage_end[0].isoformat(timespec="seconds"),
            "last_segment_received_source": "coverage_tail",
            "last_segment_received_reason": (
                "没有可用的 Watch segment 文件 mtime；使用真实 coverage 尾端"
            ),
            "last_segment": coverage_end[1],
            "last_recording_end_at": coverage_end[0].isoformat(timespec="seconds"),
            "last_recording_end_source": "coverage_tail",
        }
    return {
        "last_segment_received_at": None,
        "last_segment_received_source": None,
        "last_segment_received_reason": (
            f"{target} 没有可用的 Watch segment mtime 或 coverage 尾端；"
            "不拿处理水位/请求时间冒充最后录音"
        ),
        "last_segment": None,
        "last_recording_end_at": None,
        "last_recording_end_source": None,
    }

class LocalWebServer(_ServerServicesMixin, _ServerReadsMixin, ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    # [2026-10-29 连接重置修复] 未覆盖时用 socketserver 默认的 5。
    # 本服务 protocol_version = "HTTP/1.0"（每请求一条连接，无 keep-alive），
    # 且每连接起一个线程；并发一高，已三次握手的连接会排在 accept 队列里，
    # 队列满后内核直接丢 SYN/回 RST → 客户端看到
    #   curl (7) Could not connect / curl (56) Recv failure: Connection reset by peer。
    # 生产实测：并发 1×/api/jobs + 50×/api/health/summary 时失败 6 条（health 5 + jobs 1）。
    # 128 对「每请求一线程 + 短连接」的模型是保守值（不是无限放大）。
    request_queue_size = 128

    def handle_error(self, request, client_address) -> None:
        """客户端断连不是服务器错误，不要刷 traceback。

        实测 stderr 累积 859 次 BrokenPipeError + 53 次 ConnectionResetError +
        465 次 "Exception occurred during processing of request"（共 4.0MB 无轮转），
        其中绝大多数是浏览器刷新、手机切网、手表重连这类正常断开。
        真正的异常仍然交给父类打印。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        if isinstance(exc, TimeoutError):
            # [2026-10-29] 原来这里连 TimeoutError 一起静默：大响应写到一半超时被吞，
            # 客户端只看到 IncompleteRead，服务端日志 0 行。现在必须留痕。
            logger.warning("请求处理超时（连接可能已中断）: %s: %s", type(exc).__name__, exc)
            return
        super().handle_error(request, client_address)

    def __init__(
        self,
        address: tuple[str, int],
        store: _jobs.JobStore,
        config_path: Path,
        *,
        access_token: str = "",
        reminder_notifier: ReminderNotifier | None = None,
        watch_local_access_enabled: bool = False,
        processing_mode: str = "combined",
        input_context_manager: Any = None,
    ):
        if processing_mode not in {"combined", "split"}:
            raise ValueError("processing_mode must be combined or split")
        self.processing_mode = processing_mode
        # 这些 stop 标志和线程句柄必须在 super().__init__() 之前建好：
        # socketserver 绑定失败时会在它自己的 __init__ 里回调 server_close()，
        # 而 server_close() 要用到它们。顺序反了会抛
        # AttributeError('_watch_live_stop')，把真正的失败原因（例如端口 8765
        # 已被占用）整个盖掉，现场只剩一个误导性的 traceback。
        # [2026-10-22 P0] 读接口只读快照缓存：(key) -> (monotonic_ts, payload)
        # 只缓存纯计算结果，不涉及写路径；TTL 由调用方给（默认 8s）。
        self._read_cache: dict[str, tuple[float, Any]] = {}
        self._read_cache_lock = threading.Lock()
        # 控制接口快照（version/watch-reception）与业务读缓存解耦，
        # 不允许控制接口被 read producer / 全量 Watch 扫描拖住。
        self._control_cache: dict[str, tuple[float, Any]] = {}
        self._control_cache_lock = threading.Lock()
        self._control_inflight: dict[str, threading.Event] = {}
        # [2026-10-23 W7] 64 条上限：读快照保留近 7 天多接口工作集。
        self._read_cache_max_entries = 64
        # [2026-10-23 P0] 同一个 key 只允许一个 producer 在跑。
        # 浏览器并发/重试时，原来 10+ 个 `/api/daily` 会各自全量扫一遍
        # events/status 文件；它们互相抢 GIL，实测把一次 0.4s 的读放大到 90s。
        self._read_inflight: dict[str, threading.Event] = {}
        # [2026-10-23 P-53] 诊断：谁在跑、谁在等、等了多久。
        self._read_inflight_meta: dict[str, dict[str, Any]] = {}
        self._read_producers_active = 0
        self._read_producers_background = 0
        self._read_slot_waiting = 0
        self._last_formal_asr_invalidate_ts = 0.0
        # [2026-10-23 P-43] 全局 producer 并发上限：single-flight 只保证同一个 key
        # 不重复算；不同日期仍会同时各扫一遍 events，实测 3 个日期并发把 brief
        # 从 0.5s 放大到 5~6s。P-53 把默认名额从 2 提到 6：3 个日期不再排队，
        # 同时仍有上限防止 9778 个 segments 目录被无限并发扫爆。
        # 缓存命中不占槽位；排队超时优先返回旧快照（带 busy 标记）。
        try:
            _read_limit = int(os.environ.get("LIFE_LOGGER_READ_PRODUCER_LIMIT", "6"))
        except (TypeError, ValueError):
            _read_limit = 6
        self._read_producer_limit = max(1, _read_limit)
        try:
            self._read_producer_timeout = float(
                os.environ.get("LIFE_LOGGER_READ_PRODUCER_TIMEOUT", "4")
            )
        except (TypeError, ValueError):
            self._read_producer_timeout = 4.0
        self._read_producer_slots = threading.BoundedSemaphore(self._read_producer_limit)
        # [2026-10-23 P0] 抢到槽位不等于 producer 会结束：46 分钟的 dashboard
        # 证明必须给 producer 本体加 deadline。到点后回收槽位、返回旧快照/503，
        # 绝不让一个死 producer 永久占着 1/6 名额。
        try:
            self._read_producer_run_timeout = float(
                os.environ.get("LIFE_LOGGER_READ_PRODUCER_RUN_TIMEOUT", "4")
            )
        except (TypeError, ValueError):
            self._read_producer_run_timeout = 4.0
        try:
            self._read_producer_background_limit = int(os.environ.get("LIFE_LOGGER_READ_PRODUCER_BACKGROUND_LIMIT", str(self._read_producer_limit * 2)))
        except (TypeError, ValueError):
            self._read_producer_background_limit = self._read_producer_limit * 2
        self._doctor_cache_lock = threading.Lock()
        self._doctor_cache: tuple[str, float, dict[str, Any]] | None = None
        self._watch_live_stop = threading.Event()
        self._activity_timeline_stop = threading.Event()
        self._activity_timeline_thread = None
        self._watch_live_thread = threading.current_thread()
        self._watch_formal_scan_lock = threading.Lock()
        self._watch_status_summary_lock = threading.Lock()
        self._watch_status_summary_cache: dict[str, Any] | None = None
        self._watch_status_summary_cached_at = 0.0
        # None means there is no scanner yet. Keeping the constructor thread here is
        # unsafe: it remains alive after __init__, so the coordinator would mistake it
        # for a running ASR scanner forever.
        self._watch_formal_scan_thread: threading.Thread | None = None
        self._auto_ingest_stop = threading.Event()
        self._reminder_stop = threading.Event()
        self._reminder_thread = threading.current_thread()
        # 惰性导入门面：_Handler 仍在 local_web，patch(core) 也要生效；同时避免模块级环。
        from life_logger import local_web as _lw

        super().__init__(address, _lw._Handler)
        self.store = store
        self.access_token = access_token
        self.watch_local_access_enabled = watch_local_access_enabled
        # Credential for the three Watch-only routes (register / segments / complete). It ships
        # compiled into the Watch app, so it is not a user secret; it exists so those routes no
        # longer accept "anyone on the same network". Rotating it in secrets.json means
        # rebuilding the Watch app — the cost of not making the Watch type a token.
        self.watch_upload_key = _watch._watch_upload_key(self.store.root)
        self.config_path = Path(config_path)
        # [2026-11-07] 让模型入口认这个实例的配置路径（隔离实例不再读生产 local-config）
        try:
            from life_logger import llm_config as _llm_config

            _llm_config.set_config_path(self.config_path)
        except Exception:  # noqa: BLE001
            logger.warning("llm_config override failed; falling back to production config")
        if processing_mode == "split":
            # 读接口继续走 combined 的路径，只把任务执行和后台循环交给 Worker。
            if any(thread.is_alive() for thread in (*store._active.values(), *store._context_recovery_active.values())):
                raise ValueError("cannot move an active in-process store to split processing")
            from life_logger.runtime_queue import ProcessRLock, TaskQueue
            self.store.dispatch_queue = TaskQueue(self.store.root)
            self.store._lock = ProcessRLock(self.store.root / ".job-store.lock")
        self.started_at = _support._iso()
        self.access_log: list[str] = []
        self.passport_receiver_status: dict[str, Any] = {
            "enabled": False,
            "running": False,
        }
        self.passport_receiver_runtime: PassportReceiverRuntime | None = None
        self.watch_session_store = WatchSessionStore(self.store.root / "watch-sessions",
            process_safe=processing_mode != "combined", command_dispatcher=None)
        self.input_context_manager = input_context_manager
        if self.input_context_manager is None:
            try:
                from life_logger.input_context_runtime import InputContextRuntimeManager
                self.input_context_manager = InputContextRuntimeManager(self.watch_session_store.root)
            except Exception as error:
                logger.warning("InputContextRuntimeManager initialization skipped: %s", error)
                self.input_context_manager = None
        self.watch_formal_asr = WatchFormalASRProcessor(
            self.watch_session_store,
            self.config_path,
            self._watch_live_stop,
        )
        from life_logger.activity_timeline_runtime import ActivityTimelineRuntime
        self.activity_timeline = ActivityTimelineRuntime(self.store.root, self.config_path)
        if processing_mode == "combined":
            activity_config = self.activity_timeline.load_config().get("activity_timeline", {})
            if isinstance(activity_config, dict) and activity_config.get("enabled") is True:
                self._activity_timeline_thread = threading.Thread(
                    target=self._activity_timeline_loop, name="activity-timeline-10min", daemon=True,
                )
                self._activity_timeline_thread.start()
        # [2026-10-23 W7b] 诊断开关：默认不设置，行为完全不变。
        # 设 LIFE_LOGGER_DISABLE_WATCH_LOOP=1 才跳过 watch 协调器，
        # 用来做「关后台 vs 开后台」的对照实验。
        if processing_mode == "combined" and not _support._env_flag("LIFE_LOGGER_DISABLE_WATCH_LOOP"):
            self._watch_live_thread = threading.Thread(
                target=self._watch_live_loop, name="watch-formal-asr", daemon=True,
            )
            self._watch_live_thread.start()
        # [2026-10-26] 语音上下文采集：仅当显式配置 input_context_capture.enabled=true 且 combined 模式才启动
        if processing_mode == "combined" and self.input_context_manager is not None:
            try:
                from life_logger.local_pipeline import load_local_config
                cfg = load_local_config(self.config_path)
                capture_cfg = cfg.get("input_context_capture") if isinstance(cfg, Mapping) else None
                if isinstance(capture_cfg, Mapping) and capture_cfg.get("enabled") is True:
                    self.input_context_manager.start()
            except Exception as error:
                logger.warning("input_context_capture startup failed: %s", error)
        # 【2026-10-17】预热 VIP 常驻转写进程（SenseVoice-Small）。
        # 为什么：模型加载约 9 秒。不预热的话，用户第一次双捏触发要干等 9 秒，
        # 「快速通道」名不副实；预热后热转写实测 0.3~0.4 秒。
        # 放后台线程：启动不能被它阻塞，失败也不影响服务起来。
        # split 模式下 VIP 路由仍在 Web 里转写，预热也留在 Web。
        if processing_mode in {"combined", "split"} and not _support._env_flag("LIFE_LOGGER_DISABLE_VIP_WARMUP"):
            threading.Thread(
                target=self._warm_vip_transcriber, name="vip-transcriber-warmup", daemon=True,
            ).start()
        if (
            processing_mode in {"combined", "split"}
            and self.store.root.name == "local-web"
            and not _support._env_flag("LIFE_LOGGER_DISABLE_READ_WARMUP")
        ):
            threading.Thread(
                target=self._warm_recent_days_read_cache, name="read-cache-warmup", daemon=True,
            ).start()
        self.reminders = LocalReminderStore(
            self.store.root,
            notifier=reminder_notifier,
        )
        # [2026-10-13] 习惯的唯一事实源：habits.json。与待办（OpenClaw）完全分开 ——
        # 待办有终态，习惯天天重置；详见 local_habits.py 顶部说明。
        self.habits = LocalHabitStore(self.store.root)
        if processing_mode == "combined" and not _support._env_flag("LIFE_LOGGER_DISABLE_REMINDERS"):
            self._reminder_thread = threading.Thread(
                target=self._reminder_loop,
                name="life-logger-local-reminders",
                daemon=True,
            )
            self._reminder_thread.start()
        if processing_mode == "combined" and not _support._env_flag("LIFE_LOGGER_DISABLE_AUTO_INGEST"):
            self._auto_ingest_thread = threading.Thread(
                target=self._auto_ingest_loop,
                name="life-logger-auto-ingest",
                daemon=True,
            )
            self._auto_ingest_thread.start()
        self._recovery_thread: threading.Thread | None = None
        if processing_mode == "combined" and self.store.startup_recovery_ids:
            self._start_recovery_thread(self.store.startup_recovery_ids)


    def server_close(self) -> None:
        # 必须对「初始化中途被调用」和「被调用两次」都免疫：
        # socketserver.TCPServer.__init__ 在绑定失败时会回调本方法，而
        # socketserver 自身也可能重复调用。任何一处未建好的属性都不能让
        # 关闭流程抛异常，否则会盖掉真正的启动失败原因。
        for name in ("_watch_live_stop", "_auto_ingest_stop", "_reminder_stop", "_activity_timeline_stop"):
            stop = getattr(self, name, None)
            if isinstance(stop, threading.Event):
                stop.set()
        if getattr(self, "input_context_manager", None) is not None:
            try:
                self.input_context_manager.stop()
            except Exception as error:
                logger.warning("input_context_manager stop failed: %s", error)
        for name, thread_name in (
            ("_activity_timeline_thread", "activity-timeline-10min"),
            ("_watch_live_thread", "watch-formal-asr-coordinator"),
            ("_watch_formal_scan_thread", "watch-formal-asr-segment"),
            ("_reminder_thread", "life-logger-local-reminders"),
            ("_auto_ingest_thread", "life-logger-auto-ingest"),
        ):
            thread = getattr(self, name, None)
            if (
                isinstance(thread, threading.Thread)
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=7)
        super().server_close()
        super().server_close()

def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    store: _jobs.JobStore | None = None,
    config_path: Path | None = None,
    access_token: str = "",
    lan_access_enabled: bool = False,
    reminder_notifier: ReminderNotifier | None = None,
    processing_mode: str = "combined",
    input_context_manager: Any = None,
) -> LocalWebServer:
    normalized_host = "127.0.0.1" if host == "localhost" else host
    allowed_nonlocal = normalized_host == "0.0.0.0"
    if normalized_host not in {"127.0.0.1", "0.0.0.0"}:
        allowed_nonlocal = (
            _normalize_trusted_network_bind(normalized_host) == normalized_host
        )
    if normalized_host != "127.0.0.1" and not allowed_nonlocal:
        raise ValueError("Life Logger 仅允许监听 localhost、局域网或受信私网地址")
    if normalized_host != "127.0.0.1" and not lan_access_enabled:
        raise ValueError("局域网访问未启用；拒绝监听非 localhost 地址")
    return LocalWebServer(
        (normalized_host, port),
        store or _jobs.JobStore(recover=processing_mode == "combined"),
        config_path or PROJECT_ROOT / "local-config.json",
        access_token=access_token,
        reminder_notifier=reminder_notifier,
        processing_mode=processing_mode,
        watch_local_access_enabled=_watch._watch_direct_access_allowed(
            normalized_host,
            lan_access_enabled,
        ),
        input_context_manager=input_context_manager,
    )

def _passport_receiver_options(config_path: Path) -> dict[str, Any]:
    # The AI Passport HTTP receiver used to start unconditionally on 0.0.0.0 with no
    # authentication at all: anything on the same network could write files to disk and, via
    # /v1/audio/end, start an ASR job. It is no longer part of any shipped flow — the device is
    # used as a wireless microphone through the BLE bridge, and the last stream here was
    # 2026-09-25 — so it is off and loopback-only unless a config explicitly turns it on.
    options: dict[str, Any] = {
        "enabled": False,
        "host": "127.0.0.1",
        "port": PASSPORT_STREAM_PORT,
        "discovery_port": PASSPORT_DISCOVERY_PORT,
        "auto_start": False,
    }
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    configured = payload.get("passport_stream") if isinstance(payload, dict) else None
    if isinstance(configured, dict):
        options.update(
            {
                key: configured[key]
                for key in options
                if key in configured
            }
        )
    options["enabled"] = bool(options["enabled"])
    options["auto_start"] = bool(options["auto_start"])
    host = str(options["host"] or "0.0.0.0").strip()
    if host == "localhost":
        host = "127.0.0.1"
    if host not in {"0.0.0.0", "127.0.0.1"}:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError("Passport receiver host 必须是本机 IPv4") from error
        if address.version != 4 or not address.is_private:
            raise ValueError("Passport receiver 拒绝公网监听地址")
    options["host"] = host
    for key in ("port", "discovery_port"):
        value = int(options[key])
        if not 1 <= value <= 65535:
            raise ValueError(f"passport_stream.{key} 必须在 1..65535")
        options[key] = value
    return options

def _start_passport_receiver_for_web(
    store: _jobs.JobStore,
    config_path: Path,
) -> tuple[PassportReceiverRuntime | None, dict[str, Any]]:
    from life_logger import local_web as _lw

    options = _passport_receiver_options(config_path)
    status: dict[str, Any] = {
        "enabled": bool(options["enabled"]),
        "running": False,
        "host": options["host"],
        "http_port": options["port"],
        "discovery_port": options["discovery_port"],
        "auto_start": bool(options["auto_start"]),
        "capture_source": CAPTURE_SOURCE,
    }
    if not options["enabled"]:
        return None, status

    def import_complete(session_dir: Path, summary: dict[str, Any]) -> str | None:
        result = store.import_passport_recording(
            session_dir,
            summary,
            config_path,
            auto_start=bool(options["auto_start"]),
        )
        return str(result["job_id"])

    try:
        runtime = _lw.start_passport_receiver(
            host=str(options["host"]),
            port=int(options["port"]),
            discovery_port=int(options["discovery_port"]),
            output_root=store.root / "passport-stream",
            on_complete=import_complete,
        )
    except OSError as error:
        status["error"] = str(error)
        return None, status
    status.update(runtime.status())
    status["auto_start"] = bool(options["auto_start"])
    return runtime, status

def serve(
    *,
    host: str | None = None,
    port: int = 8765,
    config_path: Path | None = None,
    open_browser: bool = True,
    processing_mode: str = "combined",
    manage_worker: bool = True,
    root: Path | None = None,
) -> None:
    # The lease must guard the actual data root, not the production default:
    # drills/gray roots otherwise get no second-coordinator protection.
    effective_root = Path(root).expanduser().resolve() if root is not None else _jobs.DEFAULT_JOB_ROOT
    if processing_mode == "combined":
        # Prevent a rollback/second coordinator from sharing the same state
        # while an independent worker still owns processing.
        from life_logger.runtime_queue import WorkerLease
        with WorkerLease(effective_root):
            return _serve_runtime(
                host=host, port=port, config_path=config_path,
                open_browser=open_browser, processing_mode=processing_mode,
                manage_worker=manage_worker, root=effective_root,
            )
    return _serve_runtime(
        host=host, port=port, config_path=config_path,
        open_browser=open_browser, processing_mode=processing_mode,
        manage_worker=manage_worker, root=effective_root,
    )

def _serve_runtime(
    *,
    host: str | None = None,
    port: int = 8765,
    config_path: Path | None = None,
    open_browser: bool = True,
    processing_mode: str = "combined",
    manage_worker: bool = True,
    root: Path | None = None,
) -> None:
    store = _jobs.JobStore(
        Path(root).expanduser().resolve() if root is not None else _jobs.DEFAULT_JOB_ROOT,
        recover=processing_mode == "combined",
    )
    effective_config_path = Path(config_path or PROJECT_ROOT / "local-config.json")
    preferences = _read_preferences(store.root)
    lan_access_enabled = bool(preferences.get("lan_access_enabled", False))
    selected_host = host or _desired_mobile_bind(preferences)
    access_token = _auth._ensure_mobile_access_token(store.root)
    try:
        server = create_server(
            host=selected_host,
            port=port,
            store=store,
            config_path=effective_config_path,
            access_token=access_token,
            lan_access_enabled=lan_access_enabled,
            processing_mode=processing_mode,
        )
    except OSError:
        # Fallback to wildcard or localhost if the specific trusted IP is no longer assigned
        fallback_host = "0.0.0.0" if lan_access_enabled else "127.0.0.1"
        server = create_server(
            host=fallback_host,
            port=port,
            store=store,
            config_path=effective_config_path,
            access_token=access_token,
            lan_access_enabled=lan_access_enabled,
            processing_mode=processing_mode,
        )
        selected_host = fallback_host
    from life_logger.runtime_launch import publish_web_role, ensure_worker
    publish_web_role(store.root, mode=processing_mode, port=server.server_port)
    if processing_mode == "split" and manage_worker:
        ensure_worker(store.root, effective_config_path)
    passport_runtime, passport_status = _start_passport_receiver_for_web(
        store,
        effective_config_path,
    )
    server.passport_receiver_status = passport_status
    server.passport_receiver_runtime = passport_runtime
    browser_host = (
        "127.0.0.1"
        if selected_host in {"127.0.0.1", "0.0.0.0"}
        else selected_host
    )
    url = f"http://{browser_host}:{server.server_port}/"
    paired_url = f"{url}?token={quote(access_token)}"
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(paired_url,)).start()
    if selected_host == "127.0.0.1":
        scope = "仅本机"
    elif selected_host == "0.0.0.0":
        scope = "局域网"
    else:
        scope = f"受信私网 {selected_host}"
    print(f"Life Logger 网页已启动：{url}（{scope}）", flush=True)
    if passport_status.get("running"):
        print(
            "AI Passport 自动接收已启动："
            f"TCP {passport_status['http_port']} / "
            f"UDP {passport_status['discovery_port']}",
            flush=True,
        )
    elif passport_status.get("enabled"):
        print(
            "AI Passport 自动接收启动失败："
            f"{passport_status.get('error') or 'unknown error'}",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if passport_runtime is not None:
            passport_runtime.close()
        server.server_close()

