"""Launch the Life Logger localhost web UI."""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from life_logger.local_web import serve  # noqa: E402

LOCAL_FEATURE_MODULES = (
    "life_logger.local_reminders",
    "life_logger.patterns",
)


def _preload_all_modules() -> None:
    """[2026-11-07] 启动即把所有生产模块导入一遍，杜绝「改盘未重启 → 新旧代码混跑」。

    背景：请求处理里有大量函数内延迟 import，磁盘代码改了而服务没重启时，
    新请求会把**新版**模块读进**旧版**进程（已崩过两次：`nutrition_for_item(... count=)`
    与 `FoodNutrition.estimate_basis`）。预加载后，延迟 import 只会命中已加载版本。
    失败不影响启动 —— 与之前的行为一致。
    """
    try:
        from life_logger.preload import format_startup_line, preload_modules

        report = preload_modules()
        print(format_startup_line(report), flush=True)
        if report["failed"]:
            print(
                "[preload] 以下模块预加载失败（服务继续启动，用到时才会真正暴露）："
                + "; ".join(item["module"] for item in report["failed_modules"]),
                flush=True,
            )
    except Exception as error:  # noqa: BLE001 - 预加载是增强，绝不能挡住服务
        print(f"[preload] 预加载整体失败（已忽略，服务继续启动）: {type(error).__name__}: {error}",
              flush=True)


def _preflight_local_features() -> None:
    failures: list[str] = []
    for module_name in LOCAL_FEATURE_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as error:
            failures.append(f"{module_name}: {error}")
    if failures:
        raise RuntimeError(
            "Life Logger 本地功能模块预检失败：" + "; ".join(failures)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        default=None,
        help="override bind host; otherwise preferences choose localhost/LAN",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=ROOT / "local-config.json")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--processing-mode", choices=("combined", "split"), default="combined",
                        help="split keeps the combined read path and only moves background work to the worker")
    parser.add_argument("--no-worker-start", action="store_true", help="use an independently managed worker")
    args = parser.parse_args()
    _preflight_local_features()
    print(
        "Life Logger 本地模块预检通过：reminders / patterns / mobile shell",
        flush=True,
    )
    _preload_all_modules()
    serve(
        host=args.host,
        port=args.port,
        config_path=args.config,
        open_browser=not args.no_browser,
        processing_mode=args.processing_mode,
        manage_worker=not args.no_worker_start,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
