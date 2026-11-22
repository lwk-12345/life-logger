"""Q853 首页状态提示回归：内部英文不外露 / 提示最多一条 / 日角标口径统一。

设计成**纯静态断言**（不启浏览器、不连服务），因为它守的是"代码里有没有这条规矩"，
浏览器行为另有 evidence/probe_baseline.py 的 Playwright 证据。

可用 APP_JS 环境变量指向任意版本的 app.js（用来演示"改前红"）：
    APP_JS=work/.../backup/app.js pytest -q tests/test_home_status_20261104.py   # 红
    pytest -q tests/test_home_status_20261104.py                                 # 绿
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = Path(os.environ.get("APP_JS") or (ROOT / "web" / "app.js"))


@pytest.fixture(scope="module")
def app_source() -> str:
    assert APP.is_file(), f"app.js 不存在: {APP}"
    return APP.read_text(encoding="utf-8")


def _slice(src: str, start: str, end_markers: tuple[str, ...]) -> str:
    i = src.index(start)
    j = min((src.index(m, i) for m in end_markers if m in src[i:]), default=len(src))
    return src[i:j]


# ---------------------------------------------------------------- ① 内部英文不外露
def test_internal_english_reason_is_translated_not_rendered(app_source: str) -> None:
    """`read producer exceeded deadline…` 必须有中文映射，且原文不得直接进可见文本。"""
    assert "humanizeCanonicalError" in app_source, "缺少内部原因翻译函数"
    assert "read producer exceeded deadline" in app_source.lower(), "映射表里没有该内部原因"
    human = _slice(app_source, "const CANONICAL_ERROR_HUMAN", ("function journalLoadState",))
    assert "后台整理超时" in human, "缺少对应的中文文案"

    # 渲染点必须用 humanize 之后的值，原文只允许出现在 title
    block = _slice(app_source, "const status = day.canonicalError", ("const previousFrame",))
    assert "escapeHtml(day.canonicalError)" in block, "状态块应渲染已翻译的 canonicalError"
    assert 'title="${escapeHtml(day.canonicalErrorRaw)}"' in block, "原文应放进 title 供排查"


# ---------------------------------------------------------------- ② 提示最多一条
def test_switch_banner_is_suppressed_when_status_block_renders(app_source: str) -> None:
    """状态块与"正在切换到…"横幅说的是同一件事，不得同时出现。"""
    block = _slice(app_source, "const markup = `<div class=\"journal-day-header\">", ("const canReuseFrame",))
    assert "daySwitchBannerMarkup(day.date, false)" in block, (
        "同一分支里既有状态块又有切换横幅 → 会出现两条提示两个按钮"
    )


# ---------------------------------------------------------------- ③ 日角标口径
def test_day_badge_never_presents_event_count_as_activity_count(app_source: str) -> None:
    """活动数只有一个来源；未加载时不报活动数，事件条数必须写明是事件。"""
    block = _slice(app_source, "// [2026-11-04 Q853] 口径统一", ("const summary = day",))
    assert "个活动" in block, "活动数应来自 activityTimeline"
    assert "day.activityTimeline.activities.length" in block
    assert "条事件" in block


def test_activity_count_source_comment_is_present(app_source: str) -> None:
    """口径来源要写清楚，避免下一个人再把两条轨道混用。"""
    block = _slice(app_source, "// [2026-11-04 Q853] 口径统一", ("const summary = day",))
    assert "activity-timeline" in block, "应注释说明活动数来自 /api/activity-timeline"
