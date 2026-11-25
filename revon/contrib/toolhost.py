"""toolhost —— 工具宿主：把外部工具描述归一成框架内部规格，并挂权限判定。

本模块只暴露两个纯函数：

    register_tools(specs) -> {"tools": [...], "dropped": [...], "deduped": [...]}
    authorize(tool, mode, rules, target_path, workspace) -> {"decision": ..., "reason": ...}

无副作用、无全局状态、仅依赖标准库（os / re / fnmatch）。
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Dict, List, Optional

MODULE_API_VERSION = 1
SELFTEST_CASES = 25


# ---------------------------------------------------------------------------
# 内部常量 & 辅助函数
# ---------------------------------------------------------------------------

# 合法工具名：非空字符串，且只包含 [A-Za-z0-9_.-]
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# 启发式只读前缀（小写匹配）
_READ_PREFIXES = (
    "read", "list", "search", "get", "describe",
    "find", "query", "lookup", "show", "fetch",
    "check", "inspect", "print", "debug",
)

# 所有可能的 mode 值，用于校验 & 兜底
_VALID_MODES = {
    "read-only", "default", "acceptEdits",
    "auto", "dontAsk", "plan", "bypassPermissions",
}


def _is_valid_name(name: Any) -> bool:
    """工具名合法判定。"""
    if not isinstance(name, str):
        return False
    if not name:
        return False
    if " " in name:
        return False
    return bool(_NAME_RE.match(name))


# 特殊只读工具名（启发式覆盖不到但语义明显只读）
_READ_SPECIAL_NAMES = frozenset({"tool_search"})


def _guess_read_only(name: str) -> bool:
    """按名字启发式猜测只读：以 read*/list*/search*/get*/describe* 等开头，
    或命中特殊白名单（如 tool_search）→ True。

    F4-3：MCP 前缀化（``fs__read_file``）不得翻转语义——判定前先剥一层
    ``server__`` 前缀（分隔符冻结为双下划线，契约 §1.6）。
    """
    low = name.lower()
    if "__" in low:
        stripped = low.split("__", 1)[1]
        if stripped:
            low = stripped
    if low in _READ_SPECIAL_NAMES:
        return True
    return low.startswith(_READ_PREFIXES)


def _normalize_schema(spec: Dict[str, Any]) -> Dict[str, Any]:
    """schema 归一：MCP 用 inputSchema，native 用 parameters，都缺则兜底空 object。"""
    for key in ("inputSchema", "parameters"):
        value = spec.get(key)
        if isinstance(value, dict):
            return value
    return {"type": "object", "properties": {}}


def _normalize_source(spec: Dict[str, Any]) -> str:
    src = spec.get("source")
    if isinstance(src, str) and src:
        return src
    # 从形态反推
    if "inputSchema" in spec:
        return "mcp"
    if "parameters" in spec:
        return "native"
    return "unknown"


# ---------------------------------------------------------------------------
# register_tools
# ---------------------------------------------------------------------------

def register_tools(specs: Optional[List[Dict[str, Any]]]) -> Dict[str, List[Any]]:
    """把外部工具描述（MCP 或 native 形态）归一为内部工具规格。

    返回：
        {"tools":    [...],   # 归一后的完整规格（字段齐全）
         "dropped":  [...],   # 被丢弃的条目（含 reason）
         "deduped":  [...]}   # 因重名而被丢弃的条目（保留第一个）
    """
    result: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    deduped: List[Dict[str, Any]] = []
    seen_names: set = set()

    if not specs:
        return {"tools": result, "dropped": dropped, "deduped": deduped}

    for raw in specs:
        if not isinstance(raw, dict):
            dropped.append({"raw": raw, "reason": "entry is not a dict"})
            continue

        name = raw.get("name")
        if not _is_valid_name(name):
            dropped.append({"raw": raw, "reason": f"invalid tool name: {name!r}"})
            continue

        if name in seen_names:
            deduped.append({"raw": raw, "reason": f"duplicate name: {name}"})
            continue
        seen_names.add(name)

        # schema
        schema = _normalize_schema(raw)

        # read_only：显式信号优先，否则启发式。
        # F4-3 接缝：``readOnlyHint`` 是 MCP 原生声明；``read_only`` 是归一化
        # 条目重新入管（如 mcp_bridge.discover 产物）时的权威信号——两者都
        # 比名字启发可信，避免前缀化名字被重新猜测而翻转语义。
        if "readOnlyHint" in raw and isinstance(raw["readOnlyHint"], bool):
            read_only = raw["readOnlyHint"]
        elif isinstance(raw.get("read_only"), bool):
            read_only = raw["read_only"]
        else:
            read_only = _guess_read_only(name)

        # deferred：annotations.deferred
        annotations = raw.get("annotations")
        deferred = False
        if isinstance(annotations, dict) and annotations.get("deferred") is True:
            deferred = True

        entry = {
            "name": name,
            "description": raw.get("description", "") if isinstance(raw.get("description"), str) else "",
            "schema": schema,
            "read_only": read_only,
            "deferred": deferred,
            "source": _normalize_source(raw),
        }
        result.append(entry)

    return {"tools": result, "dropped": dropped, "deduped": deduped}


# ---------------------------------------------------------------------------
# authorize
# ---------------------------------------------------------------------------

def _match_rule(tool: str, patterns: List[str]) -> bool:
    """tool 名是否命中 rules 里的任一 glob 模式。"""
    if not patterns:
        return False
    for p in patterns:
        if not isinstance(p, str):
            continue
        if fnmatch.fnmatchcase(tool, p):
            return True
    return False


def _resolve_target(target_path: Optional[str], workspace: str) -> Optional[str]:
    """把 target_path 解析为绝对、归一化的路径；失败或 None 返回 None。"""
    if not target_path:
        return None
    try:
        if os.path.isabs(target_path):
            abs_path = os.path.abspath(target_path)
        else:
            ws = os.path.abspath(workspace) if workspace else os.getcwd()
            abs_path = os.path.abspath(os.path.join(ws, target_path))
        return os.path.normpath(abs_path)
    except (TypeError, ValueError):
        return None


def _is_inside_workspace(target_abs: str, workspace: str) -> bool:
    """target_abs 是否落在 workspace 目录之内（处理反斜杠、.. 越界）。"""
    try:
        ws_abs = os.path.normpath(os.path.abspath(workspace))
    except (TypeError, ValueError):
        return False
    # 完全相等也算在沙箱内
    if target_abs == ws_abs:
        return True
    return target_abs.startswith(ws_abs + os.sep)


def authorize(
    tool: str,
    mode: str,
    rules: Dict[str, Any],
    target_path: Optional[str],
    workspace: str,
    spec: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """基于规则 + mode 判定单个工具调用是否允许。

    返回 {"decision": "allow"|"ask"|"deny", "reason": "..."}。
    判定顺序严格按题目规定，不可打乱。

    F4-2：``spec`` 是该工具的归一化注册条目（register_tools 产物）；其中的
    ``read_only`` 声明（源头是 MCP readOnlyHint）优先于名字启发式——命名
    不是权限，名字启发只在无声明时兜底。调用方应把 register_tools 的产出
    条目按名字索引后传入，避免"叫 read_* 的写工具"绕过路径沙箱。
    """
    # --- 防御式归一输入 ---------------------------------------------------
    if not isinstance(tool, str) or not tool:
        return {"decision": "deny", "reason": "empty or non-string tool name"}

    # K3-#1：mode 非字符串（如 int）不得抛 AttributeError——契约“不得抛
    # 业务异常”，错误输入走保守兑底而非崩溃。
    if not isinstance(mode, str):
        mode = "default"
    mode = (mode or "default").strip()
    if mode not in _VALID_MODES:
        # 未知 mode 按保守策略：写入→deny、只读→allow
        mode = "default"

    # K3-#2：rules 非 dict（如 truthy list）时 `rules or {}` 防不住，
    # .get 会抛——非 dict 一律按空规则处理。
    if not isinstance(rules, dict):
        rules = {}
    deny_list: List[str] = list(rules.get("deny") or [])
    ask_list: List[str] = list(rules.get("ask") or [])
    allow_list: List[str] = list(rules.get("allow") or [])  # 保留但不影响 deny/ask 的优先级

    # F4-2：显式声明（spec.read_only）优先，名字启发仅兑底——
    # "叫 read_*/list_* 的写工具"不再绕过沙箱，带 server 前缀的只读工具
    # 也不再被误判成写工具。
    if isinstance(spec, dict) and isinstance(spec.get("read_only"), bool):
        read_only = spec["read_only"]
    else:
        read_only = _guess_read_only(tool)

    # --- 1. deny 规则恒优先 ----------------------------------------------
    if _match_rule(tool, deny_list):
        return {"decision": "deny", "reason": f"explicit deny rule matches {tool!r}"}

    # --- 2. 写入类工具的沙箱边界 ------------------------------------------
    if not read_only:
        resolved = _resolve_target(target_path, workspace)
        if resolved is None:
            return {
                "decision": "deny",
                "reason": "write tool with no provably sandboxed target path",
            }
        if not _is_inside_workspace(resolved, workspace):
            return {
                "decision": "deny",
                "reason": f"target path escapes sandbox: {target_path!r}",
            }

    # --- 3. ask 规则 ------------------------------------------------------
    if _match_rule(tool, ask_list):
        return {"decision": "ask", "reason": f"ask rule matches {tool!r}"}

    # --- 4. bypassPermissions 直接放行 ------------------------------------
    if mode == "bypassPermissions":
        return {"decision": "allow", "reason": "bypassPermissions mode"}

    # --- 5. read-only mode：写入一律禁止 ----------------------------------
    if mode == "read-only":
        if not read_only:
            return {"decision": "deny", "reason": "read-only mode blocks write tool"}
        return {"decision": "allow", "reason": "read-only mode allows read tool"}

    # --- 6. 其余 mode -----------------------------------------------------
    if mode in ("default", "auto", "acceptEdits"):
        if not read_only:
            return {"decision": "ask", "reason": f"write tool in {mode} mode"}
        return {"decision": "allow", "reason": f"read tool in {mode} mode"}

    if mode == "dontAsk":
        if not read_only:
            return {"decision": "deny", "reason": "dontAsk mode blocks write tool"}
        return {"decision": "allow", "reason": "dontAsk mode allows read tool"}

    if mode == "plan":
        if read_only or tool == "tool_search":
            return {"decision": "allow", "reason": "plan mode allows read/tool_search"}
        return {"decision": "deny", "reason": "plan mode blocks non-read tool"}

    # 理论上走不到这里（mode 已被归一），兜底保守 deny
    return {"decision": "deny", "reason": f"unhandled mode: {mode}"}


# ---------------------------------------------------------------------------
# revon 契约入口（冻结）
# ---------------------------------------------------------------------------

def register(api: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """revon 注册入口：返回模块元信息 & 钩子映射。"""
    return {
        "name": "toolhost",
        "version": "1.0.0",
        "capabilities": [
            "tools.register_tools",
            "tools.authorize",
        ],
        "hooks": {
            "register_tools": register_tools,
            "authorize": authorize,
        },
        "author": "Trae",
        "summary": "工具宿主：外部工具描述 → 内部规格 + 权限判定",
    }


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest() -> List[tuple[str, bool, str]]:
    """运行 SELFTEST_CASES 条自检，返回 [(name, ok, detail), ...]。
    ok=True 表示该条通过；ok=False 时 detail 给失败原因。"""
    results: List[tuple] = []

    def _ok(name: str, detail: str = "pass"):
        results.append((name, True, detail))

    def _bad(name: str, detail: str):
        results.append((name, False, detail))

    # 平台感知：POSIX 上 "C:\..." 不是绝对路径，会被当相对片段拼进 workspace，
    # #9 的越界用例随之失真（Windows 写法仅在 nt 下成立）。
    if os.name == "nt":
        ws = r"C:\work"
        outside_path = r"C:\other\x.txt"
    else:
        ws = "/w/revon-ws"
        outside_path = "/o/revon-outside.txt"

    # 1. MCP 归一
    specs_mcp = [{
        "name": "read_file", "description": "reads a file",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "readOnlyHint": True, "annotations": {"deferred": True}, "source": "mcp",
    }]
    r = register_tools(specs_mcp); t = r["tools"][0]
    if (t["name"] == "read_file" and t["schema"]["type"] == "object"
            and t["read_only"] is True and t["deferred"] is True and t["source"] == "mcp"):
        _ok("MCP 归一")
    else:
        _bad("MCP 归一", str(t))

    # 2. native 归一
    r = register_tools([{
        "name": "write_file", "description": "writes",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}},
        "source": "native",
    }])
    t = r["tools"][0]
    if t["source"] == "native" and "content" in t["schema"].get("properties", {}):
        _ok("native 归一")
    else:
        _bad("native 归一", str(t))

    # 3. 缺 schema 兜底
    r = register_tools([{"name": "ping", "source": "native"}])
    if r["tools"][0]["schema"] == {"type": "object", "properties": {}}:
        _ok("缺 schema 兜底")
    else:
        _bad("缺 schema 兜底", str(r["tools"][0]["schema"]))

    # 4. 只读启发
    r = register_tools([{"name": "list_dir"}, {"name": "delete_thing"}])
    if r["tools"][0]["read_only"] and not r["tools"][1]["read_only"]:
        _ok("只读启发")
    else:
        _bad("只读启发", f"{r['tools'][0]['read_only']=} {r['tools'][1]['read_only']=}")

    # 5. 非法名丢弃
    r = register_tools([
        {"name": ""}, {"name": "bad name"}, {"name": 123}, {"name": "ok!"}, {"name": "good_name"},
    ])
    if len(r["tools"]) == 1 and r["tools"][0]["name"] == "good_name" and len(r["dropped"]) == 4:
        _ok("非法名丢弃")
    else:
        _bad("非法名丢弃", f"tools={len(r['tools'])} dropped={len(r['dropped'])}")

    # 6. 重名去重
    r = register_tools([
        {"name": "dup", "readOnlyHint": True},
        {"name": "dup", "readOnlyHint": False},
    ])
    if len(r["tools"]) == 1 and r["tools"][0]["read_only"] and len(r["deduped"]) == 1:
        _ok("重名去重")
    else:
        _bad("重名去重", str(r))

    # 7. deferred 透传
    r = register_tools([
        {"name": "a", "annotations": {"deferred": True}},
        {"name": "b", "annotations": {"deferred": False}},
        {"name": "c"},
    ])
    nms = {t["name"]: t for t in r["tools"]}
    if nms["a"]["deferred"] and not nms["b"]["deferred"] and not nms["c"]["deferred"]:
        _ok("deferred 透传")
    else:
        _bad("deferred 透传", str({k: v["deferred"] for k, v in nms.items()}))

    # 8. deny 恒优先
    d = authorize("read_file", "default", {"deny": ["read_*"]}, r"C:\work\a.txt", ws)
    if d["decision"] == "deny":
        _ok("deny 恒优先")
    else:
        _bad("deny 恒优先", str(d))

    # 9. 越界路径被拒（界外绝对路径，随平台取值）
    d = authorize("write_file", "default", {}, outside_path, ws)
    if d["decision"] == "deny":
        _ok("越界路径被拒")
    else:
        _bad("越界路径被拒", str(d))

    # 10. 相对路径正确解析
    d = authorize("write_file", "default", {}, "sub/a.txt", ws)
    if d["decision"] == "ask":
        _ok("相对路径正确解析")
    else:
        _bad("相对路径正确解析", str(d))

    # 11. .. 越界
    d = authorize("write_file", "default", {}, "../hack.txt", ws)
    if d["decision"] == "deny":
        _ok(".. 越界")
    else:
        _bad(".. 越界", str(d))

    # 12. target_path=None 写入 deny
    d = authorize("write_file", "default", {}, None, ws)
    if d["decision"] == "deny":
        _ok("空路径写入 deny")
    else:
        _bad("空路径写入 deny", str(d))

    # 13. ask 规则命中
    d = authorize("list_spicy_things", "default", {"ask": ["list_spicy_*"]}, None, ws)
    if d["decision"] == "ask":
        _ok("ask 规则")
    else:
        _bad("ask 规则", str(d))

    # 14. bypassPermissions 放行写入
    d = authorize("write_file", "default", {}, r"C:\work\x.txt", ws)
    d2 = authorize("write_file", "bypassPermissions", {}, r"C:\work\x.txt", ws)
    if d["decision"] == "ask" and d2["decision"] == "allow":
        _ok("bypassPermissions")
    else:
        _bad("bypassPermissions", f"{d} vs {d2}")

    # 15. read-only mode：写入 deny / 读取 allow
    d = authorize("write_file", "read-only", {}, r"C:\work\x.txt", ws)
    d2 = authorize("read_file", "read-only", {}, r"C:\work\x.txt", ws)
    if d["decision"] == "deny" and d2["decision"] == "allow":
        _ok("read-only mode")
    else:
        _bad("read-only mode", f"write={d} read={d2}")

    # 16. dontAsk：写入 deny / 只读 allow
    d = authorize("write_file", "dontAsk", {}, r"C:\work\x.txt", ws)
    d2 = authorize("read_file", "dontAsk", {}, r"C:\work\x.txt", ws)
    if d["decision"] == "deny" and d2["decision"] == "allow":
        _ok("dontAsk mode")
    else:
        _bad("dontAsk mode", f"write={d} read={d2}")

    # 17. plan：tool_search allow / read allow / write deny
    a = authorize("tool_search", "plan", {}, None, ws)
    b = authorize("read_file", "plan", {}, r"C:\work\x.txt", ws)
    c = authorize("write_file", "plan", {}, r"C:\work\x.txt", ws)
    if a["decision"] == "allow" and b["decision"] == "allow" and c["decision"] == "deny":
        _ok("plan mode")
    else:
        _bad("plan mode", f"tool_search={a} read={b} write={c}")

    # 18. 空输入安全
    try:
        reg_empty = register_tools(None)["tools"] == [] and register_tools([])["tools"] == []
        auth_empty = authorize("x", "default", None, None, ws)["decision"] != "unknown"
        if reg_empty and auth_empty:
            _ok("空输入安全")
        else:
            _bad("空输入安全", f"reg_empty={reg_empty} auth_empty={auth_empty}")
    except Exception as e:
        _bad("空输入安全", f"exception: {e!r}")

    # 19. F4-2：声明即语义——写工具叫 read_* 不得绕过沙箱，声明为只读的
    # 带前缀工具不得被误判成写工具
    d = authorize("read_secrets", "default", {}, None, ws,
                  spec={"name": "read_secrets", "read_only": False})
    d2 = authorize("fs__read_file", "read-only", {}, r"C:\work\x.txt", ws,
                   spec={"name": "fs__read_file", "read_only": True})
    if d["decision"] == "deny" and d2["decision"] == "allow":
        _ok("authorize honours declared read_only")
    else:
        _bad("authorize honours declared read_only", f"write-named-read={d} prefixed-read={d2}")

    # 20. F4-2 兑底：无 spec 时名字启发仍有效（向后兼容）
    d = authorize("read_file", "default", {}, r"C:\work\a.txt", ws)
    d2 = authorize("list_and_wipe", "default", {}, None, ws,
                   spec={"name": "list_and_wipe", "read_only": False})
    if d["decision"] == "allow" and d2["decision"] == "deny":
        _ok("authorize falls back to name heuristic")
    else:
        _bad("authorize falls back to name heuristic", f"heuristic={d} declared-wipe={d2}")

    # 21. F4-3：前缀化名字在无声明时启发不翻转（fs__read_file 仍算只读）
    d = authorize("fs__read_file", "read-only", {}, r"C:\work\x.txt", ws)
    if d["decision"] == "allow":
        _ok("prefix stripped in fallback heuristic")
    else:
        _bad("prefix stripped in fallback heuristic", f"{d}")

    # 22. F4-3：register_tools 采信归一化产物的 read_only 信号（mcp_bridge 透传）
    d = register_tools([{"name": "fs__read_file", "read_only": True}])
    row = (d["tools"] or [{}])[0]
    if row.get("read_only") is True:
        _ok("register_tools honours normalised read_only")
    else:
        _bad("register_tools honours normalised read_only", f"{row}")

    # 23. F2-4：hooks 契约面无死钩子——selftest 是测试入口不是运行时钩子
    manifest = register(None)
    hooks = set(manifest["hooks"])
    if "selftest" not in hooks and hooks == {"register_tools", "authorize"}:
        _ok("hooks surface has no contract-external dead hooks")
    else:
        _bad("hooks surface has no contract-external dead hooks", f"{sorted(hooks)}")

    # 24. K3-#1：mode 非字符串不崩（契约“不得抛业务异常”）
    try:
        d = authorize("read_file", 123, {}, None, ws)
        ok24 = d.get("decision") in ("allow", "ask", "deny")
        detail24 = str(d)
    except Exception as exc:  # noqa: BLE001
        ok24, detail24 = False, f"raised {type(exc).__name__}: {exc}"
    if ok24:
        _ok("authorize(mode=123) 不抛异常")
    else:
        _bad("authorize(mode=123) 不抛异常", detail24)

    # 25. K3-#2：rules 传 truthy list 不崩（`rules or {}` 防不住）
    try:
        d = authorize("read_file", "default", ["deny-x"], None, ws)
        ok25 = d.get("decision") in ("allow", "ask", "deny")
        detail25 = str(d)
    except Exception as exc:  # noqa: BLE001
        ok25, detail25 = False, f"raised {type(exc).__name__}: {exc}"
    if ok25:
        _ok("authorize(rules=list) 不抛异常")
    else:
        _bad("authorize(rules=list) 不抛异常", detail25)

    return results


if __name__ == "__main__":
    rows = selftest()
    passed = sum(1 for _, ok, _ in rows if ok)
    for name, ok, detail in rows:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\nSELF-TEST  {passed}/{len(rows)}  ({SELFTEST_CASES} declared)")
    if passed != len(rows) or len(rows) != SELFTEST_CASES:
        raise SystemExit(1)
