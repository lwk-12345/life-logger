"""Q479 扫出的 3 处「真·时区缺陷」回归：都属「决定这是哪一天」且跟随宿主时区。

对应派单（2026-11-02 10:44）：
  1. ``src/life_logger/decision_comparison.py`` 用 mtime 的**宿主本地日**判断对比产物属于哪天
  2. ``scripts/watch_airpassport.py`` 用**宿主本地 00:00** 做步数/健康业务日窗口起点
  3. ``scripts/gate_runner.py`` 用 ``date.today()``（宿主本地）取「昨天」当门禁基线

用户要求：Live Logger **绑定死北京时间**，Mac 时区可能被改成国外。
下面每个用例都在 ``America/Los_Angeles`` 与 ``Asia/Shanghai`` 下各跑一遍，
断言「同一个 instant 必须归到同一个北京日」。

注意：这些测试**不 monkeypatch 模块里的 datetime**，而是真的改 ``TZ`` 环境变量 +
``time.tzset()``，走 C 运行时的本地时区 —— 这样才能真正复现「Mac 换时区」。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from life_logger.decision_history import collect_comparisons
from life_logger.timeutil import LOCAL_TZ

ROOT = Path(__file__).resolve().parents[1]
HOST_TZS = ("America/Los_Angeles", "Asia/Shanghai", "UTC")


@pytest.fixture(params=HOST_TZS)
def host_tz(request):
    """真的把进程本地时区切到 param（模拟用户把 Mac 改到国外）。"""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


_SCRIPT_CACHE: dict[str, object] = {}


def _load_script(name: str):
    """scripts/ 不是包，按文件路径加载。

    必须先把模块注册进 ``sys.modules``：脚本里的 ``@dataclass`` 会在 exec 期间
    通过 ``sys.modules[cls.__module__]`` 反查模块命名空间，未注册会抛
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    """
    if name in _SCRIPT_CACHE:
        return _SCRIPT_CACHE[name]
    path = ROOT / "scripts" / f"{name}.py"
    mod_name = f"_q479_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    _SCRIPT_CACHE[name] = module
    return module


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 1：decision_comparison 用文件 mtime 的宿主本地日做过滤
# ══════════════════════════════════════════════════════════════════════════
def test_comparison_mtime_filters_by_beijing_day(tmp_path, host_tz):
    """产物 mtime = 2026-11-02T00:30+08:00（= 11-01T16:30Z）。

    语义：这份对比属于**北京 11-02**。Mac 在 UTC-8 时该 mtime 的本地日是 11-01，
    查询 11-02 会看不到 —— 这就是要修的反例。

    路径里**故意不含日期**，强制走 mtime 分支。
    """
    artifact = tmp_path / "jobs" / "job-a" / "files" / "file-a" / "result" / "decision-comparison.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}", encoding="utf-8")
    stamp = datetime(2026, 11, 2, 0, 30, tzinfo=LOCAL_TZ).timestamp()
    os.utime(artifact, (stamp, stamp))

    result = collect_comparisons(tmp_path, date="2026-11-02")
    assert len(result["items"]) == 1, (
        f"TZ={host_tz} 下北京 11-02 的产物被漏掉了（本地日判断案）"
    )

    # 反向：北京 11-01 不该包含它
    other = collect_comparisons(tmp_path, date="2026-11-01")
    assert other["items"] == [], f"TZ={host_tz} 下被错归到 11-01"


def test_comparison_mtime_boundary_just_before_beijing_midnight(tmp_path, host_tz):
    """mtime = 2026-11-01T23:59:59+08:00 必须归 11-01，不能归 11-02。"""
    artifact = tmp_path / "watch-sessions" / "s1" / "w1" / "formal-asr" / "live-context" / "decision-comparison.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}", encoding="utf-8")
    stamp = datetime(2026, 11, 1, 23, 59, 59, tzinfo=LOCAL_TZ).timestamp()
    os.utime(artifact, (stamp, stamp))

    assert len(collect_comparisons(tmp_path, date="2026-11-01")["items"]) == 1
    assert collect_comparisons(tmp_path, date="2026-11-02")["items"] == []


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 2：watch_airpassport 用宿主本地 00:00 当业务日起点
# ══════════════════════════════════════════════════════════════════════════
def test_airpassport_day_start_is_beijing_midnight(host_tz):
    """``resolve_not_before`` 无 --backfill-from 时应返回**北京今天 00:00** 的秒。"""
    module = _load_script("watch_airpassport")
    args = SimpleNamespace(backfill_all=False, backfill_from=None)

    before = datetime.now(tz=LOCAL_TZ).date()
    got = module.resolve_not_before(args)
    after = datetime.now(tz=LOCAL_TZ).date()

    candidates = {
        datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ).timestamp()
        for d in (before, after)
    }
    assert got in candidates, (
        f"TZ={host_tz} 下业务日起点不是北京 00:00："
        f"got={datetime.fromtimestamp(got, tz=LOCAL_TZ)}"
    )


def test_airpassport_backfill_from_plain_date_is_beijing_midnight(host_tz):
    """``--backfill-from 2026-11-02``（裸日期）也必须按北京时间解释。"""
    module = _load_script("watch_airpassport")
    args = SimpleNamespace(backfill_all=False, backfill_from="2026-11-02")
    got = module.resolve_not_before(args)
    expected = datetime(2026, 11, 2, tzinfo=LOCAL_TZ).timestamp()
    assert got == expected, f"TZ={host_tz} 下裸日期不是按北京解释"


# ══════════════════════════════════════════════════════════════════════════
# 缺陷 3：gate_runner 用宿主本地 date.today() 取「昨天」
# ══════════════════════════════════════════════════════════════════════════
def test_gate_ledger_detail_yesterday_is_beijing(host_tz, monkeypatch):
    """门禁对账的 ``--to`` 必须是**北京昨天**。"""
    module = _load_script("gate_runner")
    captured: dict[str, list[str]] = {}

    class _Proc:
        stdout = "{}"
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return _Proc()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    before = datetime.now(tz=LOCAL_TZ).date()
    module.ledger_detail(Path("/repo"), "/usr/bin/python3", "", timeout=5)
    after = datetime.now(tz=LOCAL_TZ).date()

    argv = captured.get("argv") or []
    assert "--to" in argv, f"未捕获到 --to：{argv}"
    got = argv[argv.index("--to") + 1]
    allowed = {(d - timedelta(days=1)).isoformat() for d in (before, after)}
    assert got in allowed, (
        f"TZ={host_tz} 下门禁「昨天」不是北京昨天：got={got}, 期望 ∈ {sorted(allowed)}"
    )
