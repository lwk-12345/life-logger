"""待人工审核（pending_sources）投影与接口的行为测试。

覆盖三件必须守住的事：
1. 按 reason 分类：整批 API 失败（deepseek_batch_failed）默认只给计数；
2. 日期/时间取真实录音时间（watch 段序号 → transcript 行；job → segment_id 命中）；
3. 人工处理台账只写 pending-review-states.json，不动抽取产物。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from life_logger.local_web import JobStore, create_server
from life_logger.pending_review import (
    GROUP_BATCH_FAILURE,
    GROUP_NEEDS_REVIEW,
    STATES_FILENAME,
    PendingReviewError,
    collect_pending_sources,
    group_meta,
    reason_meta,
    set_pending_review_state,
    set_pending_review_states_bulk,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _watch_fixture(root: Path) -> Path:
    """造一个 Watch 会话产物：live-context 里有指令句 + 低置信 + 整批失败三类来源。"""

    session = root / "watch-sessions" / "watch-dev" / "SESSION0001"
    _write_json(
        session / "manifest.json",
        {"started_at": "2026-10-12T15:13:15.091000+00:00", "state": "complete"},
    )
    # watch_000003_000002 → segments/000003/transcript.json 的第 2 行
    _write_json(
        session / "formal-asr" / "segments" / "000003" / "transcript.json",
        [
            {
                "segment_id": "rec_a_seg_0001",
                "absolute_started_at": "2026-10-12T11:30:00+08:00",
            },
            {
                "segment_id": "rec_a_seg_0002",
                "absolute_started_at": "2026-10-12T19:42:00+08:00",
            },
        ],
    )
    artifact = session / "formal-asr" / "live-context" / "context-extraction.json"
    _write_json(
        artifact,
        {
            "schema_version": "context-extraction-v1",
            "status": "degraded",
            "counts": {"formal_events": 1, "pending_events": 0, "pending_sources": 3},
            "pending_sources": [
                {
                    "source_id": "src_directive",
                    "segment_id": "watch_000003_000002",
                    "quote": "全部的流程检查一遍。",
                    "reason": "model_no_event_but_directive",
                    "retryable": True,
                },
                {
                    "source_id": "src_low",
                    "segment_id": "watch_000003_000002",
                    "quote": "我在想是买一个 Cloud 的扣子还是买一个 Google 的 Ultra?",
                    "source_label": "self",
                    "reason": "low_confidence",
                    "retryable": True,
                },
                {
                    "source_id": "src_dump",
                    "segment_id": "watch_000009_000001",
                    "quote": "我今天该练什么了呀?",
                    "source_label": "self",
                    "reason": "deepseek_batch_failed",
                    "retryable": True,
                },
            ],
            "source_outcomes": [
                {
                    "source_id": "src_directive",
                    "segment_id": "watch_000003_000002",
                    "recording_id": "watch_SESSION0001",
                    "speaker_label": "self",
                    "semantic_status": "pending",
                    "reason": "model_no_event_but_directive",
                    "event_refs": [],
                }
            ],
        },
    )
    return artifact


def _job_fixture(root: Path) -> Path:
    """造一个任务产物：segment_id 能直接命中 result/<date>/transcript.json。"""

    job = root / "jobs" / "job-20261005-203915-c9f621fc" / "files" / "hash1"
    _write_json(
        job / "result" / "2026-10-05" / "transcript.json",
        [
            {
                "segment_id": "rec_funasr_nano_seg_000001",
                "absolute_started_at": "2026-10-05T02:06:44.520000+08:00",
                "text": "记录一下抽根烟。",
            }
        ],
    )
    artifact = job / "result" / "context-extraction.json"
    _write_json(
        artifact,
        {
            "pending_sources": [
                {
                    "source_id": "src_job_record",
                    "segment_id": "rec_funasr_nano_seg_000001",
                    "quote": "记录一下抽根烟。",
                    "source_label": "self",
                    "reason": "invalid_category",
                    "retryable": True,
                }
            ]
        },
    )
    return artifact


def test_collect_groups_by_reason_and_keeps_batch_dump_collapsed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local-web"
    _watch_fixture(root)
    _job_fixture(root)

    payload = collect_pending_sources(root)

    assert payload["ok"] is True
    assert payload["schema"] == "pending-sources-v1"
    assert payload["totals"]["all"] == 4
    assert payload["totals"]["needs_review"] == 2
    assert payload["totals"]["batch_failure"] == 1
    groups = {group["key"]: group for group in payload["groups"]}
    assert set(groups) == {GROUP_NEEDS_REVIEW, "extraction_gap", GROUP_BATCH_FAILURE}
    assert [item["source_id"] for item in groups[GROUP_NEEDS_REVIEW]["items"]] == [
        "src_directive",
        "src_low",
    ]
    assert [item["source_id"] for item in groups["extraction_gap"]["items"]] == [
        "src_job_record"
    ]
    # 整批失败默认只给计数，不给条目 —— 否则 3600+ 条垃圾会把真条目淹掉
    assert groups[GROUP_BATCH_FAILURE]["collapsed"] is True
    assert groups[GROUP_BATCH_FAILURE]["count"] == 1
    assert groups[GROUP_BATCH_FAILURE]["items"] == []
    assert groups[GROUP_BATCH_FAILURE]["items_truncated"] == 1

    expanded = collect_pending_sources(root, include_collapsed=True)
    dump = [g for g in expanded["groups"] if g["key"] == GROUP_BATCH_FAILURE][0]
    assert [item["source_id"] for item in dump["items"]] == ["src_dump"]


def test_collect_uses_real_recording_time_not_upload_time(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _watch_fixture(root)
    _job_fixture(root)

    payload = collect_pending_sources(root)
    items = {
        item["source_id"]: item
        for group in payload["groups"]
        for item in group["items"]
    }
    # watch：段序号 000003 的第 2 行 = 19:42（manifest 的 started_at 是 23:13 北京，
    # 说明这里取的是真实录音时间而不是会话/上传时间）
    assert items["src_directive"]["date"] == "2026-10-12"
    assert items["src_directive"]["time"] == "19:42"
    assert (
        items["src_directive"]["time_source"]
        == "formal-asr/segments/000003/transcript.json#row2"
    )
    # job：segment_id 直接命中 result/<date>/transcript.json
    assert items["src_job_record"]["date"] == "2026-10-05"
    assert items["src_job_record"]["time"] == "02:06"
    assert items["src_job_record"]["time_source"].endswith(
        "#rec_funasr_nano_seg_000001"
    )
    assert items["src_job_record"]["artifact"]["kind"] == "job"
    assert items["src_directive"]["outcome"]["semantic_status"] == "pending"


def test_collect_dedupes_same_source_across_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _watch_fixture(root)
    job_artifact = _job_fixture(root)
    # 同一个 source_id 在两个产物里各出现一次 → 只保留一份（watch-session 优先）
    duplicated = json.loads(job_artifact.read_text(encoding="utf-8"))
    duplicated["pending_sources"].append(
        {
            "source_id": "src_directive",
            "segment_id": "watch_000003_000002",
            "quote": "全部的流程检查一遍。",
            "reason": "model_no_event_but_directive",
            "retryable": True,
        }
    )
    _write_json(job_artifact, duplicated)

    payload = collect_pending_sources(root)
    assert payload["totals"]["all"] == 4
    directive = [
        item
        for group in payload["groups"]
        for item in group["items"]
        if item["source_id"] == "src_directive"
    ]
    assert len(directive) == 1
    assert directive[0]["artifact"]["kind"] == "watch-session"


def test_collect_date_filter_and_validation(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _watch_fixture(root)
    _job_fixture(root)

    scoped = collect_pending_sources(root, date="2026-10-12")
    assert scoped["totals"]["all"] == 4  # 全库口径不受 date 影响
    assert scoped["totals"]["scoped"] == 3
    assert {
        item["source_id"]
        for group in scoped["groups"]
        for item in group["items"]
    } == {"src_directive", "src_low"}
    assert [g for g in scoped["groups"] if g["key"] == GROUP_BATCH_FAILURE][0][
        "count"
    ] == 1

    with pytest.raises(PendingReviewError):
        collect_pending_sources(root, date="12-10-2026")


def test_needs_review_hint_states_no_auto_promotion() -> None:
    hint = group_meta(GROUP_NEEDS_REVIEW)["hint"]
    # 组级 hint 必须如实说明「不会自动重新抽取/晋升」，不能诱导用户重说。
    assert "不会从来源条目自动重新抽取" in hint
    assert "重说" not in hint


def test_reason_meta_marks_unknown_reason_as_needs_review() -> None:
    assert reason_meta("model_no_event_but_directive")["group"] == GROUP_NEEDS_REVIEW
    assert reason_meta("deepseek_batch_failed")["group"] == GROUP_BATCH_FAILURE
    unknown = reason_meta("something_new")
    assert unknown["group"] == "other"
    assert "something_new" in unknown["label"]


def _serve(store: JobStore, config_path: Path):
    server = create_server(
        host="127.0.0.1", port=0, store=store, config_path=config_path
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    origin = "/".join(url.split("/")[:3])
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "X-Life-Logger-CSRF": "1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_pending_sources_endpoint_groups_and_collapses(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    _watch_fixture(store.root)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server, thread, base = _serve(store, config_path)
    try:
        payload = _get_json(f"{base}/api/pending-sources")
        assert payload["ok"] is True
        groups = {group["key"]: group for group in payload["groups"]}
        assert groups[GROUP_NEEDS_REVIEW]["count"] == 2
        assert groups[GROUP_BATCH_FAILURE]["items"] == []

        expanded = _get_json(f"{base}/api/pending-sources?include_collapsed=1&limit=1")
        dump = [g for g in expanded["groups"] if g["key"] == GROUP_BATCH_FAILURE][0]
        assert [item["source_id"] for item in dump["items"]] == ["src_dump"]

        scoped = _get_json(f"{base}/api/pending-sources?date=2026-10-11")
        assert scoped["totals"]["scoped"] == 0

        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/api/pending-sources?date=nope", timeout=30)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_pending_sources_status_endpoint_writes_only_ledger(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    artifact = _watch_fixture(store.root)
    before = artifact.read_bytes()
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server, thread, base = _serve(store, config_path)
    try:
        status, result = _post_json(
            f"{base}/api/pending-sources/status",
            {"source_id": "src_directive", "status": "ignored", "note": "测试"},
        )
        assert status == 200
        assert result["ok"] is True
        assert result["counts"] == {"ignored": 1, "handled": 0}

        ledger = json.loads((store.root / STATES_FILENAME).read_text(encoding="utf-8"))
        assert ledger["schema"] == "pending-review-states-v1"
        assert ledger["records"]["src_directive"]["status"] == "ignored"
        # 抽取产物必须原样不动：忽略只是台账，不是删数据
        assert artifact.read_bytes() == before

        visible = _get_json(f"{base}/api/pending-sources")
        assert all(
            item["source_id"] != "src_directive"
            for group in visible["groups"]
            for item in group["items"]
        )
        assert visible["review_states"]["counts"]["ignored"] == 1

        with_resolved = _get_json(f"{base}/api/pending-sources?include_resolved=1")
        resolved = [
            item
            for group in with_resolved["groups"]
            for item in group["items"]
            if item["source_id"] == "src_directive"
        ]
        assert resolved and resolved[0]["state"] == "ignored"

        status, result = _post_json(
            f"{base}/api/pending-sources/status",
            {"source_id": "src_directive", "status": "open"},
        )
        assert status == 200 and result["total"] == 0

        status, result = _post_json(
            f"{base}/api/pending-sources/status",
            {"source_id": "src_directive", "status": "bogus"},
        )
        assert status == 400 and "ignored" in result["error"]

        status, result = _post_json(
            f"{base}/api/pending-sources/status", {"status": "ignored"}
        )
        assert status == 400 and result["error"] == "缺少 source_id"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_set_review_state_tolerates_missing_ledger(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    root.mkdir(parents=True)
    result = set_pending_review_state(root, source_id="src_x", status="handled")
    assert result["total"] == 1
    assert json.loads((root / STATES_FILENAME).read_text(encoding="utf-8"))["records"][
        "src_x"
    ]["status"] == "handled"
    with pytest.raises(PendingReviewError):
        set_pending_review_state(root, source_id="", status="handled")


def _add_other_date_needs_review(root: Path) -> None:
    """在同一 job 产物里补一条 10-05 的 needs_review，验证日期隔离。"""

    artifact = (
        root
        / "jobs"
        / "job-20261005-203915-c9f621fc"
        / "files"
        / "hash1"
        / "result"
        / "context-extraction.json"
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["pending_sources"].append(
        {
            "source_id": "src_job_review",
            "segment_id": "rec_funasr_nano_seg_000001",
            "quote": "记录一下别的事。",
            "reason": "low_confidence",
            "retryable": True,
        }
    )
    _write_json(artifact, payload)


def test_bulk_status_scoped_to_open_needs_review_date(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    artifact = _watch_fixture(root)
    _job_fixture(root)
    _add_other_date_needs_review(root)
    before = artifact.read_bytes()

    result = set_pending_review_states_bulk(
        root,
        date="2026-10-12",
        group=GROUP_NEEDS_REVIEW,
        status="ignored",
        note="批量清理测试口语",
    )

    assert result == {
        "ok": True,
        "count": 2,
        "date": "2026-10-12",
        "group": GROUP_NEEDS_REVIEW,
        "status": "ignored",
    }
    ledger = json.loads((root / STATES_FILENAME).read_text(encoding="utf-8"))
    assert set(ledger["records"]) == {"src_directive", "src_low"}
    assert all(row["status"] == "ignored" for row in ledger["records"].values())
    assert all(row["note"] == "批量清理测试口语" for row in ledger["records"].values())
    # 只写台账，抽取产物原样保留
    assert artifact.read_bytes() == before

    expanded = collect_pending_sources(
        root, include_resolved=True, include_collapsed=True
    )
    items = {
        item["source_id"]: item
        for group in expanded["groups"]
        for item in group["items"]
    }
    assert items["src_directive"]["state"] == "ignored"
    assert items["src_low"]["state"] == "ignored"
    # 同日整批失败、其他日期的 needs_review、同日 extraction_gap 都不许被顺手改掉
    assert items["src_dump"]["state"] == "open"
    assert items["src_job_review"]["state"] == "open"
    assert items["src_job_record"]["state"] == "open"

    # 已经处理过的不是 open，不重复计数，也不再写一次
    again = set_pending_review_states_bulk(
        root, date="2026-10-12", status="ignored", note="第二次"
    )
    assert again["count"] == 0
    assert json.loads((root / STATES_FILENAME).read_text(encoding="utf-8"))[
        "records"
    ]["src_directive"]["note"] == "批量清理测试口语"


def test_bulk_status_rejects_invalid_inputs(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _watch_fixture(root)

    with pytest.raises(PendingReviewError):
        set_pending_review_states_bulk(root, date="", status="ignored")
    with pytest.raises(PendingReviewError):
        set_pending_review_states_bulk(root, date="12-10-2026", status="ignored")
    with pytest.raises(PendingReviewError):
        set_pending_review_states_bulk(
            root, date="2026-10-12", group=GROUP_BATCH_FAILURE, status="ignored"
        )
    with pytest.raises(PendingReviewError):
        set_pending_review_states_bulk(root, date="2026-10-12", status="open")
    with pytest.raises(PendingReviewError):
        set_pending_review_states_bulk(root, date="2026-10-12", status="bogus")
    assert not (root / STATES_FILENAME).exists()


def test_bulk_status_endpoint_and_validation(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    _watch_fixture(store.root)
    config_path = tmp_path / "local-config.json"
    config_path.write_text("{}", encoding="utf-8")
    server, thread, base = _serve(store, config_path)
    try:
        status, result = _post_json(
            f"{base}/api/pending-sources/status-bulk",
            {
                "date": "2026-10-12",
                "group": "needs_review",
                "status": "ignored",
                "note": "批量清理测试口语",
            },
        )
        assert status == 200
        assert result == {
            "ok": True,
            "count": 2,
            "date": "2026-10-12",
            "group": "needs_review",
            "status": "ignored",
        }

        visible = _get_json(f"{base}/api/pending-sources")
        assert all(
            item["source_id"] not in {"src_directive", "src_low"}
            for group in visible["groups"]
            for item in group["items"]
        )
        assert visible["review_states"]["counts"]["ignored"] == 2

        status, result = _post_json(
            f"{base}/api/pending-sources/status-bulk",
            {"date": "2026-10-12", "status": "ignored"},
        )
        assert status == 200 and result["count"] == 0

        for payload in (
            {"status": "ignored"},
            {"date": "nope", "status": "ignored"},
            {"date": "2026-10-12", "group": "batch_failure", "status": "ignored"},
            {"date": "2026-10-12", "status": "open"},
        ):
            status, result = _post_json(
                f"{base}/api/pending-sources/status-bulk", payload
            )
            assert status == 400 and result["ok"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_batch_failure_archive_is_not_counted_as_user_ignored(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _watch_fixture(root)
    set_pending_review_state(
        root, source_id="src_dump", status="ignored",
        note="archived: 历史整批 API 失败",
    )
    set_pending_review_state(
        root, source_id="src_directive", status="ignored", note="人工忽略"
    )

    payload = collect_pending_sources(
        root, include_resolved=True, include_collapsed=True
    )
    items = {
        item["source_id"]: item
        for group in payload["groups"]
        for item in group["items"]
    }

    assert payload["totals"]["resolved"] == 2
    assert payload["totals"]["resolved_ignored_by_user"] == 1
    assert payload["totals"]["resolved_failed_archived"] == 1
    assert payload["totals"]["batch_failure_archived"] == 1
    assert payload["totals"]["batch_failure_open"] == 0
    assert items["src_dump"]["state"] == "failed_archived"
    assert items["src_dump"]["ledger_state"] == "ignored"
    assert items["src_dump"]["resolution"] == "failed_archived"
    assert items["src_directive"]["state"] == "ignored"
    assert items["src_directive"]["resolution"] == "ignored_by_user"
    assert payload["review_states"]["counts"] == {
        "ignored": 1,
        "handled": 0,
        "failed_archived": 1,
    }
    assert payload["review_states"]["ledger_counts"] == {
        "ignored": 2,
        "handled": 0,
    }


def test_batch_failure_retryable_comes_from_failure_metadata(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_json(
        root / "replays" / "retryable" / "context-extraction.json",
        {
            "pending_sources": [
                {
                    "source_id": "src_timeout",
                    "quote": "原文一",
                    "reason": "deepseek_batch_failed",
                    "retryable": False,
                    "failure_kind": "timeout",
                    "http_status": None,
                },
                {
                    "source_id": "src_auth",
                    "quote": "原文二",
                    "reason": "deepseek_batch_failed",
                    "retryable": True,
                    "failure_kind": "http_error",
                    "http_status": 401,
                },
            ]
        },
    )

    payload = collect_pending_sources(
        root, include_collapsed=True, include_resolved=True
    )
    items = {
        item["source_id"]: item
        for group in payload["groups"]
        for item in group["items"]
    }

    assert items["src_timeout"]["retryable"] is True
    assert items["src_timeout"]["retryable_source"] == "failure_metadata"
    assert items["src_auth"]["retryable"] is False
    assert items["src_auth"]["retryable_source"] == "failure_metadata"


def test_pending_retryable_falls_back_to_batch_failure_metadata(tmp_path: Path) -> None:
    root = tmp_path / "local-web"
    _write_json(
        root / "replays" / "batch-meta" / "context-extraction.json",
        {
            "pending_sources": [
                {
                    "source_id": "src_old_timeout",
                    "quote": "原文",
                    "reason": "deepseek_batch_failed",
                    "retryable": False,
                }
            ],
            "batches": [
                {
                    "source_ids": ["src_old_timeout"],
                    "status": "failed",
                    "failure_kind": "timeout",
                    "http_status": None,
                    "retryable_transient": False,
                }
            ],
        },
    )

    payload = collect_pending_sources(root, include_collapsed=True)
    item = [
        item
        for group in payload["groups"]
        for item in group["items"]
        if item["source_id"] == "src_old_timeout"
    ][0]

    assert item["retryable"] is True
    assert item["retryable_source"] == "failure_metadata"


# ---------------------------------------------------------------------------
# Q165 / 2026-10-24 P1：legacy `retryable=true` 必须 fail-closed
# ---------------------------------------------------------------------------
def _retry_matrix_root(tmp_path: Path, pending: list[dict], batches: list[dict] | None = None) -> Path:
    root = tmp_path / "local-web"
    payload: dict = {"pending_sources": pending}
    if batches is not None:
        payload["batches"] = batches
    _write_json(root / "replays" / "matrix" / "context-extraction.json", payload)
    return root


def _collect_items(root: Path) -> dict[str, dict]:
    payload = collect_pending_sources(root, include_collapsed=True, include_resolved=True)
    return {
        item["source_id"]: item
        for group in payload["groups"]
        for item in group["items"]
    }


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        ("timeout", None, True),
        ("connection_error", None, True),
        ("http_error", 503, True),
        ("http_error", 429, True),
        ("http_error", 408, True),
        ("http_error", 401, False),
        ("http_error", 403, False),
        ("http_error", 402, False),
        ("schema_error", None, False),     # 未知 kind
        ("http_error", None, False),       # 缺 status
    ],
)
def test_retryable_matrix_from_structured_metadata(tmp_path, kind, status, expected):
    root = _retry_matrix_root(tmp_path, [{
        "source_id": "src_x", "quote": "原文", "reason": "deepseek_batch_failed",
        "failure_kind": kind, "http_status": status, "retryable": not expected,
    }])
    item = _collect_items(root)["src_x"]
    assert item["retryable"] is expected
    assert item["retryable_source"] == "failure_metadata"
    assert item["retryable_warning"] is None


def test_legacy_retryable_true_without_metadata_is_fail_closed(tmp_path):
    """只有 retryable=true、没有 kind/status/batches → 不许自动重跑，并留告警。"""
    root = _retry_matrix_root(tmp_path, [{
        "source_id": "src_legacy_true", "quote": "原文",
        "reason": "deepseek_batch_failed", "retryable": True,
    }])
    item = _collect_items(root)["src_legacy_true"]
    assert item["retryable"] is False, "legacy 布尔不得成为重试依据"
    assert item["retryable_source"] == "legacy_unverified"
    assert item["retryable_legacy_value"] is True
    assert "fail-closed" in (item["retryable_warning"] or "")


def test_legacy_retryable_false_stays_false_without_warning(tmp_path):
    root = _retry_matrix_root(tmp_path, [{
        "source_id": "src_legacy_false", "quote": "原文",
        "reason": "deepseek_batch_failed", "retryable": False,
    }])
    item = _collect_items(root)["src_legacy_false"]
    assert item["retryable"] is False
    assert item["retryable_source"] == "legacy"
    assert item["retryable_warning"] is None


def test_legacy_true_cannot_override_batch_metadata_401(tmp_path):
    """pending 自身没写 kind/status，但 batches 说 401 → 必须 False（元数据优先）。"""
    root = _retry_matrix_root(
        tmp_path,
        [{"source_id": "src_b", "quote": "原文", "reason": "deepseek_batch_failed", "retryable": True}],
        batches=[{"source_ids": ["src_b"], "status": "failed",
                  "failure_kind": "http_error", "http_status": 401,
                  "retryable_transient": True}],
    )
    item = _collect_items(root)["src_b"]
    assert item["retryable"] is False
    assert item["retryable_source"] == "failure_metadata"


def test_legacy_retryable_helpers_are_explicitly_fail_closed():
    from life_logger.context_auto_retry import (
        LEGACY_RETRYABLE_WARNING,
        legacy_retryable_is_trusted,
        legacy_retryable_warning,
    )
    assert legacy_retryable_is_trusted(True) is False
    assert legacy_retryable_is_trusted(False) is False
    assert legacy_retryable_warning(True) == LEGACY_RETRYABLE_WARNING
    assert legacy_retryable_warning(False) is None
