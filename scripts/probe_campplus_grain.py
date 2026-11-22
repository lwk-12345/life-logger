"""验证正确设计：CAM++ 应该算在「长段」上，不是逐短句算。

依据（技能库已有结论 + 今晚实测）：
- CAM++ 在短句上极噪：实测 0.46s 的短句相似度只有 0.181；逐 span 算时本人最低到 0.133。
- 正解是用 MOSS 的 diarization 标签把 span 合并成「(chunk, 说话人) 长段」再算一次，
  这就是设计里说的「让 CAM++ 从每 2 秒短句算一次，降级成每个长段落算一次」。

本脚本对比同一批音频的三种粒度：
  A) 逐 span（当前生产粒度）—— 复现已有的噪声
  B) 整段 60s
  C) 段内按 MOSS 说话人合并

只用读取，不写产物。
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
SR = 16000

# 人耳可判的锚点段（靠内容确认，不靠旧标签）：
#   19~27  = 01:11~01:19 用户在看 AI 技术视频（他本人明说「1:17 我在看视频」）
#   10/46  = 本人自己的话
#   4      = 本人（乌冬面相关）
ANCHORS = [
    ("视频", 19), ("视频", 20), ("视频", 21), ("视频", 22), ("视频", 23),
    ("视频", 24), ("视频", 25), ("视频", 26), ("视频", 27),
    ("本人", 4), ("本人", 10), ("本人", 46), ("本人", 49),
]


def to_wav16k(src: Path) -> np.ndarray:
    import wave

    out = Path(tempfile.mkdtemp(prefix="campplus-grain-")) / "a.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(SR), str(out)],
        check=True,
    )
    with wave.open(str(out), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def cos(tpl: np.ndarray, emb: np.ndarray) -> float:
    e = np.asarray(emb).reshape(-1)
    e = e / (np.linalg.norm(e) + 1e-9)
    return float(np.dot(tpl, e))


def main() -> int:
    from funasr import AutoModel

    model = AutoModel(model=str(CAMPPLUS_DIR), model_revision="master", disable_update=True)
    tpl = to_wav16k(TEMPLATE)
    tpl_emb = np.asarray(model.generate(input=tpl, extract_embedding=True)[0]["spk_embedding"])
    tpl_emb = tpl_emb.reshape(-1) / (np.linalg.norm(tpl_emb) + 1e-9)
    print(f"  ✅ 模板 {tpl_emb.shape[0]} 维\n", flush=True)

    manifest = json.loads((SESSION / "manifest.json").read_text(encoding="utf-8"))
    seg_start = {int(s["sequence"]): float(s.get("start_offset_ms") or 0) / 1000.0
                 for s in (manifest.get("segments") or [])}

    import wave

    tmpdir = Path(tempfile.mkdtemp(prefix="campplus-grain-"))

    def _write_wav(arr: np.ndarray, tag: str) -> Path:
        p = tmpdir / f"{tag}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
        return p

    def emb_of(arr: np.ndarray, tag: str) -> float:
        if len(arr) < 3200:
            return float("nan")
        # funasr 的 campplus 不接受 {"raw":..,"sample_rate":..} dict，会在
        # Kaldi.fbank(au.unsqueeze(0)) 炸 `'dict' object has no attribute 'unsqueeze'`。
        # 传文件路径是实测可行且最稳的方式（probe 1 即如此）。
        path = _write_wav(arr, tag)
        r = model.generate(input=str(path), extract_embedding=True)
        return cos(tpl_emb, r[0]["spk_embedding"])

    print(f"  {'期望':<5} {'段':<5} {'A逐span中位':>11} {'B整段60s':>9} {'C按说话人合并':>13}")
    results = []
    for expect, seq in ANCHORS:
        sf = SESSION / "formal-asr/segments" / f"{seq:06d}" / "state.json"
        audio = SESSION / "segments" / f"segment-{seq:06d}.m4a"
        if not (sf.is_file() and audio.is_file()):
            continue
        st = json.loads(sf.read_text(encoding="utf-8"))
        rows = [r for r in (st.get("rows") or []) if isinstance(r, dict) and r.get("text")]
        if not rows:
            continue
        base = seg_start.get(seq, 0.0)
        arr = to_wav16k(audio)

        # A) 逐 span
        per_span: list[float] = []
        n_span = 0
        for r in rows:
            s, e = r.get("start_offset_seconds"), r.get("end_offset_seconds")
            if s is None or e is None:
                continue
            i0 = max(0, int(round((float(s) - base) * SR)))
            i1 = min(len(arr), int(round((float(e) - base) * SR)))
            if i1 - i0 < 3200:
                continue
            per_span.append(emb_of(arr[i0:i1], f"s{seq}_span{n_span}"))
            n_span += 1

        # B) 整段
        whole = emb_of(arr, f"s{seq}_whole")

        # C) 按 MOSS 说话人合并（段内同一 speaker 的音频拼起来）
        by_spk: dict[str, list[np.ndarray]] = {}
        for r in rows:
            s, e = r.get("start_offset_seconds"), r.get("end_offset_seconds")
            spk = str(r.get("speaker") or "?")
            if s is None or e is None:
                continue
            i0 = max(0, int(round((float(s) - base) * SR)))
            i1 = min(len(arr), int(round((float(e) - base) * SR)))
            if i1 - i0 < 3200:
                continue
            by_spk.setdefault(spk, []).append(arr[i0:i1])
        merged = {spk: emb_of(np.concatenate(v), f"s{seq}_spk{spk}") for spk, v in by_spk.items() if v}

        a = float(np.median(per_span)) if per_span else float("nan")
        cbest = max(merged.values()) if merged else float("nan")
        results.append({"expect": expect, "seq": seq, "A": a, "B": whole, "C": cbest})
        print(f"  {expect:<5} {seq:<5} {a:>11.3f} {whole:>9.3f} {cbest:>13.3f}")

    print()
    for key, name in (("A", "逐 span（当前粒度）"), ("B", "整段 60s"), ("C", "按说话人合并")):
        vs = [r[key] for r in results if r["expect"] == "视频" and not np.isnan(r[key])]
        ss = [r[key] for r in results if r["expect"] == "本人" and not np.isnan(r[key])]
        if vs and ss:
            print(f"  {name:<22} 视频中位 {np.median(vs):>6.3f}   本人中位 {np.median(ss):>6.3f}   "
                  f"间隔 {np.median(ss)-np.median(vs):>6.3f}")
    Path("/tmp/campplus_grain.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
