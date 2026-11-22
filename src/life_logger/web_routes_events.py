"""从 _Handler 机械提取的路由，分支体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

import sys
from http import HTTPStatus
from life_logger.local_habits import HabitNotFoundError
from life_logger.timeutil import LOCAL_TZ
from life_logger.web_common import _read_contract
from life_logger.web_expense_range import build_expense_range
from life_logger.web_health import _synthesis_staleness
from life_logger.web_openclaw import _openclaw_task_read_metadata
from pathlib import Path
from typing import Any
from typing import Mapping
from urllib.parse import parse_qs
from urllib.parse import unquote
import json
import logging
import os
import time

logger = logging.getLogger(__name__)


def _attach_task_input_context(
    tasks: Any, root: Path
) -> list[dict[str, Any]]:
    """给 life_logger_todos 补 input_context（失败只降级成 null，不影响任务列表）。"""
    rows = tasks if isinstance(tasks, list) else []
    try:
        from life_logger.openclaw_task_context import attach_input_context

        return attach_input_context(rows, root=root)
    except Exception as error:  # noqa: BLE001 - 读层增强绝不能整个接口报错
        logger.warning("life_logger_todos 输入上下文补全失败: %s: %s", type(error).__name__, error)
        return [dict(row) if isinstance(row, Mapping) else row for row in rows]


class _EventRoutesMixin:
    def _get_dashboard_daily(self, parsed):
        from life_logger.local_web import (
            _watch_last_segment_received,
            datetime,
        )

        query_params = parse_qs(parsed.query)
        target_date = (query_params.get("date") or [datetime.now(tz=LOCAL_TZ).strftime("%Y-%m-%d")])[0]
        from life_logger.daily_dashboard import DailyDashboardBuilder
        builder = DailyDashboardBuilder(
            self.app.store.root,
            # 生活看板隐藏 dictation 内容：场景 / 即时抓取 / 数字一起按过滤后算
            dictation_filter=self.app.dashboard_dictation_filter(),
            use_scan_cache=True,
        )
        dash = self.app.cached_read(
            f"dashboard|{target_date}",
            lambda: builder.build_for_date(target_date),
        )
        dashboard_payload = dash.to_dict()
        read_status = getattr(dash, "_read_status", None)
        if read_status:
            dashboard_payload["read_status"] = read_status
            dashboard_payload["read_note"] = getattr(dash, "_read_note", "")
        watch_last = _watch_last_segment_received(
            self.app.watch_session_store.root, target_date
        )
        # Keep the dashboard object's existing shape and add the real
        # timestamp both inside it (timeline consumers) and top-level
        # (other readers); never substitute the processing watermark.
        dashboard_payload.update(watch_last)
        self._send_json(
            {
                "ok": True,
                "dashboard": dashboard_payload,
                **({"read_status": read_status, "read_note": getattr(dash, "_read_note", "")} if read_status else {}),
                **watch_last,
                "read_contract": _read_contract(
                    "diagnostic_dashboard",
                    "raw",
                    canonical=False,
                    notice="DailyDashboardBuilder is dashboard-worker owned; use /api/stats, /api/daily or /api/daily-brief for canonical event accounting.",
                ),
            }
        )
        return

    def _get_timeline_scenes(self, parsed):
        from life_logger.local_web import (
            datetime,
        )

        query_params = parse_qs(parsed.query)
        target_date = (query_params.get("date") or [datetime.now(tz=LOCAL_TZ).strftime("%Y-%m-%d")])[0]
        from life_logger.daily_dashboard import DailyDashboardBuilder
        builder = DailyDashboardBuilder(
            self.app.store.root,
            dictation_filter=self.app.dashboard_dictation_filter(),
            use_scan_cache=True,
        )
        scenes = builder.build_timeline_scenes(target_date)
        # 【2026-10-17】同时给出动作组：场景=「哪类环境」，动作组=「在干什么」。
        # 全仓此前 action_group 命中 0 次，Web 端没有任何动作组标签。
        action_groups = builder.build_action_groups(target_date, scenes)
        self._send_json(
            {
                "ok": True,
                "date": target_date,
                "scenes": scenes,
                "action_groups": action_groups,
                "read_contract": _read_contract(
                    "diagnostic_dashboard",
                    "raw",
                    canonical=False,
                    notice="Timeline scenes are dashboard-worker owned; not canonical event accounting.",
                ),
            }
        )
        return

    def _get_version(self, parsed):
        from life_logger.local_web import (
            ControlSnapshotBusy,
        )

        # 版本端点独立存在：不需要读任何业务数据，也不返回任何路径/密钥。
        try:
            info, cached = self.app.control_snapshot_meta(
                "version", self.app._fast_version_info, ttl=30.0
            )
            self._send_json({"ok": True, **info, "cached": cached})
        except ControlSnapshotBusy:
            # Version has a safe static fallback; never disconnect a client
            # just because another request is refreshing the snapshot.
            from life_logger.version import APP_VERSION, RELEASE_NOTE, deployed_at

            self._send_json(
                {
                    "ok": True,
                    "app_version": APP_VERSION,
                    "release_note": RELEASE_NOTE,
                    "code_fingerprint": "",
                    "git_commit": "",
                    "deployed_at": deployed_at(self.app.started_at),
                    "pid": os.getpid(),
                    "unloaded_files": [],
                    "deploy_state": "unknown",
                    "cached": False,
                    "degraded": True,
                    "snapshot_busy": True,
                    "retry_after": 1,
                    "retry_after_ms": 1000,
                }
            )
        return

    def _kanban_todo_events(self):
        """喂给自动同步的 todo 事件：只给最近 7 天、本地还没关闭的。

        自动补建是“新语音待办自动进 OpenClaw”，不是把历史全搬过去；
        否则早已完成或过期的旧待办会以 pending 灌进每天三次的汇报卡。
        """
        from datetime import datetime, timedelta

        events: list[dict] = []
        try:
            jobs = self.app.store.list()
        except Exception:  # noqa: BLE001
            return events
        cutoff = (datetime.now(LOCAL_TZ) - timedelta(days=7)).strftime("%Y%m%d")
        closed: set[str] = set()
        try:
            for state in self.app.store.read_task_states():
                if state.get("status") in {"completed", "dismissed"} and state.get("event_id"):
                    closed.add(str(state["event_id"]))
        except Exception:  # noqa: BLE001
            return events
        for job in jobs:
            if len(events) >= 400:
                break
            # degraded 的产物是可用的（只是过程有子步骤失败），事件一样要同步
            if job.get("status") not in {"completed", "degraded"}:
                continue
            job_id = job.get("job_id")
            if not job_id:
                continue
            # job_id 形如 job-20261105-145815-xxxx，日期段早于 7 天前的不再自动补建。
            parts = str(job_id).split("-")
            if len(parts) < 2 or not parts[1].isdigit() or parts[1] < cutoff:
                continue
            try:
                job_events = self.app.store.read_job_events(job_id)
            except Exception:  # noqa: BLE001
                continue
            for event in job_events or []:
                if isinstance(event, dict) and str(event.get("event_id") or "") not in closed:
                    events.append(event)
                if len(events) >= 400:
                    break
        return events

    def _get_tasks(self, parsed):
        from life_logger.local_web import (
            ControlSnapshotBusy,
        )

        local_tasks = self.app.store.read_task_states()

        # 【2026-11-06】看板读取时，顺手把「新增的语音待办」推进 OpenClaw，
        # 并补一次失败队列（bridge 恢复了这里会自动补，不用人工点同步）。
        # 有 60s TTL + 全程 try/except：读接口不能因为 bridge 挂了就打不开。
        try:
            self._kanban_openclaw().auto_sync(self._kanban_todo_events, ttl=60.0)
        except Exception as error:  # noqa: BLE001
            sys.stderr.write(
                f"[life-logger] 看板自动同步跳过：{type(error).__name__} {error}\n"
            )
            sys.stderr.flush()

        def _fetch_openclaw_tasks():
            from life_logger.openclaw_bridge import OpenClawBridgeClient

            # /api/tasks is a control/status surface: one bounded call,
            # no separate is_healthy probe (which used to double the 10s timeout).
            client = OpenClawBridgeClient(timeout=2.0)
            return client.get_unified_dashboard_todos()

        try:
            openclaw_info, cached = self.app.control_snapshot_meta(
                "tasks|openclaw", _fetch_openclaw_tasks,
                ttl=10.0, wait_timeout=1.5,
            )
            if not isinstance(openclaw_info, dict):
                raise ValueError("OpenClaw returned a non-object payload")
            openclaw_info = {**openclaw_info, "degraded": False, "cached": cached}
            openclaw_info.update(
                _openclaw_task_read_metadata(
                    openclaw_info,
                    error=(None if not openclaw_info.get("degraded") else str(openclaw_info.get("error") or "")),
                )
            )
        except ControlSnapshotBusy:
            openclaw_info = {
                "degraded": True,
                "cached": False,
                "error": "control_refresh_busy",
                "reason": "OpenClaw refresh is already in flight; retry shortly",
                "retry_after": 2,
                "retry_after_ms": 2000,
            }
            openclaw_info.update(
                _openclaw_task_read_metadata(
                    openclaw_info, error=openclaw_info["error"]
                )
            )
        except Exception as error:
            # Do not invent an empty task list; make the unavailable
            # dependency visible and let the UI retry.
            openclaw_info = {
                "degraded": True,
                "cached": False,
                "error": "openclaw_unavailable",
                "reason": str(error)[:200],
                "retry_after": 5,
                "retry_after_ms": 5000,
            }
            openclaw_info.update(
                _openclaw_task_read_metadata(
                    openclaw_info, error=str(error)[:200]
                )
            )
        self._send_json({"tasks": local_tasks, "openclaw": openclaw_info})
        return

    def _get_openclaw_tasks(self, parsed):
        try:
            from life_logger.openclaw_bridge import OpenClawBridgeClient

            client = OpenClawBridgeClient()
            if client.is_healthy():
                data = client.get_unified_dashboard_todos()
                payload = {"connected": True, **data}
                # [2026-11-06] 语音待办补「当时在对哪个应用说话」：
                # OpenClaw 汇报卡只有 title，会把「对编程工具说的话」当生活待办上卡。
                # 只在**读层**加字段，不动上游任务、不改排序、判不出来就是 null。
                # 上游本来没这个键就**不新增**，保持原来的响应形状。
                if "life_logger_todos" in payload:
                    payload["life_logger_todos"] = _attach_task_input_context(
                        payload.get("life_logger_todos"), self.app.store.root
                    )
                payload.update(_openclaw_task_read_metadata(payload))
                self._send_json(payload)
            else:
                payload = {
                    "connected": False,
                    "total_open": 0,
                    "wechat_matters": [],
                    "life_logger_todos": [],
                    "general_todos": [],
                    "error": "OpenClaw bridge unreachable",
                }
                payload.update(
                    _openclaw_task_read_metadata(
                        payload, error=payload["error"]
                    )
                )
                self._send_json(payload)
        except Exception as error:
            payload = {
                "connected": False,
                "total_open": 0,
                "wechat_matters": [],
                "life_logger_todos": [],
                "general_todos": [],
                "error": str(error),
            }
            payload.update(
                _openclaw_task_read_metadata(payload, error=str(error))
            )
            self._send_json(payload)
        return

    def _get_pending_sources(self, parsed):
        # 待人工审核（context-extraction.json 的 pending_sources）的只读投影。
        # 设计意图见 pending_review 模块头：模型判「无事件」但有指令嫌疑的
        # 来源不能静默丢失。必须按 reason 分类返回：整批 API 失败
        # （deepseek_batch_failed）默认只给计数、不给条目，
        # 否则 3600+ 条垃圾会把真条目淹掉。
        from life_logger.pending_review import (
            PendingReviewError,
            collect_pending_sources_cached,
        )

        query = parse_qs(parsed.query)

        def pending_flag(name: str) -> bool:
            return str((query.get(name) or ["0"])[0]).lower() in {
                "1",
                "true",
                "yes",
            }

        try:
            # 【2026-10-24 性能】走带 10s 缓存 + single-flight 的包装：
            # 审核后刷新不再每次重读 389 个产物；写路径由 invalidate_pending_cache()
            # 立刻失效，保证写后第一次读就是新状态。
            payload = collect_pending_sources_cached(
                self.app.store.root,
                date=unquote((query.get("date") or [""])[0]) or None,
                include_collapsed=pending_flag("include_collapsed"),
                include_resolved=pending_flag("include_resolved"),
                limit=(query.get("limit") or [None])[0],
            )
        except PendingReviewError as error:
            self._send_json(
                {"ok": False, "error": str(error)},
                HTTPStatus.BAD_REQUEST,
            )
            return
        self._send_json(payload)
        return

    def _get_daily_brief(self, parsed):
        query = parse_qs(parsed.query)
        date = unquote(query.get("date", [""])[0]) or None
        brief = self.app.build_agent_brief(
            date=date,
            # An explicitly requested empty day is a valid read, not
            # an error. Invalid scope/view still raises 400.
            allow_empty=True,
            # 生活看板（含小苏/微信只读投影）隐藏 dictation 内容
            hide_dictation=True,
            scope=unquote(query.get("scope", ["provisional"])[0]),
            view=unquote(query.get("view", ["display"])[0]),
        )
        target_date = str(brief.get("date") or "")
        synthesis = None
        if target_date:
            synthesis_file = (
                self.app.store.root
                / "daily-synthesis"
                / f"synthesis-{target_date}.json"
            )
            if synthesis_file.is_file():
                try:
                    synthesis = json.loads(
                        synthesis_file.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, TypeError):
                    synthesis = None
        brief.update(_synthesis_staleness(brief, synthesis))
        self._send_json(brief)
        return

    def _get_daily(self, parsed):
        from life_logger.local_web import (
            _capture_funnel_nonblocking,
            _env_flag,
        )

        query = parse_qs(parsed.query)
        date = unquote(query.get("date", [""])[0]) or None
        allow_empty = str(query.get("allow_empty", ["true"])[0]).lower() in {"1", "true", "yes"} or date is None
        _scope_d = unquote(query.get("scope", ["provisional"])[0])
        _view_d = unquote(query.get("view", ["display"])[0])
        _t0 = time.monotonic()
        try:
            brief = self.app.cached_read(
                f"daily|{date}|{_scope_d}|{_view_d}|{allow_empty}",
                lambda: self.app.build_daily_brief(
                    date=date, allow_empty=allow_empty,
                    scope=_scope_d, view=_view_d,
                ),
            )
        except TypeError as error:
            # Compatibility for old test doubles/handlers that only
            # accept ``date``. Any other TypeError is a real bug and
            # must not silently drop scope/view validation.
            if "unexpected keyword argument" not in str(error):
                raise
            brief = self.app.build_daily_brief(date=date)
        _t_brief = time.monotonic()
        stale_read = {key: brief[key] for key in ("read_status", "read_note") if key in brief} if brief.get("read_status") == "stale_busy" else {}
        events_list = list(brief.get("events", []))
        # [2026-10-13 root 拍板] 生活看板隐藏「电脑前说话」(dictation) 内容：
        # 只动读层（含下面的台账/便签/计数），产物文件与待确认区一律不动。
        # 开关见 local-config.json 的 hide_dictation_from_dashboard。
        brief, events_list = self.app.apply_dashboard_dictation_filter(
            brief, events_list
        )
        _t_filter = time.monotonic()
        from life_logger.daily_cognitive_session import (
            build_work_cognition_ledger,
            build_pinned_memos,
        )
        target_date = str(brief.get("date") or "")
        from life_logger.day_facts import day_expense_ledger, domain_ledgers
        from life_logger.broadcast_decisions import (
            apply_decisions as _apply_broadcast_decisions,
            load_decisions as _load_broadcast_decisions,
        )

        def _ledgers() -> dict[str, Any]:
            _domains = domain_ledgers(events_list, target_date, root=self.app.store.root)
            expense = day_expense_ledger(events_list, target_date, root=self.app.store.root)
            # [2026-10-23 P-51] 人工确认的播报候选只在读层叠加，不改抽取产物/expense_policy。
            expense = _apply_broadcast_decisions(
                expense,
                _load_broadcast_decisions(self.app.store.root, target_date),
            )
            return {
                "nutrition": _domains["nutrition"],
                "fitness": _domains["fitness"],
                "smoking": _domains["smoking"],
                "expense": expense,
                "work": build_work_cognition_ledger(events_list),
                "memos": build_pinned_memos(events_list),
            }

        # [2026-11-08] 四本账也进读快照缓存（线上实测账本这一步在并发下要 1–3 秒，
        # 原来每次请求都在缓存外重算）。写失效同样靠 epoch。
        cached_read = getattr(self.app, "cached_read", None)
        if callable(cached_read) and target_date:
            ledgers = cached_read(
                f"daily-ledgers|{target_date}|{_scope_d}|{_view_d}|{allow_empty}", _ledgers,
            )
        else:
            ledgers = _ledgers()
        nutrition = ledgers["nutrition"]
        fitness = ledgers["fitness"]
        smoking = ledgers["smoking"]
        expense = ledgers["expense"]
        work = ledgers["work"]
        memos = ledgers["memos"]
        _t_ledgers = time.monotonic()
        synthesis_file = self.app.store.root / "daily-synthesis" / f"synthesis-{target_date}.json"
        synthesis = None
        if synthesis_file.is_file():
            try:
                synthesis = json.loads(synthesis_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        # 【2026-10-19】读层兜底：历史缓存里的金额可能来自旧账本
        # （实测 synthesis-2026-10-17.json 存的是 16.6，当前账本是 26.8，
        #   因为缓存生成时烧鸭饭还没被合成、复盘可乐还重复计账）。
        # 任务要求「不许手工改历史 JSON」，所以这里只做**读时对齐**：
        # 展示口径一律用当前账本，旧快照留在 stale_ledger_snapshot 里可审计。
        from life_logger.daily_cognitive_session import reconcile_synthesis_expense

        synthesis = reconcile_synthesis_expense(synthesis, expense)
        synthesis_status = _synthesis_staleness(brief, synthesis)
        _t_synthesis = time.monotonic()
        # [2026-10-16] 信息漏斗：这一天「说了多少句」。
        # 存在理由：系统此前没有任何 loss 指标，用户看不到自己说过的话被丢了多少。
        # 前端拿它和 events 数一比，就能显示「说了 N 句 / 记住 M 条」。
        if _env_flag("LIFE_LOGGER_SYNC_FUNNEL"):
            from life_logger.capture_funnel import compute_capture_funnel

            try:
                funnel = compute_capture_funnel(self.app.store.root, target_date) if target_date else {}
            except Exception as exc:  # 指标算挂了不能拖垮日记页
                funnel = {"error": f"{type(exc).__name__}: {exc}", "date": target_date}
                print(
                    f"[life-logger] capture_funnel 计算失败 date={target_date}: {exc}",
                    flush=True,
                )
        else:
            funnel = _capture_funnel_nonblocking(self.app.store.root, target_date)
        _t_funnel = time.monotonic()
        if _t_funnel - _t0 > 0.5:
            print(
                "[timing] /api/daily "
                f"date={target_date or date or '-'} "
                f"total={(_t_funnel - _t0) * 1000:.0f}ms "
                f"brief={(_t_brief - _t0) * 1000:.0f}ms "
                f"filter={(_t_filter - _t_brief) * 1000:.0f}ms "
                f"ledgers={(_t_ledgers - _t_filter) * 1000:.0f}ms "
                f"synthesis={(_t_synthesis - _t_ledgers) * 1000:.0f}ms "
                f"funnel={(_t_funnel - _t_synthesis) * 1000:.0f}ms",
                flush=True,
            )
        self._send_json(
            {
                "date": brief.get("date"),
                **stale_read,
                "daily_summary": brief,
                "events": events_list,
                "nutrition_ledger": nutrition,
                "fitness_ledger": fitness,
                "smoking_ledger": smoking,
                "expense_ledger": expense,
                "work_cognition_ledger": work,
                "pinned_memos": memos,
                "synthesis": synthesis,
                **synthesis_status,
                # 【2026-10-19 P-14】降级可见：把模型档位与降级原因提到顶层，
                # 前端不必深挖 synthesis 就能判断「今天的日报是不是降级产出的」。
                "model_tier": (synthesis or {}).get("model_tier"),
                "fallback_reason": (synthesis or {}).get("fallback_reason"),
                "capture_funnel": funnel,
                "read_contract": _read_contract(
                    str(brief.get("accounting_scope") or "provisional"),
                    str(brief.get("view") or "display"),
                ),
            }
        )
        return

    def _get_expense_pending_broadcast(self, parsed):
        # [2026-10-23 P-51] 播报候选列表；直接复用账本里的 pending_broadcast。
        from life_logger.broadcast_decisions import BroadcastDecisionError

        query = parse_qs(parsed.query)
        date = unquote(query.get("date", [""])[0])
        try:
            _base, _decisions, ledger = self.app._broadcast_expense_state(date)
        except BroadcastDecisionError as error:
            self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, ValueError) as error:
            self._send_json(
                {"ok": False, "error": f"cannot read pending broadcast: {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        pending = list(ledger.get("pending_broadcast") or [])
        self._send_json(
            {
                "ok": True,
                "date": date,
                "pending_broadcast": pending,
                "pending_broadcast_count": len(pending),
                "pending_broadcast_total_cny": ledger.get("pending_broadcast_total_cny"),
                "total_amount": ledger.get("total_amount"),
            }
        )
        return

    def _get_expense_range(self, parsed):
        # 【2026-10-19 A0 派单 P-6】区间账本聚合：与 /api/daily 同源，
        # 缺日报的天标 missing（金额 null，不按 0 混进总额）。
        query = parse_qs(parsed.query)
        date_from = unquote(query.get("from", [""])[0])
        date_to = unquote(query.get("to", [""])[0])
        scope = unquote(query.get("scope", ["provisional"])[0])
        view = unquote(query.get("view", ["display"])[0])
        if not date_from or not date_to:
            self._send_json(
                {"error": "from 与 to 必填（YYYY-MM-DD）"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            payload = build_expense_range(
                self.app.store.root,
                date_from=date_from,
                date_to=date_to,
                scope=scope,
                view=view,
                event_filter=self.app.dashboard_dictation_filter().filter_events,
            )
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, **payload})
        return

    def _get_search(self, parsed):
        query = parse_qs(parsed.query)
        text_query = unquote(query.get("q", [""])[0])
        result_type = unquote(query.get("type", ["all"])[0])
        date = unquote(query.get("date", [""])[0]) or None
        date_from = unquote(query.get("from", [""])[0]) or None
        date_to = unquote(query.get("to", [""])[0]) or None
        category = unquote(query.get("category", [""])[0]) or None
        amount_min = unquote(query.get("amount_min", [""])[0]) or None
        amount_max = unquote(query.get("amount_max", [""])[0]) or None
        import_source = unquote(query.get("import_source", ["all"])[0])
        raw_limit = unquote(query.get("limit", ["100"])[0])
        self._send_json(
            {
                "query": text_query,
                "read_contract": _read_contract(
                    "diagnostic_raw",
                    "raw",
                    canonical=False,
                    date_basis="artifact timestamp prefix; transcript time is not promoted to occurrence time",
                    notice="Diagnostic search payload; event accounting must use the shared view through /api/stats, /api/daily or /api/daily-brief.",
                ),
                "results": self.app.store.search(
                    text_query,
                    result_type=result_type,
                    date=date,
                    date_from=date_from,
                    date_to=date_to,
                    category=category,
                    amount_min=amount_min,
                    amount_max=amount_max,
                    import_source=import_source,
                    limit=int(raw_limit),
                ),
            }
        )
        return

    def _post_events_natural_correction(self, parsed):
        payload = self._body_json()
        from life_logger.intent_pool import ActiveIntentPool
        from life_logger.natural_correction import (
            DeepSeekTriIntentParser,
            process_natural_correction,
        )

        # 【2026-10-15 修】原先这里是
        #   intent_pool_path = self.app.store.root / "active-intents.json"
        # 而 store.root = output/local-web（DEFAULT_JOB_ROOT），该路径下**没有**
        # 这个文件；扫描/流水线产出的意图池其实在 output/active-intents.json
        # （intent_pool.DEFAULT_INTENT_POOL_PATH，实测 199KB 且在持续更新）。
        # 后果：网页端自然纠错读写的是一个凭空新建的空池，与扫描产出完全不通，
        # 「跨批次未结意图池」形同虚设 —— 磁盘上因此出现 26 份互不相通的副本。
        # 正解：不传路径，让 ActiveIntentPool 走 get_default_intent_pool_path()
        # （它还支持 LIFE_LOGGER_INTENT_POOL_PATH 环境变量覆盖）。
        pool = ActiveIntentPool()

        # 【2026-10-14 修】不传 parser 的严重后果：DeepSeekTriIntentParser 默认
        # api_key 从 os.environ 取，而 8765 由 launchd 启动、环境里根本没有
        # DEEPSEEK_API_KEY → self.api_key 为空 → parse() 直接走 extract_correction_fields()
        # 的**离线正则**兜底。实测「把咖啡改成18元」被抽成实体「把咖啡改成」，
        # UPDATE 退化成"最新一条事件"兜底，等于纠错功能半残。
        # 另外它的 base_url 默认值是 DEFAULT_DEEPSEEK_URL = https://api.deepseek.com，
        # 与本项目「全走 Command Code、不走官网」的红线冲突。
        # 这里按既有模式（config + secrets）显式构造 parser，复用 local_pipeline_env()
        # 组出的三个变量，保证与流水线同端点、同模型。
        # 【2026-10-15 修】默认就构造**明确的离线 parser**（api_key=""），
        # 而不是留 None。因为 process_natural_correction 里是
        # `intent_parser = parser or DeepSeekTriIntentParser()` —— 传 None
        # 等于"现造一个会捡 DEEPSEEK_API_KEY 的联网实例"，与下面注释
        # 承诺的「离线正则兜底」直接矛盾（测试从 shell 启动时环境有 key，
        # 于是测试真的联网、结果不确定；生产由 launchd 启动恰好没 key 才侥幸离线）。
        parser = DeepSeekTriIntentParser(api_key="")
        try:
            import json as _json

            _cfg: dict[str, Any] = {}
            try:
                _raw_cfg = _json.loads(
                    Path(self.app.config_path).read_text(encoding="utf-8")
                )
                if isinstance(_raw_cfg, dict):
                    _cfg = _raw_cfg
            except (OSError, ValueError):
                _cfg = {}
            try:
                _raw_secrets = _json.loads(
                    (self.app.store.root / "secrets.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                _raw_secrets = {}
            if isinstance(_raw_secrets, dict) and _raw_secrets.get("deepseek_api_key"):
                _cfg["_deepseek_api_key"] = str(_raw_secrets["deepseek_api_key"])
                _sec_deepseek = _cfg.get("deepseek")
                if not isinstance(_sec_deepseek, dict):
                    _sec_deepseek = {}
                    _cfg["deepseek"] = _sec_deepseek
                _sec_deepseek["enabled"] = True

            from life_logger.local_pipeline import local_pipeline_env

            _env = local_pipeline_env(_cfg)
            _ds_kwargs: dict[str, Any] = {
                "api_key": str(_env.get("DEEPSEEK_API_KEY") or "").strip() or None,
            }
            _ds_base = str(_env.get("DEEPSEEK_BASE_URL") or "").strip()
            if _ds_base:
                _ds_kwargs["base_url"] = _ds_base
            _ds_model = str(_env.get("DEEPSEEK_MODEL") or "").strip()
            if _ds_model:
                _ds_kwargs["model"] = _ds_model
            if _ds_kwargs.get("api_key"):
                parser = DeepSeekTriIntentParser(**_ds_kwargs)
        except Exception:
            # 构造失败就退回**离线正则**兜底（不能整条挂掉）。
            # 注意：这里必须给一个明确的离线 parser，不能留 None —— 留 None 会被
            # process_natural_correction 里的 `parser or DeepSeekTriIntentParser()`
            # 变成"再造一个联网实例"。
            parser = DeepSeekTriIntentParser(api_key="")

        try:
            result = process_natural_correction(
                self.app.store,
                text=str(payload.get("text") or ""),
                target_date=payload.get("date"),
                job_id=payload.get("job_id"),
                artifact_path=payload.get("artifact_path"),
                target_event_id=payload.get("target_event_id"),
                intent_pool=pool,
                parser=parser,
            )
            status_code = (
                HTTPStatus.OK
                if result.get("status") == "ok"
                else HTTPStatus.BAD_REQUEST
            )
            self._send_json(result, status_code)
        except Exception as error:
            self._send_json(
                {"status": "error", "error": str(error)},
                HTTPStatus.BAD_REQUEST,
            )
        return

    def _post_habits_checkin(self, parsed):
        payload = self._body_json()
        target = str(
            payload.get("target")
            or payload.get("habit_key")
            or payload.get("title")
            or ""
        )
        try:
            result = self.app.habits.checkin(
                target,
                date=payload.get("date"),
                undo=bool(payload.get("undo")),
            )
        except HabitNotFoundError as error:
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

    def _get_nutrition_day(self, parsed):
        from life_logger.nutrition_ingest import NutritionStorageError, read_day

        if not self.app.access_token:
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        query = parse_qs(parsed.query)
        try:
            result = read_day(self.app.store.root, (query.get("date") or [None])[0])
        except NutritionStorageError as error:
            self._send_json({"ok": False, "error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        except ValueError as error:
            self._send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def _post_nutrition_voice(self, parsed):
        # [2026-10-13] 「念一次存一次」：念一句营养成分表 → 写进自建品牌库（四层链第①层）。
        # 与 /api/habits/voice 同款：给文字（Watch ASR / 微信 / 网页输入框）或给 captures。
        payload = self._body_json()
        captures = payload.get("captures")
        if not captures:
            from life_logger.voice_triggers import scan_voice_triggers

            spoken = str(payload.get("text") or "")
            captures = [
                capture.to_dict() for capture in scan_voice_triggers(spoken)
            ]
        from life_logger.food_lookup import (
            apply_nutrition_captures,
            default_food_lookup,
        )

        lookup = default_food_lookup()
        outcomes = apply_nutrition_captures(lookup, captures)
        stored = [
            {
                "item": str((capture.get("metadata") or {}).get("item") or ""),
                "per_100g": (capture.get("metadata") or {}).get("per_100g"),
            }
            for capture in captures
            if isinstance(capture, dict)
            and isinstance(capture.get("metadata"), dict)
            and (capture.get("metadata") or {}).get("action") == "remember_food"
        ]
        self._send_json(
            {
                "ok": True,
                "applied_count": len([o for o in outcomes if o.get("applied")]),
                "outcomes": outcomes,
                "stored": stored,
                "library_path": str(lookup.brand.path) if lookup.brand.path else "",
            }
        )
        return
