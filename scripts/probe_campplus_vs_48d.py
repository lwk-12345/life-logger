"""决定性实验：CAM++（真声纹）vs 48 维手工特征 —— 能否分开「视频里的人」与「本人」。

背景：生产链路（moss_transcribe.py / transcribe.py）用的是 48 维手工频谱包络余弦，
实测视频段的 raw 能到 0.68~0.82，比本人纠正段（0.57~0.65）还高 → 分不开。
moss_transcribe.py:246 的注释声称「MOSS 跑在独立 venv，装不进重依赖」，
但声纹代码是在**调用方的解释器**里跑的，而调用方是 config["python"] = .venv-funasr
（有 funasr 1.3.30 + CAM++ 权重）。本脚本验证这个前提，并给出真实分离数字。

只用读取，不写任何产物。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SESSION = (
    ROOT
    / "output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d"
    / "17BFCCD8F1E54F11BCE04639E6225DC9"
)
TEMPLATE = ROOT / "work/watch-2026-10-01/template-from-watch-self.wav"
CAMPPLUS_DIR = (
    Path.home()
    / ".cache/modelscope/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master"
)

# (标签, 段号, 期望, 该段的 48 维 raw 值)
TARGETS = [
    ("视频", "000019", "non_self", 0.416412),
    ("视频", "000020", "non_self", 0.299159),
    ("视频", "000024", "non_self", 0.6794),
    ("视频", "000026", "non_self", None),
    ("本人", "000010", "self", 0.653119),
    ("本人", "000046", "self", 0.823119),
]


def to_wav16k(src: Path) -> Path:
    """m4a/wav → 16k 单声道 wav（CAM++ 要求）。"""
    out = Path(tempfile.mkdtemp(prefix="campplus-probe-")) / "a.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(out)],
        check=True,
    )
    return out


def load_wav(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return arr


def main() -> int:
    from funasr import AutoModel

    print("  加载 CAM++ …", flush=True)
    model = AutoModel(model=str(CAMPPLUS_DIR), model_revision="master", disable_update=True)
    print("  ✅ 已加载\n", flush=True)

    tpl_wav = to_wav16k(TEMPLATE)
    tpl = model.generate(input=str(tpl_wav), extract_embedding=True)
    tpl_emb = np.asarray(tpl[0]["spk_embedding"]).reshape(-1)
    tpl_emb = tpl_emb / (np.linalg.norm(tpl_emb) + 1e-9)
    print(f"  模板 embedding 维度 = {tpl_emb.shape[0]}\n", flush=True)

    print("  ── CAM++ 余弦 vs 48 维 raw ──")
    print(f"  {'标签':<5} {'段':<7} {'期望':<9} {'CAM++':>8} {'48维raw':>9}  判定")
    rows = []
    for kind, seq, expect, old in TARGETS:
        src = SESSION / "segments" / f"segment-{seq}.m4a"
        if not src.is_file():
            print(f"  {kind:<5} {seq:<7} (音频缺失: {src.name})")
            continue
        try:
            wav = to_wav16k(src)
        except subprocess.CalledProcessError as e:
            print(f"  {kind:<5} {seq:<7} (转码失败: {e})")
            continue
        res = model.generate(input=str(wav), extract_embedding=True)
        emb = np.asarray(res[0]["spk_embedding"]).reshape(-1)
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        cos = float(np.dot(tpl_emb, emb))
        verdict = "✅" if (cos >= 0.5) == (expect == "self") else "❌错判"
        old_s = f"{old:.4f}" if old is not None else "?"
        print(f"  {kind:<5} {seq:<7} {expect:<9} {cos:>8.4f} {old_s:>9}  {verdict}")
        rows.append({"kind": kind, "seq": seq, "expect": expect, "campplus": cos, "old_48d": old})

    print()
    selfs = [r["campplus"] for r in rows if r["expect"] == "self"]
    medias = [r["campplus"] for r in rows if r["expect"] == "non_self"]
    if selfs and medias:
        print(f"  本人中位 = {np.median(selfs):.4f}   视频中位 = {np.median(medias):.4f}")
        print(f"  分离间隔 = {np.median(selfs) - np.median(medias):.4f}")
        print(f"  → CAM++ 能否分开(间隔>0.2 即认为能): {'✅ 能' if np.median(selfs)-np.median(medias) > 0.2 else '❌ 分不开'}")
    out = Path("/tmp/campplus_probe_result.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
