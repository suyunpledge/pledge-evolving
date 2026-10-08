"""Contribution registry: how code written by *other* agents gets combined.

The requirement that shapes this module: the new framework is not a federation
of existing agents at runtime — it is a single framework whose modules are
written by different agents and then combined. Combining only works if the
seam is frozen before anyone writes a line, and if a contribution that does not
fit is rejected mechanically instead of being patched up by hand.

So a contribution is:

* one file under ``forge/contrib/``
* declaring ``MODULE_API_VERSION``
* implementing exactly two functions: ``register(api)`` and ``selftest()``
* pure standard library, no network, no destructive file calls
* carrying at least 8 offline assertions of its own

Everything else about it is free. The registry loads it, checks all of the
above *statically and dynamically*, and only then exposes its declared
capabilities to the rest of the framework. A module that fails is quarantined,
never half-mounted.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MODULE_API_VERSION = 1

CONTRIB_DIRNAME = "contrib"

MIN_ASSERTIONS = 8

# An adopted module cannot be held to the full assertion count when the
# contributor's own test prints a summary instead of per-case rows; this floor
# is what we accept as "a test actually ran and reported a verdict".
ADAPTER_MIN_ASSERTIONS = 8

# Static bans. A contributed module runs in-process with the framework, so a
# network client or a recursive delete inside it is not a style issue.
# The table lives in forge.guard so the config-expression scanner and this
# source scanner share one ban surface and cannot drift apart.
from .guard import (DANGEROUS_LEAVES, FORBIDDEN_ATTRS, FORBIDDEN_CALLS,  # noqa: E402
FORBIDDEN_FROM_NAMES, FORBIDDEN_IMPORTS, attr_name)

REQUIRED_MANIFEST_KEYS = ("name", "version", "capabilities")

# Contributing agents very often deliver the *logic* in the shape they already
# use internally — a plain script with ``_self_test()`` and the hook functions
# at module level. That is a legitimate deliverable; demanding a bespoke shell
# would mean either a wasted round trip or the integrator rewriting their code.
# So the registry can adopt that shape through an auditable adapter: the hook
# functions must all be present, the contributor's own test is executed and its
# verdict parsed, and the adoption is stamped into the manifest and the index.
LEGACY_HOOK_MAP: dict[str, tuple[str, ...]] = {
    "scheduler": ("due_jobs", "acquire_lease", "gate"),
    "toolhost": ("register_tools", "authorize"),
    "replay": ("replay", "fork_plan", "diff_runs"),
    "catalog": ("catalog", "build_chain"),
    "curator": ("curate",),
    "teams": ("deliver",),
    "compactor": ("should_compact", "plan"),
    "mcp_bridge": ("plan_servers", "discover", "health"),
    "router": ("match", "estimate", "fallback_plan"),
    "hooks": ("authorize_hook", "dispatch", "describe"),
}

LEGACY_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "scheduler": ("schedule.periodic", "schedule.lease", "schedule.gate"),
    "toolhost": ("tools.normalize", "tools.authorize"),
    "replay": ("session.replay", "session.fork", "session.diff"),
    "catalog": ("models.catalog", "models.chain"),
    "curator": ("knowledge.metabolize",),
    "teams": ("team.deliver",),
    "compactor": ("context.compact",),
    "mcp_bridge": ("mcp.plan", "mcp.discover", "mcp.health"),
    "router": ("route.match", "route.estimate", "route.fallback"),
    "hooks": ("hooks.authorize", "hooks.dispatch", "hooks.describe"),
}

LEGACY_TEST_ENTRIES = ("_self_test", "_selftest", "self_test", "_test", "selftest")

AUTHORS_FILE = "AUTHORS.json"

# Three authors, three naming styles: ``due_jobs`` vs ``on_due_check``,
# ``replay`` vs ``on_replay``. Rather than force a rename on code somebody else
# owns, the registry resolves the canonical name through this alias table and
# records the drift as a warning. Consumers ask for the capability, not the
# spelling.
HOOK_ALIASES: dict[str, tuple[str, ...]] = {
    "due_jobs": ("due_jobs", "on_due_check", "due_check", "due", "on_tick"),
    "acquire_lease": ("acquire_lease", "on_lease", "lease", "try_lease"),
    "gate": ("gate", "on_gate", "global_gate"),
    "replay": ("replay", "on_replay"),
    "fork_plan": ("fork_plan", "on_fork_plan"),
    "diff_runs": ("diff_runs", "on_diff_runs"),
    "register_tools": ("register_tools", "on_register_tools", "normalize"),
    "authorize": ("authorize", "on_authorize"),
    "deliver": ("deliver", "on_deliver"),
    "advance": ("advance", "on_advance"),
    "budget_split": ("budget_split", "on_budget_split"),
    "should_compact": ("should_compact", "on_should_compact", "shouldCompact"),
    "plan": ("plan", "on_plan"),
    "merge_summary": ("merge_summary", "on_merge_summary"),
    "curate": ("curate", "on_curate"),
    "review": ("review", "on_review"),
    "ledger_entry": ("ledger_entry", "on_ledger_entry"),
    "plan_servers": ("plan_servers", "on_plan_servers"),
    "discover": ("discover", "on_discover"),
    "health": ("health", "on_health"),
    "match": ("match", "on_match"),
    "estimate": ("estimate", "on_estimate"),
    "fallback_plan": ("fallback_plan", "on_fallback_plan"),
    "catalog": ("catalog", "on_catalog"),
    "build_chain": ("build_chain", "on_build_chain"),
    "authorize_hook": ("authorize_hook", "on_authorize_hook"),
    "dispatch": ("dispatch", "on_dispatch"),
    "describe": ("describe", "on_describe"),
    # rev.4.1 接线轮：thinking 引擎的四个运行期消费名（同轮由 loop 消费，
    # capability thinking.mode；converged 是库函数、不登记）。
    "should_think": ("should_think", "on_should_think"),
    "build_thinking_task": ("build_thinking_task", "on_build_thinking_task"),
    "split_thinking": ("split_thinking", "on_split_thinking"),
    "estimate_budget": ("estimate_budget", "on_estimate_budget"),
}


@dataclass(frozen=True)
class ContribAPI:
    """The only surface a contributed module is allowed to touch."""

    home: Path
    workspace: Path
    api_version: int = MODULE_API_VERSION
    config: Any = None
    policy: Any = None
    memory: Any = None
    session: Any = None
    checkpoints: Any = None
    emit: Callable[..., None] | None = None
    log: Callable[[str], None] | None = None

    def config_get(self, path: str, default: Any = None, ctx: dict | None = None) -> Any:
        """``config_get("scheduler.tickSeconds", 60)`` — dotted path, no exceptions."""
        if self.config is None:
            return default
        row, _, key = str(path).partition(".")
        # M10: ``ctx or {}`` passed an EMPTY dict into _boot_ctx, whose
        # ``if ctx is not None`` branch then returned it as-is — so a
        # contribution calling config_get("x", ctx=None) (the default) never
        # saw the process env and get('env.X') resolved to '' (the 2026-09-15
        # "401 auth header" night, second half). None must stay None.
        value = self.config.get(row, key or "value", default, ctx)
        return default if value is None else value

    def fire(self, **event: Any) -> None:
        if self.emit is not None:
            self.emit(**event)

    def say(self, message: str) -> None:
        if self.log is not None:
            self.log(str(message))


@dataclass
class Contribution:
    name: str
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    assertions: list[tuple[str, bool, str]] = field(default_factory=list)
    loaded_at: float = field(default_factory=time.time)
    module: Any = None
    adapter: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.manifest.get("capabilities") or ())

    @property
    def hooks(self) -> dict[str, Any]:
        return dict(self.manifest.get("hooks") or {})

    @property
    def author(self) -> str:
        return str(self.manifest.get("author") or "(unattributed)")

    def implementation(self, canonical: str):
        """Resolve a canonical hook name through the alias table."""
        for candidate in HOOK_ALIASES.get(canonical, (canonical,)):
            target = self.hooks.get(candidate)
            if callable(target):
                if candidate != canonical:
                    self.warnings.append(
                        f"hook naming drift: {canonical} is spelled {candidate!r} in this module"
                    )
                return target
        return None

    def unresolvable_hooks(self) -> list[str]:
        """Hooks outside the canonical vocabulary — no canonical consumer path.

        BS-1 (rev.4.1 接线轮): the vocabulary spans both the canonical keys
        and their alias spellings. A hook outside it can still be found by a
        same-name lookup — ``implementation()``/``hook()`` fall back to the
        exact spelling when a consumer asks for it — but no *canonical*
        resolution will ever reach it, so the warning records that instead
        of claiming unreachability by any means.
        """
        known = set(HOOK_ALIASES)
        for aliases in HOOK_ALIASES.values():
            known.update(aliases)
        return sorted(name for name in self.hooks if name not in known)

    def to_raw(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "ok": self.ok,
            "adapter": self.adapter,
            "author": self.author,
            "problems": self.problems,
            "warnings": self.warnings,
            "capabilities": list(self.capabilities),
            "declared_version": self.manifest.get("version"),
            "assertions": len(self.assertions),
            "assertions_failed": sum(1 for _, passed, _ in self.assertions if not passed),
        }


# _attr_name moved to forge.guard (attr_name); the method above re-exports it


class ModuleRegistry:
    """Load, validate and expose agent-authored contributions."""

    def __init__(self, api: ContribAPI, contrib_dir: Path | None = None) -> None:
        self.api = api
        self.dir = Path(contrib_dir) if contrib_dir else (Path(api.home) / CONTRIB_DIRNAME)
        self.contributions: dict[str, Contribution] = {}
        self.authors: dict[str, str] = self._load_authors()

    def _load_authors(self) -> dict[str, str]:
        """Optional attribution sidecar, so adopted modules are not anonymous."""
        candidates = (self.dir / AUTHORS_FILE, Path(__file__).parent / CONTRIB_DIRNAME / AUTHORS_FILE)
        for candidate in candidates:
            if candidate.is_file():
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    return {str(k): str(v) for k, v in data.items()}
                except json.JSONDecodeError:
                    continue
        return {}

    # -- discovery -------------------------------------------------------
    def discover(self, directory: Path | None = None) -> list[Contribution]:
        target = Path(directory) if directory else self.dir
        if not target.is_dir():
            return []
        for file in sorted(target.glob("*.py")):
            if file.name.startswith("_"):
                continue
            if file.stem in self.contributions:
                continue
            self.contributions[file.stem] = self.load(file)
        return list(self.contributions.values())

    def load(self, file: Path) -> Contribution:
        contribution = Contribution(name=Path(file).stem, path=Path(file))
        source = ""
        try:
            source = Path(file).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            contribution.problems.append(f"unreadable: {exc}")
            return contribution

        contribution.problems.extend(self._static_checks(source, contribution.name))
        if contribution.problems:
            return contribution

        module = self._import(file)
        if module is None:
            contribution.problems.append("import failed (see stderr)")
            return contribution
        contribution.module = module

        declared = getattr(module, "MODULE_API_VERSION", None)
        has_entries = callable(getattr(module, "register", None)) and callable(getattr(module, "selftest", None))
        if declared == MODULE_API_VERSION and has_entries:
            return self._load_contract(module, contribution)

        adopted = self._adapt_legacy(module, contribution)
        if adopted is not None:
            return adopted

        if declared != MODULE_API_VERSION:
            contribution.problems.append(
                f"MODULE_API_VERSION must be {MODULE_API_VERSION}, got {declared!r}, "
                f"and this file does not match any legacy-script adapter entry"
            )
        else:
            contribution.problems.append(
                "declared the contract version but is missing register()/selftest(), "
                "and no legacy-script adapter entry matched"
            )
        return contribution

    def _load_contract(self, module, contribution: Contribution) -> Contribution:
        """The strict path: the contributor shipped the full contract shape."""
        for function in ("register", "selftest"):
            if not callable(getattr(module, function, None)):
                contribution.problems.append(f"missing callable {function}()")
        if contribution.problems:
            return contribution

        try:
            manifest = module.register(self.api)
        except Exception as exc:
            contribution.problems.append(f"register() raised {type(exc).__name__}: {exc}")
            return contribution
        if not isinstance(manifest, dict):
            contribution.problems.append("register() must return a dict manifest")
            return contribution
        for key in REQUIRED_MANIFEST_KEYS:
            if key not in manifest:
                contribution.problems.append(f"manifest is missing {key!r}")
        if not isinstance(manifest.get("capabilities"), (list, tuple)):
            contribution.problems.append("manifest['capabilities'] must be a list")
        if contribution.problems:
            return contribution

        contribution.manifest = dict(manifest)
        if str(manifest.get("name")) != contribution.name:
            contribution.problems.append(
                f"manifest name {manifest.get('name')!r} does not match file {contribution.name!r}"
            )
            return contribution

        if "selftest" in contribution.hooks:
            # rev.4.1 P0-1：hooks 键 ⊆ 规范词汇表的机械收口——selftest 是契约
            # 入口、永远不是运行期钩子；出现即拒（不是警告）。
            contribution.problems.append(
                "'selftest' must never be declared as a runtime hook — it is a "
                "contract entry, not a seat consumers can resolve"
            )
            return contribution

        contribution.assertions = self._expand_from_detail(self._collect_assertions(module))
        failures = [name for name, passed, _ in contribution.assertions if not passed]
        dead = contribution.unresolvable_hooks()
        if dead:
            contribution.warnings.append(
                f"hooks unreachable by any canonical name: {', '.join(dead)} — "
                f"add them to HOOK_ALIASES or rename them"
            )
        if len(contribution.assertions) < MIN_ASSERTIONS:
            contribution.problems.append(
                f"selftest() exposed {len(contribution.assertions)} assertions, need ≥ {MIN_ASSERTIONS} "
                f"(a single summary row is not enough; return one row per case or include the "
                f"per-case report in the detail field)"
            )
        if failures:
            contribution.problems.append(f"selftest failures: {', '.join(failures[:5])}")
        contribution.ok = not contribution.problems
        return contribution

    def _adapt_legacy(self, module, contribution: Contribution) -> Contribution | None:
        """Adopt a plain-script contribution without touching the author's logic.

        Requirements stay real: every hook the brief named must exist, the
        contributor's own self-test must run and pass, and the static bans were
        already applied before we got here. What changes is only the wrapper —
        and the adoption is stamped into the manifest so the index can never
        pretend the author shipped a contract module.
        """
        stem = contribution.name
        expected = LEGACY_HOOK_MAP.get(stem)
        if not expected:
            return None

        missing = [name for name in expected if not callable(getattr(module, name, None))]
        if missing:
            contribution.problems.append(f"legacy script is missing hook(s): {', '.join(missing)}")
            return contribution

        entry = next((n for n in LEGACY_TEST_ENTRIES if callable(getattr(module, n, None))), None)
        if entry is None:
            contribution.problems.append(
                "legacy script exposes no self-test entry (expected one of "
                f"{', '.join(LEGACY_TEST_ENTRIES)})"
            )
            return contribution

        contribution.adapter = "legacy-script"
        contribution.manifest = {
            "name": stem,
            "version": "1.0.0",
            "capabilities": list(LEGACY_CAPABILITIES.get(stem, ())),
            "hooks": {name: getattr(module, name) for name in expected},
            "author": self.authors.get(stem, "(unattributed)"),
            "summary": f"adopted from a plain script; hooks verified, self-test entry {entry}()",
            "adapter": "legacy-script",
            "source_entry": entry,
        }
        contribution.assertions = self._parse_legacy_test(getattr(module, entry))
        failures = [name for name, passed, _ in contribution.assertions if not passed]
        if failures:
            contribution.problems.append(f"contributor self-test failures: {', '.join(failures[:5])}")
            return contribution
        if not contribution.assertions:
            # The hole an external audit found: synthesising a verdict here made
            # "the test passed" indistinguishable from "there is no test", so an
            # emptied test body mounted unchanged. No evidence is a failure.
            contribution.problems.append(
                "contributor self-test supplied no per-case evidence: return "
                "(name, ok, detail) rows or print per-case PASS/FAIL lines "
                "(a declared SELFTEST_CASES count is checked against evidence, "
                "never substituted for it)"
            )
            return contribution
        if len(contribution.assertions) < ADAPTER_MIN_ASSERTIONS:
            contribution.problems.append(
                f"adopted module reported only {len(contribution.assertions)} verdict(s); "
                f"the contributor's test must expose at least {ADAPTER_MIN_ASSERTIONS}"
            )
            return contribution
        declared = getattr(module, "SELFTEST_CASES", None)
        if isinstance(declared, int) and declared > len(contribution.assertions):
            contribution.problems.append(
                f"SELFTEST_CASES declares {declared} case(s) but only "
                f"{len(contribution.assertions)} verdict(s) were evidenced — "
                f"the count checks the run, it does not create it"
            )
            return contribution
        if (isinstance(declared, int) and not isinstance(declared, bool)
                and declared < len(contribution.assertions)):
            contribution.warnings.append(
                f"SELFTEST_CASES declares {declared} case(s) but "
                f"{len(contribution.assertions)} verdict(s) were evidenced — "
                f"the declaration understates the run (recorded, not load-bearing)"
            )
        if len(contribution.assertions) < MIN_ASSERTIONS:
            contribution.warnings.append(
                f"contributor self-test does not print per-case rows "
                f"({len(contribution.assertions)} verdict(s) parsed); the integrator's "
                f"independent verifier carries the rest of the evidence"
            )
        contribution.ok = True
        return contribution

    @staticmethod
    def _parse_legacy_test(entry) -> list[tuple[str, bool, str]]:
        """Run the author's own test and read its verdict, without editing it.

        Returns *only* evidence: an empty list now means "no evidence", never
        "passed". ``SELFTEST_CASES`` may be declared to let the gate verify how
        much of the run got reported; it can never be substituted for verdicts.
        """
        import contextlib
        import io

        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                returned = entry()
        except BaseException as exc:  # noqa: BLE001 - a crashing test is a failing test
            return [(f"legacy:{getattr(entry, '__name__', 'test')}", False,
                     f"{type(exc).__name__}: {exc}")]

        if isinstance(returned, (list, tuple)) and returned:
            first = returned[0]
            if isinstance(first, (list, tuple)) and len(first) >= 2:
                return ModuleRegistry._collect_assertions_from(returned)
            if isinstance(first, str):
                # a list of *failure messages*: empty list means all passed, but
                # an empty list alone is not evidence of how much ran
                return [(str(msg)[:100], False, str(msg)[:160]) for msg in returned]

        rows = ModuleRegistry._parse_verdict_lines(buffer.getvalue())
        if rows:
            return rows

        # A declared case count is not evidence and must never mint verdicts:
        # an emptied test body would otherwise ride a number into the index.
        # The count may only *check* reported rows (see _adapt_legacy).
        return []

    @staticmethod
    def _expand_from_detail(rows: list[tuple[str, bool, str]]) -> list[tuple[str, bool, str]]:
        """Unfold a contributor that returned one summary row holding its report.

        Some contributors run their own per-case test, capture stdout, and hand
        back a single ``(name, True, <report>)`` row. The cases are really in
        the report; refusing it would fail a contributor whose test is fine.
        """
        if len(rows) >= MIN_ASSERTIONS:
            return rows
        expanded: list[tuple[str, bool, str]] = []
        for name, ok, detail in rows:
            parsed = ModuleRegistry._parse_verdict_lines(detail)
            expanded.extend(parsed if parsed else [(name, ok, detail)])
        return expanded

    @staticmethod
    def _parse_verdict_lines(text: str) -> list[tuple[str, bool, str]]:
        """Prefix-driven, so a passing case whose *title* mentions「失败」is not misread."""
        pass_marks = ("pass", "[pass]", "✅", "✓", "✔")
        fail_marks = ("fail", "[fail]", "❌", "✗", "×")
        rows: list[tuple[str, bool, str]] = []
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if lowered.startswith(fail_marks):
                rows.append((stripped[:100], False, stripped[:160]))
            elif lowered.startswith(pass_marks):
                rows.append((stripped[:100], True, ""))
        return rows

    @staticmethod
    def _collect_assertions_from(raw) -> list[tuple[str, bool, str]]:
        out: list[tuple[str, bool, str]] = []
        for item in raw:
            if isinstance(item, (tuple, list)) and len(item) >= 3:
                out.append((str(item[0]), bool(item[1]), str(item[2])))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                out.append((str(item[0]), bool(item[1]), ""))
        return out

    # -- checks ----------------------------------------------------------
    @staticmethod
    def _attr_name(node: ast.AST) -> str:
        return attr_name(node)

    @staticmethod
    def _static_checks(source: str, name: str) -> list[str]:
        problems: list[str] = []
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [f"syntax error: {exc}"]

        # alias-tracking: ``import shutil as sh`` / ``from os import unlink as u``
        # must not launder forbidden roots past the gate (g3 F4-1)
        alias_root: dict[str, str] = {}
        # WB-P0 (g1-R2): runtime assignments (``x = os``), walrus, for-loop
        # targets and lambda/def parameter defaults can all launder a module
        # root past an import-only tracker. A dedicated prepass builds
        # alias_root from every binding form, iterated to a fixed point so
        # chains and reverse-order definitions are covered.
        imported_roots: set[str] = set()
        # container aliases: a name bound to a dict/list/tuple of modules
        # (``d = {"a": os}`` / ``mods = (os,)``) so indexing and unpacking
        # cannot launder a module root past the gate.
        container_alias: dict[str, object] = {}

        def _import_problems(alias: ast.alias) -> list[str]:
            root = alias.name.split(".")[0]
            if root in FORBIDDEN_IMPORTS:
                return [f"forbidden import: {alias.name}"]
            if alias.asname:
                alias_root[alias.asname] = root
            return []

        def _resolve(dotted: str) -> str:
            """Rewrite an alias head to its canonical root before matching."""
            if "." in dotted:
                head, tail = dotted.split(".", 1)
                origin = alias_root.get(head)
                if origin:
                    return f"{origin}.{tail}"
            return dotted

        def _module_value(node: ast.AST) -> str | None:
            """Canonical origin when an expression provably aliases a module."""
            if isinstance(node, ast.Name):
                canon = alias_root.get(node.id, node.id)
                root = canon.split(".")[0]
                if root in imported_roots or node.id in alias_root:
                    return canon
                return None
            if isinstance(node, ast.Attribute):
                dotted = attr_name(node)
                if "." in dotted:
                    head = dotted.split(".")[0]
                    origin = alias_root.get(head, head)
                    if origin.split(".")[0] in imported_roots:
                        return f"{origin}.{dotted.split('.', 1)[1]}"
                return None
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                info = container_alias.get(node.value.id)
                key = node.slice
                if isinstance(key, ast.Constant):
                    k = key.value
                    if isinstance(info, dict) and k in info:
                        return str(info[k]) or None
                    if isinstance(info, list) and isinstance(k, int) and -len(info) <= k < len(info):
                        return str(info[k]) or None
                return None
            return None

        def _container_value(node: ast.AST) -> object:
            """A dict/list/tuple literal whose elements alias modules, else None."""
            if isinstance(node, ast.Dict):
                out: dict = {}
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant):
                        origin = _module_value(v)
                        if origin:
                            out[k.value] = origin
                return out or None
            if isinstance(node, (ast.Tuple, ast.List)):
                out_list = [_module_value(e) or "" for e in node.elts]
                return out_list if any(out_list) else None
            return None

        def _bind(target: ast.AST, value: ast.AST) -> None:
            """Bind target name(s) to the canonical origin of a module-ish value."""
            if isinstance(target, ast.Name):
                origin = _module_value(value)
                if origin:
                    alias_root[target.id] = origin
                    return
                container = _container_value(value)
                if container is not None:
                    container_alias[target.id] = container
            elif isinstance(target, (ast.Tuple, ast.List)):
                # unpack from a literal OR from a name holding a container
                origin_list = None
                if isinstance(value, (ast.Tuple, ast.List)):
                    origin_list = [_module_value(e) for e in value.elts]
                elif isinstance(value, ast.Name):
                    info = container_alias.get(value.id)
                    if isinstance(info, list):
                        origin_list = list(info)
                if origin_list is not None and len(target.elts) == len(origin_list):
                    for el, o in zip(target.elts, origin_list):
                        if o and isinstance(el, ast.Name):
                            alias_root[el.id] = str(o)

        def _bind_defaults(fn_args: ast.arguments) -> None:
            """Bind arg names whose default values alias a module."""
            positional = list(fn_args.posonlyargs) + list(fn_args.args)
            defaults = list(fn_args.defaults)
            if defaults:
                offset = len(positional) - len(defaults)
                for arg, default in zip(positional[offset:], defaults):
                    origin = _module_value(default)
                    if origin:
                        alias_root[arg.arg] = origin
            for arg, default in zip(fn_args.kwonlyargs, fn_args.kw_defaults):
                if default is not None:
                    origin = _module_value(default)
                    if origin:
                        alias_root[arg.arg] = origin

        # collect imported roots (available as bare names)
        def _collect_imports() -> None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported_roots.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    imported_roots.add((node.module or "").split(".")[0])
        _collect_imports()

        # iterate binding pass to a fixed point (chains, reverse-order defs)
        for _ in range(3):
            size = len(alias_root)
            csize = len(container_alias)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        problems.extend(_import_problems(alias))
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    if root in FORBIDDEN_IMPORTS:
                        problems.append(f"forbidden import: {node.module}")
                    else:
                        banned = FORBIDDEN_FROM_NAMES.get(node.module or "", frozenset())
                        for alias in node.names:
                            if "*" in banned or alias.name in banned:
                                problems.append(
                                    f"forbidden from-import: from {node.module} import {alias.name}")
                            if alias.asname:
                                alias_root[alias.asname] = f"{root}.{alias.name}"
                            else:
                                alias_root.setdefault(alias.name, f"{root}.{alias.name}")
                    if "contrib" in (node.module or ""):
                        problems.append("contributions must not import each other; use api")
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        _bind(target, node.value)
                elif isinstance(node, ast.NamedExpr):
                    _bind(node.target, node.value)
                elif isinstance(node, ast.For):
                    it = node.iter
                    if isinstance(it, (ast.List, ast.Tuple)) and len(it.elts) == 1:
                        it = it.elts[0]
                    _bind(node.target, it)
                elif isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                    _bind_defaults(node.args)
            if len(alias_root) == size and len(container_alias) == csize:
                break

        def _dotted(node: ast.AST) -> str | None:
            """attr_name that also resolves Subscript heads via container_alias.

            Returns None when the chain contains an index we cannot resolve —
            the caller then falls back to a leaf check for known exec/fs sinks.
            """
            parts: list[str] = []
            while True:
                if isinstance(node, ast.Attribute):
                    parts.append(node.attr)
                    node = node.value
                elif isinstance(node, ast.NamedExpr):
                    node = node.value
                elif isinstance(node, ast.Subscript):
                    origin = _module_value(node)
                    if origin:
                        parts.append(origin)
                    return ".".join(reversed([p for p in parts if p])) or None
                else:
                    break
            if isinstance(node, ast.Name):
                parts.append(alias_root.get(node.id, node.id))
            return ".".join(reversed([p for p in parts if p])) or None

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = _dotted(node.func)
                if called is None:
                    leaf = getattr(node.func, "attr", "")
                    if leaf in DANGEROUS_LEAVES:
                        problems.append(f"unresolved call to .{leaf}()")
                elif called in FORBIDDEN_CALLS:
                    problems.append(f"forbidden call: {called}()")
            elif isinstance(node, ast.Attribute):
                # H6-M1: a forbidden callable stored via ``fn = os.system`` is
                # a laundering step even without a call on this line
                dotted = _dotted(node)
                if dotted is not None and dotted in FORBIDDEN_CALLS:
                    problems.append(f"forbidden reference: {dotted}")
                elif dotted is None and node.attr in DANGEROUS_LEAVES:
                    problems.append(f"unresolved reference: .{node.attr}")
                elif node.attr in FORBIDDEN_ATTRS:
                    # H6-M2/M4: attribute tables and dunder chains
                    problems.append(f"forbidden dunder access: .{node.attr}")
            elif isinstance(node, ast.Name):
                # H6-M5: bare ``__builtins__`` (injected by the interpreter)
                # and from-import aliases pointing at forbidden callables
                name = alias_root.get(node.id, node.id)
                if name in FORBIDDEN_CALLS:
                    problems.append(f"forbidden reference: {name}")
        # dedup: drop ``forbidden reference`` when the same dotted was flagged
        # as a ``forbidden call`` (they share the same root cause)
        problems = sorted(set(problems))
        call_names = {p[len("forbidden call: "):-2]
                      for p in problems if p.startswith("forbidden call: ")}
        return [p for p in problems
                if not (p.startswith("forbidden reference: ")
                        and p[len("forbidden reference: "):] in call_names)]

    @staticmethod
    def _import(file: Path):
        name = f"forge_contrib_{Path(file).stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, file)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module
        except Exception as exc:  # a broken contribution must not take the framework down
            print(f"[registry] {file.name}: import raised {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.modules.pop(name, None)
            return None

    @staticmethod
    def _collect_assertions(module) -> list[tuple[str, bool, str]]:
        try:
            raw = module.selftest() or []
        except Exception as exc:
            return [(f"{module.__name__}:selftest-raised", False, f"{type(exc).__name__}: {exc}")]
        out: list[tuple[str, bool, str]] = []
        for item in raw:
            if isinstance(item, (tuple, list)) and len(item) == 3:
                out.append((str(item[0]), bool(item[1]), str(item[2])))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                out.append((str(item[0]), bool(item[1]), ""))
            else:
                out.append((f"{module.__name__}:malformed-assertion", False, repr(item)[:80]))
        return out

    # -- use -------------------------------------------------------------
    def healthy(self) -> list[Contribution]:
        return [c for c in self.contributions.values() if c.ok]

    def capabilities(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for contribution in self.healthy():
            for capability in contribution.capabilities:
                out.setdefault(capability, []).append(contribution.name)
        return out

    def hook(self, name: str) -> list[tuple[str, Any]]:
        """Return ``(module_name, callable)`` for every module implementing a hook.

        BS-1 (rev.4.1 接线轮): one resolution semantics — this delegates to
        ``Contribution.implementation`` so alias spellings (``on_*``) resolve
        here exactly as they do for the mounted seats; a bare-name lookup used
        to miss them silently.
        """
        found: list[tuple[str, Any]] = []
        for contribution in self.healthy():
            target = contribution.implementation(name)
            if callable(target):
                found.append((contribution.name, target))
        return found

    def run_selftests(self) -> list[tuple[str, str, bool, str]]:
        rows: list[tuple[str, str, bool, str]] = []
        for contribution in self.contributions.values():
            if not contribution.ok:
                continue
            for name, passed, detail in contribution.assertions:
                rows.append((contribution.name, name, passed, detail))
        return rows

    def report(self) -> dict[str, Any]:
        return {
            "api_version": MODULE_API_VERSION,
            "directory": str(self.dir),
            "contributed": len(self.contributions),
            "healthy": len(self.healthy()),
            "adopted": [c.name for c in self.healthy() if c.adapter],
            "quarantined": [c.to_raw() for c in self.contributions.values() if not c.ok],
            "warnings": {c.name: c.warnings for c in self.contributions.values() if c.warnings},
            "capabilities": self.capabilities(),
            "assertions": sum(len(c.assertions) for c in self.healthy()),
        }


__all__ = [
    "ADAPTER_MIN_ASSERTIONS",
    "AUTHORS_FILE",
    "CONTRIB_DIRNAME",
    "ContribAPI",
    "Contribution",
    "FORBIDDEN_CALLS",
    "FORBIDDEN_IMPORTS",
    "LEGACY_CAPABILITIES",
    "LEGACY_HOOK_MAP",
    "LEGACY_TEST_ENTRIES",
    "MIN_ASSERTIONS",
    "MODULE_API_VERSION",
    "ModuleRegistry",
    "REQUIRED_MANIFEST_KEYS",
]
