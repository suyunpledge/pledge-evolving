"""forge_client 离线测试：起一个本机 HTTP server 模拟 forge gateway，
跑非流式 + 流式两个路径，验证返回结构正确。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(sys.path[0] and __import__('pathlib').Path(__file__).resolve().parent))
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forge_client import (  # noqa: E402
    ChatMessage,
    ForgeGatewayClient,
    GatewayError,
)


# ── mock server ──
class MockHandler(BaseHTTPRequestHandler):
    """假的 gateway：/v1/models → 200，/v1/chat/completions 按 stream 字段返回。"""

    def log_message(self, *a, **kw):
        pass  # 静音

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._send_json(200, {"data": [{"id": "fake-model"}]})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._send_json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        if body.get("stream"):
            return self._do_stream(body)
        return self._do_block(body)

    def _do_block(self, body):
        return self._send_json(200, {
            "id": "cmpl-1",
            "model": body.get("model", "fake"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "这是非流式回复"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        })

    def _do_stream(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = ["流", "式", " ", "OK"]
        for c in chunks:
            payload = json.dumps({
                "id": "cmpl-1",
                "model": body.get("model", "fake"),
                "choices": [{"index": 0, "delta": {"content": c}}],
            })
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def start_mock_server(port: int = 0) -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", port), MockHandler)
    real_port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv, real_port


# ── tests ──
def test_health():
    srv, port = start_mock_server()
    c = ForgeGatewayClient(f"http://127.0.0.1:{port}")
    ok, msg = c.health()
    srv.shutdown()
    assert ok, f"health 失败：{msg}"
    print(f"  OK  health → {msg}")


def test_block():
    srv, port = start_mock_server()
    c = ForgeGatewayClient(f"http://127.0.0.1:{port}")
    r = c.chat([ChatMessage("user", "你好")], model="x")
    srv.shutdown()
    assert r.text == "这是非流式回复", f"text 错：{r.text!r}"
    assert r.model == "x", f"model 错：{r.model}"
    assert r.usage and r.usage["total_tokens"] == 18
    print(f"  OK  非流式 → {r.text!r} (usage={r.usage['total_tokens']})")


def test_stream():
    srv, port = start_mock_server()
    c = ForgeGatewayClient(f"http://127.0.0.1:{port}")
    seen: list[str] = []
    r = c.stream_chat(
        [ChatMessage("user", "你好")], model="y",
        on_chunk=lambda p: seen.append(p),
    )
    srv.shutdown()
    assert r.text == "流式 OK", f"stream 拼接错：{r.text!r}"
    assert "".join(seen) == "流式 OK", f"callback 顺序错：{seen!r}"
    print(f"  OK  流式 → chunks={seen!r}")


def test_unreachable():
    c = ForgeGatewayClient("http://127.0.0.1:1")  # 没人监听
    ok, msg = c.health()
    assert not ok, "不应通过"
    print(f"  OK  不可达 → {msg[:60]}")


def test_chat_error():
    c = ForgeGatewayClient("http://127.0.0.1:1")
    try:
        c.chat([ChatMessage("user", "x")])
        raise AssertionError("应抛 GatewayError")
    except GatewayError as e:
        print(f"  OK  chat 错误传播 → {str(e)[:60]}")


if __name__ == "__main__":
    print("=== ForgeGatewayClient 测试 ===")
    test_health()
    test_block()
    test_stream()
    test_unreachable()
    test_chat_error()
    print("\n✅ 全部通过")