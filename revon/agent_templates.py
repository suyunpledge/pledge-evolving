"""Preset agent templates (角色模板): a ready-made role instead of configuring from scratch.

A template is *data* — one JSON file per role. Built-ins ship in
``revon/bundles/agents/``; user copies live in ``<home>/agents/`` (home =
``revon.compat.default_home()``). A template does not invent a parallel
configuration system: applying it writes the same rows every other entry point
already reads —

    policy    mode / sandbox / readScope / allow / ask / deny   (revon.policy)
    tools     expose                                            (tools:expose)
    loop      maxSteps / maxDepth / spawnBudget                 (revon.loop_limits)
    planning  level                                             (revon.planning)
    thinking  mode                                              (thinking:mode)
    role      guidance / template / skills / preferTools        (Agent.extra_system)

Security contract: a template only ever *narrows* what the already composed
configuration (bundles + user layer + --profile) allows. The composed policy row
is the ceiling; the template's policy is clamped to it and then checked tool by
tool, so a template can never turn a user's deny or ask into an allow, widen the
sandbox or read scope, add read roots, or raise a loop budget the user set in
their own layer. Templates may not request ``bypassPermissions``/``dontAsk`` or
``danger-full-access`` at all, and ``allow`` entries must be exact tool names.

Standard library only (the kernel never imports Qt).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import USER_LAYER_NAME
from .policy import Decision, Mode, Policy, ReadScope, Sandbox, WRITE_TOOLS, override_warnings

SCHEMA = 1
BUILTIN_DIR = Path(__file__).resolve().parent / "bundles" / "agents"
USER_DIRNAME = "agents"
LABEL_PREFIX = "template:"

_ID = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_RULE = re.compile(r"^[A-Za-z0-9_.:*-]{1,64}$")
_ICON = re.compile(r"^[a-z0-9-]{1,40}$")

#: Modes and sandboxes a template may ask for. The two most permissive modes and
#: the full-access sandbox are grants only the user can make (``--profile`` or the
#: desktop permission switch), never a role preset.
TEMPLATE_MODES = (Mode.READ_ONLY, Mode.PLAN, Mode.DEFAULT, Mode.ACCEPT_EDITS, Mode.AUTO)
TEMPLATE_SANDBOXES = (Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE)
PLANNING_LEVELS = ("none", "low", "medium", "high")
THINKING_LEVELS = ("off", "smart", "on")
_SANDBOX_RANK = {Sandbox.READ_ONLY: 0, Sandbox.WORKSPACE_WRITE: 1, Sandbox.FULL_ACCESS: 2}
_DECISION_RANK = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}
_LOOP_KEYS = ("maxSteps", "maxDepth", "spawnBudget")
_LOOP_BOUNDS = {"maxSteps": (1, 200), "maxDepth": (0, 8), "spawnBudget": (0, 64)}

#: Desktop (Qt) tool names that are the same capability as a kernel tool.
TOOL_ALIASES = {"public_search": "web_search"}
#: Tools only the desktop app provides (mediated locally, not kernel builtins).
DESKTOP_TOOLS = frozenset({"public_search", "text_transform", "unit_convert", "7zip_extract",
                           "generate_image", "generate_video", "comfyui_run",
                           "office_list", "office_read", "office_create", "office_apply", "office_export"})

_TOP_KEYS = {"schema", "id", "name", "nameEn", "icon", "description", "guidance", "tools", "policy",
             "loop", "planning", "thinking", "skills", "starters", "basedOn"}
_POLICY_KEYS = {"mode", "sandbox", "readScope", "allow", "ask", "deny"}
_TOOLS_KEYS = {"expose", "prefer", "mcp"}


class TemplateError(ValueError):
    """A template file is malformed or asks for something a template may not grant."""


def known_tools() -> frozenset[str]:
    from .tools import BUILTIN_TOOL_NAMES
    return frozenset(BUILTIN_TOOL_NAMES) | DESKTOP_TOOLS


def canonical_tool(name: str) -> str:
    """``keystone_read_file`` -> ``read_file``; desktop aliases -> kernel name."""
    name = str(name or "").removeprefix("keystone_")
    return TOOL_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentTemplate:
    id: str
    name: str
    icon: str
    description: str
    guidance: str
    expose: tuple[str, ...] = ()
    prefer: tuple[str, ...] = ()
    mcp: bool = True
    mode: Mode = Mode.DEFAULT
    sandbox: Sandbox = Sandbox.WORKSPACE_WRITE
    read_scope: ReadScope = ReadScope.WORKSPACE
    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    loop: dict[str, int] = field(default_factory=dict)
    planning: str = "none"
    thinking: str = "off"
    skills: tuple[str, ...] = ()
    starters: tuple[str, ...] = ()
    name_en: str = ""
    based_on: str = ""
    builtin: bool = False
    path: str = ""

    # -- parsing ---------------------------------------------------------
    @classmethod
    def from_raw(cls, raw: Any, *, builtin: bool = False, path: str = "") -> "AgentTemplate":
        where = f" ({path})" if path else ""
        if not isinstance(raw, dict):
            raise TemplateError(f"模板必须是 JSON 对象{where}")
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            raise TemplateError(f"模板含未知字段 {sorted(unknown)}{where}")
        if raw.get("schema", SCHEMA) != SCHEMA:
            raise TemplateError(f"不支持的模板版本 {raw.get('schema')!r}{where}")
        tid = raw.get("id")
        if not isinstance(tid, str) or not _ID.match(tid):
            raise TemplateError(f"模板 id 须为 2-40 位小写字母/数字/短横线，以字母开头{where}")

        def text(key: str, limit: int, *, required: bool = True) -> str:
            value = raw.get(key, "")
            if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
                raise TemplateError(f"模板 {tid}: {key} 须为 1-{limit} 字的文本{where}")
            return value.strip()

        def names(value: Any, key: str, pattern: re.Pattern = _TOOL, limit: int = 64) -> tuple[str, ...]:
            if value is None:
                return ()
            if not isinstance(value, list) or len(value) > limit or not all(
                    isinstance(v, str) and pattern.match(v) for v in value):
                raise TemplateError(f"模板 {tid}: {key} 须为工具名列表{where}")
            return tuple(dict.fromkeys(value))

        icon = raw.get("icon", "bot")
        if not isinstance(icon, str) or not _ICON.match(icon):
            raise TemplateError(f"模板 {tid}: icon 须为图标名（如 code-xml）{where}")

        tools = raw.get("tools") or {}
        if not isinstance(tools, dict) or set(tools) - _TOOLS_KEYS:
            raise TemplateError(f"模板 {tid}: tools 只能含 {sorted(_TOOLS_KEYS)}{where}")
        if not isinstance(tools.get("mcp", True), bool):
            raise TemplateError(f"模板 {tid}: tools.mcp 须为 true/false{where}")

        policy = raw.get("policy") or {}
        if not isinstance(policy, dict) or set(policy) - _POLICY_KEYS:
            raise TemplateError(f"模板 {tid}: policy 只能含 {sorted(_POLICY_KEYS)}（不能声明读根或工作区）{where}")
        try:
            mode = Mode(str(policy.get("mode", Mode.DEFAULT.value)))
            sandbox = Sandbox(str(policy.get("sandbox", Sandbox.WORKSPACE_WRITE.value)))
            read_scope = ReadScope(str(policy.get("readScope", ReadScope.WORKSPACE.value)))
        except ValueError as exc:
            raise TemplateError(f"模板 {tid}: {exc}{where}") from None
        if mode not in TEMPLATE_MODES:
            raise TemplateError(f"模板 {tid}: 模板不能申请 {mode.value} 权限模式——这只能由用户自己授予{where}")
        if sandbox not in TEMPLATE_SANDBOXES:
            raise TemplateError(f"模板 {tid}: 模板不能申请 {sandbox.value} 沙箱——这只能由用户自己授予{where}")
        # allow 只收精确工具名：通配会顺带放开用户基线里本该询问的工具。
        allow = names(policy.get("allow"), "policy.allow", _TOOL)

        loop = raw.get("loop") or {}
        if not isinstance(loop, dict) or set(loop) - set(_LOOP_KEYS):
            raise TemplateError(f"模板 {tid}: loop 只能含 {list(_LOOP_KEYS)}{where}")
        for key, value in loop.items():
            low, high = _LOOP_BOUNDS[key]
            if type(value) is not int or not low <= value <= high:
                raise TemplateError(f"模板 {tid}: loop.{key} 须为 {low}-{high} 的整数{where}")

        planning = raw.get("planning", "none")
        thinking = raw.get("thinking", "off")
        if planning not in PLANNING_LEVELS:
            raise TemplateError(f"模板 {tid}: planning 须为 {PLANNING_LEVELS} 之一{where}")
        if thinking not in THINKING_LEVELS:
            raise TemplateError(f"模板 {tid}: thinking 须为 {THINKING_LEVELS} 之一{where}")

        starters = raw.get("starters") or []
        if not isinstance(starters, list) or len(starters) > 8 or not all(
                isinstance(s, str) and 0 < len(s.strip()) <= 200 for s in starters):
            raise TemplateError(f"模板 {tid}: starters 须为最多 8 条、每条 1-200 字的文本{where}")
        based_on = raw.get("basedOn", "")
        if based_on and (not isinstance(based_on, str) or not _ID.match(based_on)):
            raise TemplateError(f"模板 {tid}: basedOn 须为模板 id{where}")

        return cls(
            id=tid, name=text("name", 24), icon=icon, description=text("description", 120),
            guidance=text("guidance", 4000), name_en=text("nameEn", 40, required=False),
            expose=names(tools.get("expose"), "tools.expose"),
            prefer=names(tools.get("prefer"), "tools.prefer"), mcp=tools.get("mcp", True),
            mode=mode, sandbox=sandbox, read_scope=read_scope, allow=allow,
            ask=names(policy.get("ask"), "policy.ask", _RULE),
            deny=names(policy.get("deny"), "policy.deny", _RULE),
            loop=dict(loop), planning=planning, thinking=thinking,
            skills=names(raw.get("skills"), "skills", _ID, 16),
            starters=tuple(s.strip() for s in starters), based_on=based_on,
            builtin=builtin, path=path,
        )

    def to_raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema": SCHEMA, "id": self.id, "name": self.name}
        if self.name_en:
            out["nameEn"] = self.name_en
        out.update(icon=self.icon, description=self.description, guidance=self.guidance)
        if self.based_on:
            out["basedOn"] = self.based_on
        tools: dict[str, Any] = {"expose": list(self.expose)}
        if self.prefer:
            tools["prefer"] = list(self.prefer)
        if not self.mcp:
            tools["mcp"] = False
        out["tools"] = tools
        out["policy"] = {"mode": self.mode.value, "sandbox": self.sandbox.value,
                         "readScope": self.read_scope.value, "allow": list(self.allow),
                         "ask": list(self.ask), "deny": list(self.deny)}
        out["loop"] = dict(self.loop)
        out["planning"], out["thinking"] = self.planning, self.thinking
        out["skills"], out["starters"] = list(self.skills), list(self.starters)
        return out

    # -- derived views ---------------------------------------------------
    def role_text(self) -> str:
        """Role guidance for the run envelope (Agent.extra_system / Qt system context)."""
        parts = [f"[角色模板：{self.name}]", self.guidance]
        if self.prefer:
            parts.append("优先使用的工具：" + "、".join(self.prefer) + "。")
        if self.skills:
            parts.append("开始前先加载技能：" + "、".join(
                f"skill_list(name=\"{s}\")" for s in self.skills) + "，按其中的说明工作。")
        return "\n".join(parts)

    def own_policy(self, workspace: Path, *, non_interactive: bool = False) -> Policy:
        """The template's policy on its own (used by the desktop to stack it on a grant)."""
        return Policy(mode=self.mode, sandbox=self.sandbox, workspace=Path(workspace),
                      allow=self.allow, ask=self.ask, deny=self.deny, read_scope=self.read_scope,
                      non_interactive=non_interactive)

    def policy_warnings(self) -> list[str]:
        return override_warnings(self.own_policy(Path('.')))

    def tool_allowed(self, name: str) -> bool:
        """Whether this role exposes ``name`` (kernel or desktop spelling)."""
        if not isinstance(name, str) or not name:
            return False
        if name.startswith("mcp_"):
            return self.mcp
        key = canonical_tool(name)
        if Policy._matches(self.deny, key) or Policy._matches(self.deny, name.removeprefix("keystone_")):
            return False
        if not self.expose:
            return True
        exposed = {canonical_tool(t) for t in self.expose}
        return key in exposed or name.removeprefix("keystone_") in self.expose

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "nameEn": self.name_en, "icon": self.icon,
                "description": self.description, "builtin": self.builtin, "basedOn": self.based_on,
                "mode": self.mode.value, "sandbox": self.sandbox.value,
                "maxSteps": int(self.loop.get("maxSteps", 0)), "planning": self.planning,
                "tools": len(self.expose), "skills": list(self.skills), "starters": list(self.starters),
                "guidance": self.guidance}


# ---------------------------------------------------------------------------
# library (built-ins + user copies)
# ---------------------------------------------------------------------------

def user_dir(home: str | Path) -> Path:
    return Path(home) / USER_DIRNAME


def _read(path: Path, *, builtin: bool) -> AgentTemplate:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TemplateError(f"无法读取模板 {path.name}: {exc}") from exc
    template = AgentTemplate.from_raw(raw, builtin=builtin, path=str(path))
    if template.id != path.stem:
        raise TemplateError(f"模板文件名须与 id 一致：{path.name} 声明的是 {template.id!r}")
    return template


def builtin_templates() -> list[AgentTemplate]:
    return [_read(p, builtin=True) for p in sorted(BUILTIN_DIR.glob("*.json"))]


def load_templates(home: str | Path | None = None) -> tuple[list[AgentTemplate], list[str]]:
    """All templates (built-ins first, in ``order``) plus human-readable load problems.

    A broken user copy never hides the built-ins; it is reported and skipped.
    A user file cannot shadow a built-in id.
    """
    templates = builtin_templates()
    templates.sort(key=lambda t: (_ORDER.index(t.id) if t.id in _ORDER else len(_ORDER), t.id))
    problems: list[str] = []
    seen = {t.id for t in templates}
    if home is not None:
        folder = user_dir(home)
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            try:
                template = _read(path, builtin=False)
            except TemplateError as exc:
                problems.append(str(exc))
                continue
            if template.id in seen:
                problems.append(f"{path.name}: id {template.id!r} 与已有模板重复，已忽略")
                continue
            seen.add(template.id)
            templates.append(template)
            problems.extend(f"{path.name} · {warning}" for warning in template.policy_warnings())
    return templates, problems


#: Display order of the built-ins (the user's own list order).
_ORDER = ("fullstack", "modeler", "tutor", "butler", "writer", "reviewer", "researcher")


def get_template(template_id: str, home: str | Path | None = None) -> AgentTemplate:
    templates, _problems = load_templates(home)
    for template in templates:
        if template.id == template_id:
            return template
    ids = ", ".join(t.id for t in templates)
    raise TemplateError(f"没有名为 {template_id!r} 的模板。可用：{ids}")


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _free_id(base: str, taken: set[str]) -> str:
    stem = re.sub(r"-copy\d*$", "", base)[:32]
    for n in range(1, 1000):
        candidate = f"{stem}-copy" + ("" if n == 1 else str(n))
        if candidate not in taken:
            return candidate
    raise TemplateError("副本太多，请先删除一些")


def save_template(home: str | Path, raw: dict[str, Any]) -> AgentTemplate:
    """Validate and write a user template. Built-in ids are read-only."""
    template = AgentTemplate.from_raw(raw)
    if template.id in {t.id for t in builtin_templates()}:
        raise TemplateError(f"内置模板 {template.id!r} 不能被覆盖，请先复制再修改")
    path = user_dir(home) / f"{template.id}.json"
    _atomic_write(path, template.to_raw())
    return _read(path, builtin=False)


def duplicate_template(home: str | Path, template_id: str, *, name: str | None = None,
                       changes: dict[str, Any] | None = None) -> AgentTemplate:
    """Copy any template into the user home under a fresh id, optionally edited."""
    source = get_template(template_id, home)
    templates, _ = load_templates(home)
    raw = source.to_raw()
    raw["id"] = _free_id(source.id, {t.id for t in templates})
    raw["name"] = (name or f"{source.name}（副本）")[:24]
    raw["basedOn"] = source.id if source.builtin else (source.based_on or source.id)
    for key, value in (changes or {}).items():
        if key in ("id", "schema"):
            continue
        raw[key] = value
    return save_template(home, raw)


def delete_template(home: str | Path, template_id: str) -> None:
    if template_id in {t.id for t in builtin_templates()}:
        raise TemplateError("内置模板不能删除")
    path = user_dir(home) / f"{template_id}.json"
    if not _ID.match(template_id) or not path.is_file():
        raise TemplateError(f"没有名为 {template_id!r} 的自定义模板")
    path.unlink()


# ---------------------------------------------------------------------------
# applying a template to a composed Config (never escalates)
# ---------------------------------------------------------------------------

@dataclass
class AppliedTemplate:
    template: AgentTemplate
    policy: dict[str, Any]
    expose: list[str] | None
    loop: dict[str, Any]
    #: Things the template asked for that the user's own policy did not allow.
    narrowed: list[str] = field(default_factory=list)


def clamp_policy(ceiling_conf: dict[str, Any], template: AgentTemplate, *,
                 workspace: Path | None = None, probe: Iterable[str] = ()) -> tuple[dict[str, Any], list[str]]:
    """Return ``(policy row config, notes)``: the template's policy, no wider than the ceiling."""
    workspace = Path(workspace or Path.cwd())
    notes: list[str] = []
    ceiling = Policy.from_config(_RowView({"policy": ceiling_conf}), workspace=workspace, non_interactive=False)
    out = dict(ceiling_conf)  # keeps readRoots, notes, workspace … the user set

    mode = template.mode.clamp_to(ceiling.mode)
    if ceiling.mode is Mode.DONT_ASK and mode.rank < Mode.DONT_ASK.rank:
        # dontAsk refuses writes outright; a lower-ranked mode would ask instead. The
        # per-tool guard below keeps the refusal; record it for the user.
        notes.append("用户策略 dontAsk 下写入仍一律拒绝")
    if mode is not template.mode:
        notes.append(f"权限模式 {template.mode.value} → {mode.value}（用户策略上限）")
    sandbox = template.sandbox if _SANDBOX_RANK[template.sandbox] <= _SANDBOX_RANK[ceiling.sandbox] else ceiling.sandbox
    if sandbox is not template.sandbox:
        notes.append(f"沙箱 {template.sandbox.value} → {sandbox.value}（用户策略上限）")
    read_scope = template.read_scope
    probe_policy = Policy(mode=mode, sandbox=sandbox, workspace=workspace, read_scope=read_scope)
    if probe_policy.read_allows_anywhere() and not ceiling.read_allows_anywhere():
        read_scope = ReadScope.WORKSPACE
        notes.append("读取范围收窄到工作区（用户策略上限）")

    allow: list[str] = []
    for tool in template.allow:
        if ceiling.evaluate(tool) is Decision.ALLOW:
            allow.append(tool)
        else:
            notes.append(f"{tool} 未获免询问授权（用户策略为 {ceiling.evaluate(tool).value}）")
    deny = list(dict.fromkeys([*ceiling.deny, *template.deny]))
    ask = [r for r in dict.fromkeys([*ceiling.ask, *template.ask]) if r not in deny]
    out.update(mode=mode.value, sandbox=sandbox.value, readScope=read_scope.value,
               allow=allow, ask=ask, deny=deny)

    # Tool-by-tool guard: whatever the mode baselines do, the result is never more
    # permissive than the ceiling for any tool we know about.
    result = Policy.from_config(_RowView({"policy": out}), workspace=workspace, non_interactive=False)
    tools = set(known_tools()) | set(WRITE_TOOLS) | set(template.expose) | set(template.allow) | set(probe)
    for tool in sorted(tools):
        want, cap = result.evaluate(tool), ceiling.evaluate(tool)
        if _DECISION_RANK[want] < _DECISION_RANK[cap]:
            target = out["deny"] if cap is Decision.DENY else out["ask"]
            if tool not in target:
                target.append(tool)
            if tool in out["allow"]:
                out["allow"].remove(tool)
    return out, notes


class _RowView:
    """Minimal ``cfg.get(row, key, default)`` over plain dicts (for Policy.from_config)."""

    def __init__(self, rows: dict[str, dict[str, Any]]):
        self._rows = rows

    def get(self, row_id: str, key: str, default: Any = None, ctx: Any = None) -> Any:
        return self._rows.get(row_id, {}).get(key, default)


def apply_template(cfg, template: AgentTemplate, *, workspace: Path | None = None) -> AppliedTemplate:
    """Layer ``template`` onto an already composed Config, in place.

    Call this *after* bundles, the user layer and ``--profile`` are applied: the
    policy found there is the ceiling the template is clamped to.
    """
    label = LABEL_PREFIX + template.id
    notes: list[str] = []
    # -- policy ---------------------------------------------------------
    policy_row = cfg.row("policy")
    ceiling_conf = {k: cfg.get("policy", k) for k in (policy_row.config if policy_row else {})}
    policy_conf, policy_notes = clamp_policy(ceiling_conf, template, workspace=workspace)
    notes += policy_notes
    cfg.apply_patch([{"id": "policy", "name": policy_row.name if policy_row and policy_row.name else "policy:core",
                      "config": policy_conf}], label=label)

    # -- tools.expose (intersection; deny-listed tools are not shown) -----
    current = cfg.get("tools", "expose", None)
    expose: list[str] | None
    if template.expose:
        expose = [t for t in template.expose if current is None or t in current]
        dropped = [t for t in template.expose if t not in expose]
        if dropped:
            notes.append("用户配置未开放的工具不会出现：" + "、".join(dropped))
    else:
        expose = list(current) if current is not None else None
    if expose is not None:
        expose = [t for t in expose if not Policy._matches(policy_conf["deny"], t)]
        tools_row = cfg.row("tools")
        merged = dict(tools_row.config) if tools_row else {}
        merged["expose"] = expose
        cfg.apply_patch([{"id": "tools", "name": "tools:expose", "config": merged}], label=label)

    # -- loop (a value the user declared in their own layer is a ceiling) ---
    loop_row = cfg.row("loop")
    loop_conf = dict(loop_row.config) if loop_row else {}
    user_set = loop_row is not None and str(loop_row.source) == USER_LAYER_NAME
    for key, value in template.loop.items():
        if user_set and key in loop_conf and int(loop_conf[key]) < value:
            # maxSteps 0 is "no step limit": the user lifted the limit, so no role budget applies.
            shown = "不限" if key == "maxSteps" and int(loop_conf[key]) <= 0 else loop_conf[key]
            notes.append(f"loop.{key} 保持用户设定的 {shown}（模板建议 {value}）")
            continue
        loop_conf[key] = value
    cfg.apply_patch([{"id": "loop", "name": loop_row.name if loop_row and loop_row.name else "loop:core",
                      "config": loop_conf}], label=label)

    # -- planning / thinking (merge, never drop the row's other keys) -----
    for row_id, name, key, value in (("planning", "planning:level", "level", template.planning),
                                     ("thinking", "thinking:mode", "mode", template.thinking)):
        row = cfg.row(row_id)
        merged = dict(row.config) if row else {}
        merged[key] = value
        cfg.apply_patch([{"id": row_id, "name": row.name if row and row.name else name, "config": merged}],
                        label=label)

    # -- role guidance ----------------------------------------------------
    cfg.apply_patch([{"id": "role", "name": "role:template", "config": {
        "template": template.id, "name": template.name, "guidance": template.role_text(),
        "skills": list(template.skills), "preferTools": list(template.prefer)}}], label=label)
    return AppliedTemplate(template=template, policy=policy_conf, expose=expose, loop=loop_conf, narrowed=notes)


def active_template_id(cfg) -> str:
    return str(cfg.get("role", "template", "") or "")


__all__ = [
    "AgentTemplate", "AppliedTemplate", "BUILTIN_DIR", "TemplateError", "apply_template", "builtin_templates",
    "canonical_tool", "clamp_policy", "delete_template", "duplicate_template", "get_template", "known_tools",
    "load_templates", "save_template", "user_dir",
]
