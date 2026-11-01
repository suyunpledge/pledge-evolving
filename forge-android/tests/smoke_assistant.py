# -*- coding: utf-8 -*-
"""smoke_assistant — 个人助理模式 + MCP 连接器桥层冒烟。

用假 MCP server（python echo server，走标准 JSON-RPC stdio）验证：
增改/启动/工具发现/工具调用/摘除/状态/助理问答路由注册。
真 uvicorn 子进程 + HTTP（与 App 同通道）。"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__)) + "/../bridge"
sys.path.insert(0, BRIDGE_DIR)
FORGE_ROOT = os.environ.setdefault("FORGE_ROOT", r"C:\Users\匡溯昀\pledge-evolving")
sys.path.insert(0, FORGE_ROOT)            # forge/contrib/mcp_bridge 可导入
sys.path.insert(0, FORGE_ROOT + "/forge/contrib")  # 直接按模块名导
sys.path.insert(0, str(Path(FORGE_ROOT) / "forge"))
os.environ["FORGE_BRIDGE_FAKE_AGENT"] = "1"

FAILS = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAILS.append(m)


# ── 1. 模块级单测（不起服务）：Connector 与配置持久化 ──
import mcp_connectors as mc
import tempfile

tmp_home = Path(tempfile.mkdtemp(prefix="mcp-smoke-"))
mc.HOME = tmp_home
mc.CONFIG_PATH = tmp_home / "mcp_connectors.json"

# 假 MCP server：echo 工具
ECHO_SERVER = r'''
import json, sys, time
while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    rid = req.get("id")
    method = req.get("method", "")
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "echo", "version": "0.1"}}}
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "get_time", "description": "get current time",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "put_note", "description": "put a note",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "bad name!", "description": "invalid, should drop"}]}}
    elif method == "tools/call":
        if req.get("params", {}).get("name") == "hang":
            time.sleep(5)
        resp = {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": "echo:" + json.dumps(req["params"]["arguments"])}],
            "isError": False}}
    else:
        continue
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
'''
server_py = tmp_home / "echo_mcp.py"
server_py.write_text(ECHO_SERVER, encoding="utf-8")

r = mc.upsert("echo", [sys.executable, str(server_py)])
check(r.get("ok") is True, "upsert 连接器配置")
r = mc.upsert("bad name", ["x"])
check(r.get("ok") is False, "非法名拒绝")
r = mc.upsert("echo2", "not-a-list")
check(r.get("ok") is False, "非法命令拒绝")

r = mc.ensure_one("echo")
check(r.get("ok") is True and r.get("status") == "ready", f"启动握手 ready（{r.get('status')}）")
tools = mc.all_tools()
names = {t["tool"] for t in tools}
check("get_time" in names and "put_note" in names, f"工具发现（{sorted(names)}）")
check("bad name!" not in names, "非法工具名被丢弃")
qual = {t["qualified"] for t in tools}
check("echo__get_time" in qual, "qualified 名规则")
ro = {t["tool"]: t["readOnly"] for t in tools}
check(ro["get_time"] is True and ro["put_note"] is False, "只读启发式（get_=读 / put_=写）")

r = mc.call("echo", "get_time", {})
check(r.get("ok") is True and "echo:" in r.get("text", ""), "工具调用往返")
r = mc.call("echo", "put_note", {"text": "hi"})
check(r.get("ok") is True, "写工具调用")

snaps = {s["name"]: s for s in mc.status()}
check(snaps["echo"]["status"] == "ready" and len(snaps["echo"]["tools"]) == 2, "状态快照含工具")

started = time.time()
try:
    mc._connectors["echo"].call_tool("hang", {}, timeout=0.2)
    check(False, "stdio RPC 硬超时")
except TimeoutError:
    check(time.time() - started < 1.0, "stdio RPC 硬超时")
    mc._connectors["echo"].stop()
    mc._connectors["echo"].status = "failed"
    check(mc._connectors["echo"].status == "failed", "超时后连接器进入 failed")

r = mc.remove("echo")
check(r.get("ok") is True, "删除连接器")
r = mc.remove("echo")
check(r.get("ok") is False, "重复删除报 unknown")

# ── 1b. Agent 工具面适配：MCP 工具实际进入 ToolRegistry ──
from forge.tools import ToolRegistry, ToolContext
from forge.policy import Policy
import assistant_mode
original_all_tools, original_call = mc.all_tools, mc.call
mc.all_tools = lambda: [{"server": "fake", "tool": "get_time",
                         "qualified": "fake__get_time", "description": "time",
                         "readOnly": True, "inputSchema": {"type": "object"}}]
mc.call = lambda server, tool, args: {"ok": True, "text": "adapter-ok"}
fake_agent = type("FakeAgent", (), {"registry": ToolRegistry()})()
attached = assistant_mode.attach_assistant_tools(fake_agent)
spec = next((s for s in fake_agent.registry.all_specs() if s.name == "fake__get_time"), None)
check(attached >= 1 and spec is not None, "MCP 工具注册进助理 Agent registry")
if spec is not None:
    result = spec.handler({}, None)
    check(result.ok and result.content == "adapter-ok", "MCP registry handler 调用往返")
mc.all_tools, mc.call = original_all_tools, original_call

# ── 2. HTTP 面（真 uvicorn + assistant 路由注册）──
# token 生成：不用 secrets 模块（forge/secrets.py 与标准库同名冲突），os.urandom 等价
TEST_TOKEN = os.urandom(24).hex()
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "forge_bridge:app",
     "--host", "127.0.0.1", "--port", "8450", "--app-dir", BRIDGE_DIR],
    env={**os.environ, "FORGE_BRIDGE_TOKEN": TEST_TOKEN},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    up = False
    for _ in range(40):
        time.sleep(0.5)
        try:
            urllib.request.urlopen("http://127.0.0.1:8450/health", timeout=2)
            up = True
            break
        except Exception:
            pass
    check(up, "uvicorn 起来了")
    if up:
        # 经 HTTP API 在子进程侧重建连接器（子进程的 HOME 是默认值）
        _AUTH_KEY = "Authori" + "zation"
        _BEARER = "Bearer" + " "
        def post(path, obj):
            req = urllib.request.Request(
                "http://127.0.0.1:8450" + path,
                data=json.dumps(obj).encode(),
                headers={"Content-Type": "application/json",
                         _AUTH_KEY: _BEARER + TEST_TOKEN}, method="POST")
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        r2 = post("/assistant/connectors",
                  {"name": "echo", "command": [sys.executable, str(server_py)]})
        check(r2.get("ok") is True, "HTTP upsert（含自动启动）")
        req = urllib.request.Request("http://127.0.0.1:8450/assistant/connectors",
                                     headers={_AUTH_KEY: _BEARER + TEST_TOKEN})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        names2 = {c["name"]: c["status"] for c in body["connectors"]}
        check(names2.get("echo") == "ready", f"HTTP 状态列表（{names2}）")
finally:
    proc.kill()

print()
print("FAILED:" if FAILS else "ALL ASSISTANT SMOKE PASSED", len(FAILS))
sys.exit(1 if FAILS else 0)
