"""JobStore mixin 共用的辅助函数，从 local_web.py 机械搬出，行为不变。"""
from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
from typing import BinaryIO
import hashlib
import os


def _safe_relative_path(value: str, fallback: str) -> str:
    normalized = value.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return Path(fallback).name
    return "/".join(parts)


def _stream_to_file(
    stream: BinaryIO,
    destination: Path,
    *,
    max_bytes: int | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as output:
        while max_bytes is None or size < max_bytes:
            read_size = 1024 * 1024
            if max_bytes is not None:
                read_size = min(read_size, max_bytes - size)
            block = stream.read(read_size)
            if not block:
                break
            digest.update(block)
            output.write(block)
            size += len(block)
        # [2026-10-29 batch3] 落地前先 fsync：调用方随后 os.replace 到 content/<sha256>，
        # 没有这一步时「rename 已持久化、数据还在页缓存」，掉电会留下 0 字节/半截目标文件。
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest(), size
