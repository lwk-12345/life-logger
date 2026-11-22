"""失败会话选择性恢复工具的回归测试（全部用临时夹具，不碰生产）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(rel: str) -> "Path":
    """优先读入库夹具（tests/fixtures/...）；没有就退回仓库内原位置。

    换机器（新克隆）时 `work/`、`output/` 都不存在 —— 那时只有入库夹具可用。
    本机两者都在，走哪个都等价，所以不影响本地行为。
    """
    shipped = FIXTURES / rel
    return shipped if shipped.exists() else ROOT / rel




import recover_failed_context_sessions as rr


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


_AUDIT_CACHE: dict[str, str] = {}


def _make_audit(root: Path) -> Path:
    d = root / "audit"
    d.mkdir(parents=True)
    _write(d / "sessions_with_zero_events.json", [
        {"session": "S_TRANSIENT", "date": "2026-10-07", "segments": 100, "failed_sources": 300},
        {"session": "S_HUMAN", "date": "2026-10-08", "segments": 50, "failed_sources": 40},
        {"session": "S_HTTP", "date": "2026-10-15", "segments": 10, "failed_sources": 5},
        {"session": "S_NOJOB", "date": "2026-10-16", "segments": 7, "failed_sources": 0},
        {"session": "S_EMPTY", "date": "2026-10-17", "segments": 0, "failed_sources": 0},
    ])
    _write(d / "failed_jobs.json", [
        {"label": "任务 job-20261007-000000-aaaaaaaa",
         "session_state": {"session": "S_TRANSIENT"}},
        {"label": "任务 job-20261008-000000-bbbbbbbb",
         "session_state": {"session": "S_HUMAN"}},
        {"label": "任务 job-20261015-000000-cccccccc",
         "session_state": {"session": "S_HTTP"}},
        {"label": "任务 job-20261016-000000-dddddddd",
         "session_state": {"session": "S_NOJOB"}},
    ])
    _write(d / "per_job_causes.json", [
        {"artifact": "jobs/job-20261007-000000-aaaaaaaa", "status": "failed",
         "kinds": {"connection_error": 10}, "top_errors": [["URLError: nodename", 10]]},
        {"artifact": "jobs/job-20261008-000000-bbbbbbbb", "status": "failed",
         "kinds": {"connection_error": 3}, "top_errors": [["URLError", 3]]},
        {"artifact": "jobs/job-20261015-000000-cccccccc", "status": "failed",
         "kinds": {"http_error": 2}, "top_errors": [["HTTP 500", 2]]},
    ])
    _write(d / "unique_failed_sources.json", [
        {"artifact": "output/local-web/jobs/job-20261007-000000-aaaaaaaa/files/x/result/context-extraction.json"},
        {"artifact": "output/local-web/jobs/job-20261007-000000-aaaaaaaa/files/x/result/context-extraction.json"},
        {"artifact": "output/local-web/jobs/job-20261008-000000-bbbbbbbb/files/x/result/context-extraction.json"},
        {"artifact": "output/local-web/jobs/job-20261015-000000-cccccccc/files/x/result/context-extraction.json"},
    ])
    return d


@pytest.fixture()
def audit(tmp_path: Path) -> Path:
    return _make_audit(tmp_path)


def _audit(tmp_path: Path, name: str) -> str:
    """给 main() 用的 audit-dir（同一 tmp_path 只造一次）。"""
    cache_key = f"{tmp_path}-{name}"
    if cache_key not in _AUDIT_CACHE:
        _AUDIT_CACHE[cache_key] = str(_make_audit(tmp_path / name))
    return _AUDIT_CACHE[cache_key]


@pytest.fixture()
def states(tmp_path: Path) -> Path:
    p = tmp_path / "pending-review-states.json"
    _write(p, {"schema": "v1", "records": {
        "pending_event:clef:evt_S_HUMAN": {"note": "用户忽略", "status": "ignored"},
        "pending_event:clef:evt_other": {"note": "archived: 历史整批 API 失败", "status": "ignored"},
    }})
    return p


def test_plan_is_sorted_and_annotated(audit: Path, states: Path, tmp_path: Path) -> None:
    plans = rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw")
    dates = [p.date for p in plans]
    assert dates == sorted(dates), "必须按日期排序"
    first = plans[0]
    for field in ("session", "job", "failure_reason", "segments", "quotes",
                  "target_events", "rollback_snapshot", "classification"):
        assert hasattr(first, field)
    assert first.rollback_snapshot.startswith("work/failed-context-recovery-")


def test_transient_error_is_recoverable(audit: Path, states: Path, tmp_path: Path) -> None:
    plans = {p.session: p for p in rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw")}
    assert plans["S_TRANSIENT"].classification == rr.RECOVERABLE
    assert plans["S_TRANSIENT"].recoverable is True


def test_human_ignored_is_skipped(audit: Path, states: Path, tmp_path: Path) -> None:
    plans = {p.session: p for p in rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw")}
    assert plans["S_HUMAN"].classification == rr.SKIP_HUMAN
    assert plans["S_HUMAN"].recoverable is False


def test_non_transient_and_blank_kinds_are_not_auto_recoverable(audit: Path, states: Path, tmp_path: Path) -> None:
    plans = {p.session: p for p in rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw")}
    # http_error 不在瞬时白名单里 → 非自动可恢复
    assert plans["S_HTTP"].classification == rr.TRIAGE
    # 没有 job / 没有 cause 记录 → 人工判定
    assert plans["S_NOJOB"].classification == rr.TRIAGE
    # 0 段 → 无内容可恢复
    assert plans["S_EMPTY"].classification == rr.SKIP_NO_TRANSCRIPT


def test_quotes_counted_per_job(audit: Path, states: Path, tmp_path: Path) -> None:
    plans = {p.session: p for p in rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw")}
    assert plans["S_TRANSIENT"].quotes == 2
    assert plans["S_HUMAN"].quotes == 1
    assert plans["S_NOJOB"].quotes == 0


def test_limit_defers_extra_sessions(audit: Path, states: Path, tmp_path: Path) -> None:
    many = tmp_path / "audit2"
    many.mkdir()
    _write(many / "sessions_with_zero_events.json", [
        {"session": f"S{i}", "date": f"2026-10-{10 + i:02d}", "segments": 5, "failed_sources": 1}
        for i in range(5)
    ])
    _write(many / "failed_jobs.json", [
        {"label": f"job-202610{10 + i:02d}-000000-0000000{i}", "session_state": {"session": f"S{i}"}}
        for i in range(5)
    ])
    _write(many / "per_job_causes.json", [
        {"artifact": f"jobs/job-202610{10 + i:02d}-000000-0000000{i}", "status": "failed",
         "kinds": {"connection_error": 1}}
        for i in range(5)
    ])
    _write(many / "unique_failed_sources.json", [])
    plans = rr.build_plan(many, states_path=states, job_root=tmp_path / "jw", limit=2)
    assert sum(1 for p in plans if p.classification == rr.RECOVERABLE) == 2
    assert sum(1 for p in plans if p.classification == "deferred_over_limit") == 3


def test_apply_requires_session_and_confirm(audit: Path, states: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    base = ["--audit-dir", str(audit), "--states", str(states),
            "--job-root", str(tmp_path / "jw"), "--out-dir", str(out)]
    assert rr.main([*base, "--apply"]) == 2
    assert "拒绝执行" in capsys.readouterr().err
    assert rr.main([*base, "--apply", "--session", "S_TRANSIENT"]) == 2
    assert not (out / "runbook").exists(), "未确认时不得生成 runbook"


def _seed_job_targets(job_root: Path, job: str, date: str) -> None:
    target = job_root / "jobs" / job / "files" / "h" / "result" / date
    target.mkdir(parents=True, exist_ok=True)
    (target / "events.json").write_text('{"events": []}', encoding="utf-8")
    (target.parent / "context-extraction.json").write_text('{"items": []}', encoding="utf-8")


def test_apply_with_confirm_writes_runbook_only(audit: Path, states: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    jw = tmp_path / "jw"
    _seed_job_targets(jw, "job-20261007-000000-aaaaaaaa", "2026-10-07")
    rc = rr.main(["--audit-dir", str(audit), "--states", str(states),
                  "--job-root", str(jw), "--out-dir", str(out),
                  "--apply", "--session", "S_TRANSIENT", "--confirm"])
    assert rc == 0
    runbook = (out / "runbook" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "未执行" in runbook and "job-20261007-000000-aaaaaaaa" in runbook
    payload = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert payload["safety"]["calls_model"] is False
    assert payload["dry_run"] is False


def test_dry_run_writes_plan_and_markdown(audit: Path, states: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    rc = rr.main(["--audit-dir", str(audit), "--states", str(states),
                  "--job-root", str(tmp_path / "jw"), "--out-dir", str(out)])
    assert rc == 0
    payload = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["safety"] == {
        "skips_human_ignored": True,
        "only_transient_and_archived": True,
        "max_concurrency": rr.MAX_CONCURRENCY,
        "max_recoverable": rr.MAX_RECOVERABLE,
        "calls_model": False,
    }
    md = (out / "PLAN.md").read_text(encoding="utf-8")
    assert "dry-run" in md and "回滚快照" in md


def test_concurrency_is_clamped(audit: Path, states: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    rr.main(["--audit-dir", str(audit), "--states", str(states), "--job-root", str(tmp_path / "jw"),
             "--out-dir", str(out), "--concurrency", "99"])
    payload = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert payload["concurrency"] == rr.MAX_CONCURRENCY


def test_priority_batches_split_auto_and_triage(audit: Path, states: Path, tmp_path: Path) -> None:
    plans = rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw")
    batches = rr.priority_batches(plans, ["2026-10-07", "2026-10-08", "2026-10-15", "2026-10-17"])
    by_date = {b["date"]: b for b in batches}
    assert by_date["2026-10-07"]["auto_sessions"] == ["S_TRANSIENT"]
    assert by_date["2026-10-07"]["triage_sessions"] == []
    assert by_date["2026-10-15"]["triage_sessions"] == ["S_HTTP"]
    # 没有可恢复也没有待判定的日期（S_HUMAN 被跳过、S_EMPTY 无内容）不该出现在批次里
    assert "2026-10-08" not in by_date
    assert "2026-10-17" not in by_date


def test_tool_never_calls_a_model() -> None:
    """代码层面（去掉注释/文档字符串后）不得出现任何模型或网络调用。"""
    path = Path(__file__).resolve().parents[1] / "scripts" / "recover_failed_context_sessions.py"
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    source = path.read_text(encoding="utf-8")
    for doc in docstrings:
        source = source.replace(doc, "")
    code_lines = [ln for ln in source.splitlines() if not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    for forbidden in ("import openai", "import requests", "urlopen(", "subprocess.run(",
                      "subprocess.Popen(", "requests.post(", "api.deepseek"):
        assert forbidden not in code, f"恢复工具不得直接调用 {forbidden}"


# ---------------------------------------------------------------------------
# Q152 返工：5 项安全缺陷
# ---------------------------------------------------------------------------

def _malicious_audit(tmp_path: Path) -> Path:
    d = tmp_path / "evil"
    d.mkdir()
    _write(d / "sessions_with_zero_events.json", [
        {"session": "../../etc/passwd", "date": "2026-10-07", "segments": 9, "failed_sources": 1},
        {"session": "S_OK; rm -rf /", "date": "2026-10-07", "segments": 9, "failed_sources": 1},
        {"session": "S_DATE", "date": "../../2026-10-07", "segments": 9, "failed_sources": 1},
        {"session": "S_GOOD", "date": "2026-10-07", "segments": 9, "failed_sources": 1},
    ])
    _write(d / "failed_jobs.json", [
        {"label": "job-20261007-000000-aaaaaaaa", "session_state": {"session": "S_GOOD"}},
    ])
    _write(d / "per_job_causes.json", [
        {"artifact": "jobs/job-20261007-000000-aaaaaaaa", "status": "failed",
         "kinds": {"connection_error": 1}},
    ])
    _write(d / "unique_failed_sources.json", [])
    return d


def test_path_traversal_identifiers_are_rejected(tmp_path: Path, states: Path) -> None:
    audit = _malicious_audit(tmp_path)
    out = tmp_path / "out"
    plans = rr.build_plan(audit, states_path=states, job_root=tmp_path / "jw", out_dir=out)
    bad = [p for p in plans if p.session != "S_GOOD"]
    assert bad, "恶意 session 必须出现在计划里（标 rejected），不能静默消失"
    for plan in bad:
        assert plan.classification == rr.REJECTED
        assert plan.rollback_snapshot == "", "被拒绝的条目不得生成任何输出路径"
        assert plan.recoverable is False
    good = [p for p in plans if p.session == "S_GOOD"]
    assert len(good) == 1 and good[0].recoverable is True
    assert rr.valid_session("../../etc/passwd") is False
    assert rr.valid_date("../../2026-10-07") is False
    assert rr.valid_date("2026-13-40") is False


def test_rollback_path_always_under_out_dir_rollback(tmp_path: Path) -> None:
    out = tmp_path / "out"
    p = rr.rollback_path("job-20261007-000000-aaaaaaaa", "2026-10-07", out)
    assert Path(p).resolve().is_relative_to((out / "rollback").resolve())
    # 非法 job 名不参与拼接
    p2 = rr.rollback_path("../../evil", "2026-10-07", out)
    assert Path(p2).resolve().is_relative_to((out / "rollback").resolve())
    assert "evil" not in Path(p2).name


def test_duplicate_session_rows_are_merged_by_max_segments(tmp_path: Path, states: Path) -> None:
    d = tmp_path / "dup"
    d.mkdir()
    _write(d / "sessions_with_zero_events.json", [
        {"session": "S_DUP", "date": "2026-10-07", "segments": 10, "failed_sources": 1},
        {"session": "S_DUP", "date": "2026-10-07", "segments": 999, "failed_sources": 1},
        {"session": "S_DUP", "date": "2026-10-07", "segments": 5, "failed_sources": 1},
    ])
    _write(d / "failed_jobs.json", [
        {"label": "job-20261007-000000-aaaaaaaa", "session_state": {"session": "S_DUP"}},
    ])
    _write(d / "per_job_causes.json", [
        {"artifact": "jobs/job-20261007-000000-aaaaaaaa", "status": "failed",
         "kinds": {"connection_error": 1}},
    ])
    _write(d / "unique_failed_sources.json", [])
    plans = rr.build_plan(d, states_path=states, job_root=tmp_path / "jw")
    assert len(plans) == 1, "同一 session 只能有一条"
    assert plans[0].segments == 999, "必须保留段数最大的那行"


def test_duplicate_session_does_not_inflate_limit(tmp_path: Path, states: Path) -> None:
    d = tmp_path / "dup2"
    d.mkdir()
    rows = [{"session": "S_A", "date": "2026-10-07", "segments": 1, "failed_sources": 1}] * 4
    rows += [{"session": "S_B", "date": "2026-10-08", "segments": 1, "failed_sources": 1}]
    _write(d / "sessions_with_zero_events.json", rows)
    _write(d / "failed_jobs.json", [
        {"label": "job-20261007-000000-aaaaaaaa", "session_state": {"session": "S_A"}},
        {"label": "job-20261008-000000-bbbbbbbb", "session_state": {"session": "S_B"}},
    ])
    _write(d / "per_job_causes.json", [
        {"artifact": "jobs/job-20261007-000000-aaaaaaaa", "status": "failed", "kinds": {"connection_error": 1}},
        {"artifact": "jobs/job-20261008-000000-bbbbbbbb", "status": "failed", "kinds": {"connection_error": 1}},
    ])
    _write(d / "unique_failed_sources.json", [])
    plans = rr.build_plan(d, states_path=states, job_root=tmp_path / "jw", limit=1)
    assert sum(1 for p in plans if p.recoverable) == 1
    assert sum(1 for p in plans if p.classification == "deferred_over_limit") == 1


def test_apply_with_no_recoverable_selection_exits_2(tmp_path: Path, states: Path, capsys) -> None:
    out = tmp_path / "out"
    rc = rr.main(["--audit-dir", _audit(tmp_path, "human"), "--states", str(states),
                  "--job-root", str(tmp_path / "jw"), "--out-dir", str(out),
                  "--apply", "--session", "S_HUMAN", "--confirm"])
    assert rc == 2
    assert "没有「可恢复」项" in capsys.readouterr().err
    assert not (out / "runbook").exists()
    assert not (out / "rollback").exists()


def test_apply_creates_and_verifies_rollback_tar_before_runbook(tmp_path: Path, states: Path) -> None:
    out = tmp_path / "out"
    jw = tmp_path / "jobroot"
    target = jw / "jobs" / "job-20261007-000000-aaaaaaaa" / "files" / "h" / "result" / "2026-10-07"
    target.mkdir(parents=True)
    (target / "events.json").write_text('{"events": []}', encoding="utf-8")
    (target.parent / "context-extraction.json").write_text('{"items": []}', encoding="utf-8")
    rc = rr.main(["--audit-dir", _audit(tmp_path, "transient"), "--states", str(states),
                  "--job-root", str(jw), "--out-dir", str(out),
                  "--apply", "--session", "S_TRANSIENT", "--confirm"])
    assert rc == 0
    payload = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    snapshot = payload["snapshots"]["S_TRANSIENT"]
    tar_path = out / "rollback" / Path(snapshot["path"]).name
    assert tar_path.is_file() and tar_path.stat().st_size > 0
    import tarfile
    with tarfile.open(tar_path, "r:gz") as archive:
        names = archive.getnames()
    assert snapshot["members"] == names and len(names) == 2
    assert "已校验" in (out / "runbook" / "RUNBOOK.md").read_text(encoding="utf-8")


def test_apply_fails_when_rollback_target_missing(tmp_path: Path, states: Path, capsys) -> None:
    out = tmp_path / "out"
    rc = rr.main(["--audit-dir", _audit(tmp_path, "transient"), "--states", str(states),
                  "--job-root", str(tmp_path / "empty-jobroot"), "--out-dir", str(out),
                  "--apply", "--session", "S_TRANSIENT", "--confirm"])
    assert rc == 4
    assert "回滚快照校验失败" in capsys.readouterr().err
    assert not (out / "runbook").exists(), "快照失败时不得写 runbook"


def test_markdown_documents_archived_vs_matched_quotes() -> None:
    text = _fixture("work/failed-context-recovery-20261024/PLAN.md").read_text(encoding="utf-8")
    assert "6529" in text and "5683" in text, "文档必须区分归档记录数与匹配 quote 数"
    assert "口径区分" in text
