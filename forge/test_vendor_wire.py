"""Vendor wire regressions backed by the local November 4 handoff evidence."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import model
from .cache_state import CacheWarmth, prefix_fingerprint
from .gateway import GatewayConfig
from .loop import Agent, LoopLimits
from .policy import Mode, Policy
from .tools import build_builtin_registry
from .vendors import PROFILES, normalize_usage, profile_for, shape_reasoning


ENDPOINTS = {
    "deepseek-flash": "https://api.deepseek.com",
    "mimo-v2.6-flash": "https://token-plan-cn.xiaomimimo.com/v1",
    "minimax-m3": "https://api.minimaxi.com/v1",
    "doubao-seed-evolving": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "glm-5.3": "https://open.bigmodel.cn/api/coding/paas/v4",
    "qwen3.8-max": "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1",
}


class VendorWireTests(unittest.TestCase):
    def setUp(self):
        stub = patch.object(model, "warmth_store", return_value=CacheWarmth())
        stub.start()
        self.addCleanup(stub.stop)

    def transport_call(self, chosen, *, response=None, **options):
        captured = []
        response = response if response is not None else {
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5}}

        def opener(request, **kwargs):
            captured.append((request.full_url, json.loads(request.data)))
            return io.BytesIO(json.dumps(response).encode())

        provider = model.Provider("probe", ENDPOINTS[chosen], default_model=chosen)
        result = model.HttpTransport(response_opener=opener).complete(
            provider, chosen, [{"role": "system", "content": "stable"},
                               {"role": "user", "content": "hello"}], **options)
        return captured[0], result

    def test_gateway_and_direct_share_reasoning_dialects(self):
        for chosen, expected in [
                ("deepseek-flash", {"type": "enabled"}),
                ("mimo-v2.6-flash", {"type": "enabled"}),
                ("minimax-m3", {"type": "adaptive"}),
                ("doubao-seed-evolving", {"type": "enabled"}),
                ("glm-5.3", {"type": "enabled"})]:
            with self.subTest(model=chosen):
                (_, direct), (_, _, extra) = self.transport_call(chosen, enable_thinking=True)
                cfg = GatewayConfig(upstream=ENDPOINTS[chosen], api_key="", models=[chosen],
                                    wire="openai", upstream_wire="openai")
                cfg.cache_warmth = CacheWarmth()
                original = {"model": chosen, "messages": [{"role": "user", "content": "hi"}],
                            "enable_thinking": True}
                before = copy.deepcopy(original)
                gateway, plan, _ = cfg.prepare_request(original)
                self.assertEqual(direct.get("thinking"), expected)
                self.assertEqual(gateway.get("thinking"), expected)
                self.assertNotIn("enable_thinking", gateway)
                self.assertEqual(before, original)
                self.assertTrue(extra["plan"]["reasoning_shaped"])
                self.assertTrue(plan["reasoning_shaped"])

    def test_gateway_auto_does_not_send_invalid_thinking_type(self):
        cfg = GatewayConfig(upstream=ENDPOINTS["doubao-seed-evolving"], api_key="", models=[],
                            wire="openai", upstream_wire="openai")
        cfg.cache_warmth = CacheWarmth()
        payload, _, _ = cfg.prepare_request({"model": "doubao-seed-evolving", "messages": [],
                                             "thinking": {"type": "auto"}})
        self.assertNotIn("thinking", payload)

    def test_explicit_small_output_cap_is_honoured(self):
        for chosen in ("minimax-m3", "deepseek-flash", "doubao-seed-evolving", "mimo-v2.6-flash"):
            with self.subTest(model=chosen):
                (_, payload), _ = self.transport_call(chosen, max_tokens=128, enable_thinking=False)
                self.assertEqual(payload["max_tokens"], 128)

    def test_default_output_reserves_room_for_reasoning(self):
        (_, payload), _ = self.transport_call("minimax-m3")
        self.assertEqual(payload["max_tokens"], 4096)

    def test_explicit_execution_budget_survives_adaptation(self):
        (_, payload), (_, _, extra) = self.transport_call("deepseek-flash", think_budget=512)
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(extra["plan"]["execution"]["reasoning"]["tokens"], 512)

    def test_disabled_adapter_preserves_requested_dialect(self):
        provider = model.Provider("probe", ENDPOINTS["minimax-m3"], adapt=False)
        original = {"messages": [], "thinking": {"type": "enabled"}, "max_tokens": 64}
        shaped, _ = model._plan_and_shape(original, [], provider, "minimax-m3")
        self.assertEqual(original, shaped)

    def test_model_specific_hy4_has_only_high_effort(self):
        for name in ("hy4-preview", "hy4free", "hy4-free"):
            profile = profile_for(model=name)
            shaped, _ = shape_reasoning({"reasoning_effort": "low"}, profile)
            self.assertEqual(shaped["reasoning_effort"], "high")
        self.assertEqual(PROFILES["hunyuan"].reasoning_efforts, ("low", "high"))

    def test_workbuddy_hy4_usage_does_not_confuse_prompt_reasoning_with_output(self):
        # Real WorkBuddy hy4free tool-roundtrip receipt. The two details objects
        # both contain reasoning_tokens; only the output-side value is CoT use.
        raw = {"prompt_tokens": 252, "completion_tokens": 217, "credit": 0,
               "completion_tokens_details": {"reasoning_tokens": 198, "cached_tokens": 0},
               "prompt_tokens_details": {"reasoning_tokens": 0, "cached_tokens": 0},
               "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 252}
        usage = normalize_usage(profile_for(model="hy4-free"), raw)
        self.assertEqual(usage["reasoning_tokens"], 198)
        self.assertEqual(usage["input_tokens"], 252)
        self.assertEqual(usage["output_tokens"], 217)
        self.assertEqual(usage["cached_tokens"], 0)

    def test_hy4_adapter_reports_its_actual_tier(self):
        provider = model.Provider("workbuddy-probe", "http://localhost", vendor="hunyuan")
        _, plan = model._plan_and_shape({"messages": [], "reasoning_effort": "low"},
                                       [], provider, "hy4free")
        self.assertTrue(any("effort 档位 high" in note for note in plan["notes"]))
        self.assertFalse(any("low/high" in note for note in plan["notes"]))

    def test_unverified_disable_does_not_claim_thinking_is_off(self):
        for vendor in ("openai", "kimi"):
            shaped, actions = shape_reasoning({"enable_thinking": False}, PROFILES[vendor])
            self.assertEqual(shaped, {})
            self.assertFalse(any("已按请求关闭" in action for action in actions))
            self.assertTrue(any("尚无已验证" in action for action in actions))

    def test_kimi_coding_cannot_be_turned_off(self):
        profile = profile_for(base_url="https://api.kimi.com/coding/v1", model="k3.1-pro")
        self.assertFalse(profile.can_disable_thinking)
        shaped, actions = shape_reasoning({"enable_thinking": False}, profile)
        self.assertEqual(shaped, {})
        self.assertTrue(any("不可关闭" in action for action in actions))

    def test_openai_does_not_infer_all_effort_levels_from_one_config(self):
        shaped, _ = shape_reasoning({"reasoning_effort": "xhigh"}, PROFILES["openai"])
        self.assertEqual(shaped["reasoning_effort"], "xhigh")

    def test_deepseek_retains_its_verified_effort_parameter(self):
        (_, payload), _ = self.transport_call("deepseek-flash", reasoning_effort="high")
        self.assertEqual(payload["reasoning_effort"], "high")
        shaped, _ = shape_reasoning({"reasoning_effort": "high", "enable_thinking": False},
                                     PROFILES["deepseek"])
        self.assertNotIn("reasoning_effort", shaped)
        self.assertEqual(shaped["thinking"], {"type": "disabled"})

    def test_anthropic_output_limit_is_reported(self):
        response = {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}],
                    "usage": {"input_tokens": 2, "output_tokens": 64}}
        provider = model.Provider("test", "https://example.invalid", wire="anthropic")
        transport = model.HttpTransport(response_opener=lambda *a, **kw: io.BytesIO(json.dumps(response).encode()))
        _, usage, extra = transport.complete(provider, "test", [{"role": "user", "content": "hello"}])
        self.assertEqual(extra["finish_reason"], "max_tokens")
        self.assertEqual(usage.completion_tokens, 64)

    def test_canonical_cache_fields_win_regardless_of_json_order(self):
        for fields in [
                [("prompt_tokens_details", {"cached_tokens": 80}), ("cached_tokens", 0)],
                [("cached_tokens", 0), ("prompt_tokens_details", {"cached_tokens": 80})]]:
            normalized = normalize_usage(PROFILES["deepseek"], dict(fields))
            self.assertEqual(normalized["cached_tokens"], 80)
        output_only = {"output_tokens_details": {"cached_tokens": 99}}
        self.assertNotIn("cached_tokens", normalize_usage(PROFILES["openai"], output_only))

    def test_fingerprint_uses_the_translated_wire(self):
        provider = model.Provider("p", ENDPOINTS["minimax-m3"])
        source = {"messages": [], "enable_thinking": True}
        shaped, plan = model._plan_and_shape(source, [], provider, "minimax-m3")
        expected = prefix_fingerprint(shaped, model="minimax-m3", endpoint=provider.base_url,
            account=provider._credential._account_fingerprint, wire=provider.wire,
            cache_mode=provider.cache_control)
        self.assertEqual(plan["fingerprint"], expected)
        self.assertNotIn("thinking", source)

    def test_qwen_disable_wins_over_configured_budget(self):
        provider = model.Provider("p", ENDPOINTS["qwen3.8-max"], think_budget=1024)
        shaped, plan = model._plan_and_shape({"messages": [], "enable_thinking": False},
                                            [], provider, "qwen3.8-max")
        self.assertFalse(shaped["enable_thinking"])
        self.assertNotIn("thinking_budget", shaped)
        self.assertEqual(plan["execution"]["reasoning"]["status"], "explicit_override")

    def test_batch_uses_the_same_vendor_dialect(self):
        from .batch_execution import BatchExecutor
        captured = []

        def opener(request, **kwargs):
            captured.append(request.data)
            answer = {"id": "file-stub"} if request.full_url.endswith("/files") else {"id": "batch-stub"}
            return io.BytesIO(json.dumps(answer).encode())

        with tempfile.TemporaryDirectory() as tmp:
            provider = model.Provider("qwen", ENDPOINTS["qwen3.8-max"])
            job = BatchExecutor(provider, tmp, opener=opener).submit([{
                "custom_id": "case", "body": {"model": "qwen3.8-max", "messages": [],
                                               "thinking": {"type": "enabled"}}}])
            self.assertEqual(job["id"], "batch-stub")
            self.assertIn(b'"enable_thinking": true', captured[0])
            self.assertNotIn(b'"thinking":', captured[0])

    def test_assistant_replay_retains_encrypted_and_reasoning_fields(self):
        message = {"role": "assistant", "content": "ok", "encrypted_content": "opaque-record",
                   "reasoning_content": "vendor reasoning"}
        response = {"choices": [{"message": message, "finish_reason": "stop"}]}
        _, (text, _, extra) = self.transport_call("doubao-seed-evolving", response=response)
        self.assertEqual(text, "ok")
        self.assertEqual(extra["assistant_message"], message)

    def test_truncation_keeps_usage_and_never_executes_partial_tools(self):
        response = {"choices": [{"finish_reason": "length", "message": {
            "content": "<think>still reasoning", "tool_calls": [{"id": "cut", "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"bad.txt"'}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 128}}
        _, (text, usage, extra) = self.transport_call("minimax-m3", response=response, max_tokens=128)
        self.assertEqual(text, "")
        self.assertEqual(usage.completion_tokens, 128)
        self.assertEqual(extra["finish_reason"], "length")
        self.assertEqual(extra["tool_calls"], [])

        class Truncated:
            def complete(self, *args, **kwargs):
                return text, usage, extra

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = model.Provider("probe", "https://example.invalid", default_model="test")
            router = model.ModelRouter([provider], transport=Truncated(), retries_per_provider=0)
            agent = Agent(home=root, workspace=root, router=router, registry=build_builtin_registry(),
                          policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=root, non_interactive=True),
                          limits=LoopLimits(max_steps=2), extensions={})
            report = agent.run("write bad.txt")
            self.assertEqual(report.stopped, "output_limit")
            self.assertEqual(report.as_result().outcome, "failed")
            self.assertEqual(report.usage["completion_tokens"], 128)
            self.assertFalse((root / "bad.txt").exists())


if __name__ == "__main__":
    unittest.main()
