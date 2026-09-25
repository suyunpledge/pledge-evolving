"""Permission policy: two-dimensional model + sandbox tiers.

Design borrowed from WorkBuddy/CodeBuddy (mode baseline + allow/ask/deny
exceptions, ``deny`` always wins) and Codex (three sandbox tiers bound to an
approval path, command-level rules).

Evaluation order (highest first):

    explicit deny  >  explicit ask  >  bypass  >  mode baseline

Children never escape their parent: a subagent mode is clamped to the parent's
ceiling, so ``auto``/``dontAsk`` parents cannot be out-permissioned by a child.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable


class Mode(str, Enum):
    READ_ONLY = "read-only"
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    AUTO = "auto"
    DONT_ASK = "dontAsk"
    PLAN = "plan"
    BYPASS = "bypassPermissions"

    @property
    def rank(self) -> int:
        """How much the mode may do on its own. Used for ceiling clamping."""
        return {
            Mode.READ_ONLY: 0,
            Mode.PLAN: 1,
            Mode.DEFAULT: 2,
            Mode.ACCEPT_EDITS: 3,
            Mode.AUTO: 4,
            Mode.DONT_ASK: 5,
            Mode.BYPASS: 6,
        }[self]

    def clamp_to(self, ceiling: "Mode") -> "Mode":
        return self if self.rank <= ceiling.rank else ceiling


class Sandbox(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    FULL_ACCESS = "danger-full-access"

    def allows_write(self, path: Path, root: Path) -> bool:
        if self is Sandbox.FULL_ACCESS:
            return True
        if self is Sandbox.READ_ONLY:
            return False
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"

    def __bool__(self) -> bool:  # truthy only when allowed
        return self is Decision.ALLOW


# Programs that hand control back to the OS and therefore bypass any
# path-based sandbox. Hard-denied before the shell ever sees them.
DEFAULT_FORBIDDEN_PROGRAMS = (
    "wsl",
    "wslconfig",
    "wmic",
    "sc",
    "reg",
    "schtasks",
    "bcdedit",
    "takeown",
    "icacls",
    "netsh",
)

# Destructive-command floor. Design: deny *combined* recursive+force forms
# (single token, split tokens, long options) and recursive hits on roots.
# Single flags (rm -f x / rm -r x / del /s x) stay allowed on purpose.
# Lookaheads are line-scoped ([^\n;|&]*) so a deny must come from one command,
# not from tokens joined across ; | & or newlines.
DEFAULT_DENY_PATTERNS = (
    r"rm\s+-rf?\s+/(?:\s|$)",
    r"rm\s+-rf?\s+[a-z]:\\?\s*$",
    r"rm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*\b",
    # rm with recursive + force in ANY token layout: -r -f, -f -r, mixed
    # short/long, extra flags in between. Long options are matched exactly
    # (--recursive/--force) so --verbose/-f stays allowed.
    r"\brm\b(?=(?:[^\n;|&]*\s)(?:-[a-z]*r[a-z]*|--recursive\b))"
    r"(?=(?:[^\n;|&]*\s)(?:-[a-z]*f[a-z]*|--force\b))",
    # rm recursive aimed at a root: bare "/", doubled slashes, quoted root,
    # root glob "/*", drive roots in any spelling (back/forward slash, quoted)
    r"\brm\b(?=(?:[^\n;|&]*\s)(?:-[a-z]*r[a-z]*|--recursive\b))"
    r"(?=(?:[^\n;|&]*\s)(?:"
    r"/{2,}(?:\s|$)|/(?=\s|$)|/\*(?:\s|$)"
    r"|['\"]/(?:['\"])(?:\s|$)"
    r"|['\"][a-z]:[\\/]?(?:['\"])(?:\s|$)"
    r"|[a-z]:\\?(?:/|\s|$)"
    r"))",
    r"\b(?:rd|rmdir)\b(?=[^\n;|&]*/s\b)(?=[^\n;|&]*/q\b)",
    r"\b(?:del|erase)\b(?=[^\n;|&]*/s\b)(?=[^\n;|&]*/q\b)",
    r"format\s+[a-z]:",
    r":\(\)\s*\{.*\};\s*:",          # fork bomb
    r"powershell(?:\.exe)?\s+.*-enc(?:odedcommand)?\b",
    r"curl\b.*\|\s*(?:ba)?sh\b",
    r"iwr\b.*\|\s*iex\b",
    # Remove-Item (and ri alias): recurse-ish + force-ish params in any order,
    # incl. PowerShell prefix abbreviations (-r, -rec, -fo, ...)
    r"\b(?:remove-item|ri)\b(?=[^\n;|&]*\s-fo[a-z]*\b)(?=[^\n;|&]*\s-r[a-z]*\b)",
    r"rmtree\b",
)

WRITE_TOOLS = {"write_file", "edit_file", "apply_patch", "shell_exec", "notebook_edit", "delete_file"}


@dataclass(frozen=True)
class Policy:
    """Frozen on purpose.

    A policy that can be mutated in place is a privilege-escalation channel: any
    code holding the reference (a tool, a plugin, a spawned helper) could flip
    its own mode. Escalation now has to go through a new object, which the
    registry never hands out.
    """

    mode: Mode = Mode.DEFAULT
    sandbox: Sandbox = Sandbox.WORKSPACE_WRITE
    workspace: Path = field(default_factory=lambda: Path.cwd())
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    forbidden_programs: tuple[str, ...] = DEFAULT_FORBIDDEN_PROGRAMS
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS
    non_interactive: bool = False

    # -- construction ----------------------------------------------------
    @classmethod
    def from_config(cls, cfg, *, workspace: Path | None = None, non_interactive: bool = True) -> "Policy":
        mode = Mode(str(cfg.get("policy", "mode", Mode.DEFAULT.value)))
        sandbox = Sandbox(str(cfg.get("policy", "sandbox", Sandbox.WORKSPACE_WRITE.value)))
        return cls(
            mode=mode,
            sandbox=sandbox,
            workspace=Path(workspace or cfg.get("policy", "workspace", str(Path.cwd()))),
            allow=tuple(cfg.get("policy", "allow", ()) or ()),
            ask=tuple(cfg.get("policy", "ask", ()) or ()),
            deny=tuple(cfg.get("policy", "deny", ()) or ()),
            non_interactive=non_interactive,
        )

    def child(self, mode: Mode | None = None) -> "Policy":
        """A subagent policy: same rules, mode clamped to this ceiling."""
        ceiling = self.mode if self.mode.rank < Mode.BYPASS.rank else Mode.BYPASS
        return Policy(
            mode=(mode or self.mode).clamp_to(ceiling),
            sandbox=self.sandbox,
            workspace=self.workspace,
            allow=self.allow,
            ask=self.ask,
            deny=self.deny,
            forbidden_programs=self.forbidden_programs,
            deny_patterns=self.deny_patterns,
            non_interactive=self.non_interactive,
        )

    # -- evaluation ------------------------------------------------------
    def evaluate(
        self,
        tool: str,
        *,
        args: dict | None = None,
        touching: Iterable[str] = (),
    ) -> Decision:
        args = args or {}

        # 1. explicit denies (tool rules and content patterns) always win
        if self._matches(self.deny, tool):
            return Decision.DENY
        command = str(args.get("command") or "")
        if command and self._command_denied(command):
            return Decision.DENY

        # 2. sandbox: writes outside the granted roots (only write tools move data)
        if tool in WRITE_TOOLS:
            # Collect all candidate paths from every source so container tools
            # like apply_patch (patches[].path / edits[].path) are not silently
            # skipped when the top-level 'path' key is absent.
            extra: list[str] = []
            patches_arg = args.get("patches") or args.get("edits") or []
            if isinstance(patches_arg, list):
                for _b in patches_arg:
                    if isinstance(_b, dict):
                        _p = _b.get("path")
                        if _p:
                            extra.append(str(_p))
            targets = list(touching) + extra
            if not targets:
                targets = [str(v) for k, v in args.items()
                           if k in {"path", "file", "target"} and v]
            for target in targets:
                if not self.sandbox.allows_write(self.abs_path(target), self.workspace):
                    return Decision.DENY
        if tool == "shell_exec" and self.sandbox is Sandbox.READ_ONLY:
            return Decision.DENY

        # 3. explicit asks
        if self._matches(self.ask, tool):
            return Decision.ALLOW if self.mode is Mode.BYPASS else Decision.ASK

        # 4. explicit grants: an allow rule lifts the mode baseline (but never a deny)
        if self._matches(self.allow, tool):
            return Decision.ALLOW

        # 5. mode baseline
        return self._baseline(tool)

    def abs_path(self, target: str | Path) -> Path:
        """Resolve a tool argument against the workspace, not the process cwd."""
        path = Path(str(target))
        return path if path.is_absolute() else (self.workspace / path)

    def _baseline(self, tool: str) -> Decision:
        writes = tool in WRITE_TOOLS
        if self.mode is Mode.BYPASS:
            return Decision.ALLOW
        if self.mode is Mode.READ_ONLY:
            return Decision.DENY if writes else Decision.ALLOW
        if self.mode is Mode.PLAN:
            # plan mode is not read-only: it may write its own plan artefact
            return Decision.ALLOW if tool in {"read_file", "list_dir", "tool_search",
                                              "grep", "skill_list"} else Decision.DENY
        if self.mode is Mode.ACCEPT_EDITS:
            return Decision.ALLOW if tool != "shell_exec" else Decision.ASK
        if self.mode is Mode.DEFAULT:
            return Decision.ALLOW if not writes else Decision.ASK
        if self.mode is Mode.AUTO:
            return Decision.ALLOW if not writes else Decision.ASK
        if self.mode is Mode.DONT_ASK:
            return Decision.ALLOW if not writes else Decision.DENY
        return Decision.ASK

    def resolve_ask(self, decision: Decision) -> Decision:
        """Headless runs have nobody to ask; ask collapses to deny."""
        if decision is Decision.ASK and self.non_interactive:
            return Decision.DENY
        return decision

    @staticmethod
    def _matches(rules: Iterable[str], tool: str) -> bool:
        for rule in rules:
            rule = rule.strip()
            if not rule:
                continue
            if rule == "*" or rule == tool:
                return True
            if rule.endswith(":*") and tool.startswith(rule[:-1]):
                return True
            if rule.endswith("*") and tool.startswith(rule[:-1]):
                return True
        return False

    def _command_denied(self, command: str) -> bool:
        lowered = command.lower()
        for pattern in self.deny_patterns:
            if re.search(pattern, lowered):
                return True
        for program in self.forbidden_programs:
            if re.search(rf"(^|[\\/\s\"']){re.escape(program)}(\.exe)?(\s|$|\")", lowered):
                return True
        return False

    def audit_line(self, tool: str, decision: Decision, detail: str = "") -> str:
        from datetime import datetime

        stamp = datetime.now().isoformat(timespec="seconds")
        return f"{stamp}\tmode={self.mode.value}\tsandbox={self.sandbox.value}\ttool={tool}\t{decision.value}\t{detail}".rstrip()


__all__ = [
    "Decision",
    "Mode",
    "Policy",
    "Sandbox",
    "DEFAULT_DENY_PATTERNS",
    "DEFAULT_FORBIDDEN_PROGRAMS",
    "WRITE_TOOLS",
]
