"""capture_funnel 的口径测试。

这些断言存在的理由：这个数字会显示在日记页面上给用户看。
口径错了就是误导，比没有更糟。所以每条规则都要有一条测试盯着。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from life_logger.capture_funnel import (
    clear_cache,
    compute_capture_funnel,
    count_said,
)


def _write_transcript(path: Path, texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"text": t, "absolute_started_at": f"2026-10-15T10:00:{i:02d}+08:00"}
        for i, t in enumerate(texts)
    ]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def test_counts_sentences_and_chars(tmp_path: Path) -> None:
    _write_transcript(
        tmp_path / "watch-sessions/dev1/sess1/formal-asr/segments/000001/2026-10-15/transcript.json",
        ["去健身房了", "卧推四十公斤"],
    )
    got = count_said(tmp_path, "2026-10-15")
    assert got["said_sentences"] == 2
    assert got["said_chars"] == len("去健身房了") + len("卧推四十公斤")
    assert got["said_segments"] == 1


def test_excludes_recovery_copies(tmp_path: Path) -> None:
    """recovery 目录是重跑副本，数进去会重复。"""
    real = tmp_path / "jobs/job-1/files/abc/result/2026-10-15/transcript.json"
    _write_transcript(real, ["一", "二", "三"])
    copy = tmp_path / "jobs/job-1/recovery/context-extraction/xyz/abc/result/2026-10-15/transcript.json"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")

    got = count_said(tmp_path, "2026-10-15")
    assert got["said_sentences"] == 3, "recovery 副本不该被重复计数"


def test_dedupes_identical_content(tmp_path: Path) -> None:
    """同一条录音被两个目录引用时，只算一次。"""
    a = tmp_path / "jobs/job-1/files/aaa/result/2026-10-15/asr/transcript.json"
    b = tmp_path / "jobs/job-1/files/aaa/result/2026-10-15/transcript.json"
    _write_transcript(a, ["同一段话"])
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(a.read_text(encoding="utf-8"), encoding="utf-8")

    got = count_said(tmp_path, "2026-10-15")
    assert got["said_sentences"] == 1


def test_junk_and_blank_not_counted(tmp_path: Path) -> None:
    _write_transcript(
        tmp_path / "watch-sessions/dev1/sess1/formal-asr/segments/000002/2026-10-15/transcript.json",
        ["真的话", "", "   ", "。", "…", "！"],
    )
    got = count_said(tmp_path, "2026-10-15")
    assert got["said_sentences"] == 1, "只有「真的话」算一句"


def test_only_target_date(tmp_path: Path) -> None:
    _write_transcript(
        tmp_path / "watch-sessions/dev1/sess1/formal-asr/segments/00001/2026-10-15/transcript.json",
        ["今天的话"],
    )
    _write_transcript(
        tmp_path / "watch-sessions/dev1/sess1/formal-asr/segments/00002/2026-10-16/transcript.json",
        ["明天的话", "还有一句"],
    )
    got = count_said(tmp_path, "2026-10-15")
    assert got["said_sentences"] == 1


def test_cache_returns_same_object_without_rescan(tmp_path: Path) -> None:
    _write_transcript(
        tmp_path / "watch-sessions/dev1/sess1/formal-asr/segments/00003/2026-10-15/transcript.json",
        ["第一句"],
    )
    first = compute_capture_funnel(tmp_path, "2026-10-15")
    assert first["cached"] is False

    # 缓存期内新增文件，不该被算进去（TTL 内的快照语义）
    _write_transcript(
        tmp_path / "watch-sessions/dev1/sess1/formal-asr/segments/00004/2026-10-15/transcript.json",
        ["第二句"],
    )
    second = compute_capture_funnel(tmp_path, "2026-10-15")
    assert second["cached"] is True
    assert second["said_sentences"] == 1

    # 关掉缓存就能看到最新的
    fresh = compute_capture_funnel(tmp_path, "2026-10-15", use_cache=False)
    assert fresh["said_sentences"] == 2


def test_missing_root_is_not_an_error(tmp_path: Path) -> None:
    """目录不存在时返回 0，不要抛异常——页面还得能显示。"""
    got = count_said(tmp_path / "nope", "2026-10-15")
    assert got["said_sentences"] == 0
