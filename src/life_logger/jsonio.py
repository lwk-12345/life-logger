"""中间 JSON 的读取端：`x.json` 优先，缺了才读 `x.json.gz`。

为什么需要（2026-11-05 A0 派单）
--------------------------------
`output/local-web` 下 11 GB 是中间 JSON（`daily_summary.json` / `transcript.json` /
`segments/*/state.json` / `event-episodes` …），压成 `.gz` 能省约 8.5 GB。
但那要求**读取端先支持 .gz**，否则一压缩页面与抽取全挂。

本模块只做"读"，有三条硬约束：
  * **未压缩优先**：两个文件都在时必须读 `x.json`（压缩只是省盘，不是新的事实源）；
  * **不做任何压缩/迁移/删除**：写路径原样只写 `x.json`；
  * **不改语义**：文件缺失时的行为与"直接 stat 那个 .json"一致（调用方自己决定
    fail-open 还是抛错），`.gz` 只作为**额外**的可读候选。

用法::

    from life_logger.jsonio import read_json, resolve_readable_json_path

    payload = read_json(path)              # x.json → 不存在则 x.json.gz → 都不存在抛 FileNotFoundError
    for path in glob_json(root, "jobs/*/files/*/result/*/daily_summary.json"):
        ...                                # 同时收 .gz，同名时未压缩优先
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "COMPRESSED_SUFFIX",
    "resolve_readable_json_path",
    "read_json_text",
    "read_json",
    "read_json_or",
    "glob_json",
]

#: 压缩变体后缀（`x.json` → `x.json.gz`）。
COMPRESSED_SUFFIX = ".gz"


def resolve_readable_json_path(path: Path | str) -> Path:
    """返回**实际应该读**的那个文件路径。

    * `x.json` 存在 → `x.json`（未压缩永远优先）；
    * 否则 `x.json.gz` 存在 → `x.json.gz`；
    * 都不存在 → 原样返回 `x.json`（让调用方的 `is_file()` / `stat()` / `open()`
      保持改造前的行为与异常类型）。
    """
    plain = Path(path)
    if plain.is_file():
        return plain
    compressed = plain.with_name(plain.name + COMPRESSED_SUFFIX)
    if compressed.is_file():
        return compressed
    return plain


def read_json_text(path: Path | str) -> str:
    """按 utf-8 读出 JSON 文本；`.gz` 变体会透明解压。"""
    target = resolve_readable_json_path(path)
    if target.name.endswith(COMPRESSED_SUFFIX):
        with gzip.open(target, "rt", encoding="utf-8") as stream:
            return stream.read()
    return target.read_text(encoding="utf-8")


def read_json(path: Path | str) -> Any:
    """读 `x.json`（优先）或 `x.json.gz`。

    文件都不存在 → ``FileNotFoundError``；`.gz` 损坏 → ``gzip.BadGzipFile`` /
    ``OSError``；内容不是合法 JSON → ``json.JSONDecodeError``。
    **本函数不吞异常**：是否 fail-open 由调用方决定（大多数读取点已有自己的兜底）。
    """
    return json.loads(read_json_text(path))


def read_json_or(path: Path | str, default: Any = None) -> Any:
    """`read_json` 的 fail-open 版本：任何读取/解析失败都返回 ``default``。"""
    try:
        return read_json(path)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def glob_json(root: Path | str, pattern: str) -> list[Path]:
    """按 glob 收 JSON 文件，**同时**收 `.gz` 变体；同名时只留未压缩那个。

    返回按路径排序的列表（与 ``sorted(root.glob(pattern))`` 在"没有 .gz"时逐项相同）。
    模式必须以 ``.json`` 结尾；传别的模式会被当作普通 glob（不加后缀）。
    """
    base = Path(root)
    plain = list(base.glob(pattern))
    if not pattern.endswith(".json"):
        return sorted(plain)
    plain_names = {item.as_posix() for item in plain}
    compressed = [
        item
        for item in base.glob(pattern + COMPRESSED_SUFFIX)
        if item.as_posix()[: -len(COMPRESSED_SUFFIX)] not in plain_names
    ]
    return sorted(plain + compressed)


def any_exists(paths: Iterable[Path | str]) -> bool:
    """这些路径里是否有任意一个可读（`x.json` 或 `x.json.gz`）。"""
    return any(resolve_readable_json_path(item).is_file() for item in paths)
