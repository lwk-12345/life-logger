"""Shared local product pipeline for CLI and localhost web control plane."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from life_logger.ingest import discover_recordings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 单阶段（ASR / 声源归属 / 决策增强）的硬超时：防止子进程挂死导致整条流水线永久卡住。
# 1 小时对最长的整段录音转写也足够宽裕，只拦截真正的挂死。
_STAGE_TIMEOUT_SECONDS = 3600

RESULT_ARTIFACT_NAMES = {
    "daily.md",
    "events.json",
    "daily_summary.json",
    "recordings.json",
    "transcript.json",
    "transcript.md",
    "unknown-review.json",
    "source-attribution.json",
    "audio-classification.json",
    "speech-separation.json",
    "self-transcript.json",
    "non-self-transcript.json",
    "run-summary.json",
    "checkpoint.json",
    "life_logger.db",
    "jev-decisions.json",
    "decision-decisions.json",
    "context-extraction.json",
    "voice-input.json",
}


@dataclass(frozen=True)
class LocalPipelineFileResult:
    source: Path
    asr_dir: Path
    result_dir: Path
    artifacts: tuple[Path, ...]


def validate_local_pipeline_config(config: dict[str, Any]) -> None:
    """Validate resources required by the product MLX Whisper Turbo / Doubao + CAM++ pipeline."""
    asr_provider = str(config.get("asr_provider", "local_turbo")).lower()
    if asr_provider not in {"doubao", "volcano", "local_turbo", "turbo", "local",
                            "moss", "moss_transcribe_diarize", "moss-td"}:
        asr_provider = "local_turbo"
    required_keys = ("python", "template")
    for key in required_keys:
        value = config.get(key)
        if not value:
            raise ValueError(f"缺少本地配置 {key}")
        if not Path(str(value)).exists():
            raise ValueError(f"缺少本地资源 {key}: {value}")
    if not config.get("speaker"):
        raise ValueError("缺少本地模型名 speaker，例如 cam++")
    separation_model = str(config.get("speech_separation_model") or "").strip()
    if separation_model and not Path(separation_model).exists():
        raise ValueError(f"缺少本地资源 speech_separation_model: {separation_model}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("缺少 ffmpeg 或 ffprobe")


def local_pipeline_env(config: dict[str, Any]) -> dict[str, str]:
    """Return the same offline execution environment used by CLI and web."""
    env = dict(
        os.environ,
        HF_HUB_OFFLINE="1",
        MODELSCOPE_OFFLINE="1",
        PYTHONUNBUFFERED="1",
        OMP_NUM_THREADS=str(config.get("cpu_threads", 2)),
    )
    env.pop("PYTHONPATH", None)
    deepseek_key = (
        config.get("_deepseek_api_key")
        or (config.get("deepseek") if isinstance(config.get("deepseek"), dict) else {}).get("api_key")
        or os.environ.get("DEEPSEEK_API_KEY")
    )
    if not deepseek_key:
        secrets_path = PROJECT_ROOT / "output/local-web/secrets.json"
        if secrets_path.exists():
            try:
                secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
                if secrets.get("deepseek_api_key"):
                    deepseek_key = secrets["deepseek_api_key"]
            except Exception:
                pass
    if deepseek_key:
        env["DEEPSEEK_API_KEY"] = str(deepseek_key)
    # 端点/模型也下发给子进程：否则 attribute_funasr_transcript 会退回环境变量默认（DeepSeek 官网）
    raw_ds = config.get("deepseek")
    deepseek_cfg = raw_ds if isinstance(raw_ds, dict) else {}
    ds_base = str(deepseek_cfg.get("base_url") or os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
    ds_model = str(deepseek_cfg.get("model") or os.environ.get("DEEPSEEK_MODEL") or "").strip()
    if ds_base:
        env["DEEPSEEK_BASE_URL"] = ds_base.rstrip("/")
    if ds_model:
        env["DEEPSEEK_MODEL"] = ds_model
    return env


def build_asr_command(
    *,
    audio: Path,
    output_dir: Path,
    config: dict[str, Any],
    start_at: str | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> list[str]:
    raw_provider = str(config.get("asr_provider", "local_turbo")).lower()
    if raw_provider in {"doubao", "volcano"}:
        asr_provider = "doubao"
    elif raw_provider in {"moss", "moss_transcribe_diarize", "moss-td"}:
        asr_provider = "moss"
    else:
        asr_provider = "local_turbo"
    command = [
        str(config.get("python") or sys.executable),
        str(PROJECT_ROOT / "scripts/run_cloud_asr_pipeline.py"),
        "--audio",
        str(audio),
        "--output-dir",
        str(output_dir),
        "--provider",
        asr_provider,
        "--timezone",
        timezone_name,
    ]
    if start_at:
        command.extend(["--start-at", start_at])
    return command


def build_attribution_command(
    *,
    audio: Path,
    transcript: Path,
    recordings: Path,
    output_dir: Path,
    config: dict[str, Any],
    timezone_name: str = "Asia/Shanghai",
) -> list[str]:
    funasr_candidate = Path(
        "$HOME/.life-logger/.venv-funasr/bin/python"
    )
    python_bin = str(
        config.get("funasr_python")
        or (funasr_candidate if funasr_candidate.is_file() else (config.get("python") or sys.executable))
    )
    command = [
        python_bin,
        str(PROJECT_ROOT / "scripts/attribute_funasr_transcript.py"),
        "--audio",
        str(audio),
        "--transcript",
        str(transcript),
        "--recordings",
        str(recordings),
        "--template",
        str(config["template"]),
        "--speaker-model",
        (
            "cloud"
            if bool(config.get("cloud_speaker")) or str(config.get("speaker", "")).lower() == "cloud"
            else (
                str(config.get("speaker"))
                if config.get("speaker") and Path(str(config["speaker"])).is_dir()
                else next(
                    (
                        str(c)
                        for c in (
                            Path.home() / ".cache/modelscope/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master",
                            Path.home() / ".cache/modelscope/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master",
                            Path.home() / ".cache/modelscope/hub/models/iic/speech_campplus_sv_zh-cn_16k-common",
                            Path.home() / ".cache/modelscope/hub/iic/speech_campplus_sv_zh-cn_16k-common",
                        )
                        if c.is_dir()
                    ),
                    str(config.get("speaker", "cam++")),
                )
            )
        ),
        "--output-dir",
        str(output_dir),
        "--timezone",
        timezone_name,
        "--self-threshold",
        str(config.get("self_threshold", 0.45)),
        "--unknown-threshold",
        str(config.get("unknown_threshold", 0.25)),
        "--cpu-threads",
        str(config.get("cpu_threads", 2)),
    ]
    # dictation 时间窗索引：由 scripts/watch_airpassport.py --mark-only 维护。
    # 传给归因脚本后，与这些时间窗重合过半的句子不会生成生活事件（改为落进
    # voice-input.json），从而把「在电脑前语音输入」从生活日志里分流出去。
    dictation_windows = config.get("dictation_windows")
    if not dictation_windows:
        dictation_windows = (
            PROJECT_ROOT / "output" / "local-web" / "dictation-windows.jsonl"
        )
    if Path(str(dictation_windows)).is_file():
        command.extend(["--dictation-windows", str(dictation_windows)])
    raw_local_llm = config.get("local_llm")
    local_llm = raw_local_llm if isinstance(raw_local_llm, dict) else {}
    local_llm_model = str(local_llm.get("model") or "").strip()
    if bool(local_llm.get("enabled")) and local_llm_model:
        command.extend(["--local-llm-enabled", "--local-llm-model", local_llm_model])
        command.extend(
            [
                "--local-llm-url",
                str(local_llm.get("base_url") or "http://127.0.0.1:11434"),
                "--local-llm-review-confidence",
                str(local_llm.get("review_confidence") or 0.80),
            ]
        )
    raw_deepseek = config.get("deepseek")
    deepseek = raw_deepseek if isinstance(raw_deepseek, dict) else {}
    if bool(deepseek.get("enabled")):
        # 【2026-10-14 修】原来硬编码 "--deepseek-model deepseek-flash" —— 无视用户在
        # local-config.json 里配的 deepseek/deepseek-v4-flash；且从不传 base_url，
        # 导致下游只能落到模块级默认的官网端点。改为按配置传（缺省不覆盖）。
        command.append("--deepseek-enabled")
        _ds_model = str(deepseek.get("model") or "").strip()
        if _ds_model:
            command.extend(["--deepseek-model", _ds_model])
        _ds_base = str(deepseek.get("base_url") or "").strip()
        if _ds_base:
            command.extend(["--deepseek-base-url", _ds_base])
    if bool(config.get("cloud_speaker")) or str(config.get("speaker", "")).lower() == "cloud":
        command.append("--cloud-speaker")
    separation_model = str(config.get("speech_separation_model") or "").strip()
    if separation_model:
        command.extend(["--separation-model", separation_model])
    return command




def find_artifact(root: Path, name: str) -> Path:
    matches = sorted(root.rglob(name))
    if not matches:
        raise RuntimeError(f"处理完成但没有找到 {name}")
    return matches[0]


def collect_artifacts(*roots: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.name in RESULT_ARTIFACT_NAMES:
                paths.append(path)
    return tuple(sorted(set(paths)))


def _run_stage(name: str, command: list[str], *, env: dict[str, str]) -> None:
    try:
        completed = subprocess.run(
            command, env=env, check=False, timeout=_STAGE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"{name} 超时（>{_STAGE_TIMEOUT_SECONDS}s），已终止子进程"
        ) from error
    if completed.returncode:
        raise RuntimeError(f"{name} 失败，退出码 {completed.returncode}")


def load_local_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"缺少本地配置文件: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"本地配置文件不是有效 JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"本地配置必须是 JSON 对象: {path}")
    return payload


def run_local_product_pipeline(
    input_path: Path,
    output_dir: Path,
    *,
    config: dict[str, Any],
    timezone_name: str,
    user_start=None,
) -> list[LocalPipelineFileResult]:
    """Run the same Fun-ASR -> CAM++ -> events chain used by the web UI."""
    validate_local_pipeline_config(config)
    recordings = discover_recordings(
        input_path,
        timezone_name=timezone_name,
        user_start=user_start,
    )
    if not recordings:
        raise ValueError(
            f"no supported audio files found under {input_path}; "
            "copy an audio file there or pass a file path"
        )

    env = local_pipeline_env(config)
    results: list[LocalPipelineFileResult] = []
    for recording in recordings:
        audio = Path(recording.source_file)
        file_root = output_dir / "files" / recording.recording_id
        asr_dir = file_root / "asr"
        result_dir = file_root / "result"
        asr_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)

        _run_stage(
            "asr",
            build_asr_command(
                audio=audio,
                output_dir=asr_dir,
                config=config,
                start_at=recording.started_at,
                timezone_name=timezone_name,
            ),
            env=env,
        )
        transcript = find_artifact(asr_dir, "transcript.json")
        recording_manifest = find_artifact(asr_dir, "recordings.json")
        _run_stage(
            "attribution",
            build_attribution_command(
                audio=audio,
                transcript=transcript,
                recordings=recording_manifest,
                output_dir=result_dir,
                config=config,
                timezone_name=timezone_name,
            ),
            env=env,
        )
        results.append(
            LocalPipelineFileResult(
                source=audio,
                asr_dir=asr_dir,
                result_dir=result_dir,
                artifacts=collect_artifacts(asr_dir, result_dir),
            )
        )
    return results
