#!/usr/bin/env python3
"""Run the independent Life Logger processor. Does not serve HTTP."""
from __future__ import annotations
import argparse
from pathlib import Path
import signal
import sys

ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'src'):
    if str(path) not in sys.path:sys.path.insert(0,str(path))


def _preload_all_modules():
    """[2026-11-07] 长驻进程同样预加载，避免「改盘未重启 → 新旧代码混跑」。

    与 `scripts/run_local_web.py::_preload_all_modules` 同一实现（`life_logger.preload`），
    worker 里也有大量函数内延迟 import（food_nutrition / daily_cognitive_session / ...）。
    失败只打警告，不影响 worker 起来。
    """
    try:
        from life_logger.preload import format_startup_line, preload_modules
        report = preload_modules()
        print(format_startup_line(report), flush=True)
        if report["failed"]:
            print("[preload] 预加载失败（继续启动）："
                  + "; ".join(i["module"] for i in report["failed_modules"]), flush=True)
    except Exception as error:
        print(f"[preload] 预加载整体失败（已忽略）: {type(error).__name__}: {error}", flush=True)


def main():
    _preload_all_modules()
    from life_logger.local_web import DEFAULT_JOB_ROOT
    from life_logger.processing_runtime import ProcessingWorker
    from life_logger.runtime_launch import refuse_legacy_processing
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=DEFAULT_JOB_ROOT)
    parser.add_argument('--config',type=Path,default=ROOT/'local-config.json')
    parser.add_argument('--no-background',action='store_true',help='isolated queue/read-model validation; no Watch/import/reminder loops')
    parser.add_argument('--no-projections',action='store_true',help='已废弃：读模型层已删除，保留该开关只为兼容 launchd 启动参数（无作用）')
    parser.add_argument('--backfill',action='store_true',help='已废弃：读模型层已删除，保留该开关只为兼容旧调用（无作用）')
    parser.add_argument('--backfill-dates',default='',help='已废弃（无作用）')
    parser.add_argument('--backfill-deadline',type=float,default=300.0,help='已废弃（无作用）')
    args=parser.parse_args()
    refuse_legacy_processing(args.root)
    worker=ProcessingWorker(args.root,args.config,background=not args.no_background)
    if args.backfill:
        # 读模型层已删除，预热无对象可做；
        # 保留开关只是为了不打断可能还在传 --backfill 的旧脚本。
        print('[processing-worker] --backfill 已废弃：读模型层已删除，本次不做任何预热', flush=True)
        return 0
    for number in (signal.SIGTERM,signal.SIGINT):
        signal.signal(number,lambda *_:worker.stop.set())
    print(f'Life Logger processing worker pid={__import__("os").getpid()} root={worker.root}',flush=True)
    worker.serve()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
