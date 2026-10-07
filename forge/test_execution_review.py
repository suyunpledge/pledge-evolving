"""Execution regressions; all upstreams in this module are local stubs."""
import copy
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from . import model
from .cache_state import CacheWarmth
from .vendors import PROFILES


class ExecutionReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = CacheWarmth()
        p = patch.object(model, "warmth_store", return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_other_instance_observes_new_write_and_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warmth.json"
            a, b = CacheWarmth(path), CacheWarmth(path)
            a.remember("deepseek", "prefix")
            self.assertTrue(b.is_warm(PROFILES["deepseek"], "prefix"))
            b.forget("deepseek", "prefix")
            a.save()
            self.assertFalse(a.is_warm(PROFILES["deepseek"], "prefix"))

    def test_independent_instances_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warmth.json"
            a, b = CacheWarmth(path), CacheWarmth(path)
            a.remember("deepseek", "a")
            b.remember("deepseek", "b")
            a.save(); b.save()
            self.assertEqual(CacheWarmth(path).to_raw()["entries"], 2)

    def test_actual_vendor_and_usage_are_required_for_confirmed_warmth(self):
        provider = model.Provider("multi", "https://gateway.example/v1", default_model="gpt-6.1-sol")
        plan = {"vendor": "deepseek", "fingerprint": "prefix", "cache_engages": True}
        model._remember_warmth(provider, plan, model.Usage(cached_tokens=100))
        self.assertTrue(self.store.is_warm(PROFILES["deepseek"], "prefix"))
        self.assertFalse(self.store.is_warm(PROFILES["openai"], "prefix"))

    def test_missing_cache_usage_is_not_confirmed_warmth(self):
        p = model.Provider("ds", "https://api.deepseek.com", default_model="deepseek-flash")
        model._remember_warmth(p, {"vendor": "deepseek", "fingerprint": "x", "cache_engages": True})
        self.assertFalse(self.store.is_warm(PROFILES["deepseek"], "x"))

    def test_qwen_reasoning_budget_reaches_wire(self):
        p = model.Provider("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        payload = {"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
        shaped, plan = model._plan_and_shape(payload, payload["messages"], p, payload["model"], think_budget=1024)
        self.assertEqual(shaped["thinking_budget"], 1024)
        self.assertEqual(plan["execution"]["reasoning"]["status"], "applied")
        self.assertNotIn("thinking_budget", payload)

    def test_explicit_effort_is_preserved_without_conflicting_budget(self):
        p = model.Provider("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        payload = {"model": "qwen3.8-flash", "messages": [], "reasoning_effort": "low"}
        shaped, plan = model._plan_and_shape(payload, [], p, payload["model"], think_budget=1024)
        self.assertNotIn("thinking_budget", shaped)
        self.assertEqual(shaped["reasoning_effort"], "low")
        self.assertEqual(plan["execution"]["reasoning"]["status"], "explicit_override")

    def test_deepseek_budget_bounds_total_output_and_reports_boundary(self):
        p = model.Provider("ds", "https://api.deepseek.com")
        payload = {"model": "deepseek-flash", "messages": [], "max_tokens": 8000}
        shaped, plan = model._plan_and_shape(payload, [], p, payload["model"], think_budget=1024)
        self.assertEqual(shaped["max_tokens"], 1024)
        self.assertEqual(plan["execution"]["reasoning"]["scope"], "total_output")

    def test_flex_goes_to_wire_and_batch_cannot_fake_sync_discount(self):
        p = model.Provider("oai", "https://api.openai.com/v1")
        shaped, plan = model._plan_and_shape({"messages": []}, [], p, "o3", flex=True)
        self.assertEqual(shaped["service_tier"], "flex")
        self.assertEqual(plan["execution"]["flex"]["status"], "requested")
        with self.assertRaises(ValueError):
            model._plan_and_shape({"messages": []}, [], p, "o3", batch=True)

    def test_unsupported_flex_fails_before_network(self):
        p = model.Provider("ds", "https://api.deepseek.com")
        with self.assertRaises(ValueError):
            model._plan_and_shape({"messages": []}, [], p, "deepseek-flash", flex=True)

    def test_completion_has_per_request_receipt(self):
        completion = model.Completion("ok", model.Usage(10, 3, "m", "p"), plan={"est_cost": 100})
        self.assertEqual(completion.requests[0]["usage"]["prompt_tokens"], 10)
        self.assertEqual(completion.requests[0]["plan"]["est_cost"], 100)
        self.assertEqual(completion.requests[0]["estimate_scope"], "reuse_window")

    def test_gateway_calls_planner_after_alias_mapping(self):
        from .gateway import GatewayConfig
        cfg = GatewayConfig(upstream="https://dashscope.aliyuncs.com/compatible-mode/v1",
                            upstream_wire="openai", model_map={"flash": "qwen3.8-flash"},
                            provider_options={"cacheControl": "explicit", "expectedCalls": 1})
        payload = {"model": "flash", "messages": [{"role": "system", "content": "S" * 6000}]}
        shaped, plan, provider = cfg.prepare_request(payload)
        self.assertEqual(shaped["model"], "qwen3.8-flash")
        self.assertTrue(plan["cache_control"])
        self.assertIn("cache_control", shaped["messages"][0]["content"][-1])

    def test_premium_receipts_keep_both_models_and_plans(self):
        from .routing import RoutingConfig, SmartRouter
        class Transport:
            def complete(self, provider, chosen, messages, **options):
                return "ok", model.Usage(10, 3), {"plan": {"vendor": chosen, "est_cost": 99}}
        provider = model.Provider("test", "https://example.invalid")
        router = SmartRouter([provider], transport=Transport(), routing=RoutingConfig(
            strategy="premium", tiers=[("test", "draft")], premium=[("test", "final")]))
        result = router.complete([{"role": "user", "content": "task"}])
        self.assertEqual([r["model"] for r in result.requests], ["draft", "final"])
        self.assertEqual([r["plan"]["vendor"] for r in result.requests], ["draft", "final"])
        self.assertEqual(result.usage.total, 26)
        self.assertEqual(result.plan["scope"], "multi_request")
        self.assertNotIn("total_est_cost", result.plan)

    def test_moa_keeps_candidate_usage(self):
        class Transport:
            def complete(self, provider, chosen, messages, **options):
                return "ok", model.Usage(10, 3, chosen, provider.name), {}
        p = model.Provider("p", "https://example.invalid", default_model="judge")
        router = model.ModelRouter([p], transport=Transport())
        result = router.complete_moa([], models=[("p", "a"), ("p", "b")], judge=("p", "judge"))
        self.assertEqual(result.usage.total, 39)
        self.assertEqual(len(result.requests), 3)

    def test_loop_ledger_keeps_models_separate_and_does_not_double_count(self):
        from .loop import Agent, RunReport
        from .pricing import CostLedger
        from types import SimpleNamespace
        loop = Agent.__new__(Agent)
        loop.metabolise = lambda report: None
        loop._run_thinking_active = False
        loop.extension_calls = {}
        loop.cost_ledger = CostLedger()
        loop._run_scope = True
        loop._run_thinking_tokens = 0
        loop._child_tokens = 0
        loop._run_model = "final"
        loop._run_probe = SimpleNamespace(model="final")
        loop.name = "probe"
        loop._emit = lambda **kwargs: None
        loop._pricing_scale = lambda *args, **kwargs: 1
        report = RunReport(text="ok", steps=[], stopped="final", events=[], usage={
            "prompt_tokens": 20, "completion_tokens": 6, "requests": [
                {"model": "draft", "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
                {"model": "final", "usage": {"prompt_tokens": 10, "completion_tokens": 3}}]})
        loop._finish(report)
        self.assertEqual([(e.model, e.tokens) for e in loop.cost_ledger.entries], [("draft", 13), ("final", 13)])

    def test_separate_processes_merge_without_lost_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            code = ("from forge.cache_state import CacheWarmth; import sys; "
                    "s=CacheWarmth(sys.argv[1]); "
                    "[s.remember('deepseek',sys.argv[2]+str(i)) for i in range(30)]")
            processes = [subprocess.Popen([sys.executable, "-c", code, str(path), str(i)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for i in range(3)]
            for p in processes:
                _, err = p.communicate(timeout=20)
                self.assertEqual(p.returncode, 0, err.decode())
            self.assertEqual(CacheWarmth(path).to_raw()["entries"], 90)

    def test_defer_wait_is_cancellable_and_bounded(self):
        from .request_execution import prepare_execution, wait_for_schedule
        from datetime import datetime, timezone
        peak = datetime(2026, 10, 6, 2, tzinfo=timezone.utc).timestamp()
        with self.assertRaises(ValueError):
            prepare_execution({}, PROFILES["deepseek"], "deepseek-flash", interactive=False,
                              defer=True, max_defer_seconds=60, when=peak)
        _, execution = prepare_execution({}, PROFILES["deepseek"], "deepseek-flash", interactive=False,
                                         defer=True, max_defer_seconds=8000, when=peak)
        self.assertEqual(execution["defer"]["delay_seconds"], 7200)
        event = threading.Event(); event.set()
        with self.assertRaises(ValueError):
            wait_for_schedule({"execution": execution}, event)
        with self.assertRaises(ValueError):
            prepare_execution({}, PROFILES["deepseek"], "deepseek-flash", defer=True)

    def test_usage_observer_split_oversized_and_anthropic_events(self):
        from .request_execution import UsageObserver
        observer = UsageObserver()
        data = b'data: {"usage":{"prompt_tokens":10,"completion_tokens":3}}\n\n'
        for byte in data:
            observer.feed(bytes([byte]))
        self.assertEqual(observer.usage["prompt_tokens"], 10)
        observer.feed(b"data: " + b"x" * (observer.MAX_EVENT_BYTES + 1))
        observer.feed(b'garbage\ndata: {"usage":{"prompt_tokens":20}}\n')
        self.assertEqual(observer.usage["prompt_tokens"], 20)
        observer.observe({"type": "message_start", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 100}}})
        observer.observe({"type": "message_delta", "usage": {"output_tokens": 4}})
        self.assertEqual(observer.usage["cache_read_input_tokens"], 100)
        self.assertEqual(observer.usage["output_tokens"], 4)

    def test_gateway_stream_and_translated_paths_execute_adaptation(self):
        from .gateway import GatewayConfig, serve
        recorded = []
        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                recorded.append(payload)
                usage = {"prompt_tokens": 2200, "completion_tokens": 1,
                         "prompt_tokens_details": {"cached_tokens": 2048}}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close"); self.end_headers()
                for event in [{"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
                              {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}]:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n"); self.close_connection = True
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        cfg = GatewayConfig(upstream=f"http://127.0.0.1:{upstream.server_port}/v1", port=0,
                            upstream_wire="openai", wire="openai", model_map={"flash": "qwen3.8-flash"},
                            provider_options={"cacheControl": "explicit"})
        gateway = serve(cfg)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            for path, payload in [("chat/completions", {"messages": [{"role": "system", "content": "S" * 6000},
                                                             {"role": "user", "content": "hi"}]}),
                                  ("messages", {"system": "S" * 6000, "messages": [{"role": "user", "content": "hi"}]})]:
                request = urllib.request.Request(f"http://127.0.0.1:{gateway.server_port}/v1/{path}",
                    data=json.dumps({**payload, "model": "flash", "stream": True, "max_tokens": 64}).encode(),
                    headers={"content-type": "application/json"})
                with urllib.request.urlopen(request, timeout=4) as response:
                    self.assertIn(b"ok", response.read())
            self.assertEqual(len(recorded), 2)
            for payload in recorded:
                self.assertEqual(payload["model"], "qwen3.8-flash")
                self.assertTrue(payload["stream_options"]["include_usage"])
                self.assertIn("cache_control", payload["messages"][0]["content"][-1])
            audits = [line for line in cfg.log.lines if "ADAPT " in line]
            self.assertEqual(len(audits), 2)
            self.assertTrue(all('"cached_tokens": 2048' in line for line in audits))
        finally:
            gateway.shutdown(); gateway.server_close(); upstream.shutdown(); upstream.server_close()

    def test_batch_submit_poll_result_cancel_is_separate_and_durable(self):
        from .batch_execution import BatchExecutor
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            if request.full_url.endswith("/files"):
                return io.BytesIO(b'{"id":"file-1"}')
            if request.full_url.endswith("/batches"):
                return io.BytesIO(b'{"id":"batch-1","status":"validating"}')
            if request.full_url.endswith("/cancel"):
                return io.BytesIO(b'{"id":"batch-1","status":"cancelling"}')
            if request.full_url.endswith("/content"):
                return io.BytesIO(b'{"custom_id":"r1","response":{"status_code":200,"body":{"usage":{"prompt_tokens":10}}}}\n')
            return io.BytesIO(b'{"id":"batch-1","status":"completed","output_file_id":"file-out"}')
        with tempfile.TemporaryDirectory() as tmp:
            p = model.Provider("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            executor = BatchExecutor(p, tmp, opener=opener)
            job = executor.submit([{"custom_id": "r1", "body": {"model": "qwen3.8-flash", "messages": []}}])
            self.assertEqual(job["id"], "batch-1")
            self.assertTrue((Path(tmp) / "batch-jobs/batch-1.json").exists())
            self.assertIn(b'"purpose"', requests[0].data)
            self.assertEqual(json.loads(requests[1].data)["completion_window"], "24h")
            new = BatchExecutor(p, tmp, opener=opener)
            self.assertEqual(len(new.results("batch-1")["results"]), 1)
            self.assertEqual(new.cancel("batch-1")["status"], "cancelling")
            with self.assertRaises(ValueError):
                new.status("../../bad")

    def test_batch_network_failure_does_not_resubmit(self):
        from .batch_execution import BatchExecutor
        calls = []
        def opener(request, **kwargs):
            calls.append(request.full_url)
            if request.full_url.endswith("/files"):
                return io.BytesIO(b'{"id":"file-1"}')
            raise TimeoutError("ambiguous acceptance")
        with tempfile.TemporaryDirectory() as tmp:
            p = model.Provider("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            executor = BatchExecutor(p, tmp, opener=opener)
            with self.assertRaises(TimeoutError):
                executor.submit([{"custom_id": "a", "body": {"model": "qwen3.8-flash", "messages": []}}])
            self.assertEqual(len(calls), 2)
            self.assertTrue((Path(tmp) / "batch-jobs/submission-file-1.json").exists())

    def test_live_probe_really_uses_gateway_stream_without_vendor_network(self):
        from .verify_execution import probe
        recorded = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                recorded.append(payload)
                raw = {"model": "deepseek-flash", "choices": [{"message": {"content": "ok"}}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 1, "prompt_cache_hit_tokens": 10}}
                if payload.get("stream"):
                    raw["choices"] = [{"delta": {"content": "ok"}, "finish_reason": "stop"}]
                    body = ("data: " + json.dumps(raw) + "\n\ndata: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    body = json.dumps(raw).encode(); content_type = "application/json"
                self.send_response(200); self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch("forge.verify_execution.time.sleep"):
                provider = model.Provider("stub", f"http://127.0.0.1:{upstream.server_port}/v1",
                                          default_model="deepseek-flash")
                with patch("sys.stdout", io.StringIO()):
                    probe(provider, Path(tmp), limit=100000, ttl_wait=0, shared_path=Path(tmp) / "cache.json")
                report = json.loads((Path(tmp) / "deepseek-flash.json").read_text(encoding="utf8"))
                self.assertEqual([bool(p.get("stream")) for p in recorded], [False, False, True])
                row = report["requests"][-1]
                self.assertEqual(row["status"], "ok")
                self.assertTrue(row["gateway_planner_observed"])
                self.assertGreater(row["sse_lines"], 0)
        finally:
            upstream.shutdown(); upstream.server_close()

    def test_gateway_forwards_first_event_before_upstream_finishes(self):
        from .gateway import GatewayConfig, serve
        release = threading.Event()
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close"); self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'); self.wfile.flush()
                release.wait(4)
                self.wfile.write(b"data: [DONE]\n\n"); self.close_connection = True
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        gateway = serve(GatewayConfig(upstream=f"http://127.0.0.1:{upstream.server_port}/v1",
                                     upstream_wire="openai", wire="openai", port=0))
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{gateway.server_port}/v1/chat/completions",
                  data=b'{"model":"stub","messages":[],"stream":true}', headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                self.assertIn(b"first", response.readline())
                self.assertFalse(release.is_set())
                release.set(); response.read()
        finally:
            release.set(); gateway.shutdown(); gateway.server_close(); upstream.shutdown(); upstream.server_close()


if __name__ == "__main__":
    unittest.main()
