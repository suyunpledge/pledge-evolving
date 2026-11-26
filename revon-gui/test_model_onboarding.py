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
        self.assertEqual(row["config"]["apiKey"], {"$expr": "get('env.REVON_DEEPSEEK_KEY', '')"})
        self.assertEqual(row["config"]["model"], "deepseek-chat")
        self.assertNotIn("sk-test-123", (self.home / "revon.patch.json").read_text(encoding="utf-8"))
        self.assertEqual(secret_store.get_provider("deepseek"), "sk-test-123")
        self.assertEqual(mo.configured_models(self.home), ["deepseek"])

    def test_overwrites_existing_row_and_preserves_other_rows(self):
        (self.home / "revon.patch.json").write_text(json.dumps([
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

    def test_setup_token_limits_are_validated_and_saved_per_model(self):
        limits = {'contextWindow': '1M', 'outputTokenBudget': '384k'}
        self.assertTrue(mo.apply_key(self.home, 'ollama', '', model='one', token_limits=limits)['ok'])
        config = load_user_layer(self.home)[0]['config']
        self.assertEqual(config['modelCapabilities'], {'one': {'contextWindow': 1048576, 'outputTokenBudget': 384000}})
        for value in ('-1', '0', '1.5K', 'unlimited'):
            self.assertFalse(mo.apply_key(self.home, 'ollama', '', model='one',
                             token_limits={'outputTokenBudget': value})['ok'])
        self.assertEqual(load_user_layer(self.home)[0]['config'], config)
        self.assertTrue(mo.apply_key(self.home, 'ollama', '', model='one',
                        token_limits={'outputTokenBudget': ''})['ok'])
        self.assertEqual(load_user_layer(self.home)[0]['config']['modelCapabilities'], {'one': {'contextWindow': 1048576}})

    def test_window_replaces_legacy_input_only_after_explicit_edit(self):
        conf = {'modelCapabilities': {'one': {'maxInputTokens': 60000}, 'two': {'maxInputTokens': 70000}}}
        self.assertEqual(mo.token_fields(conf, 'one')['legacyInput'], '60000')
        mo.set_token_fields(conf, 'one', {'contextWindow': '', 'outputTokenBudget': '4K'})
        self.assertEqual(conf['modelCapabilities']['one']['maxInputTokens'], 60000)
        mo.set_token_fields(conf, 'one', {'contextWindow': '128K'})
        self.assertEqual(conf['modelCapabilities'], {'one': {'contextWindow': 131072, 'outputTokenBudget': 4096},
                                                    'two': {'maxInputTokens': 70000}})
        with self.assertRaisesRegex(ValueError, '上下文长度'):
            mo.set_token_fields(conf, 'one', {'contextWindow': '2000'})
        self.assertEqual(conf['modelCapabilities']['one']['contextWindow'], 131072)

    def test_keyless_local_model_needs_no_secret(self):
        self.assertTrue(mo.apply_key(self.home, "ollama", "")["ok"])
        row = load_user_layer(self.home)[0]
        self.assertNotIn("apiKey", row["config"])
        self.assertFalse((self.home / "secrets.json").exists())

    def test_opencode_plans_keep_separate_keys_and_settings(self):
        from provider_catalog import route_fields
        for plan in ('go', 'zen'):
            self.assertTrue(mo.apply_key(self.home, 'opencode-' + plan, 'test-' + plan)['ok'])
        rows = load_user_layer(self.home)
        self.assertEqual([r['id'] for r in rows], ['opencode-go', 'opencode-zen'])
        self.assertEqual([route_fields(r['config'])['providerName'] for r in rows], ['OpenCode Go', 'OpenCode Zen'])
        self.assertNotEqual(rows[0]['config']['apiKey'], rows[1]['config']['apiKey'])

    def test_rejects_empty_or_broken_keys_without_writing(self):
        for key in ("", "   ", "sk-a b"):
            with self.subTest(key=key):
                self.assertFalse(mo.apply_key(self.home, "deepseek", key)["ok"])
        self.assertFalse(mo.apply_key(self.home, "nope", "sk")["ok"])
        self.assertFalse((self.home / "revon.patch.json").exists())

    def test_disabled_rows_do_not_count_as_configured(self):
        (self.home / "revon.patch.json").write_text(json.dumps([
            {"id": "k", "name": "provider:k", "disabled": True, "config": {"baseURL": "https://x"}},
        ]), encoding="utf-8")
        self.assertEqual(mo.configured_models(self.home), [])


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ConnectionTests(unittest.TestCase):
    def test_google_native_and_compatible_presets_save_and_probe_the_same_wire(self):
        for plan in mo.BY_KEY['google'].plans:
            with self.subTest(wire=plan.wire), tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                saved = mo.apply_key(home, 'google', 'fixture', base_url=plan.base_url,
                                     credential_path=home / 'secrets.json')
                self.assertEqual(saved['wire'], plan.wire)
                self.assertEqual(load_user_layer(home)[0]['config']['wire'], plan.wire)
                def opener(request, **_):
                    if plan.wire == 'google':
                        self.assertEqual(request.full_url, plan.base_url + '/v1beta/models')
                        self.assertEqual(request.get_header('X-goog-api-key'), 'fixture')
                        self.assertIsNone(request.get_header('Authorization'))
                    else:
                        self.assertEqual(request.full_url, plan.base_url + '/models')
                        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture')
                    return _Response(b'{"models":[{"name":"models/gemini-test"}]}')
                self.assertTrue(mo.test_connection('google', 'fixture', base_url=plan.base_url, opener=opener)['ok'])

    def test_opencode_discovery_uses_public_plan_catalog_and_preserves_custom_hosts(self):
        seen = []
        def opener(req, timeout):
            if req.host == 'opencode.ai':
                self.assertTrue(req.get_header('User-agent', '').startswith('revon/'))
            seen.append((req.full_url, req.get_header('Authorization'), req.get_header('X-api-key')))
            return _Response(b'{"data":[{"id":"test"}]}')
        for plan, root in [('go', 'inference/go'), ('zen', 'inference')]:
            with self.subTest(plan=plan):
                result = mo.test_connection('opencode-' + plan, '', opener=opener)
                self.assertTrue(result['ok'])
                self.assertIn('公开目录', result['message'])
                self.assertEqual(seen[-1], ('https://opencode.ai/' + root + '/v1/models', None, None))
        self.assertTrue(mo.test_connection('opencode-go', 'test-key', base_url='https://proxy.test/zen/go/v1',
                                           opener=opener)['ok'])
        self.assertEqual(seen[-1], ('https://proxy.test/zen/go/v1/models', 'Bearer test-key', None))

    def test_openai_setup_saves_its_own_row_and_custom_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            creds = home / 'secrets.json'
            for vendor_id in ('deepseek', 'openai'):
                result = mo.apply_key(home, vendor_id, 'QA-virtual', model='screen-model',
                                      base_url='http://127.0.0.1:1234/v1', credential_path=creds)
                self.assertTrue(result['ok'], result)
            self.assertEqual({r['id'] for r in load_user_layer(home)}, {'deepseek', 'openai'})
            self.assertEqual(mo.vendor('openai').base_url, mo.BY_KEY['openai'].plans[0].base_url)

    def test_probe_distinguishes_model_lists_from_other_http_200_responses(self):
        for raw in (b'<html>Welcome</html>', b'{"error":{"message":"denied"}}',
                    b'{}', b'{"data":"error"}', b'{"models":[{}]}'):
            with self.subTest(raw=raw):
                result = mo.probe('http://127.0.0.1/v1', '', opener=lambda *a, **kw: _Response(raw))
                self.assertFalse(result['ok'], result)
        for raw, count in ((b'{"data":[]}', 0), (b'{"models":[{"name":"local"}]}', 1)):
            result = mo.probe('http://127.0.0.1/v1', '', opener=lambda *a, **kw: _Response(raw))
            self.assertIn(f'{count} 个', result['message'])

    def test_success_reports_model_count_and_sends_bearer(self):
        seen = {}

        def opener(request, timeout):
            seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
            return _Response(json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode())
        result = mo.test_connection("deepseek", "sk-1", opener=opener)
        self.assertTrue(result["ok"])
        self.assertIn("2", result["message"])
        self.assertEqual(seen, {"url": "https://api.deepseek.com/models", "auth": "Bearer sk-1"})

    def test_custom_address_is_the_only_probe_target(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            return _Response(b'{"data": [{"id": "custom-model"}]}')

        result = mo.test_connection("deepseek", "sk-test", base_url="http://127.0.0.1:8765/custom/v1/",
                                    opener=opener)
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [("http://127.0.0.1:8765/custom/v1/models", "Bearer sk-test")])
        self.assertFalse(mo.test_connection("deepseek", "sk-test", base_url="",
                                            opener=opener)["ok"])
        self.assertEqual(len(seen), 1)

    def test_official_responses_address_probes_model_list_at_api_root(self):
        seen = []
        def opener(request, timeout):
            seen.append(request.full_url)
            return _Response(b'{"data": [{"id": "gpt-6.2-astra"}]}')
        result = mo.probe("https://api.openai.com/v1/responses/", "test-key", opener=opener)
        self.assertTrue(result["ok"], result)
        self.assertEqual(seen, ["https://api.openai.com/v1/models"])

    def test_anthropic_uses_its_own_header(self):
        seen = {}

        def opener(request, timeout):
            seen["key"] = request.get_header("X-api-key")
            seen["url"] = request.full_url
            return _Response(b'{"data":[]}')
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
