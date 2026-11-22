#!/usr/bin/env python3
"""诊断声纹模板标定：为什么所有片段都被判成 unknown。

背景（2026-10-01 实测）：
  * ``scripts/attribute_funasr_transcript.py:414`` 对每一段待判定音频调用
    ``normalize_audio_gain()``，把语音拉到 -24 dBFS 左右再送给 CAM++。
  * 同一文件的 ``:650`` 行，模板却是「原样整个文件」直接 ``model.generate()``，
    既不归一化也不做 VAD 裁剪。
  * 现用模板 ``work/h8-2026-09-04/template-from-device-v2.wav`` 长 38.81s、
    峰值 32364 但 RMS 只有 799 —— 绝大部分是静音。

两边因此不是同一种声学对象，余弦相似度塌到 0.001~0.12，全部落入 unknown，
于是 ``should_extract_life_events`` 拒绝生成事件，整条流水线白跑。

本脚本只用 CPU 跑一次 CAM++，对比几种模板构造方式，输出相似度矩阵。
不改动任何线上配置，纯只读诊断。

用法：
    python3 scripts/diagnose_speaker_template.py \
        --template work/h8-2026-09-04/template-from-device-v2.wav \
        --sample ~/AirPassportRecordings/ptt_1790843047.wav \
        --sample ~/AirPassportRecordings/ptt_1790860865.wav
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from life_logger.source_attribution import (  # noqa: E402
    cosine_similarity,
    normalize_audio_gain,
)

SAMPLE_RATE = 16000
DEFAULT_SPEAKER_MODEL = "cam++"


# --------------------------------------------------------------------------
# 音频加载
# --------------------------------------------------------------------------


def load_waveform(path: Path) -> Any:
    """读成 float32 单声道波形（与 attribute_funasr_transcript._load_waveform 一致）。"""
    import soundfile as sf

    waveform, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if getattr(waveform, "ndim", 1) == 2:
        waveform = waveform.mean(axis=1)
    # 统一到 16k（CAM++ 与 VAD 都按 16k 工作）
    if sample_rate != SAMPLE_RATE:
        import numpy as np

        target = int(round(len(waveform) * SAMPLE_RATE / sample_rate))
        source_index = np.linspace(0, len(waveform) - 1, target)
        waveform = np.interp(source_index, np.arange(len(waveform)), waveform)
    return waveform


def rms_dbfs(samples: Any) -> float:
    from math import log10

    values = [float(v) for v in samples]
    if not values:
        return -120.0
    peak = max(abs(v) for v in values)
    if peak == 0.0:
        return -120.0
    rms = peak * (sum((v / peak) ** 2 for v in values) / len(values)) ** 0.5
    return 20.0 * log10(max(rms, 1e-6))


# --------------------------------------------------------------------------
# VAD：找出模板里的语音区间
# --------------------------------------------------------------------------


def speech_regions(waveform: Any, *, frame_ms: int = 30, hop_ms: int = 10, margin_db: float = 10.0) -> list[tuple[int, int]]:
    """按能量门限切出语音区间，返回 (start, end) 采样点对。

    不用项目的 FSMN-VAD：本地那份是 ONNX 导出，没在 funasr 的模型注册表里，
    AutoModel 会直接拒绝。对「把模板里的静音丢掉」这个目的，能量门限就够了。
    """
    import numpy as np

    frame = max(1, int(SAMPLE_RATE * frame_ms / 1000))
    hop = max(1, int(SAMPLE_RATE * hop_ms / 1000))
    total = len(waveform)
    if total < frame:
        return []

    starts = range(0, total - frame + 1, hop)
    energies = np.array(
        [float(np.sqrt(np.mean(np.square(waveform[s : s + frame])))) for s in starts]
    )
    if energies.size == 0:
        return []
    # 噪声底：取最安静的那 10% 帧
    floor = float(np.percentile(energies, 10))
    peak = float(energies.max())
    if peak <= 0:
        return []
    threshold = max(floor * (10 ** (margin_db / 20.0)), peak * 0.05)
    active = energies >= threshold

    regions: list[tuple[int, int]] = []
    begin = None
    for index, is_active in enumerate(active):
        if is_active and begin is None:
            begin = index
        elif not is_active and begin is not None:
            regions.append((begin * hop, min(total, index * hop + frame)))
            begin = None
    if begin is not None:
        regions.append((begin * hop, total))
    # 丢掉过短的碎片（< 200ms）
    minimum = int(0.2 * SAMPLE_RATE)
    return [(s, e) for s, e in regions if e - s >= minimum]


def concat_regions(waveform: Any, regions: list[tuple[int, int]], *, max_seconds: float = 12.0) -> Any:
    """把语音区间拼起来，最多取 max_seconds，丢掉静音。"""
    import numpy as np

    limit = int(max_seconds * SAMPLE_RATE)
    chunks = []
    total = 0
    for start, end in regions:
        chunk = waveform[start:end]
        if len(chunk) == 0:
            continue
        take = min(len(chunk), limit - total)
        if take <= 0:
            break
        chunks.append(chunk[:take])
        total += take
    if not chunks:
        return np.zeros(0, dtype="float32")
    return np.concatenate(chunks)


# --------------------------------------------------------------------------
# 声纹
# --------------------------------------------------------------------------


def embed(model: Any, audio: Any) -> list[float]:
    result = model.generate(input=audio, batch_size=1)
    for item in result:
        if isinstance(item, dict) and "spk_embedding" in item:
            rows = item["spk_embedding"]
            if hasattr(rows, "tolist"):
                rows = rows.tolist()
            if rows and isinstance(rows[0], list):
                return [float(v) for v in rows[0]]
            return [float(v) for v in rows]
    raise RuntimeError("CAM++ 没有返回 spk_embedding")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="诊断声纹模板标定问题")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--sample", type=Path, action="append", required=True)
    parser.add_argument("--speaker-model", default=DEFAULT_SPEAKER_MODEL)
    parser.add_argument("--max-template-seconds", type=float, default=12.0)
    args = parser.parse_args(argv)

    from funasr import AutoModel

    print("加载 CAM++ 声纹模型（需要十几秒）...", flush=True)
    speaker = AutoModel(
        model=args.speaker_model, device="cpu", disable_update=True, disable_pbar=True
    )

    template_audio = load_waveform(args.template)
    print(
        f"\n模板: {args.template.name}  {len(template_audio) / SAMPLE_RATE:.2f}s  "
        f"RMS={rms_dbfs(template_audio):.1f} dBFS"
    )

    # ---- 变体 1：现状（原样整文件，不归一化）----
    raw_embedding = embed(speaker, template_audio)

    # ---- 变体 2：只归一化，仍带静音 ----
    normalized, norm_meta = normalize_audio_gain(template_audio)
    normalized_embedding = embed(speaker, normalized)
    print(f"  仅归一化: gain={norm_meta['gain_db']:.2f} dB  applied={norm_meta['applied']}")

    # ---- 变体 3：VAD 裁掉静音 + 归一化（与片段处理方式对齐）----
    trimmed_embedding = None
    if True:
        regions = speech_regions(template_audio)
        speech = concat_regions(template_audio, regions, max_seconds=args.max_template_seconds)
        if len(speech) > 0:
            speech_norm, speech_meta = normalize_audio_gain(speech)
            trimmed_embedding = embed(speaker, speech_norm)
            print(
                f"  静音裁剪: {len(regions)} 个语音区间 → {len(speech) / SAMPLE_RATE:.2f}s  "
                f"RMS={rms_dbfs(speech):.1f} → 归一化 gain={speech_meta['gain_db']:.2f} dB"
            )
        else:
            print("  静音裁剪: 没有检出语音区间（模板可能基本是静音）")

    print("\n待判定片段与三种模板的余弦相似度：")
    print(f"  {'片段':<28}{'时长':>7}  {'现状':>8}{'仅归一化':>10}{'裁静音+归一化':>14}")
    rows: list[dict[str, Any]] = []
    for sample_path in args.sample:
        audio = load_waveform(sample_path)
        segment, segment_meta = normalize_audio_gain(audio)
        segment_embedding = embed(speaker, segment)
        sim_raw = cosine_similarity(raw_embedding, segment_embedding)
        sim_norm = cosine_similarity(normalized_embedding, segment_embedding)
        sim_trim = (
            cosine_similarity(trimmed_embedding, segment_embedding)
            if trimmed_embedding is not None
            else None
        )
        print(
            f"  {sample_path.name:<28}{len(audio) / SAMPLE_RATE:>6.2f}s  "
            f"{sim_raw:>8.3f}{sim_norm:>10.3f}"
            + (f"{sim_trim:>14.3f}" if sim_trim is not None else f"{'-':>14}")
        )
        rows.append(
            {
                "sample": str(sample_path),
                "duration_seconds": round(len(audio) / SAMPLE_RATE, 2),
                "segment_gain_db": round(float(segment_meta["gain_db"]), 2),
                "similarity_current": round(sim_raw, 4),
                "similarity_normalized": round(sim_norm, 4),
                "similarity_vad_normalized": round(sim_trim, 4) if sim_trim is not None else None,
            }
        )

    print(
        "\n判定阈值：>= 0.50 记为 self；0.25~0.50 记 unknown；< 0.25 记 non_self。"
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
