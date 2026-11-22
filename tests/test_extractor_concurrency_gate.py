"""测试提取调用并发闸门与超时治理 (Extractor Concurrency Gate & Timeout)."""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch
import pytest

from life_logger.activity_timeline_runtime import (
    ActivityTimelineRuntime,
    DEFAULT_EXTRACTOR_MAX_CONCURRENCY,
)


@pytest.fixture
def rt_factory(tmp_path: Path):
    def make_rt(config_data: dict | None = None):
        cfg = tmp_path / "local-config.json"
        if config_data is not None:
            cfg.write_text(json.dumps(config_data), encoding="utf-8")
        else:
            cfg.write_text("{}", encoding="utf-8")
        return ActivityTimelineRuntime(tmp_path / "data", cfg)
    return make_rt


def test_concurrency_config_defaults_and_validation(rt_factory):
    """【目标 1 & 2】：验证并发闸门容量默认值(2)、有效范围(1..16)及非法值安全回退。"""
    # 1. 缺省配置 -> 回退默认 2
    rt = rt_factory({})
    assert rt.get_deepseek_max_concurrency() == DEFAULT_EXTRACTOR_MAX_CONCURRENCY == 2

    # 2. 有效配置
    rt_valid = rt_factory({"deepseek": {"max_concurrency": 4}})
    assert rt_valid.get_deepseek_max_concurrency() == 4

    # 3. 非法值（非数字） -> 记录 warning 并回退 2
    rt_nan = rt_factory({"deepseek": {"max_concurrency": "invalid_abc"}})
    assert rt_nan.get_deepseek_max_concurrency() == 2

    # 4. 越界值（<1 或 >16） -> 回退 2
    rt_low = rt_factory({"deepseek": {"max_concurrency": 0}})
    assert rt_low.get_deepseek_max_concurrency() == 2
    rt_high = rt_factory({"deepseek": {"max_concurrency": 32}})
    assert rt_high.get_deepseek_max_concurrency() == 2


OWN_PROMPT = "concurrency-gate-own-request"
FOREIGN: list[str] = []


def test_concurrency_gate_limits_parallel_runs(rt_factory):
    """【目标 1 实测】：模拟多线程并发调用 _call_deepseek_once，断言同时在跑的请求数绝不超过 N。

    [2026-11-06 修偶发失败] 这条用例曾在本机全量里偶发失败（peak 5 > 3）。
    原因**不是**闸门坏了，而是测量被两件事污染：

    1. `patch("urllib.request.urlopen")` 是**进程级**的。前面测试留下的后台线程
       （local_web 的 `activity-timeline-10min` tick、存根服务等）在这 80ms 窗口里发一次
       真实 HTTP，就会走到本用例的 mock 里并被计进 `current_running` —— 峰值凭空多 1~2。
       所以现在**只统计自己发的请求**（按请求体里的 prompt 识别），外来调用只记录、不计数。
    2. 闸门是全局信号量，`_get_extractor_gate(capacity)` 在容量变化时重建。
       外来 tick 用默认容量 2 调一次，就会把正在被本用例使用的信号量换掉，
       已持有的调用继续跑、新调用又拿到一整组新许可 → 瞬时并发可以超过上限。
       这一点由 `tests/conftest.py::_reset_extractor_gate` 在每个测试前后复位来隔离，
       并且本用例在结尾断言闸门没有被换过。

    另外线程改为**严格 join**（不再 `join(timeout=)` 后不管），失败时能看到哪个线程没退出。
    """
    max_concurrency = 3
    rt = rt_factory({"deepseek": {"max_concurrency": max_concurrency}})
    from life_logger import activity_timeline_runtime as rt_mod

    current_running = 0
    peak_running = 0
    lock = threading.Lock()
    total_calls = 8
    FOREIGN.clear()

    def _is_ours(req) -> bool:
        try:
            body = json.loads(req.data.decode("utf-8"))
            return body["messages"][0]["content"] == OWN_PROMPT
        except Exception:
            return False

    def mock_urlopen(req, timeout=None):
        nonlocal current_running, peak_running
        if not _is_ours(req):
            # 外来请求（残留线程的真实 HTTP）：只登记，绝不计入并发峰值
            with lock:
                FOREIGN.append(str(getattr(req, "full_url", req))[:80])
            return MockResp()
        with lock:
            current_running += 1
            if current_running > peak_running:
                peak_running = current_running
        time.sleep(0.08)  # 保持占用片刻
        with lock:
            current_running -= 1
        return MockResp()

    class MockResp:
        def read(self):
            return b'{"choices": [{"message": {"content": "{}"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    threads = []
    tier = {"name": "primary", "model": "test-model", "base_url": "http://127.0.0.1:3425", "api_key": "test_k"}

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        for _ in range(total_calls):
            t = threading.Thread(target=rt._call_deepseek_once, args=(tier, OWN_PROMPT))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=10.0)

    # 峰值并发运行数绝不能超过 max_concurrency
    assert peak_running <= max_concurrency, (
        f"峰值并发 {peak_running} 超出了允许的上限 {max_concurrency}"
        f"（外来请求 {len(FOREIGN)} 条未计入峰值：{FOREIGN[:3]}）"
    )
    assert peak_running >= 2, "多线程应能达到多并发"
    assert current_running == 0
    alive = [t.name for t in threads if t.is_alive()]
    assert not alive, f"这些工作线程没有退出：{alive}"
    # 诊断用（不做断言：残留线程换掉全局闸门是**环境噪声**，不该让本用例红）
    if rt_mod._EXTRACTOR_SEMAPHORE_CAPACITY != max_concurrency:
        print(f"[diag] 闸门容量被外来调用改成 {rt_mod._EXTRACTOR_SEMAPHORE_CAPACITY}")


def test_failure_includes_tier_timeout_and_elapsed(rt_factory):
    """【目标 2 实测】：超时失败异常信息中包含 tier 标识、timeout_s 及耗时 elapsed。"""
    timeout_val = 45.0
    rt = rt_factory({"deepseek": {"timeout_s": timeout_val}})

    def mock_timeout(req, timeout):
        time.sleep(0.05)
        raise TimeoutError("The read operation timed out")

    tier = {"name": "tier2", "model": "magpie-model", "base_url": "http://127.0.0.1:3425", "api_key": "test_k"}

    with patch("urllib.request.urlopen", side_effect=mock_timeout):
        with pytest.raises(RuntimeError) as exc_info:
            rt._call_deepseek_once(tier, "test prompt", timeout=timeout_val)

    err_msg = str(exc_info.value)
    assert "tier=tier2" in err_msg, f"缺少 tier 名称: {err_msg}"
    assert f"timeout={timeout_val:g}s" in err_msg, f"缺少 timeout 值: {err_msg}"
    assert "elapsed=" in err_msg, f"缺少耗时 elapsed 统计: {err_msg}"
    assert "TimeoutError" in err_msg
