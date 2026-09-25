"""Loopback protocol gateway.

This is the component that was built (and proven) while wiring Claude Code into
the fleet: some agent CLIs refuse a provider that does not advertise the model
they were told to use, and a few compute their request path from the base URL.
A local gateway fixes both without touching the vendor's config:

* serves ``/v1/models`` with the ids we want to advertise
* forwards ``/v1/messages`` (Anthropic wire) and ``/v1/chat/completions``
  (OpenAI wire) to the upstream root, so the CLI's own ``/v1`` prefix is not
  doubled
* logs every request, which is how the ``/v1/v1/messages`` bug was found
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOP_BY_HOP = {
    "host", "connection", "content-length", "transfer-encoding",
    "accept-encoding", "keep-alive", "proxy-authenticate", "te",
    "trailers", "upgrade",
}

# The gateway is a *local* bridge. urllib silently adopts the Windows registry
# proxy by default, which turns every upstream hiccup (or a stale proxy port)
# into a ConnectionRefused that looks like the model is down. Upstream calls are
# made through an opener with proxying disabled unless the caller opts in.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_upstream(request: urllib.request.Request, timeout: int, *, proxy: str = ""):
    """Open an upstream request.

    ``proxy`` is explicit on purpose. Relying on urllib's implicit proxy
    discovery produced a ConnectionRefused that looked exactly like "the model
    is down" while PowerShell (which always honours the system proxy) reached
    the same host fine — the upstream in question is only reachable through the
    proxy at all.
    """
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        return opener.open(request, timeout=timeout)
    return _DIRECT_OPENER.open(request, timeout=timeout)


class GatewayLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        stamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        with self._lock:
            self.lines.append(stamped)
            if len(self.lines) > 2000:
                del self.lines[:1000]
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(stamped + "\n")
        print(stamped, file=sys.stderr, flush=True)


class GatewayConfig:
    def __init__(
        self,
        *,
        upstream: str,
        api_key: str = "",
        port: int = 8799,
        models: list[str] | None = None,
        log_path: Path | None = None,
        wire: str = "anthropic",
        upstream_wire: str = "anthropic",
        model_map: dict[str, str] | None = None,
        proxy: str = "",
        gateway_token: str = "",
        max_auth_failures: int = 5,
        registry=None,
        workspace: str = "",
        policy=None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.api_key = api_key
        self.port = port
        self.models = models or []
        self.log = GatewayLog(log_path)
        self.wire = wire                      # wire the *client* speaks
        self.upstream_wire = upstream_wire    # wire the *upstream* speaks
        self.model_map = model_map or {}
        self.proxy = proxy
        self.gateway_token = gateway_token    # empty = no auth; set = bearer check
        self.max_auth_failures = max_auth_failures  # rate limit: lock out after N failures
        # -- tool bridge: optional forge ToolRegistry + workspace root.
        # When attached, GET /v1/tools lists OpenAI function schemas and
        # POST /v1/tools/call executes a tool through the full policy gate.
        self.registry = registry
        self.workspace = Path(workspace) if workspace else Path.cwd()
        # Tool-bridge policy: MUST come from the composed user config (never a
        # bare Policy() default — that silently re-permissions the bridge).
        # Headless gateway has nobody to approve an ASK, so non_interactive
        # stays True: unresolved ASK collapses to DENY, same as `forge run`.
        self.policy = policy
        self._auth_lock = threading.Lock()
        self._auth_failures: int = 0
        self._auth_locked_until: float = 0.0


def build_handler(cfg: GatewayConfig):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "forge-gateway/0.1"

        def log_message(self, fmt, *args):  # keep stderr for our own log only
            return

        # -- helpers ----------------------------------------------------
        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- routes -----------------------------------------------------
        def do_GET(self):  # noqa: N802
            route = self.path.split("?")[0].rstrip("/")
            if route in ("/v1/models", "/models"):
                now = int(time.time())
                self._json(200, {
                    "object": "list",
                    "data": [{"id": m, "object": "model", "created": now, "owned_by": "forge-gateway"}
                             for m in cfg.models],
                })
                cfg.log.write(f"GET {self.path} -> local model list ({len(cfg.models)} ids)")
                return
            if route == "/v1/tools":
                if cfg.registry is None:
                    self._json(404, {"type": "error",
                                     "error": {"type": "not_found",
                                               "message": "tool registry not attached to this gateway"}})
                    return
                specs = cfg.registry.callable_specs()
                tools = []
                for spec in specs:
                    props = {}
                    required = []
                    for key, val in (spec.schema or {}).items():
                        props[key] = {"type": val, "description": key}
                    required = [k for k, v in (spec.schema or {}).items()
                                if not k.startswith("_")]
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": f"forge_{spec.name}",
                            "description": spec.description,
                            "parameters": {
                                "type": "object",
                                "properties": props,
                                "required": required,
                                "additionalProperties": False,
                            },
                        },
                        "x-forge-meta": {
                            "read_only": spec.read_only,
                            "deferred": spec.is_deferred,
                            "tags": list(spec.tags),
                        },
                    },
                    )
                self._json(200, {"object": "list", "data": tools})
                cfg.log.write(f"GET {self.path} -> tool list ({len(tools)} tools)")
                return
            if route in ("/health", ""):
                health: dict[str, Any] = {"ok": True, "upstream": cfg.upstream}
                if cfg.registry is not None:
                    health["tools"] = len(cfg.registry.callable_specs())
                self._json(200, health)
                return
            self._proxy(b"")

        def do_POST(self):  # noqa: N802
            # Optional gateway authentication with rate limiting
            if cfg.gateway_token:
                now = time.time()
                with cfg._auth_lock:
                    # Rate limit: lock out after max_auth_failures consecutive failures
                    if cfg._auth_locked_until > now:
                        self._json(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                                  "message": "too many auth failures; locked out"}})
                        cfg.log.write(f"{self.command} {self.path} -> 429 (auth locked out)")
                        return
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {cfg.gateway_token}":
                        cfg._auth_failures += 1
                        if cfg._auth_failures >= cfg.max_auth_failures:
                            cfg._auth_locked_until = now + 30.0  # 30 second lockout
                            cfg.log.write(f"auth locked out after {cfg._auth_failures} failures")
                        self._json(401, {"type": "error", "error": {"type": "authentication_error",
                                                                  "message": "invalid or missing gateway token"}})
                        cfg.log.write(f"{self.command} {self.path} -> 401 (bad gateway token)")
                        return
                    cfg._auth_failures = 0  # reset on success
            route = self.path.split("?")[0].rstrip("/")
            if route == "/v1/tools/call":
                self._tool_call(self._read_body())
                return
            self._proxy(self._read_body())

        def _tool_call(self, body: bytes) -> None:
            """Execute one forge tool through the full policy gate.

            Body: {"name": "read_file", "arguments": {"path": "x.py"}}
            The name may arrive with or without the forge_ prefix.
            """
            from .tools import ToolContext

            if cfg.registry is None:
                self._json(404, {"type": "error",
                                 "error": {"type": "not_found",
                                           "message": "tool registry not attached"}})
                return
            try:
                payload = json.loads(body.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"type": "error",
                                 "error": {"type": "invalid_request_error",
                                           "message": f"bad json: {exc}"}})
                return
            name = str(payload.get("name", "")).strip()
            if name.startswith("forge_"):
                name = name[len("forge_"):]
            arguments = payload.get("arguments") or {}
            if not isinstance(arguments, dict):
                self._json(400, {"type": "error",
                                 "error": {"type": "invalid_request_error",
                                           "message": "'arguments' must be an object"}})
                return
            # deferred tools must be activated before use; do it implicitly so
            # remote callers don't need a separate activation round-trip, but
            # log it (activation widens the session surface).
            spec = None
            for s in cfg.registry.all_specs():
                if s.name == name:
                    spec = s
                    break
            if spec is None:
                self._json(404, {"type": "error",
                                 "error": {"type": "not_found", "message": f"unknown tool: {name}"}})
                return
            if spec.is_deferred:
                cfg.registry.activate(name)
                cfg.log.write(f"TOOLCALL implicit activation: {name}")
            if cfg.policy is None:
                # fail-closed: a gateway started without a policy must not
                # execute tools under invented default permissions
                self._json(503, {"type": "error",
                                 "error": {"type": "api_error",
                                           "message": "tool bridge disabled: no policy attached "
                                                      "(start gateway with --registry/--profile so the "
                                                      "bridge inherits the real permission config)"}})
                return
            ctx = ToolContext(policy=cfg.policy, workspace=cfg.workspace,
                              extras={"registry": cfg.registry})
            cfg.log.write(f"TOOLCALL {name} args={json.dumps(arguments, ensure_ascii=False)[:200]}")
            try:
                result = cfg.registry.invoke(name, arguments, ctx)
            except Exception as exc:  # never kill the handler
                self._json(500, {"type": "error",
                                 "error": {"type": "api_error", "message": f"{type(exc).__name__}: {exc}"}})
                return
            self._json(200, {
                "ok": result.ok,
                "content": result.content,
                "error": result.error,
                "meta": result.meta,
            })

        def _proxy(self, body: bytes) -> None:
            translating = cfg.upstream_wire == "openai" and self.path.split("?")[0].rstrip("/").endswith("/messages")
            if translating:
                self._proxy_translated(body)
                return

            # self.path 是客户端的 OpenAI 兼容路径（/v1/chat/completions），
            # 这个 /v1 是 gateway 自己的协议前缀，upstream 通常没有它——
            # 但有些上游（bigmodel.cn/api/coding/paas/v4）的 chat 端点确实在
            # 末尾不是 /v1；为避免 /v4/v1/chat/completions 这种双前缀，总是剥掉。
            stripped = self.path
            if stripped.startswith("/v1/"):
                stripped = stripped[len("/v1"):]
            url = cfg.upstream + stripped
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            headers.pop("Authorization", None)
            headers.pop("x-api-key", None)
            if cfg.wire == "anthropic":
                headers["x-api-key"] = cfg.api_key
                headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
            else:
                headers["Authorization"] = f"Bearer {cfg.api_key}"
            headers["accept-encoding"] = "identity"

            cfg.log.write(f"{self.command} {self.path} -> {url} | {body[:200].decode('utf-8', 'replace')}")
            request = urllib.request.Request(url, data=body or None, headers=headers, method=self.command)
            try:
                with open_upstream(request, timeout=600, proxy=cfg.proxy) as response:
                    ctype = response.headers.get("content-type", "")
                    self.send_response(response.status)
                    self.send_header("Content-Type", ctype)
                    if "text/event-stream" in ctype:
                        # close on completion, otherwise the client waits for the stream to end forever
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.close_connection = True
                        self.end_headers()
                        while True:
                            chunk = response.read(4096)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        data = response.read()
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
            except urllib.error.HTTPError as exc:
                data = exc.read()
                cfg.log.write(f"upstream HTTP {exc.code}: {data[:300]!r}")
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("content-type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                cfg.log.write(f"upstream error: {exc!r}")
                self._json(502, {"type": "error", "error": {"type": "api_error", "message": str(exc)}})

        # -- anthropic client in, openai upstream ------------------------
        def _proxy_translated(self, body: bytes) -> None:
            from .wire import OpenAIStreamTranslator, anthropic_to_openai_request, openai_to_anthropic_response

            try:
                payload = json.loads(body.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                              "message": f"bad json: {exc}"}})
                return

            requested = str(payload.get("model", ""))
            upstream_payload = anthropic_to_openai_request(payload, cfg.model_map)
            url = cfg.upstream.rstrip("/") + "/chat/completions"
            headers = {
                "content-type": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
                "accept-encoding": "identity",
            }
            cfg.log.write(f"TRANSLATE {requested} -> {upstream_payload.get('model')} @ {url} "
                          f"(messages={len(upstream_payload.get('messages', []))}, "
                          f"tools={len(upstream_payload.get('tools', []) or [])}, "
                          f"stream={bool(upstream_payload.get('stream'))})")

            request = urllib.request.Request(
                url, data=json.dumps(upstream_payload, ensure_ascii=False).encode(),
                headers=headers, method="POST")
            try:
                with open_upstream(request, timeout=600, proxy=cfg.proxy) as response:
                    if not upstream_payload.get("stream"):
                        raw = json.loads(response.read().decode("utf-8", "replace"))
                        translated = openai_to_anthropic_response(raw, requested)
                        cfg.log.write(f"TRANSLATE reply ok: stop={translated['stop_reason']} "
                                      f"blocks={len(translated['content'])}")
                        self._json(200, translated)
                        return

                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    translator = OpenAIStreamTranslator(requested)
                    for raw_line in response:
                        for event in translator.feed(raw_line.decode("utf-8", "replace")):
                            self.wfile.write(event.encode())
                            self.wfile.flush()
                    for event in translator.finish():
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                    cfg.log.write(f"TRANSLATE stream done: stop={translator.finish_reason} "
                                  f"tools={len(translator.tool_calls)}")
            except urllib.error.HTTPError as exc:
                data = exc.read()
                cfg.log.write(f"upstream HTTP {exc.code}: {data[:300]!r}")
                self._json(exc.code, {"type": "error", "error": {"type": "api_error",
                                                                 "message": data.decode("utf-8", "replace")[:400]}})
            except Exception as exc:
                cfg.log.write(f"translate error: {exc!r}")
                self._json(502, {"type": "error", "error": {"type": "api_error", "message": str(exc)}})

    return Handler


def serve(cfg: GatewayConfig) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", cfg.port), build_handler(cfg))
    cfg.log.write(f"listening on http://127.0.0.1:{cfg.port} -> {cfg.upstream}")
    return server


__all__ = ["GatewayConfig", "GatewayLog", "build_handler", "serve"]
