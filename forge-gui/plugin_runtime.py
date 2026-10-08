"""Enabled, fingerprint-trusted plugin tools with bounded process execution.

register() still returns a list of tool dictionaries. Import/register/execute run
in disposable workers; the host retains metadata and revocable proxies only.
This is a TRUST GATE and process lifetime boundary, NOT a Policy/Sandbox:
trusted Python can import, spawn, read/write host files and access the network.
Declared permissions are disclosures, not OS-enforced capabilities.
"""
from __future__ import annotations

import importlib.util
import hashlib
import math
import inspect
import json
import os
import sys
import shutil
import subprocess
import tempfile
import threading
import time
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from plugin_market import (
    MANIFEST_NAME,
    Marketplace,
    Plugin,
    _as_list,
    content_fingerprint, market_lock,
)
from plugin_worker import ProcessBoundary

#: 单个插件最多声明的工具数（防失控）
MAX_TOOLS_PER_PLUGIN = 32
#: execute 返回的 JSON 尺寸上限（字符串长度）
MAX_RESULT_CHARS = 200_000
#: execute 超时（秒）
EXEC_TIMEOUT = 20.0
LOAD_TIMEOUT = 5.0
MAX_SCHEMA_CHARS = 32_000
# Forge's baseline registry (including bridge-prefixed aliases). Live gateway
# names are additionally supplied by the caller; neither may be shadowed.
BUILTIN_TOOL_NAMES = frozenset({"read_file", "list_dir", "grep", "write_file",
    "delete_file", "edit_file", "apply_patch", "edit_config", "read_range", "file_outline",
    "shell_exec", "tool_search", "skill_list", "spawn_subagent", "memory_recall",
    "web_search", "fetch_url", "datetime", "calculator", "python"})


def _synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def _validate_schema(schema):
    if len(json.dumps(schema, ensure_ascii=False, allow_nan=False)) > MAX_SCHEMA_CHARS:
        raise ValueError("工具 schema 超过大小上限")
    if schema.get("type") != "object":
        raise ValueError("工具 parameters 的 type 必须是 object")
    nodes = 0
    def visit(node, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 512 or not isinstance(node, dict):
            raise ValueError("畸形或过深的工具 schema")
        allowed = {"type", "properties", "required", "items", "additionalProperties",
            "description", "title", "format", "pattern", "enum", "const", "default", "examples",
            "minProperties", "maxProperties", "minLength", "maxLength", "minItems", "maxItems",
            "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
            "anyOf", "allOf", "oneOf", "not", "if", "then", "else", "definitions", "$defs",
            "$schema", "$id", "$comment", "uniqueItems", "readOnly", "writeOnly", "deprecated", "$ref"}
        if set(node) - allowed:
            raise ValueError("工具 schema 含不支持的关键字")
        for key in ("definitions", "$defs"):
            if key in node:
                if not isinstance(node[key], dict):
                    raise ValueError("工具 schema definitions 必须是对象")
                for definition in node[key].values():
                    visit(definition, depth+1)
        for key in ("not", "if", "then", "else"):
            if key in node:
                visit(node[key], depth+1)
        if "examples" in node and not isinstance(node["examples"], list):
            raise ValueError("工具 schema examples 必须是数组")
        for key in ("readOnly", "writeOnly", "deprecated"):
            if key in node and type(node[key]) is not bool:
                raise ValueError(f"工具 schema {key} 必须是布尔值")
        if "$ref" in node:
            raise ValueError("工具 schema 不支持 $ref")
        for key in ("description", "title", "format", "$schema", "$id", "$comment"):
            if key in node and not isinstance(node[key], str):
                raise ValueError(f"工具 schema {key} 必须是字符串")
        for key in ("minProperties", "maxProperties", "minLength", "maxLength", "minItems", "maxItems"):
            if key in node and (type(node[key]) is not int or node[key] < 0):
                raise ValueError(f"工具 schema {key} 必须是非负整数")
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if key in node and (type(node[key]) not in (int, float) or
                                (key == "multipleOf" and node[key] <= 0)):
                raise ValueError(f"工具 schema {key} 数值无效")
        if "pattern" in node:
            import re
            if not isinstance(node["pattern"], str):
                raise ValueError("工具 schema pattern 必须是字符串")
            re.compile(node["pattern"])
        if "uniqueItems" in node and type(node["uniqueItems"]) is not bool:
            raise ValueError("工具 schema uniqueItems 必须是布尔值")
        kind = node.get("type")
        types = kind if isinstance(kind, list) else [kind]
        if kind is not None and (not types or any(t not in
                {"object", "array", "string", "number", "integer", "boolean", "null"}
                for t in types)):
            raise ValueError("工具 schema type 无效")
        props = node.get("properties", {})
        if not isinstance(props, dict):
            raise ValueError("工具 schema properties 必须是对象")
        for key, value in props.items():
            if not isinstance(key, str):
                raise ValueError("工具 schema 属性名必须是字符串")
            visit(value, depth+1)
        required = node.get("required", [])
        if (not isinstance(required, list) or any(not isinstance(s, str) for s in required)
                or len(set(required)) != len(required) or any(s not in props for s in required)):
            raise ValueError("工具 schema required 无效")
        if "items" in node:
            visit(node["items"], depth+1)
        additional = node.get("additionalProperties", True)
        if not isinstance(additional, bool):
            visit(additional, depth+1)
        for key in ("anyOf", "allOf", "oneOf"):
            if key in node:
                if not isinstance(node[key], list) or not node[key]:
                    raise ValueError(f"工具 schema {key} 无效")
                for child in node[key]:
                    visit(child, depth+1)
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise ValueError("工具 schema enum 无效")
    visit(schema)


@dataclass
class LoadedTool:
    """一个已注册的工具：元数据 + 可执行体。"""

    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[dict[str, Any]], Any]
    plugin_id: str
    plugin_dir: Path
    plugin: Plugin
    token: tuple[str, int]

    def openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI function 格式（网关 /v1/tools 同款）。"""
        return json.loads(json.dumps({
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or self.name,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }))


@dataclass
class LoadReport:
    """一轮加载的结果账本（UI/日志直接可用）。"""

    loaded: list[str] = field(default_factory=list)       # plugin ids
    tools: list[str] = field(default_factory=list)        # tool names
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (id, 原因)
    errors: list[tuple[str, str]] = field(default_factory=list)    # (id, 异常)

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_tool_spec(raw: Any, plugin: Plugin, idx: int) -> dict[str, Any]:
    """校验插件 register() 返回的一个 tool spec。不合格直接 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError(f"第 {idx} 个工具不是对象")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"第 {idx} 个工具缺 name")
    if len(name) > 64 or not re_fullmatch(name):
        raise ValueError(f"工具名不合法：{name!r}（只允许字母数字下划线连字符）")
    execute = raw.get("execute")
    if not callable(execute):
        raise ValueError(f"工具 {name} 的 execute 不是可调用对象")
    params = raw.get("parameters")
    if params is not None and not isinstance(params, dict):
        raise ValueError(f"工具 {name} 的 parameters 必须是对象")
    desc = str(raw.get("description") or "")
    if len(desc) > 4096:
        raise ValueError("工具 description 超过上限")
    params = {"type": "object", "properties": {}} if params is None else params
    _validate_schema(params)
    return {
        "name": name,
        "description": desc,
        "parameters": params,
        "execute": execute,
    }


def re_fullmatch(name: str) -> bool:
    return bool(importlib.util and _NAME_RE.fullmatch(name))


_NAME_RE = None  # 延迟编译（见下）


def _compile_name_re():
    global _NAME_RE
    import re as _re
    _NAME_RE = _re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")


_compile_name_re()


def _cap_result(value: Any) -> Any:
    """execute 返回值收敛：JSON 友好 + 尺寸封顶（防一把工具拖爆上下文）。"""
    # Never stringify arbitrary plugin objects; even __str__ may be malicious.
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[截断，原始 {len(text)} 字符]"
        return text
    return value


class PluginRuntime:
    """加载 enabled 插件并汇聚它们的工具。

    用法：
        rt = PluginRuntime(marketplace)
        report = rt.reload()
        schemas = rt.openai_schemas()      # 给模型的 tools 参数
        result  = rt.call("my_tool", {...})
    """

    def __init__(self, marketplace: Marketplace,
                 *, exec_timeout: float = EXEC_TIMEOUT, secret_isolation: bool = True):
        if (type(exec_timeout) not in (int, float) or not math.isfinite(exec_timeout)
                or exec_timeout <= 0):
            raise ValueError("插件执行超时必须是有限正数")
        self.market = marketplace
        self.exec_timeout = exec_timeout
        # Native Python has the process user's filesystem/network authority.
        # Acknowledgement does not grant access to SecretValue. The old runner
        # remains available only to an explicitly trusted host/test harness.
        self.secret_isolation = secret_isolation
        self.tools: dict[str, LoadedTool] = {}
        self.last_report = LoadReport()
        self._lock = threading.RLock()
        self._reserved_names = set(BUILTIN_TOOL_NAMES)

    # ── 加载 ────────────────────────────────────────────────────────

    @_synchronized
    def reload(self, *, reserved_names=()) -> LoadReport:
        """按当前 enabled 状态重载全部插件。失败插件只记账不拖垮别的。"""
        report = LoadReport()
        new_tools = {}
        self._reserved_names = set(BUILTIN_TOOL_NAMES) | set(reserved_names)

        for plugin in self.market.catalog():
            if plugin.installed and plugin.needs_ack:
                report.skipped.append((plugin.id, "插件信任确认缺失/过期"))
                continue
            if not plugin.enabled:
                continue
            if not plugin.installed:
                report.skipped.append((plugin.id, "未安装却标记启用（状态不一致）"))
                continue
            # ack 闸门：没有有效指纹的代码插件根本不 import
            if plugin.needs_ack:
                report.skipped.append(
                    (plugin.id, "声明会执行代码但信任确认缺失/过期"))
                continue
            if not plugin.executes_code:
                # 纯声明式插件：没有代码体，没有可执行工具
                report.skipped.append((plugin.id, "声明式条目（无可执行体）"))
                continue
            if self.secret_isolation:
                report.skipped.append((plugin.id, "Protected Agent 不执行原生 Python 插件；请使用受控声明式能力"))
                continue

            pdir = Path(plugin.path) if plugin.path else (
                self.market.plugins_dir / plugin.id)
            if not pdir.is_dir():
                report.skipped.append((plugin.id, f"目录不存在：{pdir}"))
                continue

            try:
                if plugin.error:
                    raise ValueError(plugin.error)
                tools = self._load_plugin_module(plugin, pdir)
            except Exception as exc:  # noqa: BLE001
                report.errors.append((plugin.id, f"{type(exc).__name__}: {exc}"))
                continue

            if not tools:
                report.skipped.append((plugin.id, "register() 未返回任何工具"))
                continue

            conflicts = [spec["name"] for spec in tools if spec["name"] in new_tools or
                         spec["name"].removeprefix("forge_") in self._reserved_names or
                         spec["name"] in self._reserved_names]
            if conflicts:
                report.errors.append((plugin.id, f"工具名冲突（内置或其他插件）：{', '.join(conflicts)}"))
                continue
            overflow = getattr(self, "_load_overflow", 0)
            token = (plugin.ack_of(), plugin.generation)
            for spec in tools:
                name = spec["name"]
                proxy = lambda args, p=plugin, n=name, t=token, metadata=tools: self._run_job(
                    p, t, "call", name=n, arguments=args, metadata=metadata)
                new_tools[name] = LoadedTool(
                    name=name,
                    description=spec["description"],
                    parameters=spec["parameters"],
                    execute=proxy,
                    plugin_id=plugin.id,
                    plugin_dir=pdir,
                    plugin=plugin,
                    token=token,
                )
                report.tools.append(name)
            if overflow > 0:
                report.skipped.append(
                    (plugin.id, f"超出单插件工具上限 {MAX_TOOLS_PER_PLUGIN}，截掉 {overflow} 个"))
            report.loaded.append(plugin.id)

        self.tools = new_tools
        self.last_report = report
        return report

    def _load_plugin_module(self, plugin: Plugin, pdir: Path) -> list[dict]:
        result = self._run_job(plugin, (plugin.ack_of(), plugin.generation), "load")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ValueError("工作进程返回的工具元数据无效")
        if type(result.get("overflow")) is not int or not 0 <= result["overflow"] <= 1024:
            raise ValueError("工作进程返回的工具数量无效")
        if len(result["tools"]) > MAX_TOOLS_PER_PLUGIN:
            raise ValueError("工作进程返回的工具数量超过上限")
        for idx, metadata in enumerate(result["tools"]):
            if not isinstance(metadata, dict) or set(metadata) != {"name", "description", "parameters"}:
                raise ValueError("工作进程返回的工具元数据不完整")
            if not isinstance(metadata["name"], str) or not isinstance(metadata["description"], str):
                raise ValueError("工作进程返回的工具文本无效")
            if not isinstance(metadata["parameters"], dict):
                raise ValueError("工作进程返回的工具 parameters 无效")
            _validate_tool_spec({**metadata, "execute": lambda args: None}, plugin, idx)
        if len({m["name"] for m in result["tools"]}) != len(result["tools"]):
            raise ValueError("重复注册工具名")
        fingerprint = hashlib.sha256(json.dumps(result["tools"], ensure_ascii=False,
                                                sort_keys=True).encode("utf-8")).hexdigest()
        self.market.record_tool_schema(plugin.id, (plugin.ack_of(), plugin.generation), fingerprint)
        self._load_overflow = result["overflow"]
        return result["tools"]

    def _assert_current(self, plugin, token):
        current = self.market.find(plugin.id)
        if (current is None or not current.installed or not current.enabled or current.error
                or not current.executes_code or current.needs_ack
                or (current.ack_of(), current.generation) != token):
            raise PermissionError("插件已禁用、卸载、修改或信任/生命周期已变化，请重新确认并加载")

    def _run_job(self, plugin, token, action, **payload):
        if self.secret_isolation:
            raise PermissionError("Secret isolation denies native plugin execution")
        if action == "call" and not isinstance(payload.get("arguments"), dict):
            raise ValueError("arguments 必须是对象")
        timeout = LOAD_TIMEOUT if action == "load" else self.exec_timeout
        # Serialize a plugin across conversations, runtime instances and processes.
        with market_lock(self.market.market_dir / "workers" / f"{plugin.id}.lock", timeout=timeout):
            with tempfile.TemporaryDirectory(prefix="forge-plugin-worker-") as tmp:
                root = Path(tmp)
                snapshot = root / "plugin"
                with market_lock(self.market.market_dir / "state.lock"):
                    self._assert_current(plugin, token)
                    shutil.copytree(plugin.path, snapshot, ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".git", "node_modules"))
                    if content_fingerprint(snapshot) != plugin.content_fingerprint:
                        raise PermissionError("插件文件在执行快照创建期间发生变化")
                    self._assert_current(plugin, token)
                request, output = root / "request.json", root / "output.json"
                text = json.dumps({"directory": str(snapshot), "action": action, **payload},
                                  ensure_ascii=False, allow_nan=False)
                if len(text.encode("utf-8")) > 1_500_000:
                    raise ValueError("插件请求超过传输上限")
                request.write_text(text, encoding="utf-8")
                if getattr(sys, "frozen", False):
                    command = [sys.executable, "--forge-plugin-worker", str(request), str(output)]
                else:
                    command = [sys.executable, "-B", str(Path(__file__).with_name("plugin_worker.py")),
                               str(request), str(output)]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt")
                boundary = None
                watchdog = None
                expired = threading.Event()
                try:
                    boundary = ProcessBoundary(process)
                    self._assert_current(plugin, token)
                    # Independent from status-file locks or I/O in the monitor.
                    # A blocked state check must not let the plugin outlive its budget.
                    def expire():
                        expired.set()
                        try:
                            boundary.close()
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                    watchdog = threading.Timer(timeout, expire)
                    watchdog.daemon = True
                    watchdog.start()
                    process.stdin.write(b"1")
                    process.stdin.flush()
                    process.stdin.close()
                    deadline = time.monotonic() + timeout
                    while process.poll() is None:
                        self._assert_current(plugin, token)
                        if expired.is_set() or time.monotonic() >= deadline:
                            raise TimeoutError(f"插件 {action} 超过 {timeout:g}s，工作进程树已终止")
                        time.sleep(.02)
                    if expired.is_set():
                        raise TimeoutError(f"插件 {action} 超过 {timeout:g}s，工作进程树已终止")
                    # Root exit ends this invocation's entire process lifetime.
                    boundary.close()
                    self._assert_current(plugin, token)
                    if process.returncode != 0 or not output.is_file():
                        raise RuntimeError(f"插件工作进程异常退出：{process.returncode}")
                    if output.stat().st_size > 2_000_000:
                        raise ValueError("插件输出超过传输上限")
                    def invalid_constant(value):
                        raise ValueError(f"插件输出包含非法常量：{value}")
                    with output.open("rb") as handle:
                        packet_bytes = handle.read(2_000_001)
                    if len(packet_bytes) > 2_000_000:
                        raise ValueError("插件输出超过传输上限")
                    packet = json.loads(packet_bytes.decode("utf-8"), parse_constant=invalid_constant)
                    if not isinstance(packet, dict) or packet.get("ok") is not True:
                        if isinstance(packet, dict) and packet.get("schema_changed") is True:
                            self.market.revoke_ack(plugin.id)
                        raise RuntimeError(str(packet.get("error", "插件输出无效"))[:4000]
                                           if isinstance(packet, dict) else "插件输出无效")
                    return _cap_result(packet["value"]) if action == "call" else packet["value"]
                finally:
                    if watchdog is not None:
                        watchdog.cancel()
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    if boundary is not None:
                        boundary.close()
                    else:
                        process.kill()
                        process.wait(timeout=3)

    # ── 调用 ────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        if self.secret_isolation:
            return False
        tool = self.tools.get(name)
        if tool is None:
            return False
        try:
            # Proxies validate again on invocation. Discovery also hides ghosts.
            self._assert_current(tool.plugin, tool.token)
            return name.removeprefix("forge_") not in self._reserved_names and name not in self._reserved_names
        except Exception:
            return False

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一个插件工具，返回 {ok, result|error}（网关 /v1/tools/call 风格）。"""
        tool = self.tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知插件工具：{name}"}
        args = {} if arguments is None else arguments
        if not isinstance(args, dict):
            return {"ok": False, "error": "arguments 必须是对象"}
        try:
            if not self.has(name):
                raise PermissionError("插件工具已失效或与内置工具冲突")
            from forge.secrets import redact
            return redact({"ok": True, "result": tool.execute(args)})
        except (TimeoutError, RuntimeError) as exc:
            from forge.secrets import redact
            return {"ok": False, "error": redact(str(exc))}
        except Exception as exc:  # noqa: BLE001
            from forge.secrets import redact
            return {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}

    # ── 导出 ────────────────────────────────────────────────────────

    def openai_schemas(self) -> list[dict[str, Any]]:
        """给模型 messages 里 tools 参数用的 schema 列表。"""
        return [t.openai_schema() for t in list(self.tools.values()) if self.has(t.name)]

    def names(self) -> list[str]:
        return sorted(name for name in list(self.tools) if self.has(name))
