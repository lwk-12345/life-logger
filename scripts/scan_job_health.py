#!/usr/bin/env python3
"""任务健康审计：找出「假成功」——自称完成的 job 其实有子步骤失败或产物缺失。

判据不靠 status 一个字段，而是拿证据核对（见 ``life_logger.local_web``）：

  * 文件/阶段被判失败（结构性致命）              → 应为 partial
  * 任务声明的产物不在盘上                        → 应为 partial
  * 阶段自称完成、但落盘日志最后一次失败之后再没有成功记录
      - 产物还在 → 应为 degraded（结果可用，过程不可信）
      - 产物也没了 → 应为 partial
  * 阶段失败过但重跑成功 / 日志里留着 Traceback   → 记录保留，仍可算完成

默认 dry-run：只读、只打印，不改任何文件。确认后加 ``--apply`` 才回写
（回写前会把原 status.json 备份成 ``status.json.bak-<时间戳>``）。

    PYTHONPATH=. python3 scripts/scan_job_health.py
    PYTHONPATH=. python3 scripts/scan_job_health.py --json /tmp/job-health.json
    PYTHONPATH=. python3 scripts/scan_job_health.py --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "output/local-web"

# 只在「自称成功」的状态上谈假成功：draft/failed 本来就如实。
SUCCESS_CLAIMS = {"completed", "degraded"}
_TRACEBACK_MARKER = "Traceback (most recent call last)"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _log_text(state: dict[str, Any]) -> str:
    lines = list(state.get("logs") or [])
    for item in state.get("files") or []:
        if isinstance(item, dict):
            lines.extend(item.get("logs") or [])
    return "\n".join(str(line) for line in lines)


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    # [2026-10-29 第三批收敛] 旧实现用固定名 `<name>.json.tmp`：两个进程同时写同一
    # 目标会互相覆盖，且没有 fsync（崩溃可能留下截断文件）。改用唯一的 tmp 名 +
    # fsync + os.replace；**序列化参数不变**（ensure_ascii=False, indent=2）→ 字节一致。
    from life_logger.atomic import write_json_atomic

    write_json_atomic(path, payload, indent=2)


def scan(job_root: Path) -> list[dict[str, Any]]:
    from life_logger.local_web import collect_stage_errors

    rows: list[dict[str, Any]] = []
    for path in sorted(job_root.glob("job-*/status.json")):
        state = _load(path)
        if state is None:
            rows.append(
                {
                    "job_id": path.parent.name,
                    "claimed_status": None,
                    "verdict": "unreadable",
                    "logs_have_traceback": False,
                    "errors": [],
                }
            )
            continue
        job_dir = path.parent
        verdict = copy.deepcopy(state)
        # 复用运行时的同一套判据，保证审计与线上状态不会各说各话。
        from life_logger.local_web import apply_file_outcomes

        apply_file_outcomes(verdict, job_dir=job_dir)
        errors = verdict.get("stage_errors")
        if not isinstance(errors, list):
            errors = collect_stage_errors(state, job_dir=job_dir)
        claimed = state.get("status")
        effective = verdict.get("status")
        unresolved_fatal = [
            item
            for item in errors
            if item.get("severity") == "fatal" and not item.get("resolved")
        ]
        unresolved_soft = [
            item
            for item in errors
            if item.get("severity") != "fatal" and not item.get("resolved")
        ]
        recovered = [item for item in errors if item.get("resolved")]
        if claimed not in SUCCESS_CLAIMS:
            bucket = f"already_{claimed or 'unknown'}"
        elif unresolved_fatal:
            bucket = "false_success_fatal"
        elif unresolved_soft:
            bucket = "false_success_degraded"
        elif recovered:
            bucket = "completed_with_history"
        else:
            bucket = "clean"
        rows.append(
            {
                "job_id": str(state.get("job_id") or job_dir.name),
                "path": str(path),
                "claimed_status": claimed,
                "verdict": effective,
                "bucket": bucket,
                "logs_have_traceback": _TRACEBACK_MARKER in _log_text(state),
                "failed_file_count": state.get("failed_file_count"),
                "errors": errors,
                "unresolved_fatal": len(unresolved_fatal),
                "unresolved_soft": len(unresolved_soft),
                "recovered": len(recovered),
            }
        )
    return rows


def build_report(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    buckets = Counter(row["bucket"] for row in rows)
    claimed = Counter(str(row.get("claimed_status")) for row in rows)
    lines.append("# 任务健康审计（假成功扫描）")
    lines.append("")
    lines.append(f"扫描任务数：{len(rows)}")
    lines.append("")
    lines.append("## 声称的终态")
    lines.append("")
    for status, count in claimed.most_common():
        lines.append(f"- {status}: {count}")
    lines.append("")
    lines.append("## 按证据重新判决")
    lines.append("")
    labels = {
        "clean": "✅ 干净（没有任何失败证据）",
        "completed_with_history": "⚠️ 完成但有历史失败记录（曾失败后重跑成功）",
        "false_success_degraded": "❌ 假成功（可降级失败未解决，应为 degraded）",
        "false_success_fatal": "❌❌ 假成功（致命失败/产物缺失，应为 partial）",
    }
    ordered = [
        "false_success_fatal",
        "false_success_degraded",
        "completed_with_history",
        "clean",
    ]
    for key in ordered:
        lines.append(f"- {labels[key]}: {buckets.get(key, 0)}")
    for key, count in sorted(buckets.items()):
        if key in ordered:
            continue
        lines.append(f"- {key}: {count}")
    lines.append("")
    traceback_claims = [
        row
        for row in rows
        if row["logs_have_traceback"] and row["claimed_status"] in SUCCESS_CLAIMS
    ]
    lines.append(
        f"logs 里含 Traceback 但终态是 {sorted(SUCCESS_CLAIMS)} 的任务："
        f"{len(traceback_claims)}"
    )
    for row in traceback_claims:
        note = {
            "completed_with_history": "已重跑成功，保留记录",
            "clean": "需要人工看一眼",
            "false_success_degraded": "已判 degraded",
            "false_success_fatal": "已判 partial",
        }.get(row["bucket"], row["bucket"])
        lines.append(f"  - {row['job_id']}（{row['claimed_status']} → {note}）")
    lines.append("")
    for key in ("false_success_fatal", "false_success_degraded"):
        bad = [row for row in rows if row["bucket"] == key]
        if not bad:
            continue
        lines.append(f"## {labels[key]} · {len(bad)} 个")
        lines.append("")
        for row in bad:
            unresolved = [
                item for item in row["errors"] if not item.get("resolved")
            ]
            lines.append(
                f"### {row['job_id']}（status={row['claimed_status']}，"
                f"证据判决={row['verdict']}）"
            )
            for item in unresolved[:6]:
                lines.append(
                    f"  - [{item.get('severity')}] {item.get('stage')} / "
                    f"{item.get('kind')}：{item.get('error')}"
                )
            if len(unresolved) > 6:
                lines.append(f"  - …还有 {len(unresolved) - 6} 条")
            lines.append("")
    history = [row for row in rows if row["bucket"] == "completed_with_history"]
    if history:
        lines.append(f"## 曾经失败、现已恢复 · {len(history)} 个")
        lines.append("")
        for row in history:
            kinds = Counter(str(item.get("kind")) for item in row["errors"])
            lines.append(
                f"- {row['job_id']}（{row['claimed_status']}）："
                + "、".join(f"{k}×{v}" for k, v in kinds.items())
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务健康审计（假成功扫描）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--jobs", type=Path, default=None, help="直接给 jobs 目录")
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="把判决回写到 status.json")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总行")
    args = parser.parse_args(argv)

    job_root = args.jobs or (args.root / "jobs")
    if not job_root.is_dir():
        print(f"任务目录不存在：{job_root}", file=sys.stderr)
        return 2

    rows = scan(job_root)
    report = build_report(rows)
    if not args.quiet:
        print(report)
    buckets = Counter(row["bucket"] for row in rows)
    print(
        "汇总："
        f"假成功(致命)={buckets.get('false_success_fatal', 0)} "
        f"假成功(可降级)={buckets.get('false_success_degraded', 0)} "
        f"曾失败已恢复={buckets.get('completed_with_history', 0)} "
        f"干净={buckets.get('clean', 0)} "
        f"总数={len(rows)}"
    )

    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().astimezone().isoformat(),
                    "job_root": str(job_root),
                    "buckets": dict(buckets),
                    "jobs": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"机读结果：{args.json_path}")

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        changed = 0
        for row in rows:
            # 有证据就回写：假成功要改状态，已恢复的至少要留下 stage_errors，
            # 否则下次扫描又只剩 logs 里的一段 Traceback。
            if not row["errors"]:
                continue
            path = Path(row["path"])
            state = _load(path)
            if state is None:
                continue
            from life_logger.local_web import apply_file_outcomes

            before = (state.get("status"), state.get("stage_errors"))
            apply_file_outcomes(state, job_dir=path.parent)
            if before == (state.get("status"), state.get("stage_errors")):
                continue
            shutil.copy2(path, path.with_name(f"status.json.bak-{stamp}"))
            _write_atomic(path, state)
            changed += 1
        print(
            f"已回写 {changed} 个任务的 status.json"
            f"（原文件备份为 status.json.bak-{stamp}）"
        )
    else:
        print("dry-run：没有改动任何文件；确认后加 --apply。")
    return 1 if buckets.get("false_success_fatal") else 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
