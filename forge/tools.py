"""Tool registry with deferred loading.

Borrowed from WorkBuddy/CodeBuddy: the *exposure* policy (``--tools`` with
``Defer(X)`` / ``NoDefer(X)`` modifiers) is orthogonal to the *behaviour*
policy (allow/ask/deny). ``NoDefer`` always wins, so a tool can never be
hidden by an over-eager deferral rule.

A deferred tool is invisible to the model until ``tool_search`` surfaces it;
that keeps the system prompt small when the tool surface is wide.
"""

from __future__ import annotations

import json
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .policy import Decision, Policy, ToolEffect
from .runtime_lock import resource_lock
from contextlib import contextmanager, ExitStack
from .secrets import SecretScope, assert_public_path, redact, read_public_bytes


class ToolError(RuntimeError):
    pass


#: Tool names registered by ``build_builtin_registry``. Plugin/bridge tool
#: registration sites compare against this so a contributed tool can never
#: shadow a builtin (single source of truth; keep in sync with the registry).
BUILTIN_TOOL_NAMES = frozenset({
    "read_file", "list_dir", "grep", "write_file", "delete_file", "edit_file",
    "apply_patch", "edit_config", "read_range", "file_outline", "shell_exec",
    "tool_search", "skill_list", "spawn_subagent", "memory_recall",
    "web_search", "fetch_url", "datetime", "calculator",
})


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return redact({"ok": self.ok, "content": self.content, "error": self.error, "meta": self.meta})


@dataclass
class ToolContext:
    policy: Policy
    workspace: Path
    session: Any | None = None
    emit: Callable[..., None] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    secret_scope: SecretScope = field(default_factory=SecretScope, repr=False)
    # 2026-10-08 P1 修复：默认值从 False 翻转为 True（fail-closed）。
    # 原来的 fail-open 意味着任何一个忘记传 isolated=True 的嵌入者都在
    # 静默地裸奔——agent 可以通过它调 shell_exec / 原生插件，密钥防线形同虚设。
    # 翻转后：默认即受保护；确需运行可信原生工具的宿主必须显式声明
    # ``isolated=False``（写下来这个决定，才会去审视它）。
    # 生产构造点（gateway.py / loop.py）本就显式传 True，行为不变；
    # plugin_capabilities.py 的插件桥翻转后默认获得保护，正是期望效果。
    isolated: bool = True
    cancellation: Any | None = None

    def fire(self, **event: Any) -> None:
        if self.emit is not None:
            self.emit(**redact(event))


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[[dict[str, Any], ToolContext], ToolResult]
    read_only: bool = True
    deferred: bool = False
    no_defer: bool = False
    tags: tuple[str, ...] = ()
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def is_deferred(self) -> bool:
        return self.deferred and not self.no_defer


class ToolRegistry:
    def __init__(self, *, expose: Iterable[str] | None = None, hide: Iterable[str] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._activated: set[str] = set()
        self._expose = list(expose) if expose is not None else None
        self._hide = list(hide)
        self._effects: dict[str, ToolEffect] = {}
        self._requires: dict[str, tuple[str, ...]] = {}

    # -- registration ----------------------------------------------------
    def register(self, spec: ToolSpec, *, force: bool = False, trusted: bool = False,
                 effect: ToolEffect | None = None, requires: Iterable[str] = ()) -> None:
        if effect is not None and not isinstance(effect, ToolEffect):
            raise ValueError('Host effect must be a ToolEffect')
        # QA-08（独立 QA 回归）：同名静默覆盖是潜伏风险——插件热重载或
        # 多贡献源并存时会无声顶掉既有工具。默认冲突即错；宿主显式
        # force=True 才允许覆盖。名称含空白一律拒绝。
        if any(ch.isspace() for ch in spec.name):
            raise ValueError(f"tool name {spec.name!r} must not contain whitespace")
        if spec.name in self._specs and not force:
            raise ValueError(
                f"tool {spec.name!r} is already registered; pass force=True to replace")
        self._specs[spec.name] = spec
        verified = trusted
        # RC1 compatibility for the existing declarative plugin broker. This
        # exact shipped handler is host code, not plugin code; it checks grants,
        # revocation and delegates repo operations to the Gateway's real Policy.
        # New adapters use explicit trusted registration. Names/tags/manifest
        # read_only flags alone can never activate this compatibility bridge.
        code = getattr(spec.handler, '__code__', None)
        broker = Path(__file__).resolve().parent.parent / 'forge-gui' / 'plugin_capabilities.py'
        if code is not None and Path(code.co_filename).resolve() == broker:
            # Filename/qualname can be forged by code.replace(). Require the
            # actual code object owned by the loaded host class and its instance.
            for module in tuple(sys.modules.values()):
                if module is None or getattr(module, '__file__', None) is None:
                    continue
                if Path(module.__file__).resolve() != broker:
                    continue
                cls = getattr(module, 'CapabilityRuntime', None)
                reload_code = getattr(getattr(cls, 'reload', None), '__code__', None)
                owners = [cell.cell_contents for cell in (getattr(spec.handler, '__closure__', None) or ())]
                owner = next((v for v in owners if cls is not None and type(v) is cls), None)
                if (reload_code is not None and any(code is item for item in reload_code.co_consts)
                        and owner is not None and getattr(owner._execute, '__func__', None) is cls._execute):
                    meta = (spec.handler.__defaults__ or (None,))[-1]
                    if isinstance(meta, dict):
                        target = meta.get('target')
                        if target in {'write_file', 'edit_file'}:
                            effect = ToolEffect.DELEGATED_WRITE
                        elif target in {'read_file', 'list_dir'}:
                            effect = ToolEffect.FILE_READ
                        elif target == getattr(module, 'SKILL_TARGET', None):
                            effect = ToolEffect.READ
                        else:
                            break
                        verified = True
                break
        if verified and ('native' in spec.tags or spec.name in {'shell_exec', 'notebook_edit'}):
            effect = ToolEffect.NATIVE
        self._effects[spec.name] = (effect or (ToolEffect.READ if spec.read_only else ToolEffect.WRITE)) if verified else ToolEffect.UNKNOWN
        self._requires[spec.name] = tuple(requires) if verified else ()

    def unregister(self, name: str) -> bool:
        """QA-08: plugin lifecycle needs a removal path."""
        self._effects.pop(name, None)
        self._requires.pop(name, None)
        self._activated.discard(name)
        return self._specs.pop(name, None) is not None

    def tool(
        self,
        name: str,
        description: str,
        *,
        read_only: bool = True,
        deferred: bool = False,
        no_defer: bool = False,
        tags: Iterable[str] = (),
        schema: dict[str, Any] | None = None,
        trusted: bool = False,
        effect: ToolEffect | None = None,
        requires: Iterable[str] = (),
    ):
        def deco(fn):
            self.register(ToolSpec(
                name=name,
                description=description,
                handler=fn,
                read_only=read_only,
                deferred=deferred,
                no_defer=no_defer,
                tags=tuple(tags),
                schema=schema or {},
            ), trusted=trusted, effect=effect, requires=requires)
            return fn

        return deco

    # -- exposure --------------------------------------------------------
    def _hidden(self, name: str) -> bool:
        if self._hide and Policy._matches(self._hide, name):
            return True
        if self._expose is not None and not Policy._matches(self._expose, name):
            return True
        return False

    def visible(self) -> list[ToolSpec]:
        out = []
        for spec in self._specs.values():
            if self._hidden(spec.name):
                continue
            if spec.is_deferred and spec.name not in self._activated:
                continue
            out.append(spec)
        return sorted(out, key=lambda s: s.name)

    def names(self) -> list[str]:
        return [s.name for s in self.visible()]

    def all_specs(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.name)

    def callable_specs(self) -> list[ToolSpec]:
        """All non-hidden tools including deferred ones.

        Remote bridges (gateway /v1/tools) need the full surface: deferred
        tools are activated implicitly at call time, so hiding them from the
        listing would make them uncallable from outside.
        """
        return [s for s in self.all_specs() if not self._hidden(s.name)]

    def search(self, query: str, limit: int = 5) -> list[ToolSpec]:
        query = (query or "").strip().lower()
        hits: list[tuple[int, ToolSpec]] = []
        for spec in self._specs.values():
            if self._hidden(spec.name) or not spec.is_deferred:
                continue
            haystack = f"{spec.name} {spec.description} {' '.join(spec.tags)}".lower()
            score = 0
            if query and query in haystack:
                score = 10 - haystack.index(query) // 20
            elif query:
                tokens = [t for t in query.replace(",", " ").split() if t]
                score = sum(3 for t in tokens if t in haystack)
            if score > 0:
                hits.append((score, spec))
        hits.sort(key=lambda pair: (-pair[0], pair[1].name))
        found = [spec for _, spec in hits[:limit]]
        for spec in found:
            self._activated.add(spec.name)
        return found

    def activate(self, name: str) -> bool:
        if name in self._specs:
            self._activated.add(name)
            return True
        return False

    def clone(self) -> "ToolRegistry":
        """An isolated view for a subagent.

        Specs are immutable and shared; the *activation* set is not. Without
        this, a child calling ``tool_search`` silently widens the parent's tool
        surface for the rest of the session.
        """
        out = ToolRegistry(expose=self._expose, hide=self._hide)
        out._specs = dict(self._specs)
        out._activated = set(self._activated)
        out._effects = dict(self._effects)
        out._requires = dict(self._requires)
        return out

    def effect(self, name, args=None):
        effect = self._effects.get(name, ToolEffect.UNKNOWN)
        if effect is ToolEffect.CONFIG:
            return ToolEffect.WRITE if (args or {}).get('patch') is not None else ToolEffect.FILE_READ
        return effect

    def writes(self, name, args=None):
        # A delegated host gate must not hold a workspace lock while waiting
        # for Gateway/connector approval: the actual writer acquires that lock.
        return self.effect(name, args) in {ToolEffect.WRITE, ToolEffect.NATIVE}

    def side_effects(self, name, args=None):
        return self.effect(name, args) in {ToolEffect.WRITE, ToolEffect.NATIVE, ToolEffect.DELEGATED_WRITE}

    @contextmanager
    def write_guard(self, name, args, ctx):
        with ExitStack() as locks:
            if self.writes(name, args):
                locks.enter_context(resource_lock('workspace-write', ctx.workspace))
                targets = [v for k,v in (args or {}).items() if k in {'path', 'file', 'target', 'root'} and isinstance(v, str)]
                blocks = (args or {}).get('patches') or (args or {}).get('edits') or []
                if isinstance(blocks, list):
                    targets += [b['path'] for b in blocks if isinstance(b, dict) and isinstance(b.get('path'), str)]
                paths = sorted({str(ctx.policy.abs_path(p).resolve()) for p in targets})
                for path in paths:
                    locks.enter_context(resource_lock('file-write', path))
            yield

    def available_specs(self, ctx):
        return [s for s in self.callable_specs() if all(ctx.extras.get(k) is not None
                for k in self._requires.get(s.name, ())) and self.effect(s.name) is not ToolEffect.UNKNOWN]

    def authorize(self, name, args, ctx):
        """Preflight only. invoke rechecks under the write lock before dispatch."""
        try:
            value = self._prepare(name, args, ctx)
            return value if isinstance(value, ToolResult) else None
        except Exception as exc:
            return ToolResult(False, error=redact(str(exc)), meta={'authorization': 'deny',
                              'error_code': 'boundary_violation', 'execution_state': 'not_started'})

    # -- invocation ------------------------------------------------------
    def invoke(self, name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
        if args is not None and not isinstance(args, dict):
            return ToolResult(False, error='Tool arguments must be an object', meta={
                'authorization': 'deny', 'error_code': 'invalid_arguments',
                'execution_state': 'not_started', 'outcome': 'failed'})
        try:
            with self.write_guard(name, args, ctx):
                result = self._invoke(name, args, ctx)
            result.content = ctx.secret_scope.protect_text(result.content)
            result.error = redact(result.error)
            result.meta = redact(result.meta)
            result.meta.setdefault('outcome', 'unknown' if result.meta.get('execution_state') == 'unknown' else 'cancelled' if result.meta.get('error_code') == 'cancelled' else 'succeeded' if result.ok else 'failed')
            result.meta.setdefault('error_code', '' if result.ok else 'tool_rejected')
            result.meta.setdefault('execution_state', 'completed' if result.ok else 'not_started')
            return result
        except Exception as exc:
            return ToolResult(False, error=redact(f"{type(exc).__name__}: {exc}"),
                              meta={'authorization': 'unknown', 'error_code': 'result_processing_failed',
                                    'execution_state': 'unknown' if self.side_effects(name, args) else 'completed'})

    def _invoke(self, name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
        try:
            decision = self._prepare(name, args, ctx)
        except Exception as exc:
            return ToolResult(False, error=redact(str(exc)), meta={'authorization': 'deny',
                              'error_code': 'boundary_violation', 'execution_state': 'not_started'})
        if isinstance(decision, ToolResult):
            return decision
        spec = self._specs[name]
        # Authorization may have waited for host approval; cancellation can
        # arrive during that wait even though the initial check passed.
        if ctx.cancellation is not None and ctx.cancellation.is_set():
            return ToolResult(False, error='Tool cancelled before dispatch', meta={
                'authorization': decision.value, 'error_code': 'cancelled', 'execution_state': 'not_started'})
        try:
            result = spec.handler(args or {}, ctx)
        except Exception as exc:
            return ToolResult(False, error=f'{type(exc).__name__}: {exc}', meta={
                'authorization': decision.value, 'error_code': 'tool_error',
                'execution_state': 'unknown' if self.side_effects(name, args) else 'completed'})
        try:
            if (not isinstance(result, ToolResult) or type(result.ok) is not bool
                    or not isinstance(result.content, str) or not isinstance(result.error, str)
                    or result.meta is not None and not isinstance(result.meta, dict)):
                raise TypeError('Invalid ToolResult')
            # Validate before redaction/journaling, and detach handler-owned data.
            # Never stringify arbitrary objects: that can hide broken contracts
            # and expose private object representations in the journal.
            metadata = json.loads(json.dumps(result.meta or {}, allow_nan=False))
            result = ToolResult(result.ok, result.content, result.error, metadata)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return ToolResult(False, error='Tool returned an invalid or non-JSON result', meta={
                'authorization': decision.value, 'error_code': 'invalid_tool_result',
                'execution_state': 'unknown' if self.side_effects(name, args) else 'completed'})
        result.meta.setdefault('authorization', decision.value)
        result.meta.setdefault('execution_state', 'unknown' if not result.ok and self.side_effects(name, args) else 'completed')
        return result

    def _prepare(self, name: str, args: dict[str, Any] | None, ctx: ToolContext):
        if args is not None and not isinstance(args, dict):
            return ToolResult(False, error='Tool arguments must be an object', meta={
                'authorization': 'deny', 'error_code': 'invalid_arguments', 'execution_state': 'not_started'})
        if args and (args.get('_truncated') or '_raw' in args):
            return ToolResult(False, error='Tool arguments are incomplete or invalid; resend a complete JSON object',
                meta={'authorization': 'deny', 'error_code': 'invalid_arguments', 'execution_state': 'not_started'})
        if ctx.cancellation is not None and ctx.cancellation.is_set():
            return ToolResult(False, error='Tool cancelled before dispatch', meta={
                'authorization': 'deny', 'error_code': 'cancelled', 'execution_state': 'not_started'})
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(ok=False, error=f"unknown tool {name!r}", meta={'error_code': 'unknown_tool'})
        if self._hidden(name):
            return ToolResult(ok=False, error=f"tool {name!r} is not exposed in this session", meta={'error_code': 'tool_hidden'})
        if Path(ctx.workspace).resolve() != Path(ctx.policy.workspace).resolve():
            return ToolResult(False, error='Workspace and Policy disagree', meta={'authorization': 'deny', 'error_code': 'invalid_context'})
        if any(ctx.extras.get(k) is None for k in self._requires.get(name, ())):
            return ToolResult(False, error='Required host capability is not attached', meta={'authorization': 'deny', 'error_code': 'missing_capability'})
        if spec.is_deferred and name not in self._activated:
            return ToolResult(
                ok=False,
                error=f"tool {name!r} is deferred; call tool_search first to load it",
            )
        args = args or {}
        touching = [str(v) for k, v in args.items() if k in {"path", "file", "target", "root"} and v]
        # These operations have unrestricted filesystem/import/network access.
        # No command blacklist or stdout filter can make them secret-safe.
        if ctx.isolated and (name in {"shell_exec", "notebook_edit"} or "native" in spec.tags):
            return ToolResult(False, error="Native execution is unavailable in a protected agent; use mediated tools",
                              meta={"authorization": "deny", "boundary": "secret.isolation"})
        for target in touching:
            assert_public_path(ctx.policy.abs_path(target))
        blocks = args.get("patches") or args.get("edits") or []
        if isinstance(blocks, list):
            touching += [str(b['path']) for b in blocks if isinstance(b, dict) and b.get('path')]
        effect = self.effect(name, args)
        raw_decision = ctx.policy.evaluate(name, args=args, touching=touching, effect=effect)
        decision = ctx.policy.resolve_ask(raw_decision)
        if name == "edit_config" and args.get("patch") is not None and decision is Decision.ALLOW:
            decision = ctx.policy.resolve_ask(ctx.policy.evaluate("write_file", args=args, touching=touching))
        if decision is Decision.DENY:
            return ToolResult(ok=False, error=f"denied by policy: {name}",
                              meta={"decision": "deny", "authorization": decision.value,
                                    'raw_authorization': raw_decision.value,
                                    'error_code': 'approval_unavailable' if raw_decision is Decision.ASK else 'policy_denied'})
        if decision is Decision.ASK:
            return ToolResult(ok=False, error=f"approval required by policy: {name}",
                              meta={"decision": "ask", "authorization": decision.value,
                                    "requires_approval": True})
        for target in touching:
            path = ctx.policy.abs_path(target)
            assert_public_path(path)
            if ctx.isolated and self.writes(name, args):
                host = Path(__file__).resolve().parent
                canonical = path.resolve()
                control_roots = {Path.home() / '.forge', ctx.workspace / '.forge'}
                execution_roots = set()
                if ctx.extras.get('trusted_control_root'):
                    root = Path(ctx.extras['trusted_control_root']).resolve()
                    execution_roots.update(root / name for name in ('modules', 'plugins', 'bundles', 'policies', 'hooks',
                                                                    'sessions', 'checkpoints', 'cost'))
                    if root != ctx.workspace.resolve() and root.is_relative_to(ctx.workspace.resolve()):
                        control_roots.add(root)
                    if canonical.parent == root and canonical.name in {'user.json', 'config.json', 'forge.patch.json', 'secrets.json'}:
                        control_roots.add(canonical)
                for root in control_roots:
                    execution_roots.update(root / name for name in ('modules', 'plugins', 'bundles', 'policies', 'hooks'))
                control = any(canonical.is_relative_to(root.resolve()) for root in control_roots)
                if (canonical.is_relative_to(host) or canonical.is_relative_to(host.parent / 'forge-gui')
                        or canonical.is_relative_to(host.parent / 'vscode-extension')
                        or any(canonical.is_relative_to(root.resolve()) for root in execution_roots)
                        or control and name != 'edit_config'):
                    if name != 'edit_config' or args.get('patch') is not None:
                        return ToolResult(False, error="Protected agents cannot overwrite trusted Forge execution code",
                                          meta={"authorization": "deny"})
            if name in {"write_file", "edit_file", "apply_patch", "delete_file"} and path.is_file():
                raw = read_public_bytes(path, limit=8 * 1024 * 1024).decode('utf-8-sig')
                from .config_edit import sensitive_config
                if ctx.secret_scope.file_text(path) != raw or sensitive_config(path, raw):
                    return ToolResult(False, error="Protected configuration requires edit_config; raw rewrite denied",
                                      meta={"authorization": "deny"})
        if name in {"write_file", "edit_file", "apply_patch"}:
            if 'SECRET_REF' in json.dumps(args) or ctx.secret_scope.protect(args) != args:
                return ToolResult(False, error="Raw secrets and SecretRefs require the structured config validator")
        # 瘦身 B：授权裁决不再单独发 tool_decision 事件——写进返回值的
        # meta.authorization，由调用方（loop）合并进 tool_call 事件；审计面
        # 不变（每次授权仍可追溯），事件流少一遍 args 复制
        if '_expected_revisions' in args:
            import hashlib
            revisions = args['_expected_revisions']
            if not isinstance(revisions, dict) or len(revisions) > 40:
                return ToolResult(False, error='Invalid preview revision guard')
            resolved_targets = {str(ctx.policy.abs_path(t).resolve()) for t in touching}
            if set(revisions) != resolved_targets:
                return ToolResult(False, error='Preview revision targets do not match tool targets')
            for target, expected in revisions.items():
                path = Path(target)
                try:
                    raw = read_public_bytes(path, limit=512*1024) if path.exists() else None
                except (OSError, ValueError) as exc:
                    return ToolResult(False, error=f'Cannot validate preview revision: {exc}')
                actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
                if actual != expected:
                    return ToolResult(False, error='File changed since preview; regenerate the change',
                                      meta={'authorization': decision.value, 'error_code': 'revision_conflict'})
        return decision


def _safe(args: dict[str, Any], limit: int = 400) -> str:
    """Compact JSON preview of tool args (kept for fold previews and probes)."""
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _atomic_text(path: Path, content: str) -> None:
    """Prepare and fsync a replacement before touching the previous file."""
    import os
    import tempfile
    import stat
    path=path.resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    previous_mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,
                                         prefix='.forge-edit-',delete=False) as stream:
            temporary=Path(stream.name)
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        if previous_mode is not None: os.chmod(temporary,previous_mode)
        os.replace(temporary,path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# built-in tool set
# ---------------------------------------------------------------------------

def build_builtin_registry(
    *,
    expose: Iterable[str] | None = None,
    hide: Iterable[str] = (),
    shell_timeout: int = 60,
) -> ToolRegistry:
    reg = ToolRegistry(expose=expose, hide=hide)
    from functools import partial
    host_tool = partial(reg.tool, trusted=True)

    @host_tool("edit_config", "Inspect a protected JSON/YAML/TOML/.env configuration, then apply a structured "
              "patch with its revision. Credentials remain opaque and unchanged.",
              effect=ToolEffect.CONFIG,
              schema={"path": "string", "revision": "string", "patch": "array"})
    def config_editor(args, ctx):
        from .config_edit import edit_config
        return edit_config(args, ctx)

    @host_tool("read_file", "Read a UTF-8 text file from disk.", schema={"path": "string"})
    def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        limit = int(args.get("limit", 400))
        lines = ctx.secret_scope.file_text(path).splitlines()
        body = "\n".join(lines[:limit])
        # 瘦身 A：行截断之外再加字符帽，超长内容在 handler 层就被截断，
        # 不再整块进入返回链/事件流/上下文回填
        char_truncated = False
        max_chars = 6000
        if len(body) > max_chars:
            body = body[:max_chars]
            char_truncated = True
        return ToolResult(ok=True, content=body,
                          meta={"lines": len(lines),
                                "truncated": len(lines) > limit or char_truncated,
                                "char_truncated": char_truncated})

    @host_tool("list_dir", "List entries of a directory.", schema={"path": "string"})
    def list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", "."))
        if not path.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return ToolResult(ok=True, content="\n".join(entries[:200]))

    @host_tool("grep", "Search a regex over files under a root.", schema={"pattern": "string", "root": "string"})
    def grep(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        import re

        # 排除缓存/版本控制目录与二进制产物：pyc 里的字符串常量会命中任意
        # 源码 pattern（2026-09-23 实测 T4 结果四行全是 .pyc 假命中，把真
        # 正的源码命中挤出 100 条窗口）。
        skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
        skip_suffixes = {".pyc", ".pyo", ".pyd"}
        pattern = re.compile(str(args.get("pattern", "")))
        root = _resolve(ctx.workspace, args.get("root", "."))
        hits: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix in skip_suffixes:
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            try:
                assert_public_path(path)
                if not ctx.policy.allows_read(path):
                    continue
            except (PermissionError, OSError):
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
            except OSError:
                continue
            try:
                for number, line in enumerate(ctx.secret_scope.file_text(path).splitlines(), 1):
                    if pattern.search(line):
                        # root 可指向 workspace 外（绝对路径）时 relative_to 会抛
                        # ValueError 让整个工具崩掉（实测 2 次）；越界回退绝对路径。
                        try:
                            shown: Any = path.relative_to(ctx.workspace)
                        except ValueError:
                            shown = path
                        hits.append(f"{shown}:{number}: {line.strip()[:160]}")
                        if len(hits) >= 100:
                            raise StopIteration
            except StopIteration:
                break
            except (UnicodeError, OSError):
                continue
        return ToolResult(ok=True, content="\n".join(hits))

    @host_tool(
        "write_file",
        "Write UTF-8 text to a file (append mode optional). Gated by the sandbox and permission mode.",
        read_only=False,
        schema={"path": "string", "content": "string", "append": "boolean"},
    )
    def write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        if args.get("append"):
            previous=path.read_text(encoding='utf-8') if path.exists() else ''
            _atomic_text(path,previous+content)
            return ToolResult(ok=True, content=f"appended {len(content)} bytes to {path}")
        _atomic_text(path,content)
        return ToolResult(ok=True, content=f"wrote {len(content)} bytes to {path}")

    @host_tool(
        "delete_file",
        "Delete a file inside the workspace (sandbox-gated, audit-friendly "
        "alternative to shell rm). Refuses directories.",
        read_only=False,
        schema={"path": "string"},
    )
    def delete_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        if path.is_dir():
            return ToolResult(ok=False, error=f"is a directory (use shell for dirs): {path}")
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        path.unlink()
        return ToolResult(ok=True, content=f"deleted {path}")

    @host_tool(
        "edit_file",
        "Replace an exact anchor (old -> new) inside a UTF-8 file, or overwrite a "
        "1-indexed inclusive line range (start defaults to 1, end to the last line). "
        "Refuses an absent or ambiguous anchor unless replace_all; pass either 'old' "
        "or a line range, not both.",
        read_only=False,
        schema={"path": "string", "old": "string", "new": "string",
                "replace_all": "boolean", "start_line": "integer", "end_line": "integer"},
    )
    def edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        original = path.read_text(encoding="utf-8", errors="replace")
        new = str(args.get("new", ""))
        old = str(args.get("old", ""))
        start_line, end_line = args.get("start_line"), args.get("end_line")
        has_range = start_line is not None or end_line is not None
        if has_range and old:
            return ToolResult(ok=False,
                              error="edit_file: pass either 'old' (anchor) or start_line/end_line (range), not both")
        if has_range:
            def _line_no(value: Any) -> int | None:
                if value is None or isinstance(value, bool):
                    return None
                if isinstance(value, float) and not value.is_integer():
                    return None
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            s = _line_no(start_line) if start_line is not None else 1
            e = _line_no(end_line) if end_line is not None else None
            if s is None or (end_line is not None and e is None):
                return ToolResult(ok=False, error="edit_file: start_line/end_line must be integers")
            lines = original.splitlines(keepends=True)
            if e is None:
                e = len(lines)
            if s < 1 or e < s or e > len(lines):
                return ToolResult(ok=False, error=f"line range {s}-{e} out of bounds (file has {len(lines)} lines)")
            body = new if (not new or new.endswith("\n")) else new + "\n"
            updated = "".join(lines[: s - 1]) + body + "".join(lines[e:])
            _atomic_text(path,updated)
            return ToolResult(ok=True, content=f"replaced lines {s}-{e} of {path}", meta={"lines": len(lines)})
        if not old:
            return ToolResult(ok=False, error="edit_file needs 'old' (or start_line/end_line)")
        count = original.count(old)
        if count == 0:
            return ToolResult(ok=False, error=f"anchor not found in {path.name}")
        replace_all = bool(args.get("replace_all"))
        if count > 1 and not replace_all:
            return ToolResult(ok=False,
                              error=f"anchor is ambiguous in {path.name} ({count} matches); "
                                    f"pass replace_all or add surrounding context")
        updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
        _atomic_text(path,updated)
        return ToolResult(ok=True, content=f"edited {path} ({count if replace_all else 1} replacement(s))",
                          meta={"matches": count})

    @host_tool(
        "apply_patch",
        "Apply a list of {path, old, new} edits ATOMICALLY: every block is validated "
        "first, then every file is written (per-file atomic replace); a failed "
        "validation leaves all files untouched.",
        read_only=False,
        schema={"patches": "array"},
    )
    def apply_patch(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        blocks = args.get("patches") or args.get("edits") or []
        if not isinstance(blocks, list) or not blocks:
            return ToolResult(ok=False, error="apply_patch needs a non-empty 'patches' list of {path, old, new}")
        import os as _os
        import tempfile as _tempfile

        buffers: dict[Path, str] = {}
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                return ToolResult(ok=False, error=f"patch block {i} is not an object")
            path = _resolve(ctx.workspace, block.get("path", "")).resolve()
            # defence-in-depth: handler-side sandbox assert. The policy layer also
            # collects patches[].path, but a write tool must not trust one layer alone.
            if not ctx.policy.allows_write(path):
                return ToolResult(ok=False, error=f"patch {i}: path escapes sandbox: {path}")
            current = buffers.get(path)
            if current is None:
                if not path.is_file():
                    return ToolResult(ok=False, error=f"patch {i}: not a file: {path}")
                current = path.read_text(encoding="utf-8", errors="replace")
            old = str(block.get("old", ""))
            if not old:
                return ToolResult(ok=False, error=f"patch {i}: empty 'old' anchor")
            count = current.count(old)
            if count == 0:
                return ToolResult(ok=False, error=f"patch {i}: anchor not found in {path.name}")
            if count > 1 and not block.get("replace_all"):
                return ToolResult(ok=False, error=f"patch {i}: anchor ambiguous in {path.name} ({count} matches)")
            new = str(block.get("new", ""))
            buffers[path] = current.replace(old, new) if block.get("replace_all") else current.replace(old, new, 1)
        committed: list[str] = []
        try:
            for path, text in buffers.items():  # all validated above -> one unit, per-file atomic replace
                handle, tmp_name = _tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
                try:
                    with _os.fdopen(handle, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    try:
                        _os.chmod(tmp_name, _os.stat(path).st_mode & 0o7777)
                    except OSError:
                        pass
                    _os.replace(tmp_name, path)
                except BaseException:
                    try:
                        _os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
                committed.append(str(path))
        except OSError as exc:
            return ToolResult(ok=False,
                              error=f"apply_patch: commit failed after {len(committed)} file(s): {exc}",
                              meta={"patches": len(blocks), "files": len(buffers), "committed": committed})
        return ToolResult(ok=True,
                          content=f"applied {len(blocks)} patch(es) across {len(buffers)} file(s)",
                          meta={"patches": len(blocks), "files": len(buffers), "committed": committed})

    @host_tool(
        "read_range",
        "Read a 1-indexed line range from a UTF-8 file (cheaper than a whole-file read).",
        schema={"path": "string", "start": "integer", "end": "integer"},
    )
    def read_range(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        lines = ctx.secret_scope.file_text(path).splitlines()
        s = max(1, int(args.get("start", 1)))
        e = min(len(lines), int(args.get("end", len(lines))))
        if not lines or s > e:
            return ToolResult(ok=False, error=(
                f"range {s}-{e} is out of bounds: file has {len(lines)} lines; "
                f"valid range is 1-{len(lines)}"))
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(s, e + 1))
        return ToolResult(ok=True, content=body, meta={"lines": len(lines), "start": s, "end": e})

    @host_tool(
        "file_outline",
        "List top-level symbols (def/class signatures) of a source file without reading it whole.",
        schema={"path": "string"},
    )
    def file_outline(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        import re as _re

        path = _resolve(ctx.workspace, args.get("path", ""))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        pattern = _re.compile(r"^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
        out: list[str] = []
        truncated = False
        for number, line in enumerate(ctx.secret_scope.file_text(path).splitlines(), 1):
            match = pattern.match(line)
            if match:
                indent = len(match.group(1).replace("\t", "    ")) // 4
                out.append(f"{number}\t{'  ' * indent}{match.group(2)} {match.group(3)}")
                if len(out) >= 200:
                    truncated = True
                    break
        return ToolResult(ok=True, content="\n".join(out) or "(no symbols found)",
                          meta={"symbols": len(out), "truncated": truncated})

    @host_tool(
        "shell_exec",
        "Run a shell command. Denied outright for OS-escape programs.",
        read_only=False,
        schema={"command": "string"},
    )
    def shell_exec(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", ""))
        proc = subprocess.run(  # noqa: S602 - policy already gated this
            command,
            shell=True,
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout", shell_timeout)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(ok=proc.returncode == 0, content=out[-4000:], meta={"exit": proc.returncode})

    @host_tool(
        "tool_search",
        "Discover deferred tools by keyword and load them for this session.",
        no_defer=True,
        schema={"query": "string"},
    )
    def tool_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        registry = ctx.extras.get("registry")
        found = registry.search(str(args.get("query", "")))
        lines = [f"{s.name}: {s.description}" for s in found] or ["no matching tools"]
        return ToolResult(ok=True, content="\n".join(lines), meta={"activated": [s.name for s in found]})

    @host_tool("skill_list", "List installed capabilities (skills/plugins).", no_defer=True, requires=("capabilities",))
    def skill_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        lib = ctx.extras.get("capabilities")
        if lib is None:
            return ToolResult(ok=True, content="(no capability library attached)")
        lines = []
        for cap in lib.list():
            flag = "trusted" if cap.trusted else "UNTRUSTED"
            lines.append(f"{cap.name} [{cap.kind}] v{cap.version} {flag} — {cap.description}")
        return ToolResult(ok=True, content="\n".join(lines) or "(empty)")

    @host_tool(
        "spawn_subagent",
        "Spawn a nested agent with its own context window and a permission ceiling.",
        read_only=False, effect=ToolEffect.CONTROL, requires=("spawn",),
        schema={"task": "string", "mode": "string"},
    )
    def spawn_subagent(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.extras.get("spawn")
        if runner is None:
            return ToolResult(ok=False, error="subagent runtime not attached")
        return runner(str(args.get("task", "")), str(args.get("mode", "")))

    @host_tool("memory_recall", "Recall typed long-term memory entries.", no_defer=True, requires=("memory",))
    def memory_recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = ctx.extras.get("memory")
        if store is None:
            return ToolResult(ok=True, content="(memory store not attached)")
        kind = args.get("kind")
        entries = store.recall(kind=kind)
        return ToolResult(ok=True, content="\n".join(f"- [{e.kind}] {e.text}" for e in entries) or "(empty)")

    # ---- cross-framework aligned tools (schema-compatible with Always) ----
    # web_search / fetch_url 参数与 ai-platform tools.ts 的 web_search /
    # fetch_webpage 逐字段对齐——模型在两个框架间切换零成本。

    @host_tool(
        "web_search",
        "Search the web (Bing + Baidu aggregated, Sogou/Yahoo fallback). "
        "site: all/zhihu/xiaohongshu/baidu/bing/weixin.",
        deferred=True,
        tags=("web", "search", "network"),
        schema={"query": "string", "num": "integer", "site": "string"},
    )
    def web_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="web_search needs a non-empty 'query'")
        num = min(int(args.get("num", 5) or 5), 10)
        site = str(args.get("site", "all") or "all")
        base = ctx.extras.get("web_search")
        if base is None:
            return ToolResult(ok=False,
                              error="web_search backend not attached; set extras['web_search']")
        try:
            results = base(query=query, num=num, site=site)
        except Exception as exc:
            return ToolResult(ok=False, error=f"web_search failed: {exc}")
        if not results:
            return ToolResult(ok=True, content="(no results)")
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title','')}")
            if r.get("url"):
                lines.append(f"   {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet'][:200]}")
        return ToolResult(ok=True, content="\n".join(lines), meta={"count": len(results)})

    @host_tool(
        "fetch_url",
        "Fetch a URL and extract readable text (HTML tags stripped). "
        "Aligned with Always fetch_webpage (url + max_chars).",
        deferred=True,
        tags=("web", "fetch", "network"),
        schema={"url": "string", "max_chars": "integer"},
    )
    def fetch_url(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(ok=False, error="fetch_url needs an http(s) URL")
        max_chars = min(int(args.get("max_chars", 5000) or 5000), 50000)
        fetcher = ctx.extras.get("fetch_url")
        if fetcher is None:
            return ToolResult(ok=False,
                              error="fetch_url backend not attached; set extras['fetch_url']")
        try:
            title, body = fetcher(url=url, max_chars=max_chars)
        except Exception as exc:
            return ToolResult(ok=False, error=f"fetch_url failed: {exc}")
        head = f"# {title}\n\n" if title else ""
        return ToolResult(ok=True, content=(head + body)[:max_chars],
                          meta={"url": url, "chars": len(head + body)})

    @host_tool(
        "datetime",
        "Current local time, timezone conversion, or duration between two times. "
        "Aligned with Always datetime tool.",
        no_defer=True,
        tags=("time", "utility"),
        schema={"action": "string", "value": "string", "to_tz": "string", "other": "string"},
    )
    def datetime_tool(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from datetime import datetime as _dt, timezone as _tz

        action = str(args.get("action", "now") or "now")
        try:
            if action == "now":
                now = _dt.now().astimezone()
                return ToolResult(ok=True, content=now.strftime("%Y-%m-%d %H:%M:%S %z (%A)"))
            if action == "convert":
                raw = str(args.get("value", ""))
                to_tz = str(args.get("to_tz", "UTC"))
                from zoneinfo import ZoneInfo
                dt = _dt.fromisoformat(raw).replace(tzinfo=_tz.utc) if "+" not in raw and "Z" not in raw.upper() else _dt.fromisoformat(raw)
                converted = dt.astimezone(ZoneInfo(to_tz))
                return ToolResult(ok=True, content=converted.strftime("%Y-%m-%d %H:%M:%S %z"))
            if action == "diff":
                a = _dt.fromisoformat(str(args.get("value", "")))
                b = _dt.fromisoformat(str(args.get("other", "")))
                delta = abs(b - a)
                days = delta.days
                hours, rem = divmod(delta.seconds, 3600)
                minutes = rem // 60
                return ToolResult(ok=True,
                                  content=f"{days} days {hours} hours {minutes} minutes")
            return ToolResult(ok=False, error=f"unknown action: {action} (now/convert/diff)")
        except Exception as exc:
            return ToolResult(ok=False, error=f"datetime failed: {exc}")

    @host_tool(
        "calculator",
        "Safe math expression evaluator (recursive descent, no eval). "
        "Aligned with Always calculator.",
        no_defer=True,
        tags=("math", "utility"),
        schema={"expression": "string"},
    )
    def calculator(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        expr = str(args.get("expression", "")).strip()
        if not expr:
            return ToolResult(ok=False, error="calculator needs an 'expression'")
        import math as _math

        def _tokenize(s: str) -> list[str]:
            tokens: list[str] = []
            i = 0
            while i < len(s):
                c = s[i]
                if c.isspace():
                    i += 1
                    continue
                if c.isdigit() or (c == "." and i + 1 < len(s) and s[i + 1].isdigit()):
                    j = i
                    while j < len(s) and (s[j].isdigit() or s[j] == "."):
                        j += 1
                    tokens.append(s[i:j])
                    i = j
                elif c.isalpha():
                    j = i
                    while j < len(s) and (s[j].isalnum() or s[j] == "_"):
                        j += 1
                    tokens.append(s[i:j])
                    i = j
                else:
                    tokens.append(c)
                    i += 1
            return tokens

        funcs = {
            "sin": _math.sin, "cos": _math.cos, "tan": _math.tan,
            "sqrt": _math.sqrt, "abs": abs, "log": _math.log,
            "log10": _math.log10, "exp": _math.exp, "floor": _math.floor,
            "ceil": _math.ceil, "round": round,
        }
        consts = {"pi": _math.pi, "e": _math.e}
        tokens = _tokenize(expr)

        def parse_expr(pos: int) -> tuple[float, int]:
            left, pos = parse_term(pos)
            while pos < len(tokens) and tokens[pos] in ("+", "-"):
                op = tokens[pos]
                right, pos = parse_term(pos + 1)
                left = left + right if op == "+" else left - right
            return left, pos

        def parse_term(pos: int) -> tuple[float, int]:
            left, pos = parse_factor(pos)
            while pos < len(tokens) and tokens[pos] in ("*", "/", "%"):
                op = tokens[pos]
                right, pos = parse_factor(pos + 1)
                if op == "*":
                    left *= right
                elif op == "/":
                    if right == 0:
                        raise ZeroDivisionError("division by zero")
                    left /= right
                else:
                    left %= right
            return left, pos

        def parse_factor(pos: int) -> tuple[float, int]:
            base, pos = parse_unary(pos)
            if pos < len(tokens) and tokens[pos] == "^":
                exp, pos = parse_factor(pos + 1)
                return base ** exp, pos
            return base, pos

        def parse_unary(pos: int) -> tuple[float, int]:
            if pos < len(tokens) and tokens[pos] == "-":
                val, pos = parse_unary(pos + 1)
                return -val, pos
            return parse_atom(pos)

        def parse_atom(pos: int) -> tuple[float, int]:
            tok = tokens[pos]
            if tok == "(":
                val, pos = parse_expr(pos + 1)
                if pos >= len(tokens) or tokens[pos] != ")":
                    raise ValueError("unbalanced parentheses")
                return val, pos + 1
            if tok in consts:
                return consts[tok], pos + 1
            if tok in funcs:
                if pos + 1 < len(tokens) and tokens[pos + 1] == "(":
                    val, pos = parse_expr(pos + 2)
                    if pos >= len(tokens) or tokens[pos] != ")":
                        raise ValueError("unbalanced parentheses")
                    return funcs[tok](val), pos + 1
                raise ValueError(f"function {tok} needs parentheses")
            try:
                return float(tok), pos + 1
            except ValueError:
                raise ValueError(f"unexpected token: {tok}")

        try:
            value, end = parse_expr(0)
            if end != len(tokens):
                raise ValueError(f"trailing tokens from {tokens[end:]}")
            if value == int(value):
                shown = str(int(value))
            else:
                shown = repr(value)
            return ToolResult(ok=True, content=f"{expr} = {shown}", meta={"value": value})
        except Exception as exc:
            return ToolResult(ok=False, error=f"calculator failed: {exc}")

    return reg


def _resolve(root: Path, raw: str) -> Path:
    path = Path(str(raw or "."))
    return path if path.is_absolute() else (root / path)


__all__ = [
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_builtin_registry",
]
