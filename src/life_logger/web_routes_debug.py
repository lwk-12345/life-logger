"""从 _Handler 机械提取的路由，分支体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

import gc
import json
import threading
import time


class _DebugRoutesMixin:
    def _get_debug_read_cache(self, parsed):
        # [2026-10-23 W7] 只读诊断：内存到底被谁占着。
        # 不缓存 diagnostic 自身；数字只做数量级判断，不参与业务。
        import resource

        with self.app._read_cache_lock:
            cache_items = [
                (key, len(json.dumps(value, ensure_ascii=False, default=str)))
                for key, (_ts, value) in list(self.app._read_cache.items())
            ]
        thread_counts: dict[str, int] = {}
        for thread in threading.enumerate():
            name = str(thread.name or "?")
            thread_counts[name] = thread_counts.get(name, 0) + 1
        transcript_cache = globals().get("_TRANSCRIPT_PAYLOAD_CACHE", {})
        utterance_cache = globals().get("_JOB_UTTERANCE_ITEMS_CACHE", {})
        try:
            gc_stats = [
                {"collections": row.get("collections"), "collected": row.get("collected")}
                for row in gc.get_stats()
            ]
        except Exception:
            gc_stats = []
        self._send_json(
            {
                "ok": True,
                "rusage_maxrss_mb": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1
                ),
                "read_cache": {
                    "entries": len(cache_items),
                    "max_entries": self.app._read_cache_max_entries,
                    "approx_bytes": sum(size for _key, size in cache_items),
                    "top": sorted(cache_items, key=lambda item: item[1], reverse=True)[:10],
                },
                "transcript_cache": {
                    "entries": len(transcript_cache),
                    "cached_file_bytes": sum(
                        int(item[1] or 0)
                        for item in transcript_cache.values()
                        if isinstance(item, (tuple, list)) and len(item) >= 2
                    ),
                },
                "utterance_cache": {"entries": len(utterance_cache)},
                "search_events_cache": len(globals().get("_SEARCH_EVENTS_CACHE", {})),
                "search_states_cache": len(globals().get("_SEARCH_STATES_CACHE", {})),
                "thread_count": len(thread_counts),
                "threads": dict(
                    sorted(thread_counts.items(), key=lambda item: item[1], reverse=True)[:20]
                ),
                "gc_count": list(gc.get_count()),
                "gc_stats": gc_stats,
            }
        )
        return

    def _get_debug_read_inflight(self, parsed):
        # [2026-10-23 P-53] 一眼看穿读请求是在算还是在排队。
        now = time.monotonic()
        with self.app._read_cache_lock:
            active = int(self.app._read_producers_active)
            waiting = int(self.app._read_slot_waiting)
            inflight = []
            for key, meta in self.app._read_inflight_meta.items():
                age = now - float(meta.get("started_at") or now)
                # 超时后的后台 worker 仍可能跑很久；它已不占槽位，
                # 也不应把「inflight age」指标拖过 600 秒。
                if age > 600.0:
                    continue
                inflight.append(
                    {
                        "key": key,
                        "age_seconds": round(age, 3),
                        "producer_elapsed_seconds": (
                            round(now - float(meta.get("producer_started_at")), 3)
                            if meta.get("producer_started_at")
                            else None
                        ),
                        "slot_wait_seconds": meta.get("slot_wait_seconds"),
                        "waiters": int(meta.get("waiters") or 0),
                    }
                )
        self._send_json(
            {
                "ok": True,
                "producer_limit": self.app._read_producer_limit,
                "producer_timeout_seconds": self.app._read_producer_timeout,
                "producer_run_timeout_seconds": self.app._read_producer_run_timeout,
                "active_producers": active,
                "waiting_for_slot": waiting,
                "inflight_entries": len(inflight),
                "inflight": sorted(inflight, key=lambda item: item["age_seconds"], reverse=True),
            }
        )
        return
