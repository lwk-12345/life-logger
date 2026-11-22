#!/usr/bin/env python3
"""自动把 AirPassport 的按住说话录音接进 Life Logger 处理队列。

AirPassport 的 macOS 接收端只会把每段语音落盘成
``~/AirPassportRecordings/ptt_<epoch>.wav``（见 desktop/AirPassportReceiver
/ Sources/AirPassportMenu/main.swift），它不推任何网络请求；Life Logger 的
passport-stream 接收器（TCP 8766 / UDP 8767）等的是固件的 LL_DISCOVER_V1
发现包，两边不会自动碰面。这个守护进程就是补上中间那段胶水：

  扫描新切片 → 按静默间隔切出「一次说话」→ 建任务 → 上传 → 启动 → 记状态

会话边界规则（实测：单切片中位 1.3s、最长 24s，相邻切片间隔中位 3.8s、
p90 152s，所以静默间隔是可靠的边界信号）：

  * 相邻切片间隔 <= ``--idle-seconds``（默认 120s）视为同一段
  * 单段跨度上限 ``--max-session-seconds``（默认 600s），防止持续说话时无限堆积
  * 切片落盘后静止 ``--stability-seconds``（默认 10s）才认为写完

状态写在 ``--state``（默认 output/local-web/airpassport-watch.json），按
会话起始时间戳去重，重启不会重复处理，处理失败会按 ``--max-attempts`` 重试。

注意：不要同时开启网页端的「自动导入」并指向同一个目录，那会把每个切片
单独建成一个任务，和这里的会话切分互相打架。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable



PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT / "src") not in sys.path:  # 允许从任意 cwd 运行
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
from life_logger.timeutil import LOCAL_TZ  # noqa: E402  [Q479-P0] 全仓统一北京时区
DEFAULT_RECORDINGS = Path.home() / "AirPassportRecordings"
DEFAULT_STATE = PROJECT_ROOT / "output" / "local-web" / "airpassport-watch.json"
DEFAULT_SESSIONS_DIR = PROJECT_ROOT / "output" / "local-web" / "airpassport-sessions"
DEFAULT_DICTATION_WINDOWS = PROJECT_ROOT / "output" / "local-web" / "dictation-windows.jsonl"
STATE_SCHEMA = "airpassport-watch-v1"
CLIP_PREFIX = "ptt_"
FAILED_STATUSES = {"failed", "interrupted"}


# --------------------------------------------------------------------------
# 切片发现与读取
# --------------------------------------------------------------------------


@dataclass
class Clip:
    path: Path
    started_at: float
    duration: float

    @property
    def ended_at(self) -> float:
        return self.started_at + self.duration


def parse_clip_timestamp(path: Path) -> float | None:
    """从 ``ptt_<epoch>.wav`` 取出录音结束时刻；文件名不合规返回 None。"""
    stem = path.stem
    if not stem.startswith(CLIP_PREFIX):
        return None
    raw = stem[len(CLIP_PREFIX) :]
    if not raw.isdigit():
        return None
    value = int(raw)
    # 只接受合理的 Unix 秒，挡掉 ptt_1.wav 这类垃圾文件
    if not 1_000_000_000 <= value <= 4_000_000_000:
        return None
    return float(value)


def read_wav_duration(path: Path) -> float | None:
    """读 WAV 时长；文件还没写完或格式异常时返回 None。"""
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getframerate() <= 0:
                return None
            frames = handle.getnframes()
    except (wave.Error, OSError, EOFError):
        return None
    if frames <= 0:
        return None
    with wave.open(str(path), "rb") as handle:
        return frames / float(handle.getframerate())


def load_clip(path: Path, *, stability_seconds: float, now: float) -> Clip | None:
    """把磁盘上的切片读成 Clip；未写完 / 无音频 / 名字不合规都返回 None。"""
    if path.suffix.lower() != ".wav":
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if now - stat.st_mtime < stability_seconds:
        return None
    ended_at = parse_clip_timestamp(path)
    if ended_at is None:
        return None
    duration = read_wav_duration(path)
    if duration is None:
        return None
    return Clip(path=path, started_at=ended_at - duration, duration=duration)


def discover_clips(
    recordings_dir: Path,
    *,
    stability_seconds: float,
    not_before: float | None,
    now: float,
) -> list[Clip]:
    if not recordings_dir.is_dir():
        return []
    clips: list[Clip] = []
    seen_starts: set[float] = set()
    for path in sorted(recordings_dir.glob(f"{CLIP_PREFIX}*.wav")):
        clip = load_clip(path, stability_seconds=stability_seconds, now=now)
        if clip is None:
            continue
        if not_before is not None and clip.started_at < not_before:
            continue
        # 文件名按秒命名，同一秒不会产生两个真实切片；重复视为异常并去重
        if clip.started_at in seen_starts:
            continue
        seen_starts.add(clip.started_at)
        clips.append(clip)
    clips.sort(key=lambda item: item.started_at)
    return clips


def group_sessions(
    clips: Iterable[Clip], *, idle_seconds: float, max_session_seconds: float
) -> list[list[Clip]]:
    """按静默间隔切分会话，并强制单段跨度上限。"""
    sessions: list[list[Clip]] = []
    current: list[Clip] = []
    for clip in clips:
        if not current:
            current = [clip]
            continue
        gap = clip.started_at - current[-1].ended_at
        span = clip.ended_at - current[0].started_at
        if gap > idle_seconds or span > max_session_seconds:
            sessions.append(current)
            current = [clip]
        else:
            current.append(clip)
    if current:
        sessions.append(current)
    return sessions


def session_key(session: list[Clip]) -> str:
    """会话去重键：首切片起始时刻，保留三位小数。"""
    return f"{session[0].started_at:.3f}"


# --------------------------------------------------------------------------
# Life Logger 本地 API
# --------------------------------------------------------------------------


class ApiError(RuntimeError):
    pass


class LifeLoggerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._origin = self.base_url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
            headers["Content-Length"] = str(len(body))
            # 写接口要求同源 Origin + CSRF 头
            headers["Origin"] = self._origin
            headers["X-Life-Logger-CSRF"] = "1"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise ApiError(f"{method} {path} -> HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ApiError(f"{method} {path} -> {error}") from error
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": raw.decode("utf-8", "replace")[:400]}

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health", timeout=15.0)

    def list_jobs(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/jobs", timeout=30.0)
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return jobs if isinstance(jobs, list) else []

    def create_job(self, start_at: str, timezone_name: str) -> str:
        body = json.dumps(
            {"start_at": start_at, "timezone": timezone_name}, ensure_ascii=False
        ).encode("utf-8")
        payload = self._request("POST", "/api/jobs", body=body)
        job_id = payload.get("job_id") if isinstance(payload, dict) else None
        if not job_id:
            raise ApiError(f"建任务没有返回 job_id: {payload}")
        return str(job_id)

    def upload_file(self, job_id: str, path: Path) -> dict[str, Any]:
        from urllib.parse import quote

        body = path.read_bytes()
        query = quote(path.name, safe="")
        # 服务端的文件上传路由在 do_PUT 里（与网页端 web/app.js 一致），
        # 用 POST 会命中 do_POST 末尾的 404。
        return self._request(
            "PUT",
            f"/api/jobs/{quote(job_id, safe='')}/files?name={query}",
            body=body,
            content_type="audio/wav",
            timeout=180.0,
        )

    def start_job(self, job_id: str) -> dict[str, Any]:
        from urllib.parse import quote

        return self._request(
            "POST", f"/api/jobs/{quote(job_id, safe='')}/start", body=b"{}"
        )


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------


@dataclass
class WatchState:
    path: Path
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "WatchState":
        if not path.is_file():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        return cls(
            path=path,
            sessions=dict(payload.get("sessions") or {}),
            pending=dict(payload.get("pending") or {}),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA,
            "updated_at": datetime.now().astimezone().isoformat(),
            "sessions": self.sessions,
            "pending": self.pending,
        }
        # 唯一临时名 + fsync + replace（原实现固定 `<name>.tmp`，并发写会互相覆盖）。
        # 本脚本由 run-airpassport-watch.command 用**裸 python3** 启动，不保证
        # life_logger 可导入 → 优先用它，导入失败则走等价的唯一名回退。
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            from life_logger.atomic import write_bytes_atomic
        except Exception:
            write_bytes_atomic = None
        if write_bytes_atomic is not None:
            write_bytes_atomic(self.path, text.encode("utf-8"))
        else:
            import tempfile

            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent),
                                             prefix=f".{self.path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(text.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------


def log(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def local_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def reconcile(
    client: LifeLoggerClient,
    state: WatchState,
    *,
    max_attempts: int,
    stale_seconds: float = 1800.0,
    log_fn=log,
) -> None:
    """把 pending 里已经跑完的任务结算掉，失败的按次数重试。

    另外处理「卡住」的任务：如果服务端既不报 completed/failed，又长时间停在
    queued/draft，说明它不会自己跑起来。此时必须主动释放 pending 名额，
    否则本进程的严格串行闸门会永远不再提交任何新会话。
    """
    if not state.pending:
        return
    try:
        jobs = {str(job.get("job_id")): job for job in client.list_jobs()}
    except ApiError as error:
        log_fn(f"读取任务列表失败，跳过本轮结算：{error}")
        return
    now = time.time()
    for key, entry in list(state.pending.items()):
        job = jobs.get(str(entry.get("job_id")))
        if job is None:
            log_fn(f"任务 {entry.get('job_id')} 不在列表里，标记为丢失")
            entry["status"] = "missing"
            state.sessions[key] = entry
            state.pending.pop(key, None)
            continue
        status = str(job.get("status") or "")
        if status == "completed":
            entry["status"] = "completed"
            entry["completed_at"] = job.get("completed_at")
            state.sessions[key] = entry
            state.pending.pop(key, None)
            log_fn(
                f"✅ {key} 处理完成（{entry.get('clip_count')} 切片，"
                f"任务 {entry.get('job_id')}）"
            )
            continue
        if status in FAILED_STATUSES:
            attempts = int(entry.get("attempts") or 1)
            error = str(job.get("error") or status)
            state.pending.pop(key, None)
            if attempts >= max_attempts:
                entry["status"] = "failed"
                entry["error"] = error
                state.sessions[key] = entry
                log_fn(f"❌ {key} 失败 {attempts} 次，放弃：{error}")
            else:
                entry["status"] = "retry"
                entry["error"] = error
                state.sessions[key] = entry
                log_fn(f"↻ {key} 第 {attempts} 次失败，将重试：{error}")
            continue
        # draft / queued / running：正常情况下等它跑完。但长期不动的要释放名额。
        since = entry.get("pending_since")
        if since:
            try:
                age = now - float(since)
            except (TypeError, ValueError):
                age = 0.0
            if age > stale_seconds:
                log_fn(
                    f"⚠ {key} 的任务 {entry.get('job_id')} 停在 {status} 已 "
                    f"{age / 60:.0f} 分钟，判定为卡住，释放名额等待重试"
                )
                entry["status"] = "retry"
                entry["error"] = f"stuck in {status}"
                state.sessions[key] = entry
                state.pending.pop(key, None)


def active_job_count(
    client: LifeLoggerClient, state: WatchState, *, stale_seconds: float
) -> int:
    """数一下当前有几个任务在跑，避免和网页端手动任务抢 ASR。"""
    try:
        jobs = client.list_jobs()
    except ApiError:
        return 0
    running = 0
    for job in jobs:
        if str(job.get("status")) != "running":
            continue
        started = job.get("started_at") or job.get("created_at")
        if started:
            try:
                moment = datetime.fromisoformat(str(started))
            except ValueError:
                moment = None
            if moment is not None:
                age = (
                    datetime.now(moment.tzinfo) - moment
                ).total_seconds()
                if age > stale_seconds:
                    continue
        running += 1
    return running


def session_clip_name(session: list[Clip]) -> str:
    """合成文件的固定名字，同时充当「同源任务」的查重键。"""
    return f"airpassport-{session[0].started_at:.3f}.wav"


def build_session_audio(
    session: list[Clip], target_dir: Path, *, log_fn=log
) -> Path:
    """把一段会话的若干 PTT 切片合成一个 WAV。

    为什么要合成，而不是一个任务里塞多个文件：
      1. 管线里 ``prepare_for_start`` 只给第一个文件写 ``effective_start_at``，
         其余文件是 ``unavailable``，ASR 会把它们落到 ``asr/undated/``；
      2. 随后 ``_find_artifact`` 用 rglob 取**排序后的第一个** recordings.json，
         而 ``undated`` 排在 ``2026-xx-xx`` 之前，于是归因读到没有时间的清单，
         最终结果全部落进 ``result/undated/``，不进当天简报和统计。
    合成成单文件后，一个任务就一个文件、一个时间，日期链路全程一致；
    同时与 Watch / H8 的既有流程（一段 = 一个文件）保持同构。

    切片之间的真实间隔会补等长静音，保持时间轴与原始录音一致。

    只用标准库：守护进程跑在系统 python 上，不该把装了 numpy/soundfile 的
    funasr 虚拟环境拖进来。AirPassport 的切片本来就是 16kHz 单声道 16-bit PCM。
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / session_clip_name(session)
    if target.is_file() and target.stat().st_size > 44:
        return target

    frames: list[bytes] = []
    sample_rate: int | None = None
    cursor = session[0].started_at
    for clip in session:
        with wave.open(str(clip.path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            if channels != 1 or width != 2:
                raise ValueError(
                    f"{clip.path.name} 不是 16-bit 单声道（{channels}ch/{width * 8}bit）"
                )
            if sample_rate is None:
                sample_rate = rate
            elif rate != sample_rate:
                raise ValueError(
                    f"同段内采样率不一致：{clip.path.name} 是 {rate}，期望 {sample_rate}"
                )
            gap = clip.started_at - cursor
            if gap > 0.05:
                frames.append(b"\x00\x00" * int(round(gap * sample_rate)))
            frames.append(handle.readframes(handle.getnframes()))
        cursor = clip.ended_at

    if sample_rate is None:
        raise ValueError("会话里没有可用的切片")

    temporary = target.with_suffix(".wav.tmp")
    with wave.open(str(temporary), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(b"".join(frames))
    os.replace(temporary, target)

    total_frames = sum(len(chunk) for chunk in frames) // 2
    log_fn(
        f"⊕ 已合成会话音频 {target.name}：{len(session)} 切片 → "
        f"{total_frames / sample_rate:.1f}s"
    )
    return target


def find_job_by_names(
    client: LifeLoggerClient, wanted: set[str], *, log_fn=log
) -> dict[str, Any] | None:
    """找出服务端是否已有处理同一份文件的未终结任务。

    判定依据是 ``status.json`` 里的 ``files[].original_name``（即上传时的文件名）。
    合成音频的名字由会话起始时刻唯一确定，所以这能可靠识别「同一会话被不同
    job_id 处理」的撞车。已完成/失败的任务不算，避免重跑已出结果的会话。
    """
    if not wanted:
        return None
    try:
        jobs = client.list_jobs()
    except ApiError as error:
        log_fn(f"读取任务列表失败，无法查重：{error}")
        return None
    best: dict[str, Any] | None = None
    for job in jobs:
        status = str(job.get("status") or "")
        if status in {"completed", "failed"}:
            continue
        names = {
            str(item.get("original_name"))
            for item in (job.get("files") or [])
            if isinstance(item, dict)
        }
        if wanted & names:
            # 已经有进度的那一个更值得复用
            done = int((job.get("progress") or {}).get("completed") or 0)
            best_done = int((best.get("progress") or {}).get("completed") or 0) if best else -1
            if best is None or done > best_done:
                best = job
    return best


def submit_session(
    client: LifeLoggerClient,
    state: WatchState,
    session: list[Clip],
    *,
    timezone_name: str,
    max_attempts: int,
    dry_run: bool,
    sessions_dir: Path,
    log_fn=log,
) -> bool:
    key = session_key(session)
    previous = state.sessions.get(key) or {}
    if previous.get("status") == "completed":
        return False
    attempts = int(previous.get("attempts") or 0) + 1
    if attempts > max_attempts:
        return False

    first = session[0]
    last = session[-1]
    speech = sum(clip.duration for clip in session)
    span = last.ended_at - first.started_at
    summary = (
        f"{len(session)} 切片 / 语音 {speech:.0f}s / 跨度 {span / 60:.1f} 分"
        f"（{local_iso(first.started_at)[11:19]} → {local_iso(last.ended_at)[11:19]}）"
    )
    if dry_run:
        log_fn(f"[dry-run] 会提交 {key}：{summary}")
        return False

    # 先看服务端是否已经有一个「同一批切片」的任务没跑完。
    # 场景：任务被服务重启中断后，服务端会自己把它从断点恢复；此时我们这边的
    # 记账只记得旧 job_id，如果直接新建任务，同一批音频会被两套 ASR 并行算两遍
    # （2026-10-01 22:29 实际发生过：74341664 与 5f9ffd30 撞车）。发现同源任务
    # 就复用它，而不是再建一个。
    existing = find_job_by_names(
        client, {session_clip_name(session)}, log_fn=log_fn
    )
    if existing is not None:
        existing_id = str(existing.get("job_id"))
        status = str(existing.get("status") or "")
        log_fn(
            f"↺ {key} 已存在同源任务 {existing_id}（{status}），复用而不新建"
        )
        if status in {"draft", "queued", "interrupted"}:
            try:
                client.start_job(existing_id)
            except ApiError as error:
                log_fn(f"恢复同源任务失败（{key}，任务 {existing_id}）：{error}")
                return False
        entry = {
            "status": "running",
            "attempts": attempts,
            "job_id": existing_id,
            "clip_count": len(session),
            "speech_seconds": round(speech, 1),
            "span_seconds": round(span, 1),
            "started_at": local_iso(first.started_at),
            "ended_at": local_iso(last.ended_at),
            "reused": True,
            "pending_since": time.time(),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        state.sessions[key] = entry
        state.pending[key] = entry
        state.save()
        return True

    try:
        combined = build_session_audio(session, sessions_dir, log_fn=log_fn)
    except Exception as error:  # noqa: BLE001 - 合成失败不该让守护进程退出
        log_fn(f"合成会话音频失败（{key}）：{error!r}")
        return False

    try:
        job_id = client.create_job(local_iso(first.started_at), timezone_name)
    except ApiError as error:
        log_fn(f"建任务失败（{key}）：{error}")
        return False

    try:
        client.upload_file(job_id, combined)
    except (ApiError, OSError) as error:
        log_fn(f"上传会话音频失败（{key}，任务 {job_id}）：{error}")
        entry = {
            "status": "upload_failed",
            "attempts": attempts,
            "job_id": job_id,
            "error": str(error),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        state.sessions[key] = entry
        state.save()
        return False

    try:
        client.start_job(job_id)
    except ApiError as error:
        log_fn(f"启动任务失败（{key}，任务 {job_id}）：{error}")
        state.sessions[key] = {
            "status": "start_failed",
            "attempts": attempts,
            "job_id": job_id,
            "error": str(error),
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        state.save()
        return False

    entry = {
        "status": "running",
        "attempts": attempts,
        "job_id": job_id,
        "clip_count": len(session),
        "speech_seconds": round(speech, 1),
        "span_seconds": round(span, 1),
        "started_at": local_iso(first.started_at),
        "ended_at": local_iso(last.ended_at),
        "pending_since": time.time(),
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    state.sessions[key] = entry
    state.pending[key] = entry
    state.save()
    log_fn(f"▶ 已提交 {key}：{summary} → 任务 {job_id}")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 AirPassport 录音自动接进 Life Logger 处理队列"
    )
    parser.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--mark-only",
        action="store_true",
        help="只记录 dictation 时间窗，不建任务、不跑 ASR（AirPassport 现在是语音麦，"
        "不再是 Life Logger 的录音来源；它的作用是给 Watch 录音做时间标尺）",
    )
    parser.add_argument(
        "--dictation-windows",
        type=Path,
        default=DEFAULT_DICTATION_WINDOWS,
        help="dictation 时间窗索引文件（每行一条 JSON）",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=DEFAULT_SESSIONS_DIR,
        help="合成后的会话音频存放目录（一个会话一个文件）",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LIFE_LOGGER_URL", "http://127.0.0.1:8765"),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("LIFE_LOGGER_TOKEN"),
        help="访问令牌；默认从 output/local-web/secrets.json 读 mobile_access_token",
    )
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=20.0,
        help="相邻切片间隔超过它就切一段（默认 20s）",
    )
    parser.add_argument(
        "--max-session-seconds",
        type=float,
        default=600.0,
        help="单段跨度上限，防止持续说话时无限堆积（默认 600s）",
    )
    parser.add_argument(
        "--stability-seconds",
        type=float,
        default=3.0,
        help="切片落盘后静止多久才算写完（默认 3s）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="扫描间隔秒数（默认 5s）",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="单轮最多提交几段。默认 1 = 严格串行：每个任务都会各自加载一遍 "
        "ASR/声纹模型，同时放行多个会把机器压到 kernel panic（已实测踩过）。",
    )
    parser.add_argument(
        "--stale-job-seconds",
        type=float,
        default=5400.0,
        help="running 超过这么久视为僵死，不再阻塞新任务（默认 90 分钟）",
    )
    parser.add_argument(
        "--backfill-from",
        default=None,
        help="只处理该时刻之后的录音，ISO 或 YYYY-MM-DD（默认今天 00:00）",
    )
    parser.add_argument(
        "--backfill-all",
        action="store_true",
        help="不设时间下限，处理目录里全部历史录音（很重，慎用）",
    )
    parser.add_argument(
        "--no-concurrency-guard",
        action="store_true",
        help="不检查网页端是否已有任务在跑",
    )
    parser.add_argument(
        "--list", action="store_true", help="只打印会切出哪些段，不提交"
    )
    parser.add_argument("--once", action="store_true", help="跑一轮就退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写任何东西")
    parser.add_argument("--quiet", action="store_true")
    return parser


def resolve_not_before(args: argparse.Namespace) -> float | None:
    if args.backfill_all:
        return None
    if args.backfill_from:
        raw = str(args.backfill_from)
        try:
            if len(raw) == 10:
                moment = datetime.fromisoformat(raw).replace(
                    hour=0, minute=0, second=0
                )
            else:
                moment = datetime.fromisoformat(raw)
        except ValueError:
            raise SystemExit(f"--backfill-from 无法解析：{raw}")
        if moment.tzinfo is None:
            # [Q479-P0] 裸日期/裸时间按**北京时间**解释，不再走宿主本地时区（astimezone()）
            moment = moment.replace(tzinfo=LOCAL_TZ)
        return moment.timestamp()
    # [Q479-P0] "今天 00:00" 必须是**北京今天 00:00**。原来 datetime.now() 返回宿主
    # 本地时间，Mac 在 UTC-8 时这个窗口起点 = 北京 16:00，白天步数/健康数据全落窗口外。
    return datetime.now(tz=LOCAL_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return str(args.token)
    secrets_path = PROJECT_ROOT / "output" / "local-web" / "secrets.json"
    try:
        payload = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"读不到访问令牌，请用 --token 指定：{error}")
    token = payload.get("mobile_access_token")
    if not token:
        raise SystemExit(f"{secrets_path} 里没有 mobile_access_token")
    return str(token)


def record_dictation_windows(
    sessions: list[list[Clip]], path: Path, *, log_fn=log
) -> int:
    """把每段会话的起止时刻追加进时间窗索引（``--mark-only`` 模式）。

    AirPassport 不再是 Life Logger 的录音来源，它现在是 Mac 的语音输入。
    但 Apple Watch 会把这些话一并录进去，所以要留下「这段时间在电脑前说话」
    的标记，之后处理 Watch 录音时用它剔除 dictation 句子。

    每个会话一行 JSON，按起始时刻去重，重复运行不会写重复条目。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.add(str(json.loads(line).get("start")))
            except json.JSONDecodeError:
                continue

    added = 0
    with path.open("a", encoding="utf-8") as handle:
        for session in sessions:
            start = local_iso(session[0].started_at)
            if start in existing:
                continue
            record = {
                "start": start,
                "end": local_iso(session[-1].ended_at),
                "clip_count": len(session),
                "speech_seconds": round(sum(clip.duration for clip in session), 1),
                "source": "airpassport-ptt",
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            existing.add(start)
            added += 1
    if added:
        log_fn(f"✎ 记下 {added} 条 dictation 时间窗 → {path}")
    return added


def run_once(args: argparse.Namespace, client: LifeLoggerClient, state: WatchState) -> int:
    emitted = 0

    def say(message: str) -> None:
        if not args.quiet:
            log(message)

    now = time.time()
    if not args.mark_only:
        reconcile(
            client,
            state,
            max_attempts=args.max_attempts,
            stale_seconds=args.stale_job_seconds,
            log_fn=say,
        )

    not_before = resolve_not_before(args)
    clips = discover_clips(
        args.recordings,
        stability_seconds=args.stability_seconds,
        not_before=not_before,
        now=now,
    )
    sessions = group_sessions(
        clips,
        idle_seconds=args.idle_seconds,
        max_session_seconds=args.max_session_seconds,
    )

    if args.list:
        say(f"发现 {len(clips)} 个切片，切成 {len(sessions)} 段：")
        for session in sessions:
            key = session_key(session)
            done = (state.sessions.get(key) or {}).get("status")
            speech = sum(clip.duration for clip in session)
            say(
                f"  {key}  {local_iso(session[0].started_at)[11:19]} → "
                f"{local_iso(session[-1].ended_at)[11:19]}  "
                f"{len(session):2d} 切片  语音 {speech:5.1f}s  [{done or '待处理'}]"
            )
        return 0

    if args.mark_only:
        # AirPassport 现在是 Mac 的语音输入，不再是 Life Logger 的录音来源。
        # 只记时间窗，不建任务、不跑 ASR/声纹 —— 这条链路本身没有处理价值，
        # 它的价值是给 Watch 录音当「这段时间在电脑前说话」的标尺。
        added = record_dictation_windows(
            sessions, args.dictation_windows, log_fn=say
        )
        if not args.quiet and not added:
            log(f"没有新的 dictation 时间窗（磁盘 {len(clips)} 切片 / {len(sessions)} 段）")
        return 0

    # 已经在跑（pending）或已终结（completed/failed）的会话不再作为候选，
    # 只有明确标记为待重试的才重新入队。
    candidates = []
    for session in sessions:
        key = session_key(session)
        entry = state.sessions.get(key)
        if key in state.pending:
            continue
        status = str((entry or {}).get("status") or "")
        if status in {"completed", "failed"}:
            continue
        candidates.append(session)

    if not candidates:
        if args.once and not args.quiet:
            log(f"没有待处理的新会话（磁盘 {len(clips)} 切片 / {len(sessions)} 段）")
        return 0

    # 并发闸门以「我们自己的记账」为准，不依赖服务端任务列表。
    # 理由：这里每个任务都会各自加载一遍 ASR + 声纹模型，实测同时放行多个会把
    # 内存压缩段打满、触发 kernel panic 重启（2026-10-01 22:00 真实踩过）。
    # 记账是本地文件读写，没有网络延迟造成的竞态，能给出硬保证：
    # 只要还有我们提交的任务没结算，就一个都不再提交。
    budget = max(1, int(args.limit))
    if not args.no_concurrency_guard and state.pending:
        if not args.quiet:
            log(
                f"我们还有 {len(state.pending)} 个任务没跑完，本轮不提交"
                "（严格串行，避免抢 ASR 把机器压垮）"
            )
        return 0

    for session in candidates[:budget]:
        if submit_session(
            client,
            state,
            session,
            timezone_name=args.timezone,
            max_attempts=args.max_attempts,
            dry_run=args.dry_run,
            sessions_dir=args.sessions_dir,
            log_fn=say,
        ):
            emitted += 1

    if not args.quiet and not emitted and candidates:
        log(f"本轮没有提交新任务（候选 {len(candidates)} 段）")
    return emitted


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = resolve_token(args)
    client = LifeLoggerClient(args.base_url, token)
    state = WatchState.load(args.state)

    if not args.dry_run and not args.list and not args.mark_only:
        try:
            health = client.health()
        except ApiError as error:
            raise SystemExit(f"连不上 Life Logger（{args.base_url}）：{error}")
        if not health.get("ok"):
            raise SystemExit(f"Life Logger 健康检查异常：{health}")

    if args.once or args.list or args.dry_run:
        return 1 if run_once(args, client, state) < 0 else 0

    log(
        "AirPassport 自动接入已启动："
        f"{args.recordings} → {args.base_url}（间隔 {args.poll_interval:.0f}s 扫描一次，"
        f"静默 {args.idle_seconds:.0f}s 切段）"
    )
    while True:
        try:
            run_once(args, client, state)
        except ApiError as error:
            log(f"本轮出错，稍后重试：{error}")
        except Exception as error:  # noqa: BLE001 - 守护进程不能因为单轮异常退出
            log(f"未预期错误，稍后重试：{error!r}")
        time.sleep(max(2.0, args.poll_interval))


if __name__ == "__main__":
    sys.exit(main())
