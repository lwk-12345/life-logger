#!/usr/bin/env python3
"""刷新 `web/coverage-gaps.json`（页面缺口快照）。

背景（Q585）：这个快照原来**只在 `pre_restart_gate.sh` 的 G8 里生成**。
于是「补跑把缺口补齐了」之后，页面仍然照着旧快照显示
「本日有 67 行未能整理」——用户看到「明明补了还告诉我缺」。

本脚本把 G8 里那段 transform 抽出来，做成可单独调用的一次性命令，
补跑/修复流程结束时调它即可，无需等下一次门禁。

用法：
    python3 scripts/refresh_coverage_gaps.py                    # 刷新 web/coverage-gaps.json
    python3 scripts/refresh_coverage_gaps.py --dry-run          # 只打印将写入什么
    python3 scripts/refresh_coverage_gaps.py --out /tmp/x.json  # 写到别处
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_OUT = ROOT / "web" / "coverage-gaps.json"
CHECKER = ROOT / "scripts" / "check_coverage_consistency.py"


def build_snapshot(report: dict) -> dict:
    """与 pre_restart_gate.sh 的 G8 完全一致的 transform（保持单一口径）。"""
    days = []
    for d in report.get("days") or []:
        cls = d.get("classification")
        if cls in ("no_timeline", "no_data", "consistent"):
            continue
        days.append({
            "date": d.get("date"), "classification": cls,
            "gap_count": d.get("gap_count"), "gap_first": d.get("gap_first"),
            "gap_last": d.get("gap_last"), "through": d.get("through"),
            "last_success_cutoff": d.get("last_success_cutoff"), "reason": d.get("reason"),
        })
    return {
        "schema": "coverage-gaps-snapshot-v1",
        "generated_at": datetime.datetime.now(tz=BEIJING).isoformat(timespec="seconds"),
        "route": report.get("route"),
        "source": "scripts/check_coverage_consistency.py --json",
        "summary": report.get("summary") or {},
        "asr_rows_scanned": report.get("asr_rows_scanned"),
        "days": days,
    }


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".coverage-gaps.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="刷新页面用的 coverage-gaps 快照")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径（默认 web/coverage-gaps.json）")
    ap.add_argument("--route", default="jev")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args(argv)

    proc = subprocess.run(
        [sys.executable, str(CHECKER), "--json", "--route", args.route],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        print(f"自检脚本失败（rc={proc.returncode}）：{proc.stderr[-400:]}", file=sys.stderr)
        return 1
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"自检输出不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    snapshot = build_snapshot(report)
    gaps = [d for d in snapshot["days"] if d["classification"] == "gap"]
    print(f"缺口天数={len(gaps)} 其它异常={len(snapshot['days']) - len(gaps)} "
          f"扫描行={snapshot.get('asr_rows_scanned')}")
    for d in snapshot["days"][:8]:
        print(f"  {d['date']} {d['classification']} rows={d['gap_count']} "
              f"{str(d.get('gap_first') or '')[11:19]}→{str(d.get('gap_last') or '')[11:19]}")

    if args.dry_run:
        print(f"[dry-run] 将写入 {args.out}（generated_at={snapshot['generated_at']}）")
        return 0
    write_atomic(Path(args.out), snapshot)
    print(f"已刷新 {args.out}（generated_at={snapshot['generated_at']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
