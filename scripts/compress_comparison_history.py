#!/usr/bin/env python3
"""把 comparison-history 里的旧快照压成 `.json.gz`（默认 dry-run；`--apply` 才动文件）。

comparison-history 是 Jev/Clef 每次对照前的存档，约占中间 JSON 的 77%，线上读路径都跳过它。
原文件只在压缩结果解压校验一致后才删。每个 comparison-history 最新一份不压，
`prune_comparison_history.py` 的删除后抽检要读它。解压恢复：`gunzip <file>.json.gz`。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRNAME = "comparison-history"
#: 磁盘低于这个值就停，压缩过程要写临时文件。
MIN_FREE_BYTES = 2 * 1024**3


def discover(root: Path) -> list[Path]:
    """返回可压的 `.json`：跳过每个 history 目录最新一份、符号链接和硬链接。"""
    targets: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        if DIRNAME not in dirnames:
            continue
        history = Path(dirpath) / DIRNAME
        if history.is_symlink():
            continue
        snapshots = sorted(item for item in history.iterdir() if item.is_dir() and not item.is_symlink())
        for snapshot in snapshots[:-1]:
            for path in sorted(snapshot.rglob("*.json")):
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_nlink > 1:
                    continue
                targets.append(path)
    return targets


def _sha256(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def compress_one(path: Path) -> int:
    """压缩一个文件，返回省下的字节数。校验不一致时抛错，原文件保留。"""
    target = path.with_name(path.name + ".gz")
    temp = path.with_name(f".{path.name}.gz.tmp")
    try:
        with path.open("rb") as source, gzip.open(temp, "wb", compresslevel=6) as sink:
            shutil.copyfileobj(source, sink, 1024 * 1024)
        with path.open("rb") as source:
            expected = _sha256(source)
        with gzip.open(temp, "rb") as restored:
            actual = _sha256(restored)
        if actual != expected:
            raise OSError(f"解压校验不一致：{path}")
        saved = path.stat().st_size - temp.stat().st_size
        shutil.copystat(path, temp)
        os.replace(temp, target)
        path.unlink()
        return saved
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "output" / "local-web")
    parser.add_argument("--apply", action="store_true", help="真正压缩；不带就只统计")
    parser.add_argument("--min-free-bytes", type=int, default=MIN_FREE_BYTES)
    args = parser.parse_args(argv)

    targets = discover(args.root)
    total = sum(path.stat().st_size for path in targets)
    print(f"可压缩 {len(targets)} 个文件，共 {total / 1e9:.2f} GB")
    if not args.apply:
        print("dry-run：没有改动任何文件；加 --apply 才会压缩")
        return 0

    saved = done = 0
    for path in targets:
        if shutil.disk_usage(args.root).free < args.min_free_bytes:
            print(f"磁盘可用空间不足，已停止：完成 {done}/{len(targets)}", file=sys.stderr)
            return 3
        try:
            saved += compress_one(path)
        except (OSError, EOFError) as exc:
            print(f"已停止，原文件保留：{exc}", file=sys.stderr)
            return 2
        done += 1
    print(f"完成 {done} 个文件，省下 {saved / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
