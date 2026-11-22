#!/usr/bin/env python3
"""自检：区间账本接口的总额 == 逐日 `/api/daily` 账本累加。

为什么需要：区间消费和当日消费必须是**同一本账**的两种读法。
这个脚本把两边分别算一遍，任何一天出现口径漂移都会立刻变成 `false`。

用法：
    python3 scripts/check_expense_range.py --from 2026-10-01 --to 2026-10-19
    python3 scripts/check_expense_range.py --from ... --to ... --http   # 服务已重启时比对线上接口
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def _local_side(date_from: str, date_to: str) -> tuple[dict, dict]:
    """区间接口（进程内）+ 逐日 /api/daily 等价计算。"""
    from life_logger.day_facts import event_only_expense_ledger
    from life_logger.local_web import JobStore, LocalWebServer, build_expense_range

    for name in ("_watch_live_loop", "_reminder_loop", "_auto_ingest_loop",
                 "_warm_vip_transcriber", "_run_watch_formal_asr_scan"):
        if hasattr(LocalWebServer, name):
            setattr(LocalWebServer, name, lambda self, *a, **k: None)
    app = LocalWebServer(("127.0.0.1", 0), JobStore(ROOT / "output" / "local-web", recover=False),
                         ROOT / "local-config.json")
    event_filter = app.dashboard_dictation_filter().filter_events

    range_payload = build_expense_range(
        ROOT / "output" / "local-web",
        date_from=date_from, date_to=date_to,
        scope="provisional", view="display", event_filter=event_filter,
    )

    from datetime import date, timedelta

    per_day: dict[str, dict] = {}
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        brief = app.build_daily_brief(date=key, allow_empty=True, scope="provisional", view="display")
        _filtered, events = app.apply_dashboard_dictation_filter(brief, list(brief.get("events", [])))
        ledger = event_only_expense_ledger([dict(e) for e in events])
        per_day[key] = {
            "total_cny": round(float(ledger.get("total_amount") or 0.0), 2),
            "event_count": len(events),
        }
        cursor += timedelta(days=1)
    return range_payload, per_day


def _http_side(date_from: str, date_to: str) -> dict | None:
    secrets = ROOT / "output" / "local-web" / "secrets.json"
    try:
        token = json.loads(secrets.read_text()).get("mobile_access_token") or ""
    except (OSError, ValueError):
        token = ""
    url = f"http://127.0.0.1:8765/api/expense-range?from={date_from}&to={date_to}"
    try:
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        print(f"[info] 线上接口不可用（HTTP {error.code}）——服务可能还没加载新路由，已回退纯本地比对")
        return None
    except (URLError, TimeoutError, OSError) as error:
        print(f"[info] 线上接口不可用（{type(error).__name__}）——已回退纯本地比对")
        return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--http", action="store_true", help="额外比对线上 /api/expense-range")
    args = parser.parse_args(argv)

    range_payload, per_day = _local_side(args.date_from, args.date_to)
    missing = set(range_payload.get("missing_days") or [])

    # 逐日累加时，missing 天必须**排除**（不能按 0 混进去）——这正是接口的语义。
    expected_total = round(
        sum(row["total_cny"] for day, row in per_day.items() if day not in missing), 2
    )
    per_day_mismatch = [
        {
            "date": row["date"],
            "range": row.get("total_cny"),
            "daily": per_day.get(row["date"], {}).get("total_cny"),
            "range_events": row.get("event_count"),
            "daily_events": per_day.get(row["date"], {}).get("event_count"),
        }
        for row in range_payload.get("by_day", [])
        if row["date"] not in missing
        and (
            abs(float(row.get("total_cny") or 0) - float(per_day.get(row["date"], {}).get("total_cny") or 0)) > 0.005
            or row.get("event_count") != per_day.get(row["date"], {}).get("event_count")
        )
    ]
    total_ok = abs(float(range_payload.get("total_cny") or 0) - expected_total) < 0.005
    http_ok = True
    http_total = None
    if args.http:
        live = _http_side(args.date_from, args.date_to)
        if live is not None:
            http_total = live.get("total_cny")
            http_ok = abs(float(http_total or 0) - float(range_payload.get("total_cny") or 0)) < 0.005

    verdict = bool(total_ok and not per_day_mismatch and http_ok)
    print(f"range_total={range_payload.get('total_cny')} "
          f"daily_sum_excluding_missing={expected_total} "
          f"missing_days={len(missing)} per_day_mismatch={len(per_day_mismatch)}"
          + (f" http_total={http_total}" if args.http else ""))
    print(f"aligned={str(verdict).lower()}")
    if missing:
        print(f"missing: {','.join(sorted(missing))}")
    for item in per_day_mismatch[:5]:
        print(f"  DIFF {item['date']}: range={item['range']}/{item['range_events']} "
              f"daily={item['daily']}/{item['daily_events']}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
