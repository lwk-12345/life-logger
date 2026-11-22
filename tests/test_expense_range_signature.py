"""F2-S1：日期指纹必须对"job 级输入"敏感（2026-10-24 收口）。

缺陷背景：`source_fingerprint` 用 `parts[-4]` 从
`jobs/<job>/files/<hash>/result/<date>` 取 job id，实际取到的是字面量 `files`，
于是 `jobs/<job>/manual-events.json` 等 job 级输入永远进不了单日指纹，
人工事件 / 复核派生 / 复核决议 / 事件更正改了以后单日缓存仍命中旧值。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger.day_views import GLOBAL_FILES, SOURCE_PATTERNS, source_fingerprint  # noqa: E402

JOB_LEVEL_FILES = (
    "manual-events.json",
    "review-derived-events.json",
    "review-decisions.json",
    "event-corrections.json",
)


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _touch_change(path: Path) -> None:
    """改内容（变长）+ 保证 mtime_ns 变化，让指纹一定不同。"""
    time.sleep(0.002)
    _write(path, path.read_text(encoding="utf-8") + " ")


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "local-web"
    for job, day in (("job-A", "2026-10-22"), ("job-A", "2026-10-23"), ("job-B", "2026-10-19")):
        _write(root / "jobs" / job / "files" / f"hash-{job}-{day}" / "result" / day / "events.json", "{}")
        _write(
            root / "jobs" / job / "files" / f"hash-{job}-{day}" / "result" / day / "daily_summary.json",
            "{}",
        )
    for job in ("job-A", "job-B"):
        for name in JOB_LEVEL_FILES + ("status.json",):
            _write(root / "jobs" / job / name, "[]")
    _write(root / "task-states.json", "{}")
    _write(root / "preferences.json", "{}")
    return root


def test_source_patterns_list_is_single_source_of_truth() -> None:
    """清单必须仍集中在一处，且包含 job 级输入与全局输入。"""
    assert "jobs/job-*/manual-events.json" in SOURCE_PATTERNS
    assert "jobs/job-*/review-derived-events.json" in SOURCE_PATTERNS
    assert GLOBAL_FILES <= {"task-states.json", "preferences.json", "ledger-review.json", "@config", "@build"}


@pytest.mark.parametrize("name", JOB_LEVEL_FILES)
def test_job_level_input_change_invalidates_single_day(tmp_path: Path, name: str) -> None:
    root = _make_root(tmp_path)
    before = source_fingerprint(root, ["2026-10-22"])
    _touch_change(root / "jobs" / "job-A" / name)
    after = source_fingerprint(root, ["2026-10-22"])
    assert after != before, f"{name} 变化后单日指纹未变（job id 索引仍有缺陷）"


@pytest.mark.parametrize("name", JOB_LEVEL_FILES)
def test_other_job_level_input_does_not_affect_unrelated_day(tmp_path: Path, name: str) -> None:
    root = _make_root(tmp_path)
    before = source_fingerprint(root, ["2026-10-22"])
    _touch_change(root / "jobs" / "job-B" / name)  # job-B 只有 2026-10-19
    after = source_fingerprint(root, ["2026-10-22"])
    assert after == before


def test_other_day_result_does_not_affect_single_day_but_does_affect_range(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    day_before = source_fingerprint(root, ["2026-10-22"])
    range_before = source_fingerprint(root, ["2026-10-22", "2026-10-23"])
    _touch_change(root / "jobs" / "job-A" / "files" / "hash-job-A-2026-10-23" / "result" / "2026-10-23" / "events.json")
    assert source_fingerprint(root, ["2026-10-22"]) == day_before
    assert source_fingerprint(root, ["2026-10-22", "2026-10-23"]) != range_before


def test_range_fingerprint_covers_each_member_day(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    before = source_fingerprint(root, ["2026-10-22", "2026-10-23"])
    _touch_change(root / "jobs" / "job-A" / "manual-events.json")
    after = source_fingerprint(root, ["2026-10-22", "2026-10-23"])
    assert after != before, "区间指纹未覆盖成员日期的 job 级输入"


def test_global_fingerprint_without_dates_sees_everything(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    before = source_fingerprint(root)
    _touch_change(root / "jobs" / "job-B" / "manual-events.json")
    assert source_fingerprint(root) != before


def test_same_inputs_give_stable_fingerprint(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    assert source_fingerprint(root, ["2026-10-22"]) == source_fingerprint(root, ["2026-10-22"])
    assert source_fingerprint(root, ["2026-10-22"]) != source_fingerprint(root, ["2026-10-23"])


def test_range_signature_delegates_to_source_fingerprint(tmp_path: Path, monkeypatch) -> None:
    """`local_web._expense_range_signature` 必须按区间展开日期后调用同一函数。"""
    from life_logger import local_web
    import life_logger.day_views as day_views

    root = _make_root(tmp_path)
    calls: list = []
    real = day_views.source_fingerprint

    def spy(root_arg, dates=None):
        calls.append(list(dates or []))
        return real(root_arg, dates)

    monkeypatch.setattr(day_views, "source_fingerprint", spy)
    signature = local_web._expense_range_signature(root, "2026-10-22", "2026-10-23", "provisional", "display")
    assert calls and calls[0] == ["2026-10-22", "2026-10-23"]
    assert isinstance(signature, str) and len(signature) == 16
    _touch_change(root / "jobs" / "job-A" / "manual-events.json")
    assert local_web._expense_range_signature(
        root, "2026-10-22", "2026-10-23", "provisional", "display"
    ) != signature


def test_fingerprint_for_a_century_is_fast_and_equals_listing_days(tmp_path: Path, monkeypatch) -> None:
    """搜索传 2000-2099 共 36525 天：不能每天做一次 glob，否则接口要跑十几分钟。"""
    import datetime as dt
    import pathlib
    import time

    root = _make_root(tmp_path)
    days = [(dt.date(2000, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(36525)]
    calls = []
    original = pathlib.Path.glob
    monkeypatch.setattr(pathlib.Path, "glob", lambda self, pattern, **kw: (calls.append(pattern), original(self, pattern, **kw))[1])
    started = time.monotonic()
    wide = source_fingerprint(root, days)
    assert time.monotonic() - started < 5
    assert len(calls) < 50
    assert wide == source_fingerprint(root, days[:])


def test_fingerprint_filters_jobs_and_sessions_the_same_way(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    assert source_fingerprint(root, ["2026-10-22"]) != source_fingerprint(root, ["2026-10-19"])
    assert source_fingerprint(root, ["2026-10-22", "2026-10-19"]) not in {
        source_fingerprint(root, ["2026-10-22"]), source_fingerprint(root, ["2026-10-19"]),
    }


def test_manual_only_job_invalidates_its_day(tmp_path: Path) -> None:
    """只有手动补录的 job（jobs/<job>/<date>/events.json，没有 files/）也要进单日指纹。"""
    root = _make_root(tmp_path)
    manual = root / "jobs" / "job-manual" / "manual-events.json"
    _write(manual, "[]")
    _write(root / "jobs" / "job-manual" / "2026-11-05" / "events.json", "{}")
    before = source_fingerprint(root, ["2026-11-05"])
    _touch_change(manual)
    assert source_fingerprint(root, ["2026-11-05"]) != before
    other = source_fingerprint(root, ["2026-10-22"])
    _touch_change(manual)
    assert source_fingerprint(root, ["2026-10-22"]) == other
