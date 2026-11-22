"""LocalWebServer 的方法，从 local_web.py 机械搬出，方法体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

from life_logger.timeutil import LOCAL_TZ
from life_logger.web_common import PROJECT_ROOT
from life_logger.web_common import logger
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from typing import Mapping
import hashlib
import json
import os
import threading
import time


class _ServerServicesMixin:
    def doctor_report(self) -> dict[str, Any]:
        """Single-flight, short TTL; changed config/credentials invalidate immediately."""
        from life_logger.local_web import (
            SERVER_BUILD_ID,
            run_doctor,
        )

        with self._doctor_cache_lock:
            fingerprints = []
            for path in (self.config_path, self.store.root / "preferences.json",
                         self.store.root / "secrets.json"):
                try:
                    fingerprints.append(hashlib.sha256(path.read_bytes()).hexdigest())
                except OSError:
                    fingerprints.append("missing")
            key = ":".join(fingerprints)
            now = time.monotonic()
            cached = self._doctor_cache
            if cached is not None and cached[0] == key and now - cached[1] < 30:
                return dict(cached[2])
            report = run_doctor(
                self.config_path, job_root=self.store.root,
                web_health={"ok": True, "server_build_id": SERVER_BUILD_ID,
                            "server_started_at": self.started_at},
            )
            self._doctor_cache = (key, time.monotonic(), report)
            return dict(report)

    def _watch_reception_status_summary(
        self,
        recent_asr_summaries: dict[tuple[str, str], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Return a short-lived all-session aggregate without transcript payloads."""
        with self._watch_status_summary_lock:
            now = time.monotonic()
            cached = self._watch_status_summary_cache
            if cached is not None and now - self._watch_status_summary_cached_at < 10:
                return cached

            all_sessions = self.watch_session_store.list_session_summaries(limit=None)
            result: dict[str, Any] = {
                "complete": True,
                "session_count": len(all_sessions),
                "lifecycle_counts": {
                    key: 0 for key in (
                        "receiving", "complete", "incomplete", "stalled",
                        "superseded", "abandoned", "other",
                    )
                },
                "asr_counts": {
                    key: 0 for key in (
                        "pending", "processing", "completed", "no_speech", "failed",
                    )
                },
            }
            # The store omits unreadable manifests. Detect omissions by comparing the raw
            # inventory, and never label a partial aggregate complete.
            try:
                manifest_count = sum(
                    1 for _ in self.watch_session_store.root.glob("*/*/manifest.json")
                )
                if manifest_count != len(all_sessions):
                    result["complete"] = False
            except OSError:
                result["complete"] = False

            job_status_cache: dict[str, str | None] = {}
            for item in all_sessions:
                lifecycle = item.get("status") or item.get("state")
                lifecycle_key = (
                    lifecycle
                    if isinstance(lifecycle, str) and lifecycle in result["lifecycle_counts"]
                    else "other"
                )
                result["lifecycle_counts"][lifecycle_key] += 1

                key = (str(item.get("device_id") or ""), str(item.get("session_id") or ""))
                if key in recent_asr_summaries:
                    segment_asr = recent_asr_summaries[key]
                else:
                    try:
                        segment_asr = self.watch_formal_asr.summary_for_session(
                            key[0], key[1], include_transcripts=False,
                        )
                    except (KeyError, ValueError, OSError, TypeError):
                        segment_asr = None
                if not isinstance(segment_asr, dict):
                    result["complete"] = False
                    counts = None
                    checkpoint_count = None
                else:
                    counts = segment_asr.get("counts")
                    checkpoint_count = segment_asr.get("checkpoint_count")
                    if not isinstance(counts, dict) or not isinstance(checkpoint_count, int) or isinstance(checkpoint_count, bool):
                        result["complete"] = False

                # Only legacy sessions with no segment checkpoints need their linked Job
                # read to decide whether synthetic pending counts should be suppressed.
                terminal_legacy_job = False
                job_id = str(item.get("job_id") or item.get("completion_job_id") or "")
                if item.get("state") == "complete" and checkpoint_count == 0 and job_id:
                    if job_id not in job_status_cache:
                        try:
                            job_status_cache[job_id] = str(self.store.read(job_id).get("status") or "unknown")
                        except (KeyError, ValueError, OSError, TypeError):
                            job_status_cache[job_id] = None
                            result["complete"] = False
                    terminal_legacy_job = job_status_cache[job_id] in {
                        "completed", "degraded", "partial", "failed", "interrupted",
                    }
                if terminal_legacy_job:
                    continue

                if not isinstance(counts, dict):
                    received = item.get("received_segments")
                    if isinstance(received, int) and not isinstance(received, bool):
                        result["asr_counts"]["pending"] += max(0, received)
                    continue
                for status in result["asr_counts"]:
                    value = counts.get(status, 0)
                    if isinstance(value, int) and not isinstance(value, bool):
                        result["asr_counts"][status] += max(0, value)
                    else:
                        result["complete"] = False

            self._watch_status_summary_cache = result
            self._watch_status_summary_cached_at = time.monotonic()
            return result

    @staticmethod
    def _warm_vip_transcriber() -> None:
        """后台预热 SenseVoice 常驻进程；失败只记日志，不影响服务可用性。"""
        try:
            from life_logger.vip_fast_track import warm_vip_transcriber

            started = warm_vip_transcriber()
            print(
                f"[life-logger] VIP 转写预热 {'完成' if started else '失败（首次调用会退化为冷启动）'}",
                flush=True,
            )
        except Exception as error:
            print(f"[life-logger] VIP 转写预热异常: {type(error).__name__} {error}", flush=True)

    def _warm_recent_days_read_cache(self) -> None:
        """启动后后台单线程顺序预热近 7 天切日读缓存，消除重启后首次点击「今天/前一天」的冷启动等待。"""
        try:
            import time
            from datetime import datetime, timedelta
            from life_logger.activity_fragments import project_for_read
            from life_logger.daily_dashboard import DailyDashboardBuilder
            from life_logger.timeutil import LOCAL_TZ
            from life_logger.web_startup import _watch_last_segment_received

            if self._watch_live_stop.wait(0.2):
                return
            today_dt = datetime.now(tz=LOCAL_TZ).date()
            today_str = today_dt.isoformat()
            _watch_last_segment_received(self.watch_session_store.root, today_str)
            self.watch_session_store.list_coverage_intervals()
            for offset in range(7):
                if self._watch_live_stop.is_set():
                    return
                # 让路给前台浏览器首屏请求，不与活跃读请求争抢 GIL 与读槽位
                for _ in range(20):
                    if getattr(self, "_read_producers_active", 0) == 0:
                        break
                    if self._watch_live_stop.wait(0.1):
                        return
                day = (today_dt - timedelta(days=offset)).isoformat()
                try:
                    raw = self.activity_timeline.read_day(day)
                    project_for_read(raw, date=day, root=self.store.root)
                except Exception:
                    pass
                try:
                    builder = DailyDashboardBuilder(
                        self.store.root,
                        dictation_filter=self.dashboard_dictation_filter(),
                        use_scan_cache=True,
                    )
                    self.cached_read(
                        f"dashboard|{day}",
                        lambda b=builder, d=day: b.build_for_date(d),
                    )
                except Exception:
                    pass
                try:
                    brief = self.cached_read(
                        f"daily|{day}|provisional|display|True",
                        lambda d=day: self.build_daily_brief(
                            date=d, allow_empty=True, scope="provisional", view="display"
                        ),
                    )
                    events_list = list(brief.get("events", []))
                    _b, events_list = self.apply_dashboard_dictation_filter(brief, events_list)
                    from life_logger.broadcast_decisions import (
                        apply_decisions as _apply_broadcast_decisions,
                        load_decisions as _load_broadcast_decisions,
                    )
                    from life_logger.daily_cognitive_session import (
                        build_pinned_memos,
                        build_work_cognition_ledger,
                    )
                    from life_logger.day_facts import day_expense_ledger, domain_ledgers

                    def _warm_ledgers(ev=events_list, target=day):
                        _domains = domain_ledgers(ev, target, root=self.store.root)
                        expense = day_expense_ledger(ev, target, root=self.store.root)
                        expense = _apply_broadcast_decisions(
                            expense, _load_broadcast_decisions(self.store.root, target)
                        )
                        return {
                            "nutrition": _domains["nutrition"],
                            "fitness": _domains["fitness"],
                            "smoking": _domains["smoking"],
                            "expense": expense,
                            "work": build_work_cognition_ledger(ev),
                            "memos": build_pinned_memos(ev),
                        }

                    self.cached_read(
                        f"daily-ledgers|{day}|provisional|display|True",
                        _warm_ledgers,
                    )
                except Exception:
                    pass
                time.sleep(0.05)
        except Exception as error:
            logger.debug("recent days read cache warmup skipped: %s", error)

    def _activity_timeline_loop(self) -> None:
        while not self._activity_timeline_stop.wait(getattr(self, "_activity_timeline_wait", 15.0)):
            try:
                self._activity_timeline_last_tick = self.activity_timeline.tick()
            except Exception:
                logger.exception("Ten-minute activity processing failed")

    def _watch_live_loop(self) -> None:
        from life_logger.local_web import (
            _is_retryable_asr_slot_error,
            _now,
            datetime,
        )

        idle_wait = 3.0
        while not self._watch_live_stop.wait(idle_wait):
            if not self.config_path.is_file():
                continue
            try:
                # FunASR can take minutes per segment. Keep it off this coordinator thread so
                # completion intents and already-finished sessions continue to advance.
                has_active = self._schedule_watch_formal_asr_scan()
                self.watch_session_store.finalize_completion_intents()
                if hasattr(self.watch_session_store, "auto_finalize_idle_sessions"):
                    self.watch_session_store.auto_finalize_idle_sessions(idle_threshold_seconds=1800.0, apply=True)
                self.watch_session_store.abandon_empty_sessions(apply=True)
                for summary in self.watch_session_store.list_session_summaries(limit=None):
                    state = summary.get("state")
                    if state == "receiving":
                        has_active = True
                    elif state == "complete" and not summary.get("job_id"):
                        device, session = summary["device_id"], summary["session_id"]
                        manifest = self.watch_session_store.load_session(device, session)
                        completion_job_id = str(manifest.get("completion_job_id") or "")
                        if not completion_job_id:
                            completion_job_id = self.store.create_watch_pending_job(
                                device_id=device,
                                session_id=session,
                                start_at=str(manifest.get("started_at") or "") or None,
                                timezone_name=str(manifest.get("timezone") or "Asia/Shanghai"),
                            )
                            self.watch_session_store.mark_completion_job(
                                device,
                                session,
                                completion_job_id,
                            )
                            manifest = self.watch_session_store.load_session(device, session)
                        if not self.watch_formal_asr.ready_for_session(device, session):
                            has_active = True
                            continue
                        directory = self.watch_session_store.session_directory(device, session)
                        self.watch_session_store.assemble_recording(device, session)
                        manifest = self.watch_session_store.load_session(device, session)
                        imported = self.store.import_watch_recording(
                            directory,
                            {**manifest, "complete": True},
                            self.config_path,
                            formal_asr_processor=self.watch_formal_asr,
                            reserved_job_id=completion_job_id,
                        )
                        self.watch_session_store.mark_imported(device, session, imported["job_id"])
                        # [2026-10-24 P0] 导入完成会写 job 产物（events.json/daily_summary）
                        self.invalidate_event_reads("watch_session_imported")
                        has_active = True
                    elif state == "complete" and summary.get("job_id"):
                        job_id = str(summary["job_id"])
                        try:
                            job = self.store.read(job_id)
                        except (KeyError, ValueError, OSError, json.JSONDecodeError):
                            continue
                        if self.store.run_due_context_auto_retry(
                            job_id,
                            self.config_path,
                            secrets_path=self.store.root / "secrets.json",
                        ):
                            has_active = True
                        context_retry = job.get("context_auto_retry")
                        context_recovery = job.get("context_recovery")
                        if (
                            isinstance(context_retry, dict)
                            and context_retry.get("status") in {"scheduled", "running"}
                        ) or (
                            isinstance(context_recovery, dict)
                            and context_recovery.get("origin") == "automatic"
                            and context_recovery.get("status") in {"queued", "running"}
                        ):
                            has_active = True
                        if job.get("status") != "failed" or not _is_retryable_asr_slot_error(
                            job.get("error")
                        ):
                            continue
                        retry = job.get("automatic_retry")
                        if not isinstance(retry, dict):
                            # Adopt a known slot failure created by an older service build.
                            # Only complete Watch sessions linked to this exact retryable
                            # error are eligible; unrelated failed Jobs remain untouched.
                            self.store.update_from_worker(
                                job_id,
                                {
                                    "type": "job_finished",
                                    "status": "failed",
                                    "error": job.get("error"),
                                },
                            )
                            continue
                        # Retry metadata is only actionable while a complete Watch manifest
                        # still links this job; unrelated/manual jobs are never auto-started.
                        due_at = retry.get("next_attempt_at")
                        try:
                            due = datetime.fromisoformat(str(due_at)).timestamp()
                        except (TypeError, ValueError, OverflowError):
                            self.store.update_from_worker(
                                job_id,
                                {
                                    "type": "job_finished",
                                    "status": "failed",
                                    "error": job.get("error"),
                                },
                            )
                            continue
                        if due <= _now():
                            self.store.start_job(
                                job_id,
                                self.config_path,
                                secrets_path=self.store.root / "secrets.json",
                            )
                            has_active = True
                if has_active:
                    idle_wait = 10.0
                else:
                    idle_wait = 30.0
            except Exception:
                import traceback
                traceback.print_exc()
                idle_wait = 30.0

    def _schedule_watch_formal_asr_scan(self) -> bool:
        with self._watch_formal_scan_lock:
            current = self._watch_formal_scan_thread
            if (
                isinstance(current, threading.Thread)
                and current.name == "watch-formal-asr-segment"
                and current.is_alive()
                and current is not threading.current_thread()
            ):
                return True
            thread = threading.Thread(
                target=self._run_watch_formal_asr_scan,
                name="watch-formal-asr-segment",
                daemon=True,
            )
            self._watch_formal_scan_thread = thread
            thread.start()
            return False

    def _run_watch_formal_asr_scan(self) -> None:
        try:
            import time

            stop = getattr(self, "_watch_live_stop", None)
            scanned = 0
            while stop is None or not stop.is_set():
                if not self.watch_formal_asr.scan_once():
                    break
                scanned += 1
                if stop is None:
                    break
            if scanned > 0:
                now = time.monotonic()
                last_inv = float(getattr(self, "_last_formal_asr_invalidate_ts", 0.0) or 0.0)
                # [2026-11-08 防抖] 真正产生 quick_captures 或 events.json 时 watch_formal_asr 内部已立即 bump；
                # 后台循环里单段 60s 原始转写完成最多每 60 秒触发一次跨进程读缓存刷新，防止每几秒清空一次 _read_cache。
                if stop is None or (now - last_inv) >= 60.0:
                    self._last_formal_asr_invalidate_ts = now
                    self.invalidate_event_reads("watch_formal_asr_segment")
        except Exception:
            import traceback
            traceback.print_exc()

    def recover_interrupted_jobs(self) -> list[str]:
        return self._start_recovery_thread(self.store.interrupted_job_ids())

    def run_auto_ingest_scan(self) -> dict[str, Any]:
        return self.store.scan_auto_ingest(
            self.config_path,
            secrets_path=self.store.root / "secrets.json",
        )

    def _fast_version_info(self) -> dict[str, Any]:
        """Version metadata without running ``git`` in the request path."""
        from life_logger.version import (
            APP_VERSION, RELEASE_NOTE, code_fingerprint, deployed_at, unloaded_files,
        )

        git_dir = PROJECT_ROOT / ".git"
        commit = ""
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: "):
                ref = head[5:].strip()
                ref_path = git_dir / ref
                if ref_path.is_file():
                    commit = ref_path.read_text(encoding="utf-8").strip()[:12]
            elif head:
                commit = head[:12]
            if not commit:
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        if line and not line.startswith("#") and line.endswith(" refs/heads/main"):
                            commit = line.split()[0][:12]
                            break
        except OSError:
            commit = ""
        stale = unloaded_files(self.started_at)
        return {
            "app_version": APP_VERSION,
            "release_note": RELEASE_NOTE,
            "code_fingerprint": code_fingerprint(),
            "git_commit": commit,
            "deployed_at": deployed_at(self.started_at),
            "pid": os.getpid(),
            "unloaded_files": stale,
            "deploy_state": "stale" if stale else "current",
        }

    def _broadcast_expense_state(self, date: str):
        """Return (base ledger, decisions payload, ledger with decisions applied)."""
        from life_logger.broadcast_decisions import (
            apply_decisions,
            load_decisions,
            validate_date,
        )
        from life_logger.day_facts import event_only_expense_ledger

        date = validate_date(date)
        brief = self.build_daily_brief(date=date, allow_empty=True)
        events = list(brief.get("events", []))
        base = event_only_expense_ledger(events)
        payload = load_decisions(self.store.root, date)
        return base, payload, apply_decisions(base, payload)

    def _openclaw_reminder_tasks(self) -> list[dict[str, Any]]:
        """[2026-10-13 root 拍板] 从 OpenClaw 取真待办，投影成 reminders 格式。

        为什么要换：`/api/reminders` 原先只吐 Life Logger 自动抽取的语音碎片
        （半句话、重复 200+ 条、全 dismissed），App 的严格模式把它们全拦下 →
        徽章长期空白。而 OpenClaw 里的 todo 是**人管的、能改、能归档**的真待办，
        这才是徽章该显示的东西。

        review_status 固定给 `confirmed`、source_type 给 `manual`：OpenClaw 任务
        天然满足 App 的严格模式判据（`review_status=="confirmed" || source_type=="manual"`），
        不需要人再去网页点一次确认。

        取不到（OpenClaw 挂了/超时）就返回空列表，由调用方退回本地兜底逻辑 —— 宁可
        显示旧数据，也不能让徽章因为上游抖动而整片空白。
        """
        try:
            from life_logger.openclaw_bridge import OpenClawBridgeClient

            client = OpenClawBridgeClient()
            if not client.is_healthy():
                return []
            raw = client.list_tasks()
        except Exception:
            return []

        from datetime import datetime as _dt

        rows: list[dict[str, Any]] = []
        for task in raw or []:
            status = str(task.get("status") or "").strip()
            if status not in ("pending", "in_progress", "blocked"):
                continue
            title = str(task.get("title") or "").strip()
            if not title:
                continue
            rows.append(
                {
                    "task_key": str(task.get("id") or f"oc::{title[:40]}"),
                    "title": title,
                    "status": "in_progress" if status == "in_progress" else "pending",
                    # 下游（App 严格模式）据此认定「人确认过的」
                    "review_status": "confirmed",
                    "source_type": "manual",
                    "event_id": None,
                    "job_id": None,
                    "artifact_path": None,
                    "openclaw_id": task.get("id"),
                    "openclaw_status": status,
                    "priority": task.get("priority"),
                    "due_time": task.get("due_time") or task.get("due_at"),
                    "tags": task.get("tags") or [],
                    "remind_at": task.get("due_time") or task.get("due_at"),
                    "created_at": task.get("created_at") or _dt.now(tz=LOCAL_TZ).isoformat(),
                }
            )

        # 排序：P1 优先，其次按截止时间，最后按创建时间（新的在前）。
        prio_rank = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
        rows.sort(
            key=lambda r: (
                prio_rank.get(str(r.get("priority") or "").upper(), 9),
                str(r.get("due_time") or "9999"),
                str(r.get("created_at") or ""),
            )
        )
        return rows

    def _reminder_tasks(self) -> list[dict[str, Any]]:
        tasks, _ = self._reminder_tasks_with_source()
        return tasks

    def _reminder_tasks_with_source(
        self,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """返回 (tasks, authoritative_source)。

        authoritative_source 只在「这一批就是某个上游的完整清单」时才非 None：
        当前只有 OpenClaw（它是有删除/归档能力的真事实源）。本地兜底批次返回
        None —— 本地只是投影，不能拿它去裁掉别处的记录。
        """
        # [2026-10-13 root 拍板] 数据源换成 OpenClaw：它那 300+ 条真待办才是徽章该显示的。
        # 本地语音碎片降级为兜底（OpenClaw 不可达时才用），避免徽章因上游抖动而空白。
        openclaw_rows = self._openclaw_reminder_tasks()
        if openclaw_rows:
            return openclaw_rows, "openclaw"
        state_rows = self.store.read_task_states()
        states_by_key = {
            str(item.get("task_key") or ""): item
            for item in state_rows
            if item.get("task_key")
        }
        states_by_event = {
            str(item.get("event_id") or ""): item
            for item in state_rows
            if item.get("event_id")
        }
        tasks: dict[str, dict[str, Any]] = {}
        # event_id → (review_status, source_type)：task-states 里的条目只带状态，
        # 需要按 event_id 回填这两个字段，否则它们会缺字段而被下游当成未确认。
        from life_logger.extract import _is_actionable_todo

        event_meta: dict[str, tuple[Any, Any]] = {}
        for item in self.store.search(
            "",
            result_type="event",
            category="todo",
            limit=100,
        ):
            event_id = str(item.get("event_id") or "").strip()
            task_key = event_id or "::".join(
                (
                    str(item.get("job_id") or ""),
                    str(item.get("artifact_path") or ""),
                    str(item.get("text") or ""),
                )
            )
            state = states_by_key.get(task_key) or states_by_event.get(event_id) or {}
            title = str(state.get("title") or item.get("text") or "未命名待办")
            if item.get("review_status") != "confirmed" and not _is_actionable_todo(title):
                continue
            if event_id:
                event_meta[event_id] = (
                    item.get("review_status"),
                    item.get("source_type"),
                )
            tasks[task_key] = {
                "task_key": task_key,
                "title": title,
                "status": str(state.get("status") or "pending"),
                "event_id": event_id or None,
                "job_id": item.get("job_id"),
                "artifact_path": item.get("artifact_path"),
                # 复核状态与来源：下游（含 ESP32 徽章）据此只显示「人确认过的」待办。
                # 取事件记录里的真值，取不到就是 None —— 不猜、不默认成已确认。
                "review_status": item.get("review_status"),
                "source_type": item.get("source_type"),
            }
        for item in state_rows:
            task_key = str(item.get("task_key") or "").strip()
            if task_key and task_key not in tasks:
                entry = dict(item)
                review_status, source_type = event_meta.get(
                    str(item.get("event_id") or ""), (None, None)
                )
                entry.setdefault("review_status", review_status)
                entry.setdefault("source_type", source_type)
                tasks[task_key] = entry
        return list(tasks.values()), None

    def _reminder_event_lookup(
        self, item: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        """在产物里找出待办条目对应的原始事件；找不到（或没有 event_id）返回 None。"""
        event_id = str(item.get("event_id") or "")
        if not event_id:
            return None
        artifact = str(item.get("artifact_path") or "").strip()
        job_id = str(item.get("job_id") or "").strip()
        candidates: list[Path] = []
        if artifact:
            relative = Path(*PurePosixPath(artifact).parts)
            candidates.append(self.store.root / relative)
            if job_id:
                candidates.append(self.store.root / "jobs" / job_id / relative)
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            events = payload if isinstance(payload, list) else payload.get("events")
            if not isinstance(events, list):
                continue
            for event in events:
                if isinstance(event, Mapping) and str(
                    event.get("event_id") or ""
                ) == event_id:
                    return event
        return None

    def sync_local_reminders(self) -> list[dict[str, Any]]:
        self.reminders.clean_invalid_reminders()
        tasks, source = self._reminder_tasks_with_source()
        # 上游消失的条目要能过期/清掉（见 LocalReminderStore.sync_tasks）。
        # source 只在 OpenClaw 给出完整清单时非 None，本地兜底批次不裁剪。
        return self.reminders.sync_tasks(tasks, prune_missing_source=source)

    def run_reminder_dispatch(self) -> list[dict[str, Any]]:
        self.sync_local_reminders()
        return self.reminders.dispatch_due()

    def _reminder_loop(self) -> None:
        while not self._reminder_stop.is_set():
            try:
                self.run_reminder_dispatch()
            except Exception:
                pass
            self._reminder_stop.wait(30)

    def _auto_ingest_loop(self) -> None:
        while not self._auto_ingest_stop.is_set():
            try:
                status = self.run_auto_ingest_scan()
                interval = max(5, int(status.get("scan_interval_seconds") or 15))
            except Exception:
                interval = 15
            self._auto_ingest_stop.wait(interval)
