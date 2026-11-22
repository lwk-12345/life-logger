#!/usr/bin/env python3
"""手表录音滚动备份：只保留最近 N 天（默认 30），空间快不够时提前通知。

用户决定（2026-11-04）：录音只备份一个月，装不下要提前通知。

- 录音已是 16 kHz / 32 kbps AAC，再压缩省不了多少，所以不转码，只做复制。
- 目标与源在同一磁盘时用 APFS 克隆（cp -c），几乎不占空间，能防误删和程序写坏，
  但防不了硬盘损坏。设 LL_AUDIO_BACKUP_DIR 指向外接硬盘后自动变成真实复制。
- 平时只删备份目录里超期的副本，不碰源目录。
- 唯一例外是空间兜底（用户 2026-11-04 决定）：可用空间跌破 --floor-gb（默认 5GB）时，
  删最旧的原始录音，一次一周，最多两周。这一步不可恢复，所以只删 30 天前且已有转写结果的
  音频，转写和事件文件不动；没有可删的就只通知。
- 只用标准库：launchd 下的 python3 不保证有项目依赖。

用法：
  scripts/backup_audio.py                # 备份、清理超期副本、检查空间
  scripts/backup_audio.py --dry-run      # 只打印计划，不写不删不通知
退出码：0 成功；1 有文件复制失败。空间不足只通知，不算失败。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

AUDIO_SUFFIXES = {".m4a"}
FINISHED_STATUSES = {"completed", "no_speech"}
DAY = 86400
GIB = 2**30


def iter_audio(root: Path):
    for dirpath, _, names in os.walk(root):
        for name in names:
            if Path(name).suffix.lower() in AUDIO_SUFFIXES:
                path = Path(dirpath) / name
                try:
                    yield path, path.stat()
                except OSError:
                    continue


def clone_or_copy(src: Path, dst: Path, same_volume: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.unlink(missing_ok=True)
    try:
        if same_volume:
            subprocess.run(["cp", "-c", "-p", str(src), str(tmp)], check=True, capture_output=True)
        else:
            shutil.copy2(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(f"副本大小不一致：{src}")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def recent_growth_per_day(data_root: Path, now: float, days: int = 7) -> float:
    """最近 days 天 output/local-web 新增或改写的字节数，折算成每天。"""
    total = 0
    cutoff = now - days * DAY
    for dirpath, _, names in os.walk(data_root):
        for name in names:
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                total += st.st_size
    return total / days


def nearest_existing(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def space_warning(free: int, growth_per_day: float, warn_days: int, min_free_gb: float) -> tuple[str | None, float | None]:
    days_left = free / growth_per_day if growth_per_day > 0 else None
    free_gb = free / GIB
    if free_gb < min_free_gb:
        return f"只剩 {free_gb:.1f} GB，低于 {min_free_gb:g} GB", days_left
    if days_left is not None and days_left < warn_days:
        return f"只剩 {free_gb:.1f} GB，按最近增速约 {days_left:.0f} 天后用完", days_left
    return None, days_left


def send_notification(root: Path, title: str, body: str) -> dict:
    """本机通知 + 微信，两条都试，结果如实写进状态文件。"""
    result = {}
    script = f'display notification {json.dumps(body, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}'
    try:
        local = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        result["mac"] = "sent" if local.returncode == 0 else "failed"
    except (OSError, subprocess.TimeoutExpired):
        result["mac"] = "failed"
    notifier = root / "scripts" / "notify.sh"
    try:
        wechat = subprocess.run([str(notifier), "--json", title, body], capture_output=True, timeout=30)
        result["wechat"] = {0: "sent", 3: "skipped"}.get(wechat.returncode, "failed")
    except (OSError, subprocess.TimeoutExpired):
        result["wechat"] = "failed"
    return result


def _segment_finished(path: Path) -> bool:
    """这段录音的转写是否已结束；读不到状态一律当没结束。"""
    state = path.parent.parent / "formal-asr" / "segments" / f"{int(path.stem.split('-')[-1]):06d}" / "state.json"
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("status") in FINISHED_STATUSES
    except (OSError, ValueError, AttributeError):
        return False


def _session_job_transcribed(session: Path) -> bool:
    """旧流程的录音由整场 job 转写：job 已完成且结果里确有转写文件才算。"""
    try:
        job_id = json.loads((session / "manifest.json").read_text(encoding="utf-8")).get("job_id")
    except (OSError, ValueError, AttributeError):
        return False
    if not job_id:
        return False
    job = session.parents[2] / "jobs" / str(job_id)
    try:
        if json.loads((job / "status.json").read_text(encoding="utf-8")).get("status") != "completed":
            return False
    except (OSError, ValueError, AttributeError):
        return False
    return any(True for _ in job.glob("files/*/result/**/*transcript*"))


def _removable_audio(source: Path, cutoff: float) -> tuple[list[tuple[float, int, Path]], int]:
    """返回 (可删音频, 因无转写结果被保留的数量)。"""
    removable: list[tuple[float, int, Path]] = []
    merged: list[tuple[float, int, Path]] = []
    unfinished_sessions: set[Path] = set()
    skipped = 0
    for path, st in iter_audio(source):
        if path.parent.name == "segments" and path.stem.startswith("segment-"):
            try:
                finished = _segment_finished(path)
            except ValueError:
                finished = False
            finished = finished or _session_job_transcribed(path.parent.parent)
            if not finished:
                unfinished_sessions.add(path.parent.parent)
                if st.st_mtime < cutoff:
                    skipped += 1
                continue
            if st.st_mtime < cutoff:
                removable.append((st.st_mtime, st.st_size, path))
        elif st.st_mtime < cutoff:
            merged.append((st.st_mtime, st.st_size, path))
    for item in merged:
        # 合并文件是分段的拼接；分段还没转写完时它可能是唯一可用的副本。
        if item[2].parent in unfinished_sessions or not (item[2].parent / "segments").is_dir():
            skipped += 1
        else:
            removable.append(item)
    removable.sort(key=lambda item: item[0])
    return removable, skipped


def reclaim_oldest_audio(source: Path, *, now: float, floor_gb: float, protect_days: int, free_bytes, dry_run: bool = False, max_weeks: int = 2) -> dict:
    result = {"removed": 0, "removed_bytes": 0, "weeks": 0, "skipped_no_transcript": 0, "blocked": False, "oldest": None, "newest": None}
    floor = floor_gb * GIB
    if free_bytes() >= floor:
        return result
    removable, result["skipped_no_transcript"] = _removable_audio(source, now - protect_days * DAY)
    position = 0
    while position < len(removable) and result["weeks"] < max_weeks:
        week_end = removable[position][0] + 7 * DAY
        while position < len(removable) and removable[position][0] < week_end:
            stamp, size, path = removable[position]
            if not dry_run:
                path.unlink(missing_ok=True)
            result["removed"] += 1
            result["removed_bytes"] += size
            result["oldest"] = result["oldest"] or stamp
            result["newest"] = stamp
            position += 1
        result["weeks"] += 1
        if free_bytes() >= floor:
            return result
    result["blocked"] = free_bytes() < floor and position >= len(removable)
    return result


def run(root: Path, dest: Path, days: int, warn_days: int, min_free_gb: float, dry_run: bool, notify: bool, now: float | None = None, floor_gb: float = 5.0) -> dict:
    now = time.time() if now is None else now
    data_root = root / "output" / "local-web"
    source = data_root / "watch-sessions"
    backup_root = dest / "watch-sessions"
    cutoff = now - days * DAY
    if dest.resolve() == source.resolve() or source.resolve() in dest.resolve().parents:
        raise ValueError("备份目录不能在录音源目录内")

    probe = nearest_existing(dest)
    same_volume = probe.stat().st_dev == source.stat().st_dev
    wanted = copied = skipped = bytes_copied = 0
    failures: list[str] = []
    out_of_space = False
    candidates = sorted(((p, s) for p, s in iter_audio(source) if s.st_mtime >= cutoff), key=lambda x: -x[1].st_mtime)
    for path, st in candidates:
        wanted += 1
        target = backup_root / path.relative_to(source)
        try:
            if target.exists() and target.stat().st_size == st.st_size:
                skipped += 1
                continue
        except OSError:
            pass
        if dry_run:
            copied += 1
            bytes_copied += st.st_size
            continue
        # 外接盘放不下时从最新的开始保，放不下的如实记为未备份并通知。
        if not same_volume and shutil.disk_usage(probe).free < st.st_size:
            out_of_space = True
            failures.append(str(path))
            continue
        try:
            clone_or_copy(path, target, same_volume)
            copied += 1
            bytes_copied += st.st_size
        except (OSError, subprocess.CalledProcessError) as exc:
            failures.append(f"{path}: {exc}")

    pruned = pruned_bytes = 0
    if backup_root.exists():
        for path, st in list(iter_audio(backup_root)):
            if st.st_mtime < cutoff:
                pruned += 1
                pruned_bytes += st.st_size
                if not dry_run:
                    path.unlink(missing_ok=True)
        if not dry_run:
            for dirpath, dirnames, filenames in os.walk(backup_root, topdown=False):
                if Path(dirpath) != backup_root and not dirnames and not filenames:
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass

    reclaim = reclaim_oldest_audio(
        source, now=now, floor_gb=floor_gb, protect_days=days,
        free_bytes=lambda: shutil.disk_usage(source).free, dry_run=dry_run,
    )
    free = shutil.disk_usage(probe).free
    growth = recent_growth_per_day(data_root, now)
    warning, days_left = space_warning(free, growth, warn_days, min_free_gb)
    if out_of_space:
        warning = f"备份盘放不下，有 {len(failures)} 个录音没有备份；{warning or ''}".rstrip("；")
    if reclaim["removed"]:
        span = "至".join(time.strftime("%m-%d", time.gmtime(t + 8 * 3600)) for t in (reclaim["oldest"], reclaim["newest"]))
        warning = f"磁盘不足，已删除 {span} 的 {reclaim['removed']} 个旧录音（{reclaim['removed_bytes'] / GIB:.1f} GB），转写保留；{warning or ''}".rstrip("；")
    elif reclaim["blocked"]:
        warning = f"磁盘低于 {floor_gb:g} GB，但没有 {days} 天前且已转写的录音可删，请手动处理；{warning or ''}".rstrip("；")
    notified = None
    if warning and notify and not dry_run:
        notified = send_notification(root, "Life Logger 磁盘空间提醒", warning)

    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 8 * 3600)) + "+08:00",
        "dest": str(dest),
        "mode": "apfs_clone_same_disk" if same_volume else "real_copy_other_disk",
        "protects_against_disk_failure": not same_volume,
        "retention_days": days,
        "wanted": wanted,
        "copied": copied,
        "already_backed_up": skipped,
        "bytes_copied": bytes_copied,
        "pruned": pruned,
        "pruned_bytes": pruned_bytes,
        "failures": failures[:50],
        "failure_count": len(failures),
        "free_bytes": free,
        "growth_bytes_per_day": round(growth),
        "days_left": None if days_left is None else round(days_left, 1),
        "reclaim": reclaim,
        "warning": warning,
        "notified": notified,
        "dry_run": dry_run,
    }
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        status = dest / "status.json"
        tmp = status.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, status)
    return report


def main() -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--dest", type=Path, default=None)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--warn-days", type=int, default=14)
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("--floor-gb", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    dest = args.dest or Path(os.environ.get("LL_AUDIO_BACKUP_DIR") or args.root / "backups" / "audio-30d")
    report = run(args.root, dest, args.days, args.warn_days, args.min_free_gb, args.dry_run, not args.no_notify, floor_gb=args.floor_gb)
    print(json.dumps({k: v for k, v in report.items() if k != "failures"}, ensure_ascii=False))
    return 1 if report["failure_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
