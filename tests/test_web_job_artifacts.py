"""任务产物接口 + reminders 同步接线的回归测试。

- `/api/jobs/<id>/artifacts`：partial/failed 的 job 也按磁盘实况报产物
  （2026-10-09 实测：results 空数组 / 裸日期，但 daily_summary.json 已在磁盘，
  前端旧逻辑于是把已产出的事件判成「尚未形成正式事件」）。
- `sync_local_reminders`：OpenClaw 批次必须带着「上游权威」身份进入 sync_tasks，
  上游已消失的条目才会被过期/清掉。
"""

import json
import threading
import urllib.request
from pathlib import Path

from life_logger.local_web import JobStore, create_server

ROOT = Path(__file__).resolve().parents[1]


def _write_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _openclaw_row(task_id: str, title: str) -> dict:
    return {
        "task_key": task_id,
        "title": title,
        "status": "pending",
        "review_status": "confirmed",
        "source_type": "manual",
        "openclaw_id": task_id,
        "openclaw_status": "pending",
    }


def test_job_artifacts_endpoint_reports_disk_truth_for_partial_job(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    job_id = "job-20261009-221347-aba53854"
    file_id = (
        "7527801d8a288cfe3b7d3548b5dd5540cf3dce5bc75c7dfdc5d3537e16587f96"
    )
    job_dir = store.jobs_dir / job_id
    _write_file(
        job_dir / "status.json",
        json.dumps(
            {
                "job_id": job_id,
                "status": "partial",
                "results": [],
                "files": [{"file_id": file_id, "results": [], "status": "failed"}],
            }
        ),
    )
    _write_file(
        job_dir / "files" / file_id / "result" / "2026-10-09" / "daily_summary.json",
        json.dumps({"date": "2026-10-09", "timeline": []}),
    )
    _write_file(
        job_dir / "files" / file_id / "result" / "2026-10-09" / "events.json",
        json.dumps([]),
    )
    _write_file(
        job_dir / "files" / file_id / "result" / "unknown-review.json",
        json.dumps([]),
    )

    server = create_server(host="127.0.0.1", port=0, store=store)
    server._openclaw_reminder_tasks = lambda: []  # type: ignore[method-assign]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/jobs/{job_id}/artifacts"
            ).read()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    artifacts = payload["artifacts"]
    assert f"files/{file_id}/result/2026-10-09/daily_summary.json" in artifacts
    assert f"files/{file_id}/result/2026-10-09/events.json" in artifacts
    assert f"files/{file_id}/result/unknown-review.json" in artifacts
    # 扫描只读：不改写 status.json 的 results
    stored = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert stored["results"] == []


def test_app_js_loads_journal_entries_based_on_artifacts_not_status() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    # 旧的「非 completed 整段跳过」判据已删除 —— partial/failed 也可能已产出事件
    assert 'if (job.status !== "completed") continue;' not in app
    # 新增：产物目录扫描 + 「有产物就加载」的宽判据
    assert "async function scanJobArtifactPaths(job)" in app
    assert "function isDailySummaryArtifact(path)" in app
    assert "`/api/jobs/${encodeURIComponent(job.job_id)}/artifacts`" in app
    assert "if (!summaryPaths.length && (job.files || []).length && jobHistoryLocalDate(job) === focusDate)" in app
    # results 白名单不再静默丢弃磁盘上真实存在的产物
    assert "if (!availablePaths.includes(path)) return null;" not in app
    assert '{silent: true, operation: "读取日记产物"}' in app
    # 确实无产物的 job 依旧跳过（summaryPaths 为空 → 不产出条目）
    assert "summaryPaths = scanned.filter(isDailySummaryArtifact);" in app


def test_sync_local_reminders_prunes_openclaw_orphans_and_persists_marker(
    tmp_path: Path,
) -> None:
    store = JobStore(tmp_path / "local-web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store)
    try:
        server._openclaw_reminder_tasks = lambda: [  # type: ignore[method-assign]
            _openclaw_row("oc_task_keep", "接入苹果 Health 数据"),
            _openclaw_row("oc_task_gone", "【语音自查】测试：查询北京今天天气"),
        ]
        server.sync_local_reminders()

        server._openclaw_reminder_tasks = lambda: [  # type: ignore[method-assign]
            _openclaw_row("oc_task_keep", "接入苹果 Health 数据")
        ]
        server.sync_local_reminders()

        reminders = json.loads(
            (store.root / "reminders.json").read_text(encoding="utf-8")
        )["reminders"]
        assert reminders["oc_task_keep"]["openclaw_id"] == "oc_task_keep"
        gone = reminders["oc_task_gone"]
        assert gone["status"] == "dismissed"
        assert gone["dismissed_reason"] == "upstream_removed"

        # 下一轮：clean_invalid_reminders 把标记条目清出文件
        server.sync_local_reminders()
        reminders = json.loads(
            (store.root / "reminders.json").read_text(encoding="utf-8")
        )["reminders"]
        assert "oc_task_gone" not in reminders
        assert "oc_task_keep" in reminders
    finally:
        server.server_close()
