"""PANNs（AudioSet 527 类）环境声标注 —— 场景 + 声学事件。

职责（和已有的 audio_classifier.py 不同，别混）：
  * audio_classifier.py：手写信号特征，只分 4 类（speech/music/noise/stream），10 秒窗
  * 本模块：PANNs CNN14，AudioSet 527 类，回答「我在哪（场景）」+「刚才有什么声音（事件）」

设计要点：
1. **独立 venv + subprocess**：PANNs 需要 torch + panns_inference，**绝不能装进 8765 的
   worker venv**。所以这里用 subprocess 调独立环境，跟 MOSS / Kev 同一手法。
2. **场景映射直接用官方类名**（AudioSet 的 display_name），不自己编一套 —— 官方类别表里
   本来就有 `Subway, metro, underground` / `Inside, small room` / `Chatter` 这些场景类。
3. 输出是「打分」而不是硬结论：调用方可以拿去做多源融合（WiFi / 位置 / 声音）。

红线：本模块只在被显式调用时工作，不改动现有流水线的默认行为。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_panns_python() -> str:
    raw = os.environ.get("LIFE_LOGGER_PANNS_PYTHON", "").strip()
    if raw and Path(raw).exists():
        return raw
    candidates = (
        "$HOME/.life-logger/.venv-funasr/bin/python",
        "$HOME/.life-logger/.venv-panns/bin/python",
    )
    for c in candidates:
        if Path(c).exists():
            return c
    return candidates[0]


PANNS_PYTHON = resolve_panns_python()
# [2026-11-07] 默认仍是 Cnn14：MobileNetV1 权重已修好并可加载，但 20 段对比里
# 场景一致率仅 50%，按同一批样本的粗略真值 2/20 vs Cnn14 8/20，暂不胜任默认。
# 需要实验时设 LIFE_LOGGER_PANNS_MODEL=MobileNetV1 + MobileNetV1 权重路径。
PANNS_MODEL = os.environ.get("LIFE_LOGGER_PANNS_MODEL", "Cnn14")
PANNS_MODEL_FALLBACK = os.environ.get("LIFE_LOGGER_PANNS_MODEL_FALLBACK", "MobileNetV1")
PANNS_CKPT = os.environ.get(
    "LIFE_LOGGER_PANNS_CKPT",
    os.path.expanduser("~/panns_data/Cnn14_mAP=0.431.pth"),
)
PANNS_CKPT_FALLBACK = os.environ.get(
    "LIFE_LOGGER_PANNS_CKPT_FALLBACK",
    os.path.expanduser("~/panns_data/Cnn14_mAP=0.431.pth"),
)
# 每条录音只抽固定条数窗口，而不是 2s 窗/1s 跳密扫整段：
# 一个 10 分钟的手表段密扫 = 600 次前向，抽样 12 次就够定场景（场景是分钟级稳定量）。
PANNS_MAX_WINDOWS = int(os.environ.get("LIFE_LOGGER_PANNS_MAX_WINDOWS", "12"))
PANNS_SAMPLE_SECONDS = float(os.environ.get("LIFE_LOGGER_PANNS_SAMPLE_SECONDS", "60.0"))
# 窗口与跳跃：官方 YAMNet/PANNs 惯例是 ~1s 窗、0.5s 跳；这里用 2s 窗 / 1s 跳，够稳也够快
PANNS_WINDOW_SECONDS = float(os.environ.get("LIFE_LOGGER_PANNS_WINDOW_SECONDS", 2.0))
PANNS_HOP_SECONDS = float(os.environ.get("LIFE_LOGGER_PANNS_HOP_SECONDS", 1.0))

# 场景 → 官方 AudioSet display_name 片段（逐条来自 class_labels_indices.csv，不要自创）
SCENE_CLASS_HINTS: dict[str, tuple[str, ...]] = {
    "地铁": ("Subway, metro, underground", "Rail transport", "Railroad car, train wagon",
             "Train wheels squealing", "Train horn"),
    "马路/户外": ("Traffic noise, roadway noise", "Outside, urban or manmade", "Vehicle",
                "Car", "Motorcycle", "Bus", "Truck", "Vehicle horn, car horn, honking",
                "French horn", "Air horn, truck horn", "Siren"),
    # [2026-11-07] AudioSet 527 类里**没有** Restaurant/Cafeteria/Office/Kettle，
    # 原写法永远匹配不上（实测 6 个死标签）。这里换成真实存在的近义类。
    "咖啡厅/餐厅": ("Chatter", "Dishes, pots, and pans", "Cutlery, silverware",
                 "Sink (filling or washing)", "Clatter", "Male speech, man speaking",
                 "Crowd"),
    "办公室": ("Typing", "Computer keyboard", "Printer", "Inside, small room",
             "Inside, large room or hall", "Telephone", "Beep, bleep", "Mouse"),
    "家里/室内": ("Inside, small room", "Inside, large room or hall", "Inside, public space",
               "Television", "Vacuum cleaner", "Dishes, pots, and pans", "Domestic animals, pets"),
    "户外自然": ("Outside, rural or natural", "Bird", "Wind", "Rain", "Stream",
               "Waterfall", "Animal"),
}

# 声学事件 → 官方类名（瞬时类）
EVENT_CLASS_HINTS: dict[str, tuple[str, ...]] = {
    "掌声": ("Applause",),
    "笑声": ("Laughter", "Baby laughter"),
    "音乐": ("Music", "Musical instrument", "Singing", "Background music"),
    "关门/门": ("Door", "Doorbell", "Knock"),
    "咳嗽/喷嚏": ("Cough", "Sneeze", "Sniff", "Throat clearing"),
    "脚步": ("Walk, footsteps",),
    "电话/通知": ("Telephone", "Ringtone", "Beep, bleep", "Alarm", "Bell"),
    "车辆": ("Vehicle", "Car", "Motorcycle", "Bus", "Truck", "Aircraft"),
    "厨房": ("Dishes, pots, and pans", "Frying (food)", "Microwave oven",
           "Sink (filling or washing)", "Blender"),
    "打字": ("Typing", "Computer keyboard", "Mouse"),
}


class PannsUnavailable(RuntimeError):
    """PANNs 环境或权重不可用。"""


def panns_available() -> tuple[bool, str]:
    if not Path(PANNS_PYTHON).exists():
        return False, f"PANNs python 不存在: {PANNS_PYTHON}"
    if not Path(PANNS_CKPT).exists():
        return False, f"PANNs 权重不存在: {PANNS_CKPT}"
    return True, "ok"


def resolve_checkpoint() -> tuple[str, str, str | None]:
    """返回 (model, ckpt, warning)。

    首选 MobileNetV1；权重读不出来（下载截断等）就退回 Cnn14，并带上 warning，
    绝不因为场景模型不可用而让正式转写失败。
    """
    if Path(PANNS_CKPT).exists():
        return PANNS_MODEL, str(PANNS_CKPT), None
    if PANNS_MODEL != PANNS_MODEL_FALLBACK and Path(PANNS_CKPT_FALLBACK).exists():
        return PANNS_MODEL_FALLBACK, str(PANNS_CKPT_FALLBACK), (
            f"{PANNS_MODEL} 权重不可用，已退回 {PANNS_MODEL_FALLBACK}"
        )
    return PANNS_MODEL, str(PANNS_CKPT), None


_WORKER = r'''
import sys, json, os, warnings
warnings.filterwarnings("ignore")
import numpy as np, soundfile as sf
from panns_inference import AudioTagging, labels
wav_path, outp, win, hop = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
ckpt = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None
model = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else None
max_windows = int(sys.argv[7]) if len(sys.argv) > 7 else 12
fb_model = sys.argv[8] if len(sys.argv) > 8 and sys.argv[8] else None
fb_ckpt = sys.argv[9] if len(sys.argv) > 9 and sys.argv[9] else None
used = {"model": model, "checkpoint": os.path.basename(ckpt or ""), "warning": None}
# 注意：本机 panns_inference 0.1.0 只带 Cnn14 结构（models 里没有 MobileNetV1），
# 而且 `model=` 参数要的是**已构造的模型对象**，不是名字 —— 所以这里只用 checkpoint 自动识别。
def build_at(model_name, checkpoint):
    """按名字构造模型；MobileNetV1 用官方结构（panns_inference 0.1.0 不带）。

    注意：不能把 MobileNetV1 的 23MB 权重交给 AudioTagging(checkpoint_path=...)，
    因为该库看到文件 <3e8 会**自动重新下载 Cnn14 覆盖它**。这里手工 load 权重，
    再用 AudioTagging 的 inference 包装器（不跑其 __init__ 的下载分支）。
    """
    if model_name == "MobileNetV1":
        from life_logger.panns_mobilenet import load_mobilenet
        net = load_mobilenet(checkpoint, classes_num=527)
        wrapper = AudioTagging.__new__(AudioTagging)
        wrapper.model = net
        wrapper.device = "cpu"
        wrapper.labels = labels
        wrapper.classes_num = 527
        return wrapper
    return AudioTagging(checkpoint_path=checkpoint if checkpoint and os.path.exists(checkpoint) else None,
                        device="cpu")

try:
    at = build_at(model, ckpt)
except Exception as exc:                      # 权重损坏/下载截断 → 退回备用模型，绝不中断
    if not (fb_ckpt and os.path.exists(fb_ckpt)):
        raise
    used["model"] = fb_model
    used["checkpoint"] = os.path.basename(fb_ckpt)
    used["warning"] = f"{model} 加载失败（{type(exc).__name__}: {str(exc)[:120]}），已退回 {fb_model}"
    at = build_at(fb_model, fb_ckpt)
wav, sr = sf.read(wav_path)
if wav.ndim > 1:
    wav = wav.mean(axis=1)
wav = wav.astype(np.float32)
W, H = int(win * sr), int(hop * sr)
# 抽样：整段均匀取 max_windows 个窗口（场景是分钟级稳定量，不需要密扫）
total = max(0, len(wav) - W)
if max_windows > 0 and len(wav) > W:
    step = max(W, total // max(1, max_windows - 1)) if max_windows > 1 else total
    starts = list(range(0, total + 1, step))[:max_windows]
else:
    starts = list(range(0, len(wav), H))
windows = []
for i in starts:
    seg = wav[i:i+W]
    if len(seg) < int(0.5 * sr):
        break
    clip, _ = at.inference(seg[None, :])
    top = np.argsort(clip[0])[::-1][:12]
    windows.append({
        "start": round(i / sr, 2),
        "end": round((i + len(seg)) / sr, 2),
        "top": [{"label": labels[j], "score": round(float(clip[0][j]), 4)} for j in top],
    })
json.dump({"sample_rate": sr, "windows": windows, **used}, open(outp, "w"), ensure_ascii=False)
'''


def annotate(audio: Path, *, timeout: int = 3600,
             max_windows: int | None = None) -> dict[str, Any]:
    """跑 PANNs，返回 {windows:[...], scene:{...}, events:[...], elapsed_seconds}。"""
    ok, why = panns_available()
    if not ok:
        raise PannsUnavailable(why)
    model, ckpt, warning = resolve_checkpoint()
    max_windows = PANNS_MAX_WINDOWS if max_windows is None else int(max_windows)

    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="ll-panns-"))
    wav = tmp / "in.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-ar", "32000", "-ac", "1",
         "-c:a", "pcm_s16le", str(wav)],
        capture_output=True, text=True,
        timeout=900,
)
    if not wav.exists():
        raise PannsUnavailable(f"ffmpeg 转码失败: {audio}")

    worker = tmp / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    outp = tmp / "out.json"
    env = dict(os.environ, PYTHONUNBUFFERED="1",
               PYTHONPATH=str(PROJECT_ROOT / "src") + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    p = subprocess.run(
        [PANNS_PYTHON, str(worker), str(wav), str(outp),
         str(PANNS_WINDOW_SECONDS), str(PANNS_HOP_SECONDS), str(ckpt), str(model), str(max_windows),
         str(PANNS_MODEL_FALLBACK), str(PANNS_CKPT_FALLBACK)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if not outp.exists():
        raise PannsUnavailable(f"PANNs 未产出 (exit={p.returncode}): {(p.stderr or '')[-400:]}")

    data = json.loads(outp.read_text(encoding="utf-8"))
    windows = data.get("windows") or []
    if data.get("warning"):
        warning = data["warning"]
    model = data.get("model") or model
    ckpt = str(tmp / "x") if False else (data.get("checkpoint") or Path(ckpt).name)

    # 聚合：每个窗口的最高分，按场景/事件的官方类名匹配累计
    scene_scores: dict[str, float] = {k: 0.0 for k in SCENE_CLASS_HINTS}
    event_scores: dict[str, float] = {k: 0.0 for k in EVENT_CLASS_HINTS}
    event_peaks: dict[str, float] = {}
    for w in windows:
        by_label = {t["label"]: t["score"] for t in w.get("top") or []}
        for scene, hints in SCENE_CLASS_HINTS.items():
            best = max((by_label.get(h, 0.0) for h in hints), default=0.0)
            scene_scores[scene] += best
        for ev, hints in EVENT_CLASS_HINTS.items():
            best = max((by_label.get(h, 0.0) for h in hints), default=0.0)
            event_scores[ev] += best
            if best > event_peaks.get(ev, 0.0):
                event_peaks[ev] = best

    n = max(1, len(windows))
    scenes = {k: round(v / n, 4) for k, v in scene_scores.items()}
    ranked = sorted(scenes.items(), key=lambda kv: kv[1], reverse=True)
    events = [
        {"event": k, "mean_score": round(v / n, 4), "peak_score": round(event_peaks.get(k, 0.0), 4),
         "windows": sum(1 for w in windows
                        if any(t["label"] in EVENT_CLASS_HINTS[k] and t["score"] > 0.15
                               for t in (w.get("top") or [])))}
        for k, v in sorted(event_scores.items(), key=lambda kv: kv[1], reverse=True)
        if v > 0
    ]

    return {
        "policy_version": "panns-audioset-527-v1",
        "model": model,
        "checkpoint": Path(str(ckpt)).name,
        "warning": warning,
        "sampled_windows": max_windows,
        "window_seconds": PANNS_WINDOW_SECONDS,
        "hop_seconds": PANNS_HOP_SECONDS,
        "window_count": len(windows),
        "scene": ranked[0][0] if ranked and ranked[0][1] > 0 else None,
        "scene_ranked": ranked,
        "events": events[:8],
        "windows": windows,
        "elapsed_seconds": round(time.time() - t0, 3),
    }


# ───────────────────────────────────────────── 产物字段（接线用）
# 【2026-11-07 Q977】给流水线用的**唯一入口**：一段音频 → 一条可以直接写进产物的字段 dict。
#
# 为什么要单独一个函数（而不是让调用方直接用 annotate()）：
#   1. annotate() 会把**每个窗口的 top12** 都带回来（60 秒段 = 60 窗口），直接塞进
#      run-summary.json / state.json 会把产物撑大几十倍，而产物是要长期留存的；
#   2. 这个函数**永不抛异常**（fail-open）：PANNs 环境缺失、模型崩了、ffmpeg 失败，
#      都只写 `scene_status`，绝不能让转写主流程因为一个附加标注而失败；
#   3. 字段统一带 `scene_` 前缀，和既有的 audio_classifier 字段（speech/music/noise）不冲突。
SCENE_POLICY_VERSION = "panns-audioset-527-v1"


def scene_fields(
    audio: str | Path,
    *,
    include_windows: bool = False,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """跑 PANNs，返回**要写进产物**的那一条字段 dict（fail-open，永不抛异常）。

    成功（scene_status="ok"）：
        scene_policy / scene_status / scene / scene_confidence / scene_top[<=3] /
        scene_events[<=5] / scene_window_seconds / scene_hop_seconds /
        scene_windows / scene_elapsed_seconds
        （include_windows=True 时额外带 scene_windows_detail —— 只给离线排查用，别写产物）
    失败：
        scene_policy / scene_status = "unavailable" | "error" / scene_error（截断 200 字）
    """
    base: dict[str, Any] = {"scene_policy": SCENE_POLICY_VERSION}

    ok, why = panns_available()
    if not ok:
        return {**base, "scene_status": "unavailable", "scene_error": str(why)[:200]}

    try:
        result = annotate(Path(audio), timeout=timeout_seconds)
    except Exception as error:  # noqa: BLE001 - 附加标注不允许影响转写
        return {**base, "scene_status": "error",
                "scene_error": f"{type(error).__name__}: {error}"[:200]}

    ranked = [(str(name), float(score)) for name, score in (result.get("scene_ranked") or [])]
    top = [{"scene": name, "score": round(score, 4)} for name, score in ranked[:3]]
    events = [
        {
            "event": str(item.get("event")),
            "mean_score": round(float(item.get("mean_score") or 0.0), 4),
            "peak_score": round(float(item.get("peak_score") or 0.0), 4),
            "windows": int(item.get("windows") or 0),
        }
        for item in (result.get("events") or [])[:5]
        if isinstance(item, dict)
    ]
    fields: dict[str, Any] = {
        **base,
        "scene_status": "ok",
        "scene": result.get("scene"),
        "scene_confidence": top[0]["score"] if top else None,
        "scene_top": top,
        "scene_events": events,
        "scene_window_seconds": result.get("window_seconds"),
        "scene_hop_seconds": result.get("hop_seconds"),
        "scene_windows": result.get("window_count"),
        "scene_elapsed_seconds": result.get("elapsed_seconds"),
    }
    if include_windows:
        fields["scene_windows_detail"] = result.get("windows") or []
    return fields


__all__ = ["PannsUnavailable", "panns_available", "annotate", "scene_fields",
           "SCENE_CLASS_HINTS", "EVENT_CLASS_HINTS", "SCENE_POLICY_VERSION"]
