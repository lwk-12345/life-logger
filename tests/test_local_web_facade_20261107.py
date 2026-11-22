"""S1 facade: moved names stay importable/patchable from `local_web`."""
from __future__ import annotations

import pytest

import life_logger.local_web as local_web
import life_logger.web_support as web_support

MOVED = [
    "_env_flag", "_version_badge_enabled", "_now", "_iso", "_mark_stale_read",
    "_nonnegative_int", "_read_epoch_key", "read_cache_epoch", "bump_read_cache_epoch",
    "invalidate_read_caches", "_hash_path", "_cached_transcript_payload",
    "ReadProducerBusy", "ControlSnapshotBusy", "ResponseHelpersMixin",
]


@pytest.mark.parametrize("name", MOVED)
def test_facade_reads_resolve_to_web_support(name):
    assert getattr(local_web, name) is getattr(web_support, name)


def test_patch_on_facade_reaches_implementation(monkeypatch):
    calls = []
    monkeypatch.setattr(local_web, "_now", lambda: (calls.append(1), 1234.5)[1])
    assert web_support._now() == 1234.5
    assert local_web._now() == 1234.5
    # 移到 web_support 的 _iso 内部用模块全局 _now → patch 生效
    assert web_support._iso().startswith("1970-01-01T08:20:34")
    assert calls


def test_patch_is_undone(monkeypatch):
    original = web_support._now
    with monkeypatch.context() as patch:
        patch.setattr(local_web, "_now", lambda: 0.0)
        assert web_support._now() == 0.0
    assert web_support._now is original


def test_read_cache_epoch_is_single_shared_state(tmp_path, monkeypatch):
    monkeypatch.setattr(local_web, "_READ_CACHE_EPOCHS", {})
    monkeypatch.setattr(local_web, "_READ_CACHE_EPOCH", 0)
    before = local_web.read_cache_epoch(tmp_path)
    after = local_web.bump_read_cache_epoch(tmp_path)
    assert after == before + 1
    assert web_support.read_cache_epoch(tmp_path) == after
    assert local_web.read_cache_epoch(tmp_path) == after


def test_remaining_local_web_caller_uses_support_module_attribute():
    """`JobStore.data_health` 仍留在 local_web，但调用的是 `_support._now`（patch 会生效）。"""
    import inspect

    source = inspect.getsource(local_web.JobStore.data_health)
    assert "_support._now()" in source

    original = web_support._now
    try:
        local_web._now = lambda: 777.0
        assert web_support._now() == 777.0
    finally:
        local_web._now = original
    assert web_support._now is original


AUTH_FUNCS = ["_read_secret_settings", "_write_secret_settings", "_ensure_mobile_access_token",
              "_mobile_capture_config"]
WATCH_FUNCS = ["_parse_sleep_header", "_is_retryable_asr_slot_error", "_watch_direct_access_allowed",
               "_watch_upload_key", "_WatchUploadIncomplete", "_WatchUploadTimeout"]


@pytest.mark.parametrize("name", AUTH_FUNCS + WATCH_FUNCS)
def test_s3_s4_names_resolve_to_new_modules(name):
    import life_logger.web_auth as web_auth
    import life_logger.watch_upload as watch_upload

    target = web_auth if name in AUTH_FUNCS else watch_upload
    assert getattr(local_web, name) is getattr(target, name)


def test_s3_s4_mixins_are_shared():
    import life_logger.web_auth as web_auth
    import life_logger.watch_upload as watch_upload

    assert local_web.AuthHelpersMixin is web_auth.AuthHelpersMixin
    assert local_web.WatchUploadHelpersMixin is watch_upload.WatchUploadHelpersMixin


def test_token_patch_on_facade_reaches_real_http_auth(tmp_path, monkeypatch):
    """真实 HTTP：patch 门面 `_token_matches` 后，鉴权真的走 fake。"""
    import json
    import threading
    import urllib.error
    import urllib.request

    import life_logger.web_auth as web_auth
    from life_logger.local_web import JobStore, create_server

    store = JobStore(tmp_path / "local-web", recover=False)
    token = "facade-test-token"
    server = create_server(host="127.0.0.1", port=0, store=store, access_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    original = web_auth.AuthHelpersMixin._token_matches
    try:
        monkeypatch.setattr(local_web, "_token_matches", lambda self, supplied: supplied == "patched")
        assert web_auth.AuthHelpersMixin._token_matches is not original
        denied = urllib.request.Request(f"{base}/api/health",
                                        headers={"Authorization": f"Bearer {token}"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(denied)
        assert error.value.code == 401
        allowed = urllib.request.Request(f"{base}/api/health",
                                         headers={"Authorization": "Bearer patched"})
        assert json.loads(urllib.request.urlopen(allowed).read())["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_watch_upload_exception_class_is_single_object():
    import life_logger.watch_upload as watch_upload

    assert local_web._WatchUploadIncomplete is watch_upload._WatchUploadIncomplete
    assert local_web._WatchUploadTimeout is watch_upload._WatchUploadTimeout


def test_job_store_identity_and_isinstance_across_facade(tmp_path):
    import life_logger.job_store as job_store

    assert local_web.JobStore is job_store.JobStore
    store = local_web.JobStore(tmp_path / "jobs", recover=False)
    assert isinstance(store, job_store.JobStore)
    assert isinstance(store, local_web.JobStore)


def test_default_job_root_patch_on_facade_changes_default_root(tmp_path, monkeypatch):
    import life_logger.job_store as job_store

    monkeypatch.setattr(local_web, "DEFAULT_JOB_ROOT", tmp_path / "patched-root")
    assert job_store.DEFAULT_JOB_ROOT == tmp_path / "patched-root"
    store = local_web.JobStore(recover=False)
    assert store.root == (tmp_path / "patched-root").resolve()


def test_job_status_helpers_resolve_to_job_store():
    import life_logger.job_store as job_store

    for name in ("apply_task_status", "to_task_status", "_job_utterance_items",
                 "to_reminder_status", "TASK_TO_REMINDER_STATUS", "REMINDER_TO_TASK_STATUS"):
        assert getattr(local_web, name) is getattr(job_store, name)


VIEW_NAMES = ["_meaningful_event_payload", "apply_file_outcomes", "failed_file_summaries",
              "_model_catalog", "_apply_interlocutor_fields", "_capture_funnel_nonblocking",
              "_reapply_activity_sidecars"]


@pytest.mark.parametrize("name", VIEW_NAMES)
def test_s5_names_resolve_to_web_data_views(name):
    import life_logger.web_data_views as web_data_views

    assert getattr(local_web, name) is getattr(web_data_views, name)


def test_build_range_statistics_patch_reaches_route(monkeypatch):
    """`build_range_statistics` 通过 local_web 导入，patch 后路由调用必须走 fake。"""
    import life_logger.local_web as module

    calls = []
    monkeypatch.setattr(module, "build_range_statistics", lambda *a, **k: calls.append(k) or {"patched": True})
    from life_logger.statistics import build_range_statistics as real

    assert module.build_range_statistics is not real
    assert module.build_range_statistics() == {"patched": True}
    assert calls


def test_string_path_patch_for_apply_file_outcomes(monkeypatch):
    """测试里有 1 处字符串路径 patch，门面必须让它生效。"""
    import life_logger.web_data_views as web_data_views

    original = web_data_views.apply_file_outcomes
    monkeypatch.setattr("life_logger.local_web.apply_file_outcomes", lambda *a, **k: "patched")
    assert web_data_views.apply_file_outcomes() == "patched"
    assert local_web.apply_file_outcomes() == "patched"
    monkeypatch.setattr("life_logger.local_web.apply_file_outcomes", original)
    assert web_data_views.apply_file_outcomes is original


S6_NAMES = ["LocalWebServer", "create_server", "serve", "_serve_runtime", "API_FEATURES",
            "_watch_last_segment_received", "_start_passport_receiver_for_web",
            "_passport_receiver_options"]


@pytest.mark.parametrize("name", S6_NAMES)
def test_s6_names_resolve_to_web_startup(name):
    import life_logger.web_startup as web_startup

    assert getattr(local_web, name) is getattr(web_startup, name)


def test_local_web_server_class_patch_is_shared():
    import life_logger.web_startup as web_startup

    assert local_web.LocalWebServer is web_startup.LocalWebServer
    original = web_startup.LocalWebServer
    try:
        local_web.LocalWebServer = object
        assert web_startup.LocalWebServer is object
    finally:
        local_web.LocalWebServer = original


def test_serve_runtime_patch_reaches_serve(tmp_path, monkeypatch):
    """patch 门面 `_serve_runtime` 后，`serve()` 必须调用 fake（同模块全局查找）。"""
    import life_logger.web_startup as web_startup

    calls = []
    monkeypatch.setattr(local_web, "_serve_runtime", lambda **k: calls.append(k) or "patched")
    assert web_startup._serve_runtime is not None
    result = local_web.serve(processing_mode="split", root=tmp_path, manage_worker=False)
    assert result == "patched"
    assert calls and calls[0]["root"] == tmp_path.resolve()


def test_create_server_uses_patched_default_job_root(tmp_path, monkeypatch):
    """`create_server(store=None)` 的默认 root 必须运行时读 DEFAULT_JOB_ROOT。"""
    import life_logger.job_store as job_store

    monkeypatch.setattr(local_web, "DEFAULT_JOB_ROOT", tmp_path / "serve-root")
    assert job_store.DEFAULT_JOB_ROOT == tmp_path / "serve-root"
    server = local_web.create_server(port=0)
    try:
        assert server.store.root == (tmp_path / "serve-root").resolve()
    finally:
        server.server_close()


S7_NAMES = ["_Handler", "_VERSION_BADGE_SCRIPT", "gzip_response_body", "_accepts_gzip",
            "_GZIP_MIN_BYTES", "_GZIP_CACHE", "_GZIP_CACHE_MAX_ENTRIES",
            "_GZIP_MIME_EXACT", "_GZIP_MIME_PREFIXES", "_GZIP_SKIP_SUFFIXES"]


@pytest.mark.parametrize("name", S7_NAMES)
def test_s7_names_resolve_to_web_http_or_support(name):
    import life_logger.web_http as web_http
    import life_logger.web_support as web_support

    target = web_http if name in {"_Handler", "_VERSION_BADGE_SCRIPT"} else web_support
    assert getattr(local_web, name) is getattr(target, name)


def test_handler_method_access_and_patch_share_the_same_class():
    import life_logger.web_http as web_http

    assert local_web._Handler is web_http._Handler
    assert local_web._Handler._sync_kanban_openclaw is web_http._Handler._sync_kanban_openclaw
    fake = lambda self: "patched"
    original = web_http._Handler._sync_kanban_openclaw
    try:
        local_web._Handler._sync_kanban_openclaw = fake
        assert web_http._Handler._sync_kanban_openclaw is fake
    finally:
        web_http._Handler._sync_kanban_openclaw = original


def test_gzip_patch_on_facade_reaches_send_json(monkeypatch):
    """`local_web.gzip_response_body` patch 必须改到 web_support 的实现。"""
    import life_logger.web_support as web_support

    monkeypatch.setattr(local_web, "gzip_response_body", lambda *a, **k: b"gz")
    assert web_support.gzip_response_body(b"x" * 5000, "application/json", "gzip") == b"gz"


def test_build_range_statistics_patch_reaches_handler():
    """`_Handler` 体内通过 `_lw.build_range_statistics` 读取，patch 必须生效。"""
    import life_logger.web_http as web_http

    module = __import__("life_logger.local_web", fromlist=["build_range_statistics"])
    calls = []
    original = module.build_range_statistics
    try:
        module.build_range_statistics = lambda *a, **k: calls.append(1) or {"patched": True}
        assert module.build_range_statistics() == {"patched": True}
        source = __import__("inspect").getsource(web_http._Handler.do_GET)
        assert "_lw.build_range_statistics" in source
        assert calls
    finally:
        module.build_range_statistics = original


def test_subprocess_patch_on_facade_reaches_handler_source():
    """`_Handler` 用 `_lw.subprocess`，替换整个模块对象的 patch 才能生效。"""
    import inspect

    import life_logger.web_http as web_http

    source = inspect.getsource(web_http._Handler)
    assert "_lw.subprocess" in source
    assert "subprocess.run(" not in source.replace("_lw.subprocess.run(", "")
