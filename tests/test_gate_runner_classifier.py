"""gate_runner 分类器回归测试（③3号 P-54 / 13 条 → 15 条可复现用例）。

背景：`scripts/gate_runner.py` 的 `classify()` 负责把门禁的 PASS/FAIL/SLOW
细分成功劳归因正确的四类（FAIL_ENV / FAIL_CODE / FAIL_DATA / 黄牌），
此前只在会话里手工验过，仓库中没有可复现测试。本文件把它钉死。

测试只依赖 `classify()` / `normalize_status()` 的纯函数行为，以及一次
`run_once()` 的「总结论优先级」验证（全部外部依赖被 monkeypatch，不跑真门禁、
不联网、不写仓库文件）。

运行：
    PYTHONPATH=src .venv/bin/python -m pytest tests/test_gate_runner_classifier.py -q
"""

from __future__ import annotations

import argparse
from pathlib import Path


import scripts.gate_runner as gr
from scripts.gate_runner import classify, normalize_status


# ---------------------------------------------------------------------------
# 探针夹具：服务活着 / 服务死了
# ---------------------------------------------------------------------------
def _probe(*, alive: bool = True, pytest_exe: str | None = "/usr/bin/pytest") -> dict:
    return {
        "port_open": alive,
        "process_pids": [4242] if alive else [],
        "pytest": pytest_exe,
        "service_issues": [] if alive else ["端口 8765 不通"],
        "tooling_issues": [],
    }


ALIVE = _probe(alive=True)
DEAD = _probe(alive=False)


# ---------------------------------------------------------------------------
# 1~2: FAIL_CODE / SYNTAX
# ---------------------------------------------------------------------------
def test_fail_code_syntax_in_code_gate():
    """代码关里的 py_compile 报错 → 红牌 FAIL_CODE/SYNTAX。"""
    cls, subtype = classify(
        "代码门禁", "FAIL", "py_compile failed on scripts/verify_all.sh", ALIVE
    )
    assert (cls, subtype) == ("FAIL_CODE", "SYNTAX")


def test_syntax_markers_override_gate_name():
    """语法错误压过门禁名：哪怕它报在数据关里，也是代码破损。"""
    cls, subtype = classify(
        "数据门禁", "FAIL", "node --check web/app.js failed", ALIVE
    )
    assert (cls, subtype) == ("FAIL_CODE", "SYNTAX")


# ---------------------------------------------------------------------------
# 3: FAIL_DATA / LEDGER_MISMATCH
# ---------------------------------------------------------------------------
def test_fail_data_ledger_mismatch():
    """账本对账不 PASS（带日期与期望/实际值）→ FAIL_DATA/LEDGER_MISMATCH。"""
    cls, subtype = classify(
        "数据门禁",
        "FAIL",
        "数据不一致 (失败 1 天): 2026-10-16 rule2失败(日报=8.9 vs /api/daily=0.0)",
        ALIVE,
    )
    assert (cls, subtype) == ("FAIL_DATA", "LEDGER_MISMATCH")


# ---------------------------------------------------------------------------
# 4~6: FAIL_ENV
# ---------------------------------------------------------------------------
def test_fail_env_timeout():
    """证据里出现「超时」→ 先归环境，不许冤枉代码。"""
    cls, subtype = classify("接口门禁", "FAIL", "daily=15.00s(超时)", ALIVE)
    assert (cls, subtype) == ("FAIL_ENV", "TIMEOUT")


def test_fail_env_connection_refused_in_data_gate():
    """最难的一条：数据关里其实是服务死了（connection refused）→ FAIL_ENV 而不是 FAIL_DATA。"""
    cls, subtype = classify(
        "数据门禁",
        "FAIL",
        "Connection refused while reading /api/daily",
        ALIVE,
    )
    assert cls == "FAIL_ENV", f"服务不可达被误判成 {cls}"


def test_fail_env_no_pytest():
    """测试关失败但环境里根本没有 pytest → FAIL_ENV/NO_PYTEST（不是断言失败）。"""
    cls, subtype = classify(
        "测试门禁",
        "FAIL",
        "5 failed, 3 passed",
        _probe(alive=True, pytest_exe=None),
    )
    assert (cls, subtype) == ("FAIL_ENV", "NO_PYTEST")


# ---------------------------------------------------------------------------
# 7~8: SLOW 的两副面孔
# ---------------------------------------------------------------------------
def test_slow_with_live_service_is_cold_request():
    """服务活着，只是请求慢 → 黄牌冷请求，不构成红牌。"""
    cls, subtype = classify(
        "接口门禁", "SLOW", "2026-10-22:daily=15.00s(超时)", ALIVE
    )
    assert cls is None
    assert subtype == "COLD_REQUEST"


def test_slow_with_dead_service_is_red_env():
    """最关键的一条：服务死了、curl 得 000 被记成 SLOW —— 必须升红牌 SERVICE_DOWN。"""
    cls, subtype = classify(
        "接口门禁", "SLOW", "daily=15.00s(超时)", DEAD
    )
    assert (cls, subtype) == ("FAIL_ENV", "SERVICE_DOWN")


# ---------------------------------------------------------------------------
# 9~10: UNVERIFIABLE / AUTH
# ---------------------------------------------------------------------------
def test_unverifiable_is_env():
    """门禁自己没给出结论 → FAIL_ENV，绝不能当成通过。"""
    cls, subtype = classify("一致性门禁", "UNVERIFIABLE", "门禁 JSON 缺失", ALIVE)
    assert (cls, subtype) == ("FAIL_ENV", "UNVERIFIABLE")


def test_auth_markers_are_env():
    """401/403 是令牌问题，属环境，不是数据或代码。"""
    cls, subtype = classify("接口门禁", "FAIL", "HTTP 401 unauthorized", ALIVE)
    assert (cls, subtype) == ("FAIL_ENV", "AUTH")


# ---------------------------------------------------------------------------
# 11~13: 总结论优先级 ENV > CODE > DATA
# ---------------------------------------------------------------------------
def _gate(name: str, status: str, evidence: str = "") -> dict:
    return {"name": name, "status": status, "duration_s": 0.1, "evidence": evidence}


def _run_once_with_gates(monkeypatch, tmp_path: Path, gates: list[dict],
                         claim_metrics: dict | None = None) -> dict:
    """跑一次 run_once，但把所有外部 IO 换掉，只验证总结论聚合逻辑。"""
    monkeypatch.setattr(gr, "env_probe", lambda *a, **k: dict(ALIVE))
    monkeypatch.setattr(
        gr,
        "run_verify",
        lambda *a, **k: {
            "rc": 0, "elapsed": 0.01, "killed": False, "cmd": "stub",
            "stderr": "",
        },
    )
    monkeypatch.setattr(gr, "load_gate_json", lambda path: ({"gates": gates}, None))
    monkeypatch.setattr(gr, "ledger_detail", lambda *a, **k: {"days": []})
    monkeypatch.setattr(gr, "read_history", lambda path: [])
    monkeypatch.setattr(gr, "write_history", lambda path, records: len(records))
    monkeypatch.setattr(gr, "detect_flaky", lambda records, gates: ([], []))
    monkeypatch.setattr(gr, "trend", lambda records: {})
    if claim_metrics is not None:
        monkeypatch.setattr(gr, "build_claim_metrics", lambda *a, **k: claim_metrics)

    args = argparse.Namespace(
        repo=str(tmp_path),
        python="/usr/bin/python3",
        port=8765,
        process_pattern="run_local_web.py",
        passthrough=[],
        timeout=10,
        out_json=str(tmp_path / "gate-result.json"),
        mode_label="test",
        history=str(tmp_path / "gate-history.jsonl"),
        report=str(tmp_path / "gate-report.json"),
        interval=0,
        json=False,
    )
    return gr.run_once(args)


def test_summary_priority_env_beats_code_and_data(monkeypatch, tmp_path):
    """三类同时存在时，总结论必须是 FAIL_ENV：环境坏了，后面两个结论都不可信。"""
    gates = [
        _gate("文件卫生", "PASS"),
        _gate("代码门禁", "FAIL", "py_compile failed on scripts/x.py"),
        _gate("测试门禁", "PASS"),
        _gate("数据门禁", "FAIL", "2026-10-16 rule2失败(日报=8.9 vs /api/daily=0.0)"),
        _gate("接口门禁", "FAIL", "daily=15.00s(超时)"),
        _gate("一致性门禁", "PASS"),
    ]
    report = _run_once_with_gates(monkeypatch, tmp_path, gates)
    assert report["verdict"]["total"] == "FAIL_ENV"
    assert report["verdict"]["exit_code"] == gr.EXIT_CODES["FAIL_ENV"]
    # 单一结论不能掩盖其它证据：三类失败都必须在 classes 里
    assert set(report["verdict"]["all_classes"]) == {"FAIL_ENV", "FAIL_CODE", "FAIL_DATA"}


def test_summary_priority_code_beats_data(monkeypatch, tmp_path):
    """没有 ENV 时，代码错优先于数据错：改一行就能重验，最该先修。"""
    gates = [
        _gate("文件卫生", "PASS"),
        _gate("代码门禁", "FAIL", "py_compile failed on scripts/x.py"),
        _gate("测试门禁", "PASS"),
        _gate("数据门禁", "FAIL", "2026-10-16 rule2失败(日报=8.9 vs /api/daily=0.0)"),
        _gate("接口门禁", "PASS"),
        _gate("一致性门禁", "PASS"),
    ]
    report = _run_once_with_gates(monkeypatch, tmp_path, gates)
    assert report["verdict"]["total"] == "FAIL_CODE"
    assert set(report["verdict"]["all_classes"]) == {"FAIL_CODE", "FAIL_DATA"}


def test_summary_data_only_when_no_env_no_code(monkeypatch, tmp_path):
    """只有数据不一致时，总结论才是 FAIL_DATA。"""
    gates = [
        _gate("文件卫生", "PASS"),
        _gate("代码门禁", "PASS"),
        _gate("测试门禁", "PASS"),
        _gate("数据门禁", "FAIL", "2026-10-16 rule2失败(日报=8.9 vs /api/daily=0.0)"),
        _gate("接口门禁", "PASS"),
        _gate("一致性门禁", "PASS"),
    ]
    report = _run_once_with_gates(monkeypatch, tmp_path, gates)
    assert report["verdict"]["total"] == "FAIL_DATA"
    assert report["verdict"]["exit_code"] == gr.EXIT_CODES["FAIL_DATA"]


# ---------------------------------------------------------------------------
# 14~15: 两个容易漏的边界
# ---------------------------------------------------------------------------
def test_warn_normalizes_to_pass():
    """WARN 是「HTTP 200 但重测才通过」，属真通过，只记 note，不改判定。"""
    status, note = normalize_status("WARN")
    assert status == "PASS"
    assert note == "重测通过"


def test_hygiene_failure_is_code():
    """仓库卫生违规（.bak / >5MB）是产物问题，归 FAIL_CODE/HYGIENE。"""
    cls, subtype = classify("文件卫生", "FAIL", "发现违规临时文件: a.bak-2026", ALIVE)
    assert (cls, subtype) == ("FAIL_CODE", "HYGIENE")


# ---------------------------------------------------------------------------
# 16~22: claim 断言指标（P-56）
#   上线后真正该盯的不是 PASS 率，而是每种 type 各自的 UNVERIFIABLE 占比：
#   某个 type 突然变高 = 那个检查在静默降级（退化成近乎恒真的检查）。
# ---------------------------------------------------------------------------
def _assertion(atype: str, verdict: str) -> dict:
    return {
        "id": "A00",
        "claim": "示例断言",
        "type": atype,
        "check": "stub",
        "verdict": verdict,
        "evidence": "",
    }


def _claim_report(*pairs: tuple[str, str]) -> dict:
    return {"assertions": [_assertion(t, v) for t, v in pairs]}


def test_claim_warn_when_single_type_over_threshold():
    """单 type：样本 5 且 UNVERIFIABLE 占 60% → 黄牌并点名该 type。"""
    report = _claim_report(
        ("path_exists", "UNVERIFIABLE"),
        ("path_exists", "UNVERIFIABLE"),
        ("path_exists", "UNVERIFIABLE"),
        ("path_exists", "PASS"),
        ("path_exists", "PASS"),
    )
    metrics = gr.compute_claim_metrics(report)

    bucket = metrics["by_type"]["path_exists"]
    assert bucket["total"] == 5
    assert bucket["UNVERIFIABLE"] == 3
    assert bucket["unverifiable_ratio"] == 0.6
    assert metrics["global_unverifiable_ratio"] == 0.6

    warnings = metrics["warnings"]
    assert [w["type"] for w in warnings] == ["path_exists"]
    assert "path_exists" in warnings[0]["reason"]
    assert warnings[0]["unverifiable"] == 3


def test_claim_no_warn_when_sample_too_small():
    """样本不足 5 条时不报警：4 条里 3 条 UNVERIFIABLE（75%）也不算，样本太小没统计意义。"""
    report = _claim_report(
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "PASS"),
    )
    metrics = gr.compute_claim_metrics(report)
    assert metrics["by_type"]["test_count"]["unverifiable_ratio"] == 0.75
    assert metrics["warnings"] == []


def test_claim_no_warn_when_all_pass():
    """全 PASS 不该产生黄牌，全局占比为 0。"""
    report = _claim_report(*[("amount_equals", "PASS")] * 6)
    metrics = gr.compute_claim_metrics(report)
    assert metrics["summary"]["PASS"] == 6
    assert metrics["global_unverifiable_ratio"] == 0.0
    assert metrics["warnings"] == []


def test_claim_only_offending_type_is_named():
    """多 type 同时存在时，只点名超阈值的那一个，不许连带把正常的也标黄。"""
    report = _claim_report(
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "UNVERIFIABLE"),
        ("test_count", "PASS"),
        ("path_exists", "UNVERIFIABLE"),
        ("path_exists", "PASS"),
        ("path_exists", "PASS"),
        ("path_exists", "PASS"),
        ("path_exists", "PASS"),
        ("path_exists", "PASS"),
    )
    metrics = gr.compute_claim_metrics(report)

    assert metrics["by_type"]["test_count"]["unverifiable_ratio"] == round(5 / 6, 4)
    assert metrics["by_type"]["path_exists"]["unverifiable_ratio"] == round(1 / 6, 4)

    warned = [w["type"] for w in metrics["warnings"]]
    assert warned == ["test_count"], f"只有 test_count 该被点名，实际 {warned}"
    assert "path_exists" not in metrics["warnings"][0]["reason"]


def test_claim_metrics_summary_counts_all_four_verdicts():
    """四种判定必须分开计数，不许合并；未知 verdict 归入 UNVERIFIABLE。"""
    report = _claim_report(
        ("symbol_at", "PASS"),
        ("symbol_at", "FAIL"),
        ("symbol_at", "BLOCKED_ENV"),
        ("symbol_at", "UNVERIFIABLE"),
        ("symbol_at", "SOMETHING_NEW"),
    )
    metrics = gr.compute_claim_metrics(report)

    assert metrics["summary"]["PASS"] == 1
    assert metrics["summary"]["FAIL"] == 1
    assert metrics["summary"]["BLOCKED_ENV"] == 1
    assert metrics["summary"]["UNVERIFIABLE"] == 2  # 未知 verdict 也算「没验证到」
    assert metrics["summary"]["total"] == 5
    assert metrics["unknown_verdicts"] == 1


def test_claim_warn_downgrades_total_to_warn_claim(monkeypatch, tmp_path):
    """黄牌要落到总结论上：不许是 PASS，也不许是红牌；退出码 20。"""
    gates = [_gate("文件卫生", "PASS"), _gate("代码门禁", "PASS"),
             _gate("测试门禁", "PASS"), _gate("数据门禁", "PASS"),
             _gate("接口门禁", "PASS"), _gate("一致性门禁", "PASS")]
    claim_metrics = {
        "available": True,
        "total": 5,
        "summary": {"PASS": 2, "FAIL": 0, "UNVERIFIABLE": 3, "BLOCKED_ENV": 0, "total": 5},
        "by_type": {"path_exists": {"total": 5, "PASS": 2, "FAIL": 0, "UNVERIFIABLE": 3,
                                     "BLOCKED_ENV": 0, "unverifiable_ratio": 0.6}},
        "global_unverifiable_ratio": 0.6,
        "warnings": [{"type": "path_exists", "total": 5, "unverifiable": 3,
                      "ratio": 0.6, "reason": "type=path_exists 的 UNVERIFIABLE 占比 60%"}],
    }
    report = _run_once_with_gates(monkeypatch, tmp_path, gates, claim_metrics=claim_metrics)

    assert report["claim_metrics"]["warnings"]
    assert report["verdict"]["total"] == "WARN_CLAIM"
    assert report["verdict"]["exit_code"] == 20
    assert report["verdict"]["yellow_card"] is True
    assert report["verdict"]["red_card"] is False


def test_claim_trend_flags_rising_type():
    """趋势只看「在上升」的 type（≥5 个百分点），平的和降的都不报。"""
    records = [
        {"claim": {"by_type": {"path_exists": 0.2, "test_count": 0.4, "symbol_at": 0.5}}},
        {"claim": {"by_type": {"path_exists": 0.6, "test_count": 0.4, "symbol_at": 0.1}}},
    ]
    rising = gr.claim_trend(records)
    assert [item["type"] for item in rising] == ["path_exists"]
    assert rising[0]["prev"] == 0.2
    assert rising[0]["now"] == 0.6


# ---------------------------------------------------------------------------
# 23~26: 致命 ENV vs 可推迟 ENV（P-57）
#   缺陷：`--fast` 下数据/接口/一致性关被 SKIP，服务不可达被归入 deferred_env
#   只记录不判红，于是「服务已死」只退 10（PASS_WITH_SKIP 黄牌）。
#   日常快检永远发现不了服务挂掉 —— 而那是所有故障里最致命的一种。
#
#   修法：判据从「相关关跑没跑」换成「服务活不活」。
#     致命：服务端口连不上 / 进程不存在 / 解释器缺失或依赖 import 不进去
#     可推迟：--fast 跳关产生的未验证状态、单个接口慢、可选工具没装
#   下面每条都**真跑一个子进程**取退出码，不靠断言函数返回值。
# ---------------------------------------------------------------------------
import subprocess
import sys
import textwrap

REPO_ROOT = Path(__file__).resolve().parent.parent

# 子进程驱动：把贵的六关换成桩（不跑 pytest、不联网、不写仓库文件），
# 但 env_probe / resolve_blocking / run_once / 退出码全是真的。
_DRIVER = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, {repo!r})
    import scripts.gate_runner as gr

    PROBE = {probe!r}
    GATES = {gates!r}

    gr.load_gate_json = lambda path: ({{"gates": GATES}}, None)
    gr.run_verify = lambda *a, **k: {{"rc": 0, "elapsed": 0.01, "killed": False,
                                      "cmd": "stub", "stderr": ""}}
    gr.ledger_detail = lambda *a, **k: {{"days": []}}
    gr.read_history = lambda path: []
    gr.write_history = lambda path, records: len(records)
    gr.detect_flaky = lambda records, gates: ([], [])
    gr.trend = lambda records: {{}}
    gr.build_claim_metrics = lambda *a, **k: {{
        "available": False, "reason": "stub", "total": 0, "by_type": {{}},
        "warnings": [], "summary": {{}}, "global_unverifiable_ratio": None}}

    if PROBE is not None:
        gr.env_probe = lambda *a, **k: dict(PROBE)

    rc = gr.main(["gate_runner.py", "--repo", {repo!r}, "--python", sys.executable,
                  "--port", str({port}), "--process-pattern", {pattern!r},
                  "--report", {report!r}, "--history", {history!r},
                  "--no-claim"] + {extra!r})
    sys.exit(rc)
''')

FAST_GATES = [_gate("文件卫生", "PASS"), _gate("代码门禁", "PASS"),
              _gate("测试门禁", "PASS"), _gate("数据门禁", "SKIP"),
              _gate("接口门禁", "SKIP"), _gate("一致性门禁", "SKIP")]

FULL_GATES = [_gate("文件卫生", "PASS"), _gate("代码门禁", "PASS"),
              _gate("测试门禁", "PASS"), _gate("数据门禁", "PASS"),
              _gate("接口门禁", "PASS"), _gate("一致性门禁", "PASS")]


def _run_runner_subprocess(tmp_path: Path, *, probe: dict | None, gates: list[dict],
                           port: int = 59999, pattern: str = "no_such_service_marker",
                           extra: list[str] | None = None) -> int:
    """真跑子进程，返回进程退出码（probe=None 时用真实 env_probe 探测）。"""
    driver = tmp_path / "driver.py"
    driver.write_text(
        _DRIVER.format(
            repo=str(REPO_ROOT), probe=probe, gates=gates, port=port, pattern=pattern,
            extra=extra or [], report=str(tmp_path / "gate-report.json"),
            history=str(tmp_path / "gate-history.jsonl"),
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(driver)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode in (0, 1, 2, 3, 10, 20, 30), (
        f"驱动崩溃 rc={proc.returncode}\nstdout={proc.stdout[-1500:]}\nstderr={proc.stderr[-1500:]}"
    )
    return proc.returncode


def test_is_fatal_env_recognizes_service_and_interpreter_failures():
    """致命码表：服务死活 + 解释器/依赖不可用都属 fatal。"""
    assert gr.is_fatal_env("SERVICE_DOWN") is True
    assert gr.is_fatal_env("PORT_UNREACHABLE") is True
    assert gr.is_fatal_env("PROCESS_NOT_FOUND") is True
    assert gr.is_fatal_env("NO_PYTEST") is True
    assert gr.is_fatal_env("INTERPRETER_MISSING") is True
    assert gr.is_fatal_env("IMPORT_ERROR") is True
    # 非致命：跳关产生的未验证状态、单接口慢、可选工具没装
    assert gr.is_fatal_env(None, "接口 daily 探测超时 15.00s") is False
    assert gr.is_fatal_env(None, "可选工具 jq 未安装") is False
    assert gr.is_fatal_env("COLD_REQUEST") is False


def test_fast_with_unreachable_port_exits_fail_env(tmp_path):
    """核心缺陷回归：--fast + 端口不可达 → 退 3（原本退 10）。

    这里用真实 env_probe：59999 上没有服务，进程模式也匹配不到任何东西。
    """
    rc = _run_runner_subprocess(
        tmp_path, probe=None, gates=FAST_GATES,
        port=59999, pattern="no_such_service_marker", extra=["--fast"],
    )
    assert rc == gr.EXIT_CODES["FAIL_ENV"], f"--fast 下服务死了必须退 3，实际 {rc}"


def test_fast_with_missing_process_exits_fail_env(tmp_path):
    """--fast + 端口开着但没有目标进程 → 同样退 3。

    端口用一次性本地监听（不碰用户的 8765），进程模式故意匹配不到。
    """
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        alive_port = sock.getsockname()[1]
        rc = _run_runner_subprocess(
            tmp_path, probe=None, gates=FAST_GATES,
            port=alive_port, pattern="no_such_service_marker", extra=["--fast"],
        )
    assert rc == gr.EXIT_CODES["FAIL_ENV"], f"--fast 下进程不存在必须退 3，实际 {rc}"


def test_fast_with_slow_interface_but_live_service_stays_yellow(tmp_path):
    """反向误伤防线：服务活着、只是接口慢 → --fast 仍退 10，不许升级成 3。"""
    alive = {
        "port_open": True,
        "process_pids": ["4242"],
        "pytest": "8.0.0",
        "service_issues": ["接口 daily 探测超时 15.00s（服务仍在响应首页 200）"],
        "tooling_issues": [],
    }
    rc = _run_runner_subprocess(tmp_path, probe=alive, gates=FAST_GATES, extra=["--fast"])
    assert rc == gr.EXIT_CODES["PASS_WITH_SKIP"], f"服务活着时不许误判红牌，实际 {rc}"
    assert rc != gr.EXIT_CODES["FAIL_ENV"]


def test_full_mode_with_dead_service_exits_fail_env(tmp_path):
    """非 fast 档原有行为不能坏：服务不可达 → 退 3。"""
    rc = _run_runner_subprocess(
        tmp_path, probe=None, gates=FULL_GATES,
        port=59999, pattern="no_such_service_marker", extra=[],
    )
    assert rc == gr.EXIT_CODES["FAIL_ENV"], f"全量档服务死了必须退 3，实际 {rc}"


def test_resolve_blocking_keeps_fatal_env_blocking_under_fast():
    """resolve_blocking 的二分：致命 ENV 进 blocking，非致命才进 deferred。"""
    report = {"gates": [{"name": g["name"], "status": g["status"]} for g in FAST_GATES]}
    dead = {"port_open": False, "process_pids": [],
            "service_issues": ["PORT_UNREACHABLE 端口 59999 不通（ConnectionRefusedError）"],
            "tooling_issues": []}
    blocking, deferred, fatal = gr.resolve_blocking(report, dead, fast=True)
    assert fatal and blocking, "服务死了在 --fast 下也必须 blocking"
    assert not deferred

    alive = {"port_open": True, "process_pids": ["4242"],
             "service_issues": ["接口 daily 探测超时 15.00s"], "tooling_issues": []}
    blocking, deferred, fatal = gr.resolve_blocking(report, alive, fast=True)
    assert not blocking and not fatal, "服务活着不许 blocking"
    assert deferred, "非致命的环境问题应进 deferred"


# ---------------------------------------------------------------------------
# 返工（2026-10-23）：FAIL 断言可疑 → WARN_CLAIM
# ---------------------------------------------------------------------------
def _suspicious_report(rows):
    return {"assertions": rows, "summary": {"total": len(rows)}}


def test_suspicious_fail_from_prose_is_warned_not_silent():
    """8 条 FAIL 的证据都是 /api/ 端点或状态词 → 断言本身可疑，必须黄牌点名。"""
    rows = [
        {"type": "path_exists", "verdict": "FAIL", "check": "test -e /api/version",
         "evidence": "test -e → 不存在：/api/version"},
        {"type": "path_exists", "verdict": "FAIL", "check": "test -e /api/tasks",
         "evidence": "test -e → 不存在：/api/tasks"},
        {"type": "path_exists", "verdict": "FAIL",
         "check": "test -e .../openclaw.degraded/retry_after",
         "evidence": "test -e → 不存在：openclaw.degraded/retry_after"},
        {"type": "path_exists", "verdict": "PASS", "check": "test -e README.md",
         "evidence": "文件存在"},
    ]
    metrics = gr.compute_claim_metrics(_suspicious_report(rows))
    kinds = [w.get("type_kind") for w in metrics["warnings"]]
    assert "suspicious_fail" in kinds, metrics["warnings"]
    entry = next(w for w in metrics["warnings"] if w.get("type_kind") == "suspicious_fail")
    assert entry["type"] == "path_exists"
    assert entry["suspicious_fail"] == 3


def test_genuine_missing_artifact_fail_is_not_downgraded():
    """真的缺少产物（证据是真实文件路径）→ 不该被判成"断言可疑"。"""
    rows = [
        {"type": "path_exists", "verdict": "FAIL", "check": "test -e work/a/BUILD.md",
         "evidence": "test -e → 不存在：work/a/BUILD.md"},
        {"type": "path_exists", "verdict": "FAIL", "check": "test -e work/a/tests.txt",
         "evidence": "test -e → 不存在：work/a/tests.txt"},
        {"type": "path_exists", "verdict": "FAIL", "check": "test -e work/a/REPORT.md",
         "evidence": "test -e → 不存在：work/a/REPORT.md"},
        {"type": "path_exists", "verdict": "PASS", "check": "test -e README.md",
         "evidence": "文件存在"},
    ]
    metrics = gr.compute_claim_metrics(_suspicious_report(rows))
    assert not [w for w in metrics["warnings"] if w.get("type_kind") == "suspicious_fail"]
