"""分级检索（长期记忆）接入：客户端离线测试 + Work Mode 对话路径测试。

全部离线：客户端用注入的假 opener，对话路径用 stub 模型客户端，
不会碰本机 8000 端口上的真实服务。
"""
import io
import json
import threading
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import memory_retrieval as mr
from forge.secrets import SecretScope


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *exc): self.close(); return False


RETRIEVE_HIT = {
    "query": "微服务",
    "detected_domain": {"domain_id": "d1", "name": "架构讨论"},
    "key_sentences": [{"text": "订单服务拆成独立部署", "score": 0.81},
                      {"text": "网关统一做鉴权", "score": 0.92}],
    "full_contexts": [{"chunk_text": "上周会议：先拆订单再拆库存", "score": 0.7}],
    "summary": "",
}


class FakeOpener:
    """按路径返回预设结果；记录每次请求（url、method、headers、body）。"""
    def __init__(self, routes=None, error=None):
        self.routes = routes if routes is not None else {
            "/health": {"status": "ok", "ollama_url": "x", "storage_root": "y"},
            "/retrieve": RETRIEVE_HIT,
            "/ingest": {"entry_id": "e1", "domain_id": "d1", "domain_name": "架构讨论",
                        "sentence_ids": ["s1"]},
        }
        self.error = error
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        path = req.full_url.split("127.0.0.1", 1)[1].split("/", 1)[1]
        with self.lock:
            self.calls.append({"url": req.full_url, "path": "/" + path, "method": req.get_method(),
                               "headers": dict(req.header_items()), "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return FakeResponse(json.dumps(self.routes["/" + path], ensure_ascii=False).encode("utf-8"))

    def paths(self):
        with self.lock:
            return [c["path"] for c in self.calls]


def http_error(code):
    return urllib.error.HTTPError("http://127.0.0.1:8000/x", code, "denied", {}, None)


class ClientTests(unittest.TestCase):
    def test_success_round_trip_uses_bearer_and_short_timeout(self):
        opener = FakeOpener()
        client = mr.RetrievalClient(8000, token="hr-test-token", opener=opener)
        self.assertTrue(client.health())
        result = client.retrieve("微服务", top_k_sentences=3)
        self.assertEqual(result["detected_domain"]["name"], "架构讨论")
        self.assertEqual(client.ingest("用户：a\n助手：b", {"source": "t"})["entry_id"], "e1")
        self.assertEqual(opener.paths(), ["/health", "/retrieve", "/ingest"])
        self.assertEqual(opener.calls[1]["body"], {"query": "微服务", "top_k_sentences": 3})
        self.assertEqual(opener.calls[2]["body"]["metadata"], {"source": "t"})
        for call in opener.calls:
            self.assertTrue(call["url"].startswith("http://127.0.0.1:8000/"))
            self.assertEqual(call["headers"].get("Authorization"), "Bearer hr-test-token")
            self.assertLessEqual(call["timeout"], 2.0)
        self.assertEqual(client.last_error, "")

    def test_no_token_sends_no_authorization_header(self):
        opener = FakeOpener()
        mr.RetrievalClient(8000, token=None, opener=opener).health()
        self.assertNotIn("Authorization", opener.calls[0]["headers"])

    def test_timeout_returns_empty(self):
        for error in (TimeoutError("slow"), urllib.error.URLError(TimeoutError("slow"))):
            client = mr.RetrievalClient(opener=FakeOpener(error=error))
            self.assertFalse(client.health())
            self.assertEqual(client.retrieve("q"), {})
            self.assertEqual(client.ingest("t"), {})
            self.assertEqual(client.last_error, "timeout")

    def test_401_returns_empty_and_bridge_gives_auth_notice(self):
        client = mr.RetrievalClient(token="wrong", opener=FakeOpener(error=http_error(401)))
        self.assertEqual(client.retrieve("q"), {})
        self.assertEqual(client.last_error, "http-401")
        # /health 不鉴权、/retrieve 鉴权：健康但口令错
        opener = FakeOpener()
        def auth_fail(req, timeout=None):
            if req.full_url.endswith("/retrieve"):
                opener.calls.append({"path": "/retrieve"}); raise http_error(401)
            return opener(req, timeout)
        bridge = mr.MemoryBridge(mr.RetrievalClient(token="wrong", opener=auth_fail))
        self.assertEqual(bridge.context_for("q"), "")
        self.assertEqual(bridge.consume_notice(), mr.AUTH_NOTICE)

    def test_service_not_started_returns_empty(self):
        client = mr.RetrievalClient(opener=FakeOpener(
            error=urllib.error.URLError(ConnectionRefusedError(10061, "refused"))))
        self.assertFalse(client.health())
        self.assertEqual(client.retrieve("q"), {})
        self.assertEqual(client.last_error, "unreachable")

    def test_garbage_response_is_swallowed(self):
        client = mr.RetrievalClient(opener=lambda req, timeout=None: FakeResponse(b"<html>"))
        self.assertEqual(client.retrieve("q"), {})
        client = mr.RetrievalClient(opener=lambda req, timeout=None: FakeResponse(b"[1,2]"))
        self.assertFalse(client.health())

    def test_secrets_are_redacted_before_leaving(self):
        scope = SecretScope(); self.addCleanup(scope.close)
        secret = "sk-synthetic-memory-leak-0123456789abcdef"
        scope.reference(secret)
        opener = FakeOpener()
        client = mr.RetrievalClient(opener=opener)
        client.retrieve(f"我的 key 是 {secret} 帮我查")
        client.ingest(f"用户：{secret}\n助手：好的")
        sent = json.dumps([c["body"] for c in opener.calls], ensure_ascii=False)
        self.assertNotIn(secret, sent)

    def test_format_block_is_labelled_bounded_and_ordered(self):
        block = mr.format_memory_block(RETRIEVE_HIT)
        lines = block.splitlines()
        self.assertEqual(lines[0], mr.MEMORY_HEADER)
        self.assertEqual(lines[1], "当前话题：架构讨论")
        self.assertEqual(lines[2], "- 网关统一做鉴权")       # 高分关键句在前
        self.assertEqual(lines[3], "- 订单服务拆成独立部署")
        self.assertTrue(lines[4].startswith("· 原文片段："))
        big = dict(RETRIEVE_HIT, full_contexts=[{"chunk_text": "长" * 5000, "score": 1.0}])
        self.assertLessEqual(len(mr.format_memory_block(big)), mr.CONTEXT_CHAR_BUDGET)
        self.assertEqual(mr.format_memory_block({"detected_domain": {"name": "x"}}), "")
        self.assertEqual(mr.format_memory_block({}), "")
        self.assertEqual(mr.format_memory_block(None), "")

    def test_bridge_notice_is_once_per_outage(self):
        opener = FakeOpener(error=urllib.error.URLError(ConnectionRefusedError()))
        bridge = mr.MemoryBridge(mr.RetrievalClient(opener=opener))
        self.assertEqual(bridge.context_for("q"), "")
        self.assertEqual(bridge.consume_notice(), mr.DOWN_NOTICE)
        self.assertEqual(bridge.context_for("q"), "")
        self.assertEqual(bridge.consume_notice(), "")       # 同一次故障不重复提示
        opener.error = None
        self.assertIn("网关统一做鉴权", bridge.context_for("q"))
        opener.error = TimeoutError()
        bridge.context_for("q")
        self.assertEqual(bridge.consume_notice(), mr.DOWN_NOTICE)  # 恢复后再挂，再提示一次

    def test_plugin_port_follows_market_state(self):
        import tempfile
        from pathlib import Path
        from plugin_market import Marketplace
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(mr.plugin_port(home))
            self.assertIsNone(mr.memory_bridge_for(home))
            market = Marketplace(home=home)
            market.install_from_catalog(mr.PLUGIN_ID)
            self.assertIsNone(mr.plugin_port(home))          # 装了没启用 → 仍关
            market.enable(mr.PLUGIN_ID)
            self.assertEqual(mr.plugin_port(home), 8000)
            self.assertIsInstance(mr.memory_bridge_for(home, token=""), mr.MemoryBridge)
            market.disable(mr.PLUGIN_ID)
            self.assertIsNone(mr.plugin_port(home))

    def test_token_comes_from_secret_store_connector_namespace(self):
        import secret_store
        self.assertTrue(mr.SECRET_NAME.startswith("connector."))
        with patch.object(secret_store, "get_provider", return_value="from-store") as getter:
            self.assertEqual(mr._stored_token(), "from-store")
            getter.assert_called_once_with(mr.SECRET_NAME)
        self.assertNotIn("FORGE_CONNECTOR.HIERARCHICAL_RETRIEVAL_KEY",
                         secret_store.env_for({mr.SECRET_NAME: "x"}))
        self.assertEqual(secret_store.env_for({mr.SECRET_NAME: "x"}), {})


class WorkModeDialogTests(unittest.TestCase):
    """真实 _do_send 路径（隔离 home + stub 模型客户端）。"""

    def _writer(self, seen):
        class Writer:
            base_url = "http://127.0.0.1:12345"
            def health(self, **kwargs): return True, "ok"
            def list_tools(self): return []
            def stream_chat(self, messages, **kwargs):
                seen.append([(m.role, m.content) for m in messages])
                kwargs["on_chunk"]("拆分顺序建议：先订单")
                return SimpleNamespace(text="拆分顺序建议：先订单", tool_calls=[])
        return Writer()

    def _send(self, app, root, text):
        from test_layout_dpi import pump
        with patch.object(app, "_plan_sidecars", return_value=None), \
             patch.object(app, "_reload_plugin_tools", return_value=([], None)):
            app.send_var.set(text); app._do_send()
            deadline = time.monotonic() + 5
            while app._sending and time.monotonic() < deadline:
                pump(root, .03)
        self.assertFalse(app._sending)

    def _enable_plugin(self, app):
        from plugin_market import Marketplace
        Marketplace(home=app.home).install_from_catalog(mr.PLUGIN_ID, enable=True)

    def test_enabled_and_healthy_injects_memory_and_ingests_turn(self):
        from test_layout_dpi import isolated_app, pump
        opener = FakeOpener()
        real_factory = mr.memory_bridge_for
        with isolated_app() as (root, app, errors), \
             patch.object(mr, "memory_bridge_for",
                          side_effect=lambda home: real_factory(home, opener=opener, token="")):
            self._enable_plugin(app)
            seen = []; app.client = self._writer(seen)
            self._send(app, root, "微服务怎么拆")
            deadline = time.monotonic() + 3
            while "/ingest" not in opener.paths() and time.monotonic() < deadline:
                pump(root, .03)
            self.assertEqual(opener.paths(), ["/health", "/retrieve", "/ingest"])
            self.assertEqual(opener.calls[1]["body"]["query"], "微服务怎么拆")
            sent = seen[-1]
            self.assertEqual(sent[-1], ("user", "微服务怎么拆"))      # 用户消息本体不改
            memory_msgs = [c for r, c in sent if c.startswith(mr.MEMORY_HEADER)]
            self.assertEqual(len(memory_msgs), 1)
            self.assertIn("网关统一做鉴权", memory_msgs[0])
            self.assertEqual(sent[-2][1], memory_msgs[0])            # 紧贴本轮提问之前
            ingest = opener.calls[2]["body"]
            self.assertEqual(ingest["text"], "用户：微服务怎么拆\n助手：拆分顺序建议：先订单")
            self.assertEqual(ingest["metadata"]["source"], "forge-work-mode")
            # 记忆块不进对话历史（下一轮不会重复叠加）
            self.assertFalse(any(m.content.startswith(mr.MEMORY_HEADER) for m in app._chat_history))
            self.assertFalse(errors)

    def test_not_enabled_makes_zero_network_calls(self):
        from test_layout_dpi import isolated_app
        calls = []
        def forbidden(req, *a, **k):
            url = getattr(req, "full_url", str(req))
            calls.append(url); raise AssertionError("unexpected network: " + url)
        with isolated_app() as (root, app, errors), \
             patch("urllib.request.urlopen", side_effect=forbidden), \
             patch.object(mr, "RetrievalClient", side_effect=AssertionError("client built")) as ctor:
            seen = []; app.client = self._writer(seen)
            self._send(app, root, "你好")
            ctor.assert_not_called()
            self.assertEqual(calls, [])
            self.assertFalse(any(c.startswith(mr.MEMORY_HEADER) for _r, c in seen[-1]))
            self.assertEqual(app._chat_history[-1].content, "拆分顺序建议：先订单")
            self.assertFalse(errors)

    def test_service_down_chat_continues_with_one_plain_notice(self):
        from test_layout_dpi import isolated_app
        opener = FakeOpener(error=urllib.error.URLError(ConnectionRefusedError(10061, "refused")))
        real_factory = mr.memory_bridge_for
        statuses = []
        with isolated_app() as (root, app, errors), \
             patch.object(mr, "memory_bridge_for",
                          side_effect=lambda home: real_factory(home, opener=opener, token="")):
            self._enable_plugin(app)
            original = app._set_status
            app._set_status = lambda msg, level="info": (statuses.append(msg), original(msg, level))
            seen = []; app.client = self._writer(seen)
            self._send(app, root, "第一问")
            self._send(app, root, "第二问")
            self.assertEqual(len(seen), 2)                           # 两轮都照常生成
            self.assertEqual(app._chat_history[-1].content, "拆分顺序建议：先订单")
            self.assertEqual(statuses.count(mr.DOWN_NOTICE), 1)      # 一次性提示
            self.assertNotIn("/ingest", opener.paths())              # 不健康不入库
            self.assertFalse(errors)

    def test_coding_mode_never_touches_memory(self):
        from test_layout_dpi import isolated_app
        with isolated_app() as (root, app, errors), \
             patch.object(app, "_work_memory_bridge",
                          side_effect=AssertionError("memory in coding")) as bridge:
            self._enable_plugin(app)
            app._workspace_mode = app._workspace_kind.CODING
            seen = []; app.client = self._writer(seen)
            self._send(app, root, "改一下 foo.py")
            bridge.assert_not_called()
            self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
