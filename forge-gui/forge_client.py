"""与 forge gateway 通信的最小 OpenAI 兼容客户端。

gateway 接受 /v1/chat/completions（已确认 wire 协议），我们用内置 urllib +
socket，不引入 requests/aiohttp 等额外依赖。

支持：
  - 非流式（一次性返回完整 JSON）
  - 流式（SSE，逐 chunk 回调）
  - 健康检查（GET /v1/models）

客户端 = 消费面，给 GUI 的"交互客户端"标签页用。
"""
from __future__ import annotations

import json
import http.client
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from http_transport import open_response, RequestCancelled


@dataclass
class ChatMessage:
    role: str
    content: str
    # 工具循环用（OpenAI 协议）：assistant 带 tool_calls、tool 带 tool_call_id
    tool_calls: list | None = None
    tool_call_id: str = ""

    def to_dict(self) -> dict:
        d: dict = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class CompletionResult:
    text: str
    model: str = ""
    usage: dict | None = None
    raw: dict | None = None
    # 流式累积的工具调用（OpenAI delta 形状：{id,type,function:{name,arguments}}）
    tool_calls: list[dict] = field(default_factory=list)


class GatewayError(RuntimeError):
    pass


def http_error_detail(error, limit=500):
    try:
        if hasattr(error, "_forge_detail"):
            return error._forge_detail[:limit]
        return error.read(limit).decode("utf-8", errors="replace")
    except (OSError, ValueError, http.client.HTTPException):
        return str(error.reason)
    finally:
        error.close()


class GenerationCancelled(GatewayError):
    pass


class ForgeGatewayClient:
    """与 forge gateway（默认 127.0.0.1:8799）通信。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8799",
                 api_key: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.plugin_policy_version = 0

    # ── 工具方法 ──
    def _request(self, method: str, path: str, body: dict | None = None,
                 stream: bool = False) -> urllib.request.Request:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json",
                   "User-Agent": "forge-gui/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return req

    # ── 工具桥（gateway 需以 --tools 启动）──

    def list_tools(self, *, timeout: float | None = None) -> list[dict]:
        """GET /v1/tools → OpenAI function 工具定义列表。gateway 未开工具桥时抛 GatewayError。"""
        req = self._request("GET", "/v1/tools")
        try:
            with open_response(req, timeout=self.timeout if timeout is None else timeout, cancel_event=None) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise GatewayError(f"HTTP {e.code}: {http_error_detail(e)}") from e
        except (urllib.error.URLError, OSError) as e:
            raise GatewayError(str(e)) from e
        tools = data.get("data") if isinstance(data, dict) else None
        self.plugin_policy_version = (1 if isinstance(data, dict)
            and type(data.get("plugin_policy_version")) is int and data["plugin_policy_version"] == 1 else 0)
        if not isinstance(tools, list):
            raise GatewayError(f"/v1/tools 响应结构异常: {str(data)[:200]}")
        return tools

    def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None,
                  plugin_context: dict | None = None) -> dict:
        """POST /v1/tools/call → gateway 走完整 policy gate 执行一个 forge 工具。"""
        body = {"name": name, "arguments": arguments or {}}
        if plugin_context is not None:
            if self.plugin_policy_version != 1:
                self.list_tools(timeout=timeout)
            if self.plugin_policy_version != 1:
                raise GatewayError("网关不支持插件 Policy 上下文；请更新并重启网关，插件执行已阻止")
            body["plugin_context"] = plugin_context
        req = self._request("POST", "/v1/tools/call", body)
        try:
            with open_response(req, timeout=self.timeout if timeout is None else timeout, cancel_event=None) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise GatewayError(f"HTTP {e.code}: {http_error_detail(e)}") from e
        except (urllib.error.URLError, OSError) as e:
            raise GatewayError(str(e)) from e

    # ── 健康检查 ──
    def health(self, *, cancel_event=None) -> tuple[bool, str]:
        try:
            req = self._request("GET", "/v1/models")
            with open_response(req, timeout=5, cancel_event=cancel_event) as resp:
                if resp.status == 200:
                    return True, f"HTTP {resp.status}"
                return False, f"HTTP {resp.status}"
        except RequestCancelled as exc:
            raise GenerationCancelled("已停止生成") from exc
        except urllib.error.HTTPError as e:
            e.close()
            return False, f"HTTP {e.code} {e.reason}"
        except (urllib.error.URLError, socket.timeout, ConnectionRefusedError) as e:
            return False, f"连接失败：{e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ── 非流式 ──
    def chat(self, messages: list[ChatMessage], model: str = "default",
             temperature: float = 0.7, max_tokens: int | None = None,
             reasoning_effort: str | None = None) -> CompletionResult:
        body = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        req = self._request("POST", "/v1/chat/completions", body)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = http_error_detail(e)
            raise GatewayError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise GatewayError(f"网络错误：{e}") from e

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise GatewayError(f"非 JSON 响应：{raw_text[:200]}") from e

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise GatewayError(f"响应无 choices：{raw_text[:300]}")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content", ""), (str, type(None))):
            raise GatewayError("上游回复 message.content 必须为文本")
        text = message.get("content") or ""
        return CompletionResult(
            text=text,
            model=data.get("model", model),
            usage=data.get("usage"),
            raw=data,
        )

    # ── 流式（SSE） ──
    def stream_chat(self, messages: list[ChatMessage], model: str = "default",
                    temperature: float = 0.7,
                    reasoning_effort: str | None = None,
                    on_chunk: Callable[[str], None] | None = None,
                    cancel_event: threading.Event | None = None,
                    tools: list[dict] | None = None
                    ) -> CompletionResult:
        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled("已停止生成")

        check_cancelled()
        body = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        req = self._request("POST", "/v1/chat/completions", body)
        full_text_parts: list[str] = []
        model_name = ""
        completed = False
        tool_acc: dict[int, dict] = {}   # 按 index 累积 delta.tool_calls
        try:
            with open_response(req, timeout=self.timeout, cancel_event=cancel_event) as resp:
                for line in resp:
                    check_cancelled()
                    raw = line.decode("utf-8", errors="replace").rstrip("\n")
                    if not raw.startswith("data:"):
                        continue
                    payload = raw[len("data:"):].strip()
                    if payload == "[DONE]":
                        completed = True
                        break
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("error"):
                        raise GatewayError(f"上游流式请求失败：{str(ev['error'])[:500]}")
                    if not model_name and ev.get("model"):
                        model_name = ev.get("model", "")
                    choices = ev.get("choices") or []
                    if not isinstance(choices, list):
                        continue
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason") is not None:
                            completed = True
                        delta = choice.get("delta", {}) or {}
                        if not isinstance(delta, dict):
                            continue
                        piece = delta.get("content", "")
                        if isinstance(piece, str) and piece:
                            full_text_parts.append(piece)
                            if on_chunk:
                                on_chunk(piece)
                        # 工具调用按 index 增量拼装（name/arguments 可能分片到达）
                        for item in (delta.get("tool_calls") or []):
                            if not isinstance(item, dict):
                                continue
                            idx = item.get("index")
                            if not isinstance(idx, int):
                                idx = 0
                            slot = tool_acc.setdefault(
                                idx, {"id": "", "type": "function",
                                      "function": {"name": "", "arguments": ""}})
                            if item.get("id"):
                                slot["id"] = str(item["id"])
                            fn = item.get("function") or {}
                            if isinstance(fn, dict):
                                if fn.get("name"):
                                    slot["function"]["name"] += str(fn["name"])
                                if fn.get("arguments"):
                                    slot["function"]["arguments"] += str(fn["arguments"])
        except RequestCancelled as exc:
            raise GenerationCancelled("已停止生成") from exc
        except urllib.error.HTTPError as e:
            err_body = http_error_detail(e)
            raise GatewayError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            check_cancelled()
            raise GatewayError(f"网络错误：{e}") from e
        except OSError as e:
            check_cancelled()
            raise GatewayError(f"流式连接中断：{e}") from e
        check_cancelled()
        if not completed:
            raise GatewayError("流式连接提前结束，回复尚未完成；请重试。")
        return CompletionResult(
            text="".join(full_text_parts),
            model=model_name or model,
            tool_calls=[tool_acc[k] for k in sorted(tool_acc)],
        )
