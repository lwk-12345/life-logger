"""LocalWebServer 的方法，从 local_web.py 机械搬出，方法体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

from life_logger.daily_brief import build_daily_brief, deliver_daily_brief
from life_logger.daily_brief import AGENT_MIN_CONFIDENCE
from life_logger.daily_brief import default_outbox_dir
from life_logger.daily_brief import external_agent_brief
from life_logger.daily_brief import outbound_options
from life_logger.dictation import DashboardDictationFilter
from life_logger.dictation import build_dashboard_dictation_filter
from life_logger.web_common import _read_contract
from typing import Any
from typing import Mapping
from typing import Sequence
from zoneinfo import ZoneInfo
import copy
import json
import threading
import time


class _ServerReadsMixin:
    def processing_health(self) -> dict[str, Any]:
        queue = self.store.dispatch_queue
        return queue.health() if queue is not None else {"online": None}

    def _start_recovery_thread(self, job_ids: Sequence[str]) -> list[str]:
        scheduled = [str(job_id) for job_id in job_ids]
        if not scheduled:
            return []
        if self._recovery_thread and self._recovery_thread.is_alive():
            return []
        self._recovery_thread = threading.Thread(
            target=self.store.resume_interrupted_jobs,
            kwargs={
                "config_path": self.config_path,
                "secrets_path": self.store.root / "secrets.json",
                "job_ids": scheduled,
            },
            name="life-logger-job-recovery",
            daemon=True,
        )
        self._recovery_thread.start()
        return scheduled

    def dashboard_dictation_filter(self) -> DashboardDictationFilter:
        """[2026-10-13 root 拍板] 生活看板隐藏「电脑前说话」(dictation) 内容。

        数据一条不删：产物（events.json / context-extraction.json / 转写 / 录音）
        原样保留，只在 API 读层过滤。开关在 ``local-config.json``：

          * ``hide_dictation_from_dashboard``：默认 true；写 false 就恢复原样。
          * ``dictation_windows_extra``：手工补的 dictation 时间窗（形如
            ``[{"start": "...", "end": "..."}]``），用于 PTT 没记到的「电脑前说话」，
            同样不改任何产物文件，删掉即恢复。

        配置每次请求现读，所以改配置不需要重启服务；改代码才需要重启。
        """
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return build_dashboard_dictation_filter(payload, root=self.store.root)

    def apply_dashboard_dictation_filter(
        self,
        brief: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
        """把 dictation 内容从一份「一天简报」的读层摘掉。

        返回 (过滤后的 brief, 过滤后的事件表)。brief 里由事件派生的数字
        （event_count / category_summary / todos）按过滤后的结果一起重算，
        免得看板上出现「今日事件 12 条、列表只剩 6 条」这种对不上的情况。
        产物文件不动：摘要与产物始终对应同一份原始数据。
        """
        from life_logger.local_web import (
            _reapply_activity_sidecars,
        )

        dictation_filter = self.dashboard_dictation_filter()
        kept = list(dictation_filter.filter_events(events))
        if len(kept) == len(events):
            return dict(brief), kept
        from life_logger.daily_brief import _category_summary

        hidden_ids = dictation_filter.hidden_event_ids(events)
        updated = dict(brief)
        updated["events"] = kept
        updated["event_count"] = len(kept)
        updated["category_summary"] = _category_summary(kept)
        # [2026-11-04 P02] 派生产物也要摘：cards/links/presentation/purchase_sessions
        # 是 daily_brief 用未过滤事件提前建好的，只摘 events 等于"只在顶层藏起来"。
        _reapply_activity_sidecars(updated, kept, hidden_ids)
        if isinstance(updated.get("todos"), list):
            updated["todos"] = [
                todo
                for todo in updated["todos"]
                if not (
                    isinstance(todo, Mapping)
                    and str(todo.get("event_id") or "") in hidden_ids
                )
            ]
        return updated, kept

    def _register_read_flight(self, key: str, flight: threading.Event) -> None:
        self._read_inflight_meta[key] = {
            "started_at": time.monotonic(),
            "waiters": 0,
            "flight": flight,
            "background": False,
        }

    def _stale_snapshot_locked(self, key: str):
        hit = self._read_cache.get(key)
        if hit is not None:
            return hit
        sep = key.find("|")
        if sep < 0 or not key.startswith("e"):
            return None
        suffix = key[sep:]
        best = None
        for cached_key, entry in self._read_cache.items():
            if cached_key.endswith(suffix) and cached_key.startswith("e"):
                if best is None or entry[0] > best[0]:
                    best = entry
        return best

    def _store_read_cache_locked(self, key: str, value: Any) -> None:
        sep = key.find("|")
        if sep >= 0 and key.startswith("e"):
            suffix = key[sep:]
            for old_key in [
                k for k in self._read_cache if k != key and k.endswith(suffix) and k.startswith("e")
            ]:
                self._read_cache.pop(old_key, None)
        self._read_cache[key] = (time.monotonic(), value)
        while len(self._read_cache) > self._read_cache_max_entries:
            oldest = min(self._read_cache, key=lambda item: self._read_cache[item][0])
            self._read_cache.pop(oldest, None)

    def _adjust_inflight_waiter(self, key: str, delta: int) -> None:
        with self._read_cache_lock:
            meta = self._read_inflight_meta.get(key)
            if meta is not None:
                meta["waiters"] = max(0, int(meta.get("waiters") or 0) + delta)

    def _produce_with_read_limit(self, key: str, producer):
        """Run producer in a worker with a hard deadline; reclaim its slot on timeout."""
        from life_logger.local_web import (
            ReadProducerBusy,
            _mark_stale_read,
        )

        wait_start = time.monotonic()
        with self._read_cache_lock:
            self._read_slot_waiting += 1
        acquired = self._read_producer_slots.acquire(timeout=self._read_producer_timeout)
        slot_wait = time.monotonic() - wait_start
        with self._read_cache_lock:
            self._read_slot_waiting = max(0, self._read_slot_waiting - 1)
        if not acquired:
            with self._read_cache_lock:
                hit = self._stale_snapshot_locked(key)
            stale = copy.deepcopy(hit[1]) if hit is not None else None
            _mark_stale_read(stale, "read producer busy; returned previous snapshot")
            raise ReadProducerBusy(key, self._read_producer_timeout, stale=stale)

        with self._read_cache_lock:
            self._read_producers_active += 1
            meta = self._read_inflight_meta.get(key)
            if meta is not None:
                meta["producer_started_at"] = time.monotonic()
                meta["slot_wait_seconds"] = round(slot_wait, 3)
        lease = {"finished": False, "expired": False, "slot_released": False, "result": None, "error": None}
        done = threading.Event()
        producer_start = time.monotonic()

        def worker() -> None:
            try:
                result = producer()
                lease["result"] = result
                with self._read_cache_lock:
                    # 关键：请求超时后 producer 仍继续跑；跑完必须落缓存，
                    # 下一次同 key 才能直接热命中，而不是每次重新算。
                    if result is not None:
                        self._store_read_cache_locked(key, result)
            except BaseException as exc:  # surfaced in the caller, never swallowed
                lease["error"] = exc
            finally:
                with self._read_cache_lock:
                    lease["finished"] = True
                    if not lease["expired"]:
                        self._read_producers_active = max(0, self._read_producers_active - 1)
                        self._read_producer_slots.release()
                        lease["slot_released"] = True
                    else:
                        self._read_producers_background = max(0, self._read_producers_background - 1)
                        if not lease["slot_released"]:
                            self._read_producer_slots.release()
                            lease["slot_released"] = True
                        # 超时请求已经走了；worker 跑完负责唤醒同 key 的跟随者。
                        meta = self._read_inflight_meta.get(key)
                        if meta is not None:
                            late_flight = meta.get("flight")
                            self._read_inflight_meta.pop(key, None)
                            if self._read_inflight.get(key) is late_flight:
                                self._read_inflight.pop(key, None)
                            if isinstance(late_flight, threading.Event):
                                late_flight.set()
                done.set()

        runner = threading.Thread(
            target=worker, name=f"read-producer-{key[:80]}", daemon=True
        )
        runner.start()
        finished = done.wait(timeout=self._read_producer_run_timeout)
        producer_elapsed = time.monotonic() - producer_start
        with self._read_cache_lock:
            finished = bool(lease["finished"])
            if finished:
                result = lease["result"]
                error = lease["error"]
            else:
                lease["expired"] = True
                meta = self._read_inflight_meta.get(key)
                if meta is not None:
                    meta["background"] = True
                self._read_producers_active = max(0, self._read_producers_active - 1)
                self._read_producers_background += 1
                # 后台配额保护：未超配则提前释放槽位；超配则扣留槽位直至线程退出，防死锁和线程膨胀
                if self._read_producers_background <= self._read_producer_background_limit:
                    self._read_producer_slots.release()
                    lease["slot_released"] = True
                else:
                    lease["slot_released"] = False
                hit = self._stale_snapshot_locked(key)
                stale = copy.deepcopy(hit[1]) if hit is not None else None
                _mark_stale_read(stale, "read producer exceeded deadline; returned previous snapshot")
                result = stale
                error = None
        if producer_elapsed > 0.5 or slot_wait > 0.5 or not finished:
            print(
                "[timing] read-producer "
                f"key={key} slot_wait={slot_wait:.3f}s "
                f"producer={producer_elapsed:.3f}s timeout={not finished}",
                flush=True,
            )
        if finished:
            if error is not None:
                raise error
            return result
        raise ReadProducerBusy(
            key, self._read_producer_run_timeout, stale=result, background=True
        )

    def _follow_read_flight(self, key: str, flight: threading.Event, producer, ttl: float):
        """Wait for an existing flight without holding the cache lock."""
        from life_logger.local_web import (
            ReadProducerBusy,
            _mark_stale_read,
        )

        flight.wait(timeout=30.0)
        with self._read_cache_lock:
            hit = self._read_cache.get(key)
        if hit is not None:
            value = copy.deepcopy(hit[1])
            if time.monotonic() - hit[0] > ttl:
                _mark_stale_read(value, "read producer busy; returned previous snapshot")
            return value
        with self._read_cache_lock:
            current = self._read_inflight.get(key)
            if current is None:
                flight = threading.Event()
                self._read_inflight[key] = flight
                self._register_read_flight(key, flight)
                owner = True
            else:
                flight = current
                owner = False
        if not owner:
            flight.wait(timeout=10.0)
            with self._read_cache_lock:
                hit = self._read_cache.get(key)
            if hit is not None:
                value = copy.deepcopy(hit[1])
                if time.monotonic() - hit[0] > ttl:
                    _mark_stale_read(value, "read producer busy; returned previous snapshot")
                return value
            # A different flight still owns this key.  A timeout or failed
            # producer without a snapshot must not start an untracked duplicate.
            raise ReadProducerBusy(key, self._read_producer_run_timeout, background=True)
        background_busy = False
        try:
            value = self._produce_with_read_limit(key, producer)
        except ReadProducerBusy as busy:
            background_busy = bool(busy.background)
            if busy.stale is not None:
                return busy.stale
            raise
        finally:
            if not background_busy:
                with self._read_cache_lock:
                    if self._read_inflight.get(key) is flight:
                        self._read_inflight.pop(key, None)
                        self._read_inflight_meta.pop(key, None)
                    flight.set()
        with self._read_cache_lock:
            self._store_read_cache_locked(key, value)
        return value

    def control_snapshot_meta(
        self, key: str, producer, ttl: float = 10.0, wait_timeout: float = 1.0
    ):
        """Return ``(value, cached)`` with a per-key single-flight cold refresh."""
        from life_logger.local_web import (
            ControlSnapshotBusy,
        )

        now = time.monotonic()
        with self._control_cache_lock:
            hit = self._control_cache.get(key)
            if hit is not None and now - hit[0] <= ttl:
                return copy.deepcopy(hit[1]), True
            flight = self._control_inflight.get(key)
            if flight is None:
                flight = threading.Event()
                self._control_inflight[key] = flight
                owner = True
            else:
                owner = False
        if not owner:
            flight.wait(timeout=wait_timeout)
            with self._control_cache_lock:
                hit = self._control_cache.get(key)
            if hit is not None:
                return copy.deepcopy(hit[1]), True
            raise ControlSnapshotBusy(key)
        try:
            value = producer()
        except BaseException:
            with self._control_cache_lock:
                if self._control_inflight.get(key) is flight:
                    self._control_inflight.pop(key, None)
                flight.set()
            raise
        with self._control_cache_lock:
            self._control_cache[key] = (time.monotonic(), value)
            if len(self._control_cache) > 16:
                oldest = min(self._control_cache, key=lambda k: self._control_cache[k][0])
                self._control_cache.pop(oldest, None)
            if self._control_inflight.get(key) is flight:
                self._control_inflight.pop(key, None)
            flight.set()
        return value, False

    def control_snapshot(self, key: str, producer, ttl: float = 10.0):
        """Compatibility wrapper for callers that only need the value."""
        value, _cached = self.control_snapshot_meta(key, producer, ttl=ttl)
        return value

    def _fast_watch_reception_payload(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        """Fast degraded snapshot for large Watch histories.

        Per-session ``summary_for_session`` reads every segment and is the
        30s control-endpoint stall.  Keep the list/device facts, explicitly
        mark the ASR detail as degraded, and let the normal detail route own
        the expensive refresh.
        """
        for item in sessions:
            if isinstance(item, dict):
                item.pop("preview", None)
        lifecycle: dict[str, int] = {}
        for item in sessions:
            state = str(item.get("state") or item.get("status") or "other")
            lifecycle[state] = lifecycle.get(state, 0) + 1
        from life_logger.watch_stream import DEVICE_STATUS_FRESHNESS_SECONDS
        return {
            "sessions": sessions,
            "device_statuses": self.watch_session_store.list_device_statuses(),
            # 新鲜度口径只在 watch_stream 里定义一次，避免两端各有一套。
            "device_status_freshness_seconds": DEVICE_STATUS_FRESHNESS_SECONDS,
            "status_summary": {
                "complete": False,
                "session_count": len(sessions),
                "lifecycle_counts": lifecycle,
                "asr_counts": {},
                "degraded": True,
                "degraded_reason": "formal_asr detail deferred for large history",
            },
            "degraded": True,
            "degraded_reason": "formal_asr detail deferred for large history",
        }

    def _sync_external_write_epoch(self) -> None:
        """跨进程写（worker / Watch ASR / CLI）bump 后，作废本进程的结果快照。

        为什么单独一个方法：`cached_read()` 命中 8s 快照时**根本不进**
        `build_event_view`，所以 event_view 的跨进程代际检测会被绕过。
        这里在取快照前读一次 root 的 epoch token（实测 0.035ms），变了就
        把本进程 `_READ_CACHE_EPOCH` +1 并清空 `_read_cache` —— 下一次按新
        key 必然 miss，直接重建。

        注意：**不写** token（读进程不该产生写），也不调用
        `invalidate_read_caches()`（那会再写一次 token 让其它进程接着失效）。
        """
        from life_logger.local_web import (
            bump_read_cache_epoch,
        )

        try:
            from life_logger.event_view import sync_scan_epoch

            if not sync_scan_epoch(self.store.root):
                return
        except Exception:  # 代际检测失败不能拖垮读接口
            return
        bump_read_cache_epoch(self.store.root)

    def cached_read(self, key: str, producer, ttl: float = 300.0):
        # [2026-11-08] 8s → 45s：写失效靠 epoch（同进程写 + 跨进程 token），TTL 只是兜底。
        # 8 秒时，任何开着的页面每次轮询都会把 /api/daily、/api/dashboard/daily 整份重算，
        # 线上持续占用 50%+ CPU，用户切日期的请求就排在它们后面（实测 4–6 秒）。
        from life_logger.local_web import (
            ReadProducerBusy,
            _mark_stale_read,
            read_cache_epoch,
        )

        # 跨进程写先对齐代际；变了就把结果快照整体作废（不靠 8s TTL）
        self._sync_external_write_epoch()
        # 写失效会把 epoch +1 → 旧 key 自然不可命中（不需要逐实例遍历清理）
        key = f"e{read_cache_epoch(self.store.root)}|{key}"
        """只读快照缓存（P0，2026-10-22）。

        谱系：/api/daily、/api/dashboard/daily、/api/stats 每次都要把 569 个
        events.json + 上百个 status.json 重新读一遍（进程内实测 0.5~5s），
        前端一次瀑布图会并发打 3 个接口 → 重复计算 + 互相排队。
        这里按 (接口, date, scope, view) 缓存**结果快照** 8 秒；
        写路径不感知，最坏只多 8 秒陈旧（读接口本来就非强一致）。
        """
        now = time.monotonic()
        with self._read_cache_lock:
            hit = self._read_cache.get(key)
            # [2026-10-23 P0] 深拷贝必须在锁外做。
            # 原来 `return copy.deepcopy(hit[1])` 在 `_read_cache_lock` 内：
            # 一次 `/api/dashboard/daily` 命中缓存时，可能深拷贝几十 MB 的看板对象，
            # 期间所有 `/api/daily` 读请求都堵在同一把锁上（实测 90s 超时）；
            # 拷贝本身还持续分配对象，反过来喂大 GC/CPU。
            # 这里只把“取快照引用”放在锁内，拷贝放到锁外；即使拷贝慢，也不再串行阻塞其它读接口。
            if hit is not None and now - hit[0] <= ttl:
                fresh = hit[1]
                owner = False
            else:
                fresh = None
                flight = self._read_inflight.get(key)
                if flight is None:
                    flight = threading.Event()
                    self._read_inflight[key] = flight
                    self._register_read_flight(key, flight)
                    owner = True
                else:
                    owner = False
        if fresh is not None:
            return copy.deepcopy(fresh)
        if not owner:
            # 已超时但 producer 仍在后台跑：不要再启动第二个，也不要让新请求
            # 等 30s；直接 202，后台跑完会写缓存并唤醒等待者。
            with self._read_cache_lock:
                late_meta = self._read_inflight_meta.get(key) or {}
            if late_meta.get("background"):
                with self._read_cache_lock:
                    hit = self._stale_snapshot_locked(key)
                if hit is not None:
                    return _mark_stale_read(
                        copy.deepcopy(hit[1]), "read producer busy; returned previous snapshot"
                    )
                raise ReadProducerBusy(
                    key,
                    self._read_producer_run_timeout,
                    stale=None,
                    background=True,
                )
            # 跟随者只等有限时间，避免 producer 卡死时把读线程永久拖住。
            self._adjust_inflight_waiter(key, 1)
            try:
                return self._follow_read_flight(key, flight, producer, ttl)
            finally:
                self._adjust_inflight_waiter(key, -1)
        background_busy = False
        try:
            try:
                value = self._produce_with_read_limit(key, producer)
            except ReadProducerBusy as busy:
                background_busy = bool(busy.background)
                if busy.stale is not None:
                    return busy.stale
                raise
        finally:
            if not background_busy:
                with self._read_cache_lock:
                    if self._read_inflight.get(key) is flight:
                        self._read_inflight.pop(key, None)
                        self._read_inflight_meta.pop(key, None)
                    flight.set()
        with self._read_cache_lock:
            self._store_read_cache_locked(key, value)
        # 冷路径返回**原对象**（与缓存前行为一致，调用方原有的原地改写保持不变）；
        # 命中路径返回深拷贝，避免调用方改坏缓存。
        return value

    def invalidate_event_reads(self, reason: str) -> None:
        """LocalWebServer 侧入口：委托给模块级失效（靠 epoch 使新读走冷路径，保留上一版快照供 stale_busy 降级）。"""
        from life_logger.local_web import (
            invalidate_read_caches,
        )

        invalidate_read_caches(self.store.root, reason)

    def build_daily_brief(
        self,
        *,
        date: str | None = None,
        allow_empty: bool = False,
        scope: str = "provisional",
        view: str = "display",
    ) -> dict[str, Any]:
        from life_logger.local_web import (
            _iso,
            datetime,
        )

        target_date = date or datetime.now(tz=ZoneInfo("Asia/Shanghai")).date().isoformat()
        try:
            # 【2026-10-24 P0】Web 只读路径启用 event_view 的 10s 扫描缓存：
            # 切日期时前端会并发打多个日接口，同一窗口只扫一次；
            # 测试/批处理走默认 use_scan_cache=False，保证写后立刻读是新的。
            return build_daily_brief(
                self.store.root, date=target_date,
                task_states=self.store.read_task_states(), scope=scope, view=view,
                use_scan_cache=True,
            )
        except ValueError as error:
            if not allow_empty or "没有找到" not in str(error):
                raise
            from life_logger.event_view import build_event_view
            # Validate scope/view/date even for an empty day; never silently
            # accept invalid read contracts just because there are no records.
            event_view = build_event_view(
                self.store.root, date_from=target_date, date_to=target_date,
                scope=scope, view=view, use_cache=True,
            )
            return {
                "schema_version": "daily-brief-v1", "date": target_date,
                "timezone": "Asia/Shanghai", "generated_at": _iso(),
                "event_count": 0, "category_summary": {}, "todos": [],
                "warnings": ["今天尚无已处理记录。"], "events": [],
                "source_daily_summaries": [], "accounting_scope": scope,
                "scope": scope, "view": view, "event_view": event_view["metadata"],
            }

    def build_agent_brief(
        self,
        *,
        date: str | None = None,
        allow_empty: bool = False,
        hide_dictation: bool = False,
        scope: str = "provisional",
        view: str = "display",
    ) -> dict[str, Any]:
        brief = self.build_daily_brief(date=date, allow_empty=allow_empty, scope=scope, view=view)
        if hide_dictation:
            brief, _ = self.apply_dashboard_dictation_filter(
                brief, list(brief.get("events", []))
            )
        outbound = external_agent_brief(brief)
        source_event_count = int(brief.get("event_count") or 0)
        included_event_count = int(outbound.get("event_count") or 0)

        # 【2026-10-19 修】三接口口径统一：
        # `/api/daily`、`/api/stats`、`/api/daily-brief` 在同一 scope/view 下
        # **事件数与金额必须完全相等**。此前 brief 把 `event_count` 报成「置信度过滤后」
        # 的投影数（实测 10-14：44 vs 30、10-18：24 vs 18），金额干脆不返回，
        # 于是同一个日期在三个页面上是三个数。
        # 现在：`event_count` 报权威数（与 event_view 一致），投影数改名为
        # `projection_event_count`；金额一律取 expense_policy 的账本。
        from life_logger.day_facts import event_only_expense_ledger

        ledger = event_only_expense_ledger(list(brief.get("events", [])))
        outbound["event_count"] = source_event_count
        outbound["projection_event_count"] = included_event_count
        outbound["expense_total"] = {
            "total_amount": ledger.get("total_amount"),
            "currency": ledger.get("currency"),
            "totals_by_currency": dict(ledger.get("totals_by_currency") or {}),
            "transaction_count": ledger.get("transaction_count"),
            "policy_version": ledger.get("policy_version"),
        }
        outbound["expense_total_cny"] = float(ledger.get("total_amount") or 0.0)
        outbound["read_contract"] = _read_contract(
            str(brief.get("accounting_scope") or scope),
            str(brief.get("view") or view),
            # 事件数与金额是权威口径；`events` 数组仍是置信度投影，两者差异显式给出。
            notice=(
                "event_count / expense_total are canonical (event_view + expense_policy); "
                "the events array is a confidence-gated projection of the same set "
                "(see projection_event_count)."
            ),
        )
        outbound["event_count_basis"] = "canonical_event_view"
        outbound["confidence_filter"] = {
            "policy": "external_agent_brief",
            "min_confidence": float(AGENT_MIN_CONFIDENCE),
            "source_event_count": source_event_count,
            "included_event_count": included_event_count,
            "filtered_event_count": max(0, source_event_count - included_event_count),
        }
        return outbound

    def deliver_daily_brief(
        self,
        *,
        date: str | None = None,
        webhook_url: str | None = None,
    ) -> dict[str, Any]:
        brief = self.build_agent_brief(date=date)
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
        configured_outbox, configured_webhook = outbound_options(
            config if isinstance(config, dict) else {}
        )
        delivery = deliver_daily_brief(
            brief,
            outbox_dir=configured_outbox or default_outbox_dir(self.store.root),
            webhook_url=webhook_url or configured_webhook,
        )
        return {"brief": brief, "delivery": delivery}

    def filter_reminders_dictation(
        self, reminders: Sequence[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        """只在返回层隐去 dictation 来源的待办。

        事实源（OpenClaw 的任务池、reminders.json、产物）一律不动；
        判不出来源的一律保留。
        """
        return self.dashboard_dictation_filter().filter_reminders(
            reminders, event_lookup=self._reminder_event_lookup
        )
