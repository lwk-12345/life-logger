"""活动时间线的计数由后端给出（2026-11-07）。

前端原来在 `web/waterfall-v2.html::clocksHtml()` 里自己数：

```js
const count = D.acts.length;                                                            // 全部
const live  = D.acts.filter(x => x.life === "live" && !x.a.looks_like_fragment).length;  // 我的生活
```

本文件验证后端 `read_day(for_display=True)["counts"]` 与这段 JS **逐位一致**，
并用 2026-11-06 的真实日文件（只读）跑一次比对。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger.activity_timeline_runtime import (
    COUNT_LIFECYCLES,
    ActivityTimelineRuntime,
    activity_counts,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "local-web"
REAL_DAY = "2026-11-06"


# --------------------------------------------------------------------------
# 前端算法的 Python 复刻（逐条对应 shape() + clocksHtml()）
# --------------------------------------------------------------------------
def frontend_counts(activities):
    """`shape()` 的 lifecycle 归一 + `clocksHtml()` 的两个 filter。"""
    acts = []
    for a in activities or []:
        life = a.get("lifecycle") if a.get("lifecycle") in ("live", "media", "unknown") else "unknown"
        acts.append((life, a))
    all_ = len(acts)
    live = sum(1 for life, _ in acts if life == "live")
    media = sum(1 for life, _ in acts if life == "media")
    unknown = sum(1 for life, _ in acts if life == "unknown")
    # JS `!x.a.looks_like_fragment`：缺失/false/0/"" 都算通过，只有 true 排除
    my_life = sum(1 for life, a in acts if life == "live" and not a.get("looks_like_fragment"))
    return {"all": all_, "live": live, "media": media, "unknown": unknown, "my_life": my_life}


def _act(**kw):
    base = {"activity_id": "a", "title": "x", "category": "生活·日常"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# 1) 纯函数：与前端逻辑逐条对照
# --------------------------------------------------------------------------
def test_lifecycle_normalised_like_frontend():
    acts = [
        _act(lifecycle="live"),
        _act(lifecycle="media"),
        _act(lifecycle="unknown"),
        _act(lifecycle=None),          # 字段缺失 → unknown
        _act(lifecycle="LIVE"),        # 大小写不符 → unknown（前端 includes 是严格比较）
        _act(lifecycle="bogus"),       # 乱值 → unknown
    ]
    counts = activity_counts(acts)
    assert counts["all"] == 6
    assert counts["live"] == 1
    assert counts["media"] == 1
    assert counts["unknown"] == 4
    assert counts == {**counts, **frontend_counts(acts)}, counts


@pytest.mark.parametrize("flag,expected_my_life", [
    (None, 3),        # 字段缺失 → JS `!undefined` = true → 计入
    (False, 3),
    (0, 3),
    ("", 3),
    (True, 2),        # 唯一被排除的值
])
def test_my_life_fragment_truthiness_matches_js(flag, expected_my_life):
    acts = [
        _act(activity_id="1", lifecycle="live"),                          # 无该字段
        _act(activity_id="2", lifecycle="live", looks_like_fragment=flag),
        _act(activity_id="3", lifecycle="live", looks_like_fragment=False),
        _act(activity_id="4", lifecycle="media", looks_like_fragment=False),   # media 不算我的生活
        _act(activity_id="5", lifecycle="unknown"),
    ]
    counts = activity_counts(acts)
    assert counts["live"] == 3
    assert counts["my_life"] == expected_my_life, counts
    assert counts["my_life"] == frontend_counts(acts)["my_life"]


def test_by_category_sorted_and_uncategorised():
    acts = [
        _act(category="生活·日常"),
        _act(category="生活·日常"),
        _act(category="浏览·视频"),
        _act(category=None),
        _act(category="   "),
        {"activity_id": "no-category"},
    ]
    counts = activity_counts(acts)
    assert counts["by_category"] == {"生活·日常": 2, "浏览·视频": 1, "未分类": 3}
    # 排序：先按数量降序，再按名字升序
    assert list(counts["by_category"]) == ["未分类", "生活·日常", "浏览·视频"]
    assert sum(counts["by_category"].values()) == counts["all"]


def test_empty_and_garbage_input():
    empty = activity_counts([])
    assert empty["all"] == 0 and empty["live"] == 0 and empty["media"] == 0
    assert empty["my_life"] == 0 and empty["by_category"] == {}

    assert activity_counts(None)["all"] == 0
    # 非 Mapping 的元素被跳过，不炸
    counts = activity_counts([_act(lifecycle="live"), "not-a-dict", 42])
    assert counts["all"] == 1 and counts["live"] == 1


def test_total_alias_matches_all():
    """`compare.py` 取的是 `counts.total`，两边必须同值。"""
    counts = activity_counts([_act(lifecycle="live"), _act(lifecycle="media")])
    assert counts["total"] == counts["all"] == 2


def test_basis_is_self_describing():
    counts = activity_counts([_act(lifecycle="live")])
    basis = counts["basis"]
    assert basis["lifecycle"] == list(COUNT_LIFECYCLES)
    assert basis["source"] == "backend"
    assert basis["by_category_field"] == "activity.category"
    # 没有传转写行 → 无法现算碎片判定，必须**如实**标 False（不能假装算过）
    assert basis["fragment_assessed"] is False


# --------------------------------------------------------------------------
# 2) 真实日文件（只读）：后端 counts == 前端算法
# --------------------------------------------------------------------------
@pytest.mark.skipif(not (OUTPUT / "activity-timeline" / "jev" / f"{REAL_DAY}.json").is_file(),
                    reason="本机没有 2026-11-06 的活动文件")
def test_real_20261106_counts_match_frontend_algorithm():
    runtime = ActivityTimelineRuntime(OUTPUT, ROOT / "local-config.json")
    payload = runtime.read_day(REAL_DAY, for_display=True)
    counts = payload.get("counts")
    assert isinstance(counts, dict), "read_day(for_display=True) 必须带 counts"

    # 前端在拿到 payload 之后还会跑 `project_for_read()` 补 looks_like_fragment；
    # 这里照做，然后按 clocksHtml 的写法自己再数一遍。
    from life_logger.activity_fragments import project_for_read

    rendered = project_for_read(payload, OUTPUT, REAL_DAY)
    expected = frontend_counts(rendered.get("activities") or [])

    for key in ("all", "live", "media", "unknown", "my_life"):
        assert counts[key] == expected[key], (key, counts[key], expected[key], counts)

    assert counts["basis"]["fragment_assessed"] is True, counts["basis"]
    # 不变量：live + media + unknown == all
    assert counts["live"] + counts["media"] + counts["unknown"] == counts["all"]
    assert counts["my_life"] <= counts["live"]
    assert sum(counts["by_category"].values()) == counts["all"]


@pytest.mark.skipif(not (OUTPUT / "activity-timeline" / "jev" / f"{REAL_DAY}.json").is_file(),
                    reason="本机没有 2026-11-06 的活动文件")
def test_counts_are_display_only_and_never_persisted():
    """写路径（for_display=False）不带 counts ⇒ 不会写进日文件。"""
    runtime = ActivityTimelineRuntime(OUTPUT, ROOT / "local-config.json")
    disk = json.loads((OUTPUT / "activity-timeline" / "jev" / f"{REAL_DAY}.json").read_text(encoding="utf-8"))
    assert "counts" not in disk, "日文件里不允许出现 counts（只读投影字段）"

    raw = runtime.read_day(REAL_DAY, for_display=False)
    assert "counts" not in raw, "for_display=False 是写路径快照，不能带展示计数"

    shown = runtime.read_day(REAL_DAY, for_display=True)
    assert "counts" in shown
    # 读数前后磁盘字节级不变
    again = json.loads((OUTPUT / "activity-timeline" / "jev" / f"{REAL_DAY}.json").read_text(encoding="utf-8"))
    assert again == disk, "read_day(for_display=True) 绝不能改动日文件"


@pytest.mark.skipif(not (OUTPUT / "activity-timeline" / "jev" / f"{REAL_DAY}.json").is_file(),
                    reason="本机没有 2026-11-06 的活动文件")
def test_missing_day_still_has_zero_counts(tmp_path: Path):
    """「还没生成」的一天也要有 counts（全 0），前端不用分两条路径。"""
    runtime = ActivityTimelineRuntime(tmp_path, tmp_path / "config.json")
    payload = runtime.read_day("2026-11-09", for_display=True)
    assert payload["read_state"] == "no_file"
    counts = payload["counts"]
    assert (counts["all"], counts["live"], counts["media"], counts["my_life"]) == (0, 0, 0, 0)
    assert counts["by_category"] == {}
