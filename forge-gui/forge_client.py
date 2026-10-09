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
from forge.secrets import SecretScope, redact, VendorCredential


@dataclass
class ChatMessage:
    role: str
    content: str
    # 工具循环用（OpenAI 协议）：assistant 带 tool_calls、tool 带 tool_call_id
    tool_calls: list | None = None
    tool_call_id: str = ""
    reasoning_content: str = ""

    def to_dict(self, secret_scope=None) -> dict:
        d: dict = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        return secret_scope.protect(d) if secret_scope is not None else redact(d)


@dataclass
class CompletionResult:
    text: str
    model: str = ""
    usage: dict | None = None
    raw: dict | None = None
    # 流式累积的工具调用（OpenAI delta 形状：{id,type,function:{name,arguments}}）
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_content: str = ""


class GatewayError(RuntimeError):
    def __init__(self, message):
        super().__init__(redact(str(message)))


def http_error_detail(error, limit=500):
    try:
        if hasattr(error, "_forge_detail"):
            return redact(error._forge_detail)[:limit]
        return redact(error.read(limit).decode("utf-8", errors="replace"))
    except (OSError, ValueError, http.client.HTTPException):
        return redact(str(error.reason))
    finally:
        error.close()


class GenerationCancelled(GatewayError):
    pass


class ForgeGatewayClient:
    """与 forge gateway（默认 127.0.0.1:8799）通信。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8799",
                 api_key: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._credential = VendorCredential(api_key, self.base_url)
        self.api_key = self._credential.ref
        self.secret_scope = SecretScope()
        self.secret_session = __import__('uuid').uuid4().hex
        self.timeout = timeout
        self.plugin_policy_version = 0

    # ── 工具方法 ──
    def reset_secret_session(self):
        self.secret_scope.close()
        self.secret_scope = SecretScope()
        self.secret_session = __import__('uuid').uuid4().hex

    def _request(self, method: str, path: str, body: dict | None = None,
                 stream: bool = False) -> urllib.request.Request:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json",
                   "User-Agent": "forge-gui/0.1", "X-Forge-Secret-Scope": self.secret_session}
        if self.api_key:
            headers.update(self._credential._header(url, wire='openai'))
        if body is not None:
            data = json.dumps(self.secret_scope.protect(body), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return req

    # ── 工具桥（gateway 需以 --tools 启动）──

    def list_tools(self, *, timeout: float | None = None) -> list[dict]:
        """GET /v1/tools → OpenAI function 工具定义列表。gateway 未开工具桥时抛 GatewayError。"""
        req = self._request("GET", "/v1/tools")
        try:
            with open_response(req, timeout=self.timeout if timeout is None else timeout, cancel_event=None) as resp:
                data = redact(json.loads(resp.read().decode("utf-8", "replace")))
        except urllib.error.HTTPError as e:
            raise GatewayError(f"HTTP {e.code}: {http_error_detail(e)}") from None
        except (urllib.error.URLError, OSError) as e:
            raise GatewayError(str(e)) from None
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
                return redact(json.loads(resp.read().decode("utf-8", "replace")))
        except urllib.error.HTTPError as e:
            raise GatewayError(f"HTTP {e.code}: {http_error_detail(e)}") from None
        except (urllib.error.URLError, OSError) as e:
            raise GatewayError(str(e)) from None

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
            return False, redact(f"HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, socket.timeout, ConnectionRefusedError) as e:
            return False, redact(f"连接失败：{e}")
        except Exception as e:
            return False, redact(f"{type(e).__name__}: {e}")

    # ── 非流式 ──
    def chat(self, messages: list[ChatMessage], model: str = "default",
             temperature: float = 0.7, max_tokens: int | None = None,
             reasoning_effort: str | None = None, cancel_event=None) -> CompletionResult:
        body = {
            "model": model,
            "messages": [m.to_dict(self.secret_scope) for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        req = self._request("POST", "/v1/chat/completions", body)
        try:
            with open_response(req, timeout=self.timeout, cancel_event=cancel_event) as resp:
                from forge.secret_http import redact_response
                raw = resp.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise GatewayError("响应超过 2 MiB，请缩小请求")
                raw_text = redact_response(raw).decode('utf-8')
        except RequestCancelled:
            raise GenerationCancelled("已停止生成") from None
        except urllib.error.HTTPError as e:
            err_body = http_error_detail(e)
            raise GatewayError(f"HTTP {e.code}: {err_body}") from None
        except (urllib.error.URLError, socket.timeout) as e:
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled("已停止生成") from None
            raise GatewayError(f"网络错误：{e}") from None
        except OSError as e:
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled("已停止生成") from None
            raise GatewayError(f"连接中断：{e}") from None

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise GatewayError(f"非 JSON 响应：{raw_text[:200]}") from None

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
            reasoning_content=str(message.get("reasoning_content") or ""),
        )

    def plan_task(self, messages, level, *, model="default", cancel_event=None):
        """GUI's streaming path uses the same bounded, tool-free plan contract."""
        from forge.planning import planning_level, planning_prompt, token_cap, TaskPlan, PlanningError
        planning_level(level)
        if level == 'none':
            return None, None
        inputs = [ChatMessage('system', planning_prompt(level)),
                  *[m for m in messages if m.role in {'user', 'assistant'}]]
        result = self.chat(inputs, model=model, max_tokens=token_cap(level), cancel_event=cancel_event)
        raw = result.raw or {}
        choices = raw.get('choices') or []
        if result.tool_calls or (choices and (choices[0].get('message') or {}).get('tool_calls')):
            raise PlanningError('Planner attempted to call tools')
        return TaskPlan.parse(self.secret_scope.protect_text(result.text), level), result.usage

    # ── 流式（SSE） ──
    def review_code(self, code, *, model='default', cancel_event=None):
        from forge.code_review import review_messages, ReviewReport, REVIEW_TOKENS
        inputs, truncated, chars = review_messages(code, self.secret_scope)
        result = self.chat([ChatMessage(m['role'], m['content']) for m in inputs],
                           model=model, max_tokens=REVIEW_TOKENS, temperature=0.3, cancel_event=cancel_event)
        choices = (result.raw or {}).get('choices') or []
        if result.tool_calls or (choices and (choices[0].get('message') or {}).get('tool_calls')):
            raise GatewayError('Review model attempted a tool call; no tool was executed')
        text = self.secret_scope.protect_text(result.text).strip()
        if not text or len(text) > 16000:
            raise GatewayError('Review model returned empty or oversized feedback')
        return ReviewReport(text, result.usage or {}, result.model, truncated=truncated, reviewed_chars=chars)

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
            "messages": [m.to_dict(self.secret_scope) for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        req = self._request("POST", "/v1/chat/completions", body)
        full_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = None
        model_name = ""
        completed = False
        tool_acc: dict[int, dict] = {}   # 按 index 累积 delta.tool_calls
        from forge.secret_http import SecretSSEFilter
        fence = SecretSSEFilter()
        try:
            with open_response(req, timeout=self.timeout, cancel_event=cancel_event) as resp:
                def safe_lines():
                    for upstream_line in resp:
                        yield from fence.feed(upstream_line).splitlines(keepends=True)
                    yield from fence.finish().splitlines(keepends=True)
                for line in safe_lines():
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
                    if isinstance(ev.get("usage"), dict):
                        usage = ev["usage"]
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
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            reasoning_parts.append(reasoning)
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
            raise GatewayError(f"HTTP {e.code}: {err_body}") from None
        except (urllib.error.URLError, socket.timeout) as e:
            check_cancelled()
            raise GatewayError(f"网络错误：{e}") from None
        except OSError as e:
            check_cancelled()
            raise GatewayError(f"流式连接中断：{e}") from None
        check_cancelled()
        if not completed:
            raise GatewayError("流式连接提前结束，回复尚未完成；请重试。")
        return CompletionResult(
            text="".join(full_text_parts),
            model=model_name or model,
            tool_calls=[tool_acc[k] for k in sorted(tool_acc)],
            usage=usage,
            reasoning_content="".join(reasoning_parts),
        )
