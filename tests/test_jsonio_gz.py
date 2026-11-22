"""`jsonio` 的读取契约：x.json 优先 / x.json.gz 兜底 / 两者都在取未压缩。

四种情况（派单要求逐条覆盖）：
  1. 只有 x.json                     → 读未压缩
  2. 只有 x.json.gz                  → 解压读
  3. 两者都在                        → **必须**读未压缩（压缩只是省盘，不是新事实源）
  4. x.json.gz 损坏（不是 gzip / JSON 坏）→ 明确抛错，绝不返回半个对象
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from life_logger.jsonio import (
    glob_json,
    read_json,
    read_json_or,
    read_json_text,
    resolve_readable_json_path,
)


def _write_plain(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_gz(path: Path, payload) -> Path:
    target = path.with_name(path.name + ".gz")
    with gzip.open(target, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False))
    return target


# ---------------------------------------------------------------------------
# 1) 只有未压缩文件
# ---------------------------------------------------------------------------
def test_reads_plain_json_when_compressed_missing(tmp_path: Path) -> None:
    target = _write_plain(tmp_path / "daily_summary.json", {"date": "2026-11-02", "event_count": 3})

    assert resolve_readable_json_path(target) == target
    assert read_json(target) == {"date": "2026-11-02", "event_count": 3}
    assert json.loads(read_json_text(target))["event_count"] == 3


# ---------------------------------------------------------------------------
# 2) 只有 .gz
# ---------------------------------------------------------------------------
def test_reads_gz_when_plain_missing(tmp_path: Path) -> None:
    target = tmp_path / "transcript.json"
    compressed = _write_gz(target, [{"text": "今天称了裸重"}, {"text": "八十四点三五"}])

    assert not target.exists(), "前提：未压缩文件不存在"
    assert resolve_readable_json_path(target) == compressed
    assert read_json(target) == [{"text": "今天称了裸重"}, {"text": "八十四点三五"}]


# ---------------------------------------------------------------------------
# 3) 两者都在 → 未压缩优先（哪怕 gz 内容更新/更"好看"）
# ---------------------------------------------------------------------------
def test_plain_wins_when_both_exist(tmp_path: Path) -> None:
    target = _write_plain(tmp_path / "state.json", {"status": "from-plain"})
    _write_gz(target, {"status": "from-gz"})

    assert resolve_readable_json_path(target) == target
    assert read_json(target) == {"status": "from-plain"}, "两个都在时必须读未压缩文件"


# ---------------------------------------------------------------------------
# 4) .gz 损坏
# ---------------------------------------------------------------------------
def test_corrupt_gz_raises_and_fail_open_returns_default(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    broken = target.with_name(target.name + ".gz")
    broken.write_bytes(b"this is not a gzip stream")

    with pytest.raises((OSError, EOFError)) as error:
        read_json(target)
    assert not isinstance(error.value, json.JSONDecodeError), "损坏的是压缩层，不是 JSON 层"

    # fail-open 版本：读取端自己的兜底（大多数调用点就是这样用的）
    assert read_json_or(target, default={"status": "missing"}) == {"status": "missing"}


def test_corrupt_gz_json_payload_raises(tmp_path: Path) -> None:
    """压缩流合法但内容不是 JSON：必须报 JSONDecodeError，不能返回 None/半个对象。"""
    target = tmp_path / "state.json"
    with gzip.open(target.with_name(target.name + ".gz"), "wt", encoding="utf-8") as stream:
        stream.write("{not json")

    with pytest.raises(json.JSONDecodeError):
        read_json(target)
    assert read_json_or(target, default=None) is None


# ---------------------------------------------------------------------------
# 缺失
# ---------------------------------------------------------------------------
def test_missing_both_raises_filenotfound(tmp_path: Path) -> None:
    target = tmp_path / "nope.json"
    assert resolve_readable_json_path(target) == target
    with pytest.raises(FileNotFoundError):
        read_json(target)
    assert read_json_or(target, default={}) == {}


# ---------------------------------------------------------------------------
# glob：同时收 .gz，同名时未压缩优先
# ---------------------------------------------------------------------------
def test_glob_json_collects_both_variants_and_prefers_plain(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    plain = _write_plain(tmp_path / "a" / "daily_summary.json", {"n": 1})
    only_gz = _write_gz(tmp_path / "b" / "daily_summary.json", {"n": 2})
    # a 的两个都在 → 只留未压缩
    _write_gz(tmp_path / "a" / "daily_summary.json", {"n": 99})

    found = glob_json(tmp_path, "*/daily_summary.json")

    assert found == sorted([plain, only_gz])
    assert read_json(found[0]) == {"n": 1}
    assert read_json(found[1]) == {"n": 2}


def test_glob_json_without_any_gz_matches_plain_glob(tmp_path: Path) -> None:
    for name in ("2026-11-01", "2026-11-02"):
        directory = tmp_path / name
        directory.mkdir()
        _write_plain(directory / "daily_summary.json", {"date": name})

    assert glob_json(tmp_path, "*/daily_summary.json") == sorted(
        tmp_path.glob("*/daily_summary.json")
    )
