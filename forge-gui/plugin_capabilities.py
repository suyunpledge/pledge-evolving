"""Agent-facing declarative plugins. No plugin Python is imported or executed.

Aliases register in a Forge ToolRegistry and delegate only to host-owned file
tools on the gateway, where the real configured Policy is evaluated again.
Grants narrow that policy; they never authorize an otherwise denied operation.
This is a capability broker, not an OS sandbox or signature verifier.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import stat
import threading
import uuid

from plugin_market import Marketplace, linked, market_lock, workspace_key
from plugin_runtime import BUILTIN_TOOL_NAMES, LoadReport, _validate_schema, re_fullmatch

TARGETS = {
    "read_file": ("repo.read", {"path": str, "limit": int}),
    "list_dir": ("repo.read", {"path": str}),
    "write_file": ("repo.write", {"path": str, "content": str, "append": bool}),
    "edit_file": ("repo.write", {"path": str, "old": str, "new": str,
                                  "replace_all": bool, "start_line": int, "end_line": int}),
}
#: contributions.skills 的受控执行器：返回插件自带的技能正文（SKILL.md 等）。
#: 不经网关、不触工作区文件——正文本身是插件包内容，安装时已计入指纹。
SKILL_CAPABILITY = "skill.read"
SKILL_TARGET = "skill_body"
MAX_SKILL_BODY_BYTES = 128 * 1024
MAX_INLINE_SKILL_BODY = 8192
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_CHARS = 200_000
_EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": [],
                 "additionalProperties": False}


def _skill_relative_path(plugin, raw) -> str:
    """校验技能声明里的 file 字段，返回插件目录内的相对 posix 路径。"""
    if (not isinstance(raw, str) or not raw or len(raw) > 160
            or "\\" in raw or raw != raw.strip()):
        raise ValueError("技能文件必须是非空相对路径")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts or any(":" in part for part in rel.parts):
        raise ValueError("技能文件必须位于插件目录内")
    if not plugin.path:
        raise ValueError("插件目录未知，无法定位技能文件")
    root = Path(plugin.path).resolve()
    if not (root / rel).resolve().is_relative_to(root):
        raise ValueError("技能文件必须位于插件目录内")
    return rel.as_posix()


class CapabilityRuntime:
    def __init__(self, market: Marketplace, workspace, session, dispatch):
        import forge
        self.market = market
        self.workspace = Path(workspace_key(workspace))
        self.session = str(session)
        self.dispatch = dispatch  # trusted host gateway client, never from a manifest
        self._control_root = market.home.resolve()
        # Frozen builds resolve __file__ into _MEIPASS, so also anchor the host-code
        # guard on the workspace itself when it really is the Forge repo checkout.
        host_roots = [Path(__file__).resolve().parent, Path(forge.__file__).resolve().parent]
        if (self.workspace / "forge" / "gateway.py").is_file():
            host_roots += [self.workspace / "forge-gui", self.workspace / "forge"]
        self._host_code_roots = tuple(host_roots)
        entrypoints = [Path(__file__).resolve().parents[1] / "run.py"]
        if (self.workspace / "run.py").is_file():
            entrypoints.append(self.workspace / "run.py")
        self._host_entrypoints = tuple(entrypoints)
        self.tools = {}
        self.last_report = LoadReport()
        self._lock = threading.RLock()

    def reload(self, *, reserved_names=(), available_targets=None, plugins=None):
        from forge.tools import ToolRegistry, ToolSpec
        report, tools, registry = LoadReport(), {}, ToolRegistry()
        reserved = set(BUILTIN_TOOL_NAMES) | set(reserved_names)
        available = None if available_targets is None else {n.removeprefix("forge_") for n in available_targets}
        for plugin in self.market.catalog() if plugins is None else plugins:
            if not plugin.installed or not plugin.enabled or plugin.error or plugin.needs_ack:
                continue
            if plugin.executes_code:
                report.skipped.append((plugin.id, "Python 插件仅有信任门；没有系统沙箱，不向 Agent 开放"))
                continue
            declarations = plugin.contributions.get("tools", [])
            try:
                batch = {}
                for raw in declarations:
                    name, target = raw.get("name"), raw.get("target")
                    if not isinstance(name, str) or len(name) > 64 or not re_fullmatch(name):
                        raise ValueError("工具名称无效")
                    if name in batch or name in tools or name in reserved or name.removeprefix("forge_") in reserved:
                        raise ValueError(f"工具名冲突：{name}")
                    if target not in TARGETS:
                        raise ValueError(f"未接入受控执行器：{target}")
                    if available is not None and target not in available:
                        report.skipped.append((plugin.id, f"等待工具桥提供 {target}"))
                        continue
                    capability, arg_types = TARGETS[target]
                    if capability not in plugin.declared_capabilities:
                        raise PermissionError(f"工具未声明必要能力：{capability}")
                    if set(raw) - {"name", "target", "description", "parameters"}:
                        raise ValueError("工具声明含未知执行字段")
                    description = raw.get("description", "")
                    if not isinstance(description, str) or len(description) > 4096:
                        raise ValueError("工具描述无效")
                    parameters = raw.get("parameters", {"type": "object", "properties": {
                        "path": {"type": "string"}}, "required": ["path"]})
                    if not isinstance(parameters, dict):
                        raise ValueError("parameters 必须是对象")
                    _validate_schema(parameters)
                    # A bounded flat schema is enough for these host-owned file operations.
                    if (set(parameters) - {"type", "properties", "required", "additionalProperties", "description"}
                            or parameters.get("additionalProperties", False) is not False
                            or set(parameters.get("properties", {})) - set(arg_types)):
                        raise ValueError("受控文件工具只支持已知参数的封闭平面 schema")
                    for key, prop in parameters.get("properties", {}).items():
                        expected = {str: "string", int: "integer", bool: "boolean"}[arg_types[key]]
                        if set(prop) - {"type", "description", "enum"} or prop.get("type") != expected:
                            raise ValueError("参数 schema 必须与 Forge 工具类型一致")
                    parameters = copy.deepcopy(parameters)
                    parameters["additionalProperties"] = False
                    batch[name] = {"plugin": plugin, "plugin_id": plugin.id, "name": name,
                        "token": (plugin.ack_of(), plugin.generation), "target": target,
                        "capability": capability, "description": description, "parameters": parameters}
                for raw in plugin.contributions.get("skills", []):
                    name, skill_file = raw.get("name"), raw.get("file")
                    if not isinstance(name, str) or len(name) > 64 or not re_fullmatch(name):
                        raise ValueError("技能名称无效")
                    if name in batch or name in tools or name in reserved or name.removeprefix("forge_") in reserved:
                        raise ValueError(f"技能名冲突：{name}")
                    if set(raw) - {"name", "file", "description"}:
                        raise ValueError("技能声明含未知字段")
                    description = raw.get("description", "")
                    if not isinstance(description, str) or len(description) > 4096:
                        raise ValueError("技能描述无效")
                    if SKILL_CAPABILITY not in plugin.declared_capabilities:
                        raise PermissionError(f"技能未声明必要能力：{SKILL_CAPABILITY}")
                    meta = {"plugin": plugin, "plugin_id": plugin.id, "name": name,
                        "token": (plugin.ack_of(), plugin.generation), "target": SKILL_TARGET,
                        "capability": SKILL_CAPABILITY, "description": description,
                        "parameters": copy.deepcopy(_EMPTY_SCHEMA)}
                    if skill_file is not None:
                        # 外部技能包（SKILL.md 随插件落盘）：只允许插件目录内的相对纯文本
                        meta["skill_file"] = _skill_relative_path(plugin, skill_file)
                    else:
                        # 内置目录条目可以不带文件：正文内联在 provides.skill_body，
                        # 安装时随清单一起进指纹，不依赖打包额外资源文件
                        body = plugin.provides.get("skill_body")
                        if (not isinstance(body, str) or not body.strip()
                                or len(body) > MAX_INLINE_SKILL_BODY):
                            raise ValueError("技能缺少 file 或有效的 provides.skill_body 正文")
                        meta["skill_body"] = body
                    batch[name] = meta
                for name, meta in batch.items():
                    registry.register(ToolSpec(name, meta["description"],
                        lambda args, ctx, m=meta: self._execute(m, args, ctx),
                        read_only=meta["capability"] in {"repo.read", SKILL_CAPABILITY},
                        schema=meta["parameters"]))
                    tools[name] = meta
                if batch:
                    report.loaded.append(plugin.id)
                    report.tools.extend(batch)
            except Exception as exc:
                report.errors.append((plugin.id, f"{type(exc).__name__}: {exc}"))
        with self._lock:
            self.tools, self.registry, self.last_report = tools, registry, report
        return report

    def _current(self, meta, catalog=None):
        if catalog is None:
            p = self.market.find(meta["plugin_id"])
        else:
            p = next((c for c in catalog if c.id == meta["plugin_id"]), None)
        if (p is None or not p.installed or not p.enabled or p.error or p.needs_ack
                or p.executes_code or (p.ack_of(), p.generation) != meta["token"]
                or meta["capability"] not in p.granted_capabilities(self.workspace, self.session)):
            raise PermissionError("插件未获当前范围授权，或已禁用、撤销、修改；请检查并重新加载")
        return p

    def _alive(self, meta, catalog):
        try:
            self._current(meta, catalog)
            return True
        except (ValueError, PermissionError, OSError, TimeoutError):
            return False

    def has(self, name):
        with self._lock:
            meta = self.tools.get(name)
        return meta is not None and self._alive(meta, None)

    def names(self, catalog=None):
        with self._lock:
            metas = list(self.tools.items())
        if not metas:
            return []
        # One catalog read for the whole batch: per-tool find() re-fingerprints
        # every installed plugin from disk (O(tools x plugins) IO per refresh).
        catalog = self.market.catalog() if catalog is None else catalog
        return sorted(n for n, meta in metas if self._alive(meta, catalog))

    def openai_schemas(self, catalog=None):
        with self._lock:
            tools = copy.deepcopy(self.tools)
        if not tools:
            return []
        catalog = self.market.catalog() if catalog is None else catalog
        out = []
        for name, meta in tools.items():
            if not self._alive(meta, catalog):
                continue
            if meta["capability"] == SKILL_CAPABILITY:
                note = f"返回技能「{name}」的说明正文；不访问工作区文件"
            else:
                note = f"{meta['capability']}；受 Forge Policy 约束"
            out.append({"type": "function", "function": {"name": name,
                "description": f"{meta['description']} [插件 {meta['plugin_id']}；{note}]",
                "parameters": meta["parameters"]}})
        return out

    def _arguments(self, meta, args):
        if not isinstance(args, dict):
            raise ValueError("arguments 必须是对象")
        blob = json.dumps(args, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(blob) > MAX_REQUEST_BYTES:
            raise ValueError("插件请求超过大小上限")
        schema, arg_types = meta["parameters"], TARGETS[meta["target"]][1]
        if (set(args) - set(schema.get("properties", {})) or
                not set(schema.get("required", [])) <= set(args)):
            raise ValueError("参数不符合声明的 schema")
        for key, value in args.items():
            if type(value) is not arg_types[key]:
                raise ValueError(f"参数类型错误：{key}")
            enums = schema.get("properties", {}).get(key, {}).get("enum")
            if enums is not None and value not in enums:
                raise ValueError(f"参数不在 enum 中：{key}")
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ValueError("工具需要明确文件路径")
        # Reject Windows streams/devices and escapes; absolute paths are canonicalized.
        path = Path(raw_path)
        if any(":" in part for part in path.parts if part != path.anchor):
            raise PermissionError("不允许 NTFS 附加数据流")
        path = (path if path.is_absolute() else self.workspace / path).resolve()
        from forge.secrets import assert_public_path
        assert_public_path(path)
        if not path.is_relative_to(self.workspace):
            raise PermissionError("插件路径超出已授权工作区")
        if path.is_relative_to(self._control_root):
            raise PermissionError("插件不能访问 Forge 的配置、授权、插件和审计目录")
        if meta["capability"] == "repo.write" and (
                any(path == entry for entry in self._host_entrypoints)
                or any(path.is_relative_to(root) for root in self._host_code_roots)):
            raise PermissionError("插件不能改写 Forge 宿主执行代码")
        for part in path.relative_to(self.workspace).parts:
            if part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
                raise PermissionError("不允许设备路径")
        if path.exists():
            info = path.stat()
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    raise PermissionError("插件不能操作可能关联工作区外文件的硬链接")
                if info.st_size > MAX_FILE_BYTES:
                    raise ValueError("文件超过受控插件工具大小上限")
            elif not (stat.S_ISDIR(info.st_mode) and meta["target"] == "list_dir"):
                raise PermissionError("插件只能操作普通文件或列出普通目录")
        if meta["target"] == "list_dir" and path.is_dir():
            # Bound listing work before delegating to the host registry.
            for index, _ in enumerate(path.iterdir()):
                if index >= 2000:
                    raise ValueError("目录超过受控插件工具数量上限")
        if "limit" in args and not 1 <= args["limit"] <= 400:
            raise ValueError("limit 必须介于 1 和 400")
        return {**args, "path": str(path)}

    def _execute(self, meta, args, ctx):
        from forge.tools import ToolResult
        # A retained registry handler is also revocable; call() is not the only gate.
        with market_lock(self.market.market_dir / "state.lock"):
            plugin = self._current(meta)
            if meta["capability"] == SKILL_CAPABILITY:
                return self._execute_skill(meta, plugin, args, ctx)
            arguments = self._arguments(meta, args)
            event = ctx.extras.get("plugin_audit") or {
                "call": uuid.uuid4().hex, "plugin": meta["plugin_id"], "fingerprint": meta["token"][0],
                "tool": meta["name"], "target": meta["target"], "workspace": str(self.workspace), "session": self.session}
            self.market.audit(**event, phase="dispatch", outcome="pending")
            ctx.extras["plugin_execution_possible"] = True
            response = self.dispatch(meta["target"], arguments, plugin_context={
                "id": meta["plugin_id"], "tool": meta["name"], "capability": meta["capability"],
                "fingerprint": meta["token"][0], "workspace": str(self.workspace), "session": self.session})
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise ValueError("工具桥返回结构异常")
        text = json.dumps(response, ensure_ascii=False, allow_nan=False)
        if len(text) > MAX_RESPONSE_CHARS:
            authorization = response.get("meta") or {}
            response = {"ok": response["ok"], "content": str(response.get("content", ""))[:MAX_RESPONSE_CHARS],
                        "error": str(response.get("error", ""))[:1024], "meta": {"truncated": True,
                        "authorization": authorization.get("authorization", "unknown"),
                        "requires_approval": bool(authorization.get("requires_approval"))}}
        return ToolResult(response["ok"], str(response.get("content", "")),
                          str(response.get("error", "")), response.get("meta") or {})

    def _execute_skill(self, meta, plugin, args, ctx):
        """技能正文读取：只回插件包内文件/内联正文，不经网关、不触工作区。"""
        from forge.tools import ToolResult
        if args:
            raise ValueError("技能工具不接受参数")
        if "skill_body" in meta:
            body = meta["skill_body"]
            if len(body) > MAX_RESPONSE_CHARS:
                body = body[:MAX_RESPONSE_CHARS] + "\n…[截断]"
            # 内联正文也过一遍生命周期复核：换包/禁用/撤销即失效
            self._current(meta)
        else:
            body = self._read_skill_body(meta, plugin)
        event = ctx.extras.get("plugin_audit") or {
            "call": uuid.uuid4().hex, "plugin": meta["plugin_id"], "fingerprint": meta["token"][0],
            "tool": meta["name"], "target": meta["target"], "workspace": str(self.workspace),
            "session": self.session}
        self.market.audit(**event, phase="dispatch", outcome="pending")
        return ToolResult(True, body, "", {"skill": meta["name"], "plugin": meta["plugin_id"],
            "fingerprint": meta["token"][0], "capability": SKILL_CAPABILITY,
            "authorization": "allow"})

    def _read_skill_body(self, meta, plugin) -> str:
        root = Path(plugin.path).resolve()
        path = (root / meta["skill_file"]).resolve()
        if not path.is_relative_to(root):
            raise PermissionError("技能文件超出插件目录")
        try:
            info = path.stat()
        except OSError as exc:
            raise ValueError(f"技能文件不可读：{exc}")
        if linked(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise PermissionError("技能文件必须是普通文件")
        if info.st_size > MAX_SKILL_BODY_BYTES:
            raise ValueError("技能正文超过大小上限")
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"技能文件读取失败：{exc}")
        if len(body) > MAX_RESPONSE_CHARS:
            body = body[:MAX_RESPONSE_CHARS] + "\n…[截断]"
        # 读后再核一次生命周期：换包/禁用/撤销会改变 token，立即失效
        self._current(meta)
        return body

    def call(self, name, args):
        from forge.policy import Policy
        from forge.tools import ToolContext
        with self._lock:
            meta, registry = self.tools.get(name), getattr(self, "registry", None)
        invocation = uuid.uuid4().hex
        event = {"call": invocation, "plugin": meta["plugin_id"] if meta else "",
                 "fingerprint": meta["token"][0] if meta else "", "tool": name,
                 "target": meta["target"] if meta else "", "workspace": str(self.workspace),
                 "session": self.session}
        context = ToolContext(Policy(workspace=self.workspace), self.workspace, extras={"plugin_audit": event})
        try:
            # Lifecycle mutation and invocation are linearized. Revoke waits for an
            # already admitted bounded host operation; subsequent calls are denied.
            with market_lock(self.market.market_dir / "state.lock"):
                self.market.audit(**event, phase="attempt", outcome="pending")
                if meta is None:
                    raise PermissionError("未知插件工具")
                self._current(meta)
                if meta["capability"] == SKILL_CAPABILITY:
                    if args:
                        raise ValueError("技能工具不接受参数")
                else:
                    self._arguments(meta, args)
                # This registry gates the alias; dispatch MUST go to the trusted
                # gateway's registry, which applies its real Policy to the target.
                result = registry.invoke(name, args, context)
                if result.meta.get("authorization") in {"deny", "ask"}:
                    context.extras["plugin_execution_possible"] = False
                if not result.ok and context.extras.get("plugin_execution_possible"):
                    result.meta["execution_may_have_completed"] = True
                outcome = ("allow" if result.ok else "deny" if result.meta.get("authorization") == "deny"
                           else "approval_required" if result.meta.get("requires_approval") else "error")
                self.market.audit(**event, phase="result", outcome=outcome,
                                  authorization=result.meta.get("authorization", "unknown"))
                return result.as_dict()
        except Exception as exc:
            try:
                self.market.audit(**event, phase="result",
                    outcome="deny" if isinstance(exc, (PermissionError, ValueError)) else "error",
                    error=type(exc).__name__)
            except (OSError, TimeoutError):
                pass
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "meta": {"execution_may_have_completed": bool(context.extras.get("plugin_execution_possible"))}}
