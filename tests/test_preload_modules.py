"""服务启动预加载：改盘后延迟 import 必须仍拿到**已加载的旧对象**（2026-11-07）。

事故背景：`local_web.py` 里大量函数内延迟 import。磁盘代码改了、服务没重启时，
新请求会把新版模块读进旧版进程，与已加载的旧模块混跑 —— 已崩两次
（`nutrition_for_item(..., count=)`、`FoodNutrition.estimate_basis`）。
预加载的全部价值就是这一条：**模块对象在进程生命周期内固定**。
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from life_logger.preload import (
    PRELOAD_EXCLUDE,
    format_startup_line,
    is_excluded,
    iter_module_names,
    preload_modules,
    rss_mb,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def fake_pkg(tmp_path: Path):
    """一个临时包：`impl.VALUE = 1`，`reader.read()` 在函数内延迟 import。"""
    pkg = tmp_path / "fakepreload"
    _write(pkg / "__init__.py", "")
    _write(pkg / "impl.py", """
        VALUE = 1
        def describe():
            return "v1"
    """)
    _write(pkg / "reader.py", """
        def read():
            # 函数内延迟 import —— 与 local_web 里的写法同型
            from fakepreload.impl import VALUE
            return VALUE
    """)
    sys.path.insert(0, str(tmp_path))
    try:
        yield pkg
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n == "fakepreload" or n.startswith("fakepreload.")]:
            sys.modules.pop(name, None)


def test_preload_freezes_modules_even_after_disk_changes(fake_pkg: Path):
    """核心用例：预加载后改磁盘文件，函数内 import 仍返回**旧对象**。"""
    report = preload_modules(package="fakepreload")
    assert report["failed"] == 0, report["failed_modules"]
    assert report["loaded"] >= 2

    import fakepreload.reader as reader
    assert reader.read() == 1

    # 磁盘改成 v2
    (fake_pkg / "impl.py").write_text("VALUE = 2\ndef describe():\n    return 'v2'\n",
                                      encoding="utf-8")

    # 进程里仍然是旧对象（这正是"改盘未重启不会混跑"的保证）
    assert reader.read() == 1, "预加载后函数内延迟 import 必须命中已加载模块"
    import fakepreload.impl as impl
    assert impl.VALUE == 1 and impl.describe() == "v1"
    import fakepreload.impl as impl_again
    assert impl_again is impl, "同一模块名必须始终是同一个对象"

    # 反证：把模块从 sys.modules 摘掉再导入，就能看到磁盘上的 v2
    for name in [n for n in sys.modules if n == "fakepreload" or n.startswith("fakepreload.")]:
        sys.modules.pop(name, None)
    import fakepreload.impl as refreshed
    assert refreshed.VALUE == 2, "文件确实改了；上面的 1 是预加载冻结的结果"


def test_import_failure_is_reported_not_raised(tmp_path: Path):
    """导入会抛异常的模块：只记进 failed，绝不让调用方（服务启动）挂掉。"""
    pkg = tmp_path / "brokenpkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "ok.py", "FLAG = True\n")
    _write(pkg / "boom.py", "raise RuntimeError('模块级炸了')\n")
    sys.path.insert(0, str(tmp_path))
    try:
        report = preload_modules(package="brokenpkg")
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n == "brokenpkg" or n.startswith("brokenpkg.")]:
            sys.modules.pop(name, None)

    assert report["failed"] == 1
    assert report["failed_modules"][0]["module"] == "brokenpkg.boom"
    assert "RuntimeError" in report["failed_modules"][0]["error"]
    assert "brokenpkg.ok" in report["modules"], "一个模块炸了不能影响其它模块"
    assert "brokenpkg.boom" not in sys.modules


def test_excluded_modules_are_skipped(tmp_path: Path):
    pkg = tmp_path / "exclpkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "keep.py", "X = 1\n")
    _write(pkg / "skipme.py", "X = 2\n")
    sys.path.insert(0, str(tmp_path))
    try:
        report = preload_modules(package="exclpkg",
                                 exclude={"exclpkg.skipme": "测试排除"})
        assert report["skipped"] == 1
        assert report["skipped_modules"] == ["exclpkg.skipme"]
        assert "exclpkg.skipme" not in sys.modules
        assert "exclpkg.keep" in sys.modules
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n == "exclpkg" or n.startswith("exclpkg.")]:
            sys.modules.pop(name, None)


def test_real_package_excludes_only_entry_scripts():
    """真实包的排除表：只排除 CLI 与常驻子进程入口，且每个都有理由。"""
    assert set(PRELOAD_EXCLUDE) == {"life_logger.cli", "life_logger.asr_resident"}
    for module, reason in PRELOAD_EXCLUDE.items():
        assert reason.strip(), module
    assert is_excluded("life_logger.cli") == PRELOAD_EXCLUDE["life_logger.cli"]
    assert is_excluded("life_logger.food_nutrition") is None
    # 重依赖 adapters 不能排除：服务会在请求里调用它们（只是 spawn 到 funasr venv）
    for name in ("life_logger.moss_transcribe", "life_logger.vip_fast_track",
                 "life_logger.transcribe", "life_logger.campplus_voiceprint"):
        assert is_excluded(name) is None, name


def test_real_package_preload_covers_service_modules():
    """真实预加载：0 失败、覆盖 food_nutrition 这类曾引发混跑的模块。"""
    report = preload_modules()
    assert report["failed"] == 0, report["failed_modules"]
    assert report["loaded"] + report["already"] >= 100
    covered = set(report["modules"]) | {n for n in sys.modules if n.startswith("life_logger.")}
    for name in ("life_logger.food_nutrition", "life_logger.daily_cognitive_session",
                 "life_logger.boohee", "life_logger.web_routes_events",
                 "life_logger.moss_transcribe", "life_logger.vip_fast_track"):
        assert name in covered, f"{name} 必须被预加载（或早已在内存）"
    # 排除项看"报告里被跳过"，不看 sys.modules —— 别的用例可能顺手 import 过 cli
    assert "life_logger.cli" in report["skipped_modules"]
    assert "life_logger.asr_resident" in report["skipped_modules"]
    assert iter_module_names() == sorted(iter_module_names())


def test_report_and_startup_line_shape():
    report = preload_modules()
    for key in ("total", "loaded", "already", "skipped", "failed", "duration_s",
                "rss_before_mb", "rss_after_mb", "rss_delta_mb", "modules",
                "skipped_modules", "failed_modules"):
        assert key in report, key
    assert isinstance(report["duration_s"], float) and report["duration_s"] >= 0
    assert report["rss_after_mb"] >= report["rss_before_mb"] > 0
    assert rss_mb() > 0
    line = format_startup_line(report)
    assert line.startswith("[preload]")
    assert "耗时" in line and "RSS" in line
