"""
tests/test_check_model_tiers.py
模型降级链健康检查工具（scripts/check_model_tiers.py）单元测试

覆盖项：
1. 默认 dry-run 静态模式输出与退出码测试；
2. 模拟 HTTP 402 Payment Required 失败分支（如官方 DeepSeek 欠费死链场景）；
3. 模拟超时（socket.timeout / TimeoutError）失败分支；
4. 模拟缺失必填配置字段（如 base_url 或 model 为空）的失败分支。
"""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

# 导入待测脚本模块
import scripts.check_model_tiers as checker

class TestCheckModelTiers(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "local-config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_config(self, deepseek_dict: dict):
        cfg = {"deepseek": deepseek_dict}
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_dry_run_mode_default(self):
        """测试1：默认 dry-run 模式只打印配置与各层级参数，不发起真实网络调用，退出码为 0"""
        self._write_config({
            "enabled": True,
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "magpie",
            "model": "commandcode-plan/deepseek/deepseek-v4.1-flash",
            "tier2_model": "antigravity/gemini-3.8-flash",
            "fallback": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-real",
                "model": "deepseek-chat"
            }
        })

        report = checker.run_checks(self.config_path, probe=False)
        self.assertTrue(report["all_healthy"])
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(len(report["tiers"]), 3)
        for t in report["tiers"]:
            self.assertEqual(t["status"], "CONFIG_OK")
            self.assertIsNone(t["latency_ms"])

    def test_mock_http_402_payment_required(self):
        """测试2：模拟 HTTP 402 Payment Required 欠费场景，识别死链并标记 FAILED"""
        self._write_config({
            "enabled": True,
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "magpie",
            "model": "commandcode-plan/deepseek/deepseek-v4.1-flash",
            "fallback": {
                "enabled": True,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-exhausted",
                "model": "deepseek-chat"
            }
        })

        # Mock Opener 返回 402 错误
        mock_opener = MagicMock()
        err_fp = io.BytesIO(b'{"error":{"message":"Insufficient Balance","type":"balance_error"}}')
        http_402_err = urllib.error.HTTPError(
            url="https://api.deepseek.com/v1/chat/completions",
            code=402,
            msg="Payment Required",
            hdrs={},
            fp=err_fp
        )
        mock_opener.open.side_effect = http_402_err

        report = checker.run_checks(self.config_path, probe=True, opener=mock_opener)
        self.assertFalse(report["all_healthy"], "存在 402 失败时整体健康状态必须为 False")

        # 验证 Fallback 层级被标为 FAILED 并说明 402
        fb_tier = next(t for t in report["tiers"] if t["tier"] == "tier3")
        self.assertEqual(fb_tier["status"], "FAILED")
        self.assertIn("402", fb_tier["reason"])
        self.assertIn("Payment Required", fb_tier["reason"])

    def test_mock_timeout_error(self):
        """测试3：模拟请求超时 (URLError / timeout)，标记 FAILED"""
        self._write_config({
            "enabled": True,
            "base_url": "http://127.0.0.1:3425/v1",
            "api_key": "magpie",
            "model": "commandcode-plan/deepseek/deepseek-v4.1-flash"
        })

        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("timed out")

        report = checker.run_checks(self.config_path, probe=True, timeout=2.0, opener=mock_opener)
        self.assertFalse(report["all_healthy"])

        primary_tier = next(t for t in report["tiers"] if t["tier"] == "tier1")
        self.assertEqual(primary_tier["status"], "FAILED")
        self.assertIn("timed out", primary_tier["reason"])

    def test_missing_required_fields(self):
        """测试4：模拟缺失字段（如 base_url 或 model 为空），标记 INVALID_CONFIG"""
        self._write_config({
            "enabled": True,
            "base_url": "", # 故意留空
            "api_key": "magpie",
            "model": "commandcode-plan/deepseek/deepseek-v4.1-flash"
        })

        # 在 dry-run 下即可静态识别
        report = checker.run_checks(self.config_path, probe=False)
        self.assertFalse(report["all_healthy"])

        primary_tier = next(t for t in report["tiers"] if t["tier"] == "tier1")
        self.assertEqual(primary_tier["status"], "INVALID_CONFIG")
        self.assertIn("base_url", primary_tier["reason"])

if __name__ == "__main__":
    unittest.main()
