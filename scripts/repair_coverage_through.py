#!/usr/bin/env python3
"""修复「整天只走 backfill」的历史日：`coverage.through` 被留成 null。

背景（Q585）：`through` 原来只在 live 轮推进。历史日补跑（backfill）即使把所有行
都处理完，`through` 仍是 null → 页面水位显示「—」，与「本日已有 N 个活动」自相矛盾
（实测 2026-10-02）。

运行时已经修好（补跑成功且该日无剩余未处理行时按证据推进 through），但**存量数据**
不会自己变。本脚本做一次性修复：

  对每个 activity-timeline 日产物：
    1. 全量扫描该日 ASR 行（复用 check_coverage_consistency 的扫描器，不截断）；
    2. 若该日**仍有未处理行** → 跳过（绝不凭空推 through，避免制造「through 说谎」）；
    3. 若该日已全部处理 且 (through 缺失 或 through < 最后一条已处理行) →
       把 through 设为「最后一条已处理行的时间」。

默认 **dry-run**；只有显式 `--apply` 才写盘（原子替换，写前备份可选）。

用法：
    python3 scripts/repair_coverage_through.py                  # 只报告
    python3 scripts/repair_coverage_through.py --apply          # 真写盘
    python3 scripts/repair_coverage_through.py --apply --refresh-gaps
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
DEFAULT_ROOT = ROOT / "output" / "local-web"


def _load_checker():
    path = ROOT / "scripts" / "check_coverage_consistency.py"
    spec = importlib.util.spec_from_file_location("check_coverage_consistency", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_atomic(path: Path, payload: dict) -> None:
    """原子写：统一走 life_logger.atomic（mkstemp → write → fsync(file) → replace → fsync(dir)）。

    [2026-11-03 v2] 原实现是本地手写的 mkstemp + os.replace，**缺 fsync**：
    断电/SIGKILL 后 rename 可能回退到旧文件（甚至变成空文件），而本脚本已经打印「已修复」
    并退出 0。这正是 11:39「activities=0」的同一类失效面（写入者以为自己成功了）。
    这里不再维护第二份实现，避免两边行为漂移。
    """
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from life_logger.atomic import write_json_atomic

    write_json_atomic(path, payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="修复历史日 coverage.through = null")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="output/local-web 根目录")
    ap.add_argument("--route", default="jev")
    ap.add_argument("--date", default=None, help="只修某一天")
    ap.add_argument("--apply", action="store_true", help="真写盘（默认 dry-run）")
    ap.add_argument("--refresh-gaps", action="store_true",
                    help="修完后顺手刷新 web/coverage-gaps.json")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    checker = _load_checker()
    all_rows = checker.scan_all_asr_rows(root)
    by_date: dict[str, list[dict]] = {}
    for row in all_rows:
        by_date.setdefault(row["timestamp"].date().isoformat(), []).append(row)

    tl_dir = root / "activity-timeline" / args.route
    if not tl_dir.is_dir():
        print(f"找不到 {tl_dir}", file=sys.stderr)
        return 1

    planned: list[dict] = []
    for path in sorted(tl_dir.glob("*.json")):
        if path.name.endswith(".corrections.json") or path.name.startswith("."):
            continue
        date = path.stem
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            continue
        if args.date and date != args.date:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        processed = set(meta.get("processed_source_ids") or [])
        rows = by_date.get(date, [])
        if not rows:
            continue
        pending = [r for r in rows if r["source_id"] not in processed]
        if pending:
            planned.append({"date": date, "action": "skip",
                            "why": f"仍有 {len(pending)} 行未处理", "path": str(path)})
            continue
        last_ts = max(r["timestamp"] for r in rows)
        current = coverage.get("through")
        try:
            current_dt = datetime.datetime.fromisoformat(current) if current else None
        except (TypeError, ValueError):
            current_dt = None
        if current_dt is not None and current_dt >= last_ts:
            continue
        planned.append({
            "date": date,
            "action": "set",
            "from": current,
            "to": last_ts.astimezone(BEIJING).isoformat(),
            "rows": len(rows),
            "path": str(path),
            "_payload": payload,
        })

    todo = [p for p in planned if p["action"] == "set"]
    skipped = [p for p in planned if p["action"] == "skip"]
    print(f"扫描日产物：{len(planned)} 个候选；需修复 {len(todo)} 天；跳过 {len(skipped)} 天（仍有未处理行）")
    for item in todo:
        print(f"  [set ] {item['date']}  through {item['from']} → {item['to']}  （{item['rows']} 行全部已处理）")
    for item in skipped:
        print(f"  [skip] {item['date']}  {item['why']}")

    if not args.apply:
        print("\n[dry-run] 未写盘。加 --apply 才会真正修复。")
        return 0

    changed = 0
    for item in todo:
        payload = item["_payload"]
        payload.setdefault("coverage", {})["through"] = item["to"]
        _write_atomic(Path(item["path"]), payload)
        changed += 1
    print(f"\n已修复 {changed} 天。")

    if args.refresh_gaps and changed:
        sys.path.insert(0, str(ROOT / "scripts"))
        import refresh_coverage_gaps as refresh  # noqa: PLC0415
        rc = refresh.main([])
        if rc != 0:
            print("缺口快照刷新失败（不影响 through 修复）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
