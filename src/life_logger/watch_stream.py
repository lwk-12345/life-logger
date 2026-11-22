"""Durable, idempotent Apple Watch segment intake for Life Logger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping, Sequence

from life_logger.ambient_anchors import classify_ambient_location, extract_ambient_headers
from life_logger.timeutil import LOCAL_TZ, local_date, parse_ts

import logging

logger = logging.getLogger(__name__)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SHA256_HEADER = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_WATCH_SEGMENT_BYTES = 512 * 1024 * 1024
#: [V68] 手表上报的 steps_date 必须是 YYYY-MM-DD（手表本地时区的自然日）。
_STEPS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_WATCH_SEGMENTS = 1_000_000
WATCH_CAPTURE_SOURCE = "apple-watch"
# Watch 分片是分钟级的，而且手表达不到手机时会成批补传（实测同一会话可以拖 2.7 天），
# 所以"停滞"阈值必须比 AI Passport 的 30 秒宽得多。它只是屏幕提示；superseded 只说明
# 设备开始了新会话，不证明旧会话的分片已全部补传。自动收口须依赖 Watch 明确给出的
# expected_segments 和 ended_at。
WATCH_RECEIVING_STALE_SECONDS = 6 * 60 * 60

#: 设备状态快照多久算“过期”。与 `/api/watch-reception` 回给前端的
#: `device_status_freshness_seconds` 保持一致，避免两端各有一套新鲜度口径。
DEVICE_STATUS_FRESHNESS_SECONDS = 15 * 60
#: 设备「失联」告警阈值（Q773）。快照过期只说明“状态旧”，不等于设备不在线；
#: 这里按“多久没有上报”单独判一维，且只在白天评估 3h 档，避免正常夜间空档误报。
DEVICE_OFFLINE_WARN_SECONDS = 3 * 3600
DEVICE_OFFLINE_ALERT_SECONDS = 8 * 3600
DEVICE_OFFLINE_DAY_START, DEVICE_OFFLINE_DAY_END = 8, 23
#: 计算缺口时要遍历 0..expected-1，防止一个脏 manifest 把服务卡住。
DEVICE_STATUS_MAX_EXPECTED = 200_000
#: 设备状态快照保留条数（只保留最新一条时，停录原因会被下一次上报覆盖掉）
DEVICE_STATUS_HISTORY_MAX = 20

# 停录归因允许值（Q161）。设备上报的未知值**保留原文**并打 warning，不静默丢弃、也不冒充已知原因。
ENDED_BY_VALUES = ("user", "battery", "disconnect", "app_crash", "watch_client")
ENDED_BY_INVALID_WARNING = "invalid_ended_by_enum"


def normalize_ended_by(value: object) -> tuple[str, bool]:
    """返回 (去掉首尾空白的原值, 是否在允许枚举内)。空值视为"没上报"，不算非法。"""
    text = str(value).strip() if isinstance(value, str) else ""
    if not text:
        return "", True
    return text, text in ENDED_BY_VALUES
#: completion ack 台账文件名（append-only JSONL）
COMPLETION_ACKS_FILENAME = "completion-acks.jsonl"


class WatchSessionError(ValueError):
    """Base error for invalid Watch session operations."""

    status_code = HTTPStatus.BAD_REQUEST


class WatchSessionConflictError(WatchSessionError):
    """The request conflicts with already accepted session state."""

    status_code = HTTPStatus.CONFLICT


class WatchSessionChecksumMismatchError(WatchSessionError):
    """The client checksum doesn't match the received bytes."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY


class WatchSessionNotFoundError(WatchSessionError):
    status_code = HTTPStatus.NOT_FOUND


class WatchSessionProcessingError(WatchSessionError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE


class IncompleteWatchSessionError(WatchSessionError):
    def __init__(self, missing_sequences: list[int]) -> None:
        self.missing_sequences = missing_sequences
        super().__init__(f"session is missing segments: {missing_sequences}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256_header(body: bytes) -> str:
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _activity_age_seconds(value: Any, reference_now: datetime) -> float | None:
    """把 manifest 的 updated_at 换成"距现在多少秒"；解析不出来返回 None。"""
    if not value:
        return None
    try:
        activity = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if activity.tzinfo is None:
        activity = activity.replace(tzinfo=reference_now.tzinfo)
    return max(0.0, (reference_now - activity).total_seconds())


def _manifest_segment_facts(manifest: Mapping[str, Any]) -> dict[str, int]:
    """服务端用 manifest 实数出“缺多少、多收多少”。

    为什么必须从 manifest 算，而不是信设备自报：
    设备 POST 上来的 `pending_segment_count` 是它本地队列的长度，服务端只存不算，
    实测出现过“自报 20 段待传 / 服务端 142 个会话 0 缺段”的假积压。

    `extra_segments`（sequence ≥ expected）单独计数且 **不从 missing 里抵**：
    多收说明设备把上一场或下一场的段串进来了，是另一个问题，不能拿它填缺口。
    """
    segments = [item for item in (manifest.get("segments") or []) if isinstance(item, Mapping)]
    sequences: set[int] = set()
    for item in segments:
        raw = item.get("sequence")
        try:
            sequences.add(int(raw))
        except (TypeError, ValueError):
            continue
    try:
        expected = manifest.get("expected_segments")
        expected = int(expected) if expected is not None else None
    except (TypeError, ValueError):
        expected = None
    if expected is None or expected < 0:
        return {"expected": 0, "received": len(sequences), "missing": 0,
                "extra": 0, "has_expected": 0}
    expected = min(expected, DEVICE_STATUS_MAX_EXPECTED)
    missing = sum(1 for seq in range(expected) if seq not in sequences)
    extra = sum(1 for seq in sequences if seq >= expected)
    return {"expected": expected, "received": len(sequences), "missing": missing,
            "extra": extra, "has_expected": 1}


class WatchSessionStore:
    """Accept Watch M4A segments and ACK only after the data and manifest are synced."""

    def __init__(self, root: Path, *, process_safe: bool = False, command_dispatcher=None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.device_status_root = self.root / "device-status"
        self.device_status_root.mkdir(parents=True, exist_ok=True)
        self.completion_acks_path = self.root / COMPLETION_ACKS_FILENAME
        self._process_safe = process_safe
        self.command_dispatcher = command_dispatcher
        if process_safe:
            from life_logger.runtime_queue import ProcessRLock
            self._lock = ProcessRLock(self.root / ".state.lock")
        else:
            self._lock = threading.RLock()
        self._pending_haptics: dict[str, str] = {}

    def enqueue_haptic(self, device_id: str, haptic_type: str = "success") -> None:
        """Enqueue a physical Taptic feedback signal to be returned on next Watch ACK."""
        with self._lock:
            if self._process_safe:
                if not SAFE_ID.fullmatch(str(device_id)):
                    raise WatchSessionError("invalid device id")
                _write_atomic(self.root / "haptics" / f"{device_id}.json", _json_bytes({"type": haptic_type}))
            else:
                self._pending_haptics[device_id] = haptic_type

    def pop_haptic(self, device_id: str) -> str | None:
        """Retrieve feedback once, including feedback produced by another process."""
        with self._lock:
            if not self._process_safe:
                return self._pending_haptics.pop(device_id, None)
            if not SAFE_ID.fullmatch(str(device_id)):
                raise WatchSessionError("invalid device id")
            path = self.root / "haptics" / f"{device_id}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            path.unlink()
            return str(payload["type"])

    def save_device_status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id") or "")
        if not SAFE_ID.fullmatch(device_id):
            raise WatchSessionError("invalid device id")
        reported_at = str(payload.get("reported_at") or "")
        try:
            reported = datetime.fromisoformat(reported_at)
        except ValueError as error:
            raise WatchSessionError("reported_at must be ISO-8601") from error
        if reported.tzinfo is None or reported.utcoffset() is None:
            raise WatchSessionError("reported_at must include a timezone")
        now = datetime.now(LOCAL_TZ)
        if reported > now + timedelta(minutes=5):
            raise WatchSessionError("reported_at is too far in the future")
        counts: dict[str, int] = {}
        for key in ("pending_segment_count", "pending_segment_bytes", "open_session_count", "pending_completion_count"):
            value = payload.get(key)
            if type(value) is not int or value < 0:
                raise WatchSessionError(f"{key} must be a nonnegative integer")
            counts[key] = value
        sync_state = str(payload.get("sync_state") or "")
        if sync_state not in {"syncing", "idle"}:
            raise WatchSessionError("sync_state must be syncing or idle")
        with self._lock:
            path = self.device_status_root / f"{device_id}.json"
            current: Any = None
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    current_reported_at = datetime.fromisoformat(
                        str(current.get("reported_at") or "")
                    )
                    if (
                        current_reported_at.tzinfo is not None
                        and reported < current_reported_at
                    ):
                        return {**self._verify_device_status(current), "accepted": False}
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            status = {
                "device_id": device_id,
                "reported_at": reported.isoformat(),
                **counts,
                "sync_state": sync_state,
                "received_at": now.isoformat(),
            }
            # 历史：旧文件只有一条快照（没有 history/first_seen），这里向后兼容地补上。
            previous = current if isinstance(current, dict) else {}
            history = [item for item in (previous.get("history") or []) if isinstance(item, dict)]
            status["first_seen"] = str(
                previous.get("first_seen") or previous.get("received_at") or status["received_at"]
            )
            status["last_seen"] = status["received_at"]
            status["history"] = (history + [{
                "reported_at": status["reported_at"],
                "received_at": status["received_at"],
                "sync_state": sync_state,
                "open_session_count": counts["open_session_count"],
                "pending_segment_count": counts["pending_segment_count"],
                "pending_completion_count": counts["pending_completion_count"],
            }])[-DEVICE_STATUS_HISTORY_MAX:]
            status["history_truncated"] = len(history) + 1 > DEVICE_STATUS_HISTORY_MAX
            _write_atomic(path, _json_bytes(status))
            return {**self._verify_device_status(status), "accepted": True}

    def list_device_statuses(self) -> list[dict[str, Any]]:
        """返回设备状态快照，并用服务端 manifest 实数叠加“真实性”字段。

        返回的每个条目都包含设备原始自报值（保持向后兼容）+ 以下校验字段：
          verified_at / reported_age_seconds / stale / missing_segments / extra_segments /
          reported_only_segments / pending_is_real / reported_vs_manifest{...}
        文件本身 **不改写**：快照是设备说的，校验是服务端算的，两者都要留痕。
        """
        statuses: list[dict[str, Any]] = []
        # [2026-11-08] 纯读不拿锁：设备状态文件由 _write_atomic 原子替换，读到的要么旧要么新。
        # split 模式下 self._lock 是与后台进程共用的跨进程文件锁，读也拿锁会让
        # /api/health/summary、/api/watch-coverage 在后台写手表数据时排队 30 秒（线上实测）。
        if True:
            for path in self.device_status_root.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        statuses.append(value)
                except (OSError, json.JSONDecodeError):
                    continue
            return sorted(
                (self._verify_device_status(item) for item in statuses),
                key=lambda item: str(item.get("device_id") or ""),
            )

    def _device_manifest_facts(self, device_id: str) -> dict[str, Any]:
        """统计某设备全部会话的缺口/多收/未完成情况（只读）。"""
        device = str(device_id or "")
        if not SAFE_ID.fullmatch(device):
            return {"sessions": 0, "has_expected": 0, "expected": 0, "received": 0,
                    "missing": 0, "extra": 0, "incomplete_sessions": 0,
                    "completion_pending_sessions": 0, "last_receipt_at": None}
        sessions = 0
        has_expected = 0
        expected_total = 0
        received_total = 0
        missing_total = 0
        extra_total = 0
        incomplete = 0
        completion_pending = 0
        last_receipt = None          # 服务端最后一次**收到该设备数据**的时间（manifest 落盘即代表收到分段）
        for path in self.root.glob(f"{device}/*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            # [2026-11-03 恢复路径] 分段被接受时 manifest 会被重写（record_segment → _write_manifest），
            # 所以 manifest 的 mtime 是**服务端视角**的"设备还活着"证据，不依赖设备是否再发状态。
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=LOCAL_TZ)
                if last_receipt is None or mtime > last_receipt:
                    last_receipt = mtime
            except OSError:
                pass
            facts = _manifest_segment_facts(manifest)
            sessions += 1
            has_expected += facts["has_expected"]
            expected_total += facts["expected"]
            received_total += facts["received"]
            missing_total += facts["missing"]
            extra_total += facts["extra"]
            if str(manifest.get("state") or "") != "complete":
                incomplete += 1
            if isinstance(manifest.get("completion_intent"), dict):
                completion_pending += 1
        return {
            "sessions": sessions,
            "has_expected": has_expected,
            "expected": expected_total,
            "received": received_total,
            "missing": missing_total,
            "extra": extra_total,
            "incomplete_sessions": incomplete,
            "completion_pending_sessions": completion_pending,
            "last_receipt_at": last_receipt,
        }

    def _verify_device_status(self, status: Mapping[str, Any]) -> dict[str, Any]:
        """给一条设备自报状态叠加服务端校验结果（不写盘）。"""
        now = datetime.now(LOCAL_TZ)
        reported_age = _activity_age_seconds(status.get("reported_at"), now)
        stale = reported_age is None or reported_age > DEVICE_STATUS_FRESHNESS_SECONDS
        facts = self._device_manifest_facts(str(status.get("device_id") or ""))
        try:
            reported_pending = int(status.get("pending_segment_count") or 0)
        except (TypeError, ValueError):
            reported_pending = 0
        try:
            reported_completions = int(status.get("pending_completion_count") or 0)
        except (TypeError, ValueError):
            reported_completions = 0
        missing = int(facts["missing"])
        extra = int(facts["extra"])
        # 只有服务端算出来的缺口才算“真实积压”；自报数里超出的部分明确标成“未证实”。
        reported_only = max(0, reported_pending - missing)
        # ---- 设备失联（与积压正交的一维，Q773）--------------------------------
        # 旧判据先看 missing：missing=0 且 pending=0 就直接 clean，于是“8 小时没上报”
        # 被显示成“已同步”。这里单独算 silence，并在 verdict 里前置一档。
        # [2026-11-03 恢复路径] 失联判定不能只看设备自报的 reported_at。
        # 线上真实场景：手表重新连上后**只补传分段、不再发状态快照**，此时
        # reported_at 还是 17 小时前，silence 会继续增长 → alert 永久不消。
        # 恢复证据取三者中最新的一条（都是服务端可见的事实，不需要设备配合）：
        #   1) 设备自报 reported_at
        #   2) 状态文件落盘时间 received_at（服务端时钟）
        #   3) 该设备任一会话 manifest 的 mtime（分段被接受 → 服务端确实收到了数据）
        evidence: list[tuple[datetime, str]] = []
        reported_dt = (
            now - timedelta(seconds=reported_age) if reported_age is not None else None
        )
        if reported_dt is not None:
            evidence.append((reported_dt, "device_reported_at"))
        try:
            received_at = datetime.fromisoformat(str(status.get("received_at") or ""))
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=LOCAL_TZ)
            evidence.append((received_at, "status_received_at"))
        except (TypeError, ValueError):
            pass
        last_receipt = facts.get("last_receipt_at")
        if isinstance(last_receipt, datetime):
            evidence.append((last_receipt, "server_segment_receipt"))
        last_evidence, last_evidence_kind = (max(evidence) if evidence else (None, None))
        silence = (now - last_evidence).total_seconds() if last_evidence is not None else float("inf")
        offline_since = last_evidence
        daytime = (
            DEVICE_OFFLINE_DAY_START <= now.hour < DEVICE_OFFLINE_DAY_END
            or (
                offline_since is not None
                and DEVICE_OFFLINE_DAY_START <= offline_since.hour < DEVICE_OFFLINE_DAY_END
            )
        )
        if silence >= DEVICE_OFFLINE_ALERT_SECONDS:
            offline, offline_reason = "alert", "silent_over_8h"
        elif silence >= DEVICE_OFFLINE_WARN_SECONDS and daytime:
            offline, offline_reason = "warn", "silent_over_3h_daytime"
        else:
            offline, offline_reason = None, None
        if offline:
            verdict = "offline"
            if offline_since is None:
                note = "设备从未上报状态"
            else:
                note = (
                    f"设备已 {silence / 3600.0:.1f} 小时未上报"
                    f"（上次 {offline_since:%H:%M}）"
                )
            if reported_pending or reported_completions:
                note += (
                    f"；离开时自报 {reported_pending} 段待传、"
                    f"{reported_completions} 段未确认完成。"
                )
            else:
                note += "；服务端无待传缺口。"
        elif missing > 0:
            verdict = "server_confirms_missing"
            note = f"服务端确认缺 {missing} 段，需要补传。"
        elif stale and reported_pending > 0:
            verdict = "reported_only_stale"
            note = (
                f"设备自报 {reported_pending} 段待传，但服务端 {facts['sessions']} 个会话缺口为 0；"
                "快照已过期，不能当真实积压。"
            )
        elif reported_pending > 0:
            verdict = "reported_only"
            note = (
                f"设备自报 {reported_pending} 段待传，服务端目前缺口为 0；"
                "可能是设备本地队列尚未上传，等待下次上报。"
            )
        else:
            verdict = "clean"
            note = "设备自报与服务端 manifest 一致，无待传。"
        return {
            **dict(status),
            "verified_at": now.isoformat(),
            "reported_age_seconds": round(reported_age, 1) if reported_age is not None else None,
            "stale": bool(stale),
            "silent_seconds": round(silence, 1) if silence != float("inf") else None,
            "last_evidence_at": last_evidence.isoformat() if last_evidence is not None else None,
            "last_evidence_kind": last_evidence_kind,
            "last_server_receipt_at": (
                last_receipt.isoformat() if isinstance(last_receipt, datetime) else None
            ),
            # 恢复时刻：没有失联、且有证据时给出"最后一次有效上报"的时间。
            # 没有历史状态可比时它就是 last_evidence，语义是「什么时候恢复的 / 最近一次活着」。
            "recovered_at": (
                last_evidence.isoformat()
                if offline is None and last_evidence is not None else None
            ),
            "offline": offline,
            "offline_reason": offline_reason,
            "offline_since": offline_since.isoformat() if offline_since is not None else None,
            "offline_since_clock": offline_since.strftime("%H:%M") if offline_since is not None else None,
            "freshness_seconds": float(DEVICE_STATUS_FRESHNESS_SECONDS),
            "missing_segments": missing,
            "extra_segments": extra,
            "server_expected_segments": int(facts["expected"]),
            "server_received_segments": int(facts["received"]),
            "server_sessions": int(facts["sessions"]),
            "server_incomplete_sessions": int(facts["incomplete_sessions"]),
            "server_completion_pending_sessions": int(facts["completion_pending_sessions"]),
            "reported_only_segments": reported_only,
            "pending_is_real": missing > 0,
            "reported_vs_manifest": {
                "verdict": verdict,
                "note": note,
                "reported_pending_segment_count": reported_pending,
                "reported_pending_completion_count": reported_completions,
                "server_missing_segments": missing,
                "server_extra_segments": extra,
                "server_incomplete_sessions": int(facts["incomplete_sessions"]),
                "reported_only_segments": reported_only,
            },
        }

    # ------------------------------------------------------------------
    # completion ack 台账：解释「设备自报 pending_completion_count」到底卡在哪
    # ------------------------------------------------------------------
    def record_completion_ack(
        self,
        *,
        device_id: str,
        session_id: str,
        job_id: str | None,
        result: str,
        ended_by: str | None = None,
        end_reason: str | None = None,
        received_segments: int | None = None,
    ) -> dict[str, Any]:
        """记一条 completion ack（服务端回给设备的确认）。append-only JSONL。"""
        device, session = self._validate_ids(device_id, session_id)
        record = {
            "acked_at": datetime.now(LOCAL_TZ).isoformat(),
            "device_id": device,
            "session_id": session,
            "job_id": job_id or None,
            "result": str(result or "unknown"),
            "ended_by": ended_by,
            "end_reason": end_reason,
            "received_segments": received_segments,
        }
        # [2026-10-29 batch3] 跨进程安全追加：flock 互斥 + 单次 os.write 整行 + fsync。
        # 原实现用进程内 self._lock + open("a")：两个 worker 同时写时可能交错成半行，
        # 读侧只能跳过 → 静默丢 ack。
        from life_logger.atomic import append_json_line

        try:
            result = append_json_line(
                self.completion_acks_path, record,
                on_durability_error="report",
            )
            if result.durability == "unknown":
                logger.warning("completion ack 已追加但持久性未知: %s", self.completion_acks_path)
        except OSError as exc:
            # 追加失败不阻断录音流程（原实现也是 best-effort），但必须可见
            logger.warning("completion ack 追加失败 (%s): %s", type(exc).__name__, exc)
        return record

    def list_completion_acks(self, limit: int = 100) -> list[dict[str, Any]]:
        """读最近 N 条 ack（新→旧）。文件不存在或坏行都跳过，不影响读路径。"""
        path = self.completion_acks_path
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                out.append(payload)
            if len(out) >= max(1, int(limit)):
                break
        return out

    def stop_diagnostics(self, *, recent: int = 20, limit_acks: int = 50) -> dict[str, Any]:
        """停录原因 + 未确认 completion 的只读诊断（不改任何文件）。"""
        acks = self.list_completion_acks(limit_acks)
        acked_by_session: dict[str, dict] = {}
        for record in acks:
            key = str(record.get("session_id") or "")
            if key and key not in acked_by_session:
                acked_by_session[key] = record
        recent_stops: list[dict[str, Any]] = []
        pending_completion: list[dict[str, Any]] = []
        unacked: list[dict[str, Any]] = []
        with self._lock:
            for path in sorted(self.root.glob("*/*/manifest.json")):
                try:
                    device, session = self._validate_ids(
                        path.parent.parent.name, path.parent.name
                    )
                    manifest = self._read_manifest(device, session)
                except (OSError, ValueError, TypeError, WatchSessionError):
                    continue
                state = str(manifest.get("state") or "")
                entry = {
                    "device_id": device,
                    "session_id": session,
                    "state": state,
                    "job_id": manifest.get("job_id") or manifest.get("completion_job_id"),
                    "ended_at": manifest.get("ended_at"),
                    "ended_by": manifest.get("ended_by"),
                    "ended_by_source": manifest.get("ended_by_source") or "missing",
                    "ended_by_warning": manifest.get("ended_by_warning"),
                    "end_reason": manifest.get("end_reason") or "missing",
                    "received_segments": len(manifest.get("segments") or []),
                    "expected_segments": manifest.get("expected_segments"),
                }
                ack = acked_by_session.get(session)
                entry["ack"] = (
                    {"acked_at": ack.get("acked_at"), "result": ack.get("result")}
                    if ack else None
                )
                if isinstance(manifest.get("completion_intent"), dict):
                    entry["completion_intent"] = manifest["completion_intent"]
                    pending_completion.append(entry)
                elif state == "complete":
                    recent_stops.append(entry)
                    if ack is None or str(ack.get("result")) not in {"complete", "duplicate"}:
                        unacked.append(entry)
        recent_stops.sort(key=lambda item: str(item.get("ended_at") or ""), reverse=True)
        unacked.sort(key=lambda item: str(item.get("ended_at") or ""), reverse=True)
        by_reason: dict[str, int] = {}
        for item in recent_stops:
            by_reason[str(item.get("end_reason"))] = by_reason.get(str(item.get("end_reason")), 0) + 1
        return {
            "recent_stops": recent_stops[: max(1, int(recent))],
            "pending_completion_sessions": pending_completion[: max(1, int(recent))],
            "unacked_completions": unacked[: max(1, int(recent))],
            "counts": {
                "complete_sessions": len(recent_stops),
                "pending_completion_sessions": len(pending_completion),
                "unacked_completions": len(unacked),
                "acks_recorded": len(acks),
            },
            "end_reason_counts": dict(sorted(by_reason.items())),
            "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        }

    def _validate_ids(self, device_id: str, session_id: str) -> tuple[str, str]:
        device = str(device_id or "")
        session = str(session_id or "")
        if not SAFE_ID.fullmatch(device) or not SAFE_ID.fullmatch(session):
            raise WatchSessionError("invalid device or session id")
        return device, session

    def list_session_summaries(self, limit: int | None = 50) -> list[dict[str, Any]]:
        summaries = []
        now = datetime.now(LOCAL_TZ)
        with self._lock:
            for path in self.root.glob("*/*/manifest.json"):
                try:
                    device, session = self._validate_ids(
                        path.parent.parent.name, path.parent.name
                    )
                    manifest = self._read_manifest(device, session)
                    segments = manifest.get("segments", [])
                    age = _activity_age_seconds(manifest.get("updated_at"), now)
                    state = manifest.get("state")
                    stalled = bool(
                        state == "receiving"
                        and age is not None
                        and age >= WATCH_RECEIVING_STALE_SECONDS
                    )
                    summaries.append(
                        {
                            key: manifest.get(key)
                            for key in (
                                "device_id",
                                "session_id",
                                "started_at",
                                "updated_at",
                                "state",
                                "job_id",
                                "completion_job_id",
                            )
                        }
                        | {
                            "completion_pending": isinstance(
                                manifest.get("completion_intent"), dict
                            ),
                            "received_segments": len(segments),
                            "received_duration_ms": sum(
                                int(item["duration_ms"]) for item in segments
                            ),
                            "activity_age_seconds": (
                                round(age, 1) if age is not None else None
                            ),
                            "stale_after_seconds": float(
                                WATCH_RECEIVING_STALE_SECONDS
                            ),
                            "stalled": stalled,
                            # 会话是否已被同一设备更晚的会话取代。前端可以直接
                            # 用它把"还在录音中"改成"已被新会话取代"，不需要自己
                            # 比较 started_at。
                            "superseded": False,
                            "status": "stalled" if stalled else state,
                        }
                    )
                except (OSError, ValueError, TypeError, KeyError):
                    continue
        latest: dict[str, str] = {}
        for item in summaries:
            device = str(item.get("device_id") or "")
            started = str(item.get("started_at") or "")
            if started > latest.get(device, ""):
                latest[device] = started
        for item in summaries:
            device = str(item.get("device_id") or "")
            started = str(item.get("started_at") or "")
            if (
                item.get("state") == "receiving"
                and started
                and latest.get(device, "") > started
            ):
                item["superseded"] = True
                item["status"] = "superseded"
        ordered = sorted(
            summaries, key=lambda item: str(item.get("started_at") or ""), reverse=True
        )
        return ordered if limit is None else ordered[:max(0, limit)]

    def list_coverage_intervals(self) -> list[dict[str, Any]]:
        """Return audio segment time ranges using manifest metadata only."""
        intervals: list[dict[str, Any]] = []
        # [2026-11-08] 纯读不拿跨进程锁（manifest 由 _write_atomic 原子替换），理由同 list_device_statuses。
        if True:
            for path in self.root.glob("*/*/manifest.json"):
                try:
                    device, session = self._validate_ids(
                        path.parent.parent.name, path.parent.name
                    )
                    manifest = self._read_manifest(device, session)
                    started_at = datetime.fromisoformat(str(manifest["started_at"]))
                    if started_at.tzinfo is None or started_at.utcoffset() is None:
                        continue
                    segments = manifest.get("segments")
                    if not isinstance(segments, list):
                        continue
                    for segment in segments:
                        try:
                            if not isinstance(segment, dict):
                                continue
                            sequence = segment["sequence"]
                            offset = segment["start_offset_ms"]
                            duration = segment["duration_ms"]
                            if any(
                                isinstance(value, bool) or not isinstance(value, int)
                                for value in (sequence, offset, duration)
                            ):
                                continue
                            if sequence < 0 or offset < 0 or duration <= 0:
                                continue
                            start = started_at + timedelta(milliseconds=offset)
                            end = start + timedelta(milliseconds=duration)
                        except (KeyError, TypeError, ValueError, OverflowError):
                            continue
                        interval_record: dict[str, Any] = {
                            "device_id": device,
                            "session_id": session,
                            "sequence": sequence,
                            "start_at": start.isoformat(),
                            "end_at": end.isoformat(),
                        }
                        battery_snapshot = segment.get("battery_snapshot")
                        if isinstance(battery_snapshot, dict) and battery_snapshot:
                            interval_record["battery_snapshot"] = battery_snapshot
                        network_snapshot = segment.get("network_snapshot")
                        if isinstance(network_snapshot, dict) and network_snapshot:
                            interval_record["network_snapshot"] = network_snapshot
                            if "ambient_location" in network_snapshot:
                                interval_record["ambient_location"] = network_snapshot["ambient_location"]
                        health_snapshot = segment.get("health_snapshot")
                        if isinstance(health_snapshot, dict) and health_snapshot:
                            interval_record["health_snapshot"] = health_snapshot
                        intervals.append(interval_record)
                except (OSError, ValueError, TypeError, KeyError, OverflowError):
                    continue
        return sorted(
            intervals,
            key=lambda item: (
                datetime.fromisoformat(item["start_at"]),
                item["device_id"],
                item["session_id"],
                item["sequence"],
            ),
        )

    def _session_dir(self, device_id: str, session_id: str) -> Path:
        return self.root / device_id / session_id

    def _manifest_path(self, device_id: str, session_id: str) -> Path:
        return self._session_dir(device_id, session_id) / "manifest.json"

    def _read_manifest(self, device_id: str, session_id: str) -> dict[str, Any]:
        path = self._manifest_path(device_id, session_id)
        if not path.is_file():
            raise WatchSessionNotFoundError("Watch session not found")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WatchSessionError("Watch session manifest is unreadable") from error
        if not isinstance(value, dict):
            raise WatchSessionError("Watch session manifest is invalid")
        return value

    def _write_manifest(
        self, device_id: str, session_id: str, manifest: dict[str, Any]
    ) -> None:
        # 【2026-10-17】manifest 自描述化：手表只上传 started_at（UTC）+ 每段
        # start_offset_ms，任何人要看“这一段到底几点”都得自己算一次时区+偏移。
        # 直接写入 started_at_local 与每段 started_at_local，读取方不用再换算。
        try:
            from life_logger.timeutil import normalize_manifest

            manifest = normalize_manifest(manifest)
        except Exception:
            pass
        _write_atomic(
            self._manifest_path(device_id, session_id),
            _json_bytes(manifest),
        )

    def start_session(
        self,
        *,
        device_id: str,
        session_id: str,
        started_at: str,
        sample_rate: int = 16_000,
        channels: int = 1,
        codec: str = "m4a-aac",
        ssid: str | None = None,
        bssid: str | None = None,
        network_snapshot: dict[str, Any] | None = None,
        headers: Any = None,
        client_ip: str | None = None,
    ) -> dict[str, Any]:
        device, session = self._validate_ids(device_id, session_id)
        try:
            started = datetime.fromisoformat(str(started_at))
        except ValueError as error:
            raise WatchSessionError("started_at must be ISO-8601") from error
        if started.tzinfo is None:
            raise WatchSessionError("started_at must include a timezone")
        if sample_rate not in {16_000, 24_000, 32_000, 44_100, 48_000}:
            raise WatchSessionError("unsupported sample rate")
        if channels not in {1, 2}:
            raise WatchSessionError("unsupported channel count")
        if codec != "m4a-aac":
            raise WatchSessionError("unsupported Watch audio codec")

        manifest_path = self._manifest_path(device, session)
        with self._lock:
            if manifest_path.is_file():
                manifest = self._read_manifest(device, session)
                return {"duplicate": True, "session": manifest}

            incoming_net: dict[str, Any] = {}
            if isinstance(network_snapshot, dict) and network_snapshot:
                incoming_net.update(network_snapshot)
            h_ssid, h_bssid = extract_ambient_headers(headers)
            eff_ssid = ssid if ssid is not None else h_ssid
            eff_bssid = bssid if bssid is not None else h_bssid
            if eff_ssid is not None:
                incoming_net.setdefault("ssid", eff_ssid)
            if eff_bssid is not None:
                incoming_net.setdefault("bssid", eff_bssid)
            # 【2026-10-15 修】原来只在有 SSID/BSSID 时才算 ambient_location，
            # 而这条链路上 SSID 永远拿不到（手表需 Apple 审核的 entitlement、
            # Mac 被 macOS 14+ 隐私保护 redact），所以场景判断恒为 Outside。
            # 现在补两个已经拿得到的信号：手表坐标（geo）与客户端 IP 网段。
            _geo = incoming_net.get("location")
            if not isinstance(_geo, dict):
                _geo = None
            if "ambient_location" not in incoming_net and (
                eff_ssid is not None or eff_bssid is not None or _geo or client_ip
            ):
                incoming_net["ambient_location"] = classify_ambient_location(
                    eff_ssid, eff_bssid, geo=_geo, client_ip=client_ip
                )

            manifest = {
                "schema_version": "watch-session-v1",
                "device_id": device,
                "session_id": session,
                "started_at": started.isoformat(),
                "ended_at": None,
                "state": "receiving",
                "sample_rate": sample_rate,
                "channels": channels,
                "codec": codec,
                "expected_segments": None,
                "segments": [],
                "updated_at": datetime.now(LOCAL_TZ).isoformat(),
            }
            if incoming_net:
                manifest["network_snapshot"] = incoming_net
            segment_dir = self._session_dir(device, session) / "segments"
            segment_dir.mkdir(parents=True, exist_ok=True)
            _fsync_directory(segment_dir.parent)
            self._write_manifest(device, session, manifest)
            return {"duplicate": False, "session": manifest}

    def segment_gap_report(self, device_id: str, session_id: str) -> dict[str, Any]:
        """上报「已收到哪些分段、缺哪些」——断点续传的服务端查询口。

        【2026-10-22 P-32 / 4号设计】只读，不写 manifest、不碰音频。
        为什么需要：一次 413 段的会话实测要跨两天、约 30 小时才慢慢补传完
        （`9443BA19`：0..412 全齐但耗时很久），中间反复全量重传。
        有这份缺口清单后，客户端只需补 missing，再从未收到的下一个继续。

        安全边界：只返回序号与计数，不返回文件名/音频/转写/位置。
        """
        device, session = self._validate_ids(device_id, session_id)
        with self._lock:
            manifest = self._read_manifest(device, session)

        segments = [item for item in (manifest.get("segments") or []) if isinstance(item, dict)]
        received = sorted(int(item.get("sequence", 0)) for item in segments)
        highest = received[-1] if received else -1
        try:
            expected = manifest.get("expected_segments")
            expected = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected = None

        # 缺口上限：优先用手表声明的总数；没声明就只看到"已收到的最高段"为止
        # （尾部缺口看不见，这是设计里明示的已知限制）。
        upper = (expected - 1) if expected is not None else highest
        received_set = set(received)
        missing = [seq for seq in range(0, upper + 1) if seq not in received_set]

        if not received:
            action = "start_from_zero"
            next_expected = 0
        elif not missing:
            action = "complete"
            next_expected = highest + 1
        else:
            action = "resume_from_gap"
            next_expected = missing[0]

        note = (
            "从最小缺口 %d 继续；已收到的分段不需要重传，重传也不会重复写盘" % next_expected
            if action == "resume_from_gap"
            else "全部已收到，可继续录制新分段"
            if action == "complete"
            else "尚未收到任何分段，从 0 开始"
        )
        return {
            "ok": True,
            "device_id": device,
            "session_id": session,
            "state": str(manifest.get("state") or "unknown"),
            "received_sequences": received,
            "missing_sequences": missing,
            "gap_count": len(missing),
            "received_count": len(received),
            "highest_sequence": highest,
            "next_expected_sequence": next_expected,
            "expected_segments": expected,
            "resume_hint": {
                "action": action,
                "from_sequence": next_expected,
                "note": note,
            },
            "updated_at": manifest.get("updated_at"),
        }

    def receive_segment(
        self,
        *,
        device_id: str,
        session_id: str,
        sequence: int,
        start_offset_ms: int,
        duration_ms: int,
        checksum: str,
        body: bytes,
        battery_level_percent: int | None = None,
        battery_state: str | None = None,
        ssid: str | None = None,
        bssid: str | None = None,
        network_snapshot: dict[str, Any] | None = None,
        heart_rate_bpm: float | None = None,
        step_count: int | None = None,
        # [2026-11-07 V68] 手表改成上传"当天 0 点起的累计步数"并带上它所属的自然日。
        steps_today: int | None = None,
        steps_date: str | None = None,
        steps_sampled_at: str | None = None,
        motion_state: str | None = None,
        location: dict[str, Any] | None = None,
        sleep: dict[str, Any] | None = None,
        headers: Any = None,
    ) -> dict[str, Any]:
        device, session = self._validate_ids(device_id, session_id)
        if sequence < 0 or sequence >= MAX_WATCH_SEGMENTS:
            raise WatchSessionError("segment sequence out of range")
        # File-derived AAC durations can include encoder frame padding.
        # Keep a bounded allowance rather than rejecting a normal minute chunk.
        if start_offset_ms < 0 or not 0 < duration_ms <= 61_000:
            raise WatchSessionError("segment timing out of range")
        if not body or len(body) > MAX_WATCH_SEGMENT_BYTES:
            raise WatchSessionError("segment size out of range")
        if battery_level_percent is not None and (
            type(battery_level_percent) is not int
            or not 0 <= battery_level_percent <= 100
        ):
            raise WatchSessionError("battery level out of range")
        if battery_state is not None and battery_state not in {
            "unplugged", "charging", "full"
        }:
            raise WatchSessionError("battery state is invalid")
        expected_checksum = _sha256_header(body)
        if (
            not SHA256_HEADER.fullmatch(str(checksum or ""))
            or checksum != expected_checksum
        ):
            raise WatchSessionChecksumMismatchError("segment checksum mismatch")

        with self._lock:
            manifest = self._read_manifest(device, session)
            # An abandoned session is one the Mac had already given up on (see
            # `abandon_empty_sessions`): hours with no audio, or the device had opened a
            # later session. It stays writable on purpose — a Watch that was offline for a
            # day must never be told "not accepting new segments" and have its audio thrown
            # away. Real audio revives the session and it flows through the normal path.
            was_abandoned = manifest.get("state") == "abandoned"
            if was_abandoned:
                manifest["state"] = "receiving"
                manifest.pop("abandoned_at", None)
                manifest.pop("abandon_reason", None)
            segments = list(manifest.get("segments") or [])
            if manifest.get("state") == "complete":
                existing = next(
                    (item for item in segments if int(item.get("sequence", -1)) == sequence),
                    None,
                )
                if existing and existing.get("sha256") == expected_checksum:
                    return {
                        "ok": True,
                        "duplicate": True,
                        "session_id": session,
                        "sequence": sequence,
                        "sha256": expected_checksum,
                        "stored_bytes": len(body),
                        "acknowledged_at": manifest.get("updated_at")
                        or datetime.now(LOCAL_TZ).isoformat(),
                    }
                # A new or updated segment arrived for a session that was previously closed.
                # Reopen to receiving so all remaining segments are accepted and assembled.
                manifest["state"] = "receiving"
                manifest.pop("expected_segments", None)
                manifest.pop("ended_at", None)
                manifest.pop("job_id", None)
                manifest.pop("completion_job_id", None)
                manifest.pop("recording_file", None)
                manifest.pop("recording_sha256", None)
                manifest.pop("recording_bytes", None)
            if manifest.get("state") != "receiving":
                raise WatchSessionConflictError(
                    "Watch session is not accepting new segments"
                )
            if sequence == 0 and start_offset_ms != 0:
                raise WatchSessionError("first segment must start at zero")
            previous_sequence = next(
                (item for item in segments if int(item.get("sequence", -1)) == sequence - 1),
                None,
            )
            next_sequence = next(
                (item for item in segments if int(item.get("sequence", -1)) == sequence + 1),
                None,
            )
            if previous_sequence is not None and start_offset_ms <= int(
                previous_sequence.get("start_offset_ms", -1)
            ):
                raise WatchSessionError("segment start offset must increase with sequence")
            if previous_sequence is not None:
                previous_end_ms = int(previous_sequence.get("start_offset_ms", 0)) + int(
                    previous_sequence.get("duration_ms", 0)
                )
                if start_offset_ms + 250 < previous_end_ms:
                    raise WatchSessionError("segment timing overlaps previous segment")
            if next_sequence is not None and start_offset_ms >= int(
                next_sequence.get("start_offset_ms", start_offset_ms)
            ):
                raise WatchSessionError("segment start offset must increase with sequence")
            if next_sequence is not None:
                next_start_ms = int(next_sequence.get("start_offset_ms", 0))
                if start_offset_ms + duration_ms > next_start_ms + 250:
                    raise WatchSessionError("segment timing overlaps next segment")
            previous = next(
                (item for item in segments if item.get("sequence") == sequence), None
            )
            segment_name = f"segment-{sequence:06d}.m4a"
            target = self._session_dir(device, session) / "segments" / segment_name

            if previous is not None and previous.get("sha256") != expected_checksum:
                raise WatchSessionConflictError(
                    "segment sequence already has different content"
                )
            if previous is not None and (
                int(previous.get("start_offset_ms", -1)) != start_offset_ms
                or int(previous.get("duration_ms", -1)) != duration_ms
            ):
                raise WatchSessionConflictError(
                    "segment retry timing does not match stored metadata"
                )

            duplicate = False
            if target.is_file():
                stored_digest = _sha256_header(target.read_bytes())
                if stored_digest != expected_checksum:
                    _write_atomic(target, body)
                else:
                    duplicate = previous is not None
            else:
                _write_atomic(target, body)

            segment = {
                "sequence": sequence,
                "file": segment_name,
                "start_offset_ms": start_offset_ms,
                "duration_ms": duration_ms,
                "size_bytes": len(body),
                "sha256": expected_checksum,
                "received_at": datetime.now(LOCAL_TZ).isoformat(),
            }
            # Optional V68 telemetry, measured by the Watch from the sealed audio file.
            # Malformed/missing telemetry never rejects an otherwise valid audio upload.
            # Preserve the first measurement on retries, as with capture-time snapshots.
            audio_bitrate = previous.get("audio_bitrate") if previous is not None else None
            if audio_bitrate is None:
                raw_bitrate = headers.get("X-LifeLogger-Audio-Bitrate") if headers is not None else None
                try:
                    parsed_bitrate = int(raw_bitrate)
                except (TypeError, ValueError, OverflowError):
                    parsed_bitrate = 0
                if not isinstance(raw_bitrate, bool) and 0 < parsed_bitrate <= 1_000_000:
                    audio_bitrate = parsed_bitrate
            if audio_bitrate is not None:
                segment["audio_bitrate"] = audio_bitrate
            incoming_battery_snapshot = {}
            if battery_level_percent is not None:
                incoming_battery_snapshot["level_percent"] = battery_level_percent
            if battery_state is not None:
                incoming_battery_snapshot["state"] = battery_state
            battery_snapshot = {}
            if previous is not None and isinstance(
                previous.get("battery_snapshot"), dict
            ):
                battery_snapshot.update(previous["battery_snapshot"])
            # A duplicate upload must not rewrite the capture-time snapshot.
            # Fill only fields that were absent in the first persisted attempt.
            for key, value in incoming_battery_snapshot.items():
                battery_snapshot.setdefault(key, value)
            if battery_snapshot:
                segment["battery_snapshot"] = battery_snapshot

            incoming_network_snapshot: dict[str, Any] = {}
            if isinstance(network_snapshot, dict) and network_snapshot:
                incoming_network_snapshot.update(network_snapshot)
            h_ssid, h_bssid = extract_ambient_headers(headers)
            eff_ssid = ssid if ssid is not None else h_ssid
            eff_bssid = bssid if bssid is not None else h_bssid
            if eff_ssid is not None:
                incoming_network_snapshot.setdefault("ssid", eff_ssid)
            if eff_bssid is not None:
                incoming_network_snapshot.setdefault("bssid", eff_bssid)
            # 手表读不到 WiFi，用每段自带的坐标判断；走手机热点也不会被当成在路上。
            segment_geo = location if isinstance(location, dict) and location else None
            if "ambient_location" not in incoming_network_snapshot and (
                eff_ssid is not None or eff_bssid is not None or segment_geo
            ):
                loc = classify_ambient_location(eff_ssid, eff_bssid, geo=segment_geo)
                incoming_network_snapshot["ambient_location"] = loc
            network_snapshot_record: dict[str, Any] = {}
            if previous is not None and isinstance(
                previous.get("network_snapshot"), dict
            ):
                network_snapshot_record.update(previous["network_snapshot"])
            for key, value in incoming_network_snapshot.items():
                network_snapshot_record.setdefault(key, value)
            if network_snapshot_record:
                segment["network_snapshot"] = network_snapshot_record

            incoming_health_snapshot: dict[str, Any] = {}
            if isinstance(sleep, dict) and sleep:
                incoming_health_snapshot["sleep"] = sleep
            if heart_rate_bpm is not None and heart_rate_bpm > 0:
                incoming_health_snapshot["heart_rate_bpm"] = round(float(heart_rate_bpm), 1)
            if step_count is not None and step_count >= 0:
                incoming_health_snapshot["step_count"] = int(step_count)
            # [V68] 新字段：当日累计步数 + 它属于哪一天（手表本地时区）。
            # 校验从严：日期必须是 YYYY-MM-DD，步数非负；不合法的字段直接丢（不猜）。
            if steps_today is not None and type(steps_today) is int and steps_today >= 0:
                incoming_health_snapshot["steps_today"] = steps_today
                if isinstance(steps_date, str) and _STEPS_DATE_RE.fullmatch(steps_date):
                    incoming_health_snapshot["steps_date"] = steps_date
                if isinstance(steps_sampled_at, str) and steps_sampled_at:
                    incoming_health_snapshot["steps_sampled_at"] = steps_sampled_at[:40]
            if motion_state:
                incoming_health_snapshot["motion_state"] = str(motion_state)
            health_snapshot_record: dict[str, Any] = {}
            if previous is not None and isinstance(previous.get("health_snapshot"), dict):
                health_snapshot_record.update(previous["health_snapshot"])
            for key, value in incoming_health_snapshot.items():
                health_snapshot_record.setdefault(key, value)
            if health_snapshot_record:
                segment["health_snapshot"] = health_snapshot_record
                # 尝试持久化到系统 health_store
                #
                # 【2026-10-14 修 · codex 审查 P2】原来心率按 `segment["received_at"]`
                # （**上传**时刻）打戳、步数按 `datetime.now()`（**Mac 当前日期**）归档。
                # 而手表会离线缓存、延迟数天才上传（本仓已知：`received_at` 与真实录音时间
                # 可差 1 小时，见技能库「定位历史录音的音频」节；延迟上传时差几天）。
                # 后果：历史心率被搬到上传那天、步数记到 Mac 的今天 → 健康时间轴整体错位，
                # 且 `record_steps` 是**按日期覆盖**（health_store.py:123），
                # 于是「上传日的步数」会被反复覆写成不同时段的值，历史日期永远补不回来。
                #
                # 正确时间源 = 会话录音起点 + 段内偏移，与 watch_formal_asr.py:182 同口径
                # （manifest.started_at 是 UTC ISO，start_offset_ms 是该段在会话里的偏移）。
                capture_dt = None
                try:
                    _started_raw = str(manifest.get("started_at") or "").strip()
                    if _started_raw:
                        base_dt = parse_ts(_started_raw)
                        if base_dt is not None:
                            capture_dt = base_dt + timedelta(
                                milliseconds=int(segment.get("start_offset_ms") or 0)
                            )
                except (TypeError, ValueError):
                    capture_dt = None
                try:
                    from life_logger.health_store import HealthDataStore, HeartRatePoint
                    hstore = HealthDataStore(self.root.parent / "health")
                    if isinstance(health_snapshot_record.get("sleep"), dict):
                        # 睡眠写失败不能连累本段心率和步数，但必须输出日志避免静默吞错。
                        try:
                            hstore.record_sleep(health_snapshot_record["sleep"])
                        except Exception as sleep_exc:
                            print(
                                f"[watch_stream] warning: failed to persist sleep snapshot: {sleep_exc}",
                                file=sys.stderr,
                            )
                    if "heart_rate_bpm" in health_snapshot_record:
                        hr_ts = capture_dt.isoformat() if capture_dt is not None else None
                        if hr_ts:
                            hstore.record_heart_rates([
                            HeartRatePoint(
                                timestamp=hr_ts,
                                bpm=float(health_snapshot_record["heart_rate_bpm"]),
                                source=(
                                    "watch-segment-capture"
                                    if capture_dt is not None
                                    else "watch-segment"
                                ),
                            )
                        ])
                    # [V68] 优先走新口径：按**手表给的 steps_date** 归档，同一天只增不减。
                    # 老手表（V67 及以前）没有这两个字段 → 落到下面的旧逻辑，行为完全不变。
                    if "steps_today" in health_snapshot_record and "steps_date" in health_snapshot_record:
                        hstore.record_daily_steps(
                            str(health_snapshot_record["steps_date"]),
                            int(health_snapshot_record["steps_today"]),
                            sampled_at=health_snapshot_record.get("steps_sampled_at"),
                        )
                    elif "step_count" in health_snapshot_record:
                        # 旧字段：步数按**采集日**归档（本地时区），不是 Mac 的今天
                        step_date = (
                            local_date(capture_dt)
                            if capture_dt is not None
                            else None
                        )
                        if step_date:
                            hstore.record_steps(step_date, int(health_snapshot_record["step_count"]))
                except Exception as error:
                    # 健康快照写入失败不能静默：上传流程仍会 ack，用户会以为数据已入库。
                    sys.stderr.write(
                        f"[life-logger] 手表健康数据写入失败: {type(error).__name__} {error}\n"
                    )
                    sys.stderr.flush()

            incoming_location: dict[str, Any] = {}
            if isinstance(location, dict) and location:
                try:
                    lat = float(location["latitude"])
                    lon = float(location["longitude"])
                    acc = float(location["horizontal_accuracy"])
                    ts = str(location.get("timestamp") or "").strip()
                    src = str(location.get("source") or "significant_change").strip() or "significant_change"
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and acc >= 0.0 and ts:
                        incoming_location = {
                            "latitude": lat,
                            "longitude": lon,
                            "horizontal_accuracy": acc,
                            "timestamp": ts,
                            "source": src,
                        }
                except (KeyError, TypeError, ValueError):
                    incoming_location = {}
            location_record: dict[str, Any] = {}
            if previous is not None and isinstance(previous.get("location"), dict):
                location_record.update(previous["location"])
            for key, value in incoming_location.items():
                location_record.setdefault(key, value)
            if location_record:
                segment["location"] = location_record
                manifest["location"] = location_record

            incoming_pins: list[dict[str, Any]] = []
            if headers:
                raw_pins = headers.get("X-LifeLogger-Pins")
                if raw_pins:
                    try:
                        parsed_pins = json.loads(raw_pins)
                        if isinstance(parsed_pins, list):
                            incoming_pins.extend([p for p in parsed_pins if isinstance(p, dict)])
                    except Exception:
                        pass
                if not incoming_pins:
                    pin_type = headers.get("X-LifeLogger-Pin-Type")
                    if pin_type:
                        incoming_pins.append({
                            "pin_id": f"pin_{device}_{session}_{sequence}",
                            "type": str(pin_type),
                            "offset_ms": start_offset_ms,
                            "timestamp": segment["received_at"],
                        })
            if not incoming_pins and isinstance(location, dict) and location.get("pin_type"):
                incoming_pins.append({
                    "pin_id": f"pin_{device}_{session}_{sequence}",
                    "type": str(location.get("pin_type")),
                    "offset_ms": start_offset_ms,
                    "timestamp": segment["received_at"],
                })

            if incoming_pins:
                segment["pins"] = incoming_pins
                segment["pin_type"] = incoming_pins[0].get("type")
                all_manifest_pins = manifest.get("pins", [])
                existing_pin_ids = {p.get("pin_id") for p in all_manifest_pins if isinstance(p, dict)}
                for p in incoming_pins:
                    if p.get("pin_id") not in existing_pin_ids:
                        all_manifest_pins.append(p)
                        existing_pin_ids.add(p.get("pin_id"))
                manifest["pins"] = all_manifest_pins

                if any(p.get("type") == "double_pinch" for p in incoming_pins) and target.is_file():
                    first_pinch = next(p for p in incoming_pins if p.get("type") == "double_pinch")
                    if self.command_dispatcher is None:
                        threading.Thread(
                            target=self._process_vip_command_async,
                            args=(str(target), first_pinch.get("timestamp") or segment.get("received_at"), str(first_pinch.get("pin_id"))),
                            daemon=True,
                        ).start()

            if previous is None:
                segments.append(segment)
            else:
                segments[segments.index(previous)] = segment
            segments.sort(key=lambda item: int(item["sequence"]))
            manifest["segments"] = segments
            manifest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
            self._write_manifest(device, session, manifest)
            if self.command_dispatcher is not None and incoming_pins and any(p.get("type") == "double_pinch" for p in incoming_pins):
                first_pinch = next(p for p in incoming_pins if p.get("type") == "double_pinch")
                # Persist intent before ACK; the worker owns transcription/action execution.
                self.command_dispatcher({"audio_path": str(target), "timestamp": first_pinch.get("timestamp") or segment.get("received_at"),
                    "pin_id": str(first_pinch.get("pin_id")), "device_id": device, "session_id": session})

            haptic = "directionUp" if sequence == 0 else self.pop_haptic(device)

            return {
                "ok": True,
                "duplicate": duplicate,
                "session_id": session,
                "sequence": sequence,
                "sha256": expected_checksum,
                "stored_bytes": len(body),
                "acknowledged_at": manifest["updated_at"],
                "haptic": haptic,
            }

    def _process_vip_command_async(self, audio_path: str, timestamp: str, pin_id: str) -> None:
        try:
            from life_logger.vip_fast_track import transcribe_vip_snippet, execute_vip_command
            text = transcribe_vip_snippet(audio_path)
            res = execute_vip_command(text=text, timestamp=timestamp, pin_id=pin_id)
            sys.stderr.write(f"[life-logger] VIP command processed: {res.get('title')} ({res.get('action_taken')})\n")
            sys.stderr.flush()
        except Exception as error:
            sys.stderr.write(f"[life-logger] VIP command processing failed: {error}\n")
            sys.stderr.flush()

    def complete_session(
        self,
        *,
        device_id: str,
        session_id: str,
        expected_segments: int,
        ended_at: str,
        capsules: list[dict[str, Any]] | None = None,
        ended_by: str | None = None,
        end_reason: str | None = None,
    ) -> dict[str, Any]:
        device, session = self._validate_ids(device_id, session_id)
        if not 0 < expected_segments <= MAX_WATCH_SEGMENTS:
            raise WatchSessionError("expected_segments out of range")
        try:
            ended = datetime.fromisoformat(str(ended_at))
        except ValueError as error:
            raise WatchSessionError("ended_at must be ISO-8601") from error
        if ended.tzinfo is None:
            raise WatchSessionError("ended_at must include a timezone")

        with self._lock:
            manifest = self._read_manifest(device, session)
            if manifest.get("state") == "complete":
                # A completed session is closed: the job exists, the audio is assembled. The
                # Watch re-sends `complete` on every sync until it gets its acknowledgement, so
                # this path must answer, not refuse. Comparing `ended_at` strings here used to
                # 409 every retry, because the Watch's clock runs hours ahead of the Mac's —
                # the session was already closed with the Mac-derived time, and no amount of
                # retrying changes that.
                # 旧 manifest 没有 ended_by/end_reason；设备重试时若带上了，就补写一次。
                backfilled = False
                warnings: list[str] = []
                reported, enum_ok = normalize_ended_by(ended_by)
                if reported and not enum_ok:
                    # 未知值保留原文，但标记非法枚举；绝不静默丢弃，也不覆盖已上报值。
                    manifest["ended_by_warning"] = ENDED_BY_INVALID_WARNING
                    warnings.append(ENDED_BY_INVALID_WARNING)
                # [Q161] 旧条件 `not manifest.get("ended_by")` 会被占位值 watch_client 挡住，
                # 导致设备后来重报的 ended_by 永远写不进去。改为看来源：
                # 只有「已确认是设备上报」才不覆盖。
                if reported and manifest.get("ended_by_source") != "client_reported":
                    manifest["ended_by"] = reported
                    manifest["ended_by_source"] = "client_reported"
                    backfilled = True
                    if not enum_ok:
                        pass  # warning 已记
                if end_reason and manifest.get("ended_by_source") == "client_reported" and backfilled:
                    manifest["end_reason"] = str(end_reason)
                elif end_reason and not manifest.get("end_reason"):
                    manifest["end_reason"] = str(end_reason)
                    backfilled = True
                if backfilled:
                    manifest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
                    self._write_manifest(device, session, manifest)
                return {
                    "complete": True,
                    "duplicate": True,
                    "received_segments": len(manifest.get("segments", [])),
                    "session": manifest,
                    "warnings": warnings,
                }
            if manifest.get("state") != "receiving":
                raise WatchSessionConflictError(
                    "Watch session can't be completed from this state"
                )

            by_sequence = {
                int(item["sequence"]): item for item in manifest.get("segments", [])
            }
            requested = {
                "expected_segments": expected_segments,
                "ended_at": ended.isoformat(),
            }
            intent = manifest.get("completion_intent")
            if isinstance(intent, dict):
                if int(intent.get("expected_segments", expected_segments)) != expected_segments:
                    raise WatchSessionConflictError(
                        "conflicting completion intent expected_segments"
                    )
            manifest["completion_intent"] = {
                **requested,
                "requested_at": datetime.now(LOCAL_TZ).isoformat(),
            }
            unexpected = sorted(
                sequence for sequence in by_sequence if sequence >= expected_segments
            )
            if unexpected:
                raise WatchSessionConflictError(
                    f"unexpected segments beyond expected count: {unexpected}"
                )
            missing = [
                sequence
                for sequence in range(expected_segments)
                if sequence not in by_sequence
            ]
            started = datetime.fromisoformat(str(manifest["started_at"]))
            if by_sequence:
                latest_audio_end_ms = max(
                    int(item["start_offset_ms"]) + int(item["duration_ms"])
                    for item in by_sequence.values()
                )
                minimum_ended_at = started + timedelta(
                    milliseconds=max(0, latest_audio_end_ms - 1_000)
                )
                if ended < minimum_ended_at:
                    raise WatchSessionError(
                        "ended_at is earlier than the received audio timeline"
                    )
            if missing:
                if not isinstance(intent, dict):
                    manifest["completion_intent"] = {
                        **requested,
                        "requested_at": datetime.now(LOCAL_TZ).isoformat(),
                    }
                    manifest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
                    self._write_manifest(device, session, manifest)
                raise IncompleteWatchSessionError(missing)

            manifest["state"] = "complete"
            manifest.pop("completion_intent", None)
            manifest["expected_segments"] = expected_segments
            manifest["ended_at"] = ended.isoformat()
            # 停录归因：设备上报就用设备的；没上报就显式记「推断」，不假装知道原因。
            reported_ended_by, ended_by_enum_ok = normalize_ended_by(ended_by)
            reported_reason = str(end_reason).strip() if isinstance(end_reason, str) else ""
            manifest["ended_by"] = reported_ended_by or "watch_client"
            manifest["ended_by_source"] = "client_reported" if reported_ended_by else "inferred"
            if reported_ended_by and not ended_by_enum_ok:
                # 未知值保留原文（不丢弃证据），但显式标记，便于诊断页提示与后续清洗。
                manifest["ended_by_warning"] = ENDED_BY_INVALID_WARNING
            else:
                manifest.pop("ended_by_warning", None)
            manifest["end_reason"] = reported_reason or (
                "client_complete" if reported_ended_by else "completion_request_without_ended_by"
            )
            if capsules and isinstance(capsules, list):
                manifest["capsules"] = capsules
            manifest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
            manifest["total_bytes"] = sum(
                int(item["size_bytes"]) for item in by_sequence.values()
            )
            manifest["total_duration_ms"] = sum(
                int(item["duration_ms"]) for item in by_sequence.values()
            )
            self._write_manifest(device, session, manifest)
            return {
                "complete": True,
                "duplicate": False,
                "received_segments": len(manifest["segments"]),
                "total_bytes": manifest["total_bytes"],
                "total_duration_ms": manifest["total_duration_ms"],
                "session": manifest,
                "warnings": [ENDED_BY_INVALID_WARNING] if manifest.get("ended_by_warning") else [],
            }

    def finalize_completion_intents(self) -> list[dict[str, Any]]:
        """Finish only sessions with the Watch's exact segment count and stop time.

        A continuous prefix and a newer session do not prove that every segment has reached the
        Mac: offline uploads can resume days later. A completion request is an explicit stop
        signal. Persisting it when segments are missing lets the service finish automatically
        once the exact declared range arrives, without closing a session based on inactivity.
        """
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        with self._lock:
            for path in sorted(self.root.glob("*/*/manifest.json")):
                try:
                    device, session = self._validate_ids(
                        path.parent.parent.name, path.parent.name
                    )
                    manifest = self._read_manifest(device, session)
                except (OSError, ValueError, TypeError, WatchSessionError):
                    continue
                if (
                    manifest.get("state") == "receiving"
                    and isinstance(manifest.get("completion_intent"), dict)
                ):
                    candidates.append((device, session, manifest))

        report: list[dict[str, Any]] = []
        for device, session, manifest in candidates:
            intent = manifest["completion_intent"]
            expected = intent.get("expected_segments")
            ended_at = intent.get("ended_at")
            entry: dict[str, Any] = {
                "device_id": device,
                "session_id": session,
                "expected_segments": expected,
                "received_segments": len(manifest.get("segments") or []),
            }
            if type(expected) is not int or not 0 < expected <= MAX_WATCH_SEGMENTS:
                report.append({**entry, "status": "invalid_completion_intent"})
                continue
            if not isinstance(ended_at, str):
                report.append({**entry, "status": "invalid_completion_intent"})
                continue
            try:
                sequences = sorted(
                    int(item["sequence"])
                    for item in (manifest.get("segments") or [])
                )
            except (KeyError, TypeError, ValueError):
                report.append({**entry, "status": "invalid_segments"})
                continue
            missing = [sequence for sequence in range(expected) if sequence not in sequences]
            unexpected = [sequence for sequence in sequences if sequence >= expected]
            if unexpected:
                report.append(
                    {**entry, "status": "unexpected_segments", "unexpected": unexpected[:50]}
                )
                continue
            if missing or len(sequences) != expected:
                report.append(
                    {**entry, "status": "waiting_segments", "missing": missing[:50]}
                )
                continue
            try:
                self.complete_session(
                    device_id=device,
                    session_id=session,
                    expected_segments=expected,
                    ended_at=ended_at,
                )
            except (WatchSessionError, OSError, ValueError) as error:
                report.append({**entry, "status": "failed", "error": str(error)})
                continue
            report.append({**entry, "status": "completed"})
        return report

    def assemble_recording(self, device_id: str, session_id: str) -> Path:
        device, session = self._validate_ids(device_id, session_id)
        from life_logger.runtime_queue import ProcessRLock
        # Assembly is exclusive only for this session. Never hold the metadata
        # lock while hashing audio or waiting up to minutes for ffmpeg.
        with ProcessRLock(self._session_dir(device, session) / ".assembly.lock"):
            with self._lock:
                manifest = self._read_manifest(device, session)
            if manifest.get("state") != "complete":
                raise WatchSessionConflictError(
                    "session must be complete before assembly"
                )
            session_dir = self._session_dir(device, session)
            output = session_dir / "recording.m4a"
            expected_digest = str(manifest.get("recording_sha256") or "")
            if output.is_file() and expected_digest:
                if _sha256_header(output.read_bytes()) == expected_digest:
                    return output

            segments = sorted(
                manifest.get("segments", []),
                key=lambda item: int(item["sequence"]),
            )
            concat_path = session_dir / "concat.txt"
            concat_lines: list[str] = []
            for item in segments:
                filename = str(item.get("file") or "")
                if Path(filename).name != filename:
                    raise WatchSessionError("unsafe segment filename in manifest")
                segment_path = session_dir / "segments" / filename
                if not segment_path.is_file():
                    raise WatchSessionError("a stored Watch segment is missing")
                if _sha256_header(segment_path.read_bytes()) != item.get("sha256"):
                    raise WatchSessionChecksumMismatchError(
                        "stored segment checksum mismatch"
                    )
                concat_lines.append(f"file 'segments/{filename}'")
            _write_atomic(concat_path, ("\n".join(concat_lines) + "\n").encode("utf-8"))

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise WatchSessionError("ffmpeg is required to assemble Watch segments")
            temporary = session_dir / "recording.partial.m4a"
            temporary.unlink(missing_ok=True)
            common = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_path.name,
                "-vn",
            ]
            try:
                copy_result = subprocess.run(
                    [
                        *common,
                        "-c:a",
                        "copy",
                        "-movflags",
                        "+faststart",
                        temporary.name,
                    ],
                    cwd=session_dir,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WatchSessionProcessingError(
                    "failed to assemble the Watch session audio"
                ) from error
            if (
                copy_result.returncode != 0
                or not temporary.is_file()
                or temporary.stat().st_size == 0
            ):
                temporary.unlink(missing_ok=True)
                try:
                    fallback_result = subprocess.run(
                        [
                            *common,
                            "-c:a",
                            "aac",
                            "-b:a",
                            "32k",
                            "-ar",
                            str(int(manifest["sample_rate"])),
                            "-ac",
                            str(int(manifest["channels"])),
                            temporary.name,
                        ],
                        cwd=session_dir,
                        capture_output=True,
                        text=True,
                        timeout=300,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise WatchSessionProcessingError(
                        "failed to assemble the Watch session audio"
                    ) from error
                if (
                    fallback_result.returncode != 0
                    or not temporary.is_file()
                    or temporary.stat().st_size == 0
                ):
                    temporary.unlink(missing_ok=True)
                    raise WatchSessionProcessingError(
                        "failed to assemble the Watch session audio"
                    )

            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            digest = _sha256_header(temporary.read_bytes())
            size = temporary.stat().st_size
            signature = lambda rows: [(item.get("sequence"), item.get("file"), item.get("sha256")) for item in rows]
            with self._lock:
                latest = self._read_manifest(device, session)
                if latest.get("state") != "complete" or signature(latest.get("segments", [])) != signature(segments):
                    raise WatchSessionConflictError("session audio changed during assembly")
                os.replace(temporary, output)
                _fsync_directory(session_dir)
                # Merge into current metadata; completion jobs/pins can be updated
                # while ffmpeg is running and must not be overwritten.
                latest["recording_file"] = output.name
                latest["recording_sha256"] = digest
                latest["recording_bytes"] = size
                latest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
                self._write_manifest(device, session, latest)
            return output

    def session_directory(self, device_id: str, session_id: str) -> Path:
        device, session = self._validate_ids(device_id, session_id)
        return self._session_dir(device, session)

    def superseded_receiving_sessions(self) -> list[dict[str, Any]]:
        """列出同一设备上已被更晚开始的会话取代、却仍停在 receiving 的会话。"""
        entries: list[tuple[str, str, str]] = []
        with self._lock:
            for path in sorted(self.root.glob("*/*/manifest.json")):
                try:
                    device, session = self._validate_ids(
                        path.parent.parent.name, path.parent.name
                    )
                    manifest = self._read_manifest(device, session)
                except (OSError, ValueError, TypeError):
                    continue
                entries.append((device, session, str(manifest.get("started_at") or "")))
        latest: dict[str, str] = {}
        for device, _session, started in entries:
            if started > latest.get(device, ""):
                latest[device] = started
        superseded: list[dict[str, Any]] = []
        for device, session, started in entries:
            if not started or latest.get(device, "") <= started:
                continue
            try:
                manifest = self.load_session(device, session)
            except (OSError, ValueError):
                continue
            if manifest.get("state") != "receiving":
                continue
            superseded.append(
                {
                    "device_id": device,
                    "session_id": session,
                    "started_at": started,
                    "manifest": manifest,
                }
            )
        return superseded

    def abandon_empty_sessions(
        self,
        *,
        apply: bool = False,
        idle_seconds: float = WATCH_RECEIVING_STALE_SECONDS,
        session_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """作废"已登记却一段音频都没有"的接收中会话。

        这种会话两边都关不掉：手表 `WatchSyncEngine.syncPending()` 只处理有分段的会话，
        服务端 `complete_session` 又要求 `expected_segments > 0`，于是一条 0 段的登记会
        永远以 `receiving` 躺在台账里，冒充"在途录音"，把验收数字和"整体到哪了"一起搞脏。

        判据两条，取并集：①已被同一设备更晚开始的会话取代（设备同一时刻只有一个会话）；
        ②空闲超过 ``idle_seconds``（缺省 6 小时，与 `WATCH_RECEIVING_STALE_SECONDS` 同口径）。
        两条都只作用于**没有分段**的会话，所以不存在"收口切掉后续分段"的风险。

        作废不是销毁：manifest 一个字节都不删，只把 `state` 标成 ``abandoned`` 并记下原因；
        手表之后补传分段会自动把它复活成 `receiving`（见 `receive_segment`），所以"离线一天
        再上传"的录音不会被这里判死。
        """
        wanted = {str(value) for value in session_ids} if session_ids else None
        now = datetime.now(LOCAL_TZ)
        rows: list[tuple[str, str, dict[str, Any]]] = []
        with self._lock:
            for path in sorted(self.root.glob("*/*/manifest.json")):
                try:
                    device, session = self._validate_ids(
                        path.parent.parent.name, path.parent.name
                    )
                    manifest = self._read_manifest(device, session)
                except (OSError, ValueError, TypeError):
                    continue
                rows.append((device, session, manifest))
        latest: dict[str, str] = {}
        for device, _session, manifest in rows:
            started = str(manifest.get("started_at") or "")
            if started > latest.get(device, ""):
                latest[device] = started

        report: list[dict[str, Any]] = []
        for device, session, manifest in rows:
            if manifest.get("state") != "receiving" or manifest.get("segments"):
                continue
            started = str(manifest.get("started_at") or "")
            superseded = bool(started) and latest.get(device, "") > started
            age = _activity_age_seconds(manifest.get("updated_at"), now)
            if not superseded and (age is None or age < idle_seconds):
                continue
            if wanted is not None and session not in wanted:
                continue
            entry: dict[str, Any] = {
                "device_id": device,
                "session_id": session,
                "started_at": started,
                "received_segments": 0,
                "reason": "superseded" if superseded else "idle",
                "idle_seconds": round(age, 1) if age is not None else None,
            }
            if not apply:
                report.append({**entry, "status": "dry_run"})
                continue
            try:
                with self._lock:
                    current = self._read_manifest(device, session)
                    if current.get("state") != "receiving" or current.get("segments"):
                        report.append({**entry, "status": "skipped_changed"})
                        continue
                    current["state"] = "abandoned"
                    current["abandoned_at"] = now.isoformat()
                    current["abandon_reason"] = entry["reason"]
                    current["updated_at"] = now.isoformat()
                    self._write_manifest(device, session, current)
            except (OSError, ValueError, WatchSessionError) as error:
                report.append({**entry, "status": "failed", "error": str(error)})
                continue
            report.append({**entry, "status": "abandoned"})
        return report

    def finalize_superseded_sessions(
        self,
        *,
        apply: bool = False,
        session_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """手工救援被新会话取代的连续接收记录。

        这是供显式 dry-run / apply 脚本使用的恢复工具，不可放入无人值守服务循环：
        更新会话与静默时间都不能证明旧会话的所有分片已经上传。离线上传可能数天后恢复；
        只有 Watch 的精确完成信号才能安全地自动关闭会话。

        complete_session 要求段号 0..n-1 无缺，因此只有连续无缺且有内容的会话会被
        报告为可救援；缺段与空会话只进报告不改动。

        ``session_ids`` 用于只收口指定会话：一次收口全部滞留会话会把十几个小时音频
        同时压进处理队列（实测 12 个会话 / 10.3 小时），必须能一个一个放行。
        """
        wanted = {str(value) for value in session_ids} if session_ids else None
        report: list[dict[str, Any]] = []
        for candidate in self.superseded_receiving_sessions():
            device = str(candidate["device_id"])
            session = str(candidate["session_id"])
            if wanted is not None and session not in wanted:
                continue
            manifest = candidate["manifest"]
            entry: dict[str, Any] = {
                "device_id": device,
                "session_id": session,
                "started_at": candidate["started_at"],
                "received_segments": len(manifest.get("segments") or []),
                "received_duration_ms": sum(
                    int(item.get("duration_ms") or 0)
                    for item in (manifest.get("segments") or [])
                ),
            }
            sequences = sorted(
                int(item["sequence"]) for item in (manifest.get("segments") or [])
            )
            if not sequences:
                report.append({**entry, "status": "skipped_empty"})
                continue
            expected = len(sequences)
            missing = [
                sequence
                for sequence in range(sequences[-1] + 1)
                if sequence not in set(sequences)
            ]
            if missing:
                report.append(
                    {**entry, "status": "skipped_missing", "missing": missing[:50]}
                )
                continue
            started = datetime.fromisoformat(str(manifest["started_at"]))
            latest_audio_end_ms = max(
                int(item["start_offset_ms"]) + int(item["duration_ms"])
                for item in manifest["segments"]
            )
            ended = started + timedelta(milliseconds=latest_audio_end_ms)
            # 只用「会话起始 + 音频时间轴」定结束时刻，绝不参考 updated_at。
            # updated_at 是 manifest 的最后写入时间：一次补传就把它推到当下，
            # 会把 09-29 的录音标成 10-01，成品就落进错误的日期目录、不进当天简报。
            entry["expected_segments"] = expected
            entry["ended_at"] = ended.isoformat()
            if not apply:
                report.append({**entry, "status": "dry_run"})
                continue
            try:
                self.complete_session(
                    device_id=device,
                    session_id=session,
                    expected_segments=expected,
                    ended_at=ended.isoformat(),
                )
            except (WatchSessionError, OSError, ValueError) as error:
                report.append({**entry, "status": "failed", "error": str(error)})
                continue
            report.append({**entry, "status": "completed"})
        return report

    def auto_finalize_idle_sessions(
        self,
        *,
        idle_threshold_seconds: float = 1800.0,
        apply: bool = True,
    ) -> list[dict[str, Any]]:
        """自动安全收口超过空闲门限且分片连续无缺失的接收中会话。

        判据：
        1. 会话处于 receiving 状态；
        2. 会话切片数量 > 0 且分段序列 0..n-1 严格无缺失；
        3. 最后一个分片的 received_at（或 manifest 写入时间）距今已超过 idle_threshold_seconds（默认 30 分钟）；
        4. 收口时按照「音频起始时间 + 实际录制毫秒数」精准计算 ended_at，保证日期与归档绝对准确。
        """
        now = datetime.now(timezone.utc)
        report: list[dict[str, Any]] = []
        # 注意：complete_session 内部会获取 self._lock，此处外部先收集候选
        candidates: list[tuple[str, str, dict[str, Any], int, str, float]] = []
        with self._lock:
            for path in sorted(self.root.glob("*/*/manifest.json")):
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if manifest.get("state") != "receiving":
                    continue
                segments = manifest.get("segments") or []
                if not segments:
                    continue
                device = path.parent.parent.name
                session = path.parent.name

                last_activity_str = (
                    segments[-1].get("received_at")
                    or manifest.get("updated_at")
                    or manifest.get("started_at")
                )
                if not last_activity_str:
                    continue
                try:
                    last_dt = datetime.fromisoformat(str(last_activity_str))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                idle_sec = (now - last_dt).total_seconds()
                if idle_sec < idle_threshold_seconds:
                    continue

                sequences = sorted(
                    int(item["sequence"]) for item in segments if "sequence" in item
                )
                if not sequences or sequences[0] != 0:
                    continue
                missing = [
                    seq for seq in range(sequences[-1] + 1) if seq not in set(sequences)
                ]
                if missing:
                    continue

                expected = len(sequences)
                started = datetime.fromisoformat(str(manifest["started_at"]))
                latest_audio_end_ms = max(
                    int(item.get("start_offset_ms", 0)) + int(item.get("duration_ms", 0))
                    for item in segments
                )
                ended = started + timedelta(milliseconds=latest_audio_end_ms)
                candidates.append((device, session, manifest, expected, ended.isoformat(), idle_sec))

        for device, session, manifest, expected, ended_at, idle_sec in candidates:
            entry = {
                "device_id": device,
                "session_id": session,
                "idle_seconds": round(idle_sec, 1),
                "segments": expected,
                "ended_at": ended_at,
            }
            if not apply:
                report.append({**entry, "status": "dry_run"})
                continue
            try:
                self.complete_session(
                    device_id=device,
                    session_id=session,
                    expected_segments=expected,
                    ended_at=ended_at,
                )
                report.append({**entry, "status": "auto_completed"})
            except Exception as err:
                report.append({**entry, "status": "failed", "error": str(err)})
        return report

    def get_session_pins(self, device_id: str, session_id: str) -> list[dict[str, Any]]:
        device, session = self._validate_ids(device_id, session_id)
        with self._lock:
            manifest = self._read_manifest(device, session)
            return list(manifest.get("pins", []))

    def get_session_capsules(self, device_id: str, session_id: str) -> list[dict[str, Any]]:
        device, session = self._validate_ids(device_id, session_id)
        with self._lock:
            manifest = self._read_manifest(device, session)
            return list(manifest.get("capsules", []))

    def mark_completion_job(self, device_id: str, session_id: str, job_id: str) -> None:
        """Persist a stable Job ID before the Watch completion request is acknowledged."""
        device, session = self._validate_ids(device_id, session_id)
        job = str(job_id or "")
        if not job.startswith("job-") or Path(job).name != job:
            raise WatchSessionError("invalid Life Logger job id")
        with self._lock:
            manifest = self._read_manifest(device, session)
            if manifest.get("state") not in {"receiving", "complete"}:
                raise WatchSessionConflictError("Watch session cannot reserve a job")
            existing = manifest.get("completion_job_id")
            if existing and existing != job:
                raise WatchSessionConflictError(
                    "Watch session already reserved another completion job"
                )
            manifest["completion_job_id"] = job
            manifest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
            self._write_manifest(device, session, manifest)

    def mark_imported(self, device_id: str, session_id: str, job_id: str) -> None:
        device, session = self._validate_ids(device_id, session_id)
        job = str(job_id or "")
        if not job.startswith("job-") or Path(job).name != job:
            raise WatchSessionError("invalid Life Logger job id")
        with self._lock:
            manifest = self._read_manifest(device, session)
            existing = manifest.get("job_id")
            if existing and existing != job:
                raise WatchSessionConflictError(
                    "Watch session is already linked to another job"
                )
            completion_job = manifest.get("completion_job_id")
            if completion_job and completion_job != job:
                raise WatchSessionConflictError(
                    "Watch session completion job does not match imported job"
                )
            manifest["job_id"] = job
            manifest["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
            self._write_manifest(device, session, manifest)

    def load_session(self, device_id: str, session_id: str) -> dict[str, Any]:
        device, session = self._validate_ids(device_id, session_id)
        with self._lock:
            return self._read_manifest(device, session)