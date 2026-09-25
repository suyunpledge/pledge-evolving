"""端到端集成测试：模拟「用户矫治配置 → 保存 → 启动客户端 → 与 gateway 对话」全链路。

无 tkinter、无真实 gateway：mock 一个 HTTPServer 模拟 forge gateway，
直接调用 config_model + forge_client 验证 GUI 内部的契约。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_model import (  # noqa: E402
    normalize,
    save_user_layer,
    load_user_layer,
    merge_with_user_layer,
)
from forge_client import (  # noqa: E402
    ChatMessage,
    ForgeGatewayClient,
)


# ── mock gateway（模拟 forge gateway 的 OpenAI 兼容层） ──


class MockGatewayHandler(BaseHTTPRequestHandler):
    """简易 mock：返回 canned 回复（含流式 SSE）。"""

    def log_message(self, *a, **kw):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._send_json(200, {"data": [{"id": "deepseek-flash"}]})
        return self._send_json(404, {"error": "no"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._send_json(404, {"error": "no"})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        if body.get("stream"):
            return self._stream(body)
        return self._block(body)

    def _block(self, body):
        return self._send_json(200, {
            "model": body.get("model"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": "[mock] 你好，已收到"}}
                        ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        })

    def _stream(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for piece in ["[mock] ", "流", "式 ", "OK"]:
            ev = json.dumps({
                "model": body.get("model"),
                "choices": [{"index": 0, "delta": {"content": piece}}],
            })
            self.wfile.write(f"data: {ev}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def start_mock() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), MockGatewayHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return srv, port


# ── 测试 ──


def test_full_chain():
    """1. 矫治用户输入 → 2. 保存到临时 home → 3. 启动客户端 → 4. 与 mock gateway 对话"""
    print("=== 端到端集成测试 ===\n")

    # 1. 模拟用户在 GUI 输入区粘贴一段 JSON
    raw = (
        '[{"id":"deepseek","config":{"wire":"openai",'
        '"baseURL":"https://api.deepseek.com",'
        '"apiKey":"sk-test-1234567890abcd",'
        '"model":"deepseek-flash"}}]'
    )
    res = normalize(raw)
    assert res.is_valid(), "矫治后无效"
    print(f"  [1] 矫治 ✓ {len(res.rows)} 条 row, {len(res.warnings)} 警告")

    # 2. 保存到临时 home
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        merged = merge_with_user_layer(res.rows, load_user_layer(home))
        save_user_layer(home, merged)
        # 读回验证
        loaded = load_user_layer(home)
        assert len(loaded) == len(merged), "保存/读取条数不一致"
        # 验证密钥已转为 $expr
        prov = [r for r in loaded if r["id"] == "deepseek"][0]
        ak = prov["config"]["apiKey"]
        assert isinstance(ak, dict) and "$expr" in ak, f"密钥没归一化：{ak}"
        print(f"  [2] 保存 ✓ 文件 {home / 'forge.patch.json'}, "
              f"$expr={ak['$expr']!r}")

    # 3. 启动 mock gateway，客户端联通
    srv, port = start_mock()
    client = ForgeGatewayClient(f"http://127.0.0.1:{port}")
    ok, msg = client.health()
    assert ok, f"健康检查失败：{msg}"
    print(f"  [3] gateway 联通 ✓ {msg}")

    # 4. 非流式对话
    r = client.chat([ChatMessage("user", "你好")], model="deepseek-flash")
    assert "[mock]" in r.text
    print(f"  [4] 非流式对话 ✓ {r.text!r}")

    # 5. 流式对话
    chunks: list[str] = []
    r2 = client.stream_chat(
        [ChatMessage("user", "再聊")], model="deepseek-flash",
        on_chunk=lambda p: chunks.append(p),
    )
    assert r2.text == "[mock] 流式 OK"
    assert "".join(chunks) == r2.text
    print(f"  [5] 流式对话 ✓ chunks={chunks}")

    srv.shutdown()
    print("\n✅ 端到端链路通过")


def test_provider_diversity():
    """测试多种输入形态都能被 GUI 接受 → 矫治 → 保存"""
    print("\n=== 多 provider 配置测试 ===")
    inputs = [
        # 数组 + 行注释
        '// 主力\n[{"id":"a","baseURL":"https://a.com","apiKey":"'+'A'*14+'","model":"a-1"},'
        '{"id":"b","baseURL":"https://b.com","apiKey":"'+'B'*14+'","model":"b-1"}]',
        # 分组对象
        '{"providers":{"stepfun":{"wire":"openai","baseURL":"https://api.stepfun.com/v1",'
        '"apiKey":"'+'S'*16+'","model":"step-3.5-flash"}}}',
        # 平铺对象
        '{"deepseek":{"baseURL":"https://api.deepseek.com","apiKey":"'+'D'*16+'","model":"deepseek-flash"}}',
        # 启发式
        'baseUrl: https://api.deepseek.com\napi_key=sk-1234567890abcd\nmodel: deepseek-flash',
    ]
    for i, raw in enumerate(inputs, 1):
        res = normalize(raw)
        active = [r for r in res.rows if r.get("id") != "model" and not r.get("disabled")]
        print(f"  [{i}] 输入 {raw[:40]!r:<42}... → {len(active)} 条有效 row")
        assert res.is_valid(), f"第 {i} 个失败"

    print("  ✅ 4 种形态全部接受")


def test_security_no_leak():
    """确认矫治后磁盘上没有任何明文密钥"""
    print("\n=== 密钥不落盘检查 ===")
    raw = ('{"id":"x","baseURL":"https://x.com","apiKey":"'+'P'*24+'","model":"x-1"}')
    res = normalize(raw)
    serialized = res.to_json()
    assert 'P'*24 not in serialized, "明文密钥泄漏到 JSON"
    print(f"  ✓ 序列化结果不含明文密钥（已转为 env 引用）")
    # 再确认存盘后无明文
    with tempfile.TemporaryDirectory() as tmp:
        save_user_layer(Path(tmp), res.rows)
        text = (Path(tmp) / "forge.patch.json").read_text(encoding="utf-8")
        assert 'P'*24 not in text, "落盘文件含明文密钥"
        print(f"  ✓ 落盘文件 forge.patch.json 不含明文密钥")


if __name__ == "__main__":
    test_full_chain()
    test_provider_diversity()
    test_security_no_leak()
    print("\n🎉 集成测试全部通过")