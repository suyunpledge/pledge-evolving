"""Offline regressions for model identity versus provider/transport identity."""
import copy
import io
import json
import unittest
from unittest.mock import patch

from . import model
from .adapters import LocalAdapter, adapter_for, adapter_for_id
from .cache_state import CacheWarmth
from .config import Config
from .gateway import GatewayConfig
from .model_kinds import MODEL_KINDS, manufacturer_for_model, resolve_model_kind
from .vendors import PROFILES, profile_for


class ModelKindTests(unittest.TestCase):
    def provider(self, **options):
        return model.Provider("reseller", "https://gateway.example/v1", **options)

    def plan(self, provider, chosen, **options):
        payload = {"model": chosen, "messages": [{"role": "system", "content": "S" * 6000}],
                   **options}
        before = copy.deepcopy(payload)
        result = model._plan_and_shape(payload, payload["messages"], provider, chosen,
                                       store=CacheWarmth())
        self.assertEqual(payload, before)
        return result

    def test_model_identity_wins_over_reseller_vendor_and_host(self):
        profile = profile_for(base_url="https://api.openai.com", vendor="anthropic",
                              model="MiniMax/MiniMax-M3")
        self.assertEqual((profile.id, profile.manufacturer_id, profile.model_kind_id),
                         ("minimax", "minimax", "minimax.m3"))
        self.assertEqual(adapter_for(profile).adapter_id, "minimax.m3")

    def test_family_shared_but_sibling_kind_ids_are_distinct(self):
        flash = profile_for(model="mimo-v2.6-flash")
        pro = profile_for(model="mimo-v2.6-pro")
        self.assertEqual(flash.model_family_id, pro.model_family_id)
        self.assertNotEqual(flash.model_kind_id, pro.model_kind_id)
        self.assertNotEqual(flash.behavior_fingerprint, pro.behavior_fingerprint)

    def test_aliases_share_capabilities_and_ignore_provider_labels(self):
        a = self.provider(default_model="hy4free").profile()
        b = model.Provider("other", "https://another.example", default_model="Tencent/hy4-free").profile()
        self.assertEqual(a.model_kind_id, "hunyuan.hy4.free")
        self.assertEqual(a.behavior_fingerprint, b.behavior_fingerprint)
        self.assertEqual(a.reasoning_efforts, ("high",))

    def test_m3_tuning_does_not_spread_to_m27_or_unknown_minimax(self):
        m3 = profile_for(model="minimax-m3")
        for name in ("minimax-m2.7", "minimax-m2.7-highspeed", "minimax-future"):
            with self.subTest(model=name):
                other = profile_for(model=name)
                self.assertEqual(other.steps_hint, 0)
                self.assertEqual(other.memory_char_budget, 1500)
                self.assertEqual(other.memory_min_score, 0)
        self.assertEqual((m3.steps_hint, m3.memory_char_budget, m3.memory_min_score), (24, 1000, .3))
        self.assertEqual(PROFILES["minimax"].steps_hint, 0)

    def test_unknown_future_version_inherits_only_manufacturer_defaults(self):
        profile = profile_for(model="deepseek-v99-pro")
        self.assertEqual(profile.manufacturer_id, "deepseek")
        self.assertEqual(profile.model_kind_id, "")
        self.assertEqual(profile.model_evidence, "inherited_unverified")
        self.assertEqual(adapter_for(profile).adapter_id, "deepseek.default")

    def test_dated_family_variant_is_marked_as_inherited(self):
        profile = profile_for(model="hy4-preview-20261104")
        self.assertEqual(profile.model_match_source, "family_prefix")
        self.assertEqual(profile.model_evidence, "family_inherited:registry")
        self.assertEqual(profile.reasoning_efforts, ("high",))
        self.assertNotIn("workbuddy_zero_credit_observed", profile.model_traits)

    def test_no_substring_or_url_false_positive(self):
        for name in ("foo1", "my-claude-custom", "sol-business", "https://x/deepseek-flash"):
            with self.subTest(model=name):
                self.assertEqual(manufacturer_for_model(name), "")
                self.assertIsNone(resolve_model_kind(name)[0])
                self.assertEqual(profile_for(model=name).id, "generic")

    def test_opaque_id_can_be_bound_without_rewriting_wire_model(self):
        provider = self.provider(default_model="deployment-blue", vendor="anthropic",
                                 model_kind_ids={"deployment-blue": "deepseek.v4.pro"})
        shaped, plan = self.plan(provider, "deployment-blue", enable_thinking=True)
        self.assertEqual(shaped["model"], "deployment-blue")
        self.assertEqual(shaped["thinking"], {"type": "enabled"})
        self.assertEqual(plan["adapter_id"], "deepseek.v4.pro")
        self.assertEqual(plan["model_match_source"], "explicit_kind")
        self.assertEqual(plan["behavior_fingerprint"], profile_for(model="deepseek-v4-pro").behavior_fingerprint)

    def test_default_kind_binding_does_not_leak_when_switching_models(self):
        provider = self.provider(default_model="default-deployment", model_kind_id="minimax.m3",
                                 model_kind_ids={"other-deployment": "mimo.v2.6.pro"})
        self.assertEqual(provider.profile().model_kind_id, "minimax.m3")
        self.assertEqual(provider.profile_for_model("other-deployment").model_kind_id, "mimo.v2.6.pro")
        self.assertEqual(provider.profile_for_model("hy4free").model_kind_id, "hunyuan.hy4.free")
        self.assertEqual(provider.profile_for_model("unknown-deployment").model_kind_id, "")

    def test_config_and_primary_context_budget_share_binding(self):
        from .cli import _primary_profile
        from .routing import SmartRouter
        cfg = Config()
        cfg.apply_patch([{"id": "p", "name": "provider:reseller", "config": {
            "baseURL": "https://gateway.example/v1", "model": "deployment-blue",
            "modelKinds": {"deployment-blue": "minimax.m3"}}},
            {"id": "model", "name": "model", "config": {"primary": ["p", "deployment-blue"]}}])
        for router_class in (model.ModelRouter, SmartRouter):
            self.assertEqual(router_class.from_config(cfg).providers["p"].profile().model_kind_id, "minimax.m3")
        self.assertEqual(_primary_profile(cfg).steps_hint, 24)

    def test_gateway_resolves_after_model_map(self):
        cfg = GatewayConfig(upstream="https://gateway.example/v1", wire="openai", upstream_wire="openai",
                            model_map={"client-alias": "deployment-blue"}, provider_options={
                                "modelKinds": {"deployment-blue": "minimax.m3"}})
        cfg.cache_warmth = CacheWarmth()
        shaped, plan, _ = cfg.prepare_request({"model": "client-alias", "messages": [], "enable_thinking": True})
        self.assertEqual(shaped["model"], "deployment-blue")
        self.assertEqual(shaped["thinking"], {"type": "adaptive"})
        self.assertEqual(plan["model_kind_id"], "minimax.m3")

    def test_real_transport_uses_kind_sampling_policy_but_original_wire_id(self):
        captured = []
        def opener(request, **options):
            captured.append(json.loads(request.data))
            return io.BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}')
        provider = self.provider(default_model="deployment-blue", model_kind_id="openai.gpt6.sol", temperature=.7)
        with patch.object(model, "warmth_store", return_value=CacheWarmth()):
            model.HttpTransport(response_opener=opener).complete(provider, "deployment-blue", [], top_p=.5)
        self.assertEqual(captured[0]["model"], "deployment-blue")
        self.assertNotIn("temperature", captured[0])
        self.assertNotIn("top_p", captured[0])

    def test_gateway_default_kind_does_not_leak_to_another_request(self):
        cfg = GatewayConfig(upstream="https://gateway.example/v1", wire="openai", upstream_wire="openai",
                            provider_options={"model": "deployment-blue", "modelKindId": "minimax.m3"})
        cfg.cache_warmth = CacheWarmth()
        for chosen, expected in (("deployment-blue", "minimax.m3"), ("hy4free", "hunyuan.hy4.free")):
            _, plan, _ = cfg.prepare_request({"model": chosen, "messages": []})
            self.assertEqual(plan["model_kind_id"], expected)

    def test_catalogue_retains_failed_evidence_without_exporting_messages(self):
        from pathlib import Path
        from scripts.model_fingerprints import build_catalogue, endpoint_scope
        catalogue = build_catalogue(Path(__file__).resolve().parents[1] / "docs/vendor-adapt-2026-11-04")
        rows = {row["model_kind_id"]: row for row in catalogue["model_kinds"]}
        self.assertTrue(any(obs["scenario_passed"] is False for obs in rows["minimax.m3"]["observations"]))
        self.assertTrue(any(obs.get("error_category") == "model_rejected_on_this_endpoint"
                            for obs in rows["mimo.v2.6.pro-ultraspeed"]["observations"]))
        for row in rows.values():
            for obs in row["observations"]:
                self.assertFalse({"messages", "message", "reasoning_content", "text", "headers"} & obs.keys())
        self.assertEqual(endpoint_scope("https://username:password@example.com/v1?api_key=secret#x"),
                         "https://example.com/v1")

    def test_local_engine_keeps_local_adapter_and_billing(self):
        profile = profile_for(model="minimax-m3", service="ollama")
        self.assertEqual(profile.id, "local")
        self.assertEqual(profile.model_kind_id, "minimax.m3")
        self.assertIsInstance(adapter_for(profile), LocalAdapter)
        self.assertEqual(adapter_for(profile).adapter_id, "local.default")
        self.assertEqual(profile.model_traits, ())
        self.assertEqual(profile.model_evidence, "local_unverified")
        self.assertEqual(profile.cache_mode, "none")
        self.assertEqual(profile.steps_hint, 0)
        self.assertEqual(profile.min_output_tokens, 0)

    def test_invalid_bindings_fail_before_transport(self):
        for options in ({"model_kind_id": "minimax.m999"},
                        {"model_kind_ids": {"x": "minimax.m999"}},
                        {"model_kind_ids": {"x": ""}}, {"model_kind_ids": []}):
            with self.subTest(options=options), self.assertRaises((ValueError, model.BadRequest)):
                self.provider(**options)

    def test_disabled_adaptation_still_reports_resolved_identity(self):
        shaped, plan = self.plan(self.provider(adapt=False), "minimax-m3", enable_thinking=True)
        self.assertEqual(shaped["enable_thinking"], True)
        self.assertNotIn("thinking", shaped)
        self.assertEqual(plan["model_kind_id"], "minimax.m3")

    def test_behavior_identity_does_not_merge_cache_namespaces(self):
        a = self.provider(default_model="hy4free")
        b = model.Provider("other", "https://other.example/v1", default_model="hy4-free")
        _, pa = self.plan(a, "hy4free")
        _, pb = self.plan(b, "hy4-free")
        self.assertEqual(pa["behavior_fingerprint"], pb["behavior_fingerprint"])
        self.assertNotEqual(pa["fingerprint"], pb["fingerprint"])

    def test_model_registry_does_not_mutate_when_switching(self):
        baseline = profile_for(model="minimax-m3").to_raw()
        for chosen in ("minimax-m2.7-highspeed", "hy4free", "kimi-k3"):
            self.plan(self.provider(), chosen)
        self.assertEqual(profile_for(model="minimax-m3").to_raw(), baseline)
        with self.assertRaises(TypeError):
            MODEL_KINDS["bogus"] = MODEL_KINDS["minimax.m3"]

    def test_adapter_lookup_supports_kind_and_legacy_vendor_ids(self):
        self.assertEqual(adapter_for_id("minimax.m3").profile.steps_hint, 24)
        self.assertEqual(adapter_for_id("minimax").profile.steps_hint, 0)
        self.assertEqual(adapter_for_id("not-registered").profile.id, "generic")


if __name__ == "__main__":
    unittest.main()
