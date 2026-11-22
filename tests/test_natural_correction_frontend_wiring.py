"""口语补录与快速订正（自然语言纠错）前端接线的回归测试。

背景：web/index.html 里的 #natural-correction-form 曾在 web/app.js 里完全没有接线
（用户能看到、点了没反应）。本文件把「已接线」这件事钉死：

1. 静态接线：表单 submit → POST /api/events/natural-correction；
   mic 按钮 → webkitSpeechRecognition(zh-CN)，浏览器不支持时隐藏；
   成功后刷新日记；错误文案原样转述。
2. app.js 语法：node --check（有 node 时才跑）。
3. 契约：前端实际会发出的两种请求体（带/不带 job_id + artifact_path）在真实
   server + 真实磁盘上都能被接受并落盘 —— 不 mock 业务逻辑。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from life_logger.local_web import JobStore, create_server

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "web" / "app.js"
INDEX_HTML = REPO_ROOT / "web" / "index.html"
ENDPOINT = "/api/events/natural-correction"


# ---------------------------------------------------------------------------
# 1. 静态接线
# ---------------------------------------------------------------------------


def test_natural_correction_form_elements_exist() -> None:
    """表单、输入框、发送/麦克风按钮、反馈区都要在页面里。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for element_id in (
        "natural-correction-form",
        "natural-correction-input",
        "natural-correction-submit-btn",
        "natural-correction-mic-btn",
        "natural-correction-feedback",
    ):
        assert f'id="{element_id}"' in html, f"缺少元素 #{element_id}"


def test_app_js_wires_form_to_backend() -> None:
    """app.js 必须把表单接到后端，并把 job_id/artifact_path 一起下发。"""
    source = APP_JS.read_text(encoding="utf-8")
    assert ENDPOINT in source, "app.js 没有引用自然语言纠错端点"
    assert re.search(
        r'\$\("natural-correction-form"\)\?\.addEventListener\("submit"',
        source,
    ), "表单 submit 没有接线"
    assert re.search(r"body\.job_id\s*=", source), "请求体没有带上 job_id"
    assert re.search(r"body\.artifact_path\s*=", source), "请求体没有带上 artifact_path"
    assert re.search(r"body\.date\s*=|date:\s*targetDate", source), "请求体没有带上 date"
    assert re.search(
        r"refreshJournal\(",
        source,
    ), "成功后应调用 refreshJournal"


def test_app_js_mic_button_uses_speech_recognition() -> None:
    """mic 按钮接 webkitSpeechRecognition(zh-CN)，不支持时隐藏而不是留死按钮。"""
    source = APP_JS.read_text(encoding="utf-8")
    assert "natural-correction-mic-btn" in source
    assert re.search(
        r"window\.SpeechRecognition\s*\|\|\s*window\.webkitSpeechRecognition",
        source,
    ), "没有探测浏览器语音识别能力"
    assert re.search(r'\.lang\s*=\s*"zh-CN"', source), "语音识别没有指定中文"
    assert re.search(
        r'button\.classList\.add\("hidden"\)',
        source,
    ), "不支持的浏览器里应隐藏 mic 按钮"


def test_app_js_relays_backend_errors() -> None:
    """后端错误（含 events.json 约束）必须写进反馈区，不能被吞掉。"""
    source = APP_JS.read_text(encoding="utf-8")
    assert "natural-correction-feedback" in source
    assert re.search(r"处理失败：", source), "失败时没有把错误写进反馈区"
    assert re.search(r"error\?\.message", source), "没有透传后端错误文案"


def test_app_js_passes_node_syntax_check() -> None:
    """node --check 校验 app.js 语法（本机无 node 时跳过）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，跳过 app.js 语法检查")
    result = subprocess.run(
        [node, "--check", str(APP_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 3. 契约：前端请求体在真实服务上可用
# ---------------------------------------------------------------------------


def _post(base_url: str, payload: dict, *, token: str | None) -> tuple[int, dict]:
    """按浏览器同源写请求的方式发 POST（Origin + CSRF + Bearer）。"""
    headers = {
        "Content-Type": "application/json",
        "Origin": base_url,
        "X-Life-Logger-CSRF": "1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url}{ENDPOINT}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _write_day_artifact(store: JobStore, job_id: str, date: str) -> str:
    """在 job 里铺一份「已处理的当天产物」，形状与真实管线一致。"""
    relative = f"files/feedface/result/{date}/events.json"
    artifact = store.job_path(job_id) / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "events": [
                    {
                        "event_id": "evt_base_coffee",
                        "artifact_path": relative,
                        "categories": ["expense"],
                        "activity_type": "purchase",
                        "title": "买咖啡",
                        "time": {
                            "absolute": None,
                            "relative_seconds": None,
                            "precision": "unknown",
                        },
                        "data": {
                            "expense": {"amount": 15.0, "currency": "CNY", "item": "咖啡"}
                        },
                        "capture_mode": "passive",
                        "source_type": "audio",
                        "confidence": 0.9,
                        "evidence": {"quote": "买咖啡15元"},
                        "review_status": "confirmed",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return relative


def test_frontend_request_bodies_work_against_real_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑一遍前端会发的请求体：ADD（挂当天产物）/ UPDATE / DELETE。

    ⚠️ 必须自洽（hermetic）：`create_server` 的 `config_path` 默认指向仓库真实
    `local-config.json`，而补录路由会从「config 的 deepseek.api_key + secrets.json」构造
    DeepSeek parser。不指定 config_path 的话，这个用例会**真的调线上 LLM**（实测：
    断言 `title == "水"` 因真实模型返回不同实体而失败，且耗时/联网且花钱）。
    这里显式喂一份**无 key** 的临时 config，锁定「离线正则兜底」这条确定性路径。

    【2026-10-15 补充】光喂无 key 的 config 还不够：补录路由最终走的是
    `local_pipeline_env()`，它会把**进程环境变量** `DEEPSEEK_API_KEY` 也算进去。
    从带 key 的 shell 里跑 pytest 时，parser 照样被构造成联网实例（实测 title 变成
    模型爱写的「买水」/「买了一瓶水」，随机红绿）。所以这里同时清掉该环境变量。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    token = "test-token"
    store = JobStore(tmp_path / "web", recover=False)
    hermetic_config = tmp_path / "local-config.json"
    hermetic_config.write_text(
        json.dumps({"deepseek": {"enabled": False}}), encoding="utf-8"
    )
    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=hermetic_config,
        access_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    date = "2026-10-02"

    try:
        job_id = store.create_job()
        artifact_path = _write_day_artifact(store, job_id, date)

        # 1) ADD：挂到当天已有产物上（前端首选形态）
        status, data = _post(
            base_url,
            {
                "text": "记一笔买水4.9元",
                "date": date,
                "job_id": job_id,
                "artifact_path": artifact_path,
            },
            token=token,
        )
        assert status == 200, data
        assert data["status"] == "ok" and data["action"] == "ADD"
        assert data["job_id"] == job_id, "ADD 应该落在前端指定的 job 上"
        manual_path = store.job_path(job_id) / "manual-events.json"
        manual = json.loads(manual_path.read_text(encoding="utf-8"))["events"]
        assert any(
            item["title"] == "水" and item["artifact_path"] == artifact_path
            for item in manual
        ), "补录没有写进 manual-events.json"
        summary = json.loads(
            (store.job_path(job_id) / artifact_path).parent.joinpath(
                "daily_summary.json"
            ).read_text(encoding="utf-8")
        )
        assert summary["date"] == date
        assert summary["expense_total_cny"] == 19.9

        # 2) UPDATE：必须能定位到既有事件（前端不带 target_event_id，靠后端匹配）
        status, data = _post(
            base_url,
            {
                "text": "咖啡不是15是18元",
                "date": date,
                "job_id": job_id,
                "artifact_path": artifact_path,
            },
            token=token,
        )
        assert status == 200, data
        assert data["status"] == "ok" and data["action"] == "UPDATE"
        corrections = json.loads(
            (store.job_path(job_id) / "event-corrections.json").read_text(
                encoding="utf-8"
            )
        )["corrections"]
        record = corrections[f"{artifact_path}::evt_base_coffee"]
        assert record["changes"]["data"]["expense"]["amount"] == 18.0

        # 3) DELETE：标记 rejected，当天汇总重算
        status, data = _post(
            base_url,
            {
                "text": "把咖啡删掉",
                "date": date,
                "job_id": job_id,
                "artifact_path": artifact_path,
            },
            token=token,
        )
        assert status == 200, data
        assert data["status"] == "ok" and data["action"] == "DELETE"
        assert data["correction"]["rejected"] is True
        corrections = json.loads(
            (store.job_path(job_id) / "event-corrections.json").read_text(
                encoding="utf-8"
            )
        )["corrections"]
        assert corrections[f"{artifact_path}::evt_base_coffee"]["rejected"] is True

    finally:
        server.shutdown()
        server.server_close()


def test_add_without_artifact_creates_separate_job(tmp_path: Path) -> None:
    """当天没有可写入产物时，前端的兜底请求（不带 job_id）仍能成功 —— 但要
    知道后端会另建一个任务，且它的产物不在日记扫描目录（files/*）里，
    所以前端必须先找当天产物，找不到才走这条路。"""
    token = "test-token"
    store = JobStore(tmp_path / "web", recover=False)
    server = create_server(host="127.0.0.1", port=0, store=store, access_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        status, data = _post(
            base_url,
            {"text": "记一笔买咖啡15元", "date": "2026-10-02"},
            token=token,
        )
        assert status == 200, data
        assert data["status"] == "ok" and data["action"] == "ADD"
        new_job_id = data["job_id"]
        assert data["artifact_path"] == "2026-10-02/events.json"
        # 产物落在 job 根目录，日记不扫这里（见 list_job_artifacts 只扫 files/*）
        assert store.list_job_artifacts(new_job_id) == []
        assert (store.job_path(new_job_id) / "manual-events.json").is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_update_without_existing_events_json_is_relayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不挂 artifacts 的 UPDATE 会被后端拒绝 —— 这正是前端必须透传的错误文案。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    token = "test-token"
    store = JobStore(tmp_path / "web", recover=False)
    hermetic_config = tmp_path / "local-config.json"
    hermetic_config.write_text(
        json.dumps({"deepseek": {"enabled": False}}), encoding="utf-8"
    )

    from life_logger.natural_correction import DeepSeekTriIntentParser

    server = create_server(
        host="127.0.0.1",
        port=0,
        store=store,
        config_path=hermetic_config,
        access_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        # 路径 A（离线正常 UPDATE）：证据齐全但未挂载 events.json，后端底层抛出错误由前端透传
        monkeypatch.setattr(
            DeepSeekTriIntentParser,
            "parse",
            lambda self, text, base_date=None: {
                "action": "UPDATE",
                "category": "expense",
                "title": "咖啡不是15是",
                "amount": 18.0,
                "old_amount": 15.0,
                "target_entity": "咖啡",
                "amount_review_reason": None,
                "confidence": 0.95,
                "activity_type": "purchase",
            },
        )
        status, data = _post(
            base_url,
            {"text": "咖啡不是15是18元", "date": "2026-10-02"},
            token=token,
        )
        assert status == 400
        assert data["status"] == "error"
        assert "events.json" in data["error"]

        # 路径 B（证据不足分支）：金额证据缺失，后端转入 needs_review
        monkeypatch.setattr(
            DeepSeekTriIntentParser,
            "parse",
            lambda self, text, base_date=None: {
                "action": "UPDATE",
                "category": "expense",
                "title": "咖啡",
                "amount": 18.0,
                "old_amount": 15.0,
                "target_entity": "咖啡",
                "amount_review_reason": "transaction_evidence_missing",
                "confidence": 0.95,
                "activity_type": "purchase",
            },
        )
        status, data = _post(
            base_url,
            {"text": "咖啡不是15是18元", "date": "2026-10-02"},
            token=token,
        )
        assert status == 400
        assert data["status"] == "needs_review"
        assert data["review_reason"] == "transaction_evidence_missing"

        # 路径 C：鉴权失败也要能被前端原样转述
        status, data = _post(
            base_url,
            {"text": "记一笔买咖啡15元", "date": "2026-10-02"},
            token=None,
        )
        assert status == 401
        assert data["error"] == "unauthorized"
    finally:
        server.shutdown()
        server.server_close()
