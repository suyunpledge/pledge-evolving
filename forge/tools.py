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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .policy import Decision, Policy


class ToolError(RuntimeError):
    pass


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "error": self.error, "meta": self.meta}


@dataclass
class ToolContext:
    policy: Policy
    workspace: Path
    session: Any | None = None
    emit: Callable[..., None] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def fire(self, **event: Any) -> None:
        if self.emit is not None:
            self.emit(**event)


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

    # -- registration ----------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

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
            ))
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
        return out

    # -- invocation ------------------------------------------------------
    def invoke(self, name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(ok=False, error=f"unknown tool {name!r}")
        if self._hidden(name):
            return ToolResult(ok=False, error=f"tool {name!r} is not exposed in this session")
        if spec.is_deferred and name not in self._activated:
            return ToolResult(
                ok=False,
                error=f"tool {name!r} is deferred; call tool_search first to load it",
            )
        args = args or {}
        touching = [str(v) for k, v in args.items() if k in {"path", "file", "target"} and v]
        decision = ctx.policy.resolve_ask(ctx.policy.evaluate(name, args=args, touching=touching))
        # 瘦身 B：授权裁决不再单独发 tool_decision 事件——写进返回值的
        # meta.authorization，由调用方（loop）合并进 tool_call 事件；审计面
        # 不变（每次授权仍可追溯），事件流少一遍 args 复制
        if decision is Decision.DENY:
            return ToolResult(ok=False, error=f"denied by policy: {name}",
                              meta={"decision": "deny", "authorization": decision.value})
        try:
            result = spec.handler(args, ctx)
        except Exception as exc:  # tool errors must never kill the loop
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              meta={"authorization": decision.value})
        if result.meta is None:
            result.meta = {}
        result.meta.setdefault("authorization", decision.value)
        return result


def _safe(args: dict[str, Any], limit: int = 400) -> str:
    """Compact JSON preview of tool args (kept for fold previews and probes)."""
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


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

    @reg.tool("read_file", "Read a UTF-8 text file from disk.", schema={"path": "string"})
    def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        limit = int(args.get("limit", 400))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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

    @reg.tool("list_dir", "List entries of a directory.", schema={"path": "string"})
    def list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", "."))
        if not path.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return ToolResult(ok=True, content="\n".join(entries[:200]))

    @reg.tool("grep", "Search a regex over files under a root.", schema={"pattern": "string", "root": "string"})
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
                if path.stat().st_size > 512_000:
                    continue
            except OSError:
                continue
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
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
        return ToolResult(ok=True, content="\n".join(hits))

    @reg.tool(
        "write_file",
        "Write UTF-8 text to a file. Gated by the sandbox and permission mode.",
        read_only=False,
        schema={"path": "string", "content": "string"},
    )
    def write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        return ToolResult(ok=True, content=f"wrote {len(str(args.get('content','')))} bytes to {path}")

    @reg.tool(
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
            path.write_text(updated, encoding="utf-8")
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
        path.write_text(updated, encoding="utf-8")
        return ToolResult(ok=True, content=f"edited {path} ({count if replace_all else 1} replacement(s))",
                          meta={"matches": count})

    @reg.tool(
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
            if not ctx.policy.sandbox.allows_write(path, ctx.policy.workspace):
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

    @reg.tool(
        "read_range",
        "Read a 1-indexed line range from a UTF-8 file (cheaper than a whole-file read).",
        schema={"path": "string", "start": "integer", "end": "integer"},
    )
    def read_range(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(ctx.workspace, args.get("path", ""))
        if not path.is_file():
            return ToolResult(ok=False, error=f"not a file: {path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(1, int(args.get("start", 1)))
        e = min(len(lines), int(args.get("end", len(lines))))
        if not lines or s > e:
            return ToolResult(ok=False, error=(
                f"range {s}-{e} is out of bounds: file has {len(lines)} lines; "
                f"valid range is 1-{len(lines)}"))
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(s, e + 1))
        return ToolResult(ok=True, content=body, meta={"lines": len(lines), "start": s, "end": e})

    @reg.tool(
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
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            match = pattern.match(line)
            if match:
                indent = len(match.group(1).replace("\t", "    ")) // 4
                out.append(f"{number}\t{'  ' * indent}{match.group(2)} {match.group(3)}")
                if len(out) >= 200:
                    truncated = True
                    break
        return ToolResult(ok=True, content="\n".join(out) or "(no symbols found)",
                          meta={"symbols": len(out), "truncated": truncated})

    @reg.tool(
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
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return ToolResult(ok=proc.returncode == 0, content=out[-4000:], meta={"exit": proc.returncode})

    @reg.tool(
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

    @reg.tool("skill_list", "List installed capabilities (skills/plugins).", no_defer=True)
    def skill_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        lib = ctx.extras.get("capabilities")
        if lib is None:
            return ToolResult(ok=True, content="(no capability library attached)")
        lines = []
        for cap in lib.list():
            flag = "trusted" if cap.trusted else "UNTRUSTED"
            lines.append(f"{cap.name} [{cap.kind}] v{cap.version} {flag} — {cap.description}")
        return ToolResult(ok=True, content="\n".join(lines) or "(empty)")

    @reg.tool(
        "spawn_subagent",
        "Spawn a nested agent with its own context window and a permission ceiling.",
        read_only=False,
        schema={"task": "string", "mode": "string"},
    )
    def spawn_subagent(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        runner = ctx.extras.get("spawn")
        if runner is None:
            return ToolResult(ok=False, error="subagent runtime not attached")
        return runner(str(args.get("task", "")), str(args.get("mode", "")))

    @reg.tool("memory_recall", "Recall typed long-term memory entries.", no_defer=True)
    def memory_recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = ctx.extras.get("memory")
        if store is None:
            return ToolResult(ok=True, content="(memory store not attached)")
        kind = args.get("kind")
        entries = store.recall(kind=kind)
        return ToolResult(ok=True, content="\n".join(f"- [{e.kind}] {e.text}" for e in entries) or "(empty)")

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
