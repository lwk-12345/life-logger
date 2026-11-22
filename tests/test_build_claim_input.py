"""Tests for the safe agent-board -> claim.json builder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_claim_input as b
import verify_claim as v


def _task(task_id: str, state: str = "delivered", delivery: str = "证据：`work/a/report.md`") -> dict:
    return {
        "id": task_id,
        "owner": "1",
        "state": state,
        "last_dispatched_at": "2026-10-23T10:00:00+0800",
        "delivery": delivery,
    }


def test_dry_run_builds_schema_without_writing(tmp_path: Path) -> None:
    output = tmp_path / "agent-board" / "claim.json"
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"tasks": [_task("Q1")]}), encoding="utf-8")
    assert b.main(["--queue", str(queue), "--output", str(output), "--task", "Q1"]) == 0
    assert not output.exists()


def test_write_is_atomic_and_filters_delivered_only(tmp_path: Path) -> None:
    output = tmp_path / "agent-board" / "claim.json"
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"tasks": [
        _task("Q1"),
        _task("Q2", state="queued"),
        _task("Q3", state="done"),
    ]}), encoding="utf-8")
    assert b.main(["--queue", str(queue), "--output", str(output), "--task", "Q1", "--task", "Q2", "--task", "Q3", "--write"]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "claim-input-v1"
    assert payload["source"]["task_ids"] == ["Q1", "Q3"]
    assert "Q2" not in payload["claim"]
    assert output.stat().st_size > 0


def test_session_filter_and_missing_delivery_error(tmp_path: Path) -> None:
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"tasks": [
        _task("Q1", delivery="session AAC382B0 证据"),
        _task("Q2", delivery="session OTHER 证据"),
    ]}), encoding="utf-8")
    payload = b.build_claim(b._read_queue(queue), session="AAC382B0")
    assert payload["source"]["task_ids"] == ["Q1"]
    queue.write_text(json.dumps({"tasks": [_task("Q1", delivery="")]}), encoding="utf-8")
    try:
        b.build_claim(b._read_queue(queue))
    except ValueError as error:
        assert "delivery" in str(error)
    else:
        raise AssertionError("missing delivery must fail")


def test_missing_evidence_is_not_converted_to_pass(tmp_path: Path) -> None:
    claim = b.build_claim([_task("Qno", delivery="测试 999 全绿")])
    report = v.verify(claim, root=tmp_path, run_commands=False)
    assert report["exit_code"] != v.EXIT_OK
    assert all(item["verdict"] != v.PASS for item in report["assertions"])

def test_generated_claim_has_no_pass_expectations(tmp_path: Path) -> None:
    """expected 只描述「要检查什么」，绝不预先写 PASS/verdict。"""
    payload = b.build_claim([_task("Q1", delivery="已写入 `work/a/report.md`")])
    assert payload["claim"].startswith("### Q1")
    for item in payload["expected"]:
        assert "verdict" not in item and "status" not in item
        assert item["type"] in {"path_exists", "test_count", "amount_equals", "api_reachable"}


# ---------------------------------------------------------------------------
# 返工（2026-10-23）：端点/状态词不得当路径；显式证据才生成断言
# ---------------------------------------------------------------------------
Q56_STYLE = """**异常覆盖**
- `/api/version` 单独捕获，返回 200 静态启动信息。
- `/api/tasks` 保留 200 + openclaw.degraded/retry_after。
- `/api/jobs`、`/api/watch-reception`、`/api/health` 走统一 503。

**202：前端识别 pending/computing**；200 + degraded/snapshot_busy 标降级。

**测试**
- `tests/test_control_endpoints_timeout.py` **16 passed**。
- 证据：`work/control-busy-errors-20261023/BUILD.md`、`tests.txt`。
"""


def test_endpoints_and_state_tokens_never_become_path_assertions() -> None:
    payload = b.build_claim([_task("Q56", delivery=Q56_STYLE)])
    paths = [item.get("path", "") for item in payload["expected"] if item.get("type") == "path_exists"]
    joined = " ".join(paths)
    assert "/api/" not in joined, paths
    for token in ("openclaw.degraded", "pending/computing", "degraded/snapshot_busy", "retry_after"):
        assert token not in joined, (token, paths)


def test_real_evidence_paths_are_asserted_and_fake_ones_are_left_to_fail() -> None:
    payload = b.build_claim([_task("Q56", delivery=Q56_STYLE)])
    paths = [item["path"] for item in payload["expected"] if item.get("type") == "path_exists"]
    assert any(p.endswith("tests/test_control_endpoints_timeout.py") for p in paths)
    assert any(p.endswith("work/control-busy-errors-20261023/BUILD.md") for p in paths)
    # 「证据：…、tests.txt」是明确声明的证据，但文件不存在 → 必须保留断言让它 FAIL
    assert any(p.endswith("tests.txt") for p in paths)


def test_bare_filename_reference_without_evidence_cue_is_not_a_path() -> None:
    payload = b.build_claim([_task("Q9", delivery="`local_web.py` 控制层新增 except 分支。")])
    paths = [item.get("path", "") for item in payload["expected"] if item.get("type") == "path_exists"]
    assert not any(p.endswith("local_web.py") for p in paths), paths


def test_test_count_without_command_is_unverifiable_but_present() -> None:
    payload = b.build_claim([_task("Q56", delivery=Q56_STYLE)])
    counts = [item for item in payload["expected"] if item.get("type") == "test_count"]
    assert counts and all(item.get("command") is None for item in counts)
    assert {item["claimed_passed"] for item in counts} == {16}


def test_api_reachable_only_when_command_and_status_given() -> None:
    payload = b.build_claim([_task(
        "Qapi",
        delivery="`curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8765/api/version` 期望 200",
    )])
    apis = [item for item in payload["expected"] if item.get("type") == "api_reachable"]
    assert apis and apis[0]["endpoint"] == "/api/version" and apis[0]["expect_status"] == 200
