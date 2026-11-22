"""批量对比：生产真实数据上，CAM++（192 维真声纹）vs 48 维手工特征。

目的：为「把 MOSS 路径的声纹换成 CAM++」提供阈值校准依据 ——
两者量纲不同（实测 48 维 self 0.65~0.82 / CAM++ self 0.55~0.69），
换算法必须同时重设 self/unknown 阈值，否则等于换个数字继续错。

做法：逐段解码一次音频 → 按 state.json 每行的 [start,end] 偏移切片（与生产同粒度，
生产在 moss_transcribe._attribute_spans 里就是按 MOSS diarization 的 span 切窗）
→ 算 CAM++ 余弦 → 与产物里已存的 48 维 voice_similarity 并排输出。

只读，不写任何产物。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import Counter
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
SR = 16000


def to_wav16k(src: Path) -> np.ndarray:
    import wave

    out = Path(tempfile.mkdtemp(prefix="campplus-batch-")) / "a.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(SR), str(out)],
        check=True,
    )
    with wave.open(str(out), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    from funasr import AutoModel

    print("  加载 CAM++ …", flush=True)
    model = AutoModel(model=str(CAMPPLUS_DIR), model_revision="master", disable_update=True)

    tpl = to_wav16k(TEMPLATE)
    tpl_emb = np.asarray(model.generate(input=tpl, extract_embedding=True)[0]["spk_embedding"])
    tpl_emb = tpl_emb.reshape(-1) / (np.linalg.norm(tpl_emb) + 1e-9)
    print(f"  ✅ 模板 embedding {tpl_emb.shape[0]} 维\n", flush=True)

    manifest = json.loads((SESSION / "manifest.json").read_text(encoding="utf-8"))
    seg_start = {
        int(s["sequence"]): float(s.get("start_offset_ms") or 0) / 1000.0
        for s in (manifest.get("segments") or [])
    }
    print(f"  manifest 提供 {len(seg_start)} 个段起点（start_offset_ms 是权威值，不能按 seq*60 算 —— "
          f"每段实际约 60.1s，会累积漂移）\n", flush=True)

    seg_files = sorted(SESSION.glob("formal-asr/segments/*/state.json"))
    rows_out: list[dict] = []
    for sf in seg_files:
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = [r for r in (st.get("rows") or []) if isinstance(r, dict) and r.get("text")]
        if not rows:
            continue
        seq = sf.parent.name
        base = seg_start.get(int(seq))
        if base is None:
            continue
        audio = SESSION / "segments" / f"segment-{seq}.m4a"
        if not audio.is_file():
            continue
        try:
            arr = to_wav16k(audio)
        except Exception as e:
            print(f"  段{seq}: 解码失败 {e}", file=sys.stderr)
            continue

        # 本段一次性把每个 span 的 wav 切片收集起来，批量送 CAM++
        slices: list[np.ndarray] = []
        meta: list[dict] = []
        for r in rows:
            s = r.get("start_offset_seconds")
            e = r.get("end_offset_seconds")
            if s is None or e is None:
                continue
            # state.json 的 offset 是「会话级」的，先减去段起点才是段内位置
            i0 = max(0, int(round((float(s) - base) * SR)))
            i1 = min(len(arr), int(round((float(e) - base) * SR)))
            if i1 - i0 < 3200:  # 与生产 VOICEPRINT_MIN_SAMPLES 同门限
                continue
            slices.append(arr[i0:i1])
            meta.append(
                {
                    "seq": seq,
                    "text": str(r.get("text"))[:34],
                    "old_48d": r.get("voice_similarity"),
                    "stored_label": r.get("source_label"),
                    "prox": r.get("acoustic_proximity"),
                }
            )
        if not slices:
            continue
        try:
            res = model.generate(
                input=[{"raw": np.asarray(x, dtype=np.float32), "sample_rate": SR} for x in slices],
                extract_embedding=True,
                batch_size=8,
            )
        except Exception:
            # 某些 funasr 版本不接受 raw 列表，退回逐条
            res = [model.generate(input=x, extract_embedding=True)[0] for x in slices]
        for m, r in zip(meta, res):
            emb = np.asarray(r["spk_embedding"]).reshape(-1)
            emb = emb / (np.linalg.norm(emb) + 1e-9)
            m["campplus"] = float(np.dot(tpl_emb, emb))
            rows_out.append(m)
        print(f"  段{seq}: {len(meta)} 窗", flush=True)

    out = Path("/tmp/campplus_batch.json")
    out.write_text(json.dumps(rows_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  共 {len(rows_out)} 条 → {out}")

    valid = [r for r in rows_out if r["old_48d"] is not None]
    print(f"\n  ── 有 48 维值的 {len(valid)} 条，按产物里的 stored_label 分组 ──")
    for lbl in ("self", "unknown", "non_self"):
        g = [r for r in valid if str(r.get("stored_label")) == lbl]
        if not g:
            continue
        c = np.array([r["campplus"] for r in g])
        o = np.array([float(r["old_48d"]) for r in g])
        print(
            f"  {lbl:<9} n={len(g):<4} CAM++ 中位 {np.median(c):.3f} "
            f"[{c.min():.3f},{c.max():.3f}]   48维 中位 {np.median(o):.3f} [{o.min():.3f},{o.max():.3f}]"
        )
    print(f"\n  stored_label 分布: {dict(Counter(str(r.get('stored_label')) for r in rows_out))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
