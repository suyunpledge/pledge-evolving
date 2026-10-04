"""Capability grants never replace Forge Policy; all fixtures are isolated."""
import json
import concurrent.futures
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import plugin_market as pm
import plugin_capabilities as pc
from forge.policy import Mode, Policy
from forge.tools import ToolContext, build_builtin_registry


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws = self.home / "repo"
        self.ws.mkdir()
        (self.ws / "hello.txt").write_text("hello", encoding="utf-8")
        self.market = pm.Marketplace(self.home / "forge")
        self.other = pm.Marketplace(self.home / "forge")
        self.gateway = build_builtin_registry()
        self.policy = Policy(mode=Mode.READ_ONLY, workspace=self.ws)
        self.calls = []

    def install(self, pid="reader", *, target="read_file", capability="repo.read", **fields):
        src = self.home / pid
        src.mkdir(exist_ok=True)
        raw = {"id": pid, "name": pid, "capabilities": [capability],
               "contributions": {"tools": [{"name": pid + "_tool", "target": target,
                   "description": "Repository operation", "parameters": {
                       "type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False}}]}, **fields}
        (src / pm.MANIFEST_NAME).write_text(json.dumps(raw), encoding="utf-8")
        return self.market.install_from_dir(src)

    def ready(self, pid="reader", **fields):
        p = self.install(pid, **fields)
        self.market.ack(pid, expected_fingerprint=p.ack_of())
        self.market.enable(pid)
        return self.market.grant(pid, [fields.get("capability", "repo.read")], self.ws)

    def dispatch(self, name, args, **kwargs):
        self.calls.append((name, args))
        return self.gateway.invoke(name.removeprefix("forge_"), args,
            ToolContext(self.policy, self.ws)).as_dict()

    def runtime(self, session="session-1"):
        rt = pc.CapabilityRuntime(self.market, self.ws, session, self.dispatch)
        rt.reload(reserved_names=self.gateway.names())
        return rt

    def test_install_ack_enable_grant_are_separate(self):
        p = self.install()
        self.assertTrue(p.needs_ack)
        with self.assertRaises(PermissionError):
            self.market.enable(p.id)
        self.market.ack(p.id)
        self.assertFalse(self.market.find(p.id).enabled)
        self.market.enable(p.id)
        self.assertEqual(self.runtime().openai_schemas(), [])
        self.market.grant(p.id, ["repo.read"], self.ws)
        self.assertEqual(self.runtime().names(), ["reader_tool"])

    def test_cross_instance_grant_revoke_and_old_callable(self):
        self.ready()
        rt = self.runtime()
        self.assertTrue(rt.call("reader_tool", {"path": "hello.txt"})["ok"])
        self.other.revoke_grants("reader", workspace=self.ws)
        self.assertFalse(rt.call("reader_tool", {"path": "hello.txt"})["ok"])
        self.assertEqual(len(self.calls), 1)

    def test_workspace_and_session_scope(self):
        p = self.install()
        self.market.ack(p.id)
        self.market.enable(p.id)
        self.market.grant(p.id, ["repo.read"], self.ws, session="one")
        self.assertEqual(self.runtime("two").names(), [])
        self.assertEqual(self.runtime("one").names(), ["reader_tool"])
        elsewhere = pc.CapabilityRuntime(self.market, self.home, "one", self.dispatch)
        elsewhere.reload()
        self.assertEqual(elsewhere.names(), [])

    def test_grant_cannot_override_gateway_policy(self):
        self.ready(target="write_file", capability="repo.write")
        rt = self.runtime()
        result = rt.call("reader_tool", {"path": "hello.txt"})
        self.assertFalse(result["ok"])
        self.assertIn("policy", result["error"])
        self.assertEqual((self.ws / "hello.txt").read_text(), "hello")

    def test_path_escape_absolute_relative_and_link(self):
        self.ready()
        rt = self.runtime()
        outside = self.home / "secret.txt"
        outside.write_text("private")
        for value in (str(outside), "../secret.txt"):
            self.assertFalse(rt.call("reader_tool", {"path": value})["ok"])
        try:
            (self.ws / "link.txt").symlink_to(outside)
        except OSError:
            pass
        else:
            self.assertFalse(rt.call("reader_tool", {"path": "link.txt"})["ok"])
        self.assertEqual(self.calls, [])

    def test_python_import_never_runs_in_agent_runtime(self):
        p = self.install(executes_code=True)
        self.market.ack(p.id)
        self.market.enable(p.id)
        marker = self.home / "ran"
        (self.market.plugins_dir / "reader" / "plugin.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.market.ack("reader")
        self.market.enable("reader")
        self.assertEqual(self.runtime().names(), [])
        self.assertFalse(marker.exists())

    def test_unsupported_process_network_and_unknown_grants_fail_closed(self):
        for cap, target in (("process.exec", "shell_exec"), ("network.github", "fetch_url"),
                            ("invented", "read_file")):
            with self.subTest(cap=cap):
                p = self.install("bad", target=target, capability=cap)
                self.market.ack(p.id)
                self.market.enable(p.id)
                with self.assertRaises((ValueError, PermissionError)):
                    self.market.grant(p.id, [cap], self.ws)
                self.assertEqual(self.runtime().names(), [])

    def test_undeclared_grant_is_denied(self):
        p = self.install()
        self.market.ack(p.id)
        self.market.enable(p.id)
        with self.assertRaises(PermissionError):
            self.market.grant(p.id, ["repo.write"], self.ws)

    def test_file_change_clears_ack_enable_and_grants(self):
        self.ready()
        rt = self.runtime()
        (self.market.plugins_dir / "reader" / "notes.txt").write_text("changed")
        self.assertFalse(rt.call("reader_tool", {"path": "hello.txt"})["ok"])
        p = self.other.find("reader")
        self.assertTrue(p.needs_ack)
        self.assertFalse(p.enabled)
        self.assertFalse(p.grants)

    def test_disable_uninstall_update_and_reinstall_revoke(self):
        for action in ("disable", "uninstall", "update"):
            with self.subTest(action=action):
                self.ready()
                rt = self.runtime()
                if action == "update":
                    self.install(version="2")
                else:
                    getattr(self.other, action)("reader")
                self.assertFalse(rt.call("reader_tool", {"path": "hello.txt"})["ok"])
                self.assertFalse(self.market.state()["grants"].get("reader"))
        self.install()
        self.assertTrue(self.market.find("reader").needs_ack)

    def test_ack_review_pins_grant_review(self):
        p = self.ready()
        (self.market.plugins_dir / "reader" / "notes").write_text("changed")
        with self.assertRaises(PermissionError):
            self.market.grant(p.id, ["repo.read"], self.ws, expected_fingerprint=p.ack_of())

    def test_audit_success_denial_and_transport_error(self):
        self.ready()
        rt = self.runtime()
        rt.call("reader_tool", {"path": "hello.txt"})
        rt.call("reader_tool", {"path": "../secret"})
        with patch.object(rt, "dispatch", side_effect=OSError("offline")):
            failed = rt.call("reader_tool", {"path": "hello.txt"})
        self.assertTrue(failed["meta"]["execution_may_have_completed"])
        rows = self.market.audit_tail()
        self.assertEqual([r["outcome"] for r in rows if r["phase"] == "result"],
                         ["allow", "deny", "error"])
        for row in rows:
            self.assertEqual(row["plugin"], "reader")
            self.assertEqual(row["workspace"], pm.workspace_key(self.ws))
            self.assertEqual(row["session"], "session-1")
            self.assertNotIn("hello", row)  # no file content in audit

    def test_audit_unavailable_prevents_execution(self):
        self.ready()
        rt = self.runtime()
        with patch.object(self.market, "audit", side_effect=OSError("disk full")):
            self.assertFalse(rt.call("reader_tool", {"path": "hello.txt"})["ok"])
        self.assertEqual(self.calls, [])

    def test_saved_registry_handler_cannot_invoke_ghost_plugin(self):
        from forge.tools import ToolContext
        self.ready()
        rt = self.runtime()
        handler = rt.registry.all_specs()[0].handler
        self.other.disable("reader")
        with self.assertRaises(PermissionError):
            handler({"path": "hello.txt"}, ToolContext(self.policy, self.ws))
        self.assertEqual(self.calls, [])

    def test_revoke_and_invocation_have_a_linear_order(self):
        self.ready()
        rt = self.runtime()
        entered, release = threading.Event(), threading.Event()
        def dispatch(name, args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.dispatch(name, args)
        rt.dispatch = dispatch
        with concurrent.futures.ThreadPoolExecutor() as pool:
            call = pool.submit(rt.call, "reader_tool", {"path": "hello.txt"})
            self.assertTrue(entered.wait(2))
            revoke = pool.submit(self.other.revoke_grants, "reader")
            release.set()
            self.assertTrue(call.result(timeout=3)["ok"])
            revoke.result(timeout=3)
            self.assertFalse(rt.call("reader_tool", {"path": "hello.txt"})["ok"])
        self.assertEqual(len(self.calls), 1)

    def test_arguments_and_result_limits(self):
        self.ready()
        rt = self.runtime()
        for args in ([], {"path": 123}, {"path": "hello.txt", "command": "evil"},
                     {"path": "x" * 300_000}):
            self.assertFalse(rt.call("reader_tool", args)["ok"])
        self.assertEqual(self.calls, [])
        with patch.object(rt, "dispatch", return_value={"ok": True, "content": "x" * 400_000}):
            result = rt.call("reader_tool", {"path": "hello.txt"})
        self.assertLessEqual(len(result["content"]), pc.MAX_RESPONSE_CHARS)
        self.assertTrue(result["meta"]["truncated"])
        with patch.object(rt, "dispatch", return_value={"ok": True, "content": object()}):
            self.assertFalse(rt.call("reader_tool", {"path": "hello.txt"})["ok"])

    def test_duplicate_names_reject_whole_second_plugin(self):
        self.ready()
        self.ready("zzz", contributions={"tools": [{"name": "reader_tool", "target": "read_file"}]})
        rt = self.runtime()
        self.assertEqual(rt.names(), ["reader_tool"])
        self.assertEqual(rt.last_report.errors[0][0], "zzz")

    def test_corrupt_grants_fail_closed(self):
        self.ready()
        raw = self.market.state()
        raw["grants"] = {"reader": [{"fingerprint": {}, "workspace": [], "capabilities": "repo.read"},
                                    {"fingerprint": "bad", "workspace": str(self.ws), "capabilities": ["process.exec"]}]}
        self.market.state_path.write_text(json.dumps(raw))
        self.assertEqual(self.runtime().names(), [])

    def test_unresolved_ask_never_executes_plugin_target(self):
        self.ready(target="write_file", capability="repo.write")
        self.policy = Policy(mode=Mode.DEFAULT, workspace=self.ws, non_interactive=False)
        result = self.runtime().call("reader_tool", {"path": "hello.txt"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"]["requires_approval"])
        self.assertEqual((self.ws / "hello.txt").read_text(), "hello")

    def test_builtin_declarative_package_is_really_callable_after_grant(self):
        p = self.market.install_from_catalog("tool-repo-files")
        self.assertTrue(p.needs_ack)
        self.market.ack(p.id)
        self.market.enable(p.id)
        self.market.grant(p.id, ["repo.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), ["repo_list_dir", "repo_read_file"])
        self.assertEqual(rt.call("repo_read_file", {"path": "hello.txt"})["content"], "hello")
        self.assertFalse(rt.call("repo_write_file", {"path": "hello.txt", "content": "bad"})["ok"])

    def test_windows_stream_and_device_paths_are_rejected(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows path semantics")
        self.ready()
        rt = self.runtime()
        for path in ("hello.txt:stream", "CON", "NUL.txt", "sub/COM1.log"):
            self.assertFalse(rt.call("reader_tool", {"path": path})["ok"], path)
        self.assertEqual(self.calls, [])

    def test_forge_control_state_is_protected_even_inside_workspace(self):
        self.ws = self.home
        self.ready(target="write_file", capability="repo.write", contributions={"tools": [{
            "name": "reader_tool", "target": "write_file", "parameters": {"type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}]})
        self.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws)
        before = self.market.state_path.read_bytes()
        result = self.runtime().call("reader_tool", {"path": str(self.market.state_path), "content": "{}"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.market.state_path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_plugin_cannot_rewrite_its_host_source_inside_forge_workspace(self):
        self.ws = Path(pc.__file__).resolve().parents[1]
        self.ready(target="write_file", capability="repo.write", contributions={"tools": [{
            "name": "reader_tool", "target": "write_file", "parameters": {"type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}]})
        rt = self.runtime()
        # A spy transport: never write the real checkout in this regression.
        dispatch = Mock(return_value={"ok": True})
        rt.dispatch = dispatch
        self.assertFalse(rt.call("reader_tool", {"path": pc.__file__, "content": "bad"})["ok"])
        dispatch.assert_not_called()

    def test_result_audit_failure_reports_possible_side_effects(self):
        self.ready()
        rt = self.runtime()
        audit = self.market.audit
        def fail_result(**event):
            if event["phase"] == "result":
                raise OSError("disk full after dispatch")
            return audit(**event)
        with patch.object(self.market, "audit", side_effect=fail_result):
            result = rt.call("reader_tool", {"path": "hello.txt"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"]["execution_may_have_completed"])
        self.assertEqual(len(self.calls), 1)

    def test_hardlink_cannot_read_or_modify_file_outside_workspace(self):
        import os
        outside = self.home / "outside.txt"
        outside.write_text("private")
        alias = self.ws / "alias.txt"
        try:
            os.link(outside, alias)
        except OSError as exc:
            self.skipTest(f"Hardlinks unavailable: {exc}")
        for target, capability in (("read_file", "repo.read"), ("write_file", "repo.write")):
            self.ready(target=target, capability=capability)
            self.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws)
            self.assertFalse(self.runtime().call("reader_tool", {"path": "alias.txt"})["ok"])
        self.assertEqual(outside.read_text(), "private")
        self.assertEqual(self.calls, [])

    def test_real_gateway_policy_and_audit_on_http_path(self):
        from forge.gateway import GatewayConfig, serve
        from forge_client import ForgeGatewayClient
        p = self.ready(target="write_file", capability="repo.write", contributions={"tools": [{
            "name": "reader_tool", "target": "write_file", "parameters": {"type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}]})
        cfg = GatewayConfig(upstream="http://example.invalid", port=0, registry=self.gateway,
                            workspace=str(self.ws), policy=self.policy, log_path=self.home / "gateway.log")
        server = serve(cfg)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = ForgeGatewayClient(f"http://127.0.0.1:{server.server_port}")
            rt = pc.CapabilityRuntime(self.market, self.ws, "real-http", client.call_tool)
            rt.reload(available_targets=[s["function"]["name"] for s in client.list_tools()])
            denied = rt.call("reader_tool", {"path": "hello.txt", "content": "changed"})
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["meta"]["authorization"], "deny")
            for rule in ("reader_tool", "plugin:reader:*", "repo.write"):
                cfg.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws, deny=(rule,))
                denied_alias = rt.call("reader_tool", {"path": "hello.txt", "content": "blocked"})
                self.assertFalse(denied_alias["ok"], rule)
                self.assertEqual((self.ws / "hello.txt").read_text(), "hello")
            cfg.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws)
            self.assertTrue(rt.call("reader_tool", {"path": "hello.txt", "content": "changed"})["ok"])
            self.assertEqual((self.ws / "hello.txt").read_text(), "changed")
            results = [r for r in self.market.audit_tail() if r["phase"] == "result"]
            self.assertEqual([r["outcome"] for r in results], ["deny"] * 4 + ["allow"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_older_gateway_cannot_silently_ignore_plugin_context(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from forge_client import ForgeGatewayClient, GatewayError
        posts = []
        class OldGateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = json.dumps({"data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                posts.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), OldGateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ForgeGatewayClient(f"http://127.0.0.1:{server.server_port}", timeout=1)
            with self.assertRaises(GatewayError):
                client.call_tool("write_file", {"path": "ignored"}, plugin_context={"id": "plugin"})
            self.assertEqual(posts, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_collisions_and_malformed_schema_do_not_register(self):
        self.ready()
        path = self.market.plugins_dir / "reader" / pm.MANIFEST_NAME
        raw = json.loads(path.read_text())
        for name, params in (("read_file", {"type": "object"}),
                             ("forge_read_file", {"type": "object"}),
                             ("reader_tool", {"type": "array"})):
            raw["contributions"]["tools"][0].update(name=name, parameters=params)
            path.write_text(json.dumps(raw))
            self.market.ack("reader")
            self.market.enable("reader")
            self.market.grant("reader", ["repo.read"], self.ws)
            rt = self.runtime()
            self.assertEqual(rt.names(), [])
            self.assertTrue(rt.last_report.errors)

    def test_contributions_are_fingerprinted_but_not_executed(self):
        p = self.install(contributions={"skills": [{"name": "workflow", "path": "SKILL.md"}],
                                       "policies": [{"name": "allow-everything"}]})
        self.assertEqual(p.contributions["policies"][0]["name"], "allow-everything")
        self.assertTrue(p.needs_ack)
        self.market.ack(p.id)
        self.market.enable(p.id)
        self.assertEqual(self.runtime().names(), [])


if __name__ == "__main__":
    unittest.main()
