"""收口被新会话取代、仍停在 receiving 的 Apple Watch 会话。

默认 dry-run：只报告将要收口哪些会话、能放回多少分钟音频，不改任何 manifest。
确认无误后用 --apply 真正收口；收口后的会话会被 8765 服务里的导入循环接走，
进入 ASR + 声源归属，最终变成事件。

用法：
    uv run python scripts/finalize_watch_sessions.py            # 预览
    uv run python scripts/finalize_watch_sessions.py --apply    # 执行
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from life_logger.watch_stream import WATCH_RECEIVING_STALE_SECONDS, WatchSessionStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = ROOT / "output/local-web/watch-sessions"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入 manifest；缺省只做 dry-run",
    )
    parser.add_argument(
        "--session",
        action="append",
        default=None,
        metavar="SESSION_ID",
        help="只收口指定会话，可重复。缺省收口全部滞留会话——实测一次全放行是 "
        "12 个会话 / 10.3 小时音频，会把处理队列压垮，所以一个一个来。",
    )
    parser.add_argument(
        "--abandon-empty",
        action="store_true",
        help="顺带作废「已登记却一段音频都没有」的接收中会话（两边都关不掉的僵尸行）。"
        "作废只改 state，不删任何字节；手表之后补传分段会自动复活。",
    )
    parser.add_argument(
        "--idle-hours",
        type=float,
        default=WATCH_RECEIVING_STALE_SECONDS / 3600,
        metavar="HOURS",
        help="空会话判为作废的空闲阈值（缺省 6 小时，与 WATCH_RECEIVING_STALE_SECONDS 同口径）。",
    )
    args = parser.parse_args()
    if not args.root.is_dir():
        print(f"会话目录不存在：{args.root}", file=sys.stderr)
        return 1
    store = WatchSessionStore(args.root)
    report = store.finalize_superseded_sessions(
        apply=args.apply, session_ids=args.session
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    by_status: dict[str, int] = {}
    minutes: dict[str, float] = {}
    for item in report:
        status = str(item["status"])
        by_status[status] = by_status.get(status, 0) + 1
        minutes[status] = minutes.get(status, 0.0) + item["received_duration_ms"] / 60_000
    print("\n汇总：" + "，".join(
        f"{status} {count} 个 / {minutes[status]:.1f} 分钟"
        for status, count in sorted(by_status.items())
    ))
    if args.abandon_empty:
        abandoned = store.abandon_empty_sessions(
            apply=args.apply,
            idle_seconds=args.idle_hours * 3600,
            session_ids=args.session,
        )
        print("\n空会话作废：")
        print(json.dumps(abandoned, ensure_ascii=False, indent=2))
        if not abandoned:
            print("没有可作废的空会话。")
    if not args.apply:
        print("这是 dry-run，没有改动任何会话；确认后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
