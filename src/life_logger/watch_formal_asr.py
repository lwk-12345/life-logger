"""Durable per-segment ASR for Watch sessions (no preview or attribution)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from life_logger.asr_slots import (
    acquire_asr_slot,
    release_asr_slot,
    update_asr_slot_child_pid,
)
from life_logger.audio_classifier import (
    AUDIO_CLASSIFIER_RECORD_FIELDS,
    shift_window_classification,
)
from life_logger.audio_vad import is_segment_silence
from life_logger.ingest import discover_recordings
from life_logger.timeutil import LOCAL_TZ
from life_logger.local_pipeline import (    PROJECT_ROOT,
    build_asr_command,
    load_local_config,
    local_pipeline_env,
    validate_local_pipeline_config,
)
from life_logger.timeutil import local_clock as _local_clock, local_iso as _local_iso
from life_logger.watch_stream import WatchSessionStore


logger = logging.getLogger(__name__)
_DEFAULT_BUILD_ASR_COMMAND = build_asr_command
_DEFAULT_SUBPROCESS_POPEN = subprocess.Popen


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _bump_read_epoch(root: Path | str, reason: str) -> None:
    """ASR 子进程写完后通知 Web 读进程（跨进程失效，不等 10s TTL）。"""
    try:
        from life_logger.event_view import bump_scan_epoch

        bump_scan_epoch(root, reason)
    except Exception as error:  # pragma: no cover - 通知失败不影响转写
        print(f"[life-logger] bump_scan_epoch 失败({reason}): {error}", flush=True)


def _read_json(path: Path, default: Any) -> Any:
    # [2026-11-05] 分段 state.json 可能是 `state.json.gz`（未压缩优先）。
    # 写侧仍然只写 state.json（本函数只负责读）。
    try:
        from life_logger.jsonio import read_json

        return read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def _attempt(value: Any) -> int:
    try:
        return max(0, min(1000, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _transcript_sort_offset(row: dict[str, Any]) -> float:
    value = row.get("start_offset_seconds")
    if isinstance(value, bool):
        return math.inf
    try:
        offset = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return math.inf
    return offset if math.isfinite(offset) else math.inf


def is_hallucinated_asr_noise(text: str) -> bool:
    """Filter out FunASR / SenseVoice / Whisper hallucinated noise artifacts on low energy signals."""
    clean = re.sub(r"[^\w\u4e00-\u9fff]", "", text).strip()
    if not clean:
        return True
    corrupt_chars = ("褣", "磢", "両", "鱷", "磪", "磡", "磯", "褥", "鱲", "櫡")
    if any(ch in clean for ch in corrupt_chars):
        if len(clean) <= 10 or sum(1 for ch in clean if ch in corrupt_chars) >= 2:
            return True
    if len(set(clean)) == 1 and len(clean) >= 4:
        return True
    if len(clean) >= 12 and len(set(clean)) <= 4:
        return True
    if len(clean) >= 16:
        most_common = max(clean.count(ch) for ch in set(clean))
        if most_common / len(clean) >= 0.6:
            return True
    return False


#: 电脑焦点产物（`scripts/mac_input_context.py` 的显式输出）——**固定有界路径**，不扫目录。
INPUT_CONTEXT_ARTIFACT = "input-context/latest.json"
INPUT_CONTEXT_MAX_BYTES = 2_000_000


def load_input_context_windows(root: Path | str, day: str | None = None) -> list[dict[str, Any]]:
    """读取本地焦点窗口产物（可选）。缺文件/超大/格式错 → 返回空（旧路完全等价）。

    [2026-11-04] 新增可选 `day`：给了就**额外**从 `input-context/days/<day>.jsonl`
    回溯历史窗口（`latest.json` 只留 30 分钟，活动时间线数小时后才跑，历史日必需）。
    `day=None` 时走原来的单文件路径（逐字等价，无新依赖）。
    """
    if day:
        # 委托给 input_context 的统一读取器：归档 + latest，按归档键去重。
        from life_logger.input_context import load_input_context_windows as _load_with_archive

        return _load_with_archive(root, day)

    path = Path(root) / INPUT_CONTEXT_ARTIFACT
    try:
        if not path.is_file() or path.stat().st_size > INPUT_CONTEXT_MAX_BYTES:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    windows = payload.get("windows") if isinstance(payload, dict) else payload
    if not isinstance(windows, list):
        return []
    resolved = [item for item in windows if isinstance(item, dict)]
    # [2026-11-04] 前台应用**连续采样区间**也参与解析。
    # 旧实现只有 mic 启停产生的窗口（手表录音时 Mac 麦克风没被占用 → 永远解析不到），
    # 于是 activity 上 0 条带 app 归因。front_windows 是同一采集器的连续采样合并结果，
    # 语义同样是"发声时前台是谁"，不推断投递、不读标题。
    front = payload.get("front_windows") if isinstance(payload, dict) else None
    if isinstance(front, list):
        resolved.extend(item for item in front if isinstance(item, dict))
    return resolved


def attach_watch_input_context(rows: list[dict[str, Any]], windows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """按 Watch 行的**绝对时间**附 input_context；异常窗只标原因，不改 source_label/时间。"""
    from life_logger.input_context import resolve_input_context

    stats = {"resolved": 0, "unknown": 0, "windows": len(windows)}
    if not windows:
        return stats
    for row in rows:
        start = row.get("absolute_started_at") or row.get("timestamp")
        end = row.get("absolute_ended_at") or start
        context = resolve_input_context(record_start=start, record_end=end, windows=windows)
        if context.get("status") == "resolved":
            row["input_context"] = context          # 仅作为"当时输入目标"的上下文证据
            stats["resolved"] += 1
        elif context.get("reason") not in {"no_signal", "no_overlap"}:
            row["input_context_unknown_reason"] = context.get("reason")
            stats["unknown"] += 1
    return stats


#: 单段最多尝试几次。【2026-11-06 Q958】原来没有上限：只要 state 不是
#: completed/no_speech，每轮扫描都会把同一段从零重跑一遍（000031 跑到 attempt=9），
#: 在大 backlog + 机器吃紧时等于把队列钉死在一段上。
MAX_SEGMENT_ATTEMPTS = 5

class WatchFormalASRProcessor:
    """Process acknowledged Watch segments into durable, session-scoped ASR."""

    def __init__(
        self,
        store: WatchSessionStore,
        config_path: Path,
        stop: threading.Event,
        *,
        slot_limit: int = 2,
        process_timeout_seconds: float = 900.0,
        slot_acquirer: Callable[[str], dict | None] | None = None,
        slot_releaser: Callable[[dict], None] | None = None,
    ) -> None:
        self.store = store
        self.config_path = Path(config_path)
        self.stop = stop
        self.slot_limit = max(1, slot_limit)
        timeout = float(process_timeout_seconds)
        self.process_timeout_seconds = timeout if math.isfinite(timeout) and timeout > 0 else 900.0
        self._uses_shared_slots = slot_acquirer is None
        self.slot_acquirer = slot_acquirer or (
            lambda owner: acquire_asr_slot(
                owner, limit=self.slot_limit, wait_seconds=0, require_memory_gb=1.5
            )
        )
        self.slot_releaser = slot_releaser or release_asr_slot
        self._scan_lock = threading.Lock()
        self._verified_segments: dict[tuple[str, str, int, str], tuple[int, int]] = {}
        self._session_cursor: tuple[str, str] | None = None
        self._live_sync_lock = threading.Lock()
        self._live_sync_active: set[tuple[str, str]] = set()
        self._live_sync_pending: set[tuple[str, str]] = set()

    def _directory(self, device_id: str, session_id: str) -> Path:
        return self.store.session_directory(device_id, session_id) / "formal-asr"

    def _manifest(self, device_id: str, session_id: str) -> dict[str, Any]:
        return self.store.load_session(device_id, session_id)

    def _segment_state_path(self, root: Path, sequence: int) -> Path:
        return root / "segments" / f"{sequence:06d}" / "state.json"

    def _read_segment_result(
        self, output: Path, sequence: int, start_offset_ms: int, started_at: str,
        device_id: str | None = None, session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        def artifact(name: str) -> Any:
            direct = output / name
            if direct.is_file():
                return _read_json(direct, None)
            matches = sorted(output.rglob(name))
            return _read_json(matches[0], None) if matches else None

        summary = artifact("run-summary.json")
        rows = artifact("transcript.json")
        if (
            not isinstance(summary, dict)
            or summary.get("status") != "completed"
            or isinstance(summary.get("transcript_count"), bool)
            or not isinstance(summary.get("transcript_count"), int)
            or summary["transcript_count"] < 0
            or not isinstance(rows, list)
            or summary["transcript_count"] != len(rows)
            or not isinstance(artifact("recordings.json"), (list, dict))
        ):
            raise ValueError("ASR artifacts failed validation")
        if summary["transcript_count"] == 0:
            return []
        base = datetime.fromisoformat(started_at) + timedelta(milliseconds=start_offset_ms)
        result = []
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or not str(row.get("text", "")).strip():
                raise ValueError("invalid transcript row")
            raw_text = str(row.get("text", "")).strip()
            if is_hallucinated_asr_noise(raw_text):
                continue
            relative_start = float(row.get("start_offset_seconds", 0))
            relative_end = float(row.get("end_offset_seconds", relative_start))
            if not math.isfinite(relative_start) or not math.isfinite(relative_end):
                raise ValueError("invalid transcript timestamps")
            relative_start = max(0.0, relative_start)
            relative_end = max(relative_start, relative_end)
            start = relative_start + start_offset_ms / 1000
            end = relative_end + start_offset_ms / 1000
            normalized = dict(row)
            normalized.update(
                segment_id=f"watch_{sequence:06d}_{index:06d}",
                start_offset_seconds=round(start, 3),
                end_offset_seconds=round(end, 3),
                absolute_started_at=(base + timedelta(seconds=relative_start)).isoformat(),
                absolute_ended_at=(base + timedelta(seconds=relative_end)).isoformat(),
            )
            normalized["started_at_local"] = _local_iso(normalized["absolute_started_at"])
            normalized["clock_local"] = _local_clock(normalized["absolute_started_at"])
            # 音频分类窗口是按「段内时间轴」算的（上游 moss/Whisper 只看得到本段音频），
            # 而上面的时间戳刚被加上段起点 start_offset_ms。窗口字段必须同步平移，
            # 否则每段的 window_index 都从 0 开始 —— 聚合到整场会话时会互相顶掉，
            # 只剩首批窗口（下游看板场景统计读的就是这些窗口）。
            normalized = shift_window_classification(normalized, start_offset_ms / 1000)
            # 实时扫描口语微指令 (TODO/Expense/Idea/Privacy)，仅针对本人语音触发
            #
            # 【2026-10-17】旧条件只排 non_self，把 unknown（声纹不可用 / 离得远）
            # 也放进来了 —— 实测电视剧台词「别忘了，」「大明呢，大明最后怎么样了」
            # 全部变成了待办卡片（source_label=unknown, proximity=far, sim=0.366）。
            # 现在把身份证据一并传给 scan_voice_triggers，由它做正向本人校验。
            from life_logger.voice_triggers import QuickCaptureStore, scan_voice_triggers

            try:
                captures = scan_voice_triggers(
                    str(normalized.get("text", "")),
                    timestamp=normalized.get("absolute_started_at"),
                    source_label=normalized.get("source_label"),
                    acoustic_proximity=normalized.get("acoustic_proximity"),
                    voice_similarity=normalized.get("voice_similarity"),
                    # [2026-11-06] 前台是工作软件时不触发口令词：把该行已解析的
                    # input_context（及捏两下标记）一并传下去，判不出来就照常触发。
                    input_context=normalized.get("input_context"),
                    physical_pin=normalized.get("physical_pin") or normalized.get("pin_type"),
                    pin_age_seconds=normalized.get("pin_age_seconds"),
                    source_provenance={
                        "recording_id": normalized.get("recording_id"),
                        "session_id": session_id,
                        "segment_id": normalized.get("segment_id"),
                        "start_offset_seconds": normalized.get("start_offset_seconds"),
                        "end_offset_seconds": normalized.get("end_offset_seconds"),
                        "absolute_started_at": normalized.get("absolute_started_at"),
                        "absolute_ended_at": normalized.get("absolute_ended_at"),
                    },
                )
            except Exception as error:  # 口令识别失败不能连累整段转写
                captures = []
                print(
                    f"[life-logger] voice trigger 扫描失败: {type(error).__name__} {error}",
                    flush=True,
                )

            if captures:
                capture_store = QuickCaptureStore(self.store.root.parent / "quick_captures.json")
                capture_store.append(captures)
                _bump_read_epoch(self.store.root.parent, "watch-quick-capture")
                # [2026-10-13] 习惯口令（添加/习惯打卡/删除习惯）直接落到
                # habits.json —— 与待办口令各走各的线：习惯天天重置，待办有终态。
                # 单独 try：习惯失败不能连累下面的震动反馈。
                try:
                    from life_logger.local_habits import (
                        LocalHabitStore,
                        apply_habit_captures,
                    )

                    apply_habit_captures(
                        LocalHabitStore(self.store.root.parent), captures
                    )
                except Exception:
                    pass
                # [2026-10-13] 营养口令（「记录营养 X 每100克 …」）落到自建品牌库
                # —— 与习惯口令一样单独 try：写库失败不能连累下面的震动反馈。
                try:
                    from life_logger.food_lookup import (
                        apply_nutrition_captures,
                        default_food_lookup,
                    )

                    apply_nutrition_captures(
                        default_food_lookup(),
                        [capture.to_dict() for capture in captures],
                    )
                except Exception:
                    pass
                if device_id and hasattr(self.store, "enqueue_haptic"):
                    self.store.enqueue_haptic(device_id, "success")
            result.append(normalized)
        return result

    def _try_adopt_existing_artifacts(
        self, output: Path, sequence: int, segment: dict[str, Any],
        manifest: dict[str, Any], device_id: str, session_id: str,
    ) -> list[dict[str, Any]] | None:
        """盘上已有可用产物就复用。返回 rows（可能是空列表=no_speech），不可用返回 None。

        判据（缺一不可）：
          1. run-summary.status == completed 且 transcript_count == len(transcript.json)
          2. recordings.json 存在；
          3. run-summary.source 指向**这一段自己**的音频（防止把别人的产物认领过来）。
        校验直接复用 `_read_segment_result`，不另写一套。
        """
        expected = f"segment-{sequence:06d}.m4a"
        summary = None
        for candidate in sorted(Path(output).rglob("run-summary.json")):
            try:
                summary = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summary = None
            break
        if not isinstance(summary, dict):
            return None
        if summary.get("status") != "completed":
            return None
        if expected not in str(summary.get("source") or ""):
            return None
        try:
            return self._read_segment_result(
                output, sequence, int(segment["start_offset_ms"]), str(manifest["started_at"]),
                device_id=device_id, session_id=session_id,
            )
        except Exception:
            return None


    # ---- 场景/环境声（PANNs，2026-11-07） ---------------------------------------
    #: 手表路径默认开；每 N 段抽一次（段 ≈15s，5 段 ≈ 每 75 秒一次，场景是分钟级稳定量）。
    _SCENE_STRIDE = int(os.environ.get("LIFE_LOGGER_PANNS_STRIDE", "5"))

    def _scene_enabled(self) -> bool:
        raw = os.environ.get("LIFE_LOGGER_PANNS_ENABLE")
        if raw is None:
            # [2026-11-07 总控] 先默认关：MobileNetV1 权重损坏、只能退回 312MB 的 Cnn14，
            # 且在串行转写循环里同步跑子进程，会拖慢积压中的正式转写。修好权重并实测后再开。
            return False
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _annotate_scene(self, device: str, session: str, segment: dict[str, Any]) -> None:
        """给这一段补 scene/events 字段。

        任何失败都只 warning，**绝不影响转写**（派单要求）。
        """
        if not self._scene_enabled():
            return
        try:
            sequence = int(segment["sequence"])
            # 每次读环境变量：测试/运维改 stride 不需要重新 import
            stride = int(os.environ.get("LIFE_LOGGER_PANNS_STRIDE", self._SCENE_STRIDE))
            if stride > 1 and sequence % stride != 0:
                return
            audio = self.store.session_directory(device, session) / "segments" / f"segment-{sequence:06d}.m4a"
            if not audio.is_file():
                return
            output = self._directory(device, session) / "segments" / f"{sequence:06d}"
            state_path = output / "state.json"
            state = _read_json(state_path, {})
            if not isinstance(state, dict) or state.get("scene"):
                return
            from life_logger import audio_scene
            result = audio_scene.annotate(audio)
            state["scene"] = {
                "policy_version": result.get("policy_version"),
                "model": result.get("model"),
                "checkpoint": result.get("checkpoint"),
                "warning": result.get("warning"),
                "window_count": result.get("window_count"),
                "scene": result.get("scene"),
                "scene_ranked": (result.get("scene_ranked") or [])[:3],
                "events": (result.get("events") or [])[:5],
                "elapsed_seconds": result.get("elapsed_seconds"),
                "annotated_at": datetime.now(LOCAL_TZ).isoformat(),
            }
            output.mkdir(parents=True, exist_ok=True)
            _atomic_json(state_path, state)
        except Exception as error:          # noqa: BLE001 —— 场景失败不能拖垮转写
            print(f"[life-logger] scene 标注跳过: {type(error).__name__} {error}", flush=True)

    def _process_segment(
        self, device_id: str, session_id: str, manifest: dict[str, Any], segment: dict[str, Any]
    ) -> bool:
        sequence = int(segment["sequence"])
        session_dir = self.store.session_directory(device_id, session_id)
        audio = session_dir / "segments" / f"segment-{sequence:06d}.m4a"
        root = self._directory(device_id, session_id)
        output = root / "segments" / f"{sequence:06d}"
        state_path = output / "state.json"
        old = _read_json(state_path, {})
        if not isinstance(old, dict):
            old = {}
        source_hash = str(segment["sha256"])
        cache_key = (device_id, session_id, sequence, source_hash)
        try:
            stat = audio.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            checksum_matches = audio.is_file() and (
                self._verified_segments.get(cache_key) == signature
                or _digest(audio) == source_hash
            )
            if checksum_matches:
                self._verified_segments[cache_key] = signature
        except OSError:
            checksum_matches = False
        if not checksum_matches:
            attempt = _attempt(old.get("attempt")) + 1
            output.mkdir(parents=True, exist_ok=True)
            _atomic_json(state_path, {
                "status": "failed", "sequence": sequence, "source_sha256": source_hash,
                "attempt": attempt, "error": "segment checksum mismatch or file missing",
                "retry_at": time.time() + min(1800, 15 * 2 ** min(attempt, 7)),
                "updated_at": datetime.now(LOCAL_TZ).isoformat(),
            })
            return True
        if old.get("source_sha256") == source_hash and old.get("status") in {"completed", "no_speech"}:
            return False
        # 已经试到上限并被判死的段：不再占用 ASR 槽位（否则它会永远卡在队首）。
        if old.get("source_sha256") == source_hash and old.get("exhausted") is True:
            return False
        # 【2026-11-06 Q958】**产物已经完成就不要再跑一遍**。
        # 老逻辑只看 state.json：一旦 state 被写成 processing/failed（上一轮读到半写产物、
        # 进程被杀、或超时），下一次就会从零重跑同一段 —— 实测 000031 有 48 条转写的
        # run-summary 躺在盘上，state 却是 processing，于是又跑了 8 遍。
        # 这里先尝试用**已有产物**收尾；校验不过（半写/不匹配）才照常重跑。
        if old.get("status") not in {"completed", "no_speech"}:
            adopted = self._try_adopt_existing_artifacts(
                output, sequence, segment, manifest, device_id, session_id,
            )
            if adopted is not None:
                rows = adopted
                _atomic_json(state_path, {
                    "status": "completed" if rows else "no_speech",
                    "sequence": sequence,
                    "source_sha256": source_hash,
                    "attempt": _attempt(old.get("attempt")),
                    "rows": rows,
                    "error": None,
                    "adopted_existing_artifacts": True,
                    "updated_at": datetime.now(LOCAL_TZ).isoformat(),
                })
                return True
        if old.get("source_sha256") == source_hash:
            try:
                if float(old.get("retry_at", 0) or 0) > time.time():
                    return False
            except (TypeError, ValueError, OverflowError):
                pass
        attempt = _attempt(old.get("attempt")) + 1 if old.get("source_sha256") == source_hash else 1
        if attempt > MAX_SEGMENT_ATTEMPTS:
            # 【2026-11-06 Q958】失败到上限就**结案**，不再重试、不再占槽位：
            # 否则一段永远失败的音频会把整个队列钉在它身上（用户看到的就是"9-16 点没转写"）。
            output.mkdir(parents=True, exist_ok=True)
            _atomic_json(state_path, {
                "status": "failed", "sequence": sequence, "source_sha256": source_hash,
                "attempt": attempt, "exhausted": True,
                "error": "retry limit reached; segment marked failed and skipped",
                "updated_at": datetime.now(LOCAL_TZ).isoformat(),
            })
            return True
        try:
            config = load_local_config(self.config_path)
            validate_local_pipeline_config(config)
        except Exception as error:
            output.mkdir(parents=True, exist_ok=True)
            _atomic_json(state_path, {
                "status": "failed", "sequence": sequence, "source_sha256": source_hash,
                "attempt": attempt, "error": str(error)[:240],
                "retry_at": time.time() + min(1800, 15 * 2 ** min(attempt, 7)),
                "updated_at": datetime.now(LOCAL_TZ).isoformat(),
            })
            return True
        # Silero VAD 极速静音剔除门禁：若分段 100% 纯静音，在 0.2s 内直接标 no_speech 结案，不占 ASR 槽位
        try:
            if is_segment_silence(audio):
                output.mkdir(parents=True, exist_ok=True)
                _atomic_json(state_path, {
                    "status": "no_speech", "sequence": sequence, "source_sha256": source_hash,
                    "attempt": attempt, "rows": [], "error": None,
                    "updated_at": datetime.now(LOCAL_TZ).isoformat(),
                })
                return True
        except Exception:
            pass

        owner = f"watch-formal-asr:{device_id}:{session_id}:{sequence}"
        try:
            slot = self.slot_acquirer(owner)
        except Exception:
            return False
        if slot is None:
            return False
        try:
            output.mkdir(parents=True, exist_ok=True)
            state = {"status": "processing", "sequence": sequence, "source_sha256": source_hash,
                     "attempt": attempt, "updated_at": datetime.now(LOCAL_TZ).isoformat()}
            _atomic_json(state_path, state)
            start_at_iso = (datetime.fromisoformat(manifest["started_at"]) +
                            timedelta(milliseconds=int(segment["start_offset_ms"]))).isoformat()
            # User policy: all archive dates and local times in Asia/Shanghai regardless of Mac/manifest timezone.
            timezone_name = "Asia/Shanghai"
            duration_ms = int(segment.get("duration_ms", 60000) or 60000)
            raw_provider = str(config.get("asr_provider") or "local_turbo").strip().lower()
            # 【2026-10-14 修】原来这里只放行 doubao/volcano，其余（含 moss）一律改写成
            # local_turbo —— 结果配置里写 moss 等于没写，MOSS 从未在服务里跑过
            # （实测：45906 条转写里 source="moss-transcribe-diarize" 出现 0 次）。
            # 现在与 local_pipeline.build_asr_command 的口径对齐，放行 moss 一族；
            # moss 不走 in-process 分支，会落到下面的 build_asr_command → 子进程
            # scripts/run_cloud_asr_pipeline.py --provider moss。
            if raw_provider in {"moss", "moss_transcribe_diarize", "moss-td"}:
                asr_provider = "moss"
            elif raw_provider in {"doubao", "volcano"}:
                asr_provider = "doubao"
            else:
                asr_provider = "local_turbo"
            ran_inprocess = False
            if (
                asr_provider == "local_turbo"
                and build_asr_command is _DEFAULT_BUILD_ASR_COMMAND
                and subprocess.Popen is _DEFAULT_SUBPROCESS_POPEN
            ):
                if self._uses_shared_slots:
                    try:
                        update_asr_slot_child_pid(slot, os.getpid())
                    except Exception:
                        pass
                ran_inprocess = self._run_inprocess_local_turbo_segment(
                    audio=audio,
                    output=output,
                    start_at=start_at_iso,
                    timezone_name=timezone_name,
                    duration_ms=duration_ms,
                )
            if (
                not ran_inprocess
                and asr_provider == "moss"
                and build_asr_command is _DEFAULT_BUILD_ASR_COMMAND
                and subprocess.Popen is _DEFAULT_SUBPROCESS_POPEN
            ):
                from life_logger.asr_resident import resident_enabled

                if resident_enabled():
                    if self._uses_shared_slots:
                        try:
                            update_asr_slot_child_pid(slot, os.getpid())
                        except Exception:
                            pass
                    ran_inprocess = self._run_resident_moss_segment(
                        audio=audio, output=output, start_at=start_at_iso,
                        timezone_name=timezone_name, duration_ms=duration_ms, config=config,
                    )
            if not ran_inprocess:
                command = build_asr_command(
                    audio=audio, output_dir=output, config=config,
                    start_at=start_at_iso,
                    timezone_name=timezone_name,
                )
                # [Q806] 声纹滚动池跨段持久化：把**同一个设备**共用的池文件路径塞进子进程
                # 环境。子进程里 RollingVoiceprintPool.from_template() 会据此 hydrate，
                # 更新后再原子写回 —— 否则每段都从模板重新开始，池子永远等于初始模板
                # （用户抱怨「给我妈打电话你都处理不了」的机制之一）。
                child_env = local_pipeline_env(config)
                pool_path = self._voiceprint_pool_path(config, device_id)
                if pool_path is not None:
                    child_env["LIFE_LOGGER_VOICEPRINT_POOL"] = str(pool_path)
                with (output / "asr.log").open("ab") as log:
                    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=child_env,
                                               stdout=log, stderr=subprocess.STDOUT)
                    if self._uses_shared_slots:
                        try:
                            tracked = update_asr_slot_child_pid(slot, process.pid)
                            if not tracked:
                                raise RuntimeError("ASR child PID could not be recorded")
                        except Exception:
                            self._terminate_process(process)
                            raise
                    deadline = time.monotonic() + self.process_timeout_seconds
                    while process.poll() is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._terminate_process(process)
                            raise RuntimeError(
                                f"ASR segment timed out after {self.process_timeout_seconds:g} seconds"
                            )
                        if self.stop.wait(min(0.5, remaining)):
                            self._terminate_process(process)
                            raise RuntimeError("ASR paused")
                    if process.returncode:
                        raise RuntimeError(f"ASR exited with status {process.returncode}")
            rows = self._read_segment_result(
                output, sequence, int(segment["start_offset_ms"]), str(manifest["started_at"]),
                device_id=device_id, session_id=session_id,
            )
            state.update(status="completed" if rows else "no_speech", rows=rows, error=None)
            state["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
            _atomic_json(state_path, state)
            if (
                rows
                and manifest.get("state") in {"receiving", "complete"}
                and build_asr_command is _DEFAULT_BUILD_ASR_COMMAND
            ):
                self._schedule_live_events_sync(device_id, session_id, config)
            return True
        except Exception as error:
            state = {"status": "failed", "sequence": sequence, "source_sha256": source_hash,
                     "attempt": attempt, "error": str(error)[:240],
                     "retry_at": time.time() + min(1800, 15 * 2 ** min(attempt, 7)),
                     "updated_at": datetime.now(LOCAL_TZ).isoformat()}
            _atomic_json(state_path, state)
            return True
        finally:
            self.slot_releaser(slot)

    def _run_inprocess_local_turbo_segment(
        self,
        *,
        audio: Path,
        output: Path,
        start_at: str,
        timezone_name: str,
        duration_ms: int,
    ) -> bool:
        try:
            from life_logger.domain import RecordingSegment
            from life_logger.transcribe import get_shared_turbo_transcriber

            duration_s = round(max(0.0, duration_ms / 1000.0), 3)
            user_start = None
            try:
                user_start = datetime.fromisoformat(start_at)
            except Exception:
                pass
            recordings = discover_recordings(
                audio,
                timezone_name=timezone_name,
                user_start=user_start,
            )
            if recordings:
                recording = recordings[0]
            else:
                recording = RecordingSegment(
                    recording_id=f"rec_{audio.stem}",
                    source_file=str(audio),
                    started_at=start_at,
                    ended_at=None,
                    duration_seconds=duration_s,
                    sequence=1,
                    time_source="user",
                    time_precision="exact",
                    ingestion_warnings=[],
                )
            transcriber = get_shared_turbo_transcriber()
            transcripts = transcriber.transcribe(recording)
            attr_map = getattr(transcriber, "_last_segment_attributions", {}) or {}
            transcript_records = []
            for t in transcripts:
                seg_attr = attr_map.get(t.segment_id, {})
                rec_row = {
                    "segment_id": t.segment_id,
                    "recording_id": t.recording_id,
                    "start_offset_seconds": t.start_offset_seconds,
                    "end_offset_seconds": t.end_offset_seconds,
                    "absolute_started_at": t.absolute_started_at,
                    "absolute_ended_at": t.absolute_ended_at,
                    # 【2026-10-17】绝对时刻是 UTC，直接看要心算 8 小时。
                    # 额外落一份本地时区字符串，排渣时不需要任何换算。
                    "started_at_local": _local_iso(t.absolute_started_at),
                    "clock_local": _local_clock(t.absolute_started_at),
                    "text": t.text,
                    "source": t.source,
                    "language": t.language,
                    "transcription_confidence": t.transcription_confidence,
                    "speaker": t.speaker,
                    "gender": getattr(t, "gender", None),
                    "emotion": t.emotion,
                }
                for attr_key in (
                    "source_label",
                    "voice_similarity",
                    "review_required",
                    "decision_reason",
                    "acoustic_proximity",
                    "proximity_score",
                    "proximity_confidence",
                    "snr_proxy_db",
                    "proximity_ratio_db",
                    *AUDIO_CLASSIFIER_RECORD_FIELDS,
                ):
                    if attr_key in seg_attr:
                        rec_row[attr_key] = seg_attr[attr_key]
                transcript_records.append(rec_row)
            rec_dict = recording.to_dict() if hasattr(recording, "to_dict") else {
                "recording_id": recording.recording_id,
                "source_file": str(audio),
                "started_at": recording.started_at,
                "duration_seconds": duration_s,
            }
            _atomic_json(output / "transcript.json", transcript_records)
            _atomic_json(output / "recordings.json", [rec_dict])
            _atomic_json(
                output / "run-summary.json",
                {
                    "status": "completed",
                    "source": str(audio),
                    "model": "Local-Whisper-Turbo-MLX",
                    "model_path": "mlx-community/whisper-large-v3-turbo",
                    "duration_s": duration_s,
                    "transcript_count": len(transcript_records),
                    "warnings": [],
                },
            )
            _atomic_json(
                output / "checkpoint.json",
                {
                    "status": "completed",
                    "source": str(audio),
                    "model_path": "mlx-community/whisper-large-v3-turbo",
                    "duration_s": duration_s,
                    "transcripts": transcript_records,
                },
            )
            return True
        except Exception:
            return False

    def _run_resident_moss_segment(
        self,
        *,
        audio: Path,
        output: Path,
        start_at: str,
        timezone_name: str,
        duration_ms: int,
        config: Mapping[str, Any],
    ) -> bool:
        """常驻 MOSS：模型只加载一次；CAM++ 仍在本进程（FunASR venv）里跑。

        产物（transcript.json/recordings.json/run-summary.json）与旧
        `run_cloud_asr_pipeline.py --provider moss` 保持同一形状，
        `_read_segment_result` 不需要任何改动。失败返回 False → 调用方回退旧路径。
        """
        try:
            from dataclasses import replace

            from life_logger import moss_transcribe

            duration_s = round(max(0.0, duration_ms / 1000.0), 3)
            recording_id = f"rec_{audio.stem}"
            transcripts, stats = moss_transcribe.transcribe_file(
                audio, recording_id=recording_id,
                hotwords=config.get("moss_hotwords") if isinstance(config.get("moss_hotwords"), str) else None,
            )
            base = datetime.fromisoformat(start_at)
            segment_attributions = stats.get("attributions") or {}
            rows = []
            for t in transcripts:
                started = replace(t, absolute_started_at=(
                    base + timedelta(seconds=float(t.start_offset_seconds or 0.0))).isoformat(),
                    absolute_ended_at=(base + timedelta(seconds=float(t.end_offset_seconds or 0.0))).isoformat())
                row = {
                    "segment_id": started.segment_id, "recording_id": started.recording_id,
                    "start_offset_seconds": started.start_offset_seconds,
                    "end_offset_seconds": started.end_offset_seconds,
                    "absolute_started_at": started.absolute_started_at,
                    "absolute_ended_at": started.absolute_ended_at,
                    "started_at_local": _local_iso(started.absolute_started_at),
                    "clock_local": _local_clock(started.absolute_started_at),
                    "text": started.text, "source": started.source, "language": started.language,
                    "transcription_confidence": started.transcription_confidence,
                    "speaker": started.speaker,
                    "gender": getattr(started, "gender", None), "emotion": started.emotion,
                }
                attr = segment_attributions.get(started.segment_id) or {}
                for key in ("source_label", "voice_similarity", "acoustic_proximity", "review_required",
                            "decision_reason", "proximity_score", "proximity_confidence",
                            "snr_proxy_db", "proximity_ratio_db", *AUDIO_CLASSIFIER_RECORD_FIELDS,
                            "attribution_granularity", "speaker_group_key", "speaker_group_spans",
                            "speaker_group_seconds"):
                    if key in attr:
                        row[key] = attr[key]
                rows.append(row)
            recording = {
                "recording_id": recording_id, "source_file": str(audio), "started_at": start_at,
                "duration_seconds": duration_s, "timezone": timezone_name,
            }
            _atomic_json(output / "transcript.json", rows)
            _atomic_json(output / "recordings.json", [recording])
            _atomic_json(output / "run-summary.json", {
                "status": "completed", "source": str(audio),
                "model": "MOSS-Transcribe-Diarize-0.9B", "model_path": "resident-inprocess",
                "duration_s": duration_s, "transcript_count": len(rows), "warnings": stats.get("warnings") or [],
            })
            _atomic_json(output / "checkpoint.json", {
                "status": "completed", "source": str(audio), "model_path": "resident-inprocess",
                "duration_s": duration_s, "transcripts": rows,
            })
            return True
        except Exception:
            return False

    def _voiceprint_pool_path(self, config: Mapping[str, Any], device_id: str) -> Path | None:
        """[Q806] 该设备共用的声纹池文件路径（可在配置里关掉或改位置）。

        约定：
          * `voiceprint_pool.enabled=false` → 返回 None（完全退回旧行为）
          * `voiceprint_pool.path` 给了就用它（{device} 占位）
          * 默认 `<watch-sessions 根>/voiceprints/pool-<device>.json`
        """
        settings = config.get("voiceprint_pool") if isinstance(config, Mapping) else None
        settings = settings if isinstance(settings, Mapping) else {}
        if settings.get("enabled") is False:
            return None
        root = getattr(self.store, "root", None)
        raw = str(settings.get("path") or "").strip()
        try:
            if raw:
                return Path(raw.replace("{device}", str(device_id)))
            if root is None:
                return None
            return Path(root) / "voiceprints" / f"pool-{device_id}.json"
        except Exception:
            return None

    def _schedule_live_events_sync(
        self, device_id: str, session_id: str, config: dict[str, Any]
    ) -> None:
        activity_settings = config.get("activity_timeline", {})
        if isinstance(activity_settings, dict) and activity_settings.get("enabled") is True:
            # Formal ASR still saves every row; the activity worker consumes it on its own cadence.
            return
        key = (device_id, session_id)
        with self._live_sync_lock:
            if key in self._live_sync_active:
                self._live_sync_pending.add(key)
                return
            self._live_sync_active.add(key)

        def _worker() -> None:
            while True:
                try:
                    self._sync_live_session_events(device_id, session_id, config)
                except Exception:
                    pass
                with self._live_sync_lock:
                    if key in self._live_sync_pending:
                        self._live_sync_pending.discard(key)
                        continue
                    self._live_sync_active.discard(key)
                    break

        threading.Thread(
            target=_worker,
            name=f"watch-live-events-{session_id[:8]}",
            daemon=True,
        ).start()

    def _sync_live_session_events(
        self, device_id: str, session_id: str, config: dict[str, Any]
    ) -> None:
        from life_logger.contextual_extraction import run_contextual_extraction
        from life_logger.dictation import load_dictation_windows
        from scripts.attribute_funasr_transcript import is_dictation_record

        # 【2026-10-14 修】原来这里调 is_dictation_record(row_copy, []) —— 传的是空窗口，
        # 时间窗判据永远为 False，只剩关键词判据。而 MOSS 现在是唯一转写路径，
        # 等于「在电脑前语音输入」的分流能力在生产链路上完全失效：
        # 实测 「因为今天我一天都在Hermes上面优化了一整天，我发现效率有点慢。」
        # （落在 airpassport-ptt 上报的 00:58:44~00:59:03 语音输入窗内）被当成
        # work_communication 正式生活事件，confidence 0.92。
        # 其它调用点（attribute_funasr_transcript.py / reprocess_contextual_day.py）
        # 都传了真实窗口，只有这里漏了。窗口索引由 scripts/watch_airpassport.py 维护。
        dictation_windows = load_dictation_windows(
            PROJECT_ROOT / "output" / "local-web" / "dictation-windows.jsonl"
        )

        deepseek_cfg = config.get("deepseek", {}) if isinstance(config.get("deepseek"), dict) else {}
        api_key = str(deepseek_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()

        manifest = self._manifest(device_id, session_id)
        session_dir = self.store.session_directory(device_id, session_id)
        root = self._directory(device_id, session_id)
        rows: list[dict[str, Any]] = []
        for segment in sorted(manifest.get("segments", []), key=lambda item: int(item.get("sequence", -1))):
            seq = int(segment["sequence"])
            st = _read_json(self._segment_state_path(root, seq), {})
            if isinstance(st, dict) and st.get("status") == "completed":
                for r in st.get("rows", []):
                    if isinstance(r, dict) and str(r.get("text", "")).strip():
                        raw_txt = str(r.get("text", "")).strip()
                        if is_hallucinated_asr_noise(raw_txt):
                            continue
                        row_copy = dict(r)
                        row_copy["recording_id"] = f"watch_{session_id}"
                        prox_label = str(row_copy.get("acoustic_proximity") or "")
                        prox_ratio = row_copy.get("proximity_ratio_db")
                        prox_score = row_copy.get("proximity_score")
                        # 【2026-10-14 修 · 第 4 处「远场一票否决」】
                        # 本处上游（moss_transcribe / transcribe 的 _score_window）已做
                        # 声纹打分 + 近讲场域融合，并给出 source_label / decision_reason。
                        # 旧逻辑只按 acoustic_proximity=="far" 就把结论无条件改写成
                        # non_self —— 等于把上游的修复原地撤销。「录远了」只是声学退化，
                        # 不等于「不是本人」。实测样本：上游 raw=0.7324 / decision_reason=
                        # "above_self_threshold" / source_label="self"，若该段恰好远场就会被压掉。
                        # 修法与已修的另外 3 处一致：上游有结论就原样复用；只有拿不到上游
                        # 声纹时才退化为远近场兜底。远场额外打复核标记（降权但不推翻）。
                        upstream_label = str(row_copy.get("source_label") or "").strip()
                        upstream_decided = bool(upstream_label in {"self", "non_self", "unknown"}) and (
                            row_copy.get("voice_similarity") is not None
                            or str(row_copy.get("decision_reason") or "").strip() != ""
                        )
                        is_far = prox_label == "far" or (
                            prox_ratio is not None and float(prox_ratio) < -1.5
                        )
                        if upstream_decided:
                            row_copy["source_label"] = upstream_label
                            row_copy["speaker"] = row_copy.get("speaker") or upstream_label
                            if is_far:
                                row_copy["far_field_review_required"] = True
                        elif is_far:
                            row_copy["source_label"] = "non_self"
                            row_copy["speaker"] = "non_self"
                        elif (
                            prox_label == "uncertain"
                            and prox_score is not None
                            and float(prox_score) < 0.45
                            and (prox_ratio is None or float(prox_ratio) < 4.0)
                        ):
                            row_copy["source_label"] = "non_self"
                            row_copy["speaker"] = "non_self"
                        else:
                            row_copy["source_label"] = row_copy.get("source_label") or (
                                "non_self" if row_copy.get("speaker") == "non_self" else "self"
                            )
                        row_copy["is_dictation"] = is_dictation_record(
                            row_copy, dictation_windows
                        )
                        rows.append(row_copy)
        if not rows:
            return
        rows.sort(key=lambda r: (_transcript_sort_offset(r), str(r.get("segment_id") or "")))
        live_ctx_dir = root / "live-context"
        live_ctx_dir.mkdir(parents=True, exist_ok=True)

        formal_events: list[dict[str, Any]] = []

        # ── 线下商户收款音箱播报与口头点单消费合成（如"支付到账2元"、"点两个冰杯 两块呀"）──
        from life_logger.contextual_extraction import (
            synthesize_pos_payment_events,
            synthesize_conversational_orders,
        )

        for pe in synthesize_pos_payment_events(rows):
            formal_events.append(pe)
        for ce in synthesize_conversational_orders(rows):
            formal_events.append(ce)

        if api_key:
            # 可选：本地焦点窗口产物（缺文件 = 旧路等价；不启动常驻、不读窗口文本）
            context_stats: dict[str, int] = {"resolved": 0, "unknown": 0, "windows": 0}
            try:
                # [2026-11-04] **必须按录音所属日期加载**。
                # `load_input_context_windows(root)` 不传 day 时只读 `latest.json`（最近 30 分钟滚动窗口），
                # 而这里处理的是**历史录音**——时间上永远不可能重叠，实测 11-04 的 214 行里 0 条 resolved。
                # 传 day 后走按日归档 `input-context/days/<day>.jsonl`，同一天实测 204/214（95%）可解析。
                # 录音可能跨日，所以按 rows 里出现的日期集合逐个加载并合并去重。
                _ctx_days = sorted({
                    str(r.get("absolute_started_at") or "")[:10]
                    for r in rows
                    if str(r.get("absolute_started_at") or "")[:10]
                })
                _ctx_windows = []
                _seen_win = set()
                for _d in _ctx_days:
                    for _w in load_input_context_windows(self.store.root, _d):
                        _k = (_w.get("started_at"), _w.get("ended_at"), _w.get("target_app"))
                        if _k in _seen_win:
                            continue
                        _seen_win.add(_k)
                        _ctx_windows.append(_w)
                if not _ctx_windows:      # 日期都拿不到时退回旧行为（至少不更差）
                    _ctx_windows = load_input_context_windows(self.store.root)
                context_stats = attach_watch_input_context(rows, _ctx_windows)
            except Exception as error:      # 上下文失败绝不能阻断正式抽取
                logger.warning("input context attach skipped: %s", error)
            ctx_report = run_contextual_extraction(
                rows,
                deepseek_api_key=api_key,
                deepseek_base_url=str(deepseek_cfg.get("base_url") or "").strip() or None,
                deepseek_model=str(deepseek_cfg.get("model") or "").strip() or None,
                output_dir=live_ctx_dir,
            )
            raw_formal = ctx_report.get("formal_events", [])
            if isinstance(raw_formal, list):
                existing_ev_ids = {str(ev.get("event_id") or "") for ev in formal_events if ev.get("event_id")}
                for ev in raw_formal:
                    if not isinstance(ev, dict):
                        continue
                    eid = str(ev.get("event_id") or "")
                    if eid and eid in existing_ev_ids:
                        continue
                    if eid:
                        existing_ev_ids.add(eid)
                    formal_events.append(dict(ev))

        existing_quotes = {
            str(ev.get("evidence", {}).get("quote") or "")
            for ev in formal_events
        }
        existing_titles = {str(ev.get("title") or "") for ev in formal_events}

        memo_re = re.compile(
            r"(?:^|[\s，,。；;、？?！!]|其他的|另外|还有)(?:现在|请|帮我|我要|我想|然后|呃|那)?(?:记录一下|记一下|记录下|记下|记个|备忘一下|备忘下|帮我记|我要记)[:：，,。.\s]*(.*)",
            re.IGNORECASE,
        )
        query_re = re.compile(
            r"(?:^|[\s，,。；;、？?！!]|其他的|另外|还有)(?:现在|请|帮我|我要|我想|你|再|然后|麻烦)?(?:查询一下|查一下|自查一下|帮我查|我要查|检查一下|调查一下|问一下)[:：，,。.\s]*(.*)",
            re.IGNORECASE,
        )

        for idx, r in enumerate(rows):
            # Unknown voices remain in live-context review candidates; a spoken
            # memo/query marker cannot authorize a formal self event.
            if r.get("source_label") != "self":
                continue
            text = str(r.get("text") or "").strip()
            if not text:
                continue

            memo_m = memo_re.search(text)
            query_m = query_re.search(text)

            if not memo_m and not query_m:
                continue

            # Look ahead up to 3 subsequent self utterances within 25s if content is empty or a header
            cur_offset = float(r.get("start_offset_seconds") or 0.0)
            followups: list[str] = []
            for next_r in rows[idx + 1 : idx + 4]:
                if next_r.get("source_label") == "non_self" or next_r.get("speaker") == "non_self":
                    continue
                next_offset = float(next_r.get("start_offset_seconds") or 0.0)
                if next_offset - cur_offset > 25.0:
                    break
                next_txt = str(next_r.get("text") or "").strip()
                if not next_txt or next_txt in {"然后……", "然后", "嗯", "好"}:
                    continue
                if memo_re.search(next_txt) or query_re.search(next_txt):
                    # If the next utterance itself starts a new memo/query, only take the prefix before it
                    sub_m = memo_re.search(next_txt) or query_re.search(next_txt)
                    prefix = next_txt[: sub_m.start()].strip("，, 。. :： ")
                    if prefix:
                        followups.append(prefix)
                    break
                followups.append(next_txt)

            if memo_m:
                content = memo_m.group(1).strip("，, 。. :： ")
                row_reported_at = str(r.get("absolute_started_at") or "")
                same_time_events = [
                    ev for ev in formal_events
                    if str((ev.get("evidence") or {}).get("reported_at") or "") == row_reported_at
                ]
                if not content and followups:
                    # Check if the first followup is already a formal event; if so, upgrade it to pinned_memo
                    first_followup = followups[0]
                    upgraded = False
                    for ev in formal_events:
                        ev_q = str(ev.get("evidence", {}).get("quote") or "")
                        if ev_q and (first_followup in ev_q or ev_q in first_followup):
                            ev["pinned_memo"] = True
                            if isinstance(ev.get("evidence"), dict):
                                ev["evidence"]["pinned_memo"] = True
                            cur_title = str(ev.get("title") or "")
                            if not cur_title.startswith(("便签：", "记录：")):
                                ev["title"] = f"便签：{cur_title}"
                            upgraded = True
                            break
                    if upgraded:
                        continue
                    content = " ".join(followups[:2]).strip("，, 。. :： ")
                elif (content in {"购买清单", "问题"} or content.startswith("问题")) and followups:
                    extra = "；".join(f.strip("，, 。. ") for f in followups[:2] if f.strip("，, 。. "))
                    if extra:
                        content = f"{content}（{extra}）"
                    if same_time_events:
                        target_ev = same_time_events[0]
                        target_ev["title"] = f"便签：{content}"
                        target_ev["pinned_memo"] = True
                        target_ev["categories"] = ["todo"]
                        if isinstance(target_ev.get("evidence"), dict):
                            target_ev["evidence"]["pinned_memo"] = True
                            target_ev["evidence"]["quote"] = f"{text} {' '.join(followups[:2])}".strip()
                        existing_titles.add(target_ev["title"])
                        continue

                if not content:
                    continue
                if same_time_events and memo_m.start() <= 4:
                    # DeepSeek already extracted event(s) for this exact utterance
                    for ev in same_time_events:
                        ev["pinned_memo"] = True
                        if isinstance(ev.get("evidence"), dict):
                            ev["evidence"]["pinned_memo"] = True
                    continue
                if any(content in q for q in existing_quotes if len(q) >= 6):
                    continue
                title = f"便签：{content}"
                if title in existing_titles:
                    continue
                full_quote = f"{text} {' '.join(followups[:2])}".strip() if followups and len(text) <= 10 else text
                evt_id = f"evt_ctx_{uuid.uuid4().hex[:16]}"
                synthesized = {
                    "activity_type": "task",
                    "capture_mode": "passive",
                    "categories": ["todo"],
                    "confidence": 0.95,
                    "data": {"todo": {}},
                    "event_id": evt_id,
                    "evidence": {
                        "claim_type": "intent",
                        "deepseek_model": "rule_fallback",
                        "dictation_context": False,
                        "end_offset_seconds": r.get("end_offset_seconds"),
                        "event_state": "completed",
                        "extractor": "voice_trigger_fallback",
                        "occurrence_time_evidence": None,
                        "pending_reason": None,
                        "proposed_category": "todo",
                        "proposed_expense_amount": None,
                        "quote": full_quote,
                        "recording_id": f"watch_{session_id}",
                        "reported_at": r.get("absolute_started_at"),
                        "segment_id": r.get("segment_id"),
                        "source_id": str(r.get("source_id") or ""),
                        "source_label": "self",
                        "source_review_required": False,
                        "speaker_fused_reason": None,
                        "speaker_fused_score": None,
                        "start_offset_seconds": r.get("start_offset_seconds"),
                        "temporal_relation": "at_speech",
                        "topic": "待办便签",
                        "pinned_memo": True,
                    },
                    "pinned_memo": True,
                    "review_status": "reviewed",
                    "source_type": "audio",
                    "time": {
                        "absolute": r.get("absolute_started_at"),
                        "precision": "exact",
                        "relative_seconds": r.get("start_offset_seconds"),
                    },
                    "title": title,
                }
                formal_events.append(synthesized)
                existing_titles.add(title)
                existing_quotes.add(full_quote)
            elif query_m:
                content = query_m.group(1).strip("，, 。. :： ")
                row_reported_at = str(r.get("absolute_started_at") or "")
                same_time_events = [
                    ev for ev in formal_events
                    if str((ev.get("evidence") or {}).get("reported_at") or "") == row_reported_at
                ]
                if not content and followups:
                    content = " ".join(followups[:2]).strip("，, 。. :： ")
                if not content:
                    continue
                if same_time_events and query_m.start() <= 6:
                    for ev in same_time_events:
                        ev["query_dispatch"] = True
                        if isinstance(ev.get("evidence"), dict):
                            ev["evidence"]["query_dispatch"] = True
                    continue
                if any(content in q for q in existing_quotes if len(q) >= 6):
                    continue
                title = f"查询：{content}"
                if title in existing_titles:
                    continue
                full_quote = f"{text} {' '.join(followups[:1])}".strip() if followups and len(content) <= 25 else text
                evt_id = f"evt_ctx_{uuid.uuid4().hex[:16]}"
                synthesized = {
                    "activity_type": "task",
                    "capture_mode": "passive",
                    "categories": ["todo"],
                    "confidence": 0.95,
                    "data": {"todo": {}},
                    "event_id": evt_id,
                    "evidence": {
                        "claim_type": "task",
                        "deepseek_model": "rule_fallback",
                        "dictation_context": False,
                        "end_offset_seconds": r.get("end_offset_seconds"),
                        "event_state": "planned",
                        "extractor": "voice_trigger_fallback",
                        "occurrence_time_evidence": None,
                        "pending_reason": None,
                        "proposed_category": "todo",
                        "proposed_expense_amount": None,
                        "quote": full_quote,
                        "recording_id": f"watch_{session_id}",
                        "reported_at": r.get("absolute_started_at"),
                        "segment_id": r.get("segment_id"),
                        "source_id": str(r.get("source_id") or ""),
                        "source_label": "self",
                        "source_review_required": False,
                        "speaker_fused_reason": None,
                        "speaker_fused_score": None,
                        "start_offset_seconds": r.get("start_offset_seconds"),
                        "temporal_relation": "at_speech",
                        "topic": "查询任务",
                        "query_dispatch": True,
                    },
                    "query_dispatch": True,
                    "review_status": "reviewed",
                    "source_type": "audio",
                    "time": {
                        "absolute": r.get("absolute_started_at"),
                        "precision": "exact",
                        "relative_seconds": r.get("start_offset_seconds"),
                    },
                    "title": title,
                }
                formal_events.append(synthesized)
                existing_titles.add(title)
                existing_quotes.add(full_quote)

        # ── 线下商户收款音箱播报消费合成（如"支付到账2元"、"微信收款15元"）──
        from life_logger.contextual_extraction import synthesize_pos_payment_events

        for pe in synthesize_pos_payment_events(rows):
            if pe["title"] not in existing_titles and pe["evidence"]["quote"] not in existing_quotes:
                formal_events.append(pe)
                existing_titles.add(pe["title"])
                existing_quotes.add(pe["evidence"]["quote"])

        # 【2026-10-18】会话级聚合（Session Aggregator）接入生产链路。
        # 背景：`session_aggregator.py` 早就写好了（把 15~60 分钟的连续会话整段喂给
        # DeepSeek 的百万上下文，提炼完整训练组/会议决议/菜品），但**从未被任何生产
        # 调用方引用**——等于一个只存在于测试里的能力。
        # 现在接在批次抽取之后：批次回答「这一句是什么」，会话级回答「这几十分钟干了什么」。
        try:
            session_events, session_stats = self._aggregate_session_level_events(
                device_id, session_id, rows, formal_events, config
            )
            formal_events.extend(session_events)
            # 落一份旁证：证明这层**真的被调用过**、以及为了什么被跳过。
            # 没有这份文件，「接进来了」只能靠读代码相信。
            _atomic_json(
                root / "live-context" / "session-aggregation.json",
                {
                    "session_id": session_id,
                    "device_id": device_id,
                    "recorded_at": datetime.now(LOCAL_TZ).isoformat(),
                    **session_stats,
                },
            )
        except Exception as error:  # 会话级失败不能拖垮整场事件
            print(
                f"[life-logger] session aggregation 跳过: {type(error).__name__} {error}",
                flush=True,
            )

        formal_events.sort(
            key=lambda ev: (
                str(ev.get("time", {}).get("absolute") or ev.get("evidence", {}).get("reported_at") or ""),
                float(ev.get("time", {}).get("relative_seconds") or 0.0),
            )
        )

        # 【2026-10-17】剔除「复盘句」造成的重复消费。
        # 实测：14:49 POS 播报“支付到账一点八元”已生成 ¥1.80；16:34 用户在家对账说
        # “今天出门一共是买了一块八的一个可乐”，又被算成第二笔 ¥1.80，全天虚报。
        try:
            from life_logger.contextual_extraction import collapse_recap_expenses

            formal_events = collapse_recap_expenses(formal_events)
        except Exception as exc:  # 去重挂了不能拖垓主链路
            print(f"[life-logger] collapse_recap_expenses 跳过: {exc}", flush=True)

        # 【2026-10-22】合并「一句话被 ASR 切成多段」造成的重复消费。
        # 实测 10-21：一次录音的连续分段 16/17/18（间隔 2s）各自抽出一条
        # 「花了26.8点了一份 / 丑宝汉堡王套餐 / 三件套26.8」，页面上成了 3 张卡。
        # 口径：同录音 + 60s 内 + 同商户/文本高度重叠 + 簇内只有 1 条带金额 → 合并成 1 条；
        # 被并的只打 suppressed/merged_into，原始事件与 quote 一律保留（零数据丢失）。
        try:
            from life_logger.contextual_extraction import merge_duplicate_expense_events

            formal_events = merge_duplicate_expense_events(formal_events)
        except Exception as exc:  # 合并失败不能拖垮主链路
            print(f"[life-logger] merge_duplicate_expense_events 跳过: {exc}", flush=True)

        # 【2026-10-17】落盘前给每条事件补上本地可读时间字段
        # （time.local / time.clock / time.date / evidence.*_local）。
        # 背景：原来只有 UTC 的 time.absolute，排查时脚本截 [11:19] 就把
        # 下午 14:56 的烧鸭饭读成了早上 06:56。存储仍保留 UTC 绝对时刻，
        # 只新增本地副本，下游读 UTC 的逻辑零影响。
        try:
            from life_logger.timeutil import annotate_local_time

            for ev in formal_events:
                if isinstance(ev, dict):
                    annotate_local_time(ev)
        except Exception as exc:  # 注释失败不能阻止事件落盘
            print(f"[life-logger] annotate_local_time 跳过: {exc}", flush=True)

        _atomic_json(session_dir / "events.json", formal_events)
        _bump_read_epoch(self.store.root, f"watch-events:{session_id}")

    # ---- 会话级聚合（session_aggregator） --------------------------------------
    #: 已知类别白名单：会话级模型输出必须落在既有七类里，否则丢弃（不许新造类别）。
    _SESSION_ALLOWED_CATEGORIES = {
        "routine", "food", "expense", "exercise",
        "work_communication", "work", "idea", "todo", "health",
    }

    def _aggregate_session_level_events(
        self,
        device_id: str,
        session_id: str,
        rows: list[dict[str, Any]],
        existing_events: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把整场会话交给 DeepSeek 做一次「全局理解」，产出的高价值事件并入事件表。

        触发条件（避免为几分钟的碎片白花一次百万上下文调用）：
          - 配置 `session_aggregation.enabled`（默认 true）
          - 会话时长 ≥ `min_duration_minutes`（默认 15）
          - self 行数 ≥ `min_rows`（默认 30）
          - 行数上限 `max_rows`（默认 4000，防止极端长会话输入失控）

        去重策略：会话级事件与批次事件表达同一件事时保留批次那条（更贴原话），
        会话级只补充批次漏掉的「跨分钟集合型」事件（如整套训练组）。
        """
        from life_logger.session_aggregator import aggregate_session_events

        settings = config.get("session_aggregation") if isinstance(config.get("session_aggregation"), dict) else {}
        enabled = bool(settings.get("enabled", True))
        min_minutes = float(settings.get("min_duration_minutes") or 15)
        min_rows = int(settings.get("min_rows") or 30)
        max_rows = int(settings.get("max_rows") or 4000)

        self_rows = [r for r in rows if str(r.get("source_label") or "") == "self"]
        duration_minutes = 0.0
        try:
            starts = [float(r.get("start_offset_seconds") or 0.0) for r in rows]
            ends = [float(r.get("end_offset_seconds") or 0.0) for r in rows]
            if starts and ends:
                duration_minutes = max(0.0, (max(ends) - min(starts)) / 60.0)
        except (TypeError, ValueError):
            duration_minutes = 0.0

        stats: dict[str, Any] = {
            "enabled": enabled,
            "rows": len(rows),
            "self_rows": len(self_rows),
            "duration_minutes": round(duration_minutes, 1),
            "min_duration_minutes": min_minutes,
            "min_rows": min_rows,
            "called": False,
            "produced": 0,
            "merged": 0,
            "skipped_duplicate": 0,
            "skipped_invalid": 0,
        }
        if not enabled:
            stats["skip_reason"] = "disabled"
            return [], stats
        if duration_minutes < min_minutes:
            stats["skip_reason"] = "too_short"
            return [], stats
        if len(self_rows) < min_rows:
            stats["skip_reason"] = "too_few_self_rows"
            return [], stats

        session_rows = rows[:max_rows]
        stats["called"] = True
        started = time.monotonic()
        raw_events = aggregate_session_events(session_rows)
        stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
        stats["produced"] = len(raw_events or [])
        # 【2026-10-25 Q391】接收侧**不盲信**聚合结果：落盘前再用真实段行边界校验一次时间证据。
        # 越界/解析失败的事件会变成 pending_review + unknown（保留 model_reported_time 审计），
        # 绝不把日期覆盖成会话日期（那等于伪造发生时间）。
        from life_logger.session_aggregator import validate_aggregated_events

        raw_events = validate_aggregated_events(raw_events or [], session_rows)
        stats["time_isolated"] = sum(
            1 for event in raw_events if event.get("review_status") == "pending_review"
        )

        merged: list[dict[str, Any]] = []
        for raw in raw_events or []:
            if not isinstance(raw, dict):
                stats["skipped_invalid"] += 1
                continue
            title = str(raw.get("title") or "").strip()
            categories = [str(c) for c in (raw.get("categories") or []) if str(c) in self._SESSION_ALLOWED_CATEGORIES]
            if not title or not categories:
                stats["skipped_invalid"] += 1
                continue
            if self._session_event_is_duplicate(raw, categories, existing_events, merged):
                stats["skipped_duplicate"] += 1
                continue
            raw_time = raw.get("time") if isinstance(raw.get("time"), dict) else {}
            absolute = _local_iso(raw_time.get("absolute"))
            ended = _local_iso(raw_time.get("end"))
            evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
            quote = str(evidence.get("quote") or title)[:200]
            # 【2026-10-25 Q396】上游 validator 的时间隔离状态必须**原样保留**：
            # 之前这里硬编码 reviewed，把 pending_review 洗白，且丢掉 pending_reason /
            # model_reported_time / session_bounds / 源引用。现在只**显式**保留合同字段
            # （不盲拷整个 raw，避免模型恶意字段混入）。
            upstream_status = str(raw.get("review_status") or "").strip()
            time_isolated = upstream_status == "pending_review"
            if time_isolated:
                absolute = None
                ended = None
            inherited_evidence = {
                key: evidence.get(key)
                for key in (
                    "session_bounds", "session_source_ids", "session_segment_ids",
                    "source_ids", "segment_ids", "session_scope",
                )
                if evidence.get(key) is not None
            }
            merged.append({
                "event_id": "evt_session_" + hashlib.sha256(
                    f"{session_id}:{title}:{absolute or ''}".encode()
                ).hexdigest()[:16],
                "title": title[:120],
                "categories": categories,
                "activity_type": str(raw.get("activity_type") or ("workout" if "exercise" in categories else "general")),
                "confidence": 0.75,
                # 【2026-10-18】会话级对闲聊特别容易产出低价值条目（实测真实调用给出过
                # 「哼唱情歌片段」「晨间睡眠状况闲聊」，类别 routine）。这类不该直接
                # 冒充正式生活事件进时间轴，但也不能丢 —— 降级为待复核，人来决定。
                # 合法事件沿用旧规则（routine → unreviewed，其余 reviewed）；
                # 被上游隔离的事件保持 pending_review，绝不洗白。
                "review_status": (
                    "pending_review"
                    if time_isolated
                    else ("unreviewed" if categories == ["routine"] else "reviewed")
                ),
                **(
                    {"pending_reason": raw.get("pending_reason") or "time_outside_session",
                     "model_reported_time": raw.get("model_reported_time"),
                     "model_response_time_raw": raw.get("model_response_time_raw")}
                    if time_isolated else {}
                ),
                **({"retrospective": True} if raw.get("retrospective") else {}),
                "source_type": "audio",
                "capture_mode": "session_aggregation",
                "time": {
                    "absolute": absolute,
                    "absolute_ended_at": ended,
                    "precision": "exact" if absolute else "unknown",
                    "relative_seconds": None,
                },
                "evidence": {
                    "quote": quote,
                    "reported_at": absolute,
                    "recording_id": f"watch_{session_id}",
                    "source_label": "self",
                    # 让下游/看板能一眼看出这条是「整场会话级理解」而不是单句抽取
                    "extractor": "session_aggregator",
                    "session_scope": {
                        "rows": len(session_rows),
                        "duration_minutes": stats["duration_minutes"],
                    },
                    **inherited_evidence,
                },
                "data": raw.get("data") if isinstance(raw.get("data"), dict) else {},
            })

        stats["merged"] = len(merged)
        return merged, stats

    @staticmethod
    def _session_event_is_duplicate(
        raw: Mapping[str, Any],
        categories: list[str],
        existing_events: list[Mapping[str, Any]],
        merged: list[Mapping[str, Any]],
    ) -> bool:
        """会话级事件与已有事件是否重复（同一件事不重复计入）。"""
        title = str(raw.get("title") or "").strip()
        amount = None
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        try:
            expense = data.get("expense") if isinstance(data.get("expense"), dict) else {}
            if expense.get("amount") is not None:
                amount = float(expense["amount"])
        except (TypeError, ValueError):
            amount = None
        tokens = {t for t in re.findall(r"[\u4e00-\u9fa5]{2,}", title)}

        for candidate in list(existing_events) + list(merged):
            cand_categories = [str(c) for c in (candidate.get("categories") or [])]
            if not set(categories) & set(cand_categories):
                continue
            try:
                cand_amount = ((candidate.get("data") or {}).get("expense") or {}).get("amount")
                if amount is not None and cand_amount is not None and abs(float(cand_amount) - amount) < 1e-6:
                    return True
            except (TypeError, ValueError, AttributeError):
                pass
            cand_title = str(candidate.get("title") or "")
            overlap = tokens & {t for t in re.findall(r"[\u4e00-\u9fa5]{2,}", cand_title)}
            if len(overlap) >= 2:
                return True
        return False

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def scan_once(self) -> bool:
        """Try one segment, rotating between sessions to avoid backlog starvation."""
        if self.stop.is_set() or not self._scan_lock.acquire(blocking=False):
            return False
        try:
            summaries = self.store.list_session_summaries(limit=None)
            # 【2026-11-06 Q958】先旧后新。store 给的是**新→旧**（前端要这个顺序），
            # 直接拿来做调度就变成"越新的会话越先转"，11-06 白天那段 525 段的老会话
            # 永远排在 17:42/21:51/22:51 之后 —— 用户看到的就是"9-16 点全没转写"。
            summaries = sorted(summaries, key=lambda item: str(item.get("started_at") or ""))
            if self._session_cursor is not None:
                cursor_index = next(
                    (
                        index
                        for index, summary in enumerate(summaries)
                        if (
                            str(summary.get("device_id") or ""),
                            str(summary.get("session_id") or ""),
                        )
                        == self._session_cursor
                    ),
                    None,
                )
                if cursor_index is not None:
                    summaries = summaries[cursor_index + 1 :] + summaries[: cursor_index + 1]
            for summary in summaries:
                if self.stop.is_set():
                    return False
                device, session = str(summary["device_id"]), str(summary["session_id"])
                manifest = self._manifest(device, session)
                if manifest.get("job_id"):
                    continue
                for segment in sorted(manifest.get("segments", []), key=lambda item: int(item.get("sequence", -1))):
                    if self.stop.is_set():
                        return False
                    if self._process_segment(device, session, manifest, segment):
                        self._annotate_scene(device, session, segment)
                        self._session_cursor = (device, session)
                        return True
            return False
        finally:
            self._scan_lock.release()

    def summary_for_session(
        self, device_id: str, session_id: str, *, include_transcripts: bool = True
    ) -> dict[str, Any]:
        manifest = self._manifest(device_id, session_id)
        root = self._directory(device_id, session_id)
        counts = {key: 0 for key in ("pending", "processing", "completed", "no_speech", "failed")}
        transcripts: list[dict[str, Any]] = []
        retry_times: list[float] = []
        checkpoint_count = 0
        transcript_count = 0
        now = time.time()
        current_segment_record: dict[str, Any] | None = None
        latest_completed_record: dict[str, Any] | None = None
        first_pending_record: dict[str, Any] | None = None

        started_at_str = str(manifest.get("started_at") or "")
        try:
            started_dt = datetime.fromisoformat(started_at_str)
        except (ValueError, TypeError):
            started_dt = None

        def _calc_segment_range(seg: dict[str, Any]) -> tuple[str | None, str | None]:
            if started_dt is None:
                return None, None
            try:
                offset_ms = int(seg.get("start_offset_ms", 0) or 0)
                dur_ms = int(seg.get("duration_ms", 0) or 0)
                start_dt = started_dt + timedelta(milliseconds=offset_ms)
                end_dt = start_dt + timedelta(milliseconds=dur_ms)
                return start_dt.isoformat(), end_dt.isoformat()
            except (ValueError, TypeError, OverflowError):
                return None, None

        segments = manifest.get("segments", [])
        for segment in segments:
            sequence = int(segment["sequence"])
            state_path = self._segment_state_path(root, sequence)
            if state_path.is_file():
                checkpoint_count += 1
            state = _read_json(state_path, {})
            if not isinstance(state, dict):
                state = {}
            if state.get("source_sha256") != segment.get("sha256") or state.get("status") not in counts:
                status = "pending"
            else:
                status = state["status"]
            counts[status] += 1
            if status == "processing" and current_segment_record is None:
                s_time, e_time = _calc_segment_range(segment)
                current_segment_record = {
                    "sequence": sequence,
                    "start_at": s_time,
                    "end_at": e_time,
                    "status": "processing",
                }
            elif status in {"completed", "no_speech"}:
                s_time, e_time = _calc_segment_range(segment)
                latest_completed_record = {
                    "sequence": sequence,
                    "start_at": s_time,
                    "end_at": e_time,
                    "status": status,
                }
            elif status == "pending" and first_pending_record is None:
                s_time, e_time = _calc_segment_range(segment)
                first_pending_record = {
                    "sequence": sequence,
                    "start_at": s_time,
                    "end_at": e_time,
                    "status": "pending",
                }
            if status == "failed":
                try:
                    retry_at = float(state.get("retry_at", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    retry_at = 0
                if math.isfinite(retry_at) and retry_at > now:
                    retry_times.append(retry_at)
            if status == "completed":
                for row in state.get("rows", []):
                    if isinstance(row, dict):
                        transcript_count += 1
                        if include_transcripts:
                            transcripts.append({key: row.get(key) for key in (
                                "segment_id", "recording_id", "start_offset_seconds", "end_offset_seconds",
                                "absolute_started_at", "absolute_ended_at", "started_at_local", "clock_local", "text", "source", "language", "timestamp_type",
                                "source_label", "speaker", "emotion", "voice_similarity", "acoustic_proximity")}
                                | {"checkpoint_updated_at": state.get("updated_at")})
        if include_transcripts:
            transcripts.sort(key=lambda row: (_transcript_sort_offset(row), str(row.get("segment_id") or "")))
        manifest_complete = manifest.get("state") == "complete"
        all_terminal = counts["pending"] == counts["processing"] == counts["failed"] == 0

        active_segment = current_segment_record or (first_pending_record if counts["pending"] > 0 else latest_completed_record)
        queued_until = None
        if segments:
            _, queued_until = _calc_segment_range(segments[-1])

        return {
            "status": "completed" if manifest_complete and all_terminal else "processing",
            "counts": counts,
            "transcript_count": transcript_count,
            "transcripts": transcripts[-30:] if include_transcripts else [],
            "checkpoint_count": checkpoint_count,
            "current_segment": active_segment,
            "latest_completed_segment": latest_completed_record,
            "queued_until": queued_until,
            "total_segments": len(segments),
            "automatic_retry": {
                "failed_segments": counts["failed"],
                "waiting_segments": len(retry_times),
                "next_retry_in_seconds": (
                    max(1, math.ceil(min(retry_times) - now)) if retry_times else None
                ),
            },
        }

    def ready_for_session(self, device_id: str, session_id: str) -> bool:
        manifest = self._manifest(device_id, session_id)
        if manifest.get("state") != "complete":
            return False
        segments = manifest.get("segments")
        expected = manifest.get("expected_segments")
        if not isinstance(segments, list) or isinstance(expected, bool) or not isinstance(expected, int) or len(segments) != expected:
            return False
        indexed = {int(item.get("sequence", -1)): item for item in segments if isinstance(item, dict)}
        if set(indexed) != set(range(expected)):
            return False
        root = self._directory(device_id, session_id)
        for sequence, segment in indexed.items():
            state = _read_json(self._segment_state_path(root, sequence), {})
            if not isinstance(state, dict):
                return False
            if state.get("source_sha256") != segment.get("sha256") or state.get("status") not in {"completed", "no_speech"}:
                return False
            rows = state.get("rows")
            if not isinstance(rows, list) or any(
                not isinstance(row, dict)
                or not isinstance(row.get("text"), str)
                or not row["text"].strip()
                for row in rows
            ):
                return False
            audio = self.store.session_directory(device_id, session_id) / "segments" / f"segment-{sequence:06d}.m4a"
            try:
                if _digest(audio) != segment.get("sha256"):
                    return False
            except OSError:
                return False
        return True

    def materialize_for_job(
        self, device_id: str, session_id: str, recording_path: Path,
        target_asr_dir: Path, timezone_name: str,
    ) -> bool:
        """Write canonical full-session ASR artifacts without rerunning ASR."""
        if not self.ready_for_session(device_id, session_id):
            return False
        manifest = self._manifest(device_id, session_id)
        recording_path, target_asr_dir = Path(recording_path), Path(target_asr_dir)
        recordings = discover_recordings(
            recording_path,
            timezone_name=timezone_name,
            user_start=datetime.fromisoformat(str(manifest["started_at"])),
        )
        if len(recordings) != 1:
            return False
        recording = recordings[0].to_dict()
        recording_id = str(recording["recording_id"])
        root = self._directory(device_id, session_id)
        transcripts = []
        for segment in sorted(manifest["segments"], key=lambda item: int(item["sequence"])):
            state = _read_json(self._segment_state_path(root, int(segment["sequence"])), {})
            if not isinstance(state, dict):
                return False
            for index, row in enumerate(state.get("rows", []), 1):
                value = dict(row)
                value["started_at_local"] = _local_iso(value.get("absolute_started_at"))
                value["clock_local"] = _local_clock(value.get("absolute_started_at"))
                value["recording_id"] = recording_id
                value["segment_id"] = f"{recording_id}_seg_{len(transcripts) + index:06d}"
                transcripts.append(value)
        transcripts.sort(key=lambda row: (float(row.get("start_offset_seconds") or 0), str(row.get("segment_id") or "")))
        for index, row in enumerate(transcripts, 1):
            row["segment_id"] = f"{recording_id}_seg_{index:06d}"
        target_asr_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(target_asr_dir / "transcript.json", transcripts)
        _atomic_json(target_asr_dir / "recordings.json", [recording])
        checkpoint = {"status": "completed", "transcripts": transcripts,
                      "completed_windows": len(manifest["segments"]), "total_windows": len(manifest["segments"]),
                      "progress_unit": "segments", "source": recording_path.name}
        _atomic_json(target_asr_dir / "checkpoint.json", checkpoint)
        # 【2026-10-14 修】原来写死 "Local-Whisper-Turbo-MLX" —— 而本方法是
        # 「复用已完成的实时切片转写」（不重跑 ASR），rows 的 source 可能是
        # moss-transcribe-diarize / doubao_asr / local_whisper_turbo 中任意一个。
        # 写死会与同 job 的 transcript.json（source 字段）自相矛盾 ——
        # 实测 job-20261014-031125 的 219 条 rows 全是 moss-transcribe-diarize，
        # 而这里的 run-summary 声明是 Whisper-Turbo。改为从 rows 推导真实来源。
        observed_sources = sorted(
            {
                str(row.get("source") or "").strip()
                for row in transcripts
                if str(row.get("source") or "").strip()
            }
        )
        _atomic_json(target_asr_dir / "run-summary.json", {
            "status": "completed", "source": recording_path.name,
            "model": observed_sources[0] if len(observed_sources) == 1
            else ("Local-Whisper-Turbo-MLX" if not observed_sources else "mixed:" + ",".join(observed_sources)),
            "declared_sources": observed_sources,
            "duration_s": recording.get("duration_seconds"), "transcript_count": len(transcripts),
            "total_windows": len(manifest["segments"]), "completed_windows": len(manifest["segments"]),
            "warnings": [],
        })
        return True
