#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/dispatch_engine.py —— 调度层：队列驱动 + 单写者约束 + 自动派下一单。

职责边界（见 work/automation-20261023/DESIGN.md 2.3）：
  * **只管派单，不管判定**——判定权在取证层（verify_claim.py）与门禁层（gate_runner.py）。
  * 读 agent-board/queue.json + `dispatch.sh list`（复用，不重写）+ 仓库 git 实际改动。
  * 派单四条规则按顺序短路，任一不满足就 refused 并写明原因；**无法决定就不派**。

派单规则（顺序即短路顺序，reason 见 REASONS）：

  1. state 必须是 todo；blocked_user 永不自动派，只在汇报里列出。
  2. dependencies 全部 completed —— 依赖 id 不存在判 DEP_MISSING，绝不当作满足。
  3. 目标窗口必须 idle（来自 dispatch.sh list）。
  4. 单写者：write_scope ∪ observed_write 与任何 state∈{running,verifying} 任务的同一
     并集有交集 → 拒绝，输出冲突任务 id + 冲突文件。write_scope 为空 → 拒绝
     （NO_WRITE_SCOPE：声明为空就无法证明单写者，宁可不派）。

闭环（这条才是价值所在）：

    派单 → 窗口回报 → verify_claim.py 取证
              ├─ exit 0 全 PASS      → completed
              ├─ exit 1 有 FAIL      → todo + 「上次未通过：<理由>」+ attempts++
              ├─ exit 2 UNVERIFIABLE → verifying，进「要人拍板」清单（不消耗 attempts）
              └─ exit 3 BLOCKED_ENV  → 保持 running，下一轮重试（不消耗 attempts）

四条硬规矩（都是今晚踩过的坑）：

  * **调 dispatch.sh 必须走 zsh / 直接执行该文件，绝不许 `bash dispatch.sh`。**
    实测 `bash agent-board/dispatch.sh list` → stdout 空 + 退出码 0，会被误读成
    「没有窗口在线」然后静默死锁。这里额外校验 stdout 可解析且认得 6 个窗号，
    否则判 LIST_FAIL → 本轮一单都不派（不是「全部空闲」）。
  * **默认 dry-run。** 不加 `--live` 时不向任何窗口投递，只把决策写进 dispatch-log。
    自动派单一旦真的往 6 个窗口里塞活，就是不可回滚的副作用，必须显式开。
  * **收割必须相对于派单基线。** `readall.py` 取的是「最近一条非代码文本」，与派单时间
    无关——它可能拿到派单之前那条自述，再交给取证器，就变成用旧话证明新活。所以派单时
    记 `baseline_offset`（piwatch.py offset），收割用 `piwatch.py tail <sid> <offset>`；
    缺基线才退回 readall，并在证据里标 harvest_source=readall_fallback 作降级告警。
  * **写 queue.json 用临时文件 + os.replace；同一时刻只允许一个引擎实例（flock）。**
    手动跑一次 + launchd 跑一次会互相覆盖，queue.json 是唯一台账。

退出码：0 正常（含「无事可派」）/ 1 有单被拒绝或取证未通过 / 2 环境不可用（LIST_FAIL 等）
/ 3 引擎自身异常。**注意：0 不等于派成了单**，派没派看 stdout 的 dispatched。

用法：
  python3 scripts/dispatch_engine.py --once
  python3 scripts/dispatch_engine.py --once --live
  python3 scripts/dispatch_engine.py --watch --interval 300 --live
  python3 scripts/dispatch_engine.py --once --board-dir "<看板目录>" --dry-run
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

SCHEMA = "dispatch-engine-v1"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENV = 2
EXIT_ENGINE = 3

# 本引擎把这两条历史状态名当作 todo 处理（旧台账用 queued，新 schema 用 todo）。
TODO_STATES = {"todo", "queued"}
# 依赖判定：新 schema 用 completed，旧台账用 done。两者都算「已结」。
COMPLETED_STATES = {"completed", "done"}
# 占着文件的状态：running 在改，verifying 在等判定但文件仍归它。
# 占着文件的状态：running 在改，verifying 在等判定，delivered 已自述完成但**还没判定**。
# delivered 必须算占着——窗口说「我做完了」不等于「交付被接受」，取证随时可能判 FAIL，
# 那时这批文件还得归它重做。若此时把同一文件派给另一单，就是自己制造撞车。
SCOPE_HOLDING_STATES = {"running", "verifying", "delivered"}
# 已经报了活、等取证的状态（delivered 是旧台账 harvest 后的状态名）。
REPORTED_STATES = {"running", "delivered", "verifying"}

REASONS = {
    "DISPATCH": "可派",
    "DISPATCH_DRY_RUN": "可派（dry-run：未真投递）",
    "BLOCKED_USER": "state=blocked_user：等用户拍板，永不自动派",
    "WRONG_STATE": "state 不属 todo，本轮不处理",
    "NO_OWNER": "没有指定窗口（owner 为空/-），不自动派",
    "NO_WRITE_SCOPE": "write_scope 为空：无法证明单写者，宁可不派",
    "DEP_MISSING": "依赖的任务 id 在台账里不存在",
    "DEP_UNMET": "依赖未 completed",
    "WINDOW_UNKNOWN": "窗号不在 targets.json 里",
    "WINDOW_NOT_IDLE": "目标窗口不 idle",
    "WINDOW_OCCUPIED": "目标窗口还有另一单未结（一窗同时只一单）",
    "SCOPE_CONFLICT": "单写者冲突：write_scope 与其他在跑任务有交集",
    "ALREADY_DISPATCHED": "同一单在冷却窗口内刚派过，防重复派",
    "STALLED": "派出后超时未回报，标 stalled 且不自动重派（重派会造成两窗改同一文件）",
    "NO_ELIGIBLE_WINDOW": "本轮无可用窗口",
    "VERIFY_PASS": "取证全 PASS",
    "VERIFY_FAIL": "取证有 FAIL",
    "VERIFY_UNVERIFIABLE": "取证判不了（需人拍板）",
    "VERIFY_BLOCKED_ENV": "取证环境不可用（下一轮重试）",
    "NO_REPORT": "窗口还没回报",
    "ALREADY_VERIFIED": "同一份回报已取证过，跳过（幂等）",
}


# --------------------------------------------------------------------------- #
# 台账读写
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def now_ts() -> float:
    return time.time()


def parse_ts(value) -> float | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(str(value), fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


class Board:
    """queue.json 的读写门面：原子写 + 结构归一。

    结构归一很要紧：真实台账里 16 项任务用的字段是 `scope`，而 DESIGN 2.3 定的新
    schema 是 `write_scope`。引擎两种都认（write_scope 优先），但**只在需要时**把
    新字段落盘——不对整份台账做无条件重写，避免把别人正在写的文件搅乱。
    """

    def __init__(self, board_dir: Path):
        self.dir = Path(board_dir)
        self.queue_path = self.dir / "queue.json"
        self.log_path = self.dir / "dispatch-log.jsonl"

    def load(self) -> dict:
        with self.queue_path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
            raise ValueError(f"{self.queue_path} 结构不对：缺 tasks 数组")
        return doc

    def save(self, doc: dict) -> None:
        doc["updated"] = now_iso()
        tmp = self.queue_path.with_suffix(".json.tmp-%d" % os.getpid())
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.queue_path)

    def log(self, record: dict) -> None:
        record.setdefault("ts", now_iso())
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# 任务字段归一
# --------------------------------------------------------------------------- #

def norm_paths(values) -> list[str]:
    out = []
    for item in values or []:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        # 统一成仓库相对路径的味道：去掉前导 ./ 与重复斜杠，保留结尾 /（目录语义）
        while text.startswith("./"):
            text = text[2:]
        text = text.replace("//", "/")
        if text not in out:
            out.append(text)
    return out


def write_scope(task: dict) -> list[str]:
    """声明的写入范围。新字段 write_scope 优先，回落到旧字段 scope。"""
    if "write_scope" in task and task["write_scope"] is not None:
        return norm_paths(task.get("write_scope"))
    return norm_paths(task.get("scope"))


def observed_write(task: dict) -> list[str]:
    return norm_paths(task.get("observed_write"))


def dependencies(task: dict) -> list[str]:
    deps = task.get("dependencies")
    if deps is None:
        deps = task.get("depends_on")
    if isinstance(deps, str):
        deps = [deps]
    return [str(d) for d in (deps or []) if str(d).strip()]


def effective_scope(task: dict) -> list[str]:
    """单写者判定用的并集：声明 ∪ 实际。**必须带实际**，因为今晚的撞车正是
    「声明说没碰、实际在碰」——只用声明等于相信撒谎的人自证清白。"""
    merged = list(write_scope(task))
    for p in observed_write(task):
        if p not in merged:
            merged.append(p)
    return merged


def scope_overlap(a: list[str], b: list[str]) -> list[str]:
    """路径级交集。支持「目录前缀」语义：`work/x/` 与 `work/x/f.md` 算相交。"""
    hits: list[str] = []
    for left in a:
        for right in b:
            if left == right:
                hits.append(left)
                continue
            if left.endswith("/") and right.startswith(left):
                hits.append(left + "*")
                continue
            if right.endswith("/") and left.startswith(right):
                hits.append(right + "*")
                continue
            # 同目录同文件但写法不同（a/b 与 ./a/b）已在上一步归一；这里不再猜
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def task_state(task: dict) -> str:
    return str(task.get("state") or "").strip().lower()


def task_id(task: dict) -> str:
    return str(task.get("id") or "").strip()


# --------------------------------------------------------------------------- #
# 窗口状态：复用 dispatch.sh list（必须 zsh）
# --------------------------------------------------------------------------- #

class WindowTable:
    """targets.json 是「窗号 ↔ session id」的唯一真源，这里读它，不写死常量。"""

    def __init__(self, board_dir: Path | None = None):
        self.by_label: dict[str, str] = {}   # "1" -> sid
        self.label_of: dict[str, str] = {}   # sid -> "①1号"
        self.short_of: dict[str, str] = {}   # sid[:8] -> "1"
        base = Path(board_dir) if board_dir else Path(__file__).resolve().parent.parent / "agent-board"
        path = base / "targets.json"
        self.source = str(path)
        if not path.exists():
            return
        doc = json.loads(path.read_text(encoding="utf-8"))
        for key, val in doc.items():
            if not key.startswith("①") and len(key) != 1:
                continue
            if not isinstance(val, dict) or not val.get("id"):
                continue
            label = str(val.get("label") or key)
            num = StrNum(label)
            sid = str(val["id"])
            if num:
                self.by_label[num] = sid
                self.short_of[sid[:8]] = num
            self.label_of[sid] = label

    def sid_for(self, window: str) -> str | None:
        key = str(window).strip()
        if key in self.by_label:
            return self.by_label[key]
        if key in self.label_of:
            return key
        return None

    def label_for(self, window: str) -> str:
        sid = self.sid_for(window)
        if sid and sid in self.label_of:
            return self.label_of[sid]
        return f"窗{window}"


def StrNum(label: str) -> str:
    """从 '③3号' / '3号' / '3' 里取出纯数字窗号。

    必须**只认 ASCII 数字**：Python 的 `str.isdigit()` 对带圈数字 `①` 也返回 True，
    于是 '①1号' 会被取成 '①'，之后和 targets.json / dispatch.sh 里的 '1' 永远对不上，
    表现为「6 个窗口一个都认不出来」。targets.json 的键是 ①..⑥ 而 label 是 '1号'..'6号'，
    两种写法都得能吃。
    """
    for ch in str(label):
        if ch in "0123456789":
            return ch
    return ""


class StatusSource:
    """窗口忙闲。默认走真 dispatch.sh；`--status-file` 允许用夹具替换（供验收/回归）。"""

    def __init__(self, dispatch_sh: Path | None, windows: WindowTable, status_file: Path | None = None):
        self.dispatch = dispatch_sh
        self.windows = windows
        self.status_file = status_file
        self.last_error: str | None = None
        self.last_source = ""

    def fetch(self) -> tuple[dict[str, str] | None, str]:
        """返回 ({窗号: status}, source)。None 表示「读不到」——绝不当成全空闲。"""
        if self.status_file is not None:
            self.last_source = f"status_file:{self.status_file}"
            try:
                doc = json.loads(Path(self.status_file).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.last_error = f"status-file 不可读/不可解析：{exc}"
                return None, self.last_source
            return self._parse(doc, require_all=False)
        if self.dispatch is None or not Path(self.dispatch).exists():
            self.last_error = "dispatch.sh 不存在"
            return None, "missing"
        argv = self._argv()
        self.last_source = " ".join(argv)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_error = f"dispatch.sh list 无法执行：{exc}"
            return None, self.last_source
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if not out:
            self.last_error = (
                f"dispatch.sh list 输出为空（rc={proc.returncode}）"
                + (f"；stderr={err[:200]}" if err else "")
                + "。这是 bash 调用 zsh 脚本的典型症状，绝不能当成「没有窗口在线」"
            )
            return None, self.last_source
        try:
            doc = json.loads(out)
        except json.JSONDecodeError as exc:
            self.last_error = f"dispatch.sh list 输出不是 JSON：{exc}；head={out[:200]}"
            return None, self.last_source
        return self._parse(doc, require_all=True)

    def _argv(self) -> list[str]:
        path = Path(self.dispatch)
        shebang = ""
        try:
            with path.open("rb") as fh:
                shebang = fh.readline().decode("utf-8", "replace")
        except OSError:
            pass
        if os.access(path, os.X_OK) and "zsh" in shebang:
            return [str(path), "list"]      # 靠 shebang 走 zsh
        return ["zsh", str(path), "list"]   # 显式 zsh，永不 bash

    def _parse(self, doc, require_all: bool) -> tuple[dict[str, str] | None, str]:
        sessions = doc.get("sessions") if isinstance(doc, dict) else None
        if not isinstance(sessions, list) or not sessions:
            self.last_error = "list 里没有 sessions 数组或为空 —— 判 BLOCKED_ENV，不当成全空闲"
            return None, self.last_source
        table: dict[str, str] = {}
        for item in sessions:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "")
            num = self.windows.short_of.get(sid[:8], "")
            if not num:
                continue
            table[num] = str(item.get("status") or "?").lower()
        if not table:
            self.last_error = (
                "sessions 里没有一个 id 能对上 targets.json —— 身份表变了，"
                "此时判忙闲会全错，本轮不派"
            )
            return None, self.last_source
        if require_all and len(table) < 6:
            self.last_error = f"只认出 {len(table)}/6 个窗口（{sorted(table)}），当环境不完整处理"
            return None, self.last_source
        return table, self.last_source


def collect_dirty(repo: Path) -> tuple[set[str], str]:
    """仓库实际改动集（声明之外的真话）。失败返回空集 + 原因，绝不冒充「干净」。"""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), f"git status 无法执行：{exc}"
    if proc.returncode != 0:
        return set(), f"git status rc={proc.returncode}：{(proc.stderr or '')[:160]}"
    dirty: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:                  # rename：两边都算脏
            for part in rest.split(" -> "):
                dirty.add(part.strip().strip('"'))
        else:
            dirty.add(rest.strip().strip('"'))
    return dirty, ""


# 真在并发改文件的状态。delivered 不算——它只是「自述完成、等判定」，
# 两个 delivered 声明同一文件不是事故，只是「先后做过同一处」。
CONCURRENT_STATES = {"running", "verifying"}


def attribute_observed(tasks: list[dict], dirty: set[str]) -> list[dict]:
    """把实际改动按声明 scope 归到任务上，并分出两种重叠。

    这个区分今晚踩过：把「两个 delivered 都声明了 tests/」报成「双写」，
    真正的事故（两个窗口同时在改同一文件）就淹在噪声里，等于没有告警。

    severity:
      * `concurrent` —— ≥2 个任务处于 running/verifying 且声明同一脏文件。
        **这是事故形态**（今晚 ①1号 与 ⑤5号 同时改 daily_cognitive_session.py）。
      * `declared` —— 重叠里含 delivered。不是事故，但能解释「为什么这单重派会被拒」。
    """
    holders: dict[str, list[dict]] = {}
    for t in tasks:
        state = task_state(t)
        if state not in SCOPE_HOLDING_STATES and state not in CONCURRENT_STATES:
            continue
        scope = effective_scope(t)
        hit = sorted(p for p in dirty if any(p == s or (s.endswith("/") and p.startswith(s)) for s in scope))
        if hit:
            t["observed_write"] = hit
        for p in hit:
            holders.setdefault(p, []).append({"task": task_id(t), "state": state})
    out = []
    for path, items in sorted(holders.items()):
        if len(items) < 2:
            continue
        states = [i["state"] for i in items]
        live = [i["task"] for i in items if i["state"] in CONCURRENT_STATES]
        out.append({
            "path": path,
            "tasks": [i["task"] for i in items],
            "states": states,
            "severity": "concurrent" if len(live) >= 2 else "declared",
            "concurrent_tasks": live,
        })
    return out


# --------------------------------------------------------------------------- #
# 派单判定
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(
        self,
        board: Board,
        windows: WindowTable,
        status: StatusSource,
        repo: Path,
        live: bool = False,
        cooldown_s: int = 600,
        stall_s: int = 1800,
        verify_cmd: list[str] | None = None,
        verify_timeout: int = 600,
        harvest: bool = False,
    ):
        self.board = board
        self.windows = windows
        self.status = status
        self.repo = Path(repo)
        self.live = live
        self.cooldown_s = cooldown_s
        self.stall_s = stall_s
        self.verify_cmd = verify_cmd or []
        self.verify_timeout = verify_timeout
        self.harvest = harvest
        self.notes: list[str] = []

    # ---------------- 派单 ----------------

    def plan(self, doc: dict, statuses: dict[str, str]) -> list[dict]:
        tasks = doc["tasks"]
        by_id = {task_id(t): t for t in tasks}
        busy_scope: list[tuple[str, list[str]]] = []
        for t in tasks:
            if task_state(t) in SCOPE_HOLDING_STATES:
                busy_scope.append((task_id(t), effective_scope(t)))
        occupied = {
            str(t.get("owner")).strip()
            for t in tasks
            if task_state(t) in SCOPE_HOLDING_STATES and str(t.get("owner") or "").strip() not in ("", "-")
        }

        decisions: list[dict] = []
        claimed_windows: set[str] = set()
        taken_scope = list(busy_scope)

        for t in tasks:
            tid = task_id(t)
            state = task_state(t)
            owner = str(t.get("owner") or "").strip()
            entry = {
                "task_id": tid,
                "title": (t.get("title") or "")[:60],
                "window": owner,
                "decision": "SKIPPED",
                "reason_code": "",
                "reason": "",
                "conflict_with": [],
                "claimed_files": [],
            }

            if state == "blocked_user":
                entry.update(decision="LIST_ONLY", reason_code="BLOCKED_USER", reason=REASONS["BLOCKED_USER"])
                decisions.append(entry)
                continue
            if state not in TODO_STATES:
                entry.update(reason_code="WRONG_STATE", reason=f"{REASONS['WRONG_STATE']}（state={state or '?'}）")
                decisions.append(entry)
                continue
            if not owner or owner == "-":
                entry.update(reason_code="NO_OWNER", reason=REASONS["NO_OWNER"])
                decisions.append(entry)
                continue

            scope = write_scope(t)
            if not scope:
                entry.update(reason_code="NO_WRITE_SCOPE", reason=REASONS["NO_WRITE_SCOPE"])
                decisions.append(entry)
                continue

            deps = dependencies(t)
            missing = [d for d in deps if d not in by_id]
            if missing:
                entry.update(
                    reason_code="DEP_MISSING",
                    reason=f"{REASONS['DEP_MISSING']}：{', '.join(missing)}",
                )
                decisions.append(entry)
                continue
            unmet = [d for d in deps if task_state(by_id[d]) not in COMPLETED_STATES]
            if unmet:
                entry.update(
                    reason_code="DEP_UNMET",
                    reason=(
                        f"{REASONS['DEP_UNMET']}："
                        + ", ".join(f"{d}={task_state(by_id[d]) or '?'}" for d in unmet)
                    ),
                )
                decisions.append(entry)
                continue

            claims = list(scope) + [p for p in observed_write(t) if p not in scope]
            entry["claimed_files"] = claims

            conflicts: list[str] = []
            files: list[str] = []
            for other_id, other_scope in taken_scope:
                if other_id == tid:
                    continue
                hit = scope_overlap(claims, other_scope)
                if hit:
                    conflicts.append(other_id)
                    files.extend(h for h in hit if h not in files)
            if conflicts:
                entry.update(
                    decision="REFUSED",
                    reason_code="SCOPE_CONFLICT",
                    reason=(
                        "单写者约束：与在跑任务 "
                        + ", ".join(sorted(conflicts))
                        + " 的写入范围相交（"
                        + ", ".join(files[:6])
                        + "）→ 拒绝派单"
                    ),
                    conflict_with=sorted(conflicts),
                )
                decisions.append(entry)
                continue

            sid = self.windows.sid_for(owner)
            if not sid:
                entry.update(reason_code="WINDOW_UNKNOWN", reason=f"{REASONS['WINDOW_UNKNOWN']}（owner={owner}）")
                decisions.append(entry)
                continue

            st = statuses.get(owner, "?")
            if st != "idle":
                entry.update(
                    reason_code="WINDOW_NOT_IDLE",
                    reason=f"{REASONS['WINDOW_NOT_IDLE']}（status={st}）",
                )
                decisions.append(entry)
                continue
            if owner in occupied or owner in claimed_windows:
                entry.update(reason_code="WINDOW_OCCUPIED", reason=REASONS["WINDOW_OCCUPIED"])
                decisions.append(entry)
                continue

            last = parse_ts(t.get("dispatched_at") or t.get("last_dispatched_at"))
            if last and (now_ts() - last) < self.cooldown_s:
                entry.update(
                    reason_code="ALREADY_DISPATCHED",
                    reason=f"{REASONS['ALREADY_DISPATCHED']}（{int(now_ts() - last)}s < {self.cooldown_s}s）",
                )
                decisions.append(entry)
                continue

            entry.update(
                decision="DISPATCH" if self.live else "DISPATCH_DRY_RUN",
                reason_code="DISPATCH",
                reason=REASONS["DISPATCH"] if self.live else REASONS["DISPATCH_DRY_RUN"],
            )
            claimed_windows.add(owner)
            taken_scope.append((tid, claims))
            decisions.append(entry)

        return decisions

    def send(self, task: dict, window: str) -> tuple[bool, str]:
        """真投递。复用 dispatch.sh send（不重写投递通道）。"""
        if not self.live:
            return True, "dry-run：未投递"
        text = build_dispatch_text(task, self.windows.label_for(window))
        dispatch = self.status.dispatch
        if dispatch is None or not Path(dispatch).exists():
            return False, "dispatch.sh 不存在"
        argv = self.status._argv()[:-1] + ["send", window, text]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f" dispatch.sh send 无法执行：{exc}"
        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            return False, f"dispatch.sh send rc={proc.returncode}：{(proc.stderr or out)[:200]}"
        if "TIMEOUT after" in out:
            return False, f"投递后窗口超时未答（stalled）：{out[:160]}"
        return True, out[:200] or "ok"

    def run_dispatch(self, doc: dict, decisions: list[dict]) -> dict:
        dispatched, refused, list_only, skipped = [], [], [], []
        st = doc.get("tasks")
        pos = {task_id(t): t for t in st}
        for d in decisions:
            if d["reason_code"] == "BLOCKED_USER":
                list_only.append(d)
                continue
            if d["decision"] == "SKIPPED" and d["reason_code"] not in ("WRONG_STATE",):
                # 「跳过了」必须能被看见并写进流水：静默跳过是 loop.sh 的病
                skipped.append(d)
                self.board.log({
                    "event": "skip", "task_id": d["task_id"], "window": d["window"],
                    "decision": "SKIPPED", "reason": d["reason"],
                    "reason_code": d["reason_code"], "conflict_with": [], "claimed_files": [],
                })
                continue
            if d["decision"] == "REFUSED":
                refused.append(d)
                self.board.log({
                    "event": "dispatch", "task_id": d["task_id"], "window": d["window"],
                    "decision": "REFUSED", "reason": d["reason"], "reason_code": d["reason_code"],
                    "conflict_with": d["conflict_with"], "claimed_files": d["claimed_files"],
                })
                continue
            if d["decision"] not in ("DISPATCH", "DISPATCH_DRY_RUN"):
                continue
            task = pos[d["task_id"]]
            ok, detail = self.send(task, d["window"])
            record = {
                "event": "dispatch", "task_id": d["task_id"], "window": d["window"],
                "decision": "DISPATCHED" if (ok and self.live) else ("DRY_RUN" if ok else "SEND_FAILED"),
                "reason": d["reason"], "reason_code": d["reason_code"],
                "conflict_with": [], "claimed_files": d["claimed_files"], "detail": detail,
            }
            if ok:
                if not self.live:
                    # dry-run 只报告，绝不改台账 —— 否则「看一眼」就等于把单派出去了，
                    # 下一次真跑时这单已经不在 todo 里，人会以为它已经被派过。
                    dispatched.append(d)
                    self.board.log(record)
                    continue
                task["state"] = "running"
                task["dispatched_at"] = now_iso()
                task["window"] = d["window"]
                task["attempts"] = int(task.get("attempts") or 0)
                task["write_scope"] = write_scope(task)
                task["baseline_offset"] = self._baseline(task, d["window"])
                # 必须清掉上一轮的回报与取证痕迹：否则下一轮收割会拿到旧自述，
                # 用旧话证明新活 —— 这跟「拿派单前的自述当交付」是同一个错。
                for stale in ("delivery", "delivered_at", "verified_claim_sha256",
                              "last_verdict", "last_verify_exit", "last_verify_summary",
                              "stalled", "stalled_at"):
                    task.pop(stale, None)
                task.setdefault("history", []).append({
                    "at": now_iso(), "action": "dispatched" if self.live else "dispatch_dry_run",
                    "window": d["window"], "by": "dispatch_engine",
                })
                dispatched.append(d)
            else:
                # 投递失败不消耗任务状态（可能是 broker 挂了/session 不在），
                # 只记 log，下一轮重试。**这不是 stalled**：stalled 是「发出去了不回」。
                record["reason_code"] = "SEND_FAILED"
                record["reason"] = f"投递失败：{detail}"
                refused.append({**d, "reason": record["reason"], "decision": "REFUSED",
                                "reason_code": "SEND_FAILED"})
            self.board.log(record)
        return {"dispatched": dispatched, "refused": refused, "list_only": list_only,
                "skipped": skipped}

    def _baseline(self, task: dict, window: str) -> int | None:
        sid = self.windows.sid_for(window)
        if not sid:
            return None
        piwatch = Path(self.status.dispatch).resolve().parent / "piwatch.py" if self.status.dispatch else None
        if not piwatch or not piwatch.exists():
            return None
        try:
            proc = subprocess.run(
                [sys.executable, str(piwatch), "offset", sid],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = (proc.stdout or "").strip()
        return int(text) if text.isdigit() else None

    # ---------------- 收割 + 取证 ----------------

    def read_report(self, task: dict) -> tuple[str | None, str]:
        """取一单的回报正文。相对派单基线读，避免拿派单前那条自述当新活。"""
        delivery = (task.get("delivery") or "").strip()
        if delivery:
            return delivery, "queue.delivery"
        window = str(task.get("window") or task.get("owner") or "").strip()
        sid = self.windows.sid_for(window)
        if not sid or not self.status.dispatch:
            return None, "no_window"
        board_dir = Path(self.status.dispatch).resolve().parent
        offset = task.get("baseline_offset")
        if isinstance(offset, int):
            piwatch = board_dir / "piwatch.py"
            if piwatch.exists():
                try:
                    proc = subprocess.run(
                        [sys.executable, str(piwatch), "tail", sid, str(offset)],
                        capture_output=True, text=True, timeout=60,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return None, "piwatch_fail"
                text = strip_transcript((proc.stdout or ""))
                if text:
                    return text, "piwatch_tail"
                return None, "piwatch_empty"
        # 降级：readall 取「最近一条非代码文本」，与派单时间无关 —— 可能拿到旧自述
        readall = board_dir / "readall.py"
        if readall.exists():
            try:
                proc = subprocess.run(
                    [sys.executable, str(readall), self.windows.label_for(window), "6000"],
                    capture_output=True, text=True, timeout=90,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None, "readall_fail"
            text = strip_transcript((proc.stdout or ""))
            if text:
                self.notes.append(
                    f"{task_id(task)}：缺 baseline_offset，退回 readall 收割"
                    "（可能与派单时间无关，取证结论置信度下降）"
                )
                return text, "readall_fallback"
        return None, "no_source"

    def verify(self, task: dict) -> dict:
        """调 T2 的 verify_claim.py 取证，把四值映射成状态转移。"""
        if not self.verify_cmd:
            return {"ok": False, "reason": "未配置 verify_claim.py"}
        claim, source = self.read_report(task)
        if not claim:
            return {"ok": False, "reason_code": "NO_REPORT", "reason": source}
        digest = hashlib.sha256(claim.encode("utf-8", "replace")).hexdigest()
        if task.get("verified_claim_sha256") == digest and task.get("last_verdict"):
            return {
                "ok": True, "reason_code": "ALREADY_VERIFIED", "verdict": task["last_verdict"],
                "exit_code": task.get("last_verify_exit"), "summary": task.get("last_verify_summary"),
            }
        window = str(task.get("window") or task.get("owner") or "").strip()
        payload = {
            "window": window,
            "claim": claim,
            "dispatched_at": task.get("dispatched_at"),
            "expected": task.get("verify_expected") or [],
            "source": source,
        }
        argv = list(self.verify_cmd) + ["--input", "-"]
        if task.get("verify_no_run_commands"):
            argv.append("--no-run-commands")
        try:
            proc = subprocess.run(
                argv, input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, timeout=self.verify_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "reason_code": "VERIFY_BLOCKED_ENV", "reason": f"取证器无法执行：{exc}"}
        try:
            report = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            return {
                "ok": False, "reason_code": "VERIFY_BLOCKED_ENV",
                "reason": f"取证器输出不是 JSON（rc={proc.returncode}）：{(proc.stderr or '')[:200]}",
            }
        summary = report.get("summary") or {}
        code = proc.returncode
        verdict = {
            0: "PASS", 1: "FAIL", 2: "UNVERIFIABLE", 3: "BLOCKED_ENV",
        }.get(code, "BLOCKED_ENV")
        failures = [a for a in (report.get("assertions") or []) if a.get("verdict") == "FAIL"]
        reason_lines = [
            f"{a.get('type')}：{str(a.get('evidence') or '')[:160]}" for a in failures[:3]
        ]
        return {
            "ok": True,
            "reason_code": f"VERIFY_{verdict}",
            "verdict": verdict,
            "exit_code": code,
            "summary": summary,
            "fail_reason": "；".join(reason_lines) if reason_lines else "",
            "claim_sha256": digest,
            "harvest_source": source,
            "assertions": report.get("assertions") or [],
        }

    def run_verify(self, doc: dict) -> dict:
        results = {"passed": [], "failed": [], "unverifiable": [], "blocked": [], "pending": []}
        for t in doc["tasks"]:
            state = task_state(t)
            if state not in REPORTED_STATES:
                continue
            res = self.verify(t)
            code = res.get("reason_code", "")
            tid = task_id(t)
            if code == "NO_REPORT":
                results["pending"].append({"task_id": tid, "reason": res.get("reason")})
                continue
            if not res.get("ok"):
                self.board.log({"event": "verify", "task_id": tid, "decision": code,
                                "reason": res.get("reason")})
                results["blocked"].append({"task_id": tid, "reason": res.get("reason")})
                task_state_set(t, "verifying")
                continue
            verdict = res["verdict"]
            if not self.live:
                # dry-run 做取证（取证本身只读、有价值），但**不改台账**。
                # 否则「跑一次看看到底通没通」就把单判成 completed 了 —— 人会以为
                # 已经被人复核过，而其实只是机器隔着看一眼。
                bucket = {"PASS": "passed", "FAIL": "failed"}.get(verdict, "unverifiable")
                results[bucket].append({
                    "task_id": tid, "dry_run": True,
                    "summary": res.get("summary"), "reason": res.get("fail_reason"),
                    "reason_code": res.get("reason_code"),
                })
                self.board.log({
                    "event": "verify", "task_id": tid, "decision": verdict,
                    "dry_run": True, "reason_code": res.get("reason_code"),
                    "summary": res.get("summary"), "harvest_source": res.get("harvest_source"),
                })
                continue
            t["verified_claim_sha256"] = res.get("claim_sha256")
            t["last_verdict"] = verdict
            t["last_verify_exit"] = res.get("exit_code")
            t["last_verify_summary"] = res.get("summary")
            t["harvest_source"] = res.get("harvest_source")
            if verdict == "PASS":
                if "delivery" not in t:
                    claim, _ = self.read_report(t)
                    t["delivery"] = (claim or "")[:6000]
                task_state_set(t, "completed")
                t["completed_at"] = now_iso()
                t.setdefault("history", []).append(
                    {"at": now_iso(), "action": "verify_pass", "summary": res.get("summary")}
                )
                results["passed"].append({"task_id": tid, "summary": res.get("summary")})
            elif verdict == "FAIL":
                task_state_set(t, "todo")
                t["dispatched_at"] = None
                t["baseline_offset"] = None
                t["attempts"] = int(t.get("attempts") or 0) + 1
                reason = res.get("fail_reason") or "取证未通过"
                t["last_fail_reason"] = reason
                stamp = now_iso()
                append_banner(t, f"上次未通过（{stamp}）：{reason}")
                t.setdefault("history", []).append(
                    {"at": stamp, "action": "verify_fail", "reason": reason,
                     "attempts": t["attempts"]}
                )
                results["failed"].append({"task_id": tid, "reason": reason, "attempts": t["attempts"]})
                # 退回 todo 的一瞬间，同一文件可能已被本轮刚派出去的另一单占了。
                # 不在这里点名，人只会看到「怎么又有一单要改同一个文件」——
                # 下一轮 plan() 会拒绝重派，但那是延迟一轮的救火，不是当下的告警。
                holder = requeue_conflict(t, doc)
                if holder:
                    t["requeue_conflict_with"] = holder["conflict_with"]
                    t["requeue_conflict_files"] = holder["files"]
                    results.setdefault("requeue_conflict", []).append(
                        {"task_id": tid, **holder}
                    )
                    self.board.log({
                        "event": "verify", "task_id": tid, "decision": "REQUEUE_CONFLICT",
                        "reason": f"退回 todo 时 {', '.join(holder['conflict_with'])} 正占着同一批文件",
                        "conflict_with": holder["conflict_with"],
                        "claimed_files": holder["files"],
                    })
            elif verdict == "UNVERIFIABLE":
                task_state_set(t, "verifying")
                t["last_fail_reason"] = res.get("fail_reason") or "取证判不了"
                t.setdefault("history", []).append(
                    {"at": now_iso(), "action": "verify_unverifiable",
                     "summary": res.get("summary")}
                )
                results["unverifiable"].append({"task_id": tid, "summary": res.get("summary")})
            else:  # BLOCKED_ENV
                task_state_set(t, "running")
                t.setdefault("history", []).append(
                    {"at": now_iso(), "action": "verify_blocked_env"}
                )
                results["blocked"].append({"task_id": tid, "reason": "环境不可用，下一轮重试"})
            self.board.log({
                "event": "verify", "task_id": tid, "window": t.get("window") or t.get("owner"),
                "decision": verdict, "reason_code": res.get("reason_code"),
                "summary": res.get("summary"), "harvest_source": res.get("harvest_source"),
            })
        return results

    # ---------------- 一轮 ----------------

    def tick(self) -> dict:
        doc = self.board.load()
        digest_before = hashlib.sha256(
            json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        dirty, dirty_err = collect_dirty(self.repo)
        double = attribute_observed(doc["tasks"], dirty)
        if dirty_err:
            self.notes.append(f"实际改动读不到：{dirty_err}（observed_write 这一路失效）")
        for d in double:
            if d["severity"] == "concurrent":
                self.notes.append(
                    "🔴 实际双写（事故）：" + d["path"] + " 同时被 "
                    + ", ".join(d["concurrent_tasks"]) + " 在改"
                )
            else:
                self.notes.append(
                    "声明重叠（非事故）：" + d["path"] + " 被 " + ", ".join(d["tasks"])
                    + " 先后声明过，只影响重派是否被拒"
                )

        statuses, source = self.status.fetch()
        if statuses is None:
            self.board.log({
                "event": "round", "decision": "BLOCKED_ENV", "reason": self.status.last_error,
                "source": source,
            })
            return {
                "ok": False, "reason_code": "LIST_FAIL", "reason": self.status.last_error,
                "dispatch": {"dispatched": [], "refused": []}, "verify": {},
                "observed_double_write": double, "notes": self.notes,
            }

        stalled = mark_stalled(doc["tasks"], self.stall_s)
        # 顺序：先取证（结算上一轮的回报）再派单。反过来会拿陈旧状态派活——
        # 一单自述完成、其实过不了取证，同轮里它的文件就被派给了别人。
        ver = self.run_verify(doc) if self.verify_cmd else {
            "passed": [], "failed": [], "unverifiable": [], "blocked": [], "pending": []}
        decisions = self.plan(doc, statuses)
        disp = self.run_dispatch(doc, decisions)

        digest_after = hashlib.sha256(
            json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if digest_after != digest_before:
            self.board.save(doc)
        self.board.log({
            "event": "round", "decision": "OK", "live": self.live,
            "dispatched": [d["task_id"] for d in disp["dispatched"]],
            "refused": [d["task_id"] for d in disp["refused"]],
            "verified": {k: [x["task_id"] for x in v] for k, v in ver.items()},
            "stalled": stalled, "source": source,
        })
        return {
            "ok": True, "live": self.live, "window_status": statuses, "status_source": source,
            "dispatch": disp, "verify": ver, "stalled": stalled,
            "observed_double_write": double, "dirty_count": len(dirty), "notes": self.notes,
        }


def requeue_conflict(task: dict, doc: dict) -> dict | None:
    """一单被退回 todo 时，检查文件是否已被别的在写任务占了。"""
    claims = effective_scope(task)
    if not claims:
        return None
    tid = task_id(task)
    conflicts, files = [], []
    for other in doc["tasks"]:
        oid = task_id(other)
        if oid == tid or task_state(other) not in SCOPE_HOLDING_STATES:
            continue
        hit = scope_overlap(claims, effective_scope(other))
        if hit:
            conflicts.append(oid)
            files.extend(h for h in hit if h not in files)
    if not conflicts:
        return None
    return {"conflict_with": sorted(conflicts), "files": files}


def task_state_set(task: dict, state: str) -> None:
    task["state"] = state


def append_banner(task: dict, line: str) -> None:
    """在描述里追加「上次未通过：理由」——派人看得见的那一处，不是只写 history。

    追加而不是覆盖：上一轮的失败理由是人判断「是不是同一个坑」的唯一线索。
    """
    key = "description" if "description" in task else "body"
    body = str(task.get(key) or "").rstrip()
    marker = "上次未通过"
    if marker in body:
        body = body.split("\n\n【" + marker)[0].rstrip()
    task[key] = (body + "\n\n【" + line + "】").strip() if body else ("【" + line + "】")


def strip_transcript(text: str) -> str:
    out = []
    for line in (text or "").splitlines():
        if line.startswith("====="):
            continue
        if line.startswith("AGENT> "):
            out.append(line[len("AGENT> "):])
        elif line.startswith("USER> "):
            continue
        else:
            out.append(line)
    return "\n".join(out).strip()


def mark_stalled(tasks: list[dict], stall_s: int) -> list[str]:
    """派出去太久没回报 → 标 stalled。**不自动重派**：重派就是两个窗口改同一文件。"""
    out = []
    for t in tasks:
        if task_state(t) not in SCOPE_HOLDING_STATES:
            continue
        if (t.get("delivery") or "").strip():
            continue
        at = parse_ts(t.get("dispatched_at"))
        if at is None:
            continue
        if (now_ts() - at) <= stall_s:
            continue
        if t.get("stalled"):
            continue
        t["stalled"] = True
        t["stalled_at"] = now_iso()
        t.setdefault("history", []).append({"at": now_iso(), "action": "stalled"})
        out.append(task_id(t))
    return out


def build_dispatch_text(task: dict, label: str) -> str:
    tid = task_id(task)
    body = str(task.get("body") or task.get("description") or task.get("title") or "")
    scope = write_scope(task)
    return (
        f"【DSH 总控台派单 · {tid}】目标窗口 {label}\n"
        f"目标：{task.get('title') or tid}\n"
        f"范围：{', '.join(scope) if scope else '（未声明——请先补 write_scope）'}\n"
        f"验收：{task.get('acceptance') or '见正文（完成后给出可机器核对的判据）'}\n"
        f"回报：完成后在会话末尾输出 —— 交付物路径 / 验收证据 / 卡点\n"
        f"禁止：改范围之外的文件；不许 git push；不许重启服务\n\n"
        f"{body}"
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def default_board_dir(repo: Path) -> Path:
    env = os.environ.get("LIFELOGGER_BOARD_DIR")
    if env:
        return Path(env).expanduser()
    return repo / "agent-board"


def find_dispatch_sh(board_dir: Path) -> Path | None:
    env = os.environ.get("LIFELOGGER_DISPATCH_SH")
    if env:
        return Path(env).expanduser()
    cand = board_dir / "dispatch.sh"
    if cand.exists():
        return cand
    # 看板目录与投递通道常常不在一处（台账在总控台、脚本在仓库）
    for probe in (
        Path.home() / ".life-logger" / "agent-board" / "dispatch.sh",
        Path(__file__).resolve().parent.parent / "agent-board" / "dispatch.sh",
    ):
        if probe.exists():
            return probe
    return None


def render(result: dict) -> str:
    lines = []
    if not result.get("ok"):
        lines.append(f"🔴 环境不可用：{result.get('reason_code')} {result.get('reason')}")
        lines.append("   本轮一单都没派（不是「全部空闲」）")
        return "\n".join(lines)
    disp = result["dispatch"]
    ver = result["verify"]
    mode = "LIVE" if result.get("live") else "DRY-RUN"
    lines.append(f"=== dispatch_engine [{mode}] {now_iso()} ===")
    lines.append("窗口：" + "  ".join(f"{k}={v}" for k, v in sorted(result["window_status"].items())))
    if result.get("stalled"):
        lines.append("⏳ stalled（不自动重派）：" + ", ".join(result["stalled"]))
    for d in result.get("observed_double_write") or []:
        if d["severity"] == "concurrent":
            lines.append(
                f"🔴 实际双写（事故）：{d['path']} ← {', '.join(d['concurrent_tasks'])} 同时在改"
            )
    declared = [d for d in (result.get("observed_double_write") or []) if d["severity"] != "concurrent"]
    if declared:
        lines.append(
            "· 声明重叠（非事故，仅影响重派）："
            + "；".join(f"{d['path']}←{','.join(d['tasks'])}" for d in declared[:4])
        )
    if disp["dispatched"]:
        for d in disp["dispatched"]:
            lines.append(f"📤 {d['task_id']} → 窗{d['window']}  {d['title']}")
    if disp["refused"]:
        for d in disp["refused"]:
            lines.append(f"⛔ {d['task_id']} 拒绝：{d['reason']}")
    if disp["list_only"]:
        lines.append("✋ 等用户拍板（永不自动派）：")
        for d in disp["list_only"]:
            lines.append(f"     {d['task_id']}  {d['title']}")
    if disp.get("skipped"):
        lines.append("⏭️ 跳过（写明原因，不静默）：")
        for d in disp["skipped"]:
            lines.append(f"     {d['task_id']}  {d['reason']}")
    for item in ver.get("requeue_conflict") or []:
        lines.append(
            f"⚠️ {item['task_id']} 退回 todo，但 {', '.join(item['conflict_with'])} 正占着同一批文件"
            f"（{', '.join(item['files'][:4])}）→ 下一轮会被单写者拒绝，先别看它"
        )
    for key, icon in (("passed", "✅"), ("failed", "❌"), ("unverifiable", "❓"), ("blocked", "🚧")):
        for item in ver.get(key) or []:
            extra = item.get("reason") or json.dumps(item.get("summary") or {}, ensure_ascii=False)
            lines.append(f"{icon} {item['task_id']} 取证 {key}：{str(extra)[:160]}")
    if not any([disp["dispatched"], disp["refused"], disp["list_only"], disp.get("skipped")]) and not any(ver.values()):
        lines.append("· 本轮无事可做")
    for note in result.get("notes") or []:
        lines.append(f"· 注：{note}")
    return "\n".join(lines)


def build_engine(args) -> Engine:
    repo = Path(args.repo).resolve()
    board_dir = Path(args.board_dir).resolve() if args.board_dir else default_board_dir(repo)
    windows = WindowTable(board_dir)
    dispatch_sh = None if args.no_dispatch_sh else find_dispatch_sh(board_dir)
    status = StatusSource(dispatch_sh, windows, Path(args.status_file) if args.status_file else None)
    verify_cmd = []
    if args.verify_claim:
        raw = args.verify_claim.split()
        verify_cmd = [sys.executable, str(Path(raw[0]).resolve())] + raw[1:]
    return Engine(
        board=Board(board_dir),
        windows=windows,
        status=status,
        repo=repo,
        live=args.live and not args.dry_run,
        cooldown_s=args.cooldown,
        stall_s=args.stall,
        verify_cmd=verify_cmd,
        verify_timeout=args.verify_timeout,
        harvest=args.harvest,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="派单引擎：队列驱动 + 单写者约束 + 自动派下一单（默认 dry-run）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="跑一轮就退出（默认）")
    mode.add_argument("--watch", action="store_true", help="常驻，每 --interval 秒一轮")
    parser.add_argument("--interval", type=int, default=300, help="--watch 的间隔秒数（默认 300）")
    parser.add_argument("--max-rounds", type=int, default=0, help="--watch 最多跑几轮（0=无限）")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent),
                        help="仓库根（判实际改动用）")
    parser.add_argument("--board-dir", default=None, help="看板目录（含 queue.json / dispatch-log.jsonl）")
    parser.add_argument("--status-file", default=None, help="用夹具文件替代 dispatch.sh list（验收用）")
    parser.add_argument("--no-dispatch-sh", action="store_true", help="不读真实窗口状态（只做队列判定）")
    parser.add_argument("--live", action="store_true", help="真投递（不加则 dry-run，不碰任何窗口）")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run（与默认一致，便于脚本自证）")
    parser.add_argument("--harvest", action="store_true",
                        help="主动从窗口会话收割回报再取证（默认只对已有 delivery 取证）")
    parser.add_argument("--verify-claim", default=None,
                        help="verify_claim.py 路径；给了才启用取证闭环")
    parser.add_argument("--verify-timeout", type=int, default=600)
    parser.add_argument("--cooldown", type=int, default=600, help="同一单防重复派的冷却秒数")
    parser.add_argument("--stall", type=int, default=1800, help="派出去多久没回报算 stalled")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    engine = build_engine(args)
    lock_path = engine.board.dir / ".dispatch-engine.lock"
    engine.board.dir.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"ok": False, "reason_code": "LOCKED",
                          "reason": "另一个 dispatch_engine 实例正在跑"}, ensure_ascii=False))
        return EXIT_ENV

    rounds = 0
    worst = EXIT_OK
    while True:
        rounds += 1
        try:
            result = engine.tick()
        except Exception:
            tb = traceback.format_exc().strip().splitlines()
            print(json.dumps({"ok": False, "reason_code": "ENGINE_ERROR",
                              "reason": tb[-1] if tb else "unknown"}, ensure_ascii=False))
            return EXIT_ENGINE
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
        if not result.get("ok"):
            worst = max(worst, EXIT_ENV)
        elif result["dispatch"]["refused"] or result["verify"].get("failed"):
            worst = max(worst, EXIT_REFUSED)
        if not args.watch:
            break
        if args.max_rounds and rounds >= args.max_rounds:
            break
        time.sleep(max(1, args.interval))
    return worst


if __name__ == "__main__":
    sys.exit(main())
