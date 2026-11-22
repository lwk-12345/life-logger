#!/usr/bin/env python3
"""影子对比（docs/adr/0005 P2）：活动事实和正式事件逐日对比，只读，不改任何数字。"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.fact_types import has_self_evidence, item_fact_type  # noqa: E402

BASE = "http://127.0.0.1:8765"
EVENT_TYPES = {"expense": "expense", "food": "food", "exercise": "exercise", "todo": "todo", "idea": "idea"}


def fetch(path: str, token: str) -> dict:
    request = Request(BASE + path, headers={"Authorization": "Bearer " + token})
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def day_row(day: str, token: str) -> dict:
    timeline = fetch(f"/api/activity-timeline?date={day}", token)
    daily = fetch(f"/api/daily?date={day}", token)
    facts: list[dict] = []
    for activity in timeline.get("activities") or []:
        for item in activity.get("items") or []:
            kind = item_fact_type(item, activity)
            if kind == "none":
                continue
            facts.append({
                "type": kind,
                "self": has_self_evidence(item),
                "action": str(item.get("action") or ""),
                "amount": item.get("amount"),
                "quotes": [str(e.get("quote") or "") for e in item.get("evidence") or []],
            })
    events = collections.Counter()
    event_quotes: dict[str, list[str]] = collections.defaultdict(list)
    for event in daily.get("events") or []:
        quote = str((event.get("evidence") or {}).get("quote") or "")
        for category in event.get("categories") or []:
            if category in EVENT_TYPES:
                events[category] += 1
                event_quotes[category].append(quote)
    only_activity, overlap = collections.Counter(), collections.Counter()
    samples: dict[str, list[str]] = collections.defaultdict(list)
    for fact in facts:
        hit = any(q and any(q[:8] and q[:8] in other for other in event_quotes[fact["type"]]) for q in fact["quotes"])
        (overlap if hit else only_activity)[fact["type"]] += 1
        if not hit and len(samples[fact["type"]]) < 3:
            samples[fact["type"]].append(fact["action"][:30])
    return {
        "date": day,
        "activity_facts": dict(collections.Counter(f["type"] for f in facts)),
        "self_facts": sum(1 for f in facts if f["self"]),
        "events": dict(events),
        "overlap": dict(overlap),
        "only_activity": dict(only_activity),
        "samples_only_activity": dict(samples),
        "ledger_total": daily["expense_ledger"].get("total_amount"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    token = json.loads((ROOT / "output/local-web/secrets.json").read_text())["mobile_access_token"]
    day, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    rows = []
    while day <= end:
        rows.append(day_row(day.isoformat(), token))
        day += dt.timedelta(days=1)
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(row["date"], "活动", row["activity_facts"], "事件", row["events"], "重合", row["overlap"], "账", row["ledger_total"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
