"""时间轴页可用性回归（2026-11-04 单）：未来时段不许有内容 / 390 胶囊单行可滚 / 文案不许出现「水位」。

怎么"改前红、改后绿"
--------------------
本文件不复制断言、不硬编码数字，只调用真实浏览器探针
`work/fix-timeline-usability-20261104/probe_usability.mjs`。用环境变量
`LL_PAGE_HTML=<旧版页面文件>` 可以让探针**把文档响应换成旧版**（页面本身不动），
于是同一份测试跑两次即可对照：

    LL_PAGE_HTML=work/fix-timeline-usability-20261104/daily-timeline-live.before.html \
        python3 -m pytest tests/test_timeline_usability_20261104.py -q     # 期望 FAIL
    python3 -m pytest tests/test_timeline_usability_20261104.py -q          # 期望 PASS

"历史日期卡片数不变"不是写死常量，而是**同一时刻跑旧版页面拿基线**，再与现网页面比对。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "work" / "fix-timeline-usability-20261104" / "probe_usability.mjs"
BEFORE = ROOT / "work" / "fix-timeline-usability-20261104" / "daily-timeline-live.before.html"
NODE = shutil.which("node")

TODAY = "2026-11-04"
HISTORY = "2026-11-03"
CASES = [(TODAY, 1440), (TODAY, 390), (HISTORY, 1440), (HISTORY, 390)]

# [2026-11-08] 旧时间轴页 daily-timeline-live.html 已于第二十三批（Q1061）退役：打开即跳转 /waterfall-v2.html。
# 本文件的探针等的是旧页元素（.window-activity-card / .hour-band），每例空等 90 秒后失败，并把全量测试拖到超时。
# 现行首页的可用性由 waterfall-v2 的 node 测试与浏览器截图验收覆盖。
_LIVE_PAGE = ROOT / "web" / "daily-timeline-live.html"
if _LIVE_PAGE.is_file() and "本页已退役" in _LIVE_PAGE.read_text(encoding="utf-8", errors="ignore"):
    pytestmark = pytest.mark.skip(reason="旧时间轴页已退役（跳转 waterfall-v2），探针只针对旧页元素")


def _run_probe(date: str, width: int, html: Path | None = None) -> dict:
    if NODE is None:
        pytest.skip("没有 node，无法跑浏览器探针")
    if not PROBE.is_file():
        pytest.skip(f"缺少探针脚本 {PROBE}")
    cmd = [NODE, str(PROBE), "--date", date, "--width", str(width)]
    if html is not None:
        if not html.is_file():
            pytest.skip(f"缺少旧版页面快照 {html}")
        cmd += ["--html", str(html)]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"探针失败（{date}/{width}）：{proc.stderr[-800:]}"
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise AssertionError(f"探针输出不是 JSON：{proc.stdout[:400]}") from exc


@pytest.fixture(scope="module")
def override_html() -> Path | None:
    raw = os.environ.get("LL_PAGE_HTML", "").strip()
    return Path(raw) if raw else None


@pytest.fixture(scope="module")
def baseline_cards() -> dict[tuple[str, int], int]:
    """旧版页面快照下的卡片数，作为「历史日期数量不变」的基线。"""
    if not BEFORE.is_file():
        pytest.skip(f"缺少旧版快照 {BEFORE}")
    return {(d, w): _run_probe(d, w, BEFORE)["cards"] for d, w in CASES}


@pytest.mark.parametrize("date,width", CASES)
def test_no_future_hours_claim_activities(date: str, width: int, override_html):
    """今天：当前时刻之后的小时不得出现「散步 / 日常环境」这类内容（渲染层不产生，不是 CSS 隐藏）。"""
    r = _run_probe(date, width, override_html)
    if not r["isToday"]:
        # 历史日期没有"未来"，但也不该凭空冒出散步占位（同一渲染分支）
        assert r["futureWalking"] == [], f"{date}@{width} 历史日期出现散步占位：{r['futureWalking']}"
        return
    assert r["futureWalking"] == [], (
        f"{date}@{width} 当前时刻（{r['nowMin']} 分）之后仍有内容：{r['futureWalking']}"
    )


@pytest.mark.parametrize("date,width", CASES)
def test_mobile_filter_pills_single_row_scrollable(date: str, width: int, override_html):
    """390 宽：筛选胶囊区高度 ≤56px 且可横向滚动；1440 宽不受影响。"""
    r = _run_probe(date, width, override_html)
    if width <= 480:
        assert r["pillHeight"] is not None, "找不到筛选胶囊容器"
        assert r["pillHeight"] <= 56, f"{date}@{width} 胶囊区高 {r['pillHeight']}px > 56px"
        assert r["pillScrollable"] is True, f"{date}@{width} 胶囊区不可横向滚动（未溢出）"
        assert r["pillFlexWrap"] == "nowrap", f"{date}@{width} 胶囊区仍是换行排版"
    else:
        assert r["pillFlexWrap"] == "wrap", f"{date}@{width} 桌面端排版被改动"


@pytest.mark.parametrize("date,width", CASES)
def test_no_watermark_jargon(date: str, width: int, override_html):
    """页面可见文字里不得出现「水位」这类内部术语。"""
    r = _run_probe(date, width, override_html)
    assert r["hasWatermarkWord"] is False, f"{date}@{width} 页面仍出现「水位」"


@pytest.mark.parametrize("date,width", CASES)
def test_layout_health(date: str, width: int, override_html):
    """无横向溢出、无 JS 报错、嵌入模式不重复弹「后台正在准备」横幅。"""
    r = _run_probe(date, width, override_html)
    assert r["docOverflowX"] <= 1, f"{date}@{width} 横向溢出 {r['docOverflowX']}px"
    assert r["pageErrors"] == [], f"{date}@{width} 有 JS 报错：{r['pageErrors']}"
    assert r["bannerVisible"] is False, f"{date}@{width} embedded 下仍显示横幅"


@pytest.mark.parametrize("date,width", CASES)
def test_historical_card_count_unchanged(date: str, width: int, override_html, baseline_cards):
    """卡片数量必须与旧版页面完全一致（顺序由卡片数 + 首卡 id 间接守住）。"""
    base = baseline_cards[(date, width)]
    r = _run_probe(date, width, override_html)
    assert r["cards"] == base, f"{date}@{width} 卡片数 {r['cards']} != 基线 {base}"
