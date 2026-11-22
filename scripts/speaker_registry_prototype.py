#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻联系人声纹：离线聚类原型（**只读**，不碰生产链路、不写真实数据）。

做什么
------
把最近几天的转写片段按声纹做**无监督聚类**，输出「这一簇有多久、说了什么」，
用来回答一个很朴素的问题：*如果给说话人起名字，现在到底能分出几个人？*

不做什么
--------
* 不接 `watch_formal_asr` / `moss_transcribe` 的生产路径；
* 不写 `output/` 下任何真实数据（只往 `work/speaker-registry-20261105/` 写自己的产物）；
* 不改 `source_label`、不参与 self 判定、不碰金额。

后端限制（必须先知道）
----------------------
本机 `.venv` 里 **funasr / onnxruntime 都装不上**，CAM++ 加载失败：

    load 0.0s available=False reason=funasr_unavailable:ModuleNotFoundError

所以原型只能退到 **48 维纯 numpy 声纹**（`moss_transcribe._extract_numpy_voiceprint`）。
而这个后端本身是**已知不靠谱的降级路径** —— 源码注释自己写着
「实测 48 维会把 AI 技术视频判成本人：段 000024 给 0.650，CAM++ 给 -0.024」。
**因此本原型的聚类质量只代表"下限"，不代表上线后的效果。**

用法
----
    .venv/bin/python scripts/speaker_registry_prototype.py --days 3 --limit 60 \
        --out work/speaker-registry-20261105/clusters.json

    .venv/bin/python scripts/speaker_registry_prototype.py --days 3 --dry-run   # 只统计可抽段数
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SH = timezone(timedelta(hours=8))
SR = 16000
#: 与 ResidentSpeakerRegistry 的 cluster 档一致（speaker_registry.DEFAULT_CLUSTER_THRESHOLD）
CLUSTER_THRESHOLD = 0.40
#: 太短的段抽不出可靠声纹（moss_transcribe.VOICEPRINT_MIN_SAMPLES / SR）
MIN_SAMPLES = 3200


# ---------------------------------------------------------------- 纯函数（可测）
def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0 or not (math.isfinite(na) and math.isfinite(nb)):
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def _centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    if not vectors:
        return []
    n = len(vectors)
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    return [x / n for x in acc]


def cluster_embeddings(
    embeddings: Sequence[Sequence[float]] | None,
    *,
    threshold: float = CLUSTER_THRESHOLD,
) -> list[int]:
    """贪心聚类，返回每个输入的簇号（0 起）。**确定性**：同一输入永远同一结果。

    规则（与 registry 的 cluster 档一致）：
      * 依次处理每个向量，找**当前质心**余弦最高的簇；
      * ≥ threshold 就并入并更新质心，否则新开一簇；
      * 空输入 / 空向量返回空列表，不抛异常。
    """
    if not embeddings:
        return []
    labels: list[int] = []
    members: list[list[Sequence[float]]] = []
    centroids: list[list[float]] = []
    for emb in embeddings:
        if not emb:
            labels.append(-1)          # 空向量：单独标 -1，不参与任何簇
            continue
        best, best_sim = -1, -1.0
        for idx, cen in enumerate(centroids):
            sim = cosine(emb, cen)
            if sim > best_sim:
                best, best_sim = idx, sim
        if best >= 0 and best_sim >= threshold:
            members[best].append(emb)
            centroids[best] = _centroid(members[best])
            labels.append(best)
        else:
            members.append([emb])
            centroids.append(list(emb))
            labels.append(len(members) - 1)
    return labels


def summarize_clusters(
    labels: Sequence[int],
    durations: Sequence[float],
    texts: Sequence[str],
    embeddings: Sequence[Sequence[float]] | None = None,
    *,
    samples: int = 3,
) -> list[dict[str, Any]]:
    """按簇汇总：条数、总时长、平均簇内相似度、样本文字。按总时长降序。"""
    groups: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        groups.setdefault(lab, []).append(i)

    out: list[dict[str, Any]] = []
    for lab, idxs in groups.items():
        total = round(sum(durations[i] for i in idxs), 1)
        stamps = [round(durations[i], 1) for i in idxs]
        sims: list[float] = []
        if embeddings:
            vecs = [embeddings[i] for i in idxs if embeddings[i]]
            cen = _centroid(vecs)
            sims = [cosine(v, cen) for v in vecs] if cen else []
        out.append({
            "cluster": lab,
            "count": len(idxs),
            "seconds": total,
            "minutes": round(total / 60.0, 1),
            "mean_internal_similarity": round(sum(sims) / len(sims), 3) if sims else None,
            "min_internal_similarity": round(min(sims), 3) if sims else None,
            "sample_seconds": stamps[:5],
            "samples": [texts[i] for i in idxs[:samples]],
        })
    out.sort(key=lambda c: (-c["seconds"], c["cluster"]))
    return out


def quality_report(
    labels: Sequence[int],
    embeddings: Sequence[Sequence[float]] | None,
) -> dict[str, Any]:
    """聚类质量：簇内平均相似度、跨簇质心最大相似度、单例比例。

    为什么看这三个：
      * 簇内平均相似度低 → 同一簇里其实是不同的人（**过合并**，会认错人）；
      * 跨簇质心相似度高 → 同一个人被拆成两簇（**欠合并**，名字对不上）；
      * 单例比例高 → 大部分段都自成一簇，等于"没分出来"。
    """
    valid = [(i, e) for i, e in enumerate(embeddings or []) if e]
    if not labels or not valid:
        return {"clusters": 0, "items": 0, "note": "no_data"}
    groups: dict[int, list[list[float]]] = {}
    for i, e in valid:
        if labels[i] >= 0:
            groups.setdefault(labels[i], []).append(e)
    internal: list[float] = []
    centroids: list[list[float]] = []
    for lab in sorted(groups):
        cen = _centroid(groups[lab])
        centroids.append(cen)
        internal.extend(cosine(e, cen) for e in groups[lab])
    cross = [
        cosine(centroids[i], centroids[j])
        for i in range(len(centroids)) for j in range(i + 1, len(centroids))
    ]
    singles = sum(1 for lab in groups if len(groups[lab]) == 1)
    return {
        "clusters": len(groups),
        "items": len(valid),
        "mean_internal_similarity": round(sum(internal) / len(internal), 3) if internal else None,
        "max_cross_centroid_similarity": round(max(cross), 3) if cross else None,
        "singleton_ratio": round(singles / len(groups), 3) if groups else None,
    }


def annotate_speaker_name(
    record: dict[str, Any] | None,
    *,
    cluster_id: Any = None,
    name: str | None = None,
    similarity: float | None = None,
) -> dict[str, Any]:
    """把「这是谁」**追加**到归属记录上；**绝不改** `source_label` 与复核标记。

    这是给生产接入定的契约（本原型只演示，不接线）：

      * `source_label`（self / non_self / unknown）是**身份结论**，由声纹+阈值决定，
        常驻联系人只在 non_self / unknown 上补名字 —— **self 一律不加名字**，
        因为"这是本人"和"这是挚友甲哥"是两个互斥的答案；
      * 只增键，不删键、不改值：`speaker_name` / `speaker_cluster_id` / `speaker_similarity`；
      * 名字为空或没有簇号 ⇒ 原样返回，**不写半截字段**；
      * 入参为 None 或不是 dict ⇒ 返回 `{}`（不抛），保证"空数据不崩"。
    """
    if not isinstance(record, dict):
        return {}
    out = dict(record)
    label = str(out.get("source_label") or "")
    if label not in ("non_self", "unknown"):
        # self / 缺字段 / 任何别的值：一律不挂名字。
        # 与 source_attribution.NAMED_CONTACT_LABELS 同一口径 —— 认名字只在
        # "已确认不是本人"的记录上做。
        return out
    cid = str(cluster_id) if cluster_id not in (None, "") else ""
    if not cid or not name:
        return out                      # ← 没绑定就别写半截
    out["speaker_name"] = str(name)
    out["speaker_cluster_id"] = cid
    if similarity is not None:
        out["speaker_similarity"] = round(float(similarity), 4)
    return out


# ---------------------------------------------------------------- 数据侧（只读）
def recent_segment_rows(days: int, limit: int) -> list[dict[str, Any]]:
    """取最近 `days` 天里、有文字且有音频的转写行（按时间升序，最多 `limit` 条）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    pattern = str(ROOT / "output/local-web/watch-sessions/*/*/formal-asr/segments/*/state.json")
    for path in glob.glob(pattern):
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("status") != "completed":
            continue
        rec_dir = Path(path).parents[3]          # <recording>
        seg_no = Path(path).parent.name
        audio = rec_dir / "segments" / f"segment-{seg_no}.m4a"
        if not audio.is_file():
            continue
        try:
            seg_start = int(seg_no) * 60.0
        except ValueError:
            continue
        for row in state.get("rows") or []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if len(text) < 4:
                continue
            ts = str(row.get("absolute_started_at") or "")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None or when < cutoff:
                continue
            try:
                start = float(row.get("start_offset_seconds"))
                end = float(row.get("end_offset_seconds"))
            except (TypeError, ValueError):
                continue
            if end - start < 0.8:               # 太短抽不出声纹
                continue
            rows.append({
                "when": when.astimezone(SH).isoformat(timespec="seconds"),
                "text": text,
                "audio": str(audio),
                "seg_start": seg_start,
                "start": start,
                "end": end,
            })
    rows.sort(key=lambda r: r["when"])
    if limit and len(rows) > limit:
        step = len(rows) / float(limit)         # 均匀抽样，避免全挤在同一小时
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows


def decode_window(audio: Path, start: float, end: float) -> Any:
    """从录音里切一小段 → 16k mono float32 numpy；失败返回 None。"""
    import numpy as np

    dur = max(0.2, float(end) - float(start))
    rel = max(0.0, float(start) - float(seg_start_of(audio, start)))
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{rel:.3f}", "-t", f"{dur:.3f}",
           "-i", str(audio), "-ar", str(SR), "-ac", "1", "-f", "f32le", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=30).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    if len(raw) < MIN_SAMPLES * 4:
        return None
    return np.frombuffer(raw, dtype=np.float32)


def seg_start_of(audio: Path, _start: float) -> float:
    """segment-NNNNNN.m4a 的起始秒 = 编号 × 60。"""
    try:
        return int(audio.stem.split("-")[-1]) * 60.0
    except ValueError:
        return 0.0


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="常驻联系人声纹：离线聚类原型（只读）")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--threshold", type=float, default=CLUSTER_THRESHOLD)
    ap.add_argument("--out", default=str(ROOT / "work/speaker-registry-20261105/clusters.json"))
    ap.add_argument("--dry-run", action="store_true", help="只统计可抽段数，不解码不比声纹")
    args = ap.parse_args(list(argv) if argv is not None else None)

    rows = recent_segment_rows(args.days, args.limit)
    print(f"最近 {args.days} 天可抽段：{len(rows)} 条")
    if args.dry_run or not rows:
        return 0

    from life_logger.moss_transcribe import _extract_numpy_voiceprint

    embeddings: list[list[float]] = []
    durations: list[float] = []
    texts: list[str] = []
    kept: list[dict[str, Any]] = []
    for row in rows:
        arr = decode_window(Path(row["audio"]), row["start"], row["end"])
        if arr is None:
            continue
        emb = _extract_numpy_voiceprint(arr)
        if not emb:
            continue
        embeddings.append(emb)
        durations.append(round(row["end"] - row["start"], 2))
        texts.append(row["text"][:60])
        kept.append({k: row[k] for k in ("when", "text")})

    labels = cluster_embeddings(embeddings, threshold=args.threshold)
    clusters = summarize_clusters(labels, durations, texts, embeddings)
    quality = quality_report(labels, embeddings)
    payload = {
        "schema": "speaker-cluster-prototype-v1",
        "generated_at": datetime.now(SH).isoformat(timespec="seconds"),
        "backend": "numpy48（CAM++ 不可用：funasr missing）",
        "threshold": args.threshold,
        "days": args.days,
        "requested": len(rows),
        "scored": len(embeddings),
        "quality": quality,
        "clusters": clusters,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"成功抽出声纹：{len(embeddings)} 条 → 聚成 {quality['clusters']} 簇")
    print(f"簇内平均相似度 {quality['mean_internal_similarity']} · "
          f"跨簇质心最大相似度 {quality['max_cross_centroid_similarity']} · "
          f"单例比例 {quality['singleton_ratio']}")
    for c in clusters[:10]:
        print(f"  簇{c['cluster']:<2} {c['minutes']:>5.1f}分钟 {c['count']:>3}条 "
              f"内部相似度={c['mean_internal_similarity']}  例：{c['samples'][0][:34] if c['samples'] else ''}")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
