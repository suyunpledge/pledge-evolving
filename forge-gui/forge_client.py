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
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class CompletionResult:
    text: str
    model: str = ""
    usage: dict | None = None
    raw: dict | None = None


class GatewayError(RuntimeError):
    pass


class ForgeGatewayClient:
    """与 forge gateway（默认 127.0.0.1:8799）通信。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8799",
                 api_key: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

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

    # ── 健康检查 ──
    def health(self) -> tuple[bool, str]:
        try:
            req = self._request("GET", "/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True, f"HTTP {resp.status}"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code} {e.reason}"
        except (urllib.error.URLError, socket.timeout, ConnectionRefusedError) as e:
            return False, f"连接失败：{e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ── 非流式 ──
    def chat(self, messages: list[ChatMessage], model: str = "default",
             temperature: float = 0.7, max_tokens: int | None = None) -> CompletionResult:
        body = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        req = self._request("POST", "/v1/chat/completions", body)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise GatewayError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise GatewayError(f"网络错误：{e}") from e

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise GatewayError(f"非 JSON 响应：{raw_text[:200]}") from e

        choices = data.get("choices") or []
        if not choices:
            raise GatewayError(f"响应无 choices：{raw_text[:300]}")
        text = choices[0].get("message", {}).get("content", "")
        return CompletionResult(
            text=text,
            model=data.get("model", model),
            usage=data.get("usage"),
            raw=data,
        )

    # ── 流式（SSE） ──
    def stream_chat(self, messages: list[ChatMessage], model: str = "default",
                    temperature: float = 0.7,
                    on_chunk: Callable[[str], None] | None = None
                    ) -> CompletionResult:
        body = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        req = self._request("POST", "/v1/chat/completions", body)
        full_text_parts: list[str] = []
        model_name = ""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for line in resp:
                    raw = line.decode("utf-8", errors="replace").rstrip("\n")
                    if not raw.startswith("data:"):
                        continue
                    payload = raw[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not model_name and ev.get("model"):
                        model_name = ev.get("model", "")
                    for choice in ev.get("choices") or []:
                        delta = choice.get("delta", {}) or {}
                        piece = delta.get("content", "")
                        if piece:
                            full_text_parts.append(piece)
                            if on_chunk:
                                try:
                                    on_chunk(piece)
                                except Exception:
                                    pass  # 回调异常不能断流
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise GatewayError(f"HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise GatewayError(f"网络错误：{e}") from e

        return CompletionResult(
            text="".join(full_text_parts),
            model=model_name or model,
        )