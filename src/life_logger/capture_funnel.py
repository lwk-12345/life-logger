"""Capture funnel —— 「说了多少 → 记住多少」。

存在理由（Karpathy 视角的第一步）：
一个记录系统必须有一个 loss。这个 loss 是「用户看到自己说过的多少」。
在它被显示出来之前，没有人会发现系统丢掉了 99.7%。

本模块只做一件事：数清楚某一天「转写出来多少句话」。
剩下的对比（事件数、显示数）由调用方用自己的数据算——避免口径打架。

口径规则（每一条都是为了不重复计数）：
  1. 只扫**日期目录**下的 transcript.json（路径里带 <date>）
  2. 排除 recovery / live / preview / backup 目录（副本或已废弃层）
  3. 同一内容按 md5 只算一次（同一条录音可能被多个目录引用）
  4. 空白和纯标点不算一句话

性能：全量扫描约 2 秒，所以进程内缓存 TTL 60 秒。
「说了多少」滞后一分钟完全可接受，不值得为此做增量。
"""

from __future__ import annotations

import gzip
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from life_logger.jsonio import glob_json, read_json, resolve_readable_json_path

BJ_TZ = timezone(timedelta(hours=8))

# 这些目录里的是重跑副本或已废弃的中间层，数进去会重复
_EXCLUDED_PARTS = ("/recovery/", "/live/", "/preview/", "/backup", "/.tmp")

# 纯标点 / 语气符号不算「说了一句话」
_JUNK_TEXTS = {"。", ".", "，", ",", "-", "…", "？", "?", "！", "!", "：", ":", "、", "；", ";"}

_TTL_SECONDS = 60.0
# key = f"{date_str}", value = (写入时间戳, 结果)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _md5(path: Path) -> str:
    # [2026-11-05] 去重指纹要按**实际读取的那个文件**算：`x.json.gz` 与 `x.json` 并存时
    # 未压缩优先（指纹取未压缩），只有 .gz 时才读压缩字节。
    try:
        target = resolve_readable_json_path(path)
        if target.name.endswith(".gz"):
            with gzip.open(target, "rb") as stream:
                return hashlib.md5(stream.read()).hexdigest()
        return hashlib.md5(target.read_bytes()).hexdigest()
    except Exception:
        return path.as_posix()


def _load_list(path: Path) -> list | None:
    try:
        data = read_json(path)
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _iter_transcript_paths(web_root: Path, date_str: str):
    """产出当天所有候选转写文件路径（用精确 glob，比 rglob 全库快得多）。

    [2026-11-05] 同时收 `transcript.json.gz`（中间 JSON 压缩后仍可读）。
    """
    # 手表链路：watch-sessions/<dev>/<sess>/formal-asr/segments/<seq>/<date>/transcript.json
    yield from glob_json(web_root, f"watch-sessions/*/*/formal-asr/segments/*/{date_str}/transcript.json")
    # 作业包链路：jobs/job-*/files/*/result/<date>/transcript.json
    yield from glob_json(web_root, f"jobs/*/files/*/result/{date_str}/transcript.json")
    # 作业包 ASR 层：jobs/job-*/files/*/result/{date}/asr/transcript.json
    yield from glob_json(web_root, f"jobs/*/files/*/result/{date_str}/asr/transcript.json")


def count_said(web_root: Path, date_str: str) -> dict[str, int]:
    """数当天「说了多少」。返回 sentences / chars / segments / files_scanned。"""
    sentences = chars = segments = scanned = 0
    seen: set[str] = set()

    for path in _iter_transcript_paths(web_root, date_str):
        s = path.as_posix()
        if any(part in s for part in _EXCLUDED_PARTS):
            continue
        fingerprint = _md5(path)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        rows = _load_list(path)
        if rows is None:
            continue
        scanned += 1
        if "/formal-asr/segments/" in s:
            segments += 1
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text or text in _JUNK_TEXTS:
                continue
            sentences += 1
            chars += len(text)

    return {
        "said_sentences": sentences,
        "said_chars": chars,
        "said_segments": segments,
        "files_scanned": scanned,
    }


def compute_capture_funnel(
    web_root: Path,
    date_str: str,
    *,
    ttl: float = _TTL_SECONDS,
    use_cache: bool = True,
) -> dict[str, Any]:
    """带 TTL 缓存的入口。返回 said_* 指标 + 元信息。"""
    now = time.time()
    if use_cache:
        hit = _CACHE.get(date_str)
        if hit is not None and (now - hit[0]) < ttl:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached

    result: dict[str, Any] = {
        "date": date_str,
        "computed_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "cached": False,
    }
    result.update(count_said(web_root, date_str))
    _CACHE[date_str] = (now, result)
    return result


def clear_cache() -> None:
    """测试与手动刷新用。"""
    _CACHE.clear()
