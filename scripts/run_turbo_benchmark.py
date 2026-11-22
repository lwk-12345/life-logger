import os
import json
import time
from pathlib import Path
from faster_whisper import WhisperModel

# [2026-11-04 Q860] 模型目录不写死：LL_TURBO_MODEL_DIR 优先，未设置时保留原值。
model_dir = os.environ.get("LL_TURBO_MODEL_DIR") or "$HOME/.cache/modelscope/models/pengzhendong--faster-whisper-large-v3-turbo/snapshots/master"
segments_dir = Path("output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d/901BE63F70EF4937B42E836033B1A8F6/segments")

test_seqs = [0, 5, 28, 214]

print(f"Loading faster-whisper Large-v3-Turbo from: {model_dir}")
t0_load = time.time()
# CPU int8 mode for Mac
model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=4)
print(f"Model loaded in {time.time() - t0_load:.2f}s")

results = []

for seq in test_seqs:
    m4a_path = segments_dir / f"segment-{seq:06d}.m4a"
    if not m4a_path.is_file():
        continue
    
    t0 = time.time()
    segments, info = model.transcribe(
        str(m4a_path),
        language="zh",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    # Collect text
    text_chunks = [s.text.strip() for s in segments]
    cost = time.time() - t0
    full_text = " ".join(text_chunks)
    
    results.append({
        "seq": seq,
        "turbo_text": full_text,
        "turbo_time": round(cost, 2),
        "language": info.language,
        "language_probability": round(info.language_probability, 3)
    })
    print(f"Seq {seq} finished in {cost:.2f}s: {full_text[:60]}...")

print("FINAL_TURBO_RESULTS_START")
print(json.dumps(results, ensure_ascii=False, indent=2))
print("FINAL_TURBO_RESULTS_END")
