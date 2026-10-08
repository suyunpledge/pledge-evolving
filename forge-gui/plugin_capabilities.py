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

from plugin_market import Marketplace, market_lock, workspace_key
from plugin_runtime import BUILTIN_TOOL_NAMES, LoadReport, _validate_schema, re_fullmatch

TARGETS = {
    "read_file": ("repo.read", {"path": str, "limit": int}),
    "list_dir": ("repo.read", {"path": str}),
    "write_file": ("repo.write", {"path": str, "content": str, "append": bool}),
    "edit_file": ("repo.write", {"path": str, "old": str, "new": str,
                                  "replace_all": bool, "start_line": int, "end_line": int}),
}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_CHARS = 200_000


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
                for name, meta in batch.items():
                    registry.register(ToolSpec(name, meta["description"],
                        lambda args, ctx, m=meta: self._execute(m, args, ctx),
                        read_only=meta["capability"] == "repo.read", schema=meta["parameters"]))
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
        return [{"type": "function", "function": {"name": name,
                 "description": f"{meta['description']} [插件 {meta['plugin_id']}；{meta['capability']}；受 Forge Policy 约束]",
                 "parameters": meta["parameters"]}} for name, meta in tools.items() if self._alive(meta, catalog)]

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
            self._current(meta)
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
