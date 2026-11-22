"""UTC+8 时间代码回归：9 处已确认站点的行为断言。

背景（`work/audit-utc8-20261104/`）：项目统一 UTC+8，但机器现在在
America/Los_Angeles（UTC-8）。本文件在 **TZ=America/Los_Angeles** 下运行，
锁住"结果不随系统时区变化"这件事。

覆盖：
  1  scripts/verify_all.sh:79            昨天（回退分支）
  2  scripts/verify_daily_experience.py  默认考察日
  3  scripts/check_landed_vs_live.py     --date 默认值
  4  scripts/post_restart_check.sh:438   兜底日期
  5  scripts/release_tag.sh:38           发布 tag 日期
  6  activity_timeline_runtime._parse_clock   无时区钟点
  7-8 version.deployed_at               /api/version 里的部署时间
  9  voice_triggers.captured_at         即时抓取记录时间

写法约定：
  * 环境无关 —— 断言的是"北京日期/半天偏移"，不是某个写死日期；
  * shell 站点既断言**结构**（命令里必须带 `TZ=Asia/Shanghai` / `hours=8`），
    也**真跑**一遍取结果；
  * 只用 .venv/bin/python 的标准库，不联网、不写盘。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))


def beijing_today() -> date:
    return datetime.now(BEIJING).date()


def beijing_yesterday_iso() -> str:
    return (beijing_today() - timedelta(days=1)).isoformat()


def run_under_la(cmd: str) -> str:
    """在 TZ=America/Los_Angeles 下跑一条 shell 命令（模拟出问题的机器）。"""
    env = dict(os.environ)
    env["TZ"] = "America/Los_Angeles"
    proc = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, env=env, cwd=ROOT)
    assert proc.returncode == 0, f"命令失败：{cmd}\n{proc.stderr}"
    return proc.stdout.strip()


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 1. verify_all.sh:79 的 python 回退分支 ────────────────────────────────────
def test_verify_all_yesterday_fallback_is_beijing():
    line = next((ln for ln in read("scripts/verify_all.sh").splitlines()
                 if ln.startswith("YESTERDAY=")), None)
    assert line, "scripts/verify_all.sh 里找不到 YESTERDAY 赋值"
    match = re.search(r'python3 -c "([^"]+)"', line)
    assert match, "找不到 python 回退分支（只有 BSD date 那支）"
    code = match.group(1)
    assert "Asia/Shanghai" in code or "hours=8" in code, (
        f"回退分支没有显式北京时区，会跟随系统时区：{code}"
    )
    assert run_under_la(f'python3 -c "{code}"') == beijing_yesterday_iso()


# ── 2. verify_daily_experience 默认考察日 ────────────────────────────────────
def test_verify_daily_experience_default_day_is_beijing():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import verify_daily_experience as mod  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    assert mod.default_today().isoformat() == beijing_today().isoformat()


# ── 3. check_landed_vs_live --date 默认值 ────────────────────────────────────
def test_check_landed_vs_live_default_date_is_beijing():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_landed_vs_live as mod  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    assert mod.default_probe_date() == beijing_today().isoformat()


# ── 4. post_restart_check.sh:438 兜底日期 ────────────────────────────────────
def test_post_restart_check_fallback_date_is_beijing():
    line = next((ln for ln in read("scripts/post_restart_check.sh").splitlines()
                 if 'doc.get("date") or' in ln), None)
    assert line, "找不到 post_restart_check.sh 里的兜底日期"
    expr = line.split('doc.get("date") or', 1)[1].strip()
    assert "hours=8" in expr or "Asia/Shanghai" in expr, (
        f"兜底日期没有显式北京时区：{expr}"
    )
    got = run_under_la(f'python3 -c "import datetime; print({expr})"')
    assert got == beijing_today().isoformat()


# ── 5. release_tag.sh 的 TODAY ───────────────────────────────────────────────
def test_release_tag_today_is_beijing():
    line = next((ln for ln in read("scripts/release_tag.sh").splitlines()
                 if ln.startswith("TODAY=")), None)
    assert line, "找不到 release_tag.sh 的 TODAY"
    match = re.search(r"\$\((.*)\)", line)
    assert match, f"TODAY 不是命令替换：{line}"
    cmd = match.group(1)
    assert "TZ=Asia/Shanghai" in cmd, f"TODAY 没指定时区，会跟随系统时区：{cmd}"
    assert run_under_la(cmd) == beijing_today().strftime("%Y.%m.%d")


# ── 6. _parse_clock：无时区钟点按 +08:00 解释 ────────────────────────────────
def test_parse_clock_naive_is_interpreted_as_beijing():
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from life_logger.activity_timeline_runtime import _parse_clock  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    parsed = _parse_clock("2026-11-02T10:36:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=8), f"偏移应为 +08:00，实际 {parsed.utcoffset()}"
    assert (parsed.hour, parsed.minute) == (10, 36), "钟点本身不能被改写"


def test_parse_clock_keeps_explicit_offset():
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from life_logger.activity_timeline_runtime import _parse_clock  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    parsed = _parse_clock("2026-11-02T10:36:00+00:00")
    assert parsed is not None and parsed.utcoffset() == timedelta(0)


# ── 7-8. version.deployed_at 输出 +08:00 ─────────────────────────────────────
def test_version_deployed_at_is_beijing():
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from life_logger.version import deployed_at  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    text = deployed_at(1762200000)
    assert text.endswith("+08:00"), f"应为 +08:00，实际 {text}"
    assert datetime.fromisoformat(text).utcoffset() == timedelta(hours=8)


# ── 9. voice_triggers.captured_at 带 +08:00 ─────────────────────────────────
def test_voice_trigger_captured_at_has_beijing_offset():
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from life_logger.voice_triggers import _scan_voice_triggers  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    captures = _scan_voice_triggers(
        "记一下买牛奶", source_label="self", acoustic_proximity="near"
    )
    assert captures, "测试前提不成立：这条口令没有产出捕获记录"
    captured_at = captures[0].captured_at
    parsed = datetime.fromisoformat(captured_at)
    assert parsed.tzinfo is not None, f"captured_at 没有时区偏移（裸墙上时间）：{captured_at}"
    assert parsed.utcoffset() == timedelta(hours=8), f"应为 +08:00，实际 {captured_at}"
