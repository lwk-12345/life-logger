"""HTTP 请求处理器（S7 从 local_web.py 原样搬出）。

`_Handler` 体内的本地名字统一通过门面模块属性 `_lw.<name>` 运行时读取，
保证 `monkeypatch.setattr("life_logger.local_web.<name>", ...)` 真正生效。
"""
from __future__ import annotations

import json
import math
import mimetypes
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from life_logger import local_web as _lw
from life_logger.watch_stream import (
    MAX_WATCH_SEGMENT_BYTES,
    IncompleteWatchSessionError,
    WatchSessionChecksumMismatchError,
    WatchSessionConflictError,
    WatchSessionError,
    WatchSessionNotFoundError,
    WatchSessionProcessingError,
)
from life_logger.web_common import PROJECT_ROOT, logger
from life_logger.web_preferences import _read_preferences
from life_logger.web_routes_debug import _DebugRoutesMixin
from life_logger.web_routes_events import _EventRoutesMixin
from life_logger.web_routes_settings import _SettingsRoutesMixin
from life_logger.web_routes_watch import _WatchRoutesMixin

from . import job_store as _jobs
from . import web_auth as _auth
from . import web_startup as _startup
from . import web_support as _support
from . import watch_upload as _watch

import threading as _threading
import time as _time

# [2026-11-08] 大响应（活动时间线 1.6–2.8 MB、录音覆盖 4.8 MB）的**序列化字节**缓存。
# 线上实测切一天时同一接口被并发打 2–3 次，每次都要重新读盘+投影+json.dumps，
# 互相抢 GIL 拖到 10–30 秒。这里按 (key, 指纹) 缓存最终字节，同 key 单飞：
# 并发请求只算一次，其余等它；指纹（文件 mtime + 读缓存代数）变了立刻重算。
_BYTES_CACHE: dict[str, tuple[float, Any, bytes]] = {}
_BYTES_CACHE_LOCK = _threading.Lock()
_BYTES_KEY_LOCKS: dict[str, Any] = {}
_BYTES_CACHE_MAX = 24


def _cached_json_bytes(key: str, fingerprint: Any, ttl: float, producer) -> bytes:
    def _hit() -> bytes | None:
        with _BYTES_CACHE_LOCK:
            hit = _BYTES_CACHE.get(key)
        if hit is not None and hit[1] == fingerprint and _time.monotonic() - hit[0] <= ttl:
            return hit[2]
        return None

    body = _hit()
    if body is not None:
        return body
    with _BYTES_CACHE_LOCK:
        key_lock = _BYTES_KEY_LOCKS.setdefault(key, _threading.Lock())
    with key_lock:
        body = _hit()
        if body is not None:
            return body
        body = json.dumps(producer(), ensure_ascii=False).encode("utf-8")
        with _BYTES_CACHE_LOCK:
            _BYTES_CACHE[key] = (_time.monotonic(), fingerprint, body)
            while len(_BYTES_CACHE) > _BYTES_CACHE_MAX:
                oldest = min(_BYTES_CACHE, key=lambda k: _BYTES_CACHE[k][0])
                _BYTES_CACHE.pop(oldest, None)
        return body


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0
_VERSION_BADGE_SCRIPT = """
(function () {
  var info = __BUILD_INFO__;
  if (!info || !info.app_version) return;
  // 保留 "未部署" 注释供回归测试识别 stale 分支
  var LABELS = {
    current: { dot: "\u25cf", dotColor: "#2e7d4f", text: "服务正常" },
    stale:   { dot: "\u2191", dotColor: "#b45309", text: "有更新待加载" },
    unknown: { dot: "\u25cb", dotColor: "#8a8578", text: "状态未知" }
  };
  function loadedClock() {
    var raw = String(info.deployed_at || "");
    if (!/[zZ]$|[+-][0-9]{2}:[0-9]{2}$/.test(raw)) return "";
    var instant = new Date(raw);
    if (!Number.isFinite(instant.getTime())) return "";
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    }).format(instant);
  }
  function state() {
    if (info.deploy_state === "stale") return "stale";
    if (info.deploy_state === "current" && !info.degraded) return "current";
    return "unknown";
  }
  function mount() {
    if (document.getElementById("life-logger-version-badge")) return;
    var key = state();
    var label = LABELS[key];
    var clock = loadedClock();

    var el = document.createElement("span");
    el.id = "life-logger-version-badge";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.style.cssText = [
      "display:inline-flex", "align-items:center", "gap:6px",
      "font:500 11.5px/1.4 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif",
      "color:#6e7570", "padding:2px 4px"
    ].join(";");

    var dot = document.createElement("span");
    dot.setAttribute("aria-hidden", "true");
    dot.style.cssText = "color:" + label.dotColor + ";font-size:10px;";
    dot.textContent = label.dot;

    var txt = document.createElement("span");
    txt.textContent = label.text + (clock ? " · " + clock : "");

    el.appendChild(dot);
    el.appendChild(txt);
    el.title = ["v" + info.app_version, label.text, clock ? ("加载于 " + clock) : ""].filter(Boolean).join(String.fromCharCode(10));

    var sidebarFoot = document.getElementById("sidebar-service-status");
    var title = document.getElementById("page-title");
    var host = sidebarFoot || (title && title.parentNode ? title.parentNode : null);
    if (sidebarFoot) {
      var bindSpan = document.getElementById("sidebar-bind-status");
      if (bindSpan) bindSpan.style.display = "none";
      var dotSpan = document.getElementById("sidebar-status-dot");
      if (dotSpan) dotSpan.style.display = "none";
    }
    if (host) {
      host.appendChild(el);
    } else {
      document.body.insertBefore(el, document.body.firstChild);
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
})();
"""

class _Handler(_support.ResponseHelpersMixin, _auth.AuthHelpersMixin, _watch.WatchUploadHelpersMixin, _WatchRoutesMixin, _EventRoutesMixin, _SettingsRoutesMixin, _DebugRoutesMixin, BaseHTTPRequestHandler):
    server_version = "LifeLoggerLocal/0.1"
    protocol_version = "HTTP/1.0"

    @property
    def app(self) -> "_startup.LocalWebServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        status = str(args[1]) if len(args) > 1 else "-"
        path = urlparse(self.path).path
        self.app.access_log.append(f"{self.command} {path} {status}")
        self.app.access_log[:] = self.app.access_log[-80:]








    # [2026-10-29 /api/jobs 截断] 大响应必须「全量送达」或「显式报错」，不许静默半截。
    # 现场：/api/jobs 3.8MB 与 /app.js 542KB 都在 ~327KB（≈ 套接字发送缓冲）处被客户端读到
    # IncompleteRead；Content-Length 正确但连接提前关闭。旧写法直接 `self.wfile.write(body)`：
    #   1) 若连接被前序处理器留在非阻塞/带超时状态，sendall 在缓冲区满时立刻抛错 → 半截；
    #   2) 异常被 `handle_error` 的 TimeoutError 分支静默吞掉 → 日志里没有任何痕迹。
    # 这里：强制阻塞 + 明确超时 + 按已发送字节循环 send；失败时 WARNING 带 path/label/expected/sent。
    RESPONSE_WRITE_TIMEOUT_SECONDS = 60.0


    # ────────────────────────────────────────── 看板 → OpenClaw 同步
    def _kanban_openclaw(self):
        """懒建同步器（每次读都是同一份映射文件，成本只有一次 JSON 读）。"""
        from life_logger.kanban_openclaw_sync import KanbanOpenClawSync

        return KanbanOpenClawSync(self.app.store.root)

    def _sync_kanban_openclaw(self, *, task_key: str, status: str, title=None,
                              event_id=None, artifact_path=None) -> dict[str, Any]:
        """把一次看板改动同步到 OpenClaw。**绝不让同步失败影响本地写入**。

        失败时返回 {"synced": False, "queued": True, "error": "..."}，
        前端据此显示"未同步到 OpenClaw"；恢复后 `/api/tasks` 读取时会自动补。
        """
        try:
            return self._kanban_openclaw().sync_change(
                task_key=task_key,
                status=status,
                title=title,
                event_id=event_id,
                artifact_path=artifact_path,
            )
        except Exception as error:  # noqa: BLE001 - 同步是附加动作，不能带崩写入
            _lw.sys.stderr.write(
                f"[life-logger] OpenClaw 同步异常 task_key={task_key}: {type(error).__name__} {error}\n"
            )
            _lw.sys.stderr.flush()
            return {"synced": False, "queued": False, "error": f"{type(error).__name__}"}


    def _send_json_attachment(self, payload: Any, filename: str) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body, label=f"attachment:{filename}")

    def _body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            raise ValueError("请求过大")
        body = self.rfile.read(length)
        if not body:
            return {}
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON 请求必须是对象")
        self._last_json_body = value
        return value

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._require_api_auth():
            return
        try:
            watch_parts = parsed.path.strip("/").split("/")
            if len(watch_parts) == 4 and watch_parts[:2] == ["api", "watch-sessions"]:
                manifest = self.app.watch_session_store.load_session(
                    watch_parts[2],
                    watch_parts[3],
                )
                self._send_json({"session": manifest})
                return
            # 【2026-10-22 P-32】断点续传：已收/缺失分段查询（只读，仅序号与计数）
            if (
                len(watch_parts) == 5
                and watch_parts[:2] == ["api", "watch-sessions"]
                and watch_parts[4] == "received-segments"
            ):
                try:
                    report = self.app.watch_session_store.segment_gap_report(
                        watch_parts[2], watch_parts[3]
                    )
                except WatchSessionNotFoundError:
                    self._send_json(
                        {"error": "watch_session_not_found"}, HTTPStatus.NOT_FOUND
                    )
                    return
                except WatchSessionError as error:
                    message = str(error)
                    if "invalid device or session id" in message:
                        self._send_json(
                            {"error": "invalid_watch_session_id"},
                            HTTPStatus.BAD_REQUEST,
                        )
                    else:
                        self._send_json({"error": message}, error.status_code)
                    return
                self._send_json(report)
                return
            if parsed.path == "/api/watch-health":
                return self._get_watch_health(parsed)
            if parsed.path == "/api/watch-coverage":
                def _coverage_payload() -> dict[str, Any]:
                    intervals = self.app.watch_session_store.list_coverage_intervals()
                    try:
                        devices = self.app.watch_session_store.list_device_statuses()
                    except Exception:  # 设备状态坏了不能拖垮 1.7MB 的覆盖响应
                        devices = []
                    return {
                        "intervals": intervals,
                        "device_offline": _lw._device_offline_summary(devices),
                    }
                # 覆盖不分日期、4.8MB：20 秒内复用同一份字节（前端本身也缓存 60 秒）。
                self._send_prepared_json(_cached_json_bytes(
                    f"watch-coverage|{self.app.watch_session_store.root}", None, 20.0, _coverage_payload,
                ))
                return
            if parsed.path == "/api/health/summary":
                query_params = parse_qs(parsed.query)
                target_date = (query_params.get("date") or [_lw.datetime.now(tz=_lw.LOCAL_TZ).strftime("%Y-%m-%d")])[0]
                from life_logger.health_store import HealthDataStore
                hstore = HealthDataStore(self.app.store.root / "health")
                summary = hstore.get_daily_summary(target_date)
                latest_hr = hstore.get_latest_heart_rate()
                try:
                    device_offline = _lw._device_offline_summary(
                        self.app.watch_session_store.list_device_statuses())
                except Exception:
                    device_offline = _lw._device_offline_summary([])
                self._send_json({"ok": True, "summary": summary.to_dict(), "latest_heart_rate": latest_hr,
                                 "device": device_offline})
                return
            if parsed.path == "/api/dashboard/daily":
                return self._get_dashboard_daily(parsed)
            if parsed.path == "/api/timeline-scenes":
                return self._get_timeline_scenes(parsed)
            if parsed.path == "/api/quick-captures":
                query_params = parse_qs(parsed.query)
                target_date = (query_params.get("date") or [None])[0]
                from life_logger.voice_triggers import QuickCaptureStore
                cstore = QuickCaptureStore(self.app.store.root / "quick_captures.json")
                if target_date:
                    captures = cstore.filter_by_date(target_date)
                else:
                    captures = cstore.list_all()
                self._send_json({"ok": True, "captures": captures})
                return
            if parsed.path == "/api/watch-live-events":
                return self._get_watch_live_events(parsed)
            if parsed.path == "/api/settings":
                return self._get_settings(parsed)
            if parsed.path == "/api/settings/mobile-token":
                return self._get_settings_mobile_token(parsed)
            if parsed.path == "/api/mobile-capture":
                preferences = _read_preferences(self.app.store.root)
                self._send_json(_auth._mobile_capture_config(preferences))
                return
            if parsed.path == "/api/settings/stage-models":
                return self._get_settings_stage_models(parsed)
            if parsed.path == "/api/decision-comparisons":
                from life_logger.decision_history import collect_comparisons

                self._send_json(collect_comparisons(
                    self.app.store.root, date=parse_qs(parsed.query).get("date", [""])[0],
                ))
                return
            if parsed.path == "/api/version":
                return self._get_version(parsed)
            if parsed.path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "local_first": True,
                        "server_build_id": _lw.SERVER_BUILD_ID,
                        "server_started_at": self.app.started_at,
                        "project_root": str(PROJECT_ROOT),
                        "api_features": list(_startup.API_FEATURES),
                        "processing_mode": getattr(self.app, "processing_mode", "combined"),
                        "processing": self.app.processing_health() if getattr(self.app, "processing_mode", "combined") == "split" else {"embedded": True},
                        # 【2026-10-19】版本块：`/api/version` 同源，健康检查即可看出是否落后于磁盘
                        "version": {
                            **self.app.control_snapshot(
                                "version", self.app._fast_version_info, ttl=30.0
                            ),
                        },
                    }
                )
                return
            if parsed.path == "/api/doctor":
                self._send_json(self.app.doctor_report())
                return
            if parsed.path == "/api/backup":
                self._serve_state_backup()
                return
            if parsed.path == "/api/data-health":
                report = self.app.store.data_health()
                query = parse_qs(parsed.query)
                if str(query.get("download", ["0"])[0]).lower() in {
                    "1",
                    "true",
                    "yes",
                }:
                    stamp = _lw.datetime.now(tz=_lw.LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
                    filename = f"life-logger-data-health-{stamp}.json"
                    self._send_json_attachment(report, filename)
                else:
                    self._send_json(report)
                return
            if parsed.path == "/api/auto-ingest":
                self._send_json(self.app.store.auto_ingest_status())
                return
            if parsed.path == "/api/watch-sessions/diagnostics":
                # [2026-10-24 Q144] 只读诊断：为什么停录、哪些 completion 没被确认。
                # 不改任何文件；数据来自 manifest + completion-acks.jsonl。
                query = parse_qs(parsed.query)
                try:
                    recent = int(query.get("recent", ["20"])[0])
                except (TypeError, ValueError):
                    recent = 20
                recent = max(1, min(recent, 200))
                try:
                    payload = self.app.watch_session_store.stop_diagnostics(recent=recent)
                except Exception as exc:  # noqa: BLE001 - 诊断端点不允许拖垮服务
                    self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                    HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json({"ok": True, **payload})
                return
            if parsed.path == "/api/watch-reception":
                return self._get_watch_reception(parsed)
            if parsed.path == "/api/passport-sessions":
                return self._get_passport_sessions(parsed)
            if parsed.path == "/api/passport-sessions/audio":
                query = parse_qs(parsed.query)
                device = unquote(query.get("device", [""])[0])
                session = unquote(query.get("session", [""])[0])
                self._serve_passport_audio(device, session)
                return
            if parsed.path == "/api/passport/cards":
                return self._get_passport_cards(parsed)
            if parsed.path == "/api/access-log":
                # Recent request lines (method path status), from memory. The process writes its
                # access log to a buffered stdout, so this is the only way to see *why* a device
                # request was refused without restarting the service.
                self._send_json({"lines": list(self.app.access_log)})
                return
            if parsed.path == "/api/debug/read-cache":
                return self._get_debug_read_cache(parsed)
            if parsed.path == "/api/debug/read-inflight":
                return self._get_debug_read_inflight(parsed)
            if parsed.path == "/api/jobs":
                # 1 秒控制快照：避免同一时刻的 Watch 扫描/写任务把列表拖成长读。
                jobs, jobs_cached = self.app.control_snapshot_meta(
                    "jobs|list", self.app.store.list, ttl=1.0
                )
                visible_jobs = [
                    job
                    for job in jobs
                    if not (
                        isinstance(job.get("watch_pending_session"), dict)
                        and not job.get("files")
                    )
                ]
                self._send_json({"jobs": visible_jobs, "cached": jobs_cached})
                return
            if parsed.path == "/api/tasks":
                return self._get_tasks(parsed)
            if parsed.path == "/api/openclaw/health":
                try:
                    from life_logger.openclaw_bridge import OpenClawBridgeClient

                    client = OpenClawBridgeClient()
                    self._send_json({"ready": client.is_healthy(), "base_url": client.base_url})
                except Exception as error:
                    self._send_json({"ready": False, "error": str(error)})
                return
            if parsed.path == "/api/openclaw/tasks":
                return self._get_openclaw_tasks(parsed)
            if parsed.path == "/api/reminders":
                # 事实源（OpenClaw / reminders.json）不动；只在返回层隐去
                # dictation（电脑前说话）来源的待办，见 life_logger.dictation。
                self._send_json(
                    {
                        "backend": "local",
                        "reminders": self.app.filter_reminders_dictation(
                            self.app.sync_local_reminders()
                        ),
                    }
                )
                return
            if parsed.path == "/api/habits":
                # 习惯的唯一事实源（habits.json）的只读投影。网页看板、徽章第 3 屏、
                # 微信汇报都从这里读同一份数据；写入口只有下面的 /api/habits/*。
                query = parse_qs(parsed.query)
                target_date = (query.get("date") or [None])[0]
                include_inactive = str(
                    (query.get("include_inactive") or ["0"])[0]
                ).lower() in {"1", "true", "yes"}
                snapshot = self.app.habits.snapshot(
                    today=target_date,
                    include_inactive=include_inactive,
                )
                snapshot["badge_slots"] = self.app.habits.habits_for_badge(
                    today=target_date
                )
                self._send_json({"ok": True, **snapshot})
                return
            if parsed.path == "/api/nutrition/day":
                return self._get_nutrition_day(parsed)
            if parsed.path == "/api/nutrition/library":
                # [2026-10-13] 自建品牌库（四层查询链第①层）的只读投影：用户念过的 + OFF 写回的。
                # 只读，供看板与排查用；写入口是同层的 /api/nutrition/voice。
                from life_logger.food_lookup import default_food_lookup

                lookup = default_food_lookup()
                entries = lookup.brand.all_entries()
                self._send_json(
                    {
                        "ok": True,
                        "count": len(entries),
                        "seeded": len([entry for entry in entries if entry.get("seed")]),
                        "library_path": str(lookup.brand.path) if lookup.brand.path else "",
                        "data_dir": str(lookup.data_dir),
                        "entries": entries,
                    }
                )
                return
            if parsed.path == "/api/review-questions":
                from life_logger.review_questions import ReviewQuestionError, get_review_question
                query = parse_qs(parsed.query)
                try:
                    result = get_review_question(
                        self.app.store.root,
                        source_id=str((query.get("source_id") or [""])[0]),
                    )
                except ReviewQuestionError as error:
                    self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/judgment-queue":
                from life_logger.judgment_queue import build_queue
                query = parse_qs(parsed.query)
                self._send_json(build_queue(
                    self.app.store.root,
                    purpose=str((query.get("purpose") or [""])[0]),
                    include_older=str((query.get("older") or ["0"])[0]) == "1",
                ))
                return
            if parsed.path == "/api/pending-sources":
                return self._get_pending_sources(parsed)
            if parsed.path == "/api/daily-brief":
                return self._get_daily_brief(parsed)
            if parsed.path == "/api/activity-digest":
                from life_logger.activity_digest import read_cached, trigger_generation, valid_day
                day = parse_qs(parsed.query).get("date", [_lw.datetime.now(_lw.LOCAL_TZ).date().isoformat()])[0]
                try:
                    valid_day(day)
                except ValueError:
                    self._send_json({"error": "invalid_date"}, status=400)
                    return
                cached = read_cached(self.app.store.root, day)
                scheduled = trigger_generation(self.app.store.root, day)
                self._send_json({**cached, "generation_scheduled": scheduled})
                return
            if parsed.path == "/api/activity-timeline/status":
                runtime = self.app.activity_timeline
                self._send_json({"schema": "activity-timeline-status-v1",
                                 "busy": runtime._thread_lock.locked(),
                                 "cadence_minutes": 10,
                                 "last_tick": getattr(self.app, "_activity_timeline_last_tick", None)})
                return
            if parsed.path == "/api/activity-timeline":
                query = parse_qs(parsed.query)
                day = query.get("date", [""])[0]
                if not day:
                    day = _lw.datetime.now(_lw.LOCAL_TZ).date().isoformat()
                try:
                    if _lw.datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                        raise ValueError("invalid date")
                except ValueError:
                    self._send_json({"error": "invalid_date"}, status=400)
                    return
                # Reading this view must never schedule model work.
                # [2026-11-03] 文件损坏必须 500 + 明确原因，**绝不能**伪装成
                # 「200 + activities=[]」（那样前端只会显示"这一天没有活动"）。
                from life_logger.activity_timeline_runtime import TimelineFileCorrupt

                def _timeline_payload() -> dict[str, Any]:
                    payload = self.app.activity_timeline.read_day(day)
                    from life_logger.activity_fragments import project_for_read as _fragment_projection

                    return _fragment_projection(payload, self.app.store.root, day)

                try:
                    sync = getattr(self.app, "_sync_external_write_epoch", None)
                    if callable(sync):
                        sync()
                    route_dir = self.app.store.root / "activity-timeline" / "jev"
                    fingerprint = (
                        _support.read_cache_epoch(self.app.store.root),
                        _mtime_ns(route_dir / f"{day}.json"),
                        _mtime_ns(route_dir / f"{day}.corrections.json"),
                    )
                    is_today = day == _lw.datetime.now(_lw.LOCAL_TZ).date().isoformat()
                    body = _cached_json_bytes(
                        f"activity-timeline|{self.app.store.root}|{day}", fingerprint,
                        30.0 if is_today else 600.0, _timeline_payload,
                    )
                except TimelineFileCorrupt as corrupt:
                    logger.error("activity timeline corrupt: %s", corrupt)
                    self._send_json(corrupt.to_dict(), status=500)
                    return
                self._send_prepared_json(body)
                return
            if parsed.path == "/api/daily":
                return self._get_daily(parsed)
            if parsed.path == "/api/daily-health":
                # 【2026-10-19 P-14】日报健康度：缺日报 / 降级产出，原来两件事用户都看不见。
                query = parse_qs(parsed.query)
                try:
                    days = int(query.get("days", ["14"])[0])
                except (TypeError, ValueError):
                    days = 14
                try:
                    payload = _lw.build_daily_health(self.app.store.root, days=days)
                except Exception as exc:
                    self._send_json(
                        {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                        status=500,
                    )
                    return
                payload["ok"] = True
                self._send_json(payload)
                return
            if parsed.path == "/api/capture-funnel":
                # [2026-10-16] 信息漏斗（轻量端点）：只回答「这一天说了多少句」。
                # 为什么单独开一个而不是复用 /api/daily：日记页已经有自己的数据，
                # 不该为了显示一个数字去跑整个 daily brief（含 5 个 ledger）。
                query = parse_qs(parsed.query)
                target_date = unquote(query.get("date", [""])[0]) or _lw.datetime.now(
                    tz=_lw.ZoneInfo("Asia/Shanghai")
                ).strftime("%Y-%m-%d")
                from life_logger.capture_funnel import compute_capture_funnel

                try:
                    funnel = compute_capture_funnel(self.app.store.root, target_date)
                except Exception as exc:  # 指标算挂了不能拖垮日记页
                    funnel = {"error": f"{type(exc).__name__}: {exc}", "date": target_date}
                    print(
                        f"[life-logger] capture_funnel 计算失败 date={target_date}: {exc}",
                        flush=True,
                    )
                self._send_json({"ok": True, "capture_funnel": funnel})
                return
            if parsed.path == "/api/patterns":
                query = parse_qs(parsed.query)
                today = _lw.datetime.now(tz=_lw.ZoneInfo("Asia/Shanghai")).date()
                default_from = (today - _lw.timedelta(days=13)).isoformat()
                date_from = unquote(query.get("from", [default_from])[0])
                date_to = unquote(query.get("to", [today.isoformat()])[0])
                self._send_json(
                    _lw.build_pattern_observations(
                        self.app.store.root,
                        date_from=date_from,
                        date_to=date_to,
                    )
                )
                return
            if parsed.path == "/api/expense/pending-broadcast":
                return self._get_expense_pending_broadcast(parsed)
            if parsed.path == "/api/expense-range":
                return self._get_expense_range(parsed)
            if parsed.path == "/api/stats":
                query = parse_qs(parsed.query)
                today = _lw.datetime.now(tz=_lw.ZoneInfo("Asia/Shanghai")).date()
                default_from = (today - _lw.timedelta(days=6)).isoformat()
                date_from = unquote(query.get("from", [default_from])[0])
                date_to = unquote(query.get("to", [today.isoformat()])[0])
                scope = unquote(query.get("scope", ["provisional"])[0])
                view = unquote(query.get("view", ["display"])[0])
                def _produce_stats():
                    try:
                        return _lw.build_range_statistics(
                            self.app.store.root, date_from=date_from, date_to=date_to,
                            scope=scope, view=view,
                            event_filter=self.app.dashboard_dictation_filter().filter_events,
                            use_scan_cache=True,
                        )
                    except TypeError as exc:
                        if "use_scan_cache" not in str(exc):
                            raise
                        return _lw.build_range_statistics(
                            self.app.store.root, date_from=date_from, date_to=date_to,
                            scope=scope, view=view,
                            event_filter=self.app.dashboard_dictation_filter().filter_events,
                        )

                statistics = self.app.cached_read(
                    f"stats|{date_from}|{date_to}|{scope}|{view}",
                    _produce_stats,
                )
                statistics["accounting_scope"] = scope
                statistics["read_contract"] = _lw._read_contract(scope, view)
                self._send_json(statistics)
                return
            if parsed.path == "/api/events/search":
                query = parse_qs(parsed.query)
                payload = _lw._canonical_event_search(
                    self.app.store,
                    text_query=unquote(query.get("q", [""])[0]),
                    result_type=unquote(query.get("type", ["all"])[0]),
                    date=unquote(query.get("date", [""])[0]) or None,
                    date_from=unquote(query.get("from", [""])[0]) or None,
                    date_to=unquote(query.get("to", [""])[0]) or None,
                    category=unquote(query.get("category", [""])[0]) or None,
                    amount_min=unquote(query.get("amount_min", [""])[0]) or None,
                    amount_max=unquote(query.get("amount_max", [""])[0]) or None,
                    currency=unquote(query.get("currency", [""])[0]) or None,
                    import_source=unquote(query.get("import_source", ["all"])[0]),
                    limit=unquote(query.get("limit", ["100"])[0]),
                    scope=unquote(query.get("scope", ["provisional"])[0]),
                    view=unquote(query.get("view", ["display"])[0]),
                    event_filter=self.app.dashboard_dictation_filter().filter_events,
                )
                self._send_json(payload)
                return
            if parsed.path == "/api/search":
                return self._get_search(parsed)
            parts = parsed.path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "context-relation-reviews"
            ):
                artifact_path = unquote(parse_qs(parsed.query).get("path", [""])[0])
                reviews = self.app.store.read_context_relation_reviews(
                    parts[2], artifact_path=artifact_path
                )
                self._send_json({"reviews": reviews})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "reviews"
            ):
                artifact_path = unquote(parse_qs(parsed.query).get("path", [""])[0])
                self._send_json(
                    {
                        "reviews": self.app.store.read_reviews(
                            parts[2], artifact_path=artifact_path or None
                        )
                    }
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "manual-events"
            ):
                query = parse_qs(parsed.query)
                artifact_path = unquote(query.get("path", [""])[0])
                include_rejected = str(
                    query.get("include_rejected", ["0"])[0]
                ).lower() in {"1", "true", "yes"}
                self._send_json(
                    {
                        "events": self.app.store.read_manual_events(
                            parts[2],
                            artifact_path=artifact_path or None,
                            include_rejected=include_rejected,
                        )
                    }
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "event-corrections"
            ):
                artifact_path = unquote(parse_qs(parsed.query).get("path", [""])[0])
                self._send_json(
                    {
                        "corrections": self.app.store.read_event_corrections(
                            parts[2], artifact_path=artifact_path or None
                        )
                    }
                )
                return
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                self._send_json(self.app.store.read(parts[2]))
                return
            if len(parts) == 4 and parts[:3] == ["api", "jobs", "artifact"]:
                self._send_json(
                    {"error": "artifact path missing"}, HTTPStatus.NOT_FOUND
                )
                return
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith(
                "/artifact"
            ):
                query = parse_qs(parsed.query)
                job_id = parsed.path.split("/")[3]
                relative = unquote(query.get("path", [""])[0])
                self._serve_artifact(job_id, relative)
                return
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith(
                "/artifacts"
            ):
                # 磁盘实况产物清单。partial/failed 的 job 曾出现 results 空数组/
                # 裸日期但产物已在磁盘的记录，前端靠这个宽判据才能把已产出的事件
                # 显示出来（见 _jobs.JobStore.list_job_artifacts）。
                job_id = parsed.path.split("/")[3]
                self._send_json(
                    {
                        "job_id": job_id,
                        "artifacts": self.app.store.list_job_artifacts(job_id),
                    }
                )
                return
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/audio"):
                query = parse_qs(parsed.query)
                job_id = parsed.path.split("/")[3]
                relative = unquote(query.get("path", [""])[0])
                self._serve_audio(job_id, relative)
                return
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith(
                "/audio-clip"
            ):
                query = parse_qs(parsed.query)
                job_id = parsed.path.split("/")[3]
                relative = unquote(query.get("path", [""])[0])
                start = float(unquote(query.get("start", ["0"])[0]))
                end = float(unquote(query.get("end", ["0"])[0]))
                self._serve_audio_clip(job_id, relative, start=start, end=end)
                return
            if parsed.path == "/version-badge.js":
                self._serve_version_badge()
                return
            query_token = str(parse_qs(parsed.query).get("token", [""])[0])
            self._serve_static(
                parsed.path,
                set_auth_cookie=self._token_matches(query_token),
            )
        except _support.ControlSnapshotBusy as error:
            # [2026-10-23 P0] 控制快照 single-flight 忙碌时，不能把连接直接断掉。
            self._send_json(
                {
                    "ok": False,
                    "error": "control_snapshot_busy",
                    "snapshot_busy": True,
                    "endpoint": parsed.path,
                    "message": str(error),
                    "retry_after": 1,
                    "retry_after_ms": 1000,
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except _support.ReadProducerBusy as error:
            # [2026-10-23 Q26] 冷 producer 仍在后台跑：返回可重试的 202，
            # 不再把「还在算」误报成 503 不可用。
            self._send_json(
                {
                    "ok": False,
                    "status": "computing",
                    "error": "read_busy",
                    "message": str(error),
                    "retry_after": error.retry_after,
                    "retry_after_ms": error.retry_after_ms,
                },
                HTTPStatus.ACCEPTED,
            )
        except WatchSessionNotFoundError as error:
            self._send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except WatchSessionError as error:
            self._send_json({"error": str(error)}, error.status_code)
        except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _serve_state_backup(self) -> None:
        body = self.app.store.build_state_backup()
        filename = f"life-logger-state-{_lw.datetime.now(tz=_lw.LOCAL_TZ).strftime('%Y%m%d-%H%M%S')}.zip"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body, label="state-backup.zip")

    def _serve_artifact(self, job_id: str, relative: str) -> None:
        job_dir = self.app.store.job_path(job_id).resolve()
        target = (job_dir / relative).resolve()
        if target != job_dir and job_dir not in target.parents:
            raise ValueError("artifact path outside job")
        if not target.is_file():
            self._send_json({"error": "result not found"}, HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        content_type = (
            mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        if content_type == "application/json" or content_type.startswith("text/"):
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_body(body, label=f"artifact:{relative}")

    def _serve_audio(self, job_id: str, artifact_path: str) -> None:
        source = self.app.store.audio_path_for_artifact(job_id, artifact_path)
        self._serve_audio_path(source)

    def _serve_passport_audio(self, device: str, session: str) -> None:
        for value in (device, session):
            if not value or Path(value).name != value or value in {".", ".."}:
                raise ValueError("AI Passport 会话路径无效")
        root = (self.app.store.root / "passport-stream").resolve()
        session_dir = (root / device / session).resolve()
        if root not in session_dir.parents:
            raise ValueError("AI Passport 会话路径越界")
        summary_path = session_dir / "summary.json"
        if not summary_path.is_file():
            raise ValueError("AI Passport 会话摘要不存在")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(summary, dict) or not summary.get("complete"):
            raise ValueError("AI Passport 会话不完整，不能回放")
        source = session_dir / "recording.wav"
        if not source.is_file():
            raise ValueError("AI Passport 完整会话缺少 recording.wav")
        self._serve_audio_path(source)

    def _serve_audio_path(self, source: Path) -> None:
        size = source.stat().st_size
        start = 0
        end = max(0, size - 1)
        status = HTTPStatus.OK
        range_header = str(self.headers.get("Range") or "").strip()
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                self._send_audio_range_error(size)
                return
            raw_start, separator, raw_end = range_header[6:].partition("-")
            if not separator:
                self._send_audio_range_error(size)
                return
            try:
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size - 1
                elif raw_end:
                    suffix = int(raw_end)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, size - suffix)
                    end = size - 1
                else:
                    raise ValueError
            except ValueError:
                self._send_audio_range_error(size)
                return
            if start < 0 or start >= size or end < start:
                self._send_audio_range_error(size)
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header(
            "Content-Type", mimetypes.guess_type(source.name)[0] or "audio/wav"
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not length:
            return
        try:
            with source.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining > 0:
                    block = stream.read(min(64 * 1024, remaining))
                    if not block:
                        break
                    self._write_body(block, label=f"audio:{source.name}")
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_audio_clip(
        self,
        job_id: str,
        artifact_path: str,
        *,
        start: float,
        end: float,
    ) -> None:
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("音频片段时间范围无效")
        if end - start > 90:
            raise ValueError("音频片段过长")
        watch_spans = self.app.store.audio_clip_spans_for_artifact(
            job_id,
            artifact_path,
            start=start,
            end=end,
        )
        if watch_spans is not None:
            if not watch_spans:
                raise ValueError("Watch 录音时间范围内没有可试听音频")
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            filters = []
            audio_input_index = 0
            for index, span in enumerate(watch_spans):
                audio_path = span.get("path")
                if isinstance(audio_path, Path):
                    command.extend(["-i", str(audio_path)])
                    filters.append(
                        f"[{audio_input_index}:a]atrim=start={float(span['start_seconds']):.6f}:"
                        f"end={float(span['end_seconds']):.6f},"
                        f"asetpts=PTS-STARTPTS,aresample=16000,"
                        f"aformat=channel_layouts=mono[a{index}]"
                    )
                    audio_input_index += 1
                    continue
                silence_duration = float(span.get("silence_seconds") or 0)
                if not math.isfinite(silence_duration) or silence_duration <= 0:
                    raise ValueError("Watch 试听片段时间范围无效")
                filters.append(
                    f"anullsrc=r=16000:cl=mono:d={silence_duration:.6f},"
                    f"atrim=duration={silence_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            audio_inputs = "".join(f"[a{index}]" for index in range(len(watch_spans)))
            filters.append(
                f"{audio_inputs}concat=n={len(watch_spans)}:v=0:a=1[outa]"
            )
            command.extend(
                [
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    "[outa]",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    "pipe:1",
                ]
            )
        else:
            source = self.app.store.audio_path_for_artifact(job_id, artifact_path)
            padding = 0.35
            clip_start = max(0.0, start - padding)
            clip_end = end + padding
            duration = clip_end - clip_start
            coarse_start = max(0.0, clip_start - 2.0)
            fine_start = clip_start - coarse_start
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{coarse_start:.3f}",
                "-i",
                str(source),
                "-ss",
                f"{fine_start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                "pipe:1",
            ]
        try:
            completed = _lw.subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=30,
            )
        except FileNotFoundError as error:
            raise ValueError("试听功能需要 ffmpeg") from error
        except _lw.subprocess.TimeoutExpired as error:
            raise ValueError("生成试听片段超时") from error
        except _lw.subprocess.CalledProcessError as error:
            detail = error.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"生成试听片段失败: {detail or 'ffmpeg error'}") from error

        body = completed.stdout
        if not body:
            raise ValueError("生成的试听片段为空")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self._write_body(body, label="review-audio.wav")

    def _send_audio_range_error(self, size: int) -> None:
        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_version_badge(self) -> None:
        """动态生成版本角标脚本（不落盘、不需要鉴权、不含任何路径/密钥）。

        为什么服务端生成：角标要显示「运行中进程」的版本，而不是磁盘上某个文件的版本；
        同时避免为了一行 UI 去改 web/ 目录（2号 正在并发编辑，直接改会冲突）。
        """
        from life_logger.version import build_info

        info = build_info(process_started_at=self.app.started_at)
        payload = json.dumps(info, ensure_ascii=False)
        script = _VERSION_BADGE_SCRIPT.replace("__BUILD_INFO__", payload)
        body = script.encode("utf-8")
        content_type = "application/javascript; charset=utf-8"
        packed = _support.gzip_response_body(body, content_type, self.headers.get("Accept-Encoding"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if packed is not None:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            body = packed
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write_body(body, label="version-badge.js")

    def _serve_static(self, path: str, *, set_auth_cookie: bool = False) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (_lw.WEB_ROOT / relative).resolve()
        if target.is_dir():
            target = (target / "index.html").resolve()
        if _lw.WEB_ROOT not in target.parents and target != _lw.WEB_ROOT:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "text/plain"
        # 【2026-10-19】版本角标：后端在返回 index.html 时注入一行 script。
        # 为什么不用改前端文件：2号 正在并发改 web/，任何直接编辑都会冲突；
        # 注入在响应层，属于后端职责，且可用 local-config 的 web_version_badge 关掉。
        if target.name == "index.html" and _support._version_badge_enabled(self.app.config_path):
            marker = b"</body>"
            inject = b'  <script src="/version-badge.js" defer></script>\n</body>'
            if marker in body:
                body = body.replace(marker, inject, 1)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        # 【2026-10-19 P-13】文本类静态资源按需 gzip；不带 Accept-Encoding 的客户端拿原始字节
        packed = _support.gzip_response_body(body, content_type, self.headers.get("Accept-Encoding"))
        if packed is not None:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            body = packed
        self.send_header("Content-Length", str(len(body)))
        if target.suffix in {".html", ".js", ".css", ".json"}:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        if set_auth_cookie and self.app.access_token:
            self.send_header(
                "Set-Cookie",
                "life_logger_access="
                f"{self.app.access_token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        self._write_body(body, label=f"static:{path}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._require_api_auth(write=True):
            return
        try:
            if parsed.path == "/api/watch-status":
                payload = self._body_json()
                result = self.app.watch_session_store.save_device_status(payload)
                accepted = result.pop("accepted", True)
                self._send_json(
                    result,
                    HTTPStatus.CREATED if accepted else HTTPStatus.OK,
                )
                return

            if parsed.path == "/api/watch-sessions":
                payload = self._body_json()
                result = self.app.watch_session_store.start_session(
                    device_id=str(payload.get("device_id") or ""),
                    session_id=str(payload.get("session_id") or ""),
                    started_at=str(payload.get("started_at") or ""),
                    sample_rate=int(payload.get("sample_rate", 16_000)),
                    channels=int(payload.get("channels", 1)),
                    codec=str(payload.get("codec") or "m4a-aac"),
                    # 【2026-10-15 新增】客户端 IP 用于场景判断的粗粒度兜底：
                    # 192.168.43.x / 172.20.10.x（手机热点）在配置里映射为 Transit。
                    # 手表在蜂窝网络上时这个 IP 没意义，所以它只当辅助信号。
                    client_ip=(self.client_address[0] if getattr(self, "client_address", None) else None),
                )
                self._send_json(
                    result,
                    HTTPStatus.OK if result.get("duplicate") else HTTPStatus.CREATED,
                )
                return

            if parsed.path == "/api/health/sync":
                payload = self._body_json()
                from life_logger.health_store import HealthDataStore
                hstore = HealthDataStore(self.app.store.root / "health")
                # 接收心率数组
                hr_points = payload.get("heart_rates") or []
                if isinstance(hr_points, list) and hr_points:
                    hstore.record_heart_rates(hr_points)
                # 接收单点心率
                if "bpm" in payload:
                    hstore.record_heart_rates([{
                        "timestamp": str(payload.get("timestamp") or _lw.datetime.now(tz=_lw.LOCAL_TZ).isoformat()),
                        "bpm": float(payload["bpm"]),
                        "source": str(payload.get("source") or "health-sync"),
                        "context": str(payload.get("context") or "active"),
                    }])
                # 接收步数
                if "steps" in payload:
                    today = str(payload.get("date") or _lw.datetime.now(tz=_lw.LOCAL_TZ).strftime("%Y-%m-%d"))
                    hstore.record_steps(today, int(payload["steps"]))
                # 接收睡眠
                if "sleep" in payload and isinstance(payload["sleep"], dict):
                    hstore.record_sleep(payload["sleep"])
                self._send_json({"ok": True, "message": "健康体征数据已成功同步并持久化"})
                return

            if parsed.path == "/api/memory/ask":
                payload = self._body_json()
                query = str(payload.get("query") or payload.get("question") or "").strip()
                if not query:
                    self._send_json({"error": "缺少提问内容 query"}, HTTPStatus.BAD_REQUEST)
                    return
                from life_logger.memory_rag import MemoryRAGService
                rag = MemoryRAGService(self.app.store.root, self.app.config_path)
                answer = rag.ask(query)
                self._send_json({"ok": True, "result": answer.to_dict()})
                return

            if parsed.path == "/api/events/natural-correction":
                return self._post_events_natural_correction(parsed)

            if parsed.path in {
                "/api/expense/pending-broadcast/accept",
                "/api/expense/pending-broadcast/reject",
                "/api/expense/pending-broadcast/undo",
            }:
                # [2026-10-23 P-51] 人工处置播报候选：文件锁 + 原子落盘，读时叠加。
                from life_logger.broadcast_decisions import (
                    BroadcastDecisionError,
                    apply_decisions,
                    record_decision,
                    undo_decision,
                    validate_date,
                )

                payload_body = self._body_json()
                action = parsed.path.rsplit("/", 1)[-1]
                try:
                    date = validate_date(str(payload_body.get("date") or ""))
                    event_id = str(payload_body.get("event_id") or "").strip()
                    if not event_id:
                        raise BroadcastDecisionError("event_id is required")
                    base, decisions, _applied = self.app._broadcast_expense_state(date)
                    pending = {
                        str(item.get("event_id") or ""): item
                        for item in (base.get("pending_broadcast") or [])
                        if isinstance(item, dict)
                    }
                    item = pending.get(event_id)
                    if item is None:
                        raise BroadcastDecisionError(
                            "event_id is not a pending broadcast for this date"
                        )
                    if action == "undo":
                        decisions, changed = undo_decision(self.app.store.root, date, event_id)
                    else:
                        amount = payload_body.get("amount")
                        if action == "accept" and amount is None:
                            amount = item.get("proposed_amount")
                        if action == "accept" and amount is None:
                            raise BroadcastDecisionError(
                                "amount is required when proposed_amount is missing"
                            )
                        decisions, changed = record_decision(
                            self.app.store.root,
                            date,
                            event_id,
                            action,
                            amount=amount if action == "accept" else None,
                        )
                    applied = apply_decisions(base, decisions)
                except BroadcastDecisionError as error:
                    self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                except OSError as error:
                    self._send_json(
                        {"ok": False, "error": f"cannot persist broadcast decision: {error}"},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "date": date,
                        "event_id": event_id,
                        "action": action,
                        "changed": changed,
                        "total_amount": applied.get("total_amount"),
                        "pending_broadcast_count": applied.get("pending_broadcast_count"),
                        "transaction_event_ids": [
                            str(tx.get("event_id") or "")
                            for tx in (applied.get("transactions") or [])
                            if str(tx.get("event_id") or "") == event_id
                        ],
                    }
                )
                return
            if parsed.path in {"/api/expense/edit", "/api/expense/edit/undo"}:
                from life_logger.expense_corrections import (
                    ExpenseEditError,
                    edit_expense,
                    undo_expense_edit,
                )
                from life_logger.judgment_queue import invalidate_cache as invalidate_jq_cache
                from life_logger.web_support import invalidate_read_caches

                payload_body = self._body_json()
                try:
                    if parsed.path == "/api/expense/edit/undo":
                        result = undo_expense_edit(
                            self.app.store.root,
                            date=str(payload_body.get("date") or ""),
                            entry_key=str(payload_body.get("entry_key") or ""),
                        )
                    else:
                        result = edit_expense(
                            self.app.store.root,
                            date=str(payload_body.get("date") or ""),
                            entry_key=str(payload_body.get("entry_key") or ""),
                            amount=payload_body.get("amount"),
                            label=payload_body.get("label"),
                            original_amount=payload_body.get("original_amount"),
                            original_label=payload_body.get("original_label"),
                            quote=payload_body.get("quote"),
                            time=payload_body.get("time"),
                        )
                except ExpenseEditError as error:
                    self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                invalidate_jq_cache()
                invalidate_read_caches(self.app.store.root, parsed.path)
                self._send_json(result)
                return
            if parsed.path in {"/api/food/rename", "/api/food/rename/undo"}:
                from life_logger.food_corrections import (
                    FoodRenameError,
                    rename_food,
                    undo_food_rename,
                )
                from life_logger.judgment_queue import invalidate_cache as invalidate_jq_cache
                from life_logger.web_support import invalidate_read_caches

                payload_body = self._body_json()
                try:
                    if parsed.path == "/api/food/rename/undo":
                        result = undo_food_rename(
                            self.app.store.root,
                            date=str(payload_body.get("date") or ""),
                            event_id=str(payload_body.get("event_id") or ""),
                        )
                    else:
                        result = rename_food(
                            self.app.store.root,
                            date=str(payload_body.get("date") or ""),
                            event_id=str(payload_body.get("event_id") or ""),
                            name=payload_body.get("name"),
                        )
                except FoodRenameError as error:
                    self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                invalidate_jq_cache()
                invalidate_read_caches(self.app.store.root, parsed.path)
                self._send_json(result)
                return
            if parsed.path == "/api/daily/synthesize":
                payload = self._body_json()
                date_str = str(payload.get("date") or "").strip()
                force = bool(payload.get("force"))
                brief = self.app.build_daily_brief(date=date_str or None, allow_empty=True)
                target_date = str(brief.get("date") or date_str or "")
                events_list = list(brief.get("events", []))
                from life_logger.daily_cognitive_session import generate_daily_cognitive_synthesis
                from life_logger.day_facts import activities_for_day
                synthesis = generate_daily_cognitive_synthesis(
                    target_date,
                    events_list,
                    force=force,
                    cache_dir=self.app.store.root / "daily-synthesis",
                    activities=None if events_list else activities_for_day(target_date, root=self.app.store.root),
                )
                self._send_json(synthesis)
                return

            if parsed.path == "/api/daily/chat":
                payload = self._body_json()
                date_str = str(payload.get("date") or "").strip()
                question = str(payload.get("question") or "").strip()
                history = payload.get("history") if isinstance(payload.get("history"), list) else None
                if not question:
                    self._send_json({"error": "missing_question"}, HTTPStatus.BAD_REQUEST)
                    return
                brief = self.app.build_daily_brief(date=date_str or None, allow_empty=True)
                target_date = str(brief.get("date") or date_str or "")
                events_list = list(brief.get("events", []))
                from life_logger.daily_cognitive_session import ask_daily_cognitive_session
                synthesis_file = self.app.store.root / "daily-synthesis" / f"synthesis-{target_date}.json"
                synthesis = None
                if synthesis_file.is_file():
                    try:
                        synthesis = json.loads(synthesis_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                chat_res = ask_daily_cognitive_session(target_date, question, events_list, synthesis, history=history)
                self._send_json(chat_res)
                return

            if parsed.path == "/api/openclaw/tasks/status":
                payload = self._body_json()
                task_id = str(payload.get("task_id") or "")
                status = str(payload.get("status") or "")
                if not re.fullmatch(r"oc_task_[\w-]+", task_id) or status not in {
                    "pending", "in_progress", "blocked", "completed", "dismissed",
                }:
                    self._send_json({"error": "invalid_task_status"}, HTTPStatus.BAD_REQUEST)
                    return
                from life_logger.openclaw_bridge import OpenClawBridgeClient
                try:
                    task = _lw.patch_task_status(
                        OpenClawBridgeClient(), task_id, status, payload.get("revision")
                    )
                except RuntimeError as error:
                    conflict = "HTTP 409" in str(error)
                    self._send_json(
                        {"error": "这条任务刚在别处改过，请刷新后再试" if conflict else str(error)},
                        HTTPStatus.CONFLICT if conflict else HTTPStatus.BAD_GATEWAY,
                    )
                    return
                self._send_json({"ok": True, "task": task})
                return

            if parsed.path == "/api/openclaw/reconciliation-callback":
                payload = self._body_json()
                action = str(payload.get("action") or "confirm")
                target_id = str(payload.get("target_id") or "")
                res = {
                    "status": "success",
                    "action": action,
                    "target_id": target_id,
                    "resolved_at": _support._iso(),
                }
                self._send_json(res, HTTPStatus.OK)
                return

            watch_parts = parsed.path.strip("/").split("/")
            if (
                len(watch_parts) == 5
                and watch_parts[:2] == ["api", "watch-sessions"]
                and watch_parts[4] == "complete"
            ):
                payload = self._body_json()
                device_id = watch_parts[2]
                session_id = watch_parts[3]
                expected_segments = payload.get("expected_segments")
                if type(expected_segments) is not int or expected_segments <= 0:
                    raise ValueError("expected_segments must be a positive integer")
                ended_at = str(payload.get("ended_at") or "")
                if not ended_at:
                    raise ValueError("ended_at is required")
                current_manifest = self.app.watch_session_store.load_session(
                    device_id,
                    session_id,
                )
                completion_job_id = str(
                    current_manifest.get("job_id")
                    or current_manifest.get("completion_job_id")
                    or ""
                )
                job_reused = bool(completion_job_id)
                if not current_manifest.get("job_id") and not completion_job_id:
                    completion_job_id = self.app.store.create_watch_pending_job(
                        device_id=device_id,
                        session_id=session_id,
                        start_at=str(current_manifest.get("started_at") or "") or None,
                        timezone_name=str(current_manifest.get("timezone") or "Asia/Shanghai"),
                    )
                    self.app.watch_session_store.mark_completion_job(
                        device_id,
                        session_id,
                        completion_job_id,
                    )
                result = self.app.watch_session_store.complete_session(
                    device_id=device_id,
                    session_id=session_id,
                    expected_segments=expected_segments,
                    ended_at=ended_at,
                    capsules=payload.get("capsules"),
                    # 设备可选上报停录归因；没带就由 store 记成 inferred，不编原因。
                    ended_by=payload.get("ended_by"),
                    end_reason=payload.get("end_reason"),
                )
                try:
                    self.app.watch_session_store.record_completion_ack(
                        device_id=device_id,
                        session_id=session_id,
                        job_id=str((result.get("session") or {}).get("job_id")
                                   or (result.get("session") or {}).get("completion_job_id") or "") or None,
                        result="duplicate" if result.get("duplicate") else "complete",
                        ended_by=(result.get("session") or {}).get("ended_by"),
                        end_reason=(result.get("session") or {}).get("end_reason"),
                        received_segments=result.get("received_segments"),
                    )
                except (OSError, ValueError, TypeError):
                    # 台账写失败不能影响给设备的 ACK
                    pass
                manifest = result["session"]
                job_id = str(manifest.get("job_id") or completion_job_id or "") or None
                if not job_id:
                    raise ValueError("Watch completion was accepted without a reserved Job ID")
                if not manifest.get("job_id") and self.app.watch_formal_asr.ready_for_session(
                    device_id,
                    session_id,
                ):
                    session_dir = self.app.watch_session_store.session_directory(device_id, session_id)
                    self.app.watch_session_store.assemble_recording(device_id, session_id)
                    manifest = self.app.watch_session_store.load_session(device_id, session_id)
                    imported = self.app.store.import_watch_recording(
                        session_dir,
                        {**manifest, "complete": True},
                        self.app.config_path,
                        auto_start=bool(payload.get("auto_start", True)),
                        formal_asr_processor=self.app.watch_formal_asr,
                        reserved_job_id=str(manifest.get("completion_job_id") or completion_job_id),
                    )
                    job_id = str(imported["job_id"])
                    job_reused = job_reused or bool(imported.get("reused"))
                    self.app.watch_session_store.mark_imported(device_id, session_id, job_id)
                    manifest = self.app.watch_session_store.load_session(device_id, session_id)
                self._send_json(
                    {
                        "complete": True,
                        "duplicate": bool(result.get("duplicate")),
                        "session_id": session_id,
                        "job_id": job_id,
                        "job_reused": job_reused,
                        "processing": "formal_job_started" if manifest.get("job_id") else "waiting_for_formal_asr",
                        # [Q161] 非法 ended_by 等非致命问题：保留证据 + 明确告警，不阻断 ACK
                        "warnings": list(result.get("warnings") or []),
                        "ended_by": manifest.get("ended_by"),
                        "ended_by_source": manifest.get("ended_by_source"),
                    }
                )
                return
            if parsed.path == "/api/settings/deepseek":
                return self._post_settings_deepseek(parsed)
            if parsed.path == "/api/settings/stage-models":
                payload = self._body_json()
                stage = str(payload.get("stage") or "").strip()
                updates = payload.get("updates")
                if not isinstance(updates, dict):
                    self._send_json({"ok": False, "error": "updates 必须为对象"}, HTTPStatus.BAD_REQUEST)
                    return
                if stage not in _lw.VALID_STAGES:
                    self._send_json({"ok": False, "error": f"非法 stage: {stage}，有效阶段为: {list(_lw.VALID_STAGES)}"}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    _lw.update_stage_config(stage, updates, self.app.config_path)
                    self._send_json({"ok": True})
                except Exception as exc:
                    self._send_json({"ok": False, "error": f"保存失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if parsed.path == "/api/settings/stage-models/check":
                return self._post_settings_stage_models_check(parsed)
            if parsed.path == "/api/settings/preferences":
                payload = self._body_json()
                current = _read_preferences(self.app.store.root)
                current.update(payload)
                saved = _lw._write_preferences(self.app.store.root, current)
                self._send_json({"preferences": saved})
                return
            if parsed.path == "/api/mobile-access":
                return self._post_mobile_access(parsed)
            if parsed.path == "/api/mobile-capture":
                payload = self._body_json()
                current = _read_preferences(self.app.store.root)
                current["mobile_capture_enabled"] = bool(
                    payload.get("enabled", current.get("mobile_capture_enabled", True))
                )
                current["mobile_capture_auto_start"] = bool(
                    payload.get(
                        "auto_start",
                        current.get("mobile_capture_auto_start", False),
                    )
                )
                saved = _lw._write_preferences(self.app.store.root, current)
                self._send_json({"mobile_capture": _auth._mobile_capture_config(saved)})
                return
            if parsed.path == "/api/auto-ingest":
                payload = self._body_json()
                raw_folders = payload.get("folders")
                if not isinstance(raw_folders, list):
                    raise ValueError("自动导入目录格式无效")
                result = self.app.store.configure_auto_ingest(
                    enabled=bool(payload.get("enabled")),
                    folders=[str(item) for item in raw_folders],
                    stability_seconds=payload.get("stability_seconds"),
                    scan_interval_seconds=payload.get("scan_interval_seconds"),
                )
                self._send_json(result)
                return
            if parsed.path == "/api/auto-ingest/scan":
                result = self.app.run_auto_ingest_scan()
                self._send_json(result)
                return
            if parsed.path == "/api/auto-ingest/choose-folder":
                selected = self.app.store.choose_auto_ingest_folder()
                status = (
                    self.app.store.add_auto_ingest_folder(selected)
                    if selected is not None
                    else self.app.store.auto_ingest_status()
                )
                self._send_json(
                    {
                        "path": selected,
                        "cancelled": selected is None,
                        "status": status,
                    }
                )
                return
            if parsed.path == "/api/daily-brief":
                payload = self._body_json()
                self._send_json(
                    self.app.deliver_daily_brief(
                        date=str(payload.get("date") or "") or None,
                        webhook_url=str(payload.get("webhook_url") or "") or None,
                    )
                )
                return
            if parsed.path == "/api/tasks/status":
                payload = self._body_json()
                self._send_json(_jobs.apply_task_status(
                    self.app, payload, sync_openclaw=self._sync_kanban_openclaw,
                ))
                return
            if parsed.path == "/api/reminders/status":
                payload = self._body_json()
                task_key = str(payload.get("task_key") or "").strip()
                status = str(payload.get("status") or "").strip()
                reminder = self.app.reminders.set_status(task_key, status)
                try:
                    self.app.store.save_task_state(
                        task_key=task_key,
                        # 【2026-10-15】跨存储换词：提醒本的 scheduled -> 任务本 in_progress
                        status=_jobs.to_task_status(status),
                        title=payload.get("title") or reminder.get("title"),
                        event_id=payload.get("event_id") or reminder.get("event_id"),
                    )
                except Exception as error:
                    # 反向同步失败同样不能静默：reminders 已更新而 task state 未更新。
                    _lw.sys.stderr.write(
                        f"[life-logger] task state 同步失败 task_key={task_key}: {type(error).__name__} {error}\n"
                    )
                    _lw.sys.stderr.flush()
                self._send_json({"ok": True, "reminder": reminder})
                return
            if parsed.path == "/api/habits/add":
                payload = self._body_json()
                result = self.app.habits.add(
                    str(payload.get("title") or payload.get("target") or ""),
                    source=str(payload.get("source") or "manual"),
                )
                self._send_json({"ok": True, **result})
                return
            if parsed.path in ("/api/judgment-queue/answer", "/api/judgment-queue/undo", "/api/judgment-queue/manual", "/api/judgment-queue/duplicate"):
                from life_logger.judgment_queue import answer, decide_duplicate, decide_lost_manual, undo
                from life_logger.review_questions import ReviewQuestionError
                payload = self._body_json()
                try:
                    if parsed.path.endswith("/answer"):
                        result = answer(self.app.store, payload)
                    elif parsed.path.endswith("/undo"):
                        result = undo(self.app.store, payload)
                    elif parsed.path.endswith("/duplicate"):
                        result = decide_duplicate(
                            self.app.store.root,
                            event_id=str(payload.get("event_id") or ""),
                            action=str(payload.get("action") or ""),
                            date=str(payload.get("date") or ""),
                        )
                    else:
                        result = decide_lost_manual(
                            self.app.store.root,
                            job_id=str(payload.get("job_id") or ""),
                            event_id=str(payload.get("event_id") or ""),
                            action=str(payload.get("action") or ""),
                        )
                except (ValueError, OSError, ReviewQuestionError) as error:
                    self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    from life_logger.pending_review import invalidate_pending_cache
                    invalidate_pending_cache()
                except Exception:
                    pass
                try:
                    from life_logger.judgment_queue import invalidate_cache as invalidate_jq_cache
                    from life_logger.web_support import invalidate_read_caches
                    invalidate_jq_cache()
                    invalidate_read_caches(self.app.store.root, parsed.path)
                except Exception:
                    pass
                self._send_json(result)
                return
            if parsed.path in ("/api/review-questions/answer", "/api/review-questions/undo"):
                # 来源判断（query/media/personal/uncertain）与撤销：只写本来源反馈台账。
                from life_logger.pending_review import collect_pending_sources
                from life_logger.review_questions import (
                    ReviewQuestionError,
                    answer_review_question,
                    undo_review_question,
                )

                payload = self._body_json()
                root = self.app.store.root

                def lookup(sid: str):
                    try:
                        data = collect_pending_sources(root)
                    except Exception:
                        return None
                    for group in data.get("groups", []) or []:
                        for item in group.get("items", []) or []:
                            if str(item.get("source_id") or "") == sid:
                                return item
                    return None

                try:
                    if parsed.path.endswith("/undo"):
                        result = undo_review_question(
                            root,
                            source_id=str(payload.get("source_id") or ""),
                            op_id=str(payload.get("op_id") or ""),
                        )
                        from life_logger.judgment_queue import invalidate_cache, undo_answer
                        undo_answer(str(payload.get("op_id") or ""))
                        invalidate_cache()
                    else:
                        result = answer_review_question(
                            root,
                            source_id=str(payload.get("source_id") or ""),
                            operation=str(payload.get("operation") or ""),
                            op_id=str(payload.get("op_id") or ""),
                            reason=str(payload.get("reason") or ""),
                            source_lookup=lookup,
                        )
                        from life_logger.judgment_queue import invalidate_cache, record_answer
                        record_answer(
                            lookup(str(payload.get("source_id") or "")),
                            str(payload.get("operation") or ""),
                            str(result.get("op_id") or ""),
                        )
                        invalidate_cache()
                except ReviewQuestionError as error:
                    self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    from life_logger.pending_review import invalidate_pending_cache
                    invalidate_pending_cache()     # muted/undo 后 pending 投影立即生效
                except Exception:
                    pass
                self._send_json(result)
                return
            if parsed.path == "/api/pending-sources/status":
                # 人工处理台账（唯一写入口）：ignored / handled / open（撤销）。
                # 不改动任何抽取产物，只在 pending-review-states.json 记一笔，
                # 让「已看过/已忽略」的条目不再占版面。
                from life_logger.pending_review import (
                    PendingReviewError,
                    set_pending_review_state,
                )

                payload = self._body_json()
                try:
                    result = set_pending_review_state(
                        self.app.store.root,
                        source_id=str(payload.get("source_id") or ""),
                        status=str(payload.get("status") or ""),
                        note=str(payload.get("note") or ""),
                    )
                except PendingReviewError as error:
                    self._send_json(
                        {"ok": False, "error": str(error)},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/pending-sources/status-bulk":
                # 按日期/分组批量处理（目前只允许 needs_review）。仍然只写台账，
                # 一次加载一次写盘，避免逐条 set 造成半批已写/半批失败。
                from life_logger.pending_review import (
                    PendingReviewError,
                    set_pending_review_states_bulk,
                )

                payload = self._body_json()
                try:
                    result = set_pending_review_states_bulk(
                        self.app.store.root,
                        date=str(payload.get("date") or ""),
                        group=str(payload.get("group") or "needs_review"),
                        status=str(payload.get("status") or ""),
                        note=str(payload.get("note") or ""),
                    )
                except PendingReviewError as error:
                    self._send_json(
                        {"ok": False, "error": str(error)},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._send_json(result)
                return
            if parsed.path == "/api/habits/checkin":
                return self._post_habits_checkin(parsed)
            if parsed.path == "/api/habits/status":
                payload = self._body_json()
                target = str(
                    payload.get("target")
                    or payload.get("habit_key")
                    or payload.get("title")
                    or ""
                )
                try:
                    result = self.app.habits.set_status(
                        target, str(payload.get("status") or "").strip()
                    )
                except _lw.HabitNotFoundError as error:
                    self._send_json(
                        {
                            "ok": False,
                            "error": str(error),
                            "candidates": error.candidates,
                        },
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/habits/voice":
                # 口语口令入口：直接给文字（Watch ASR / 微信 / 网页输入框都用这一条），
                # 由本地口令表判定「添加习惯 / 习惯打卡 / 删除习惯」后落到台账。
                payload = self._body_json()
                captures = payload.get("captures")
                if not captures:
                    from life_logger.voice_triggers import scan_voice_triggers

                    spoken = str(payload.get("text") or "")
                    captures = [
                        capture.to_dict() for capture in scan_voice_triggers(spoken)
                    ]
                outcomes = _lw.apply_habit_captures(self.app.habits, captures)
                self._send_json(
                    {
                        "ok": True,
                        "applied_count": len(
                            [o for o in outcomes if o.get("applied")]
                        ),
                        "outcomes": outcomes,
                    }
                )
                return
            if parsed.path == "/api/watch/vip-command":
                return self._post_watch_vip_command(parsed)
            if parsed.path == "/api/nutrition/voice":
                return self._post_nutrition_voice(parsed)
            if parsed.path == "/api/openclaw/sync":
                try:
                    from life_logger.openclaw_bridge import OpenClawBridgeClient

                    client = OpenClawBridgeClient()
                    payload = self._body_json() if self.headers.get("Content-Length") else {}
                    events = payload.get("events")
                    if not events:
                        events = []
                        for job in self.app.store.list():
                            # degraded 的产物是可用的（只是过程有子步骤失败），
                            # 事件一样要同步；否则降级会顺带吞掉真实待办。
                            if job.get("status") not in {"completed", "degraded"}:
                                continue
                            if job.get("job_id"):
                                job_events = self.app.store.read_job_events(job["job_id"])
                                if job_events:
                                    events.extend(job_events)
                    outcomes = client.sync_todo_events(events)
                    self._send_json({"ok": True, "synced_count": len(outcomes), "outcomes": outcomes})
                except Exception as error:
                    self._send_json({"ok": False, "error": str(error)})
                return
            if parsed.path == "/api/passport/cards/action":
                payload = self._body_json()
                action = str(payload.get("action") or "toggle_todo")
                task_key = str(payload.get("task_key") or "")
                new_status = str(payload.get("status") or "completed")
                if action == "toggle_todo" and task_key:
                    result = self.app.store.save_task_state(
                        task_key=task_key,
                        status=new_status,
                        title=payload.get("title"),
                    )
                    openclaw_sync = self._sync_kanban_openclaw(
                        task_key=task_key,
                        status=new_status,
                        title=payload.get("title") or result.get("title"),
                        event_id=payload.get("event_id") or result.get("event_id"),
                    )
                    self._send_json(
                        {"ok": True, "action": action, "task": result, "openclaw": openclaw_sync}
                    )
                else:
                    self._send_json({"ok": True, "action": action})
                return
            if parsed.path == "/api/reminders/schedule":
                payload = self._body_json()
                self.app.sync_local_reminders()
                reminder = self.app.reminders.schedule(
                    str(payload.get("task_key") or ""),
                    remind_at=str(payload.get("remind_at") or ""),
                    title=str(payload.get("title") or "") or None,
                )
                self.app.run_reminder_dispatch()
                self._send_json({"reminder": reminder})
                return
            if parsed.path == "/api/reminders/dispatch":
                self._send_json(
                    {"delivered": self.app.run_reminder_dispatch()}
                )
                return
            if parsed.path == "/api/jobs/recover-interrupted":
                scheduled = self.app.recover_interrupted_jobs()
                self._send_json({"scheduled": scheduled, "count": len(scheduled)})
                return
            if parsed.path == "/api/jobs":
                payload = self._body_json()
                job_id = self.app.store.create_job(
                    start_at=payload.get("start_at"),
                    timezone_name=payload.get("timezone", "Asia/Shanghai"),
                    processing_options=(
                        payload.get("processing_options")
                        if isinstance(payload.get("processing_options"), dict)
                        else None
                    ),
                )
                self._send_json(
                    {"job_id": job_id, **self.app.store.read(job_id)},
                    HTTPStatus.CREATED,
                )
                return
            parts = parsed.path.strip("/").split("/")
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "context-relation-review"
            ):
                payload = self._body_json()
                result = self.app.store.save_context_relation_review(
                    parts[2],
                    artifact_path=str(payload.get("path") or ""),
                    candidate_id=str(payload.get("candidate_id") or ""),
                    decision=payload.get("decision"),
                    note=payload.get("note"),
                )
                self._send_json({"review": result})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "review"
            ):
                payload = self._body_json()
                result = self.app.store.save_review_decision(
                    parts[2],
                    artifact_path=str(payload.get("path") or ""),
                    segment_id=str(payload.get("segment_id") or ""),
                    source_decision=payload.get("source_decision"),
                    transcript_decision=payload.get("transcript_decision"),
                    corrected_text=payload.get("corrected_text"),
                    interlocutor_relation=payload.get("interlocutor_relation"),
                    interlocutor_name=payload.get("interlocutor_name"),
                )
                self._send_json({"review": result})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "reviews-batch"
            ):
                payload = self._body_json()
                results = self.app.store.save_review_decisions_batch(
                    parts[2],
                    artifact_path=str(payload.get("path") or ""),
                    items=list(payload.get("items") or []),
                )
                self._send_json({"reviews": results})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "event-correction"
            ):
                payload = self._body_json()
                raw_changes = payload.get("changes")
                result = self.app.store.save_event_correction(
                    parts[2],
                    artifact_path=str(payload.get("path") or ""),
                    event_id=str(payload.get("event_id") or ""),
                    changes=raw_changes if isinstance(raw_changes, dict) else {},
                    rejected=(
                        bool(payload.get("rejected"))
                        if "rejected" in payload
                        else None
                    ),
                )
                try:
                    from life_logger.flywheel import (
                        record_dismissed_event,
                        record_user_correction,
                    )

                    if payload.get("rejected"):
                        record_dismissed_event(
                            result or {},
                            source_text=str(payload.get("note") or ""),
                            reason="web_user_reject",
                        )
                    elif raw_changes:
                        record_user_correction(
                            payload.get("original") or {},
                            result or {},
                            correction_text=str(payload.get("note") or ""),
                        )
                except Exception:
                    pass
                self._send_json({"correction": result})
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "manual-event"
            ):
                payload = self._body_json()
                result = self.app.store.save_manual_event(
                    parts[2],
                    artifact_path=str(payload.get("path") or ""),
                    title=str(payload.get("title") or ""),
                    categories=list(payload.get("categories") or []),
                    absolute_time=payload.get("absolute_time"),
                    relative_seconds=payload.get("relative_seconds"),
                    time_precision=str(payload.get("time_precision") or "unknown"),
                    activity_type=payload.get("activity_type"),
                    data=payload.get("data")
                    if isinstance(payload.get("data"), dict)
                    else {},
                    note=str(payload.get("note") or "") or None,
                )
                self._send_json({"event": result}, HTTPStatus.CREATED)
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "retry-context-extraction"
            ):
                state = self.app.store.retry_context_extraction(
                    parts[2],
                    self.app.config_path,
                    secrets_path=self.app.store.root / "secrets.json",
                )
                self._send_json(state, HTTPStatus.ACCEPTED)
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] in {"start", "restart"}
            ):
                state = self.app.store.start_job(
                    parts[2],
                    self.app.config_path,
                    secrets_path=self.app.store.root / "secrets.json",
                )
                self._send_json(state)
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (_watch._WatchUploadIncomplete, _watch._WatchUploadTimeout) as error:
            # 上传体问题优先处理：结构化载荷 + 正确状态码（400 短读 / 408 超时）
            _lw.sys.stderr.write(
                f"PUT upload issue on {self.path}: {error.payload.get('error')} "
                f"expected={error.payload.get('expected')} received={error.payload.get('received')}\n"
            )
            _lw.sys.stderr.flush()
            self._send_json(error.payload, error.status)
        except WatchSessionChecksumMismatchError as error:
            self._send_json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except IncompleteWatchSessionError as error:
            self._send_json(
                {"error": str(error), "missing_sequences": error.missing_sequences},
                HTTPStatus.CONFLICT,
            )
        except WatchSessionConflictError as error:
            self._send_json({"error": str(error)}, HTTPStatus.CONFLICT)
        except WatchSessionProcessingError as error:
            self._send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except WatchSessionError as error:
            self._send_json({"error": str(error)}, error.status_code)
        except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._require_api_auth(write=True):
            return
        parts = parsed.path.strip("/").split("/")
        try:
            if (
                len(parts) == 6
                and parts[:2] == ["api", "watch-sessions"]
                and parts[4] == "segments"
            ):
                try:
                    sequence = int(parts[5])
                    start_offset_ms = int(
                        self.headers.get("X-LifeLogger-Start-Offset-Ms", "-1")
                    )
                    duration_ms = int(
                        self.headers.get("X-LifeLogger-Duration-Ms", "-1")
                    )
                    size = int(self.headers.get("Content-Length", "-1"))
                    battery_level_header = self.headers.get(
                        "X-LifeLogger-Battery-Level-Percent"
                    )
                    battery_level_percent = (
                        int(battery_level_header)
                        if battery_level_header is not None
                        else None
                    )
                    battery_state = self.headers.get("X-LifeLogger-Battery-State")
                    hr_header = self.headers.get("X-LifeLogger-Heart-Rate")
                    heart_rate_bpm = float(hr_header) if hr_header else None
                    step_header = self.headers.get("X-LifeLogger-Step-Count")
                    step_count = int(step_header) if step_header else None
                    # [2026-11-07 V68] 当日累计步数 + 值所属的自然日（手表本地时区）。
                    # 老手表不发这三个头 → 全是 None → 服务端走旧逻辑，行为不变。
                    steps_today_header = self.headers.get("X-LifeLogger-Steps-Today")
                    steps_today = int(steps_today_header) if steps_today_header else None
                    steps_date = self.headers.get("X-LifeLogger-Steps-Date") or None
                    steps_sampled_at = self.headers.get("X-LifeLogger-Steps-Sampled-At") or None
                    motion_state = self.headers.get("X-LifeLogger-Motion-State")
                    lat_header = self.headers.get("X-LifeLogger-Location-Latitude")
                    lon_header = self.headers.get("X-LifeLogger-Location-Longitude")
                    acc_header = self.headers.get("X-LifeLogger-Location-Accuracy")
                    ts_header = self.headers.get("X-LifeLogger-Location-Timestamp")
                    src_header = self.headers.get("X-LifeLogger-Location-Source")
                    location_payload = None
                    if lat_header and lon_header and acc_header and ts_header:
                        location_payload = {
                            "latitude": float(lat_header),
                            "longitude": float(lon_header),
                            "horizontal_accuracy": float(acc_header),
                            "timestamp": str(ts_header),
                            "source": str(src_header or "significant_change"),
                        }
                except ValueError as error:
                    raise ValueError("Watch segment headers are invalid") from error
                if not 0 < size <= MAX_WATCH_SEGMENT_BYTES:
                    raise ValueError("Watch segment size is invalid")
                media_type = (
                    self.headers.get("Content-Type", "").split(";", 1)[0].lower()
                )
                if media_type not in {"audio/mp4", "audio/m4a"}:
                    raise ValueError("Watch segment must be M4A/AAC")
                # A client that goes away mid-body (the Watch lowering its wrist, the app being
                # suspended) leaves a short read here. That is a transport outcome, not a bad
                # request, so it must answer 400 rather than raise out of `do_PUT` and become an
                # unhandled traceback in the service log.
                # 【2026-10-22 P-31】分块读 + 读超时 + 结构化错误。
                # 旧写法 `self.rfile.read(size)` 有两个问题：
                #   1) 没有读超时：对端把连接吊着不放时，线程会一直阻塞；
                #   2) 短读只回 `{"error": "Watch segment upload is incomplete"}`，
                #      不带 expected/received/sequence，日志里无法定位是哪一段缺多少。
                previous_timeout = None
                try:
                    previous_timeout = self.connection.gettimeout()
                except OSError:
                    previous_timeout = None
                try:
                    self.connection.settimeout(_lw.WATCH_UPLOAD_READ_TIMEOUT_SECONDS)
                except OSError:
                    pass
                chunks: list[bytes] = []
                received = 0
                try:
                    while received < size:
                        try:
                            chunk = self.rfile.read(min(65536, size - received))
                        except (socket.timeout, TimeoutError) as error:
                            raise _watch._WatchUploadTimeout(
                                size, received, sequence, _lw.WATCH_UPLOAD_READ_TIMEOUT_SECONDS
                            ) from error
                        except (ConnectionResetError, BrokenPipeError, OSError) as error:
                            raise _watch._WatchUploadIncomplete(size, received, sequence) from error
                        if not chunk:
                            break
                        chunks.append(chunk)
                        received += len(chunk)
                finally:
                    try:
                        self.connection.settimeout(previous_timeout)
                    except OSError:
                        pass
                body = b"".join(chunks)
                if len(body) != size:
                    raise _watch._WatchUploadIncomplete(size, len(body), sequence)
                result = self.app.watch_session_store.receive_segment(
                    device_id=parts[2],
                    session_id=parts[3],
                    sequence=sequence,
                    start_offset_ms=start_offset_ms,
                    duration_ms=duration_ms,
                    checksum=str(self.headers.get("X-LifeLogger-Checksum") or ""),
                    body=body,
                    battery_level_percent=battery_level_percent,
                    battery_state=battery_state,
                    heart_rate_bpm=heart_rate_bpm,
                    step_count=step_count,
                    steps_today=steps_today,
                    steps_date=steps_date,
                    steps_sampled_at=steps_sampled_at,
                    motion_state=motion_state,
                    location=location_payload,
                    sleep=_watch._parse_sleep_header(self.headers.get("X-LifeLogger-Sleep")),
                    headers=self.headers,
                )
                self._send_json(
                    {
                        "ok": True,
                        "duplicate": bool(result.get("duplicate")),
                        "ack": {
                            "session_id": result["session_id"],
                            "sequence": result["sequence"],
                            "sha256": result["sha256"],
                            "stored_bytes": result["stored_bytes"],
                            "acknowledged_at": result["acknowledged_at"],
                            **({"haptic": result["haptic"]} if result.get("haptic") else {}),
                        },
                    },
                    HTTPStatus.OK if result.get("duplicate") else HTTPStatus.CREATED,
                )
                return
            if (
                len(parts) == 4
                and parts[:2] == ["api", "jobs"]
                and parts[3] == "files"
            ):
                job_id = parts[2]
                name = parse_qs(parsed.query).get("name", [""])[0]
                if not name:
                    raise ValueError("缺少文件名")
                content_length = self.headers.get("Content-Length")
                if content_length is None:
                    raise ValueError("上传请求必须提供 Content-Length")
                expected_size = int(content_length)
                if expected_size < 0:
                    raise ValueError("Content-Length 无效")
                result = self.app.store.add_file(
                    job_id,
                    filename=unquote(name),
                    relative_path=unquote(self.headers.get("X-Relative-Path", name)),
                    stream=self.rfile,
                    modified_at=float(self.headers.get("X-File-Mtime", "0")),
                    max_bytes=expected_size,
                    reuse_existing=self.headers.get("X-Reuse-Existing") == "1",
                )
                self._send_json(result, HTTPStatus.CREATED)
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (_watch._WatchUploadIncomplete, _watch._WatchUploadTimeout) as error:
            # 【2026-10-22 P-31】上传体问题：结构化载荷 + 语义正确的状态码
            # （400 短读 / 408 超时）。必须排在通用 ValueError/OSError 之前，
            # 否则会被降级成不带 expected/received/sequence 的裸 400。
            _lw.sys.stderr.write(
                f"PUT upload issue on {self.path}: {error.payload.get('error')} "
                f"expected={error.payload.get('expected')} received={error.payload.get('received')}\n"
            )
            _lw.sys.stderr.flush()
            self._send_json(error.payload, error.status)
        except WatchSessionChecksumMismatchError as error:
            self._send_json({"error": str(error)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except WatchSessionConflictError as error:
            self._send_json({"error": str(error)}, HTTPStatus.CONFLICT)
        except WatchSessionError as error:
            self._send_json({"error": str(error)}, error.status_code)
        except (KeyError, ValueError, OSError) as error:
            _lw.sys.stderr.write(f"PUT error on {self.path}: {type(error).__name__} {error}\n")
            _lw.sys.stderr.flush()
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

