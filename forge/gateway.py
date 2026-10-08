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
import re
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
def open_upstream(request: urllib.request.Request, timeout: int, *, proxy: str = ""):
    """Open upstream with explicit proxy selection and no credential redirects."""
    from .secret_http import open_authenticated
    return open_authenticated(request, timeout=timeout, proxy=proxy)


class GatewayLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.lines: list[str] = []
        self._lock = threading.Lock()

    #: Rotate at 8 MiB, keep one previous file (same policy as the plugin
    #: audit log) — a long-lived gateway must not grow its log unbounded.
    MAX_LOG_BYTES = 8 * 1024 * 1024

    def _rotate_if_needed(self) -> None:
        try:
            if (self.path.exists()
                    and self.path.stat().st_size >= self.MAX_LOG_BYTES):
                rotated = self.path.with_suffix(self.path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                self.path.replace(rotated)
        except OSError:
            pass  # rotation is best-effort; never block the request path

    def write(self, line: str) -> None:
        from .secrets import redact
        line = redact(line)
        stamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        with self._lock:
            self.lines.append(stamped)
            if len(self.lines) > 2000:
                del self.lines[:1000]
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._rotate_if_needed()
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(stamped + "\n")
                except OSError:
                    pass  # a dead log file must not take the gateway down
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
        provider_options: dict | None = None,
        warmth_path: Path | None = None,
        _credential=None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        from .secrets import VendorCredential, install_logging_redaction
        self._credential = _credential or VendorCredential(api_key, self.upstream)
        self.api_key = self._credential.ref
        install_logging_redaction()
        self.port = port
        self.models = models or []
        self.log = GatewayLog(log_path)
        self.wire = wire                      # wire the *client* speaks
        self.upstream_wire = upstream_wire    # wire the *upstream* speaks
        self.model_map = model_map or {}
        self.proxy = proxy
        self.gateway_token = gateway_token    # trusted gateway authentication only
        if gateway_token:
            from .secrets import _remember
            _remember(gateway_token)
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
        self.provider_options = dict(provider_options or {})
        from .cache_state import CacheWarmth
        from .model import warmth_store
        self.cache_warmth = CacheWarmth(warmth_path) if warmth_path else warmth_store()
        self._auth_lock = threading.Lock()
        self._secret_lock = threading.Lock()
        self._secret_scopes = {}
        self._auth_failures: int = 0
        self._auth_locked_until: float = 0.0

    def secret_scope(self, identifier):
        from .secrets import SecretScope
        import re
        if not identifier:
            return SecretScope()
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('Invalid secret session identifier')
        with self._secret_lock:
            now = time.monotonic()
            for key, (stamp, scope) in list(self._secret_scopes.items()):
                if now - stamp > 4 * 3600:
                    scope.close()
                    del self._secret_scopes[key]
            if identifier not in self._secret_scopes:
                if len(self._secret_scopes) >= 256:
                    raise ValueError('Too many active protected sessions')
                self._secret_scopes[identifier] = (now, SecretScope())
            scope = self._secret_scopes[identifier][1]
            self._secret_scopes[identifier] = (now, scope)
            return scope

    def prepare_request(self, payload, *, secret_scope=None):
        from .model import Provider, _plan_and_shape
        if not isinstance(payload, dict):
            raise ValueError("JSON request must be an object")
        from .secrets import SecretScope
        payload = (secret_scope or SecretScope()).protect(dict(payload))
        model = str(payload.get("model") or "")
        model = self.model_map.get(model, model)
        payload["model"] = model
        conf = self.provider_options
        provider = Provider(
            "gateway", self.upstream, _credential=self._credential, wire=self.upstream_wire,
            default_model=model, vendor=str(conf.get("vendor") or ""),
            cache_control=str(conf.get("cacheControl", "auto")),
            expected_calls=int(conf.get("expectedCalls", 2)),
            call_gap_seconds=float(conf.get("callGapSeconds", 60)),
            adapt=bool(conf.get("adapt", True)), think_budget=conf.get("thinkBudget"),
            flex=bool(conf.get("flex", False)), defer=bool(conf.get("defer", False)),
            max_defer_seconds=float(conf.get("maxDeferSeconds", 0)))
        from . import sampling
        sampling.strip_extra_params(payload, model, self.upstream_wire)
        # These three vendors document include_usage on streaming Chat
        # Completions. Unknown compatibility gateways retain their own shape.
        if (payload.get("stream") and self.upstream_wire == "openai"
                and provider.profile_for_model(model).id in ("deepseek", "bailian", "mimo", "openai")):
            payload["stream_options"] = {**(payload.get("stream_options") or {}), "include_usage": True}
        if "reasoning_effort" in payload and "thinking_budget" in payload:
            raise ValueError("reasoning_effort and thinking_budget cannot be combined")
        shaped, plan = _plan_and_shape(payload, payload.get("messages") or [], provider, model,
                                       store=self.cache_warmth, interactive=True)
        return shaped, plan, provider

    def record_usage(self, provider, plan, raw_usage, service_tier=None):
        from .model import _remember_warmth
        from .vendors import normalize_usage
        usage = normalize_usage(provider.profile_for_model(plan.get("model", "")), raw_usage)
        _remember_warmth(provider, plan, usage, store=self.cache_warmth)
        flex = plan.get("execution", {}).get("flex")
        if flex:
            flex["status"] = "confirmed" if service_tier == "flex" else "unconfirmed"
            flex["discount_confirmed"] = service_tier == "flex"
        self.log.write("ADAPT " + json.dumps({"model": plan.get("model"), "plan": plan,
                                             "usage": usage, "service_tier": service_tier,
                                             "invoice_verified": False}, ensure_ascii=False))


def build_handler(cfg: GatewayConfig):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "forge-gateway/0.1"

        def log_message(self, fmt, *args):  # keep stderr for our own log only
            return

        # -- helpers ----------------------------------------------------
        def _read_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding is not supported; use Content-Length")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or (lengths and not lengths[0].strip().isascii()):
                raise ValueError("invalid Content-Length")
            value = lengths[0].strip() if lengths else "0"
            if not value.isdecimal():
                raise ValueError("Content-Length must be a nonnegative integer")
            length = int(value)
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                raise ValueError("request body ended before Content-Length")
            return body

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            from .secrets import redact
            payload = redact(payload)
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
                self._json(200, {"object": "list", "data": tools, "plugin_policy_version": 1})
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
                        self.close_connection = True
                        self._json(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                                  "message": "too many auth failures; locked out"}})
                        cfg.log.write(f"{self.command} {self.path} -> 429 (auth locked out)")
                        return
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {cfg.gateway_token}":
                        self.close_connection = True
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
            try:
                cfg.secret_scope(self.headers.get('X-Forge-Secret-Scope', ''))
                body = self._read_body()
            except (ValueError, OverflowError) as exc:
                self.close_connection = True
                self._json(400, {"error": {"type": "invalid_request_error", "message": str(exc)}})
                return
            if route == "/v1/tools/call":
                self._tool_call(body)
                return
            self._proxy(body)

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
            if not isinstance(payload, dict):
                self._json(400, {"error": {"type": "invalid_request_error", "message": "body must be an object"}})
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
            plugin_context = payload.get("plugin_context")
            if "plugin_context" in payload:
                from .policy import Decision
                from .tools import ToolResult
                try:
                    capability = {"read_file": "repo.read", "list_dir": "repo.read",
                                  "write_file": "repo.write", "edit_file": "repo.write"}.get(name)
                    limits = {"id": 128, "tool": 64, "capability": 64,
                              "fingerprint": 64, "workspace": 8192, "session": 256}
                    if (not isinstance(plugin_context, dict) or set(plugin_context) != set(limits)
                            or any(not isinstance(plugin_context[k], str) or len(plugin_context[k]) > limit
                                   for k, limit in limits.items())
                            or not re.fullmatch(r"[\w-]{1,128}", plugin_context["id"])
                            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", plugin_context["tool"])
                            or not re.fullmatch(r"[0-9a-f]{64}", plugin_context["fingerprint"])
                            or capability is None or plugin_context["capability"] != capability):
                        raise ValueError("invalid or unsupported plugin capability context")
                    scope = Path(plugin_context["workspace"]).resolve()
                    path_arg = arguments.get("path")
                    if (not scope.is_relative_to(cfg.workspace.resolve()) or not isinstance(path_arg, str)
                            or not Path(path_arg).is_absolute() or not Path(path_arg).resolve().is_relative_to(scope)):
                        raise ValueError("plugin workspace/path exceeds gateway workspace")
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": {"type": "invalid_request_error", "message": str(exc)}})
                    return
                policy_names = (plugin_context["tool"],
                    f"plugin:{plugin_context['id']}:{plugin_context['tool']}", capability)
                touching = [path_arg]
                decisions = [cfg.policy.resolve_ask(cfg.policy.evaluate(n, args=arguments, touching=touching))
                             for n in policy_names]
                decision = (Decision.DENY if Decision.DENY in decisions else
                            Decision.ASK if Decision.ASK in decisions else Decision.ALLOW)
                cfg.log.write(f"PLUGINTOOL {policy_names[1]} capability={capability} decision={decision.value}")
                if decision is not Decision.ALLOW:
                    result = ToolResult(False, error=f"plugin denied or awaiting approval by policy: {policy_names[1]}",
                        meta={"authorization": decision.value, "plugin_policy_checked": True,
                              "requires_approval": decision is Decision.ASK})
                    self._json(200, result.as_dict())
                    return
            ctx = ToolContext(policy=cfg.policy, workspace=cfg.workspace,
                              secret_scope=cfg.secret_scope(self.headers.get('X-Forge-Secret-Scope', '')),
                              isolated=True, extras={"registry": cfg.registry})
            cfg.log.write(f"TOOLCALL {name} args={json.dumps(arguments, ensure_ascii=False)[:200]}")
            try:
                result = cfg.registry.invoke(name, arguments, ctx)
                if plugin_context is not None:
                    result.meta["plugin_policy_checked"] = True
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
            self._upstream_started = False
            scope = cfg.secret_scope(self.headers.get('X-Forge-Secret-Scope', ''))
            if scope.protect_text(self.path) != self.path:
                self._json(400, {'error': {'message': 'Secrets must not be placed in request URLs'}})
                return
            if body:
                try:
                    payload = json.loads(body)
                    if not isinstance(payload, dict): raise ValueError('Request body must be a JSON object')
                    body = json.dumps(scope.protect(payload), ensure_ascii=False).encode('utf-8')
                except (ValueError, TypeError) as exc:
                    self._json(400, {'error': {'message': str(exc)}})
                    return
            translating = cfg.upstream_wire == "openai" and self.path.split("?")[0].rstrip("/").endswith("/messages")
            if translating:
                self._proxy_translated(body)
                return

            # 客户端请求的是「友好模型名」（如 mimo），上游只认真实 id
            # （如 mimo-v2.6-flash）。有映射就改写请求体里的 model。
            body = self._apply_model_map(body)
            plan = provider = None
            if self.path.split("?")[0].rstrip("/").endswith(("/chat/completions", "/messages")):
                try:
                    payload, plan, provider = cfg.prepare_request(json.loads(body),
                        secret_scope=cfg.secret_scope(self.headers.get('X-Forge-Secret-Scope', '')))
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": {"message": str(exc)}})
                    return

            # self.path 是客户端的 OpenAI 兼容路径（/v1/chat/completions），
            # 这个 /v1 是 gateway 自己的协议前缀，upstream 通常没有它——
            # 但有些上游（bigmodel.cn/api/coding/paas/v4）的 chat 端点确实在
            # 末尾不是 /v1；为避免 /v4/v1/chat/completions 这种双前缀，总是剥掉。
            stripped = self.path
            if stripped.startswith("/v1/"):
                stripped = stripped[len("/v1"):]
            url = cfg.upstream + stripped
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() in {'content-type', 'accept', 'user-agent', 'anthropic-version', 'anthropic-beta'}}
            headers.pop("Authorization", None)
            headers.pop("x-api-key", None)
            if cfg.upstream_wire == "anthropic":
                headers["x-api-key"] = cfg._credential._header(url, wire='anthropic')['x-api-key']
                headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
            else:
                headers.update(cfg._credential._header(url, wire='openai'))
            headers["accept-encoding"] = "identity"

            cfg.log.write(f"{self.command} {self.path} -> {url} (bytes={len(body)})")
            request = urllib.request.Request(url, data=body or None, headers=headers, method=self.command)
            try:
                with open_upstream(request, timeout=600, proxy=cfg.proxy) as response:
                    from .request_execution import UsageObserver
                    observer = UsageObserver()
                    ctype = response.headers.get("content-type", "")
                    self.send_response(response.status)
                    self.send_header("Content-Type", ctype)
                    if "text/event-stream" in ctype:
                        # close on completion, otherwise the client waits for the stream to end forever
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.close_connection = True
                        self.end_headers()
                        self._upstream_started = True
                        from .secret_http import SecretSSEFilter
                        fence = SecretSSEFilter()
                        while True:
                            chunk = response.readline(1024 * 1024 + 1)
                            if not chunk: break
                            observer.feed(chunk)
                            self.wfile.write(fence.feed(chunk))
                            self.wfile.flush()
                        self.wfile.write(fence.finish())
                        self.wfile.flush()
                    else:
                        from .secrets import redact
                        from .secret_http import redact_response
                        data = redact_response(response.read())
                        try:
                            observer.observe(json.loads(data))
                        except (ValueError, UnicodeDecodeError):
                            pass
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    if provider is not None:
                        cfg.record_usage(provider, plan, observer.usage, observer.service_tier)
            except urllib.error.HTTPError as exc:
                from .secrets import redact
                from .secret_http import redact_response
                data = redact_response(exc.read())
                cfg.log.write(f"upstream HTTP {exc.code}: {data[:300]!r}")
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("content-type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                cfg.log.write(f"upstream error: {exc!r}")
                self._upstream_failure(exc)

        def _upstream_failure(self, exc):
            from .secrets import redact
            payload = {"type": "error", "error": {"type": "api_error", "message": redact(str(exc))}}
            if getattr(self, "_upstream_started", False):
                # HTTP headers cannot be sent twice after streaming starts.
                self.close_connection = True
                try:
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()
                except OSError:
                    pass  # client already disconnected
            else:
                self._json(502, payload)

        # -- anthropic client in, openai upstream ------------------------
        def _apply_model_map(self, body: bytes) -> bytes:
            """按 cfg.model_map 把请求体里的 model 换成上游真实名；无映射则原样返回。"""
            if not body or not cfg.model_map:
                return body
            if "json" not in (self.headers.get("Content-Type") or "").lower():
                return body
            try:
                payload = json.loads(body.decode("utf-8", "replace") or "{}")
            except (ValueError, UnicodeDecodeError):
                return body
            if not isinstance(payload, dict):
                return body
            requested = payload.get("model")
            if requested is None:
                return body
            mapped = cfg.model_map.get(str(requested))
            if not mapped or mapped == requested:
                return body
            payload["model"] = mapped
            cfg.log.write(f"MODEL-MAP {requested} -> {mapped}")
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def _proxy_translated(self, body: bytes) -> None:
            self._upstream_started = False
            from .wire import OpenAIStreamTranslator, anthropic_to_openai_request, openai_to_anthropic_response

            try:
                payload = json.loads(body.decode("utf-8", "replace") or "{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                              "message": f"bad json: {exc}"}})
                return

            requested = str(payload.get("model", ""))
            upstream_payload = anthropic_to_openai_request(payload, cfg.model_map)
            try:
                upstream_payload, plan, provider = cfg.prepare_request(upstream_payload,
                    secret_scope=cfg.secret_scope(self.headers.get('X-Forge-Secret-Scope', '')))
            except (ValueError, TypeError) as exc:
                self._json(400, {"error": {"message": str(exc)}})
                return
            url = cfg.upstream.rstrip("/") + "/chat/completions"
            headers = {
                "content-type": "application/json",
                **cfg._credential._header(url, wire='openai'),
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
                        from .secrets import redact
                        raw = redact(json.loads(response.read().decode("utf-8", "replace")))
                        cfg.record_usage(provider, plan, raw.get("usage"), raw.get("service_tier"))
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
                    self._upstream_started = True
                    translator = OpenAIStreamTranslator(requested)
                    from .request_execution import UsageObserver
                    observer = UsageObserver()
                    from .secret_http import SecretSSEFilter
                    fence = SecretSSEFilter()
                    for raw_line in response:
                        observer.feed(raw_line)
                        safe_lines = fence.feed(raw_line).decode('utf-8', 'replace').splitlines()
                        for safe_line in safe_lines:
                            for event in translator.feed(safe_line):
                                self.wfile.write(event.encode())
                                self.wfile.flush()
                    for safe_line in fence.finish().decode('utf-8', 'replace').splitlines():
                        for event in translator.feed(safe_line):
                            self.wfile.write(event.encode())
                            self.wfile.flush()
                    for event in translator.finish():
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                    cfg.log.write(f"TRANSLATE stream done: stop={translator.finish_reason} "
                                  f"tools={len(translator.tool_calls)}")
                    cfg.record_usage(provider, plan, observer.usage, observer.service_tier)
            except urllib.error.HTTPError as exc:
                from .secrets import redact
                from .secret_http import redact_response
                data = redact_response(exc.read())
                cfg.log.write(f"upstream HTTP {exc.code}: {data[:300]!r}")
                self._json(exc.code, {"type": "error", "error": {"type": "api_error",
                                                                 "message": data.decode("utf-8", "replace")[:400]}})
            except Exception as exc:
                cfg.log.write(f"translate error: {exc!r}")
                self._upstream_failure(exc)

    return Handler


def serve(cfg: GatewayConfig) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", cfg.port), build_handler(cfg))
    cfg.log.write(f"listening on http://127.0.0.1:{cfg.port} -> {cfg.upstream}")
    return server


__all__ = ["GatewayConfig", "GatewayLog", "build_handler", "serve"]
