"""model_onboarding：Coding 上手清单第 ① 步的配置写入与连接测试（离线）。"""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import model_onboarding as mo
import secret_store
from config_model import load_user_layer


class ApplyKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patcher = patch.object(secret_store, "SECRETS_FILE", self.home / "secrets.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_reference_row_and_keeps_plaintext_only_in_secret_store(self):
        result = mo.apply_key(self.home, "deepseek", "  sk-test-123  ")
        self.assertTrue(result["ok"], result)
        rows = load_user_layer(self.home)
        row = next(r for r in rows if r["id"] == "deepseek")
        self.assertEqual(row["name"], "provider:deepseek")
        self.assertEqual(row["config"]["baseURL"], "https://api.deepseek.com")
        self.assertEqual(row["config"]["apiKey"], {"$expr": "get('env.FORGE_DEEPSEEK_KEY', '')"})
        self.assertEqual(row["config"]["model"], "deepseek-chat")
        self.assertNotIn("sk-test-123", (self.home / "forge.patch.json").read_text(encoding="utf-8"))
        self.assertEqual(secret_store.get_provider("deepseek"), "sk-test-123")
        self.assertEqual(mo.configured_models(self.home), ["deepseek"])

    def test_overwrites_existing_row_and_preserves_other_rows(self):
        (self.home / "forge.patch.json").write_text(json.dumps([
            {"id": "connector.x", "name": "connector:x", "config": {"enabled": True}},
            {"id": "deepseek", "name": "provider:deepseek", "disabled": True,
             "config": {"baseURL": "https://old", "smallModel": "keep-me"}},
        ]), encoding="utf-8")
        self.assertTrue(mo.apply_key(self.home, "deepseek", "sk-new")["ok"])
        rows = {r["id"]: r for r in load_user_layer(self.home)}
        self.assertIn("connector.x", rows)
        self.assertNotIn("disabled", rows["deepseek"])
        self.assertEqual(rows["deepseek"]["config"]["baseURL"], "https://api.deepseek.com")
        self.assertEqual(rows["deepseek"]["config"]["smallModel"], "keep-me")

    def test_keyless_local_model_needs_no_secret(self):
        self.assertTrue(mo.apply_key(self.home, "ollama", "")["ok"])
        row = load_user_layer(self.home)[0]
        self.assertNotIn("apiKey", row["config"])
        self.assertFalse((self.home / "secrets.json").exists())

    def test_rejects_empty_or_broken_keys_without_writing(self):
        for key in ("", "   ", "sk-a b"):
            with self.subTest(key=key):
                self.assertFalse(mo.apply_key(self.home, "deepseek", key)["ok"])
        self.assertFalse(mo.apply_key(self.home, "nope", "sk")["ok"])
        self.assertFalse((self.home / "forge.patch.json").exists())

    def test_disabled_rows_do_not_count_as_configured(self):
        (self.home / "forge.patch.json").write_text(json.dumps([
            {"id": "k", "name": "provider:k", "disabled": True, "config": {"baseURL": "https://x"}},
        ]), encoding="utf-8")
        self.assertEqual(mo.configured_models(self.home), [])


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ConnectionTests(unittest.TestCase):
    def test_success_reports_model_count_and_sends_bearer(self):
        seen = {}

        def opener(request, timeout):
            seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
            return _Response(json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode())
        result = mo.test_connection("deepseek", "sk-1", opener=opener)
        self.assertTrue(result["ok"])
        self.assertIn("2", result["message"])
        self.assertEqual(seen, {"url": "https://api.deepseek.com/models", "auth": "Bearer sk-1"})

    def test_anthropic_uses_its_own_header(self):
        seen = {}

        def opener(request, timeout):
            seen["key"] = request.get_header("X-api-key")
            seen["url"] = request.full_url
            return _Response(b"{}")
        self.assertTrue(mo.test_connection("anthropic", "k", opener=opener)["ok"])
        self.assertEqual(seen, {"key": "k", "url": "https://api.anthropic.com/v1/models"})

    def test_unauthorized_and_network_errors_are_plain_language(self):
        def denied(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

        def offline(request, timeout):
            raise urllib.error.URLError("timed out")
        self.assertIn("密钥无效", mo.test_connection("deepseek", "sk", opener=denied)["error"])
        self.assertIn("网络", mo.test_connection("deepseek", "sk", opener=offline)["error"])
        self.assertIn("Ollama", mo.test_connection("ollama", "", opener=offline)["error"])
        self.assertIn("密钥", mo.test_connection("deepseek", "", opener=offline)["error"])


if __name__ == "__main__":
    unittest.main()
