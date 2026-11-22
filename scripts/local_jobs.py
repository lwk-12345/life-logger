"""Local H8 queue: durable copies, stage recovery and exclusive execution."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from life_logger.local_pipeline import (
    RESULT_ARTIFACT_NAMES,
    build_asr_command,
    build_attribution_command,
    local_pipeline_env,
    validate_local_pipeline_config,
)
from life_logger.local_web import stage_severity

ROOT = Path(__file__).resolve().parents[1]


def validate_web_config(config):
    """Validate local-only resources immediately before a web job starts."""
    validate_local_pipeline_config(config)














def run_command(
    command,
    *,
    log_path=None,
    on_line=None,
    env=None,
    cwd=ROOT,
    child_pid_callback=None,
):
    """Run one pipeline stage while streaming durable progress to the caller."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log = None
    try:
        if child_pid_callback:
            tracked = child_pid_callback(process.pid)
            if tracked is False:
                raise RuntimeError("ASR child PID could not be recorded")
        log = Path(log_path).open("a", encoding="utf-8") if log_path else None
        assert process.stdout is not None
        for line in process.stdout:
            if log:
                log.write(line)
                log.flush()
            if on_line:
                on_line(line.rstrip())
        return process.wait()
    except BaseException:
        # A log/progress callback can fail while the model process is still alive.
        # Stop and reap it before JobStore releases the shared ASR slot.
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        if child_pid_callback:
            try:
                child_pid_callback(None)
            except Exception:
                pass
        if log:
            log.close()


def _find_artifact(root, name):
    matches = sorted(Path(root).rglob(name))
    if not matches:
        raise RuntimeError(f"处理完成但没有找到 {name}")
    return matches[0]


def _web_stage_commands(job_dir, item, config, *, timezone_name):
    file_root = Path(job_dir) / "files" / item["sha256"]
    audio = Path(job_dir) / item["stored_path"]
    asr = file_root / "asr"
    result = file_root / "result"
    asr_command = build_asr_command(
        audio=audio,
        output_dir=asr,
        config=config,
        start_at=item.get("effective_start_at"),
        timezone_name=timezone_name,
    )
    return file_root, audio, asr, result, asr_command


def _web_results(job_dir, file_root, asr, result):
    paths = []
    for root in (asr, result):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.name in RESULT_ARTIFACT_NAMES:
                paths.append(str(path.relative_to(job_dir)))
    return sorted(set(paths))


def _completed_empty_transcript(asr_dir: Path, transcript_path: Path) -> bool:
    """Return true only for an explicitly successful, structurally valid empty ASR result."""
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    if not isinstance(transcript, list):
        raise ValueError("ASR transcript must be a JSON list")
    if transcript:
        if not all(isinstance(row, dict) for row in transcript):
            raise ValueError("ASR transcript contains a malformed row")
        return False
    summary_paths = sorted(Path(asr_dir).rglob("run-summary.json"))
    if not summary_paths:
        return False
    summary = json.loads(summary_paths[0].read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or summary.get("status") != "completed":
        return False
    count = summary.get("transcript_count")
    return isinstance(count, int) and not isinstance(count, bool) and count == 0


def run_web_job(job_dir, config, callback, *, child_pid_callback=None):
    """Run uploaded files sequentially, preserving stage checkpoints in status."""
    status_path = Path(job_dir) / "status.json"
    state = json.loads(status_path.read_text(encoding="utf-8"))
    callback({"type": "job_started"})
    env = local_pipeline_env(config)
    current_index = None
    failed_files = []
    failed_indices = set()

    def prior_stage_error(item, stage):
        """上一轮跑这个阶段时留下的错误；没有则 None。

        重跑成功时把它回带给状态层，这样「曾经失败过」不会被 completion 抹掉。
        """
        stage_state = (item.get("stages") or {}).get(stage)
        if isinstance(stage_state, dict):
            return str(stage_state.get("error") or "").strip() or None
        return None

    def stage_failure_error(stage, return_code):
        """同一条失败文案，日志与结构化字段共用，避免两边说法不一致。"""
        return f"{stage} 失败，退出码 {return_code}"

    def mark_file_failed(index, item, stage, error):
        """记录当前文件失败，保留现场并让外层循环继续处理其他文件。"""
        failed_indices.add(index)
        item["error"] = error
        failed_files.append(
            {
                "file_index": index,
                "name": item.get("original_name"),
                "stage": stage,
                "error": error,
                "severity": stage_severity(stage),
            }
        )
        callback(
            {
                "type": "file_finished",
                "file_index": index,
                "status": "failed",
                "error": error,
            }
        )
        return True

    try:
        for index, item in enumerate(state["files"]):
            current_index = index
            if item.get("status") == "completed" and item.get("results"):
                callback(
                    {
                        "type": "file_finished",
                        "file_index": index,
                        "status": "completed",
                        "results": item.get("results", []),
                    }
                )
                continue
            file_root, audio, asr, result, asr_command = _web_stage_commands(
                job_dir,
                item,
                config,
                timezone_name=state.get("timezone", "Asia/Shanghai"),
            )
            file_root.mkdir(parents=True, exist_ok=True)
            stages = [
                ("asr", asr_command),
            ]
            for stage, command in stages:
                if item.get("stages", {}).get(stage, {}).get("status") == "completed":
                    continue
                begun = time.time()
                previous_error = prior_stage_error(item, stage)
                callback({"type": "stage_started", "file_index": index, "stage": stage})
                pattern = re.compile(r"window\s+(\d+)/(\d+)")

                def on_line(line, file_index=index, stage_name=stage):
                    callback(
                        {
                            "type": "log",
                            "file_index": file_index,
                            "stage": stage_name,
                            "line": line,
                        }
                    )
                    match = pattern.search(line)
                    if match:
                        callback(
                            {
                                "type": "progress",
                                "file_index": file_index,
                                "completed": int(match.group(1)),
                                "total": int(match.group(2)),
                                "unit": "windows",
                            }
                        )

                return_code = run_command(
                    command,
                    log_path=file_root / f"{stage}.log",
                    on_line=on_line,
                    env=env,
                    child_pid_callback=child_pid_callback,
                )
                elapsed = round(time.time() - begun, 2)
                if return_code:
                    error = stage_failure_error(stage, return_code)
                    callback(
                        {
                            "type": "stage_finished",
                            "file_index": index,
                            "stage": stage,
                            "status": "failed",
                            "elapsed_seconds": elapsed,
                            "error": error,
                            "severity": stage_severity(stage),
                        }
                    )
                    # break 只跳出一层循环；真正的现状判定交给下面的 failed_indices。
                    mark_file_failed(index, item, stage, error)
                    break
                else:
                    callback(
                        {
                            "type": "stage_finished",
                            "file_index": index,
                            "stage": stage,
                            "status": "completed",
                            "elapsed_seconds": elapsed,
                            "recovered_from": previous_error,
                        }
                    )

            if index in failed_indices:
                current_index = None
                continue

            transcript = _find_artifact(asr, "transcript.json")
            if _completed_empty_transcript(asr, transcript):
                reason = "ASR 已完成且无语音；原音频已保留，跳过声源归属与事件提取。"
                callback(
                    {
                        "type": "stage_finished",
                        "file_index": index,
                        "stage": "attribution",
                        "status": "skipped",
                        "reason": reason,
                    }
                )
                callback(
                    {
                        "type": "file_finished",
                        "file_index": index,
                        "status": "completed",
                        "outcome": "no_speech",
                        "outcome_reason": reason,
                        "results": _web_results(job_dir, file_root, asr, result),
                    }
                )
                current_index = None
                continue
            recordings = _find_artifact(asr, "recordings.json")
            attribution_command = build_attribution_command(
                audio=audio,
                transcript=transcript,
                recordings=recordings,
                output_dir=result,
                config=config,
                timezone_name=state.get("timezone", "Asia/Shanghai"),
            )
            attribution_done = (
                item.get("stages", {}).get("attribution", {}).get("status")
                == "completed"
            )
            if not attribution_done:
                begun = time.time()
                previous_error = prior_stage_error(item, "attribution")
                callback(
                    {
                        "type": "stage_started",
                        "file_index": index,
                        "stage": "attribution",
                    }
                )
                pattern = re.compile(r"window\s+(\d+)/(\d+)")

                def on_attribution_line(line, file_index=index):
                    callback(
                        {
                            "type": "log",
                            "file_index": file_index,
                            "stage": "attribution",
                            "line": line,
                        }
                    )
                    match = pattern.search(line)
                    if match:
                        callback(
                            {
                                "type": "progress",
                                "file_index": file_index,
                                "completed": int(match.group(1)),
                                "total": int(match.group(2)),
                                "unit": "windows",
                            }
                        )

                return_code = run_command(
                    attribution_command,
                    log_path=file_root / "attribution.log",
                    on_line=on_attribution_line,
                    env=env,
                    child_pid_callback=child_pid_callback,
                )
                elapsed = round(time.time() - begun, 2)
                if return_code:
                    error = stage_failure_error("attribution", return_code)
                    callback(
                        {
                            "type": "stage_finished",
                            "file_index": index,
                            "stage": "attribution",
                            "status": "failed",
                            "elapsed_seconds": elapsed,
                            "error": error,
                            "severity": stage_severity("attribution"),
                        }
                    )
                    mark_file_failed(index, item, "attribution", error)
                    current_index = None
                    continue
                callback(
                    {
                        "type": "stage_finished",
                        "file_index": index,
                        "stage": "attribution",
                        "status": "completed",
                        "elapsed_seconds": elapsed,
                        "recovered_from": previous_error,
                    }
                )

            callback(
                {
                    "type": "file_finished",
                    "file_index": index,
                    "status": "completed",
                    "results": _web_results(job_dir, file_root, asr, result),
                }
            )
        if failed_files:
            # 有文件被跳过：结果照常保留，但终态不再冒充 completed。跳过的文件在
            # warnings 里点名，任务级状态由状态层按严重性判为 partial/degraded
            # （见 life_logger.local_web.apply_file_outcomes），网页端可单独重试。
            callback(
                {
                    "type": "job_finished",
                    "status": "partial",
                    "warnings": failed_files,
                }
            )
        else:
            callback({"type": "job_finished", "status": "completed"})
    except Exception as error:
        if current_index is not None:
            callback(
                {
                    "type": "file_finished",
                    "file_index": current_index,
                    "status": "failed",
                    "error": str(error),
                }
            )
        callback({"type": "job_finished", "status": "failed", "error": str(error)})


def save(path, value):
    # 唯一临时名 + fsync + replace（原实现固定 `<name>.tmp`，并发 save 会互相覆盖）
    import sys
    from pathlib import Path as _Path
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from life_logger.atomic import write_json_atomic

    write_json_atomic(_Path(path), value, indent=2)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(config, check=False):
    for key in ("python", "asr", "vad", "template"):
        if not Path(config[key]).exists():
            raise ValueError(f"缺少本地资源 {key}: {config[key]}")
    if not config.get("speaker"):
        raise ValueError("缺少本地模型名 speaker，例如 cam++")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("缺少 ffmpeg 或 ffprobe")
    source = Path(config["input"])
    if not source.is_dir():
        raise ValueError(f"请连接录音设备：{source}")
    files = sorted(
        p
        for p in source.iterdir()
        if p.suffix.lower() == ".wav" and not p.name.startswith(".")
    )
    print(f"发现 {len(files)} 个 WAV 文件", flush=True)
    if check:
        return
    queue = ROOT / "output/local-jobs"
    queue.mkdir(parents=True, exist_ok=True)
    with (queue / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for audio in files:
            # H8 names are device timestamps, not independently verified dates.
            start = datetime.strptime(audio.stem, "%Y%m%d_%H%M%S")
            key = digest(audio)
            job = queue / key
            job.mkdir(exist_ok=True)
            state_path = job / "status.json"
            state = (
                json.loads(state_path.read_text())
                if state_path.exists()
                else {
                    "source": str(audio),
                    "sha256": key,
                    "stages": {},
                    "time_source": "device_filename_unverified",
                }
            )
            if state.get("status") == "completed":
                print(f"已完成，跳过 {audio.name}", flush=True)
                continue
            local = job / audio.name
            if not local.exists():
                temporary = job / "copy.partial"
                shutil.copyfile(audio, temporary)
                if digest(temporary) != key:
                    raise ValueError("设备文件复制校验失败，请重试")
                temporary.replace(local)
            day = start.date().isoformat()
            asr = job / "asr"
            final = job / "result"
            commands = {
                "asr": build_asr_command(
                    audio=local,
                    output_dir=asr,
                    config=config,
                    start_at=start.isoformat() + "+08:00",
                ),
                "source": (
                    [
                        config["python"],
                        str(ROOT / "scripts/attribute_funasr_transcript.py"),
                        "--audio",
                        str(local),
                        "--transcript",
                        str(asr / day / "transcript.json"),
                        "--recordings",
                        str(asr / day / "recordings.json"),
                        "--speaker-model",
                        config.get("speaker", "cloud"),
                        "--output-dir",
                        str(final),
                        "--self-threshold",
                        "0.45",
                        "--unknown-threshold",
                        "0.25",
                        "--cpu-threads",
                        "2",
                        "--cloud-speaker",
                    ]
                    if (config.get("cloud_speaker") or str(config.get("speaker")).lower() == "cloud")
                    else [
                        config["python"],
                        str(ROOT / "scripts/attribute_funasr_transcript.py"),
                        "--audio",
                        str(local),
                        "--transcript",
                        str(asr / day / "transcript.json"),
                        "--recordings",
                        str(asr / day / "recordings.json"),
                        "--template",
                        config["template"],
                        "--speaker-model",
                        config["speaker"],
                        "--output-dir",
                        str(final),
                        "--self-threshold",
                        "0.45",
                        "--unknown-threshold",
                        "0.25",
                        "--cpu-threads",
                        "2",
                    ]
                ),
            }
            env = dict(
                os.environ,
                HF_HUB_OFFLINE="1",
                MODELSCOPE_OFFLINE="1",
                PYTHONUNBUFFERED="1",
                OMP_NUM_THREADS="2",
            )
            env.pop("PYTHONPATH", None)
            for stage, command in commands.items():
                if state["stages"].get(stage, {}).get("status") == "completed":
                    continue
                begun = time.time()
                state.update(status="running", current_stage=stage)
                state["stages"][stage] = {"status": "running", "started_at": begun}
                save(state_path, state)
                print(
                    f"{audio.name}：{stage}；日志 {job / (stage + '.log')}",
                    flush=True,
                )
                with (job / (stage + ".log")).open("a") as log:
                    result = subprocess.run(
                        command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
                    )
                state["stages"][stage].update(
                    status="completed" if result.returncode == 0 else "failed",
                    elapsed_seconds=round(time.time() - begun, 2),
                )
                state["status"] = "running" if result.returncode == 0 else "failed"
                save(state_path, state)
                if result.returncode:
                    raise RuntimeError(f"{audio.name} {stage} 失败；重试会恢复断点")
            state["status"] = "completed"
            save(state_path, state)
            print(f"完成：{final}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        run(json.loads((ROOT / "local-config.json").read_text()), args.check)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"任务未完成：{error}", file=sys.stderr)
        sys.exit(1)
