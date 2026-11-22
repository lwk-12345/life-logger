import os
import json
import time
import subprocess
from pathlib import Path

whisper_bin = "/opt/homebrew/bin/whisper-cli"
# [2026-11-04 Q860] 模型文件路径不写死：LL_WHISPER_MODEL_BIN 优先，未设置时保留原值。
model_bin = os.environ.get("LL_WHISPER_MODEL_BIN") or "$HOME/models/work/transcription/ggml-large-v3-turbo.bin"
segments_dir = Path("output/local-web/watch-sessions/watch-41aa6b87347f4f0fb5238b559f705c4d/901BE63F70EF4937B42E836033B1A8F6/segments")
tmp_dir = Path("/tmp/whisper_test")
tmp_dir.mkdir(parents=True, exist_ok=True)

test_seqs = [0, 5, 28, 214]

results = []

for seq in test_seqs:
    m4a_path = segments_dir / f"segment-{seq:06d}.m4a"
    wav_path = tmp_dir / f"seg_{seq:06d}.wav"
    
    if not m4a_path.is_file():
        continue
        
    # Convert m4a to 16k wav
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(m4a_path), "-ar", "16000", "-ac", "1", str(wav_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True
    )
    
    t0 = time.time()
    # Run whisper-cli with large-v3-turbo
    proc = subprocess.run(
        [
            whisper_bin,
            "-m", model_bin,
            "-l", "zh",
            "-nt",  # no timestamps
            "-t", "4",  # 4 threads
            str(wav_path)
        ],
        capture_output=True,
        text=True
    )
    cost = time.time() - t0
    
    output_text = proc.stdout.strip()
    # Clean up output lines
    cleaned_lines = [line.strip() for line in output_text.splitlines() if line.strip() and not line.startswith("[") and not line.startswith("load_") and not line.startswith("ggml_") and not line.startswith("whisper_")]
    text = " ".join(cleaned_lines)
    
    results.append({
        "seq": seq,
        "turbo_text": text if text else output_text,
        "turbo_time": round(cost, 2),
        "returncode": proc.returncode,
    })

print("WHISPER_RESULTS_START")
print(json.dumps(results, ensure_ascii=False, indent=2))
print("WHISPER_RESULTS_END")
