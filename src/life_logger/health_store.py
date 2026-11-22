"""Durable Health & Biometric Data Store for Life Logger.

Tracks and aggregates health indicators from Apple Watch / HealthKit:
- Heart Rate (BPM) time series
- Steps & Active Energy
- Sleep stages (In Bed, Asleep, Deep, Core, REM, Awake)
- Physiological stress correlation with audio events
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Sequence

from life_logger.atomic import write_bytes_atomic
from life_logger.timeutil import LOCAL_TZ, local_date, parse_ts

try:            # POSIX 才有 fcntl；非 POSIX 明确告警，不静默假装加锁
    import fcntl
except ImportError:  # pragma: no cover - 仅非 POSIX 平台
    fcntl = None

logger = logging.getLogger(__name__)

#: 等待文件锁的上限（秒）。可用环境变量覆盖，便于运维/测试。
LOCK_TIMEOUT_SECONDS = float(os.environ.get("HEALTH_STORE_LOCK_TIMEOUT", "5"))


class HealthStoreAccessError(PermissionError):
    """健康文件不可读写时的显式错误。

    绝不静默返回默认值，也绝不改权限后假装成功 —— 调用方必须看到失败与原因。
    """


def _describe(path: Path) -> str:
    """给诊断用的路径 + 权限快照（失败不抛）。"""
    try:
        st = path.stat()
        return (f"{path} mode={stat.S_IMODE(st.st_mode):04o} "
                f"uid={st.st_uid} gid={st.st_gid} size={st.st_size}")
    except OSError as exc:
        return f"{path} stat失败: {type(exc).__name__}: {exc}"


def _require_readable(path: Path) -> None:
    """读之前显式检查：没有读位或实际不可读 → 失败可见（不留空数据假象）。"""
    try:
        st = path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HealthStoreAccessError(
            f"health_store: 无法检查健康文件权限（{_describe(path)}）：{type(exc).__name__}: {exc}"
        ) from exc
    mode = stat.S_IMODE(st.st_mode)
    if not (mode & 0o444) or not os.access(path, os.R_OK):
        raise HealthStoreAccessError(
            f"health_store: 健康文件不可读，拒绝返回空数据（{_describe(path)}）；"
            f"请修复权限后重试（例如 chmod 600 <file>）"
        )


def _require_writable(path: Path) -> None:
    """写之前显式检查：没有写位或实际不可写 → 失败可见（绝不覆盖后改权限假装成功）。"""
    try:
        st = path.stat()
    except FileNotFoundError:
        return                      # 新文件：目录可写即可
    except OSError as exc:
        raise HealthStoreAccessError(
            f"health_store: 无法检查健康文件权限（{_describe(path)}）：{type(exc).__name__}: {exc}"
        ) from exc
    mode = stat.S_IMODE(st.st_mode)
    if not (mode & 0o200):
        raise HealthStoreAccessError(
            f"health_store: 健康文件不可写，拒绝覆盖（{_describe(path)}）；"
            f"请修复权限后重试（例如 chmod 600 <file>）"
        )
    if not os.access(path, os.W_OK):
        raise HealthStoreAccessError(
            f"health_store: 健康文件对当前用户不可写，拒绝覆盖（{_describe(path)}）"
        )


def _target_mode(path: Path) -> int:
    """沿用目标文件现有权限；文件不存在时用 0600（健康数据默认私有）。

    不能用固定 0o644：那会把用户收紧到 0600 的文件在一次写入后放宽回 0644。
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return 0o600
    except OSError as exc:
        raise HealthStoreAccessError(
            f"health_store: 无法读取健康文件权限（{_describe(path)}）：{type(exc).__name__}: {exc}"
        ) from exc
    mode = stat.S_IMODE(st.st_mode)
    if not (mode & 0o400):
        # 沿用不了「不可读」的权限（写出去也读不回来），显式失败而不是改成 0600 假装成功
        raise HealthStoreAccessError(
            f"health_store: 目标文件权限不允许读取，拒绝写入（{_describe(path)}）；"
            f"请修复权限后重试"
        )
    return mode


@contextlib.contextmanager
def _locked(path: Path):
    """带超时的跨进程排它锁（flock）。

    * 拿锁失败会在 ``LOCK_TIMEOUT_SECONDS`` 后抛 ``TimeoutError``，**绝不静默等待**；
    * 非 POSIX 平台没有 fcntl：记一条 warning 后无锁执行（行为可见，不假装加锁）。
    """
    if fcntl is None:  # pragma: no cover - 仅非 POSIX 平台
        logger.warning("health_store: 当前平台无 fcntl，文件锁降级为无锁（%s）", path)
        yield
        return

    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"health_store: 等待文件锁超时（{LOCK_TIMEOUT_SECONDS:g}s）：{lock_path}"
                    ) from None
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


@dataclass(frozen=True)
class HeartRatePoint:
    timestamp: str  # ISO-8601
    bpm: float
    source: str = "apple-watch"
    context: str = "active"  # resting, active, workout

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SleepSessionRecord:
    date: str  # YYYY-MM-DD
    start_time: str
    end_time: str
    total_minutes: int
    deep_minutes: int
    core_minutes: int
    rem_minutes: int
    awake_minutes: int
    source: str = "apple-watch"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sort_key(record: Any) -> tuple[bool, float]:
    """排序键：能解析的按 instant 排，解析不了的排最后（不丢、不猜）。"""
    instant = _instant_key(record)
    return (instant is None, instant or 0.0)


def _instant_key(record: Any) -> float | None:
    """采集 instant（epoch 秒）；解析不出来返回 ``None``（不猜、不造）。"""
    raw = record.get("timestamp") if isinstance(record, Mapping) else record
    parsed = parse_ts(raw)
    return parsed.timestamp() if parsed is not None else None


@dataclass(frozen=True)
class DailyHealthSummary:
    date: str
    avg_heart_rate: float
    min_heart_rate: float
    max_heart_rate: float
    total_steps: int
    sleep_minutes: int
    deep_sleep_minutes: int
    stress_spikes_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HealthDataStore:
    """Persistent storage and query engine for Watch health data."""

    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.hr_path = self.storage_dir / "health_heart_rate.json"
        self.sleep_path = self.storage_dir / "health_sleep.json"
        self.steps_path = self.storage_dir / "health_steps.json"
        self._init_files()

    def _init_files(self) -> None:
        for path in (self.hr_path, self.sleep_path, self.steps_path):
            if not path.exists():
                write_bytes_atomic(path, b"[]", mode=_target_mode(path))

    def _read_json_list(self, path: Path) -> list[dict[str, Any]]:
        """读取健康文件。

        语义（2026-10-30 修复 H）：
          * 文件不存在 → ``[]``（正常首次读取）；
          * 权限不足 / IO 失败 → 抛 :class:`HealthStoreAccessError`（**绝不静默返回 ``[]``**）；
          * JSON 损坏 → ``[]``（保持既有容错，未改动）。
        """
        _require_readable(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except PermissionError as exc:
            raise HealthStoreAccessError(
                f"health_store: 读取被拒绝（{_describe(path)}）：{exc}"
            ) from exc
        except json.JSONDecodeError:
            return []
        except OSError as exc:
            raise HealthStoreAccessError(
                f"health_store: 读取失败（{_describe(path)}）：{type(exc).__name__}: {exc}"
            ) from exc

    def _write_json_list(self, path: Path, items: list[dict[str, Any]]) -> None:
        _require_writable(path)
        payload = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
        write_bytes_atomic(path, payload, mode=_target_mode(path))

    def record_heart_rates(self, points: Sequence[HeartRatePoint | dict[str, Any]]) -> None:
        """Batch record heart rate points."""
        with _locked(self.hr_path):
            if not points:
                return
            records = self._read_json_list(self.hr_path)
            existing_ts = {str(r.get("timestamp")) for r in records}
            for pt in points:
                d = pt.to_dict() if isinstance(pt, HeartRatePoint) else dict(pt)
                ts = str(d.get("timestamp"))
                # 【保持既有语义】去重仍按**原始时间戳字符串**：同一 instant 用不同偏移
                # 上传的两次采样各自保留（不合并、不改写），只是排序按 instant 做。
                if ts and ts not in existing_ts:
                    records.append(d)
                    existing_ts.add(ts)
            # [Q480] 采集 instant 排序：`+00:00` 与 `+08:00` 混存时按字符串比较会排错，
            # 原始 timestamp 字符串一律不改，只用解析出的 instant 做排序键。
            records.sort(key=_sort_key)
            self._write_json_list(self.hr_path, records)

    def record_sleep(self, sleep_record: SleepSessionRecord | dict[str, Any]) -> None:
        """Record or update a daily sleep session."""
        with _locked(self.sleep_path):
            d = sleep_record.to_dict() if isinstance(sleep_record, SleepSessionRecord) else dict(sleep_record)
            records = self._read_json_list(self.sleep_path)
            # 替换同日期的旧睡眠记录
            target_date = str(d.get("date", ""))
            existing = next((r for r in records if str(r.get("date", "")) == target_date), None)
            # 手表补传的旧快照可能比已有记录短，手表来源只允许变长。
            if (
                existing is not None
                and d.get("source") == "apple-watch"
                and int(existing.get("total_minutes") or 0) > int(d.get("total_minutes") or 0)
            ):
                return
            if existing == d:
                return
            records = [r for r in records if str(r.get("date", "")) != target_date]
            records.append(d)
            records.sort(key=lambda r: str(r.get("date", "")))
            self._write_json_list(self.sleep_path, records)

    def record_steps(self, date_str: str, steps: int, timestamp: str | None = None) -> None:
        """Record step count checkpoint for a given date."""
        with _locked(self.steps_path):
            records = self._read_json_list(self.steps_path)
            entry = {
                "date": date_str,
                "steps": int(steps),
                # [Q480] 无偏移的 naive 时间戳在跨时区机器上无法判日 → 新产物一律北京带偏移。
                "updated_at": timestamp or datetime.now(LOCAL_TZ).isoformat(),
            }
            records = [r for r in records if str(r.get("date", "")) != date_str]
            records.append(entry)
            self._write_json_list(self.steps_path, records)

    def record_daily_steps(
        self,
        date_str: str,
        steps: int,
        *,
        sampled_at: str | None = None,
    ) -> dict[str, Any]:
        """记录「某自然日的当日累计步数」——**同一天只增不减**。

        为什么需要它（2026-11-07 V68）：
        手表 V67 及以前只上传一个 `stepCount`（不带"这个数属于哪一天"），服务端按
        **分段采集日**归档、且 `record_steps()` 是**整条覆盖**（最后写入者赢）。
        结果是：延迟上传的旧分段带着当天较早的小值，把已经写好的大值盖掉
        （11-04 实测：真实 7666 被覆盖成 1212）；跨日还会把前一天的累计带进新的一天
        （11-02 23:12 的 8328 出现在 11-03 00:38）。

        新契约（V68）：手表上传 `steps_today` + `steps_date`（值所属的自然日，手表本地时区），
        服务端按 `steps_date` 归档，**同一自然日只接受更大的值**；手表重启/重装导致计数变小时
        忽略并记 warning，不覆盖已有的较大值。

        返回落盘的那条记录（含 `accepted` / `previous_steps`，便于调用方透出）。
        """
        date_str = str(date_str or "").strip()
        if not date_str:
            raise ValueError("steps_date 不能为空")
        steps = int(steps)
        if steps < 0:
            raise ValueError("steps 不能为负")
        with _locked(self.steps_path):
            records = self._read_json_list(self.steps_path)
            existing = next(
                (r for r in records if str(r.get("date", "")) == date_str), None
            )
            previous = int(existing.get("steps", 0)) if isinstance(existing, dict) else None
            if previous is not None and steps < previous:
                # 当日累计只增不减：手表重启会从 0 重新数，不能把已有的大值覆盖掉。
                logger.warning(
                    "steps_today 变小，忽略：date=%s previous=%s candidate=%s sampled_at=%s",
                    date_str, previous, steps, sampled_at,
                )
                return {**(existing or {"date": date_str}), "accepted": False,
                        "previous_steps": previous, "ignored_steps": steps}
            entry = {
                "date": date_str,
                "steps": steps,
                "source": "watch-daily",
                "updated_at": sampled_at or datetime.now(LOCAL_TZ).isoformat(),
            }
            if existing is not None:
                entry["updated_at"] = datetime.now(LOCAL_TZ).isoformat()
            records = [r for r in records if str(r.get("date", "")) != date_str]
            records.append(entry)
            records.sort(key=lambda r: str(r.get("date", "")))
            self._write_json_list(self.steps_path, records)
            return {**entry, "accepted": True, "previous_steps": previous}

    def get_heart_rate_for_range(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        """Get heart rate samples between start and end ISO timestamps."""
        records = self._read_json_list(self.hr_path)
        return [
            r for r in records
            if start_iso <= str(r.get("timestamp", "")) <= end_iso
        ]

    def get_latest_heart_rate(self) -> dict[str, Any] | None:
        records = self._read_json_list(self.hr_path)
        return records[-1] if records else None

    def get_daily_summary(self, date_str: str) -> DailyHealthSummary:
        """Compute aggregated health summary for date YYYY-MM-DD."""
        records = self._read_json_list(self.hr_path)
        day_hrs = [
            float(r.get("bpm", 0.0))
            for r in records
            if local_date(r.get("timestamp")) == date_str and float(r.get("bpm", 0.0)) > 30.0
        ]

        avg_hr = round(sum(day_hrs) / len(day_hrs), 1) if day_hrs else 0.0
        min_hr = round(min(day_hrs), 1) if day_hrs else 0.0
        max_hr = round(max(day_hrs), 1) if day_hrs else 0.0
        # 压力峰值统计：心率连续超过 100bpm 的次数
        stress_spikes = sum(1 for bpm in day_hrs if bpm >= 105.0)

        # 步数
        steps_records = self._read_json_list(self.steps_path)
        day_step = next(
            (int(r.get("steps", 0)) for r in steps_records if str(r.get("date", "")) == date_str),
            0,
        )

        # 睡眠
        sleep_records = self._read_json_list(self.sleep_path)
        sleep_entry = next(
            (r for r in sleep_records if str(r.get("date", "")) == date_str),
            None,
        )
        total_sleep = int(sleep_entry.get("total_minutes", 0)) if sleep_entry else 0
        deep_sleep = int(sleep_entry.get("deep_minutes", 0)) if sleep_entry else 0

        return DailyHealthSummary(
            date=date_str,
            avg_heart_rate=avg_hr,
            min_heart_rate=min_hr,
            max_heart_rate=max_hr,
            total_steps=day_step,
            sleep_minutes=total_sleep,
            deep_sleep_minutes=deep_sleep,
            stress_spikes_count=stress_spikes,
        )
