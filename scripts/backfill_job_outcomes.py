"""把历史任务的"假 completed"重算成 partial / degraded（补上失败文件数与
stage_errors）。

任务级 status 是派生数据，可以直接按落地事实重算；已经真正跑出来的结果一个都不动。
判据会核对阶段落盘日志与产物（不只是状态字段），所以「阶段其实是崩的、状态却写完成」
也会被认出来。默认 dry-run，只打印将要改写的清单；确认后加 --apply。

    PYTHONPATH=. uv run python scripts/backfill_job_outcomes.py
    PYTHONPATH=. uv run python scripts/backfill_job_outcomes.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from life_logger.local_web import apply_file_outcomes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "output/local-web"


def _write_atomic(path: Path, payload: dict) -> None:
    # [2026-10-29 第三批收敛] 旧实现用固定名 `<name>.json.tmp`：两个进程同时写同一
    # 目标会互相覆盖，且没有 fsync（崩溃可能留下截断文件）。改用唯一的 tmp 名 +
    # fsync + os.replace；**序列化参数不变**（ensure_ascii=False, indent=2）→ 字节一致。
    from life_logger.atomic import write_json_atomic

    write_json_atomic(path, payload, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--apply", action="store_true", help="缺省只做 dry-run")
    args = parser.parse_args()
    jobs_dir = args.root / "jobs"
    if not jobs_dir.is_dir():
        print(f"任务目录不存在：{jobs_dir}", file=sys.stderr)
        return 1
    changed = 0
    status_changes = 0
    for path in sorted(jobs_dir.glob("job-*/status.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        before_status = state.get("status")
        before_count = state.get("failed_file_count")
        before_errors = state.get("stage_errors")
        # 传 job_dir：除状态字段外，还要对着阶段落盘日志与产物核对证据，否则
        # 「阶段日志其实是崩的、状态却写完成」这类假成功看不出来。
        apply_file_outcomes(state, job_dir=path.parent)
        if (
            before_status == state.get("status")
            and before_count == state.get("failed_file_count")
            and before_errors == state.get("stage_errors")
        ):
            continue
        changed += 1
        if before_status != state.get("status"):
            status_changes += 1
        names = ", ".join(
            str(item.get("name")) for item in state.get("failed_files") or []
        )
        print(
            f"{state.get('job_id') or path.parent.name}: "
            f"{before_status} → {state['status']} "
            f"(failed_file_count={state['failed_file_count']}: {names or '无'}; "
            f"stage_errors={len(state.get('stage_errors') or [])})"
        )
        if args.apply:
            _write_atomic(path, state)
    verb = "已改写" if args.apply else "待改写"
    print(f"\n{verb} {changed} 个任务，其中任务级状态变化 {status_changes} 个")
    if not args.apply:
        print("这是 dry-run，没有改动任何文件；确认后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
