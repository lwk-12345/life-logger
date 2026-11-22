#!/usr/bin/env python3
"""用同一录音链路的素材重建声纹模板。

为什么需要：``config["template"]`` 原本指向 H8 录音笔的
``work/h8-2026-09-04/template-from-device-v2.wav``。2026-10-01 实测发现，
同一个人在不同麦克风上的 CAM++ 余弦相似度只有 0.31，而 ``self`` 门槛是 0.50
（同一人 + 同一麦克风时相似度为 0.46~0.78）。跨声道让所有片段掉进 unknown，
``should_extract_life_events`` 于是拒绝生成任何事件，整条流水线白跑。

本脚本从指定的录音目录里挑出「像正常说话」的片段，各自裁掉静音、归一化，
再拼成一个干净的模板 WAV。全程只做音频处理，不碰线上配置。

用法：
    python3 scripts/build_speaker_template.py \
        --source ~/AirPassportRecordings \
        --output work/airpassport-2026-10-01/template-from-airpassport.wav \
        --count 6
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from life_logger.source_attribution import normalize_audio_gain  # noqa: E402

SAMPLE_RATE = 16000


def load_waveform(path: Path):
    import soundfile as sf

    waveform, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if getattr(waveform, "ndim", 1) == 2:
        waveform = waveform.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        import numpy as np

        target = int(round(len(waveform) * SAMPLE_RATE / sample_rate))
        waveform = np.interp(
            np.linspace(0, len(waveform) - 1, target),
            np.arange(len(waveform)),
            waveform,
        )
    return waveform


def speech_regions(waveform, *, frame_ms: int = 30, hop_ms: int = 10, margin_db: float = 10.0):
    """按能量门限切出语音区间（同 diagnose_speaker_template 的实现）。"""
    import numpy as np

    frame = max(1, int(SAMPLE_RATE * frame_ms / 1000))
    hop = max(1, int(SAMPLE_RATE * hop_ms / 1000))
    total = len(waveform)
    if total < frame:
        return []
    energies = np.array(
        [
            float(np.sqrt(np.mean(np.square(waveform[s : s + frame]))))
            for s in range(0, total - frame + 1, hop)
        ]
    )
    if energies.size == 0:
        return []
    floor = float(np.percentile(energies, 10))
    peak = float(energies.max())
    if peak <= 0:
        return []
    threshold = max(floor * (10 ** (margin_db / 20.0)), peak * 0.05)
    active = energies >= threshold
    regions, begin = [], None
    for index, is_active in enumerate(active):
        if is_active and begin is None:
            begin = index
        elif not is_active and begin is not None:
            regions.append((begin * hop, min(total, index * hop + frame)))
            begin = None
    if begin is not None:
        regions.append((begin * hop, total))
    minimum = int(0.2 * SAMPLE_RATE)
    return [(s, e) for s, e in regions if e - s >= minimum]


def main(argv: list[str] | None = None) -> int:
    import numpy as np

    parser = argparse.ArgumentParser(description="从同一录音链路重建声纹模板")
    parser.add_argument("--source", type=Path, required=True, help="录音目录")
    parser.add_argument("--pattern", default="ptt_*.wav")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=6, help="挑几个片段拼模板")
    parser.add_argument("--min-seconds", type=float, default=2.0, help="候选片段最短时长")
    parser.add_argument("--max-seconds", type=float, default=15.0, help="候选片段最长时长")
    parser.add_argument("--per-clip-cap", type=float, default=4.0, help="每个片段最多取几秒语音")
    parser.add_argument("--target-seconds", type=float, default=20.0, help="模板总时长目标")
    args = parser.parse_args(argv)

    candidates = []
    for path in sorted(args.source.glob(args.pattern)):
        try:
            waveform = load_waveform(path)
        except Exception:
            continue
        duration = len(waveform) / SAMPLE_RATE
        if not (args.min_seconds <= duration <= args.max_seconds):
            continue
        regions = speech_regions(waveform)
        speech_seconds = sum(e - s for s, e in regions) / SAMPLE_RATE
        # 语音占空比太低的（按了没说话 / 环境噪声）不要
        if speech_seconds < args.min_seconds * 0.5:
            continue
        candidates.append((speech_seconds, duration, path, waveform, regions))

    if not candidates:
        raise SystemExit(f"{args.source} 里没有找到合适的候选片段")

    # 按「有效语音时长」降序，取前 N 个
    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen = candidates[: args.count]
    print(f"候选 {len(candidates)} 个，选中 {len(chosen)} 个：")
    for speech_seconds, duration, path, _, regions in chosen:
        print(
            f"  {path.name:<24} 总长 {duration:5.2f}s  语音 {speech_seconds:5.2f}s  "
            f"区间 {len(regions)}"
        )

    chunks = []
    total = 0
    cap = int(args.per_clip_cap * SAMPLE_RATE)
    target = int(args.target_seconds * SAMPLE_RATE)
    for _, _, _, waveform, regions in chosen:
        taken = 0
        for start, end in regions:
            if taken >= cap or total >= target:
                break
            piece = waveform[start:end]
            take = min(len(piece), cap - taken, target - total)
            if take <= 0:
                continue
            chunks.append(piece[:take])
            taken += take
            total += take
        if total >= target:
            break

    if not chunks:
        raise SystemExit("没有拼出任何语音，检查候选筛选条件")

    template = np.concatenate(chunks)
    template, meta = normalize_audio_gain(template)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.asarray(template) * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(args.output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())

    peak = int(np.max(np.abs(pcm))) if len(pcm) else 0
    rms = float(np.sqrt(np.mean(np.square(pcm.astype("float64"))))) if len(pcm) else 0.0
    print(
        f"\n已写出 {args.output}\n"
        f"  时长 {len(pcm) / SAMPLE_RATE:.2f}s  峰值 {peak}  RMS {rms:.0f}  "
        f"归一化 {meta['gain_db']:+.2f} dB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
