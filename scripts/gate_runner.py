#!/usr/bin/env python3
"""scripts/gate_runner.py —— 门禁自动化：跑六关 + 自动定性失败原因（红牌/黄牌）。

职责边界（见 work/automation-20261023/DESIGN.md 2.2）：
  * 只「调用 + 分类」 scripts/verify_all.sh，不重写六关本体。
  * 输出 agent-board/gate-report.json（单次）+ output/gate-history.jsonl（最近 30 次）。
  * 把原三态 PASS/FAIL/SLOW 里的 FAIL 细分为 FAIL_CODE / FAIL_DATA / FAIL_ENV，
    并保留 UNVERIFIABLE（门禁自己没跑出结论）与 FLAKY（连续两次不一致）。

分类判据互斥，按顺序短路（先 ENV，因为环境坏会让所有关都 FAIL，先归因才不会冤枉代码）：
  1. FAIL_ENV  —— 端口不通 / 进程不在 / 请求超时(000) / 认证失败(401,403) /
                  门禁 JSON 缺失或不可解析 / verify_all.sh 自身崩溃 / 测试解释器缺 pytest
  2. FAIL_CODE —— py_compile 报错、node --check 失败、bash -n 失败、pytest 断言失败、
                  journal_logic 条数不对、仓库卫生违规
  3. FAIL_DATA —— 账本对账规则不 PASS（带具体日期 + 期望值 + 实际值）
  4. SLOW / WARN —— 不阻断（黄牌）
  5. FLAKY     —— 同一关连续两次 PASS/FAIL 翻转，单列，不算 PASS 也不算 FAIL

退出码（供无人值守调度消费）：
  0 PASS / 10 PASS_WITH_SKIP / 20 SLOW 或 WARN_CLAIM / 30 FLAKY（仅抖动，无失败）
  1 FAIL_CODE / 2 FAIL_DATA / 3 FAIL_ENV

ENV 分两级（见 is_fatal_env / resolve_blocking）：
  * **致命 ENV**（fatal）——服务不可达（端口拒连/超时/5xx）、目标进程不存在、
    解释器缺失或依赖 import 不进去。**任何档位（含 --fast）都退 3。**
    「服务已经死了」是最致命的故障，日常快检也必须报红，否则快检永远发现不了服务挂掉。
  * **可推迟 ENV**（deferred）——「相关那几关本次没跑」造成的未验证状态、
    单个接口探测慢、可选工具没装。这类在 --fast 下只记录不判红：
    否则一次 --fast 就会把所有 run 打成 FAIL_ENV，工具立刻变噪音。
  判据：**服务活不活是 fatal；其它一切 ENV 问题在 --fast 下可以 deferred。**

claim 指标（P-56）：除六关外，还会调用 scripts/verify_claim.py 统计**每类断言**
的 UNVERIFIABLE 占比。任一 type 样本 ≥5 且占比 >50% → 黄牌 WARN_CLAIM：
那不是坏码，但意味着那个检查在静默降级（检查失效时会退化成近乎恒真）。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone  # [Q479-P0] date 已不再使用
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:  # 允许从任意 cwd 运行
    sys.path.insert(0, str(_REPO_ROOT / "src"))
from life_logger.timeutil import LOCAL_TZ  # noqa: E402  [Q479-P0] 统一北京时区

DEFAULT_PORT = 8765
HISTORY_KEEP = 30
GATE_ORDER = ["文件卫生", "代码门禁", "测试门禁", "数据门禁", "接口门禁", "一致性门禁"]

EXIT_CODES = {
    "PASS": 0,
    "PASS_WITH_SKIP": 10,
    "SLOW": 20,
    # WARN_CLAIM 与 SLOW 同属「黄牌」退出码 20：都不是代码坏了，
    # 但也都不是绿牌。独立的 total 名保留，便于报告里区分原因。
    "WARN_CLAIM": 20,
    "FLAKY": 30,
    "FAIL_CODE": 1,
    "FAIL_DATA": 2,
    "FAIL_ENV": 3,
}

# 环境类标记：命中即判 ENV，绝不判 CODE（今晚 3 关超时被误当代码错的教训）
ENV_MARKERS = (
    "超时", "timeout", "timed out", "000:",
    "连接被拒", "refused", "无法连接", "connection reset", "连接重置",
    "端口不通", "进程不在", "not running", "name or service",
    "解析异常", "未找到任何 synthesis",
)
AUTH_MARKERS = ("http401", "http403", "http 401", "http 403", "未授权", "unauthorized")
# 代码类标记：命中即判 CODE，压过门禁名。语法坏就是语法坏，跟在哪个关里报出来无关。
CODE_MARKERS = (
    "py_compile", "node --check", "syntaxerror", "indentationerror",
    "语法错误", "断言失败", "assertionerror", "failed on",
)

# 哪些关的失败可以归 ENV（其余关的 ENV 标记只在其自身证据里生效）
CODE_GATES = {"代码门禁", "测试门禁", "文件卫生"}
DATA_GATES = {"数据门禁", "一致性门禁"}
TIMEOUT_SENSITIVE_GATES = {"接口门禁", "一致性门禁", "测试门禁", "数据门禁"}

# ---- 致命 ENV：任何档位（含 --fast）都必须退 3 的环境故障 ----
# 判据只有一条：**服务活不活是 fatal**。
# 比服务是否在本轮被跑过重要得多——--fast 跳过数据/接口/一致性关时，
# 「相关关没跑」不代表「服务没事」，恰恰相反，那一刻没人验证过它。
# 曾经因此让「服务已死」在 --fast 下退 10（PASS_WITH_SKIP 黄牌），
# 于是日常快检永远发现不了最致命的故障。
FATAL_ENV_PATTERNS = (
    "SERVICE_DOWN",
    "PORT_UNREACHABLE",
    "PROCESS_NOT_FOUND",
    "NO_PYTEST",
    "INTERPRETER_MISSING",
    "IMPORT_ERROR",
)

# 非 2xx 且属「服务不可用类」的状态码：5xx / 503 / 502 / 504
SERVICE_UNAVAILABLE_STATUS = (500, 502, 503, 504)

# ---- claim 断言指标（P-56）----
# 上线后真正该盯的不是 PASS 率，而是**每种断言类型各自的 UNVERIFIABLE 占比**：
# 某一种突然变高 = 那个检查在静默降级（检查失效时会退化成近乎恒真的判断）。
# T5 首轮只抓到 1/5，三个漏报全是同一模式。
CLAIM_VERDICTS = ("PASS", "FAIL", "UNVERIFIABLE", "BLOCKED_ENV")
CLAIM_UNVERIFIABLE_MIN_SAMPLES = 5     # 样本太少时占比没有统计意义，不报警
# 【2026-10-23 返工】"FAIL 断言本身可疑"：同类型 FAIL 占比高、且证据里全是
# `/api/` 端点或状态枚举词（degraded/pending/retry_after…）→ 大概率是把散文当路径，
# 不是产物真的缺失。这种红必须降级成 WARN_CLAIM（黄牌），否则门禁会长期红且无法归因。
CLAIM_SUSPICIOUS_FAIL_MIN_SAMPLES = 3
CLAIM_SUSPICIOUS_FAIL_THRESHOLD = 0.5
SUSPICIOUS_CLAIM_EVIDENCE_RE = re.compile(
    r"/api/|degraded|snapshot_busy|retry_after|pending/|computing|openclaw",
    re.IGNORECASE,
)
CLAIM_UNVERIFIABLE_THRESHOLD = 0.5     # 占比 > 50% 判黄牌
CLAIM_TREND_MIN_DELTA = 0.05           # 趋势：上升 ≥5 个百分点才算「在上升」


# --------------------------------------------------------------------------
# 环境探针：先独立判断「环境好不好」，再谈代码
# --------------------------------------------------------------------------
def pick_python(repo: Path) -> str:
    venv = repo / ".venv" / "bin" / "python"
    if venv.is_file() and os.access(venv, os.X_OK):
        return str(venv)
    return "python3"


def env_probe(repo: Path, port: int, python_exe: str, process_pattern: str) -> dict:
    """只收集事实，不在这里决定是否阻断——阻断要看「哪几关真的跑了」（见 resolve_blocking）.

    消息里带显式 reason code（SERVICE_DOWN / PROCESS_NOT_FOUND / ...），
    让 is_fatal_env() 能确定性地判「这条是不是致命 ENV」，而不是靠中文措辞去猜。
    """
    probe: dict = {"port": port, "process_pattern": process_pattern, "problems": [],
                   "service_issues": [], "tooling_issues": [], "fatal_issues": []}

    # 1) 端口
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            probe["port_open"] = True
    except OSError as error:
        probe["port_open"] = False
        probe["port_error"] = f"{type(error).__name__}: {error}"
        message = f"PORT_UNREACHABLE 端口 {port} 不通（{type(error).__name__}）"
        probe["problems"].append(message)
        probe["service_issues"].append(message)

    # 1b) 端口开着也未必活着：进程卡死/半死时 TCP 能连上，HTTP 却 5xx。
    # 只有「服务不可用类」状态码算致命；401/403 是令牌问题，
    # 4xx 是路由/参数问题，都不代表服务死了，交给逐关 classify 处理。
    if probe.get("port_open"):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/")
            response = conn.getresponse()
            probe["http_status"] = response.status
            response.read(1)
            conn.close()
            if response.status in SERVICE_UNAVAILABLE_STATUS:
                message = f"SERVICE_DOWN 服务返回 {response.status}（端口在听但不提供服务）"
                probe["problems"].append(message)
                probe["service_issues"].append(message)
        except (OSError, http.client.HTTPException) as error:
            probe["http_status"] = None
            message = f"PORT_UNREACHABLE HTTP 探测失败 {type(error).__name__}"
            probe["problems"].append(message)
            probe["service_issues"].append(message)

    # 2) 进程
    try:
        found = subprocess.run(
            ["pgrep", "-f", process_pattern], capture_output=True, text=True, timeout=10
        )
        pids = [p for p in found.stdout.split() if p.strip()]
        probe["process_pids"] = pids
        if not pids:
            message = f"PROCESS_NOT_FOUND 进程不在（pgrep -f {process_pattern} 无输出）"
            probe["problems"].append(message)
            probe["service_issues"].append(message)
    except (OSError, subprocess.SubprocessError) as error:
        probe["process_pids"] = []
        message = f"PROCESS_NOT_FOUND 进程探测失败 {type(error).__name__}"
        probe["problems"].append(message)
        probe["service_issues"].append(message)

    # 3) 测试解释器：pytest 缺失 = ENV，不是代码错
    probe["python"] = python_exe
    try:
        check = subprocess.run(
            [python_exe, "-c", "import pytest; print(pytest.__version__)"],
            capture_output=True, text=True, timeout=60,
        )
        if check.returncode == 0:
            probe["pytest"] = check.stdout.strip()
        else:
            probe["pytest"] = None
            stderr_tail = (check.stderr or "").strip().splitlines()
            if check.returncode != 0 and any(
                "modulenotfounderror" in line.lower() or "importerror" in line.lower()
                for line in stderr_tail
            ):
                # 依赖缺失让门禁根本跑不起来 —— 致命，不是「可选工具没装」。
                message = f"IMPORT_ERROR 解释器缺 pytest（测试门禁无法可信执行）：{stderr_tail[-1][:120]}"
            else:
                message = "NO_PYTEST 解释器缺 pytest（测试门禁无法可信执行）"
            probe["problems"].append(message)
            probe["tooling_issues"].append(message)
    except (OSError, subprocess.SubprocessError) as error:
        probe["pytest"] = None
        message = f"INTERPRETER_MISSING pytest 探测失败 {type(error).__name__}"
        probe["problems"].append(message)
        probe["tooling_issues"].append(message)

    # 4) token（接口关的输入）
    secrets = repo / "output" / "local-web" / "secrets.json"
    token_ok = False
    try:
        token_ok = bool(json.loads(secrets.read_text()).get("mobile_access_token"))
    except (OSError, ValueError):
        token_ok = False
    probe["token_present"] = token_ok
    if not token_ok:
        probe["problems"].append("output/local-web/secrets.json 缺 mobile_access_token")

    return probe


# --------------------------------------------------------------------------
# 调门禁
# --------------------------------------------------------------------------
def run_verify(repo: Path, json_path: Path, passthrough: list[str], timeout: int,
               python_exe: str, port: int) -> dict:
    env = dict(os.environ)
    venv_bin = str(Path(python_exe).parent)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 端口必须真的下发到 verify_all.sh，否则 --port 只是探针自己看的数字，
    # 门禁仍打 8765 —— 那是「配置存在但无效」的假象。
    env["LIFELOGGER_API_BASE"] = f"http://127.0.0.1:{port}"

    cmd = ["bash", "scripts/verify_all.sh", f"--json={json_path}", *passthrough]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout
        )
        rc, out, err, killed = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as error:
        rc, killed = -1, True
        out = (error.stdout or b"").decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        err = f"gate_runner: verify_all.sh 超过 {timeout}s 未返回"
    elapsed = round(time.time() - started, 2)
    return {
        "cmd": " ".join(cmd), "rc": rc, "stdout": out, "stderr": err,
        "elapsed": elapsed, "killed": killed,
    }


def load_gate_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, "门禁 JSON 文件未生成（verify_all.sh 的 --json 落盘块以 `|| true` 结尾，序列化失败即静默）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"门禁 JSON 不可解析：{type(error).__name__}: {error}"
    if not isinstance(data.get("gates"), list):
        return None, "门禁 JSON 缺 gates 数组"
    return data, None


# --------------------------------------------------------------------------
# 分类
# --------------------------------------------------------------------------
def has_any(text: str, markers: tuple[str, ...] | set[str]) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in markers)


def _service_down(probe: dict) -> bool:
    """服务不可达：端口没开，或进程根本不在了。两者都算 ENV，不算代码/数据。"""
    return probe.get("port_open") is False or not probe.get("process_pids")


def is_fatal_env(reason_code: str | None, evidence: str = "") -> bool:
    """服务死活 / 环境不可用属致命，任何档位都必须红牌（退 3）。

    判据只看**服务还活着吗**，不看「相关那几关这一轮跑没跑」：
    --fast 跳过关卡只说明没人验证业务，不说明服务没事，必须照样报红。

    evidence 里出现 FATAL_ENV_PATTERNS 任一标记也算致命——探针消息带显式 reason code，
    classify() 的 subtype（SERVICE_DOWN / NO_PYTEST）也复用同一套码，两条路径同源。
    """
    if reason_code and any(code in str(reason_code) for code in FATAL_ENV_PATTERNS):
        return True
    return has_any(evidence or "", FATAL_ENV_PATTERNS)


def classify(gate: str, status: str, evidence: str, probe: dict) -> tuple[str | None, str | None]:
    """返回 (class, subtype)。class 为 None 表示不构成红牌。

    短路顺序 = ENV > DATA > CODE，理由：环境坏会让下面所有关都跟着报错，
    不先归因就一定冤枉代码（今晚 3 关超时被当成代码错，就是这么来的）。
    """
    ev = evidence or ""
    # 优先级一：状态 → 分类 → 门禁名。顺序不能反。
    # 之前先问门禁名、后问 ENV 标记，导致「数据关里其实是服务死了」被判成 FAIL_DATA，
    # 把基础设施故障写成了数据错 —— 这正是今晚要消灭的误判。
    if status == "UNVERIFIABLE":
        return "FAIL_ENV", "UNVERIFIABLE"

    if status == "FAIL":
        if has_any(ev, AUTH_MARKERS):
            return "FAIL_ENV", "AUTH"
        if has_any(ev, ENV_MARKERS):
            return "FAIL_ENV", "TIMEOUT"
        if gate == "测试门禁" and probe.get("pytest") is None:
            return "FAIL_ENV", "NO_PYTEST"
        # 优先级二：证据里的语法错误压过门禁名。
        # 代码关的证据一旦是 py_compile/node --check，就是代码破损，哪怕它出现在别的关里。
        if has_any(ev, CODE_MARKERS):
            return "FAIL_CODE", "SYNTAX"
        if gate in CODE_GATES:
            return "FAIL_CODE", {
                "代码门禁": "SYNTAX", "测试门禁": "ASSERTION", "文件卫生": "HYGIENE"
            }.get(gate, "UNKNOWN_GATE")
        if gate in DATA_GATES:
            return "FAIL_DATA", "LEDGER_MISMATCH"
        return "FAIL_CODE", "UNKNOWN_GATE"

    # ---- SLOW：先问环境，再问是不是冷请求 ----
    # 服务停掉时 curl 得到 000，verify_all.sh 记 SLOW 且退出码 0 ——「查不了」被当成
    # 「没问题」。所以服务死掉时 SLOW 必须升成红牌 ENV；服务活着才降级为黄牌冷请求。
    if status == "SLOW":
        if gate in ("接口门禁", "一致性门禁", "数据门禁") and _service_down(probe):
            return "FAIL_ENV", "SERVICE_DOWN"
        return None, ("COLD_REQUEST" if has_any(ev, ("超时", "timeout", "000:")) else "SLOW")

    # ---- 2) SLOW：服务活着 = 冷请求慢，黄牌，不阻断 ----
    if status == "SLOW":
        return None, ("COLD_REQUEST" if has_any(ev, ("超时", "timeout", "000:")) else "SLOW")

    return None, None


def normalize_status(raw_status: str) -> tuple[str, str | None]:
    """把 verify_all 的原始状态映射成四态；返回 (status, note)。"""
    if raw_status == "PASS":
        return "PASS", None
    if raw_status == "WARN":       # HTTP 200 重测通过 —— 是真通过，只是慢
        return "PASS", "重测通过"
    if raw_status == "SLOW":
        return "SLOW", None
    if raw_status == "SKIP":
        return "SKIP", None
    if raw_status == "FAIL":
        return "FAIL", None
    return "UNVERIFIABLE", f"未知状态 {raw_status!r}"


# --------------------------------------------------------------------------
# FAIL_DATA 的具体证据：日期 + 期望值 + 实际值
# --------------------------------------------------------------------------
def ledger_detail(repo: Path, python_exe: str, evidence: str, timeout: int) -> dict:
    """数据门禁 FAIL 时，独立跑对账脚本取结构化明细。失败不影响分类结论。"""
    # [Q479-P0] 门禁基线的"昨天"必须是**北京昨天**。原来 date.today() 跟随宿主时区，
    # Mac 在 UTC-8 时会取成北京前天 → 假 FAIL / 假 PASS。
    yesterday = (datetime.now(tz=LOCAL_TZ).date() - timedelta(days=1)).isoformat()
    date_from = "2026-10-01"
    match = re.search(r"范围\s+(\d{4}-\d{2}-\d{2})~(\d{4}-\d{2}-\d{2})", evidence or "")
    if match:
        date_from, yesterday = match.group(1), match.group(2)

    env = dict(os.environ)
    env["PATH"] = str(Path(python_exe).parent) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            [python_exe, "scripts/verify_ledger_consistency.py",
             "--from", date_from, "--to", yesterday, "--json"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout,
        )
        payload = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"from": date_from, "to": yesterday,
                "error": f"{type(error).__name__}: {error}", "days": []}

    days = []
    for row in payload.get("rows", []):
        bad = [(rule, row.get(f"rule{i}_note", ""))
               for i, rule in enumerate(("rule1", "rule2", "rule3", "rule4"), start=1)
               if row.get(rule) == "FAIL"]
        if not bad:
            continue
        days.append({
            "date": row.get("date"),
            "rules": [r for r, _ in bad],
            "expected": row.get("synthesis_total"),
            "actual": row.get("daily_total"),
            "range_total": row.get("range_total"),
            "events": {"synthesis": row.get("synthesis_events"),
                       "api": row.get("daily_events"),
                       "event_view": row.get("event_view_events")},
            "note": "; ".join(n for _, n in bad if n),
        })
    return {"from": date_from, "to": yesterday, "days": days}


# --------------------------------------------------------------------------
# claim 断言指标：每种 type 的 UNVERIFIABLE 占比（P-56）
# --------------------------------------------------------------------------
def run_claim_verify(repo: Path, python_exe: str, claim_input: Path,
                     timeout: int) -> tuple[dict | None, str | None]:
    """调用 scripts/verify_claim.py，取回完整 JSON（含每条断言的 type/verdict）。

    为什么带 `--no-run-commands`：门禁本身已经跑过 pytest，这里再跑一遍测试命令
    只会把单次门禁从 3 分钟拖到 10 分钟；test_count 断言会因此判 BLOCKED_ENV ——
    那正是「本关没验证到」的真实表达，指标里会体现出来，不会被当成 PASS。
    """
    if not claim_input.is_file():
        return None, f"claim 输入不存在：{claim_input}"
    cmd = [
        python_exe, str(repo / "scripts" / "verify_claim.py"),
        "--input", str(claim_input),
        "--root", str(repo),
        "--no-run-commands",
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"verify_claim.py 超时 {timeout}s"
    except OSError as exc:
        return None, f"verify_claim.py 无法执行：{exc}"
    raw = (proc.stdout or "").strip()
    if not raw:
        tail = (proc.stderr or "").strip()[-200:]
        return None, f"verify_claim.py 无输出（rc={proc.returncode}）{('：' + tail) if tail else ''}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"verify_claim.py 输出不是 JSON：{exc}"
    if not isinstance(payload, dict):
        return None, "verify_claim.py 输出不是对象"
    return payload, None


def claim_warnings(metrics: dict) -> list[dict]:
    """任一 type：样本 ≥5 且 UNVERIFIABLE 占比 >50% → 黄牌，点名该 type。

    只判 WARN：这不一定有坏码，但一定意味着**这个检查没在起作用**。
    所以既不许记 PASS（假装验证过），也不许判红牌（冤枉代码）。
    """
    warnings: list[dict] = []
    for atype, bucket in (metrics.get("by_type") or {}).items():
        total = int(bucket.get("total") or 0)
        unverifiable = int(bucket.get("UNVERIFIABLE") or 0)
        ratio = float(bucket.get("unverifiable_ratio") or 0.0)
        if total >= CLAIM_UNVERIFIABLE_MIN_SAMPLES and ratio > CLAIM_UNVERIFIABLE_THRESHOLD:
            warnings.append({
                "type": atype,
                "type_kind": "unverifiable_ratio",
                "total": total,
                "unverifiable": unverifiable,
                "ratio": round(ratio, 4),
                "reason": (f"type={atype} 的 UNVERIFIABLE 占比 {ratio:.0%}"
                           f"（{unverifiable}/{total}，样本 ≥{CLAIM_UNVERIFIABLE_MIN_SAMPLES}）"
                           f" > {CLAIM_UNVERIFIABLE_THRESHOLD:.0%}：该检查在静默降级"),
            })
        # 【2026-10-23 返工】FAIL 断言可疑：证据是端点/状态词 → 不是产物缺失
        fails = int(bucket.get("FAIL") or 0)
        suspicious = int(bucket.get("suspicious_fail") or 0)
        if fails:
            fail_ratio = fails / total
            suspicious_ratio = suspicious / total
            if (
                total >= CLAIM_SUSPICIOUS_FAIL_MIN_SAMPLES
                and fail_ratio > CLAIM_SUSPICIOUS_FAIL_THRESHOLD
                and suspicious_ratio > CLAIM_SUSPICIOUS_FAIL_THRESHOLD
            ):
                warnings.append({
                    "type": atype,
                    "type_kind": "suspicious_fail",
                    "total": total,
                    "fail": fails,
                    "suspicious_fail": suspicious,
                    "ratio": round(fail_ratio, 4),
                    "reason": (f"type={atype} 的 FAIL 占比 {fail_ratio:.0%}（{fails}/{total}），"
                               f"其中 {suspicious} 条证据是 /api/ 端点或状态枚举词 → "
                               f"疑似把散文当路径，不是产物缺失（断言本身可疑）"),
                })
    warnings.sort(key=lambda item: (-item["ratio"], item["type"]))
    return warnings


def compute_claim_metrics(claim_report: dict) -> dict:
    """把 verify_claim 的逐条断言压成「按 type 的四值计数 + 占比」。

    未知 verdict 一律并入 UNVERIFIABLE：宁可说「没验证到」，也不能默认可信。
    """
    assertions = [a for a in (claim_report.get("assertions") or []) if isinstance(a, dict)]
    by_type: dict[str, dict] = {}
    summary = {v: 0 for v in CLAIM_VERDICTS}
    unknown_verdicts = 0

    for item in assertions:
        atype = str(item.get("type") or "(unknown)")
        verdict = str(item.get("verdict") or "").upper()
        if verdict not in CLAIM_VERDICTS:
            verdict = "UNVERIFIABLE"
            unknown_verdicts += 1
        bucket = by_type.setdefault(atype, {"total": 0, **{v: 0 for v in CLAIM_VERDICTS}})
        bucket[verdict] += 1
        bucket["total"] += 1
        if verdict == "FAIL" and SUSPICIOUS_CLAIM_EVIDENCE_RE.search(
            f"{item.get('check') or ''} {item.get('evidence') or ''} {item.get('detail') or ''}"
        ):
            bucket["suspicious_fail"] = int(bucket.get("suspicious_fail") or 0) + 1
        summary[verdict] += 1

    total = len(assertions)
    for bucket in by_type.values():
        count = int(bucket["total"]) or 1
        bucket["unverifiable_ratio"] = round(bucket["UNVERIFIABLE"] / count, 4)

    metrics = {
        "available": True,
        "total": total,
        "summary": {**summary, "total": total},
        "global_unverifiable_ratio": round((summary["UNVERIFIABLE"] / total) if total else 0.0, 4),
        "by_type": dict(sorted(by_type.items())),
        "unknown_verdicts": unknown_verdicts,
    }
    metrics["warnings"] = claim_warnings(metrics)
    return metrics


def build_claim_metrics(repo: Path, python_exe: str, claim_input: Path | None,
                        timeout: int) -> dict:
    """采集 claim 指标。采集不到时 available=False，绝不伪装成「全 PASS」。"""
    unavailable = {
        "available": False,
        "total": 0,
        "by_type": {},
        "warnings": [],
        "summary": {**{v: 0 for v in CLAIM_VERDICTS}, "total": 0},
        "global_unverifiable_ratio": None,
    }
    if claim_input is None:
        return {**unavailable, "reason": "未指定 claim 输入"}

    claim_report, error = run_claim_verify(repo, python_exe, claim_input, timeout)
    if claim_report is None:
        return {**unavailable, "reason": error}

    metrics = compute_claim_metrics(claim_report)
    metrics["schema_version"] = claim_report.get("schema_version")
    metrics["exit_code"] = claim_report.get("exit_code")
    metrics["input"] = str(claim_input)
    return metrics


def claim_trend(records: list[dict]) -> list[dict]:
    """指出哪个 type 的 UNVERIFIABLE 占比在上升（最近一次 vs 上一次）。"""
    series: dict[str, list[float]] = {}
    for record in records:
        claim = record.get("claim") or {}
        for atype, ratio in (claim.get("by_type") or {}).items():
            try:
                series.setdefault(str(atype), []).append(float(ratio))
            except (TypeError, ValueError):
                continue

    rising: list[dict] = []
    for atype, values in series.items():
        if len(values) < 2:
            continue
        prev, last = values[-2], values[-1]
        delta = last - prev
        if delta >= CLAIM_TREND_MIN_DELTA:
            rising.append({
                "type": atype,
                "prev": round(prev, 4),
                "now": round(last, 4),
                "delta": round(delta, 4),
            })
    rising.sort(key=lambda item: -item["delta"])
    return rising


# --------------------------------------------------------------------------
# 历史与趋势
# --------------------------------------------------------------------------
def read_history(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # 坏行跳过，不让历史污染当前判定
    return records


def _write_text_atomic(path: Path, text: str) -> None:
    """唯一临时名 + fsync + replace（优先 life_logger.atomic，无该包时等价回退）。

    【2026-10-29】原实现用固定 `<name>.tmp`：两个进程同时写同一历史文件会互相顶掉
    临时文件，后 replace 的一方 FileNotFoundError / 静默丢写。
    """
    data = text.encode("utf-8")
    try:
        from life_logger.atomic import write_bytes_atomic
    except Exception:            # 允许裸 python3（无 life_logger）下独立运行
        write_bytes_atomic = None
    if write_bytes_atomic is not None:
        write_bytes_atomic(path, data)
        return
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_history(path: Path, records: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = records[-HISTORY_KEEP:]
    # 字节格式不变：每行一个 JSON + 换行，ensure_ascii=False
    _write_text_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    return len(kept)


def trend(records: list[dict]) -> dict:
    """看「一直在坏」还是「偶尔抖」。"""
    out: dict = {}
    for gate in GATE_ORDER:
        series = [r.get("gates", {}).get(gate) for r in records]
        series = [s for s in series if s]
        if not series:
            continue
        fails = sum(1 for s in series if s == "FAIL")
        streak = 0
        for status in reversed(series):
            if status == "FAIL":
                streak += 1
            else:
                break
        rate = round(fails / len(series), 3)
        if streak >= 3 or rate >= 0.75:
            label = "一直在坏"
        elif fails:
            label = "偶尔抖"
        else:
            label = "稳"
        out[gate] = {"runs": len(series), "fails": fails, "fail_streak": streak,
                     "fail_rate": rate, "label": label}
    return out


def detect_flaky(records: list[dict], gates: list[dict]) -> tuple[list[dict], list[dict]]:
    """连续两次不一致：PASS/FAIL 翻转或 SKIP 变化 → FLAKY；SLOW↔PASS 是延迟抖动，单列 jitter。"""
    if not records:
        return [], []
    prev = records[-1]
    prev_gates = prev.get("gates", {})
    flaky, jitter = [], []
    for gate in gates:
        name, now = gate["name"], gate["status"]
        before = prev_gates.get(name)
        if not before or before == now:
            continue
        if {before, now} == {"PASS", "FAIL"} or "SKIP" in (before, now) or "UNVERIFIABLE" in (before, now):
            flaky.append({"gate": name, "prev": before, "now": now, "prev_ts": prev.get("ts")})
        elif {before, now} & {"SLOW"}:
            jitter.append({"gate": name, "prev": before, "now": now, "prev_ts": prev.get("ts")})
    return flaky, jitter


SERVICE_GATES = {"接口门禁", "一致性门禁", "数据门禁"}


def resolve_blocking(report: dict, probe: dict,
                     fast: bool = False) -> tuple[list[str], list[str], list[str]]:
    """把环境问题分成「致命」和「可推迟」两堆，并决定哪一堆真的阻断。

    返回 ``(blocking, deferred, fatal)``：

    * blocking：门禁结果本身不可用（无 JSON / 缺关 / 工具崩溃）+ **所有致命 ENV**。
      一律 FAIL_ENV（退 3）。
    * fatal：诊断用，列出本次命中的致命 ENV 原因（blocking 已包含它们）。
    * deferred：非致命的环境问题，且相关那几关本轮没跑（SKIP 或 --fast）——
      只记录，不改判定。

    为什么 --fast 下致命 ENV 也必须 blocking：
    以前 service_issues 一律进 deferred，于是「服务已死 + --fast」只退 10（黄牌）——
    日常快检永远发现不了最致命的故障。现在的判据是**服务活不活**，不是「相关关跑没跑」：
      * 服务死了 → 任何档位都红；
      * 服务活着但某关被 --fast 跳过 → deferred，快检不乱叫。
    逐关的 ENV 归因仍由 classify() 负责，不在这里重复。
    """
    blocking: list[str] = []
    fatal: list[str] = []
    deferred: list[str] = []

    # ---- 1) 结构性：门禁结果本身不可用 ----
    if report.get("gate_json_error"):
        blocking.append(report["gate_json_error"])
    if report.get("missing_gates"):
        blocking.append(f"六关输出缺关：{', '.join(report['missing_gates'])}（verify_all.sh 未按契约输出）")

    ran = {g["name"] for g in report["gates"] if g["status"] not in ("SKIP", "UNVERIFIABLE")}
    mode_note = "（--fast 跳过，本次未验证）" if fast else "（相关关本次未执行，未计入判定）"

    # ---- 2) 服务类问题：致命 / 可推迟二分 ----
    # 判据是「服务还活着吗」，不是「这条消息措辞像不像故障」：
    #   * 探针事实（端口不通 / 进程为空）→ 服务死了 → fatal，与哪几关跑过无关；
    #   * 消息自带 fatal reason code（PORT_UNREACHABLE / PROCESS_NOT_FOUND ...）→ fatal；
    #   * 其余（单个接口慢、跳关未验证等）→ 服务活着 → 只在相关关没跑时 deferred。
    # 注意绝不能无条件传 "SERVICE_DOWN"：那会把「接口慢」也一并判成服务死掉。
    service_is_fatal = _service_down(probe) or bool(probe.get("service_down"))
    for message in probe.get("service_issues", []):
        if service_is_fatal or is_fatal_env(None, message):
            fatal.append(message)
            continue
        if ran & SERVICE_GATES:
            continue  # 相关关跑了且服务活着，逐关 classify 已经归因，不重复记
        deferred.append(message + mode_note)

    # ---- 3) 工具类问题：解释器缺失/依赖 import 不进去属致命 ----
    for message in probe.get("tooling_issues", []):
        if is_fatal_env(None, message):
            fatal.append(message)
            continue
        if "测试门禁" in ran:
            continue
        deferred.append(message + mode_note)

    for message in fatal:
        blocking.append(f"致命环境故障（任何档位都判红）：{message}")

    return blocking, deferred, fatal


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run_once(args) -> dict:
    repo = Path(args.repo).resolve()
    python_exe = args.python or pick_python(repo)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    started_wall = datetime.now(timezone.utc).astimezone()

    json_path = Path(args.out_json) if args.out_json else Path("/tmp") / f"gate-result-{run_id}.json"
    if not json_path.is_absolute():
        json_path = repo / json_path

    probe = env_probe(repo, args.port, python_exe, args.process_pattern)
    run = run_verify(repo, json_path, args.passthrough, args.timeout, python_exe, args.port)
    data, json_error = load_gate_json(json_path)

    report: dict = {
        "run_id": run_id,
        "ts": started_wall.isoformat(),
        "repo": str(repo),
        "git_head": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo),
                                   capture_output=True, text=True).stdout.strip() or "unknown",
        "mode": args.mode_label,
        "verify_all": {"rc": run["rc"], "elapsed_s": run["elapsed"],
                       "killed": run["killed"], "cmd": run["cmd"],
                       "json_path": str(json_path)},
        "env_probe": probe,
        "gates": [], "failures": [], "flaky": [], "jitter": [],
        "skip_used": [], "slow_gates": [], "warn_gates": [],
    }

    if json_error or data is None:
        # 「读不到门禁结果」绝不等于「门禁通过」
        report["gate_json_error"] = json_error or "门禁 JSON 缺失"
        report["gates"] = [{"name": g, "raw_status": "UNVERIFIABLE", "status": "UNVERIFIABLE",
                            "class": "FAIL_ENV", "elapsed": None,
                            "evidence": report["gate_json_error"], "subtype": "NO_GATE_JSON"}
                           for g in GATE_ORDER]
    else:
        raw_gates = {g.get("name"): g for g in data.get("gates", [])}
        missing = [g for g in GATE_ORDER if g not in raw_gates]
        if missing:
            report["missing_gates"] = missing
        for name in GATE_ORDER:
            item = raw_gates.get(name)
            if item is None:
                report["gates"].append({"name": name, "raw_status": "MISSING",
                                        "status": "UNVERIFIABLE", "class": "FAIL_ENV",
                                        "elapsed": None, "evidence": "该关未出现在门禁输出中",
                                        "subtype": "GATE_MISSING"})
                continue
            raw_status = str(item.get("status") or "UNVERIFIABLE")
            evidence = str(item.get("evidence") or "")
            status, note = normalize_status(raw_status)
            cls, subtype = classify(name, status, evidence, probe)
            entry = {"name": name, "raw_status": raw_status, "status": status,
                     "class": cls, "subtype": subtype,
                     "elapsed": item.get("duration_s"), "evidence": evidence}
            if note:
                entry["note"] = note
                report["warn_gates"].append(name)
            if status == "SLOW":
                report["slow_gates"].append(name)
            if status == "SKIP":
                report["skip_used"].append(name)
            report["gates"].append(entry)
            if cls:
                failure = {"gate": name, "class": cls, "subtype": subtype, "evidence": evidence}
                if cls == "FAIL_DATA":
                    failure["detail"] = ledger_detail(repo, python_exe, evidence, args.timeout)
                report["failures"].append(failure)

    # verify_all.sh 自己崩了 —— 门禁工具坏了不等于代码坏了
    if run["killed"] or run["rc"] not in (0, 1):
        report["failures"].append({"gate": "门禁工具", "class": "FAIL_ENV", "subtype": "GATE_CRASH",
                                   "evidence": (run["stderr"] or "")[-400:] or f"rc={run['rc']}"})

    # ---- claim 断言指标：每种 type 的 UNVERIFIABLE 占比 ----
    # 采集不到（没有 claim 输入 / 取证器崩了）时 available=False，
    # 不参与判定，但会在报告里明说「这一项没测」。
    if getattr(args, "no_claim", False):
        claim_metrics = {
            "available": False,
            "reason": "--no-claim 显式跳过",
            "total": 0,
            "by_type": {},
            "warnings": [],
            "summary": {**{v: 0 for v in CLAIM_VERDICTS}, "total": 0},
            "global_unverifiable_ratio": None,
        }
    else:
        claim_input = getattr(args, "claim_input", None)
        claim_path = Path(claim_input) if claim_input else None
        if claim_path is not None and not claim_path.is_absolute():
            claim_path = repo / claim_path
        claim_metrics = build_claim_metrics(
            repo, python_exe, claim_path, getattr(args, "claim_timeout", 120)
        )
    report["claim_metrics"] = claim_metrics
    claim_warn = claim_metrics.get("warnings") or []

    fast = bool(getattr(args, "fast", False)) or args.mode_label == "fast"
    blocking_env, deferred_env, fatal_env = resolve_blocking(report, probe, fast=fast)
    report["blocking_env"] = blocking_env
    if fatal_env:
        report["fatal_env"] = fatal_env
    if deferred_env:
        report["deferred_env"] = deferred_env

    # ---- 历史 / 抖动 ----
    history_path = Path(args.history) if Path(args.history).is_absolute() else repo / args.history
    records = read_history(history_path)
    flaky, jitter = detect_flaky(records, report["gates"])
    report["flaky"], report["jitter"] = flaky, jitter

    # ---- 总结论（互斥，ENV > DATA > CODE）----
    if blocking_env:
        report["failures"].insert(0, {"gate": "环境探针", "class": "FAIL_ENV",
                                      "subtype": "PROBE", "evidence": "; ".join(blocking_env)})
    # 互斥总结论按「修复紧迫度」排，不按类别直觉排：
    # 环境坏 → 后面全不可信，先修环境；代码坏 → 改一行就能重验，代价最低；
    # 数据坏 → 往往要人确认口径，最慢。若让常驻的 FAIL_DATA 盖住 FAIL_CODE，
    # 新引入的代码破损会被旧的金额差异永久掩盖（本次实测就撞上了）。
    classes = {f["class"] for f in report["failures"]}
    if "FAIL_ENV" in classes:
        total = "FAIL_ENV"
    elif "FAIL_CODE" in classes:
        total = "FAIL_CODE"
    elif "FAIL_DATA" in classes:
        total = "FAIL_DATA"
    elif flaky:
        total = "FLAKY"
    elif claim_warn:
        # 黄牌：某些断言类型的 UNVERIFIABLE 占比过高 = 那些检查没在起作用。
        # 不判 PASS（会假装验证过），也不判红牌（不一定是坏码）。
        # 排在 SKIP 之前：--fast 跳过几关时，claim 的静默降级同样不能被掩盖。
        total = "WARN_CLAIM"
    elif report["skip_used"]:
        total = "PASS_WITH_SKIP"
    elif report["slow_gates"]:
        total = "SLOW"
    else:
        total = "PASS"

    exit_code = EXIT_CODES[total]
    report["verdict"] = {
        "total": total,
        "exit_code": exit_code,
        "red_card": total.startswith("FAIL"),
        # 红牌即终局：一次运行只能有一张牌，否则「红还是黄」又要人来裁决。
        # FLAKY/SLOW/SKIP 照样记在报告里，只是不再冒充颜色。
        "yellow_card": (not total.startswith("FAIL")) and bool(
            flaky or report["slow_gates"] or report["warn_gates"]
            or report["skip_used"] or jitter or claim_warn),
        "classes": sorted(classes),
        # 结论只认一个类别，但其余类别的失败必须一并列出：否则「红牌 FAIL_CODE」
        # 会让人以为数据是好的，等于用结论掩盖证据。
        "all_classes": sorted(classes),
        "summary": _summary(report, total, blocking_env),
    }

    # ---- 落盘报告 + 历史 ----
    report_path = repo / args.report if not Path(args.report).is_absolute() else Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)

    records.append({
        "ts": report["ts"], "run_id": run_id, "git_head": report["git_head"],
        "total": total, "exit_code": exit_code,
        "gates": {g["name"]: g["status"] for g in report["gates"]},
        "classes": sorted(classes), "flaky": [f["gate"] for f in flaky],
        "skip": report["skip_used"], "duration_s": run["elapsed"],
        "mode": report["mode"],
        # 每轮记录各 type 的 UNVERIFIABLE 占比，供 claim_trend() 判「哪个在上升」。
        "claim": {
            "global_unverifiable_ratio": claim_metrics.get("global_unverifiable_ratio"),
            "by_type": {
                atype: bucket.get("unverifiable_ratio")
                for atype, bucket in (claim_metrics.get("by_type") or {}).items()
            },
            "warned_types": [w["type"] for w in claim_warn],
        },
    })
    written = write_history(history_path, records)
    if written == 0:
        # 历史写不进去 = 本次结果作废（无法区分「一直坏」和「偶尔抖」）
        report["verdict"]["total"] = "FAIL_ENV"
        report["verdict"]["exit_code"] = EXIT_CODES["FAIL_ENV"]
        report["verdict"]["note"] = "gate-history.jsonl 写入为空，本次结果作废"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["history_path"] = str(history_path)
    report["history_count"] = written
    report["trend"] = trend(read_history(history_path))
    report["claim_trend"] = claim_trend(read_history(history_path))
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _summary(report: dict, total: str, env_problems: list[str]) -> str:
    bits = []
    if report["failures"]:
        for failure in report["failures"]:
            detail = ""
            if failure.get("detail", {}).get("days"):
                first = failure["detail"]["days"][0]
                detail = f"（{first['date']} 期望 ¥{first['expected']} 实际 ¥{first['actual']}）"
            bits.append(f"[{failure['class']}] {failure['gate']}: {failure['evidence'][:120]}{detail}")
    if report["flaky"]:
        bits.append("FLAKY: " + ", ".join(f"{f['gate']} {f['prev']}→{f['now']}" for f in report["flaky"]))
    if env_problems:
        bits.append("环境: " + "; ".join(env_problems))
    claim_warnings_list = ((report.get("claim_metrics") or {}).get("warnings") or [])
    if claim_warnings_list:
        bits.append("claim 检查静默降级: " + ", ".join(
            f"{w['type']} {w['ratio']:.0%}({w['unverifiable']}/{w['total']})"
            for w in claim_warnings_list))
    if not bits:
        bits.append("六关全绿" + ("（含 SKIP，不可作 commit 依据）" if report["skip_used"] else ""))
    return f"{total} | " + " | ".join(bits)


def print_report(report: dict) -> None:
    verdict = report["verdict"]
    card = "🔴 红牌" if verdict["red_card"] else ("🟡 黄牌" if verdict["yellow_card"] else "🟢 绿牌")
    print(f"\n== 门禁 {report['run_id']} @ {report['git_head']} ({report['mode']}) ==")
    print(f"{'门禁':<10}{'状态':<13}{'分类':<11}{'用时':>8}  证据")
    for gate in report["gates"]:
        cls = gate.get("class") or "-"
        elapsed = f"{gate['elapsed']:.2f}s" if isinstance(gate.get("elapsed"), (int, float)) else "-"
        print(f"{gate['name']:<10}{gate['status']:<13}{cls:<11}{elapsed:>8}  {gate['evidence'][:110]}")
    for failure in report["failures"]:
        days = (failure.get("detail") or {}).get("days") or []
        if days:
            for day in days:
                print(f"  ↳ FAIL_DATA {day['date']} {'/'.join(day['rules'])}: "
                      f"期望 ¥{day['expected']} 实际 ¥{day['actual']} 区间 ¥{day['range_total']} "
                      f"| 条数 {day['events']} | {day['note'][:90]}")
    if report["flaky"]:
        print("  FLAKY 单列: " + ", ".join(f"{f['gate']} {f['prev']}→{f['now']}" for f in report["flaky"]))
    if report["jitter"]:
        print("  延迟抖动（不算 FLAKY）: " + ", ".join(f"{f['gate']} {f['prev']}→{f['now']}" for f in report["jitter"]))
    if report.get("fatal_env"):
        print("  致命环境故障（任何档位都判红）: " + "; ".join(report["fatal_env"]))
    if report.get("deferred_env"):
        print("  环境仅记录（相关关本次未跑，非致命）: " + "; ".join(report["deferred_env"]))
    print(f"{card} 结论 {verdict['total']} (exit {verdict['exit_code']}) | "
          f"历史 {report['history_count']} 条 → {report['history_path']}")
    print(f"报告: {report['report_path']}")
    print(f"摘要: {verdict['summary']}")
    bad = {g: t for g, t in report.get("trend", {}).items() if t["fails"]}
    if bad:
        print("趋势: " + " | ".join(f"{g} {t['label']}(失败 {t['fails']}/{t['runs']}，连败 {t['fail_streak']})"
                                   for g, t in bad.items()))
    _print_claim_metrics(report)


def _print_claim_metrics(report: dict) -> None:
    """打印每种断言类型的 UNVERIFIABLE 占比（比 PASS 率更能暴露静默降级）。"""
    claim = report.get("claim_metrics") or {}
    if not claim.get("available"):
        if claim:
            print(f"claim 指标: 未采集（{claim.get('reason')}）")
        return
    total = claim.get("total") or 0
    summary = claim.get("summary") or {}
    print(f"claim 指标: 断言 {total} 条 | 全局 UNVERIFIABLE "
          f"{claim.get('global_unverifiable_ratio', 0):.0%} "
          f"({summary.get('UNVERIFIABLE', 0)}/{total}) | "
          f"PASS {summary.get('PASS', 0)} FAIL {summary.get('FAIL', 0)} "
          f"BLOCKED_ENV {summary.get('BLOCKED_ENV', 0)}")
    for atype, bucket in (claim.get("by_type") or {}).items():
        print(f"  - {atype:<20} n={bucket['total']:>2} "
              f"PASS={bucket['PASS']} FAIL={bucket['FAIL']} "
              f"UNVERIFIABLE={bucket['UNVERIFIABLE']} "
              f"BLOCKED_ENV={bucket['BLOCKED_ENV']} "
              f"UNVERIFIABLE占比={bucket['unverifiable_ratio']:.0%}")
    for warn in claim.get("warnings") or []:
        print(f"  ⚠️  WARN_CLAIM {warn['reason']}")
    rising = report.get("claim_trend") or []
    if rising:
        print("claim 趋势（UNVERIFIABLE 占比上升）: " + " | ".join(
            f"{r['type']} {r['prev']:.0%}→{r['now']:.0%}" for r in rising))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="门禁自动化：六关 + 红黄牌定性")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="跑一次就退出（默认）")
    mode.add_argument("--watch", action="store_true", help="每 --interval 秒跑一次（默认 1800=30 分钟）")
    parser.add_argument("--interval", type=int, default=1800, help="--watch 间隔秒数，默认 1800")
    parser.add_argument("--max-runs", type=int, default=0, help="--watch 最多跑几轮，0=无限")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--python", default=None, help="默认用 <repo>/.venv/bin/python")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--process-pattern", default="run_local_web.py",
                        help="服务进程匹配串（pgrep -f）")
    parser.add_argument("--timeout", type=int, default=900, help="单次 verify_all.sh 超时秒数")
    parser.add_argument("--report", default="agent-board/gate-report.json")
    parser.add_argument("--history", default="output/gate-history.jsonl")
    parser.add_argument("--out-json", default=None, help="verify_all.sh --json 的落盘路径")
    parser.add_argument("--fast", action="store_true", help="透传 --fast（数据/接口/一致性关 SKIP）")
    parser.add_argument("--skip-slow", action="store_true", help="透传 --skip-slow")
    parser.add_argument("--json", action="store_true", help="把最终报告再打到 stdout")
    parser.add_argument("--claim-input", default="agent-board/claim.json",
                        help="verify_claim.py 的输入 JSON（默认 agent-board/claim.json；不存在则跳过采集）")
    parser.add_argument("--no-claim", action="store_true", help="不采集 claim 断言指标")
    parser.add_argument("--claim-timeout", type=int, default=120,
                        help="verify_claim.py 超时秒数，默认 120")
    args = parser.parse_args(argv[1:])

    args.passthrough = []
    if args.fast:
        args.passthrough.append("--fast")
    if args.skip_slow:
        args.passthrough.append("--skip-slow")
    args.mode_label = "fast" if args.fast else ("skip-slow" if args.skip_slow else "full")

    if args.watch:
        runs = 0
        while True:
            report = run_once(args)
            print_report(report)
            if args.json:
                print(json.dumps(report, ensure_ascii=False))
            runs += 1
            if args.max_runs and runs >= args.max_runs:
                return report["verdict"]["exit_code"]
            time.sleep(args.interval)

    report = run_once(args)
    print_report(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    return report["verdict"]["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
