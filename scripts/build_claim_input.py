#!/usr/bin/env python3
"""Build a safe verify_claim.py input from delivered agent-board tasks.

Dry-run by default. Only ``--write`` atomically writes agent-board/claim.json.
The generated payload contains only machine-checkable claim text/metadata; it
never marks a task PASS. Missing artifacts are left for verify_claim.py to
report as UNVERIFIABLE/BLOCKED_ENV.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

REPO = Path(__file__).resolve().parents[1]
# [2026-11-04 Q860] 调度台目录不写死：LL_DISPATCH_QUEUE 优先，未设置时保留原值。
DEFAULT_QUEUE = Path(os.environ.get("LL_DISPATCH_QUEUE")
                     or "$HOME/.life-logger/agent-board/queue.json")
DEFAULT_OUTPUT = REPO / "agent-board" / "claim.json"


def _read_queue(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(tasks, list):
        raise ValueError("queue.tasks must be a list")
    return [item for item in tasks if isinstance(item, dict)]


def _matches(task: dict[str, Any], task_ids: set[str], session: str | None) -> bool:
    task_id = str(task.get("id") or "")
    if task_ids and task_id not in task_ids:
        return False
    if session:
        haystack = json.dumps(task, ensure_ascii=False)
        if session not in haystack:
            return False
    return True



# ---------------------------------------------------------------------------
# 从交付文本里**只提取机器可核对的断言**（2026-10-23 返工）
# ---------------------------------------------------------------------------
#: 只有这些后缀才可能是磁盘文件；`openclaw.degraded/retry_after` 这类状态词不在此列。
PATH_SUFFIXES = (
    ".md", ".py", ".json", ".jsonl", ".js", ".mjs", ".cjs", ".ts", ".sh",
    ".txt", ".log", ".yml", ".yaml", ".toml", ".cfg", ".plist", ".csv",
)
#: 状态/枚举词：出现这些说明是 JSON 字段或状态机取值，绝不能拼成 repo 路径。
STATE_TOKEN_RE = re.compile(
    r"^(?:degraded|pending|computing|busy|snapshot_busy|retry_after|cached|"
    r"openclaw|control_snapshot_busy|control_refresh_busy)$"
)
API_PATH_RE = re.compile(r"^/api/[A-Za-z0-9_\-/]*$")   # 整 token 是否端点
API_TOKEN_RE = re.compile(r"(/api/[A-Za-z0-9_\-/]*)")     # 在整行里找端点
PATH_TOKEN_RE = re.compile(r"(?:/Users/[^\s`'\"()\[\]，。；]+|(?:\.{1,2}/)?[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,8})")
TEST_COUNT_RE = re.compile(r"(\d+)\s*(?:passed|tests?\s*(?:全\s*)?PASS|项测试通过)")
TEST_CMD_RE = re.compile(r"(?:pytest|python3?\s+-m\s+pytest)\s+[^\n`]+")


#: 「证据：…」「产物：…」这类句子里的路径属于交付证据，缺失必须 FAIL。
EVIDENCE_CUE_RE = re.compile(r"(?:证据|产物|附|见|输出|报告|artifact|evidence)", re.IGNORECASE)


def _looks_like_state_token(token: str) -> bool:
    """`openclaw.degraded/retry_after`、`pending/computing` 这类不是路径。"""
    if API_PATH_RE.match(token):
        return True
    parts = [p for p in re.split(r"[/.]", token) if p]
    return bool(parts) and all(STATE_TOKEN_RE.match(part) or part in {"degraded", "snapshot"} for part in parts)


def _extract_paths(claim: str, root: Path) -> list[Path]:
    """只保留「物理上可能是文件」的路径：有已知后缀 + 存在或明显缺失。"""
    found: list[Path] = []
    seen: set[str] = set()
    for match in PATH_TOKEN_RE.finditer(claim or ""):
        token = match.group(0).rstrip(".,;:）)")
        if not token or token in seen:
            continue
        if _looks_like_state_token(token):
            continue
        if not token.lower().endswith(PATH_SUFFIXES):
            continue
        line_start = claim.rfind("\n", 0, match.start()) + 1
        line_end = claim.find("\n", match.end())
        line = claim[line_start: line_end if line_end != -1 else len(claim)]
        # 「证据：`A`、`B`」这类句子里的文件名属于**交付证据**：缺失就该 FAIL。
        # 而「（`local_web.py` 控制层）」这种正文引用不是证据，解析不出唯一文件就跳过。
        evidence_line = bool(EVIDENCE_CUE_RE.search(line))
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = (root / token).resolve()
        # 【返工·3】裸文件名（`local_web.py`）先按全仓唯一同名文件解析：
        # 交付里常写"改了 local_web.py"，真实路径是 src/life_logger/local_web.py。
        # 只有唯一命中才认；0 个或多个命中都说明"这不是可定位的证据"，直接不生成断言
        # （但带 `/` 的显式路径仍然照写，缺失就让它 FAIL —— 见 tests.txt 那种真缺失）。
        if "/" not in token and not candidate.exists():
            matches = [
                item for item in root.rglob(candidate.name)
                if item.is_file() and ".git" not in item.parts and "node_modules" not in item.parts
            ]
            if len(matches) == 1:
                candidate = matches[0].resolve()
            elif not matches:
                if not evidence_line:
                    continue
            elif not evidence_line:
                continue
        seen.add(token)
        found.append(candidate)
    return found


def _extract_test_counts(claim: str) -> list[dict[str, Any]]:
    """test_count 只在「给了可复现命令」时才可执行；没命令的仍保留，但 command=None（UNVERIFIABLE）。"""
    items: list[dict[str, Any]] = []
    for match in TEST_COUNT_RE.finditer(claim or ""):
        line_start = claim.rfind("\n", 0, match.start()) + 1
        line_end = claim.find("\n", match.end())
        line = claim[line_start: line_end if line_end != -1 else len(claim)]
        command = None
        cmd = TEST_CMD_RE.search(line)
        if cmd:
            command = cmd.group(0).strip()
        items.append({
            "type": "test_count",
            "claimed_passed": int(match.group(1)),
            "command": command,
            "claim": line.strip()[:300],
        })
    return items


def _extract_amounts(claim: str) -> list[dict[str, Any]]:
    """amount_equals 只在「日期 + 明确 scope」都写了时才生成（否则不猜）。"""
    items: list[dict[str, Any]] = []
    for line in (claim or "").splitlines():
        if "¥" not in line and "CNY" not in line:
            continue
        date = re.search(r"(20\d{2}-\d{2}-\d{2})", line)
        scope = re.search(r"\b(provisional|settled)\b", line)
        amount = re.search(r"(?:¥|CNY\s*)(\d+(?:\.\d+)?)", line)
        if not (date and scope and amount):
            continue
        items.append({
            "type": "amount_equals",
            "date": date.group(1),
            "scope": scope.group(1),
            "amount": float(amount.group(1)),
            "claim": line.strip()[:300],
        })
    return items


def _extract_api_claims(claim: str) -> list[dict[str, Any]]:
    """`/api/*` 只有在同时给出「命令 + 期望状态码」时才生成 api_reachable。

    verify_claim 目前没有 api_reachable 执行器 → 该断言会落 UNVERIFIABLE；
    这比把它当文件路径判 FAIL 诚实（端点不是文件）。
    """
    items: list[dict[str, Any]] = []
    for line in (claim or "").splitlines():
        endpoint = API_TOKEN_RE.search(line)
        if not endpoint:
            continue
        status = re.search(r"\b(200|201|202|204|400|401|403|404|409|429|500|503)\b", line)
        command = re.search(r"\bcurl\b[^\n`]+", line)
        if not (status and command):
            continue
        items.append({
            "type": "api_reachable",
            "endpoint": endpoint.group(1),
            "expect_status": int(status.group(1)),
            "command": command.group(0).strip(),
            "claim": line.strip()[:300],
        })
    return items


def extract_expected(claim: str, root: Path) -> list[dict[str, Any]]:
    """把交付文本转成**显式断言**：只保留能核对的，绝不从散文猜。"""
    expected: list[dict[str, Any]] = []
    for path in _extract_paths(claim, root):
        expected.append({"type": "path_exists", "path": str(path)})
    expected.extend(_extract_test_counts(claim))
    expected.extend(_extract_amounts(claim))
    expected.extend(_extract_api_claims(claim))
    return expected


def build_claim(
    queue: list[dict[str, Any]],
    *,
    task_ids: set[str] | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    selected = [
        task for task in queue
        if str(task.get("state") or "") in {"delivered", "done"}
        and _matches(task, task_ids or set(), session)
    ]
    if not selected:
        raise ValueError("no delivered tasks matched the filter")
    sections = []
    windows = []
    dispatched = []
    for task in selected:
        task_id = str(task.get("id") or "?")
        owner = str(task.get("owner") or "")
        delivery = str(task.get("delivery") or "").strip()
        if not delivery:
            continue
        windows.append(f"{task_id}:{owner}" if owner else task_id)
        if task.get("last_dispatched_at"):
            dispatched.append(str(task["last_dispatched_at"]))
        sections.append(f"### {task_id} / {owner}\n{delivery}")
    if not sections:
        raise ValueError("matched tasks have no delivery text")
    claim_text = "\n\n".join(sections)
    expected = extract_expected(claim_text, REPO)
    # Use the earliest dispatch so mtime checks cannot be relaxed by a later task.
    dispatched_at = min(dispatched) if dispatched else None
    return {
        "schema_version": "claim-input-v1",
        "window": ", ".join(windows),
        "claim": claim_text,
        "dispatched_at": dispatched_at,
        "expected": expected,
        "source": {
            "kind": "agent-board-queue",
            "task_ids": [str(task.get("id") or "") for task in selected],
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--session")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    try:
        queue = _read_queue(args.queue)
        payload = build_claim(queue, task_ids=set(args.task), session=args.session)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"build_claim_input: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.write:
        _atomic_write(args.output, payload)
        print(f"wrote {args.output} ({len(payload['claim'])} chars, tasks={payload['source']['task_ids']})")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
