"""Offline regressions for provider routing and request adaptation boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import io
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from . import model as supply
from .adapters import PlanContext, adapter_for
from .cache_state import CacheWarmth, prefix_fingerprint
from .context_plan import ContextPlan, estimate_tokens
from .vendors import PROFILES, normalize_usage, profile_for, shape_anthropic, shape_bailian


class ModelAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.store = CacheWarmth()
        self.patcher = patch.object(supply, "warmth_store", return_value=self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def provider(self, **kwargs):
        args = dict(name="test", base_url="https://api.anthropic.com", wire="anthropic",
                    default_model="claude-sonnet-5-5")
        args.update(kwargs)
        return supply.Provider(**args)

    def payload(self):
        return {"system": "S" * 6000, "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "read", "description": "read data", "input_schema": {}}]}

    def plan(self, provider=None, payload=None, model="claude-sonnet-5-5"):
        payload = payload or self.payload()
        return supply._plan_and_shape(payload, payload["messages"], provider or self.provider(), model)

    def test_actual_model_selects_profile_for_multi_vendor_gateway(self):
        provider = self.provider(base_url="https://gateway.example/v1", wire="openai",
                                 default_model="gpt-6.1-sol")
        payload = {"messages": [{"role": "system", "content": "S" * 6000},
                                {"role": "user", "content": "hello"}]}
        _, plan = self.plan(provider, payload, "deepseek-flash")
        self.assertEqual(plan["vendor"], "deepseek")

    def test_cache_off_is_respected(self):
        with patch("forge.pricing.base_rate", return_value={"input": 1, "output": 4}):
            shaped, plan = self.plan(self.provider(cache_control="off"))
        self.assertIsInstance(shaped["system"], str)
        self.assertFalse(plan["cache_control"])
        self.assertFalse(plan["cache_engages"])

    def test_explicit_cache_is_applied_even_for_one_call(self):
        shaped, plan = self.plan(self.provider(cache_control="explicit", expected_calls=1))
        self.assertTrue(plan["cache_control"])
        self.assertIn("cache_control", shaped["system"][-1])

    def test_anthropic_markers_never_leak_to_openai_wire(self):
        payload = {"messages": [{"role": "system", "content": "S" * 6000}],
                   "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}]}
        with patch("forge.pricing.base_rate", return_value={"input": 1, "output": 4}):
            shaped, plan = self.plan(self.provider(wire="openai"), payload)
        self.assertNotIn("cache_control", shaped["tools"][-1])
        self.assertFalse(plan["cache_engages"])

    def test_adaptation_does_not_mutate_caller_payload(self):
        payload = self.payload()
        before = copy.deepcopy(payload)
        self.plan(self.provider(cache_control="explicit"), payload=payload)
        self.assertEqual(payload, before)

    def test_openai_system_prefix_is_hashed_but_growing_history_is_not(self):
        first = {"messages": [{"role": "system", "content": "instructions A"}]}
        second = {"messages": [{"role": "system", "content": "instructions B"}]}
        self.assertNotEqual(prefix_fingerprint(first), prefix_fingerprint(second))
        growing = copy.deepcopy(first)
        growing["messages"].append({"role": "user", "content": "next input"})
        self.assertEqual(prefix_fingerprint(first), prefix_fingerprint(growing))

    def test_structured_system_prefix_preserves_nontext_data(self):
        a = {"system": [{"type": "image", "source": {"data": "a"}}]}
        b = {"system": [{"type": "image", "source": {"data": "b"}}]}
        self.assertNotEqual(prefix_fingerprint(a), prefix_fingerprint(b))

    def test_openai_system_tokens_are_counted_once(self):
        payload = {"messages": [{"role": "system", "content": "S" * 6000},
                                {"role": "user", "content": "hello"}]}
        adapter = adapter_for(PROFILES["openai"])
        plan = adapter.plan(payload, payload["messages"], PlanContext(PROFILES["openai"], base_input=1))
        self.assertGreater(adapter.prefix_tokens(payload, payload["messages"]), 2000)
        self.assertTrue(plan.cache_engages)
        self.assertLess(plan.est_tokens, 2600)

    def test_noncacheable_message_tail_is_charged_at_full_rate(self):
        payload = self.payload()
        payload["messages"] = [{"role": "user", "content": "x" * 20000}]
        adapter = adapter_for(PROFILES["anthropic"])
        ctx = PlanContext(PROFILES["anthropic"], base_input=1, calls_expected=2)
        plan = adapter.plan(payload, payload["messages"], ctx)
        prefix = adapter.prefix_tokens(payload, payload["messages"])
        expected = (prefix * (1.25 + .1) + estimate_tokens(payload["messages"]) * 2) / 1000000
        self.assertAlmostEqual(plan.est_cost, expected)

    def test_warm_prefix_current_call_is_not_free(self):
        payload = self.payload()
        adapter = adapter_for(PROFILES["anthropic"])
        plan = adapter.plan(payload, payload["messages"],
                            PlanContext(PROFILES["anthropic"], base_input=1, calls_expected=1, warm=True))
        expected = (adapter.prefix_tokens(payload, payload["messages"]) * .1
                    + estimate_tokens(payload["messages"])) / 1000000
        self.assertAlmostEqual(plan.est_cost, expected)

    def test_long_ttl_prices_the_actual_write_premium(self):
        payload = self.payload()
        adapter = adapter_for(PROFILES["anthropic"])
        plan = adapter.plan(payload, payload["messages"],
                            PlanContext(PROFILES["anthropic"], base_input=1, calls_expected=4, gap_seconds=600))
        self.assertEqual(plan.ttl, "1h")
        expected = (adapter.prefix_tokens(payload, payload["messages"]) * (2 + .1 * 3)
                    + estimate_tokens(payload["messages"]) * 4) / 1000000
        self.assertAlmostEqual(plan.est_cost, expected)

    def test_cache_namespace_separates_models_endpoints_and_accounts(self):
        provider = self.provider()
        _, first = self.plan(provider)
        variants = [self.plan(provider, model="claude-haiku-4-5")[1],
                    self.plan(self.provider(base_url="https://gateway.example", vendor="anthropic"))[1],
                    self.plan(self.provider(api_key="test-secret"))[1]]
        for variant in variants:
            self.assertNotEqual(first["fingerprint"], variant["fingerprint"])
        self.assertNotIn("test-secret", json.dumps(variants))

    def test_success_without_cache_telemetry_does_not_claim_warmth(self):
        provider = self.provider()
        with patch("forge.pricing.base_rate", return_value={"input": 1, "output": 4}):
            _, plan = self.plan(provider)
        supply._remember_warmth(provider, plan)
        self.assertFalse(self.store.is_warm(PROFILES["anthropic"], plan["fingerprint"]))

    def test_corrupt_warmth_record_cannot_break_model_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text('{"seen":{"anthropic":{"bad":"invalid"}}}', encoding="utf-8")
            store = CacheWarmth(path)
            self.assertFalse(store.is_warm(PROFILES["anthropic"], "bad"))

    def test_future_cache_timestamp_is_not_warm(self):
        self.store.remember("anthropic", "prefix", when=1000)
        self.assertFalse(self.store.is_warm(PROFILES["anthropic"], "prefix", when=900))

    def test_usage_rejects_nonfinite_and_negative_counts(self):
        usage = normalize_usage(PROFILES["openai"], {"prompt_tokens": float("inf"),
                                "completion_tokens": -5, "cached_tokens": float("nan")})
        self.assertTrue(all(value >= 0 for value in usage.values()))

    def test_vendor_identity_uses_hostname_not_path_or_suffix(self):
        for url in ("https://api.openai.com.attacker.example/v1",
                    "https://other.example/api.openai.com/v1"):
            self.assertEqual(profile_for(base_url=url).id, "generic")

    def test_request_plan_is_available_on_routed_completion(self):
        class Transport:
            def complete(self, provider, model, messages, **kwargs):
                return "ok", supply.Usage(), {"plan": {"vendor": "anthropic", "saving": .01}}
        router = supply.ModelRouter([self.provider()], transport=Transport())
        result = router.complete([{"role": "user", "content": "hello"}])
        self.assertEqual(result.plan["vendor"], "anthropic")

    def test_context_budget_uses_chars_not_tokens(self):
        profile = PROFILES["gemini"]
        self.assertEqual(ContextPlan(mode="compact").adjust_budget(800000, profile),
                         int(profile.long_context_threshold * .85 * 2.5))

    def test_context_policy_reads_wizard_and_cli_config_shape(self):
        from .config import Config
        cfg = Config()
        cfg.apply_patch([{"id": "context", "name": "context:policy", "config": {
            "mode": "lossless", "defaultMode": "lossless", "keepTail": 12}}])
        plan = ContextPlan.from_config(cfg)
        self.assertEqual((plan.mode, plan.default_mode, plan.keep_tail), ("lossless", "lossless", 12))

    def test_wizard_keys_survive_a_process_restart_and_medium_is_first(self):
        from .cmd_setup import build_patch
        from .config import Config
        from .routing import SmartRouter
        plan = {tier: ("deepseek", "deepseek-flash") for tier in ("lite", "medium", "premium")}
        rows = build_patch(plan, {"deepseek": "test-key"}, {"mode": "lossless"})
        cfg = Config()
        cfg.apply_patch(rows)
        with patch.dict("os.environ", {}, clear=True):
            router = SmartRouter.from_config(cfg)
        self.assertEqual(router.providers["medium"].api_key, "test-key")
        self.assertEqual(router._order(None)[0][0], "medium")

    def test_smart_router_preserves_adaptation_plan(self):
        from .routing import RoutingConfig, SmartRouter
        class Transport:
            def complete(self, provider, model, messages, **kwargs):
                return "ok", supply.Usage(), {"plan": {"vendor": "anthropic"}}
        router = SmartRouter([self.provider()], transport=Transport(),
                             routing=RoutingConfig(tiers=[("test", "claude-sonnet-5-5")]))
        self.assertEqual(router.complete([]).plan["vendor"], "anthropic")

    def test_transport_ignores_malformed_usage_without_crashing(self):
        response = {"choices": [{"message": {"content": "ok"}}], "usage": {
            "prompt_tokens": float("inf"), "completion_tokens": -1, "cached_tokens": float("nan")}}
        with patch.object(supply.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            _, usage, _ = supply.HttpTransport().complete(self.provider(wire="openai", adapt=False),
                                                          "unknown-model", [{"role": "user", "content": "hello"}])
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens), (0, 0, 0))

    def test_confirmed_cache_usage_is_scoped_to_the_actual_request(self):
        provider = self.provider()
        _, plan = self.plan(self.provider(cache_control="explicit"))
        supply._remember_warmth(provider, plan, {"cache_write_tokens": 2400})
        self.assertTrue(self.store.is_warm(PROFILES["anthropic"], plan["fingerprint"]))
        _, other = self.plan(self.provider(cache_control="explicit", api_key="other"))
        self.assertFalse(self.store.is_warm(PROFILES["anthropic"], other["fingerprint"]))

    def test_many_tool_definitions_still_get_a_single_cache_breakpoint(self):
        payload = {"tools": [{"name": f"t{n}", "input_schema": {}} for n in range(60)]}
        shape_anthropic(payload, PROFILES["anthropic"])
        self.assertEqual(sum("cache_control" in tool for tool in payload["tools"]), 1)

    def test_bailian_marks_stable_system_instead_of_changing_user_tail(self):
        payload = {"messages": [{"role": "system", "content": "S" * 6000},
                                {"role": "user", "content": "changing input"}]}
        shape_bailian(payload, PROFILES["bailian"])
        self.assertIsInstance(payload["messages"][0]["content"], list)
        self.assertEqual(payload["messages"][1]["content"], "changing input")

    def test_parallel_cache_reads_writes_and_saves_produce_valid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            store = CacheWarmth(path)
            def worker(n):
                for index in range(20):
                    store.remember("anthropic", f"{n}:{index}")
                    store.is_warm(PROFILES["anthropic"], f"{n}:{index}")
                    store.save()
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(worker, range(4)))
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["seen"]["anthropic"]), 80)

    def test_context_profile_follows_the_actual_strategy_and_model(self):
        from .config import Config
        from .cli import _primary_profile
        cfg = Config()
        cfg.apply_patch([
            {"id": "gateway", "name": "provider:custom", "config": {
                "baseURL": "https://gateway.example", "model": "gpt-6.1-sol"}},
            {"id": "model", "name": "model:router", "config": {
                "primary": ["gateway", "gpt-6.1-sol"], "routing": {
                    "strategy": "medium", "tiers": [["gateway", "gemini-3.1-pro"]]}}}])
        self.assertEqual(_primary_profile(cfg).id, "gemini")

    def test_premium_preserves_cache_and_reasoning_usage_from_both_stages(self):
        from .routing import RoutingConfig, SmartRouter
        class Transport:
            def complete(self, provider, model, messages, **kwargs):
                return "ok", supply.Usage(prompt_tokens=100, completion_tokens=10,
                    cached_tokens=40, cache_write_tokens=20, reasoning_tokens=5), {}
        router = SmartRouter([self.provider()], transport=Transport(), routing=RoutingConfig(
            strategy="premium", tiers=[("test", "draft")], premium=[("test", "review")]))
        usage = router.complete([{"role": "user", "content": "task"}]).usage
        self.assertEqual((usage.cached_tokens, usage.cache_write_tokens, usage.reasoning_tokens), (80, 40, 10))

    def test_disabled_context_policy_keeps_default_behavior(self):
        from .config import Config
        cfg = Config()
        cfg.apply_patch([{"id": "context", "name": "context:policy", "disabled": True,
                          "config": {"mode": "lossless"}}])
        self.assertEqual(ContextPlan.from_config(cfg).mode, "ask")


if __name__ == "__main__":
    unittest.main()
