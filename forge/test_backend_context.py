"""Backend integration regressions: no upstream API calls or real credentials."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import model
from .cache_state import CacheWarmth
from .cache_metrics import annotate_requests, cache_coverage
from .compaction import ContextBudget
from .context_transaction import archive_source, compact_transaction, digest, valid_tool_pairs
from .gateway import GatewayConfig
from .loop import Agent, LoopLimits
from .policy import Mode, Policy
from .protocol import _cache_trajectory
from .route_capabilities import effective_capabilities, preview_budget, request_budget
from .tools import build_builtin_registry


class ContextTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.messages = [{"role": "system", "content": "stable system"},
                         {"role": "user", "content": "task pinned"},
                         *[{"role": "assistant", "content": f"{i}:" + "x" * 1000} for i in range(12)],
                         {"role": "user", "content": "latest"}]
        self.compactor = ContextBudget(max_chars=2000, keep_tail=2)

    def compact(self, **options):
        return compact_transaction(self.messages, self.compactor, directory=self.tmp.name,
                                   max_chars=2000, pinned_contents=("task pinned",), **options)

    def test_archive_precedes_rewrite_and_checkpoint_covers_source(self):
        original = copy.deepcopy(self.messages)
        original_compact = self.compactor.compact

        def compact(*args, **kwargs):
            files = list(Path(self.tmp.name).glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["messages"], original)
            return original_compact(*args, **kwargs)

        with patch.object(self.compactor, "compact", side_effect=compact):
            candidate, audit = self.compact()
        self.assertTrue(audit["committed"])
        self.assertEqual(candidate[:2], original[:2])
        self.assertEqual(self.messages, original)
        self.assertEqual(audit["candidate_digest"], digest(candidate))
        self.assertEqual(audit["checkpoint"]["coverage"]["end_exclusive"], len(original))
        self.assertEqual(audit["checkpoint"]["summary_status"], "unverified_excerpt")

    def test_archive_failure_never_runs_compactor(self):
        with patch.object(self.compactor, "compact") as compact:
            candidate, audit = self.compact(archiver=lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        compact.assert_not_called()
        self.assertIs(candidate, self.messages)
        self.assertEqual(audit["reason"], "transaction_failed:OSError")

    def test_source_mutation_after_archive_refuses_commit(self):
        def archive(*args):
            saved = archive_source(*args)
            self.messages[-1]["content"] = "new user input"
            return saved
        candidate, audit = self.compact(archiver=archive)
        self.assertIs(candidate, self.messages)
        self.assertEqual(candidate[-1]["content"], "new user input")
        self.assertEqual(audit["reason"], "source_or_policy_changed")

    def test_policy_change_during_validation_refuses_commit(self):
        policy = {"model": "a"}
        def validate(candidate):
            policy["model"] = "b"
            return True
        candidate, audit = self.compact(validate=validate, state=lambda: policy)
        self.assertIs(candidate, self.messages)
        self.assertFalse(audit["committed"])

    def test_failed_fit_and_cancellation_keep_original(self):
        for options in ({"validate": lambda _: False}, {"cancelled": lambda: True}):
            candidate, audit = self.compact(**options)
            self.assertIs(candidate, self.messages)
            self.assertFalse(audit["committed"])

    def test_changed_system_or_invalid_tool_group_refuses_commit(self):
        for bad in ([{"role": "user", "content": "changed"}],
                    [*self.messages[:2], {"role": "tool", "tool_call_id": "orphan", "content": "ok"}]):
            with patch.object(self.compactor, "compact", return_value=bad):
                candidate, audit = self.compact()
            self.assertIs(candidate, self.messages)
            self.assertFalse(audit["committed"])

    def test_developer_prefix_is_preserved(self):
        self.messages.insert(1, {"role": "developer", "content": "host instructions"})
        candidate, audit = self.compact()
        self.assertTrue(audit["committed"])
        self.assertEqual(candidate[:3], self.messages[:3])

    def test_tool_groups_survive_cut_without_orphans(self):
        for anthropic in (False, True):
            call = {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}], "content": ""}
            results = [{"role": "tool", "tool_call_id": c, "content": ""} for c in ("a", "b")]
            if anthropic:
                call = {"role": "assistant", "content": [{"type": "tool_use", "id": c} for c in ("a", "b")]}
                results = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": c} for c in ("a", "b")]}]
            self.messages = self.messages[:10] + [call, *results]
            candidate, audit = self.compact()
            self.assertTrue(audit["committed"])
            self.assertTrue(valid_tool_pairs(candidate))
            self.assertEqual(candidate[-len(results)-1:], [call, *results])

    def test_archive_uses_secret_redaction(self):
        record = archive_source(self.tmp.name, "fixture", [{"api_key": "not-a-real-credential", "content": "safe"}])
        text = Path(record["path"]).read_text(encoding="utf-8")
        self.assertNotIn("not-a-real-credential", text)


class RouteCapabilityTests(unittest.TestCase):
    def setUp(self):
        stub = patch.object(model, "warmth_store", return_value=CacheWarmth())
        stub.start()
        self.addCleanup(stub.stop)

    def provider(self, **options):
        return model.Provider("p", "https://example.invalid/v1", default_model="deployment",
                              model_kind_ids={"deployment": "deepseek.v4.flash"}, **options)

    def test_metadata_does_not_rewrite_payload_or_cache_key(self):
        payload = {"model": "deployment", "messages": [{"role": "system", "content": "stable"},
                    {"role": "user", "content": "hi"}], "max_tokens": 200}
        before = copy.deepcopy(payload)
        a, plan_a = model._plan_and_shape(payload, payload["messages"], self.provider(), "deployment")
        b, plan_b = model._plan_and_shape(payload, payload["messages"], self.provider(
            route_capabilities={"contextWindow": 100000, "maxOutputTokens": 4096, "cacheTailRule": "deepseek-128"}), "deployment")
        self.assertEqual(a, b)
        self.assertEqual(plan_a["fingerprint"], plan_b["fingerprint"])
        self.assertEqual(plan_a["model_kind_id"], plan_b["model_kind_id"])
        self.assertEqual(payload, before)

    def test_route_and_model_constraints_intersect_without_alias_leak(self):
        provider = self.provider(route_capabilities={"contextWindow": 8000, "tools": False,
                                                     "reasoningEfforts": ["low", "high"]},
            model_capabilities={"deployment": {"contextWindow": 16000, "tools": True,
                                               "reasoningEfforts": ["high", "max"], "maxOutputTokens": 1000}})
        caps = provider.capabilities_for_model("deployment")
        self.assertEqual(caps, {"contextWindow": 8000, "tools": False, "reasoningEfforts": ["high"], "maxOutputTokens": 1000})
        self.assertNotIn("maxOutputTokens", provider.capabilities_for_model("other"))

    def test_cacheable_wire_is_identical_across_manufacturers(self):
        for chosen, wire in [("deepseek-flash", "openai"), ("minimax-m3", "openai"),
                             ("mimo-v2.6-flash", "openai"), ("hy4free", "openai"),
                             ("qwen3.8-max", "openai"), ("claude-sonnet-5", "anthropic")]:
            with self.subTest(model=chosen):
                messages = [{"role": "system", "content": "stable prefix " * 1800},
                            {"role": "user", "content": "unchanged task"}]
                payload = {"model": chosen, "messages": messages, "max_tokens": 300,
                           "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]}
                if wire == "anthropic":
                    payload.update(system=messages[0]["content"], messages=messages[1:])
                    payload["tools"] = [{"name": "read", "input_schema": {"type": "object"}}]
                plain = model.Provider("same", "https://example.invalid", default_model=chosen, wire=wire)
                constrained = model.Provider("same", "https://example.invalid", default_model=chosen, wire=wire,
                    route_capabilities={"contextWindow": 100000, "maxOutputTokens": 4096, "tools": True})
                before, bp = model._plan_and_shape(payload, messages, plain, chosen)
                after, ap = model._plan_and_shape(payload, messages, constrained, chosen)
                self.assertEqual(json.dumps(before, ensure_ascii=False).encode(), json.dumps(after, ensure_ascii=False).encode())
                self.assertEqual(bp["fingerprint"], ap["fingerprint"])
                self.assertEqual(bp["cache_control"], ap["cache_control"])

    def test_config_and_smart_router_preserve_deployment_constraints(self):
        from .config import Config
        from .routing import SmartRouter
        cfg = Config()
        cfg.apply_patch([{"id": "p", "name": "provider:test", "config": {
            "baseURL": "https://example.invalid", "model": "deployment",
            "modelKinds": {"deployment": "deepseek.v4.flash"},
            "routeCapabilities": {"contextWindow": 10000},
            "modelCapabilities": {"deployment": {"maxOutputTokens": 1000}}}}])
        for router in (model.ModelRouter.from_config(cfg), SmartRouter.from_config(cfg)):
            self.assertEqual(router.providers["p"].capabilities_for_model("deployment"),
                             {"contextWindow": 10000, "maxOutputTokens": 1000})

    def test_adapt_disabled_still_enforces_declared_output_limit(self):
        provider = model.Provider("unknown", "https://example.invalid", adapt=False,
                                  route_capabilities={"contextWindow": 10000, "maxOutputTokens": 100})
        payload, plan = model._plan_and_shape({"messages": [{"role": "user", "content": "hi"}]}, [], provider, "unknown")
        self.assertEqual(payload["max_tokens"], 100)
        self.assertEqual(plan["request_budget"]["output_reserve"], 100)

    def test_invalid_declarations_fail_early(self):
        for caps in ({"contextWindow": True}, {"tools": "yes"}, {"madeUp": 2},
                     {"cacheTailRule": "guess"}, {"reasoningEfforts": ["high", "high"]}):
            with self.assertRaises(model.BadRequest):
                self.provider(route_capabilities=caps)
        with self.assertRaises(model.BadRequest):
            self.provider(model_capabilities={3: {}})

    def test_tools_replay_and_output_reserve_count_towards_capacity(self):
        payload = {"messages": [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "function": {"arguments": "z" * 6000}}]}],
            "tools": [{"description": "y" * 3000}], "max_tokens": 1000}
        budget = request_budget(payload, {"contextWindow": 4000})
        self.assertFalse(budget["fits"])
        self.assertEqual(budget["input_budget"], 3000)
        with self.assertRaises(model.BadRequest):
            model._plan_and_shape(payload, payload["messages"], self.provider(
                route_capabilities={"contextWindow": 4000}), "deployment")

    def test_direct_default_cap_and_missing_cache_evidence(self):
        captured = []
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}],
                                          "usage": {"prompt_tokens": 50}}).encode())
        transport = model.HttpTransport(response_opener=opener)
        provider = self.provider(route_capabilities={"maxOutputTokens": 300})
        _, usage, _ = transport.complete(provider, "deployment", [{"role": "user", "content": "hello"}])
        self.assertEqual(captured[0]["max_tokens"], 300)
        self.assertIs(usage.cache_observed, False)
        with self.assertRaises(model.BadRequest):
            transport.complete(provider, "deployment", [{"role": "user", "content": "hello"}], max_tokens=301)
        self.assertEqual(len(captured), 1)

    def test_gateway_and_direct_reject_same_route_restrictions(self):
        for caps, extra in [({"tools": False}, {"tools": [{"type": "function"}]}),
                            ({"reasoningEfforts": ["low"]}, {"reasoning_effort": "high"}),
                            ({"maxOutputTokens": 100}, {"max_tokens": 101})]:
            payload = {"model": "deployment", "messages": [{"role": "user", "content": "hello"}], **extra}
            provider = self.provider(route_capabilities=caps)
            with self.assertRaises(model.BadRequest):
                model._plan_and_shape(payload, payload["messages"], provider, "deployment")
            gateway = GatewayConfig(upstream="https://example.invalid/v1", api_key="", models=["deployment"],
                wire="openai", upstream_wire="openai", provider_options={
                    "modelKinds": {"deployment": "deepseek.v4.flash"}, "routeCapabilities": caps})
            with self.assertRaises(model.BadRequest):
                gateway.prepare_request(payload)


class CacheCoverageTests(unittest.TestCase):
    def request(self, prompt=362, cached=128, **fields):
        return {"provider": "p", "model": "m", "usage": {"prompt_tokens": prompt, "cached_tokens": cached}, **fields}

    def test_raw_and_eligible_denominators_remain_separate(self):
        req = self.request(plan={"route_capabilities": {"cacheTailRule": "deepseek-128"}})
        before = copy.deepcopy(req)
        metrics = cache_coverage([req])
        self.assertEqual(metrics["all_observed"]["hit_rate"], .3536)
        self.assertEqual(metrics["eligible_prefix"]["hit_rate"], 1.0)
        self.assertEqual(metrics["eligible_prefix"]["excluded_tail_tokens"], 234)
        self.assertEqual(req, before)
        self.assertEqual(cache_coverage([self.request()])["eligible_prefix"]["requests"], 0)

    def test_rebuild_per_route_and_revision_leaves_judge_and_costs_untouched(self):
        seen = {}
        rows = [self.request(), self.request(stage="integrate")]
        marked = annotate_requests(rows, 1, "transaction", seen)
        self.assertIn("cache_rebuild", marked[0])
        self.assertNotIn("cache_rebuild", marked[1])
        self.assertNotIn("cache_rebuild", rows[0])
        next_rows = annotate_requests([self.request()], 1, "transaction", seen)
        self.assertNotIn("cache_rebuild", next_rows[0])
        metrics = cache_coverage(marked + next_rows)
        self.assertEqual(metrics["all_observed"]["input_tokens"], 3 * 362)
        self.assertEqual(metrics["rebuild"]["requests"], 1)
        self.assertEqual(metrics["non_rebuild"]["requests"], 2)

    def test_unknown_invalid_and_duplicates_are_explicit(self):
        row = self.request(request_id="r", attempt_id="a")
        unknown = self.request()
        unknown["usage"]["cache_observed"] = False
        data = [row, copy.deepcopy(row), unknown, self.request(10, 11), self.request(float("nan"), 0), None]
        metrics = cache_coverage(data)
        self.assertEqual(metrics["all_observed"]["requests"], 1)
        self.assertEqual(metrics["unknown_requests"], 2)
        self.assertEqual(metrics["invalid_requests"], 2)
        self.assertEqual(metrics["duplicate_observations"], 1)
        self.assertIn("coverage", _cache_trajectory({"requests": data}))


class BackendLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def agent(self, caps=None, transport=None):
        provider = model.Provider("p", "https://example.invalid", default_model="m", route_capabilities=caps or {})
        transport = transport or _RecordedTransport()
        router = model.ModelRouter([provider], transport=transport, retries_per_provider=0)
        return Agent(home=self.root, workspace=self.root, router=router, registry=build_builtin_registry(),
                     policy=Policy(mode=Mode.PLAN, workspace=self.root, non_interactive=True),
                     limits=LoopLimits(max_steps=3, context_chars=5000), project_rules=False), transport

    def test_real_loop_commits_archive_and_marks_only_rebuild_request(self):
        agent, transport = self.agent()
        report = agent.run("summarize", history=[{"role": "assistant", "content": "old " * 500} for _ in range(12)])
        events = [e for e in report.events if e.get("type") == "compaction" and e.get("committed")]
        self.assertTrue(events)
        self.assertTrue(Path(events[0]["archive"]["path"]).exists())
        self.assertIn("cache_rebuild", report.usage["requests"][0])
        self.assertEqual(report.stopped, "final")
        self.assertNotIn(events[0]["transaction_id"], json.dumps(transport.calls))

    def test_protected_overflow_stops_before_transport(self):
        agent, transport = self.agent({"contextWindow": 100, "maxOutputTokens": 50})
        report = agent.run("do not lose this task")
        self.assertEqual(report.stopped, "context_limit")
        self.assertEqual(transport.calls, [])
        self.assertFalse(any(e.get("committed") for e in report.events))

    def test_soft_archive_failure_preserves_context_and_continues(self):
        agent, transport = self.agent()
        history = [{"role": "assistant", "content": "old " * 500} for _ in range(12)]
        with patch("forge.context_transaction.archive_source", side_effect=OSError("full")):
            report = agent.run("summarize", history=history)
        self.assertEqual(report.stopped, "final")
        self.assertTrue(all(m in transport.calls[0] for m in history))
        self.assertNotIn("cache_rebuild", report.usage["requests"][0])

    def test_lossless_never_rewrites_even_when_route_capacity_is_small(self):
        agent, transport = self.agent({"contextWindow": 100, "maxOutputTokens": 50})
        agent.context_mode = 'lossless'
        report = agent.run("keep everything", history=[{"role": "assistant", "content": "x" * 30000}])
        self.assertEqual(report.stopped, "context_limit")
        self.assertFalse(any(e.get("type") == "compaction" for e in report.events))
        self.assertFalse((self.root / "context-archives").exists())
        self.assertEqual(transport.calls, [])

    def test_capacity_compacts_then_resumes_append_only_cache_prefix(self):
        class MultiTurn(_RecordedTransport):
            def complete(self, provider, model_id, messages, **options):
                result, usage = super().complete(provider, model_id, messages, **options)
                if len(self.calls) <= 2:
                    result = '<tool_call>{"tool":"list_dir","args":{"path":"."}}</tool_call>'
                return result, usage
        transport = MultiTurn()
        agent, _ = self.agent({"contextWindow": 18000, "maxOutputTokens": 1000}, transport)
        agent.limits.context_chars = 1000000
        agent.compactor.max_chars = 1000000
        agent.compactor.keep_tail = 3
        history = [{"role": "assistant", "content": "old information " * 600} for _ in range(18)]
        report = agent.run("list files then finish", history=history)
        self.assertEqual(report.stopped, "final")
        self.assertEqual(len(transport.calls), 3)
        for old, new in zip(transport.calls, transport.calls[1:]):
            self.assertEqual(old, new[:len(old)])
        self.assertIn("cache_rebuild", report.usage["requests"][0])
        self.assertTrue(all("cache_rebuild" not in req for req in report.usage["requests"][1:]))


class _RecordedTransport:
    def __init__(self):
        self.calls = []

    def complete(self, provider, model_id, messages, **options):
        self.calls.append(copy.deepcopy(messages))
        return "done", model.Usage(prompt_tokens=100, cached_tokens=40, completion_tokens=10,
                                   model=model_id, provider=provider.name)


if __name__ == "__main__":
    unittest.main()
