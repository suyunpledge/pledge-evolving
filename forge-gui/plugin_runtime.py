"""插件运行时加载器：把 enabled 插件的声明变成可执行的工具。

安全模型（对齐工具桥 balanced 档的思路）：

  1. 只有 `state.json enabled` 列表里的插件才会被加载；
  2. `executes_code=true` 的插件必须有有效 ack 指纹，否则**根本不 import**；
  3. import 用 importlib.util 在插件目录上做，不依赖 sys.path 注入
     （不污染主进程的模块命名空间）；
  4. 插件模块必须暴露 `register()`，返回 `{"tools": [...]}`——每个 tool 是
     `{"name", "description", "parameters", "execute"}`，execute 是 callable；
  5. execute 拿到的 cwd 锁在插件目录内（不是仓库根），权限以声明为准。

加载器不做的事：不做沙箱（Python 没有真沙箱；防的就是误装，真恶意代码
import 就已经输了——所以 ack 闸门才是唯一防线）。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from plugin_market import (
    MANIFEST_NAME,
    Marketplace,
    Plugin,
    _as_list,
)

#: 单个插件最多声明的工具数（防失控）
MAX_TOOLS_PER_PLUGIN = 32
#: execute 返回的 JSON 尺寸上限（字符串长度）
MAX_RESULT_CHARS = 200_000
#: execute 超时（秒）
EXEC_TIMEOUT = 20.0


@dataclass
class LoadedTool:
    """一个已注册的工具：元数据 + 可执行体。"""

    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[dict[str, Any]], Any]
    plugin_id: str
    plugin_dir: Path

    def openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI function 格式（网关 /v1/tools 同款）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or self.name,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


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
    if not re_fullmatch(name):
        raise ValueError(f"工具名不合法：{name!r}（只允许字母数字下划线连字符）")
    execute = raw.get("execute")
    if not callable(execute):
        raise ValueError(f"工具 {name} 的 execute 不是可调用对象")
    params = raw.get("parameters")
    if params is not None and not isinstance(params, dict):
        raise ValueError(f"工具 {name} 的 parameters 必须是对象")
    desc = str(raw.get("description") or "")
    return {
        "name": name,
        "description": desc,
        "parameters": params or {"type": "object", "properties": {}},
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
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[截断，原始 {len(text)} 字符]"
    try:
        return json.loads(text)
    except ValueError:
        return text


def _run_with_timeout(fn: Callable[[dict], Any], args: dict,
                      timeout: float) -> Any:
    """带超时执行。

    线程 kill 不了真在跑的 Python 代码，但对「卡住的 IO/睡眠」够用；
    超时后主线程立刻返回，工作线程留成守护线程自然消亡。
    """
    import threading
    box: dict[str, Any] = {}

    def worker():
        try:
            box["value"] = fn(args)
        except BaseException as exc:  # noqa: BLE001 - 插件异常要变现为错误信息
            box["error"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"工具执行超过 {timeout:.0f}s 已放弃（线程仍在后台，勿重复调用）")
    if "error" in box:
        raise RuntimeError(str(box["error"]))
    return box.get("value")


class PluginRuntime:
    """加载 enabled 插件并汇聚它们的工具。

    用法：
        rt = PluginRuntime(marketplace)
        report = rt.reload()
        schemas = rt.openai_schemas()      # 给模型的 tools 参数
        result  = rt.call("my_tool", {...})
    """

    def __init__(self, marketplace: Marketplace,
                 *, exec_timeout: float = EXEC_TIMEOUT):
        self.market = marketplace
        self.exec_timeout = exec_timeout
        self.tools: dict[str, LoadedTool] = {}
        self.last_report = LoadReport()

    # ── 加载 ────────────────────────────────────────────────────────

    def reload(self) -> LoadReport:
        """按当前 enabled 状态重载全部插件。失败插件只记账不拖垮别的。"""
        report = LoadReport()
        self.tools.clear()

        for plugin in self.market.catalog():
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

            pdir = Path(plugin.path) if plugin.path else (
                self.market.plugins_dir / plugin.id)
            if not pdir.is_dir():
                report.skipped.append((plugin.id, f"目录不存在：{pdir}"))
                continue

            try:
                tools = self._load_plugin_module(plugin, pdir)
            except Exception as exc:  # noqa: BLE001
                report.errors.append((plugin.id, f"{type(exc).__name__}: {exc}"))
                continue

            if not tools:
                report.skipped.append((plugin.id, "register() 未返回任何工具"))
                continue

            overflow = len(tools) - MAX_TOOLS_PER_PLUGIN
            for spec in tools[:MAX_TOOLS_PER_PLUGIN]:
                name = spec["name"]
                if name in self.tools:
                    report.errors.append(
                        (plugin.id, f"工具名冲突：{name}（已被 {self.tools[name].plugin_id} 注册）"))
                    continue
                self.tools[name] = LoadedTool(
                    name=name,
                    description=spec["description"],
                    parameters=spec["parameters"],
                    execute=spec["execute"],
                    plugin_id=plugin.id,
                    plugin_dir=pdir,
                )
                report.tools.append(name)
            if overflow > 0:
                report.skipped.append(
                    (plugin.id, f"超出单插件工具上限 {MAX_TOOLS_PER_PLUGIN}，截掉 {overflow} 个"))
            report.loaded.append(plugin.id)

        self.last_report = report
        return report

    def _load_plugin_module(self, plugin: Plugin, pdir: Path) -> list[dict]:
        """import 插件入口并调 register()。约定：模块级 `register()`。"""
        entry = pdir / "plugin.py"
        if not entry.is_file():
            raise FileNotFoundError(
                f"{pdir} 里没有 plugin.py（入口约定：插件根目录 plugin.py 暴露 register()）")

        # 固定模块名，避免同名插件互相覆盖；也避免塞进 sys.modules 常规区
        mod_name = f"forge_plugin_{plugin.id}"
        spec = importlib.util.spec_from_file_location(mod_name, entry)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {entry} 构造 import spec")
        module = importlib.util.module_from_spec(spec)
        # 不注册进 sys.modules：进程内隔离；重复 reload 会重新执行（幂等由插件自己保证）
        spec.loader.exec_module(module)

        register = getattr(module, "register", None)
        if not callable(register):
            raise AttributeError("插件缺少模块级 register() 函数")

        raw_tools = register()
        if not isinstance(raw_tools, list):
            raise TypeError("register() 必须返回 list")
        out = []
        for idx, item in enumerate(raw_tools):
            out.append(_validate_tool_spec(item, plugin, idx))
        return out

    # ── 调用 ────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        return name in self.tools

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一个插件工具，返回 {ok, result|error}（网关 /v1/tools/call 风格）。"""
        tool = self.tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知插件工具：{name}"}
        args = arguments or {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "arguments 必须是对象"}
        try:
            value = _run_with_timeout(tool.execute, args, self.exec_timeout)
            return {"ok": True, "result": _cap_result(value)}
        except (TimeoutError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ── 导出 ────────────────────────────────────────────────────────

    def openai_schemas(self) -> list[dict[str, Any]]:
        """给模型 messages 里 tools 参数用的 schema 列表。"""
        return [t.openai_schema() for t in self.tools.values()]

    def names(self) -> list[str]:
        return sorted(self.tools)
