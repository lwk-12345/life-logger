"""测试：修复死模型兜底，配置缺失时禁止静默回退到 deepseek-chat 或死模型。"""
import json
from life_logger.activity_timeline_runtime import ActivityTimelineRuntime

def test_config_missing_model_raises_or_uses_auto_gateway(tmp_path, monkeypatch):
    """当 local-config.json 与环境变量均未配置 model 时：
    1. 严禁静默回退到死模型 'deepseek-chat'！
    2. 若采取方案（报错退出）：验证抛出 ValueError 或 RuntimeError。
    3. 若采取方案（安全网关兜底）：验证模型为 'auto/deepseek-v4.1-flash'。
    """
    # 清空环境变量
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    cfg_file = tmp_path / "local-config.json"
    # 配置中完全缺失 model 字段
    cfg_file.write_text(json.dumps({
        "deepseek": {
            "api_key": "test_key",
            "base_url": "http://127.0.0.1:3425/v1"
        }
    }), encoding="utf-8")

    rt = ActivityTimelineRuntime(tmp_path, cfg_file)
    
    # 获取设置
    url, key, model = rt.get_deepseek_settings()
    
    # 核心断言：绝对不能拿到已死的 'deepseek-chat'
    assert model != "deepseek-chat", "致命缺陷：配置缺失时静默回退到了死模型 'deepseek-chat'！"
    
    # 期望兜底为网关自动路由模型
    assert model == "auto/deepseek-v4.1-flash", f"预期兜底为 'auto/deepseek-v4.1-flash'，实际为: {model}"

def test_configured_model_preserved(tmp_path, monkeypatch):
    """当配置了有效 model 时，必须严格保持配置值，不破坏现有正常行为。"""
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    cfg_file = tmp_path / "local-config.json"
    cfg_file.write_text(json.dumps({
        "deepseek": {
            "api_key": "test_key",
            "base_url": "http://127.0.0.1:3425/v1",
            "model": "custom/my-special-model"
        }
    }), encoding="utf-8")

    rt = ActivityTimelineRuntime(tmp_path, cfg_file)
    url, key, model = rt.get_deepseek_settings()
    assert model == "custom/my-special-model"

def test_env_var_model_respected(tmp_path, monkeypatch):
    """当配置未设置但环境变量设置了 DEEPSEEK_MODEL 时，优先尊重环境变量。"""
    monkeypatch.setenv("DEEPSEEK_MODEL", "env/deepseek-v4")
    cfg_file = tmp_path / "local-config.json"
    cfg_file.write_text(json.dumps({
        "deepseek": {
            "api_key": "test_key"
        }
    }), encoding="utf-8")

    rt = ActivityTimelineRuntime(tmp_path, cfg_file)
    url, key, model = rt.get_deepseek_settings()
    assert model == "env/deepseek-v4"
