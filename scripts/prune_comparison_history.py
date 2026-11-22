#!/usr/bin/env python3
"""comparison-history 保留工具（**默认 dry-run**；`--apply` 必须带匹配的 `--plan-id`）。

背景：`output/local-web/watch-sessions/*/*/formal-asr/live-context/comparison-history/`
下每个 19 位时间戳目录是一份完整快照；实测 20 个会话共 8.77 GiB，且**全仓没有读者**，
相邻快照又几乎从不完全相同（去重无收益），因此只能按保留策略回收。

策略（`--policy`）：
  * `c`（默认，混合）：首份 + 最近 5 份 + 每小时最后一份；
  * `a`：首份 + 最近 10 份；
  * `a2`：首份 + 最近 5 份；
  * `b`（里程碑）：首份 + 最近 1 份 + 每天最后一份。

安全闸门：
  1. 默认 dry-run；`--apply` 必须带 `--plan-id`，与本次重算的 plan-id 完全一致，否则 exit 2 且零删除；
  2. 会话（或其中任一份快照）在 `--min-age-hours` 内有写入 → **整个会话跳过**；
  3. 只处理 `<root>/<watch-*>/<session>/formal-asr/live-context/comparison-history/<19位数字>/`；
     不 follow symlink、不碰别的路径；
  4. 删除后对**保留集**抽检 `json.loads`（每会话 ≥1 份，且总数 ≥ 保留数的 10%），失败进 errors；
  5. 每会话至少保留 1 份，且**首份必留**；
  6. 输出：kept / deleted / bytes_deleted / bytes_kept / skipped_active_sessions / errors / plan_id。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

SNAPSHOT_RE = re.compile(r"^\d{19}$")
COMPARISON_DIRNAME = "comparison-history"
POLICIES = {"c": "hybrid", "a": "last10", "a2": "last5", "b": "milestone"}
DEFAULT_ROOT = "output/local-web/watch-sessions"


def _is_snapshot(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and SNAPSHOT_RE.match(path.name) is not None


def snapshot_size(path: Path) -> int:
    """目录内普通文件字节数（不 follow symlink）。"""
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            f = Path(base) / name
            if f.is_symlink():
                continue
            try:
                total += f.stat().st_size
            except OSError:
                continue
    return total


def snapshot_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            try:
                newest = max(newest, (Path(base) / name).stat().st_mtime)
            except OSError:
                continue
    return newest


def discover(root: Path) -> list[tuple[Path, str]]:
    """返回 (comparison-history 目录, session_id)。只认固定层级，且不 follow symlink。"""
    found = []
    if not root.is_dir():
        return found
    for device in sorted(root.iterdir()):
        if not device.is_dir() or device.is_symlink():
            continue
        for session in sorted(device.iterdir()):
            if not session.is_dir() or session.is_symlink():
                continue
            ch = session / "formal-asr" / "live-context" / COMPARISON_DIRNAME
            if ch.is_dir() and not ch.is_symlink():
                found.append((ch, session.name))
    return found


def keep_indexes(snaps: list[Path], policy: str, keep_last: int) -> set[int]:
    n = len(snaps)
    keep = {0, n - 1}                       # 首份必留 + 最新一份必留（闸门 5）
    for idx in range(max(0, n - keep_last), n):
        keep.add(idx)
    if policy in {"c", "b"}:
        # 每小时/每天最后一份（用快照目录名里的纳秒时间戳分桶，稳定、不依赖 mtime）
        bucket = 3_600_000_000_000 if policy == "c" else 86_400_000_000_000
        seen: dict[int, int] = {}
        for idx, snap in enumerate(snaps):
            seen[int(snap.name) // bucket] = idx
        keep |= set(seen.values())
    return keep


def build_plan(root: Path, policy: str, min_age_hours: float) -> dict:
    now = time.time()
    sessions, targets, skipped, errors = [], [], [], []
    bytes_kept = bytes_deleted = kept = deleted = 0
    for ch, session_id in discover(root):
        snaps = [p for p in sorted(ch.iterdir(), key=lambda p: p.name) if _is_snapshot(p)]
        if not snaps:
            continue
        sizes = {p: snapshot_size(p) for p in snaps}
        newest = max(snapshot_mtime(p) for p in snaps)
        age_hours = (now - newest) / 3600.0
        if min_age_hours > 0 and age_hours < min_age_hours:
            skipped.append({"session_id": session_id, "age_hours": round(age_hours, 2),
                            "snapshots": len(snaps),
                            "bytes": sum(sizes.values())})
            continue
        keep_last = {"c": 5, "a": 10, "a2": 5, "b": 1}[policy]
        keep_idx = keep_indexes(snaps, policy, keep_last)
        keep_idx.add(len(snaps) - 1)
        to_delete = [(p, sizes[p]) for i, p in enumerate(snaps) if i not in keep_idx]
        kept += len(keep_idx)
        bytes_kept += sum(sizes[p] for p in snaps if p in {snaps[i] for i in keep_idx})
        deleted += len(to_delete)
        bytes_deleted += sum(sz for _p, sz in to_delete)
        targets.extend(p for p, _sz in to_delete)
        sessions.append({"session_id": session_id, "path": str(ch), "snapshots": len(snaps),
                         "keep": len(keep_idx), "delete": len(to_delete),
                         "bytes_total": sum(sizes.values()),
                         "bytes_kept": sum(sizes[p] for p in snaps if p in {snaps[i] for i in keep_idx}),
                         "bytes_delete": sum(sz for _p, sz in to_delete)})
    digest = hashlib.sha256(json.dumps({
        "policy": policy, "bytes_delete": bytes_deleted,
        "targets": sorted(str(p) for p in targets),
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {"policy": policy, "plan_id": digest, "sessions": sessions, "targets": targets,
            "kept": kept, "deleted": deleted, "bytes_kept": bytes_kept,
            "bytes_deleted": bytes_deleted, "skipped_active_sessions": skipped, "errors": errors}


def sample_kept(plan: dict, policy: str) -> list[str]:
    """删除后抽检保留集：每会话 ≥1 份、总数 ≥ 保留数 10%。"""
    problems: list[str] = []
    files_checked = 0
    session_count = 0
    for s in plan["sessions"]:
        ch = Path(s["path"])
        snaps = [p for p in sorted(ch.iterdir(), key=lambda p: p.name) if _is_snapshot(p)] if ch.is_dir() else []
        session_count += 1
        if not snaps:
            problems.append(f"{s['session_id']}: 保留集为空")
            continue
        files = [f for f in snaps[-1].rglob("*.json") if f.is_file()]
        if not files:
            continue
        target = files[0]
        try:
            json.loads(target.read_text(encoding="utf-8"))
            files_checked += 1
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{target}: {type(exc).__name__}: {exc}")
    need = max(session_count, int(plan["kept"] * 0.10) + 1)
    if files_checked < need:
        problems.append(f"抽检数量不足：{files_checked} < {need}（保留 {plan['kept']} 份）")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="comparison-history 保留工具（默认 dry-run）")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--policy", choices=sorted(POLICIES), default="c")
    ap.add_argument("--min-age-hours", type=float, default=0.0,
                    help="会话在这么多小时内仍有写入 → 整会话跳过（活跃保护）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--plan-id", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--log", default="")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    plan = build_plan(root, a.policy, a.min_age_hours)
    out = {"mode": "apply" if a.apply else "dry-run", "root": str(root),
           "policy": a.policy, "policy_name": POLICIES[a.policy],
           "plan_id": plan["plan_id"], "kept": plan["kept"], "deleted": plan["deleted"],
           "bytes_kept": plan["bytes_kept"], "bytes_deleted": plan["bytes_deleted"],
           "gib_deleted": round(plan["bytes_deleted"] / 1024 ** 3, 2),
           "skipped_active_sessions": plan["skipped_active_sessions"],
           "sessions": plan["sessions"], "errors": list(plan["errors"])}

    if a.apply:
        if not a.plan_id:
            print("拒绝执行：--apply 必须同时提供 --plan-id（先跑一次 dry-run 取 plan_id）", file=sys.stderr)
            return 2
        if a.plan_id != plan["plan_id"]:
            print(f"拒绝执行：plan-id 不匹配（给定 {a.plan_id}，本次重算 {plan['plan_id']}）→ 零删除",
                  file=sys.stderr)
            return 2
        removed = 0
        for target in plan["targets"]:
            parent = target.parent
            if target.is_symlink() or not _is_snapshot(target) or parent.name != COMPARISON_DIRNAME:
                out["errors"].append(f"跳过非法目标：{target}")
                continue
            try:
                shutil.rmtree(target)
                removed += 1
            except OSError as exc:
                out["errors"].append(f"{target}: {type(exc).__name__}: {exc}")
        out["removed"] = removed
        out["sample_problems"] = sample_kept(plan, a.policy)
        out["errors"].extend(out["sample_problems"])
    else:
        out["removed"] = 0

    text = json.dumps(out, ensure_ascii=False, indent=2)
    print(text)
    if a.json_out:
        Path(a.json_out).write_text(text, encoding="utf-8")
    if a.log:
        Path(a.log).write_text(text, encoding="utf-8")
    if not a.apply:
        print(f"\n[dry-run] 未删除任何文件。确认后执行：\n  --apply --plan-id {plan['plan_id']}", file=sys.stderr)
    return 0 if not out["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
