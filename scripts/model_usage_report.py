#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天、按步骤汇总模型调用用量（读 `output/local-web/model-usage/*.jsonl`）。

数据来源：`life_logger.model_usage` 写的 JSONL（每行一条调用记录，UTC+8 归日）。

用法
----
    python3 scripts/model_usage_report.py                     # 最近 7 天
    python3 scripts/model_usage_report.py --days 30
    python3 scripts/model_usage_report.py --date 2026-11-06
    python3 scripts/model_usage_report.py --json              # 机器可读
    python3 scripts/model_usage_report.py --dir /tmp/usage    # 换目录（测试用）

只读：本脚本只读 JSONL，不写任何东西。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.model_usage import DEFAULT_DIR, STEPS  # noqa: E402

UTC8 = dt.timezone(dt.timedelta(hours=8))


def _usage_dir(arg: str | None) -> Path:
    import os

    if arg:
        return Path(arg)
    override = os.environ.get("LL_MODEL_USAGE_DIR")
    return Path(override) if override else DEFAULT_DIR


def _iter_records(directory: Path, days: int | None, only_day: str | None):
    if not directory.is_dir():
        return
    files = sorted(directory.glob("*.jsonl"))
    if only_day:
        files = [p for p in files if p.stem == only_day]
    elif days is not None:
        cutoff = (dt.datetime.now(tz=UTC8).date() - dt.timedelta(days=days - 1)).isoformat()
        files = [p for p in files if p.stem >= cutoff]
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # 坏行不静默吞：计数后由调用方报告
                yield {"__bad_line__": path.name}
                continue
            record.setdefault("date", path.stem)
            yield record


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def summarize(records: list[dict]) -> dict:
    bad = sum(1 for r in records if "__bad_line__" in r)
    good = [r for r in records if "__bad_line__" not in r]

    by_day: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"calls": 0, "ok": 0, "fail": 0, "prompt": 0, "completion": 0,
                 "total": 0, "cached": 0, "reasoning": 0, "elapsed": [], "models": set()}
    ))
    for record in good:
        day = str(record.get("date") or "?")
        step = str(record.get("step") or "?")
        cell = by_day[day][step]
        cell["calls"] += 1
        if record.get("ok") is False:
            cell["fail"] += 1
        else:
            cell["ok"] += 1
        for key, field in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"),
                           ("total", "total_tokens"), ("cached", "cached_tokens"),
                           ("reasoning", "reasoning_tokens")):
            cell[key] += int(_num(record.get(field)))
        if isinstance(record.get("elapsed_ms"), (int, float)):
            cell["elapsed"].append(float(record["elapsed_ms"]))
        if record.get("model"):
            cell["models"].add(str(record["model"]))

    out = {}
    for day, steps in by_day.items():
        out[day] = {}
        for step, cell in steps.items():
            elapsed = sorted(cell["elapsed"])
            out[day][step] = {
                "calls": cell["calls"],
                "ok": cell["ok"],
                "fail": cell["fail"],
                "prompt_tokens": cell["prompt"],
                "completion_tokens": cell["completion"],
                "total_tokens": cell["total"],
                "cached_tokens": cell["cached"],
                "reasoning_tokens": cell["reasoning"],
                "elapsed_ms_p50": round(statistics.median(elapsed), 1) if elapsed else None,
                "elapsed_ms_max": round(max(elapsed), 1) if elapsed else None,
                "models": sorted(cell["models"]),
            }
    return {"days": dict(sorted(out.items())), "bad_lines": bad, "records": len(good)}


def _fmt(n: float | int | None) -> str:
    if n is None:
        return "-"
    if isinstance(n, float):
        return f"{n:,.0f}"
    return f"{n:,}"


def print_table(summary: dict) -> None:
    if not summary["records"]:
        print("（没有用量记录：output/local-web/model-usage/ 里还没有 JSONL）")
        return
    print(f"{'日期':12s} {'步骤':26s} {'次数':>5s} {'失败':>4s} {'prompt':>10s} "
          f"{'completion':>11s} {'缓存':>9s} {'reasoning':>10s} {'p50ms':>8s} {'maxms':>8s}")
    print("-" * 122)
    totals = {"calls": 0, "fail": 0, "prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
    for day, steps in summary["days"].items():
        day_tot = {"calls": 0, "fail": 0, "prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
        for step, c in steps.items():
            label = STEPS.get(step, step)
            print(f"{day:12s} {label[:26]:26s} {c['calls']:5d} {c['fail']:4d} "
                  f"{_fmt(c['prompt_tokens']):>10s} {_fmt(c['completion_tokens']):>11s} "
                  f"{_fmt(c['cached_tokens']):>9s} {_fmt(c['reasoning_tokens']):>10s} "
                  f"{_fmt(c['elapsed_ms_p50']):>8s} {_fmt(c['elapsed_ms_max']):>8s}")
            for key in day_tot:
                day_tot[key] += c[{"calls": "calls", "fail": "fail", "prompt": "prompt_tokens",
                                   "completion": "completion_tokens", "cached": "cached_tokens",
                                   "reasoning": "reasoning_tokens"}[key]]
        print(f"{'  └ 小计':12s} {'':26s} {day_tot['calls']:5d} {day_tot['fail']:4d} "
              f"{_fmt(day_tot['prompt']):>10s} {_fmt(day_tot['completion']):>11s} "
              f"{_fmt(day_tot['cached']):>9s} {_fmt(day_tot['reasoning']):>10s}")
        for key in totals:
            totals[key] += day_tot[key]
    print("-" * 122)
    all_calls = totals["calls"]
    print(f"{'合计':12s} {'':26s} {all_calls:5d} {totals['fail']:4d} "
          f"{_fmt(totals['prompt']):>10s} {_fmt(totals['completion']):>11s} "
          f"{_fmt(totals['cached']):>9s} {_fmt(totals['reasoning']):>10s}")
    if summary["bad_lines"]:
        print(f"\n⚠ 跳过坏行 {summary['bad_lines']} 条（JSONL 解析失败）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="模型调用用量汇总（只读）")
    ap.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7）")
    ap.add_argument("--date", default=None, help="只看某一天（YYYY-MM-DD，UTC+8）")
    ap.add_argument("--dir", default=None, help="用量目录（默认 output/local-web/model-usage）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    directory = _usage_dir(args.dir)
    records = list(_iter_records(directory, None if args.date else args.days, args.date))
    summary = summarize(records)
    if args.json:
        print(json.dumps({"dir": str(directory), **summary}, ensure_ascii=False, indent=1))
    else:
        print(f"目录：{directory}")
        print_table(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
