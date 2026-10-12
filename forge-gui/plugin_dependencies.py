"""plugin_dependencies.py — 插件 requires 声明的版本约束与依赖检查（零第三方依赖）。

背景（2026-10-12 插件系统专项调查）：清单里的 `requires` 字段此前只进入
指纹（ack_of），从不校验——内置目录里的 {"forge": ">=0.7.0"} 是一句没人
核对的口号。本模块把它变成安装/启用时的硬检查：

    {"forge": ">=0.7.0", "plugins": {"other-id": ">=1.0 <2.0"}}

设计口径：
- 约束只支持比较运算符 >= <= > < == != =；多个约束用空格或逗号 AND 连接。
  ^ ~ 等范围语法明确报"不支持"，fail closed，不猜语义。
- 版本比较：按点分段，数字段数值比较、非数字段 ASCII 比较（比 semver 的
  预发布语义简单，但对目录里 ">=0.7" / "<1.2.3" 这类约束保守正确）。
- requires 的未知键不硬拒（向后兼容已装清单），doctor 里提示即可；
  "forge" / "plugins" 两个已知键出现时必须形状合法，否则安装/启用失败。
- 依赖只要求"已安装且版本满足"，不要求已启用：启用 A 而 A 依赖的 B 处于
  禁用态时，A 的能力加载本来就会跳过 B，不构成安全边界问题。
- 环检测在 check_requirements 内完成（含自依赖）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

#: 单个约束表达式允许的运算符（顺序重要：先匹配长运算符）
_OPS = (">=", "<=", "==", "!=", ">", "<", "=")
#: 约束字符串与版本字符串的长度上限（清单本身另有 256 KiB 总限）
_MAX_TEXT = 64
#: 每条 requires["plugins"] 最多依赖数
_MAX_PLUGIN_DEPS = 16

_VERSION_RE = re.compile(r"^[A-Za-z0-9.+-]+$")
_PLUGIN_ID_RE = re.compile(r"^[\w-]{1,128}$")


class RequirementError(ValueError):
    """requires 声明非法或不满足。安装/启用路径把它当作硬失败。"""


def parse_version(text: Any) -> tuple:
    """把版本字符串解析成可比较的分段元组；非法输入抛 RequirementError。

    "1.2.3" → ((1,),(2,),(3,)) 的简化实现：数字段转 int，其余段保留 str。
    比较规则见 _segment_key：数字段永远小于字符串段（1.2.0 < 1.2.0-beta）。
    """
    if not isinstance(text, str) or not text or len(text) > _MAX_TEXT:
        raise RequirementError(f"版本号无效：{text!r}")
    text = text.strip().lstrip("vV")
    if not _VERSION_RE.fullmatch(text):
        raise RequirementError(f"版本号含非法字符：{text!r}")
    parts = text.split(".")
    if len(parts) > 8 or any(not part for part in parts):
        raise RequirementError(f"版本号无效：{text!r}")
    return tuple(_segment(part) for part in parts)


def _segment(part: str):
    if part.isdigit():
        return (0, int(part), "")          # 数字段：数值序
    return (1, 0, part)                    # 非数字段：字典序，且高于任何数字段


def parse_constraint(text: Any) -> tuple[tuple[str, tuple], ...]:
    """把 ">=0.7 <2.0, !=1.2.3" 解析成 ((op, version), ...)；AND 语义。"""
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT * 2:
        raise RequirementError(f"版本约束无效：{text!r}")
    out = []
    for chunk in re.split(r"[\s,]+", text.strip()):
        if not chunk:
            continue
        for op in _OPS:
            if chunk.startswith(op):
                out.append((op, parse_version(chunk[len(op):])))
                break
        else:
            if chunk[0] in "^~*" or chunk == "latest":
                raise RequirementError(f"不支持的约束语法：{chunk}（只支持比较运算符）")
            # 裸版本 == 该版本（目录里 "0.7.0" 这种写法按精确匹配处理）
            out.append(("==", parse_version(chunk)))
        if len(out) > 8:
            raise RequirementError("单条约束子表达式超过上限")
    if not out:
        raise RequirementError(f"版本约束为空：{text!r}")
    return tuple(out)


def satisfies(version: Any, constraint: Any) -> bool:
    """version 是否满足全部 AND 约束。比较前按零段补齐（1.0 == 1.0.0）。"""
    left = parse_version(version)
    zero = (0, 0, "")
    for op, right in parse_constraint(constraint):
        width = max(len(left), len(right))
        a = left + (zero,) * (width - len(left))
        b = right + (zero,) * (width - len(right))
        if op in ("=", "=="):
            ok = a == b
        elif op == "!=":
            ok = a != b
        elif op == ">=":
            ok = a >= b
        elif op == "<=":
            ok = a <= b
        elif op == ">":
            ok = a > b
        else:
            ok = a < b
        if not ok:
            return False
    return True


def normalize_requires(raw: Any) -> dict:
    """校验 requires 形状，返回 {"forge": str|None, "plugins": {id: str},
    "unknown": [键]}。已知键形状非法直接抛错；未知键原样上报不抛。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RequirementError("requires 必须是对象")
    forge = raw.get("forge")
    if forge is not None:
        if not isinstance(forge, str):
            raise RequirementError('requires.forge 必须是版本约束字符串')
        parse_constraint(forge)
    plugins = raw.get("plugins", {})
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict) or len(plugins) > _MAX_PLUGIN_DEPS:
        raise RequirementError("requires.plugins 必须是 id→约束 的对象（最多 16 项）")
    for pid, spec in plugins.items():
        if not isinstance(pid, str) or not _PLUGIN_ID_RE.fullmatch(pid):
            raise RequirementError(f"requires.plugins 依赖 id 非法：{pid!r}")
        if not isinstance(spec, str):
            raise RequirementError(f"requires.plugins[{pid}] 必须是版本约束字符串")
        parse_constraint(spec)
    return {"forge": forge, "plugins": dict(plugins),
            "unknown": sorted(k for k in raw if k not in ("forge", "plugins"))}


def check_requirements(plugin, installed: Mapping[str, Any],
                       forge_version: str, *,
                       requires_graph: Mapping[str, Any] | None = None) -> list[str]:
    """对一个插件做完整依赖检查，返回问题列表（空 = 通过）。

    plugin: 带 id/version/requires 属性的对象（Plugin 或测试替身）。
    installed: 已安装插件 id → version。
    forge_version: 当前 Forge 版本（forge.__version__）。
    requires_graph: 参与环检测的已装插件 id → requires 原始声明（默认空，
        只查直接依赖与自依赖，不做跨插件环检测）。
    """
    try:
        spec = normalize_requires(plugin.requires)
    except RequirementError as exc:
        return [f"requires 声明非法：{exc}"]
    problems: list[str] = []
    if spec["forge"] is not None and forge_version:
        if not satisfies(forge_version, spec["forge"]):
            problems.append(f"需要 Forge {spec['forge']}，当前 {forge_version}")
    # forge_version 为空（异常环境）时跳过 forge 约束比对：
    # 不拦安装（0.0.0 会把所有带约束条目拦死），由 doctor 单独提示版本未知
    deps = spec["plugins"]
    for dep_id, constraint in sorted(deps.items()):
        if dep_id == plugin.id:
            problems.append(f"依赖自身（{dep_id}），构成依赖环")
            continue
        version = installed.get(dep_id)
        if version is None:
            problems.append(f"依赖的插件未安装：{dep_id} {constraint}")
        elif not satisfies(version, constraint):
            problems.append(f"依赖 {dep_id} {constraint} 不满足：已装 {version}")
    problems.extend(_cycle_problems(plugin.id, plugin.requires,
                                    requires_graph or {}))
    return problems


def _cycle_problems(root: str, root_requires: Any,
                    requires_graph: Mapping[str, Any]) -> list[str]:
    """在 requires_graph 加上 root 自身依赖构成的图里，找经过 root 的环。

    只报告回到 root 的环（安装/启用 root 时其余插件间的既有环是 doctor 的
    事）。递归深度上限 16，防御畸形大图。
    """
    def neighbors(pid: str) -> dict:
        raw = root_requires if pid == root else requires_graph.get(pid)
        if not raw:
            return {}
        try:
            return normalize_requires(raw)["plugins"]
        except RequirementError:
            return {}

    done: set[str] = set()

    def visit(path: list[str]) -> list[str]:
        current = path[-1]
        for dep in sorted(neighbors(current)):
            if dep == root:
                return [f"依赖环：{' → '.join(path + [root])}"]
            if dep in path or dep in done or len(path) >= 16:
                continue
            found = visit(path + [dep])
            if found:
                return found
        done.add(current)
        return []

    return visit([root])


def format_problems(plugin_id: str, problems: Iterable[str]) -> str:
    return f"插件 {plugin_id} 依赖检查失败：" + "；".join(problems)
