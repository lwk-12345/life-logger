#!/usr/bin/env python3
"""重跑指定日期的日报（daily synthesis），并统计模型调用与 token 消耗。

用途（2026-10-19 用户授权）：把 10-17 / 10-18 / 10-19 三天用**当前修好的代码**
重新生成一遍，让「日报金额 == 账本金额」在磁盘文件上真正成立。

设计要点：
1. 事件集合与 `/api/daily` **完全同源**：`build_daily_brief` + dictation 读层过滤，
   否则重跑出来的日报又会和接口对不上；
2. 只允许白名单日期，防止误伤更早历史；
3. 统计模型调用次数与 token：包一层 `urlopen`，读响应里的 `usage` 再原样返回；
4. 不重启服务、不打印密钥。

用法：
    python3 scripts/rerun_daily_synthesis.py --dry-run          # 只算不写
    python3 scripts/rerun_daily_synthesis.py --dates 2026-10-17
    python3 scripts/rerun_daily_synthesis.py --dates 2026-10-17,2026-10-18,2026-10-19
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import urlopen as real_urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from life_logger import daily_cognitive_session as dcs  # noqa: E402
from life_logger.day_facts import event_only_expense_ledger  # noqa: E402
from life_logger.local_web import JobStore, LocalWebServer  # noqa: E402

ALLOWED = {"2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"}


class UsageCounter:
    """包一层 urlopen：记录每次模型调用的 usage，再把响应交给原逻辑。"""

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.per_call: list[dict[str, object]] = []

    def __call__(self, request, *args, **kwargs):  # noqa: ANN001
        response = real_urlopen(request, *args, **kwargs)
        body = response.read()
        self.calls += 1
        usage: dict[str, object] = {}
        try:
            payload = json.loads(body)
            usage = payload.get("usage") or {}
            model = payload.get("model")
        except Exception:  # 非 JSON（例如错误页）也要放行，让上层正常报错
            model = None
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)
        self.per_call.append({
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        })
        return _Replay(body)


class _Replay:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, *args, **kwargs) -> bytes:  # noqa: ANN002, ANN003
        return self._body

    def __enter__(self) -> "_Replay":
        return self

    def __exit__(self, *args) -> bool:  # noqa: ANN002
        return False


def _build_app() -> LocalWebServer:
    """构造只读的 LocalWebServer；后台写线程全部置空（不产生任何副作用）。"""
    for name in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                 "_warm_vip_transcriber", "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, name):
            setattr(LocalWebServer, name, lambda self, *a, **k: None)
    store = JobStore(ROOT / "output" / "local-web", recover=False)
    return LocalWebServer(("127.0.0.1", 0), store, ROOT / "local-config.json")


def _events_for(app: LocalWebServer, date: str) -> tuple[list[dict], dict]:
    """与 `/api/daily` 完全同源的事件集合 + 账本。"""
    brief = app.build_daily_brief(date=date, allow_empty=True, scope="provisional", view="display")
    _filtered, events = app.apply_dashboard_dictation_filter(brief, list(brief.get("events", [])))
    events = [dict(e) for e in events]
    return events, event_only_expense_ledger(events)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", default="2026-10-17,2026-10-18,2026-10-19")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    outside = [d for d in dates if d not in ALLOWED]
    if outside:
        print(f"拒绝执行：这些日期不在白名单内 {outside}（只允许 {sorted(ALLOWED)}）")
        return 2

    app = _build_app()
    counter = UsageCounter()
    original = dcs.urllib.request.urlopen
    dcs.urllib.request.urlopen = counter  # type: ignore[assignment]
    results = []
    try:
        for date in dates:
            events, ledger = _events_for(app, date)
            file_path = ROOT / "output" / "local-web" / "daily-synthesis" / f"synthesis-{date}.json"
            before = None
            if file_path.is_file():
                try:
                    before = (json.loads(file_path.read_text()).get("expense_analysis") or {}).get("total_amount_cny")
                except Exception:
                    before = None
            if args.dry_run:
                results.append({"date": date, "dry_run": True, "events": len(events),
                                "ledger_total": ledger.get("total_amount"), "file_before": before})
                print(f"[dry-run] {date}: events={len(events)} ledger=¥{ledger.get('total_amount')} file_before={before}")
                continue
            started = time.monotonic()
            synthesis = dcs.generate_daily_cognitive_synthesis(
                date, events, force=True, cache_dir=ROOT / "output" / "local-web" / "daily-synthesis"
            )
            elapsed = round(time.monotonic() - started, 1)
            after = (synthesis.get("expense_analysis") or {}).get("total_amount_cny")
            results.append({
                "date": date,
                "events": len(events),
                "ledger_total": ledger.get("total_amount"),
                "file_before": before,
                "file_after": after,
                "synthesis_event_count": synthesis.get("event_count"),
                "fallback": bool(synthesis.get("fallback")),
                "elapsed_seconds": elapsed,
                "aligned": abs(float(after or 0) - float(ledger.get("total_amount") or 0)) < 0.005,
            })
            print(f"{date}: events={len(events)} ledger=¥{ledger.get('total_amount')} "
                  f"file {before} → {after} ({elapsed}s, fallback={bool(synthesis.get('fallback'))})")
    finally:
        dcs.urllib.request.urlopen = original  # type: ignore[assignment]

    summary = {
        "dates": dates,
        "dry_run": args.dry_run,
        "results": results,
        "model": {
            "calls": counter.calls,
            "prompt_tokens": counter.prompt_tokens,
            "completion_tokens": counter.completion_tokens,
            "total_tokens": counter.total_tokens,
            "per_call": counter.per_call,
        },
    }
    out = ROOT / "work" / "rerun-3days-20261019" / "rerun-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n模型调用 {counter.calls} 次 | prompt {counter.prompt_tokens} tok | "
          f"completion {counter.completion_tokens} tok | total {counter.total_tokens} tok")
    print(f"结果: {out.relative_to(ROOT)}")
    return 0 if all(r.get("dry_run") or r.get("aligned") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
