#!/usr/bin/env python3
"""台账自动同步：把 `main` 上「上次同步点之后」的提交补进 .planning 台账。

设计原则（P-21）：
1. **幂等**：同一提交绝不重复追加。同步点存 `.planning/.ledger-sync-state.json`；
   另外再做一层保险——如果某个 hash 已经出现在 `task_plan.md` 的版本流水里，也跳过。
2. **严禁编造**：只有能从 commit subject / diffstat 确定性推出来的列才自动填；
   需要人判断的列（验证结果 / 引入的新问题 / 下版目标）一律写 `待补（A0）`。
3. **可预览**：`--dry-run` 只打印将要追加的行，不写任何文件、不推进同步点。

用法：
    python3 scripts/ledger_sync.py --dry-run
    python3 scripts/ledger_sync.py
    python3 scripts/ledger_sync.py --base <commit>      # 手动指定起点（覆盖 state）
    python3 scripts/ledger_sync.py --reset-state         # 丢掉同步点，重新按 tag 引导
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".planning" / ".ledger-sync-state.json"
TASK_PLAN = ROOT / ".planning" / "life-logger" / "task_plan.md"
LOCK_FILE = ROOT / ".planning" / "life-logger" / ".ledger_sync.lock"


def atomic_write_text(path: Path, text: str) -> None:
    """唯一临时名 + fsync + os.replace：中途崩溃不会留下半截台账。

    【2026-10-29】原实现用固定 `<name>.tmp`：两个进程同时写同一台账会互相顶掉临时文件
    （后 replace 的一方 FileNotFoundError / 静默丢写）。优先走 life_logger.atomic；
    系统 python3 下没有该包时退回等价的唯一名实现，行为（raise）保持一致。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    try:
        from life_logger.atomic import write_bytes_atomic
    except Exception:            # 允许本脚本在裸 python3（无 life_logger）下独立运行
        write_bytes_atomic = None
    if write_bytes_atomic is not None:
        write_bytes_atomic(path, data)
        return
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextmanager
def ledger_lock():
    """进程间互斥锁（fcntl.flock），防两次同步/人工编辑同时改台账。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

PROGRESS = ROOT / ".planning" / "life-logger" / "progress.md"
SHANGHAI = timezone(timedelta(hours=8))

PENDING = "待补（A0）"

# 流域判定：按改动路径归属，跟 task_plan.md 顶部的流域表保持一致
DOMAIN_RULES: list[tuple[str, str]] = [
    ("watchos/", "[手表]"),
    ("ios/", "[手表]"),
    ("mobile/", "[手表]"),
    ("desktop/AirPassportReceiver/", "[接收器]"),
    ("web/", "[网页端]"),
    ("src/life_logger/", "[网页端]"),
    ("scripts/", "[网页端]"),
    (".planning/", "[网页端 / 台账]"),
]


def sh(args: list[str], check: bool = True) -> str:
    """跑一条命令，返回 stdout（去掉尾部换行）。失败按 check 决定是否抛出。"""
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def git(*args: str, check: bool = True) -> str:
    return sh(["git", *args], check=check)


def latest_canonical_tag() -> str | None:
    """最近一个 canonical tag（版本基准）。没有就返回 None。"""
    tags = git("tag", "--sort=-creatordate").splitlines()
    for t in tags:
        if t.endswith("-canonical"):
            return t.strip()
    return None


def resolve_base(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (base_commit, base_desc)。优先级：--base > state > 最近 canonical tag > 初始提交。"""
    if args.base:
        return git("rev-parse", args.base), f"手动指定 {args.base}"

    if STATE_FILE.is_file() and not args.reset_state:
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            commit = str(state.get("last_commit") or "").strip()
            if commit:
                git("rev-parse", "--verify", commit)  # 校验仍存在
                return commit, f"同步点 {commit[:7]}（{state.get('synced_at', '?')}）"
        except Exception:
            pass  # state 坏了就往下走，不阻塞

    tag = latest_canonical_tag()
    if tag:
        return git("rev-list", "-n", "1", tag), f"tag {tag}"
    return git("rev-list", "--max-parents=0", "HEAD").splitlines()[0], "初始提交"


def commits_since(base: str) -> list[dict]:
    """base..HEAD 的提交（base 本身不含）。新的在前。"""
    raw = git(
        "log", f"{base}..HEAD",
        "--pretty=format:%H%x1f%h%x1f%ad%x1f%s%x1f%b%x1e",
        "--date=format:%Y-%m-%d %H:%M",
    )
    out: list[dict] = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 4:
            continue
        full, short, date, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        out.append({
            "hash": full,
            "short": short,
            "date": date,
            "subject": subject,
            "body": body,
        })
    return out


def diffstat(commit: str) -> dict:
    """改动概览：文件数 / 增删行数 / 主要目录。"""
    numstat = git("show", "--numstat", "--format=", commit)
    files: list[str] = []
    added = deleted = 0
    for line in numstat.splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        a, d, path = cols[0], cols[1], cols[2]
        files.append(path)
        try:
            added += int(a)
        except ValueError:
            pass
        try:
            deleted += int(d)
        except ValueError:
            pass

    dirs: dict[str, int] = {}
    for f in files:
        top = f.split("/")[0] + ("/" if "/" in f else "")
        dirs[top] = dirs.get(top, 0) + 1
    top_dirs = sorted(dirs.items(), key=lambda kv: -kv[1])[:3]

    return {
        "file_count": len(files),
        "added": added,
        "deleted": deleted,
        "files": files,
        "top_dirs": top_dirs,
    }


def domain_of(files: list[str]) -> str:
    """按路径命中最多的一条规则判流域。"""
    score: dict[str, int] = {}
    for f in files:
        for prefix, label in DOMAIN_RULES:
            if f.startswith(prefix):
                score[label] = score.get(label, 0) + 1
                break
    if not score:
        return "[未知流域]"
    return sorted(score.items(), key=lambda kv: -kv[1])[0][0]


def cell(text: str) -> str:
    """Markdown 表格单元格转义：竖线换掉、换行压平。"""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def describe_changes(info: dict) -> str:
    """把 diffstat 翻译成一句人话（只描述事实，不做价值判断）。"""
    if info["file_count"] == 0:
        return "无文件改动（可能是合并提交或空提交）"
    dirs = "、".join(f"`{d}` {n} 文件" for d, n in info["top_dirs"])
    return (
        f"改动 {info['file_count']} 个文件（+{info['added']}/-{info['deleted']} 行）；"
        f"集中在 {dirs}"
    )


def already_recorded(short: str, plan_text: str) -> bool:
    """版本流水里是否已经有这个 hash（防止历史手工条目被重复追加）。"""
    return re.search(rf"`?{re.escape(short)}`?", plan_text) is not None


def build_row(commit: dict, info: dict) -> str:
    return (
        f"| `{commit['short']}` | {domain_of(info['files'])} "
        f"| {cell(commit['subject'])} "
        f"| {cell(describe_changes(info))} "
        f"| {PENDING} | {PENDING} | {PENDING} |"
    )


def append_rows_to_table(text: str, rows: list[str]) -> str:
    """把新行插到版本流水表格的最后一行之后。"""
    lines = text.split("\n")
    header_idx = None
    for i, line in enumerate(lines):
        if "版本（hash / sha256）" in line:
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("task_plan.md 里找不到「版本流水」表头")

    last_row = header_idx
    for j in range(header_idx + 1, len(lines)):
        if lines[j].lstrip().startswith("|"):
            last_row = j
        else:
            break

    return "\n".join(lines[: last_row + 1] + rows + lines[last_row + 1:])


def already_recorded_row(row: str, plan_text: str) -> bool:
    """这一行是否已在台账里（按行内 7 位 hash 判定），用于锁内二次校验。"""
    hashes = re.findall(r"\b[0-9a-f]{7,40}\b", row)
    return any(h in plan_text for h in hashes) if hashes else row in plan_text


def update_baseline(text: str, head_short: str, tag: str | None, pending_count: int) -> str:
    """更新「当前基准」块里的基准实体行与下一步行。

    说明：**已验证可用 / 已知坏项** 不自动改写——那两处需要人判断，
    自动填等于编造。这里只写能确定性推出来的两条。
    """
    tag_desc = f"tag `{tag}`" if tag else "暂无 canonical tag"
    bullets = [
        ("基准实体", f"- **基准实体**：{tag_desc} / commit `{head_short}`（{datetime.now(SHANGHAI).strftime('%Y-%m-%d')} 自动同步）"),
        ("下一步", f"- **下一步**：本次同步纳入 {pending_count} 个提交，验收结论待 A0 补齐（见版本流水 `{PENDING}` 列）"),
    ]
    # 只改「当前基准」这一节的**顶层条目**（H2 之后、第一个 H3 之前），
    # 绝不碰下面按日期归档的历史块——那些是过去某次交付的事实陈述，
    # 拿今天的 HEAD 去覆盖它们等于篡改历史。
    marker = re.search(r"^## 当前基准.*$", text, re.MULTILINE)
    if not marker:
        return text
    region_start = marker.end()
    tail = text[region_start:]
    # 【2026-10-22 P-33】原来的截止只找下一个 `### `，若「当前基准」后面**先出现下一个 H2、
    # 中间没有 H3**，region 就会跨过 H2 一直吃到后面的历史基准块（263911d 被覆盖过）。
    # 现在取「下一个 H3」与「下一个 H2」中更早的那个作为边界，绝不跨节。
    next_h3 = re.search(r"^### ", tail, re.MULTILINE)
    next_h2 = re.search(r"^## ", tail, re.MULTILINE)
    boundaries = [m.start() for m in (next_h3, next_h2) if m is not None]
    region_end = region_start + (min(boundaries) if boundaries else len(tail))
    region = text[region_start:region_end]
    outside = text[region_end:]

    for key, newline in bullets:
        pattern = re.compile(rf"^- \*\*{key}\*\*：.*$", re.MULTILINE)
        if pattern.search(region):
            region = pattern.sub(newline, region, count=1)
        elif pattern.search(outside):
            # 字段只在基准块之外出现：说明结构变了，拒绝写，而不是把历史基准覆盖掉
            raise RuntimeError(
                f"「{key}」字段出现在「当前基准」块之外，拒绝写入（可能是台账结构被改动）"
            )
        else:
            region = region.rstrip("\n") + "\n" + newline + "\n"

    return text[:region_start] + region + outside


def append_progress(n: int, base_short: str, head_short: str, skipped: list[str]) -> str:
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M")
    line = (
        f"- {today} · 同步 {n} 个提交 · 区间 `{base_short}`..`{head_short}`"
        f"（脚本 `scripts/ledger_sync.py` 自动生成；跳过已在流水中的 {len(skipped)} 个）"
    )
    return line


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="台账自动同步（幂等，不编造）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写什么，不落盘")
    ap.add_argument("--base", default=None, help="手动指定同步起点（覆盖 state 文件）")
    ap.add_argument("--reset-state", action="store_true", help="忽略已有同步点，按 tag 重新引导")
    args = ap.parse_args(argv)

    head_full = git("rev-parse", "HEAD")
    head_short = head_full[:7]
    base_full, base_desc = resolve_base(args)
    base_short = base_full[:7]

    if base_full == head_full:
        print(f"[ledger_sync] 无新提交（同步点已是 HEAD {head_short}，起点来源：{base_desc}）")
        return 0

    commits = commits_since(base_full)
    if not commits:
        print(f"[ledger_sync] 无新提交（{base_short}..{head_short} 区间为空，起点来源：{base_desc}）")
        return 0

    plan_text = TASK_PLAN.read_text(encoding="utf-8")
    rows: list[str] = []
    skipped: list[str] = []
    for c in reversed(commits):  # 按时间正序追加
        if already_recorded(c["short"], plan_text):
            skipped.append(c["short"])
            continue
        rows.append(build_row(c, diffstat(c["hash"])))

    print(f"[ledger_sync] 起点：{base_desc}（{base_short}）  →  HEAD {head_short}")
    print(f"[ledger_sync] 区间内提交 {len(commits)} 个：待写入 {len(rows)} 行，已存在于流水 {len(skipped)} 个")
    if skipped:
        print(f"[ledger_sync] 跳过（已在台账里）：{', '.join(skipped)}")
    if rows:
        print("[ledger_sync] 将要追加的 3 行示例：")
        for r in rows[:3]:
            print("  " + r)
        if len(rows) > 3:
            print(f"  … 其余 {len(rows) - 3} 行同格式")

    progress_line = append_progress(len(rows), base_short, head_short, skipped)

    if args.dry_run:
        print("[ledger_sync] --dry-run：未修改任何文件，未推进同步点")
        print("[ledger_sync] 将追加到 progress.md：")
        print("  " + progress_line)
        return 0

    # 【2026-10-22 P-33】整段读-改-写放在文件锁内，并**重新读一次**台账：
    # 旧实现用函数开头读到的 plan_text 全文覆写，一旦中间有人/另一个进程改过，
    # 那些改动会被旧快照吞掉（并发或人工编辑直接丢历史）。
    with ledger_lock():
        fresh_plan = TASK_PLAN.read_text(encoding="utf-8")
        # 锁内二次校验：同一 hash 已经写进去就跳过（幂等保险）
        rows = [row for row in rows if not already_recorded_row(row, fresh_plan)]
        if not rows:
            print("[ledger_sync] 没有需要新增的行；只推进同步点")
        else:
            new_text = append_rows_to_table(fresh_plan, rows)
            tag = latest_canonical_tag()
            new_text = update_baseline(new_text, head_short, tag, len(rows))
            atomic_write_text(TASK_PLAN, new_text)

        # progress 追加同样改为「读-去重-原子写」，不裸 append
        progress_text = PROGRESS.read_text(encoding="utf-8") if PROGRESS.is_file() else ""
        if progress_line.strip() and progress_line.strip() not in progress_text:
            atomic_write_text(
                PROGRESS,
                progress_text.rstrip("\n") + "\n" + progress_line + "\n",
            )

        atomic_write_text(STATE_FILE, json.dumps({
            "last_commit": head_full,
            "last_commit_short": head_short,
            "synced_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "from": base_full,
            "from_short": base_short,
            "commits_synced": len(rows),
            "commits_skipped": skipped,
        }, ensure_ascii=False, indent=2) + "\n")

    print(f"[ledger_sync] 已写入 {len(rows)} 行到 task_plan.md；同步点推进为 {head_short}")
    try:
        state_display = STATE_FILE.relative_to(ROOT)
    except ValueError:  # 测试/临时目录场景下 state 可能不在仓库内
        state_display = STATE_FILE
    print(f"[ledger_sync] state：{state_display}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
