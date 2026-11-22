#!/usr/bin/env python3
"""真实音频端到端：MOSS 声纹新主路径（CAM++ + 按说话人合并打分）。

必须用 8765 worker 的那个 venv 跑 —— 只有它装了 funasr（``local-config.json`` 的
``python`` 键）：

    PYTHONPATH=src \
      $HOME/.life-logger/.venv-funasr/bin/python \
      scripts/probe_moss_campplus_e2e.py

验证四件事（全部走生产代码：``moss_transcribe.MossSpeakerAttributor`` /
``score_segments`` / ``transcribe_file``，同进程，无子进程）：

1. 后端真的是 CAM++（funasr 同进程加载），阈值是它自己那套 0.40/0.30；
2. 同一 (chunk, 说话人) 组只调一次模型、分数回填给组内每个 span；
3. 真实录音上的分离：AI 晚报视频组 < 0.40 < 本人组（内容经人工确认）；
4. ``transcribe_file`` 的生产接线（只有 MOSS ASR 子进程被真实 diarization 结果替换）
   也能跑通，并把后端/阈值/粒度写进 stats。

只读：不写仓库任何产物。

现场数据（2026-10-14 的 watch 会话）：
* 音频 ``segments/segment-0000NN.m4a``（每段 60s）；
* 说话人 + 时间戳 ``formal-asr/segments/NNNNNN/state.json`` 的 rows ——
  注意 ``start_offset_seconds`` 是**会话级**的，段内定位必须减
  ``manifest.json`` 的 ``segments[].start_offset_ms``（不能用 seq*60，会累积漂移）；
* 模板 ``work/watch-2026-10-01/template-from-watch-self.wav``。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from life_logger import moss_transcribe  # noqa: E402
from life_logger.moss_transcribe import (  # noqa: E402
    MossSpeakerAttributor,
    score_segments,
    transcribe_file,
)

SESSION = (
    ROOT
    / "output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d"
    / "17BFCCD8F1E54F11BCE04639E6225DC9"
)
TEMPLATE = ROOT / "work/watch-2026-10-01/template-from-watch-self.wav"
# 人耳/内容确认过的锚点段：19、20 = AI 晚报视频（"晚上好，我是阿林…"）；
# 4、10 = 本人的话（乌冬面、国考报名）。
ANCHORS: tuple[tuple[str, int], ...] = (("视频", 19), ("视频", 20), ("本人", 4), ("本人", 10))

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    print(f"  {'✅' if condition else '❌'} {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def _manifest_base_seconds(sequence: int) -> float:
    manifest = json.loads((SESSION / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest.get("segments") or []:
        if int(item.get("sequence", -1)) == sequence:
            return float(item.get("start_offset_ms") or 0) / 1000.0
    return 0.0


def _rows(sequence: int) -> list[dict[str, Any]]:
    path = SESSION / "formal-asr/segments" / f"{sequence:06d}" / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    base = _manifest_base_seconds(sequence)
    for row in state.get("rows") or []:
        if not isinstance(row, dict) or not row.get("text"):
            continue
        start, end = row.get("start_offset_seconds"), row.get("end_offset_seconds")
        if start is None or end is None:
            continue
        item = dict(row)
        # 段内坐标（真实数据是会话级时间轴，必须减 manifest 的段起点）
        item["local_start"] = float(start) - base
        item["local_end"] = float(end) - base
        rows.append(item)
    return rows


def _decode(path: str | Path, *, sampling_rate: int = 16000) -> np.ndarray:
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=sampling_rate)


def _moss_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把真实 diarization 结果拼成 MOSS 会返回的形状（只替换 ASR 子进程那一步）。"""

    speaker_ids: dict[str, str] = {}
    for row in rows:
        name = str(row.get("speaker") or "说话人一")
        if name not in speaker_ids:
            speaker_ids[name] = f"S{len(speaker_ids) + 1:02d}"
    return {
        "language": "zh",
        "segments": [
            {
                "text": f"[{speaker_ids[str(row.get('speaker') or '说话人一')]}] {row['text']}",
                "start": round(row["local_start"], 3),
                "end": round(row["local_end"], 3),
                "speaker_id": speaker_ids[str(row.get("speaker") or "说话人一")],
            }
            for row in rows
        ],
    }


def part_one_merged_grain() -> dict[str, float]:
    """逐段跑 score_segments（生产函数）：看后端、阈值、分组回填与真实分离度。"""

    print("\n=== 1) MossSpeakerAttributor + score_segments（真实音频、真实 CAM++） ===")
    t0 = time.time()
    attributor = MossSpeakerAttributor(template_path=TEMPLATE, decode=_decode)
    summary = attributor.summary()
    print(f"  模板: {summary['template']}（{summary['template_source']}）")
    print(f"  后端: {summary['voiceprint_backend']} / 维度 {summary['embedding_dim']}"
          f" / 阈值 self={summary['self_threshold']} unknown={summary['unknown_threshold']}"
          f"（{summary['threshold_source']}）")
    print(f"  CAM++ 权重: {summary['campplus_model_dir']}（{summary['campplus_model_source']}）")
    print(f"  模型加载+模板 embedding 用时 {time.time() - t0:.2f}s")
    check(attributor.available, "归属器可用", str(attributor.degraded_reason))
    check(
        summary["voiceprint_backend"] == "campplus",
        "走的是 CAM++ 新路径（同进程 funasr）",
        str(summary["backend_fallback_reason"]),
    )
    check(
        (summary["self_threshold"], summary["unknown_threshold"]) == (0.40, 0.30),
        "用的是 CAM++ 自己那套阈值",
    )
    if not attributor.available or attributor.embedder is None:
        print(f"❌ 归属器没起来（{attributor.degraded_reason}），后面没法继续")
        return {}

    embed_calls: list[int] = []
    real_embed = attributor.embedder.embed

    def _counting_embed(samples):  # noqa: ANN001 - 计数壳
        embed_calls.append(int(np.asarray(samples, dtype=np.float32).reshape(-1).size))
        return real_embed(samples)

    attributor.embedder.embed = _counting_embed  # type: ignore[method-assign]

    scores: dict[str, float] = {}
    for expect, sequence in ANCHORS:
        audio = SESSION / "segments" / f"segment-{sequence:06d}.m4a"
        rows = _rows(sequence)
        if not audio.is_file() or not rows:
            print(f"  ⚠️ 跳过 seq {sequence}（音频/rows 缺失）")
            continue
        arr = _decode(audio)
        spans = [(row["local_start"], row["local_end"]) for row in rows]
        groups = [str(row.get("speaker") or "") or None for row in rows]
        before = len(embed_calls)
        records = score_segments(
            attributor, arr, spans, span_offset=0.0, speaker_groups=groups
        )
        windows = embed_calls[before:]
        by_group: dict[str, list[dict[str, Any]]] = {}
        for group, record in zip(groups, records):
            by_group.setdefault(str(group), []).append(record)

        group_scores = {key: rows_[0]["voice_similarity"] for key, rows_ in by_group.items()}
        best = max(group_scores.values())
        scores[f"{expect}{sequence}"] = float(best)
        labels = {key: rows_[0]["source_label"] for key, rows_ in by_group.items()}
        shared = all(
            len({record["voice_similarity"] for record in group_records}) == 1
            for group_records in by_group.values()
        )
        print(
            f"  {expect} seq={sequence}: {len(rows)} 段 → {len(by_group)} 组"
            f" 组分数={ {k: (round(v, 3) if v is not None else None) for k, v in group_scores.items()} }"
            f" 标签={labels} 模型调用窗长(样点)={windows}"
        )
        check(shared, f"seq {sequence} 组内每个 span 复用同一个分数")
        check(
            len(windows) == len(by_group),
            f"seq {sequence} 每个说话人组只算一次",
            f"组数={len(by_group)} 调用={len(windows)}",
        )
        if expect == "视频":
            check(best < summary["self_threshold"], f"seq {sequence} 视频组低于自阈值", f"{best:.3f}")
            check(
                all(label != "self" for label in labels.values()),
                f"seq {sequence} 视频没有被判 self",
                str(labels),
            )
        else:
            check(best >= summary["self_threshold"], f"seq {sequence} 本人组高于自阈值", f"{best:.3f}")
            check(
                "self" in set(labels.values()),
                f"seq {sequence} 本人被判 self",
                str(labels),
            )
        check(
            all(
                record["decision_reason"] != "voiceprint_extraction_failed"
                for record in records
            ),
            f"seq {sequence} 每段都拿到了真实分数",
        )

    # 逐 span（老粒度）对比：同一批音频同一模型，只改粒度
    print("\n  --- 对照：同一模型、同一音频，改成逐 span 打分 ---")
    per_span: dict[str, float] = {}
    for expect, sequence in ANCHORS:
        audio = SESSION / "segments" / f"segment-{sequence:06d}.m4a"
        rows = _rows(sequence)
        if not audio.is_file() or not rows:
            continue
        arr = _decode(audio)
        spans = [(row["local_start"], row["local_end"]) for row in rows]
        records = score_segments(attributor, arr, spans, span_offset=0.0)
        values = [
            float(record["voice_similarity"])
            for record in records
            if record.get("voice_similarity") is not None
        ]
        if values:
            per_span[f"{expect}{sequence}"] = float(max(values))
            print(f"  {expect} seq={sequence}: 逐 span 最高 {max(values):.3f}（{len(values)} 个有效窗）")
    video_per_span = [v for k, v in per_span.items() if k.startswith("视频")]
    self_per_span = [v for k, v in per_span.items() if k.startswith("本人")]
    if video_per_span and self_per_span:
        print(
            f"  逐 span 分离间隔 {min(self_per_span) - max(video_per_span):+.3f}"
            f"；合并粒度分离间隔 {min(s for k, s in scores.items() if k.startswith('本人')) - max(s for k, s in scores.items() if k.startswith('视频')):+.3f}"
        )
    return scores


def part_two_transcribe_file() -> None:
    """生产接线：只把 MOSS ASR 子进程换成真实 diarization 结果，其余全真。"""

    print("\n=== 2) transcribe_file 生产接线（真音频 + 真 CAM++，只替 ASR 子进程） ===")
    sequence = 19
    rows = _rows(sequence)
    payload = _moss_payload(rows)
    audio = SESSION / "segments" / f"segment-{sequence:06d}.m4a"

    original_run_moss = moss_transcribe._run_moss
    moss_transcribe._run_moss = lambda chunk, outp, hotwords=None: payload  # type: ignore[assignment]
    try:
        segments, stats = transcribe_file(
            audio,
            recording_id=f"e2e-{sequence:06d}",
            template_path=TEMPLATE,
            config_path=ROOT / "local-config.json",
        )
    finally:
        moss_transcribe._run_moss = original_run_moss  # type: ignore[assignment]

    attribution = stats["speaker_attribution"]
    print(
        f"  段数={len(segments)} 后端={attribution['voiceprint_backend']}"
        f" 阈值={attribution['self_threshold']}/{attribution['unknown_threshold']}"
        f" 粒度={attribution['granularity']}"
        f" 标签分布={attribution['label_counts']}"
    )
    rows_out = [
        (
            segment.segment_id,
            stats["attributions"][segment.segment_id],
        )
        for segment in segments
    ]
    for segment_id, record in rows_out:
        print(
            f"    {segment_id}: sim={record.get('voice_similarity')}"
            f" label={record.get('source_label')!r}"
            f" 组={record.get('speaker_group_key')}"
            f" 组内段数={record.get('speaker_group_spans')}"
            f" 组时长={record.get('speaker_group_seconds')}"
        )
    check(len(segments) == len(rows), "转写段数与真实 diarization 段数一致")
    check(attribution["voiceprint_backend"] == "campplus", "接线里也是 CAM++ 后端")
    check(attribution["granularity"] == "speaker_group", "接线里用的是说话人合并粒度")
    values = {
        record.get("voice_similarity") for _sid, record in rows_out
    }
    check(len(values) == 1, "同一说话人组的所有段共享同一个分数", str(values))
    label = next(iter({record.get("source_label") for _sid, record in rows_out}))
    check(label != "self", "AI 晚报视频段没有被判 self", str(label))


def main() -> int:
    try:
        import funasr  # noqa: F401

        print(f"funasr {getattr(__import__('funasr'), '__version__', '?')} 可用（同进程）")
    except Exception as exc:  # pragma: no cover - 环境不对时如实报
        print(f"❌ 当前解释器没有 funasr（{type(exc).__name__}）—— 请用 local-config.json 的 python 跑")
        return 2
    if not (TEMPLATE.is_file() and SESSION.is_dir()):
        print(f"❌ 现场数据缺失：{TEMPLATE} / {SESSION}")
        return 2

    scores = part_one_merged_grain()
    print(f"\n  分组粒度分数：{ {k: round(v, 3) for k, v in scores.items()} }")
    part_two_transcribe_file()

    print("\n=== 结论 ===")
    if FAILURES:
        print(f"❌ {len(FAILURES)} 条检查未通过：{FAILURES}")
        return 1
    print("✅ 全部检查通过：CAM++ + 按说话人合并的新主路径在真实音频上跑通")
    return 0


if __name__ == "__main__":
    sys.exit(main())
