"""全局测试夹具：把模型重试的退避时间清零。

【2026-10-22 W4】日报生成现在每级重试 3 次、退避 1s/3s/8s。
凡是「模拟模型失败」的测试都会走满 3 级 × 3 次 = 12 次失败，
如果真按 1s/3s/8s 等待，单个测试就是 36 秒。测试里退避一律设 0，
重试次数保持默认 3（要测的就是"重试 3 次才判死"这条语义）。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("LL_NOTIFY_MAC_FALLBACK", "0")  # 测试不弹真实通知
os.environ.setdefault("LL_NOTIFY_MODE", "disabled")  # 测试绝不发真实微信；要测发送的用例自己设 auto


@pytest.fixture(autouse=True)
def _no_model_retry_backoff(monkeypatch):
    monkeypatch.setenv("LIFE_LOGGER_MODEL_BACKOFF", "0")


# [2026-11-04] 跨文件串扰修复：
# `test_local_web.py` 里这 5 个 watch-live-loop 用例共用模块级单例状态
# （失步的 slot / inflight 表），单独跑全绿，全量跑时被前面的用例污染后失败。
# 这里在**每个测试之间**重置这些单例，让结果只取决于用例本身，而不是文件顺序。
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _reset_local_web_singletons():
    yield
    try:
        from life_logger import local_web as _lw
    except Exception:
        return
    for name in ("_WATCH_LIVE_LOOP_STATE", "_WATCH_LIVE_LOOP_LOCK",
                 "_WATCH_FORMAL_SCAN_STATE", "_WATCH_FORMAL_SCAN_LOCK"):
        obj = getattr(_lw, name, None)
        if obj is None:
            continue
        try:
            if hasattr(obj, "clear"):
                obj.clear()
        except Exception:
            pass

# [2026-11-06 Q921 并发闸门偶发失败] 提取器并发闸门是**进程级全局**信号量：
# ``_get_extractor_gate(capacity)`` 在容量变化时**重建**信号量（activity_timeline_runtime.py:57-64）。
# 两个后果都会让测试互相污染：
#   1) 某个用例把容量改成 3/4 之后，后面的用例继承这个容量；
#   2) 更糟的是残留后台线程（local_web 起的 ``activity-timeline-10min`` tick）按默认容量 2
#      调一次，就会把**别的测试正在用的**信号量换掉 —— 已持有旧信号量的调用继续跑，
#      新调用又拿到一整组新许可，瞬时并发可以超过上限（本机实测 peak 7~9 > 3）。
# 这里在每个测试前后把闸门恢复成「未初始化」，让结果只取决于用例本身。
@_pytest.fixture(autouse=True)
def _reset_extractor_gate():
    try:
        from life_logger import activity_timeline_runtime as _rt
    except Exception:  # pragma: no cover - 极端情况下模块导入失败不影响用例
        yield
        return

    def _reset() -> None:
        _rt._EXTRACTOR_CONCURRENCY_SEMAPHORE = None
        _rt._EXTRACTOR_SEMAPHORE_CAPACITY = _rt.DEFAULT_EXTRACTOR_MAX_CONCURRENCY

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_llm_config_override():
    """llm_config 的实例配置路径是进程级状态：起过 LocalWebServer 的用例会留下覆盖，
    让后面依赖“生产默认配置”的用例读到别的路径。每个用例前后都清掉。"""
    try:
        from life_logger import llm_config as _llm_config
    except Exception:  # pragma: no cover - 模块导入失败不影响用例
        yield
        return
    _llm_config.set_config_path(None)
    yield
    _llm_config.set_config_path(None)
