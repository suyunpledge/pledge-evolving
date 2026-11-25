"""mcp_bridge —— MCP server 生命周期规划、工具发现、健康快照。

本模块暴露三个纯函数，零第三方依赖、无全局状态、无副作用：

    plan_servers(configs, running=None) -> {"start":[...], "stop":[...], "keep":[...], "warnings":[...]}
    discover(server, raw_tools)          -> {"server": str, "tools":[...], "dropped":[...]}
    health(running, now, stale_after=300.0) -> {"ready":[...], "stale":[...], "failed":[...], "unknown":[...]}

冻结契约（CONTRACT §1 六禁令）：
    1. 纯函数，不得读写磁盘/网络
    2. 不得抛业务异常（输入坏 → 返回安全默认 + warnings）
    3. 字段全集必须与契约对齐，缺省字段用 Python 默认值兜底
    4. 不得依赖第三方库
    5. 重名/非法条目只可入 warnings/dropped/deduped，不得中断
    6. 返回值必须可直接 JSON 序列化
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

MODULE_API_VERSION = 1
SELFTEST_CASES = 18  # K3 修复轮：+2（非法 server 名整批 dropped / health 非数字时钟安全）


# ---------------------------------------------------------------------------
# 与 toolhost 共享的判定逻辑（此处独立复制，保持模块自包含）
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_READ_PREFIXES = (
    "read", "list", "search", "get", "describe",
    "find", "query", "lookup", "show", "fetch",
    "check", "inspect", "print", "debug",
)
_READ_SPECIAL = frozenset({"tool_search"})
_VALID_STATUS = {"starting", "ready", "failed"}


def _is_valid_name(name: Any) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if " " in name:
        return False
    return bool(_NAME_RE.match(name))


def _guess_read_only(name: str) -> bool:
    low = name.lower()
    if low in _READ_SPECIAL:
        return True
    return low.startswith(_READ_PREFIXES)


def _normalize_schema(raw: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("inputSchema", "parameters"):
        v = raw.get(key)
        if isinstance(v, dict):
            return v
    return {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# plan_servers
# ---------------------------------------------------------------------------

def plan_servers(
    configs: Optional[List[Dict[str, Any]]],
    running: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, List[str]]:
    """根据期望配置 + 运行快照，产出 start / stop / keep 计划和 warnings。"""
    start: List[str] = []
    stop: List[str] = []
    keep: List[str] = []
    warnings: List[str] = []

    if not isinstance(configs, list) or not configs:
        return {"start": start, "stop": stop, "keep": keep, "warnings": warnings}

    # 建运行态索引：name → running 条目
    run_idx: Dict[str, Dict[str, Any]] = {}
    if isinstance(running, list):
        for r in running:
            if not isinstance(r, dict):
                continue
            n = r.get("name")
            if isinstance(n, str) and n:
                run_idx[n] = r

    seen: set = set()

    for raw in configs:
        if not isinstance(raw, dict):
            warnings.append("config entry is not a dict, skipped")
            continue
        name = raw.get("name")
        if not _is_valid_name(name):
            warnings.append(f"invalid config name: {name!r}")
            continue
        if name in seen:
            warnings.append(f"duplicate server config, keeping first: {name!r}")
            continue
        seen.add(name)

        # enabled 默认 True
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = bool(enabled)

        run = run_idx.get(name)
        run_status: Optional[str] = None
        if run and isinstance(run.get("status"), str):
            run_status = run["status"]

        if run_status == "failed":
            # 允许重试 → 建议 start，并记 warnings
            warnings.append(f"server {name!r} is in 'failed' state, will retry")
            if enabled:
                start.append(name)
            else:
                # 未启用且 failed，建议 stop 清掉脏状态
                stop.append(name)
            continue

        if enabled:
            if run_status == "ready":
                keep.append(name)
            else:
                # 未运行 或 starting（starting 也视作需要明确 start 维持拉起意图）
                start.append(name)
        else:
            # 未 enabled：正在运行则 stop；压根没跑就什么都不做
            if run is not None:
                stop.append(name)

    return {"start": start, "stop": stop, "keep": keep, "warnings": warnings}


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

def discover(
    server: Optional[Dict[str, Any]],
    raw_tools: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """把 MCP server 返回的原始工具列表归一成框架内部规格。"""
    server_name = ""
    if isinstance(server, dict):
        n = server.get("name")
        if isinstance(n, str):
            server_name = n

    tools: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    seen_final: set = set()  # final_name 跨 raw_tools 去重

    # K3-#3：server_name 非法（含空格等）时前缀化必产出非法工具名，下游
    # register_tools 会整批丢弃——在这里前置拦截，整批进 dropped 并写明
    # 根因，而不是把问题遇伏到下一层。
    if server_name and not _is_valid_name(server_name):
        for raw in (raw_tools or []):
            dropped.append({"raw": raw,
                            "reason": f"invalid server name: {server_name!r}"})
        return {"server": server_name, "tools": tools, "dropped": dropped}

    if not raw_tools:
        return {"server": server_name, "tools": tools, "dropped": dropped}

    for raw in raw_tools:
        if not isinstance(raw, dict):
            dropped.append({"raw": raw, "reason": "entry is not a dict"})
            continue

        name = raw.get("name")
        if not _is_valid_name(name):
            dropped.append({"raw": raw, "reason": f"invalid tool name: {name!r}"})
            continue

        # 只读：显式 readOnlyHint 优先，否则启发
        if "readOnlyHint" in raw and isinstance(raw["readOnlyHint"], bool):
            read_only = raw["readOnlyHint"]
        else:
            read_only = _guess_read_only(name)

        # deferred：annotations.deferred
        ann = raw.get("annotations")
        deferred = bool(isinstance(ann, dict) and ann.get("deferred") is True)

        # 跨 server 同名 → 加前缀 server__tool；只要 server_name 有值就加前缀，
        # 分隔符冻结为双下划线（契约 §1.6），符合 _NAME_RE [A-Za-z0-9_.-]。
        final_name = f"{server_name}__{name}" if server_name else name

        # 同 server 内前缀化后仍冲突 → 丢弃后到者，记录 dropped（不静默覆盖）
        if final_name in seen_final:
            dropped.append({"raw": raw, "reason": f"duplicate after prefix: {final_name}"})
            continue
        seen_final.add(final_name)

        tools.append({
            "name": final_name,
            "description": raw.get("description", "") if isinstance(raw.get("description"), str) else "",
            "schema": _normalize_schema(raw),
            "read_only": read_only,
            "deferred": deferred,
        })

    # discover 单次处理一个 server，跨 server 同名靠调用方合并时再处理；
    # 这里已经给每个名字加了 server 前缀，合并天然去重。
    return {"server": server_name, "tools": tools, "dropped": dropped}


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def health(
    running: Optional[List[Dict[str, Any]]],
    now: float,
    stale_after: float = 300.0,
) -> Dict[str, List[str]]:
    """运行态快照 → ready / stale / failed / unknown 四类。"""
    ready: List[str] = []
    stale: List[str] = []
    failed: List[str] = []
    unknown: List[str] = []

    if not isinstance(running, list) or not running:
        return {"ready": ready, "stale": stale, "failed": failed, "unknown": unknown}

    if not isinstance(stale_after, (int, float)) or stale_after <= 0:
        stale_after = 300.0

    # K3-#4：now 非数字（如 None）时不崩——时钟缺失比 TypeError 好：
    # 无法判龄的 starting 一律归 unknown（保守方向，与无 started_at 同道）。
    if not isinstance(now, (int, float)):
        now = 0.0

    for r in running:
        if not isinstance(r, dict):
            continue
        name = r.get("name")
        if not isinstance(name, str) or not name:
            continue

        status = r.get("status")
        if status not in _VALID_STATUS:
            unknown.append(name)
            continue

        if status == "ready":
            ready.append(name)
            continue

        if status == "failed":
            failed.append(name)
            continue

        # status == "starting"：看 started_at 是否超时
        started_at = r.get("started_at")
        if isinstance(started_at, (int, float)):
            age = now - started_at
            if age > stale_after:
                stale.append(name)
            else:
                # 仍在 starting 中、没超时 → 也视作 unknown（未 ready 也未 failed）
                unknown.append(name)
        else:
            # 没有 started_at 无法判定年龄
            unknown.append(name)

    return {"ready": ready, "stale": stale, "failed": failed, "unknown": unknown}


# ---------------------------------------------------------------------------
# revon 契约入口（冻结）
# ---------------------------------------------------------------------------

def register(api: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """revon 注册入口：返回模块元信息 & 钩子映射。"""
    return {
        "name": "mcp_bridge",
        "version": "1.0.0",
        "capabilities": [
            "mcp.plan_servers",
            "mcp.discover",
            "mcp.health",
        ],
        "hooks": {
            "plan_servers": plan_servers,
            "discover": discover,
            "health": health,
        },
        "author": "Trae",
        "summary": "MCP server 生命周期规划、工具发现、健康快照",
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

    # ====== plan_servers ======
    # 1. enabled 未运行 → start
    r = plan_servers([{"name": "a", "command": "x", "enabled": True}], running=[])
    if "a" in r["start"]:
        _ok("enabled 未运行 → start")
    else:
        _bad("enabled 未运行 → start", str(r))

    # 2. 未 enabled 但正在运行 → stop
    r = plan_servers(
        [{"name": "b", "command": "x", "enabled": False}],
        running=[{"name": "b", "pid": 1, "started_at": 0, "status": "ready"}],
    )
    if "b" in r["stop"]:
        _ok("未 enabled 在跑 → stop")
    else:
        _bad("未 enabled 在跑 → stop", str(r))

    # 3. enabled 且 ready → keep
    r = plan_servers(
        [{"name": "c", "command": "x", "enabled": True}],
        running=[{"name": "c", "pid": 2, "started_at": 0, "status": "ready"}],
    )
    if "c" in r["keep"]:
        _ok("ready → keep")
    else:
        _bad("ready → keep", str(r))

    # 4. status=failed → warnings + 建议 start（重试）
    r = plan_servers(
        [{"name": "d", "command": "x", "enabled": True}],
        running=[{"name": "d", "pid": 3, "started_at": 0, "status": "failed"}],
    )
    warn_hit = any("d" in w for w in r["warnings"])
    if warn_hit and "d" in r["start"]:
        _ok("failed → warnings + start")
    else:
        _bad("failed → warnings + start", f"warnings_hit={warn_hit} start={r['start']}")

    # 5. 重名配置 warn + 只保留第一个
    r = plan_servers([
        {"name": "dup", "command": "x", "enabled": True},
        {"name": "dup", "command": "y", "enabled": False},
    ])
    dup_warn = any("duplicate" in w for w in r["warnings"])
    if len(r["start"]) == 1 and "dup" in r["start"] and dup_warn:
        _ok("重名配置 warn + 保第一个")
    else:
        _bad("重名配置 warn + 保第一个", f"{r} dup_warn={dup_warn}")

    # ====== discover ======
    # 6. 只读启发
    r = discover({"name": "s"}, [
        {"name": "list_files", "inputSchema": {}},
        {"name": "delete_file", "inputSchema": {}},
    ])
    tols = {t["name"]: t for t in r["tools"]}
    if tols["s__list_files"]["read_only"] and not tols["s__delete_file"]["read_only"]:
        _ok("只读启发")
    else:
        _bad("只读启发", str({k: v["read_only"] for k, v in tols.items()}))

    # 7. 非法名丢弃
    r = discover({"name": "s"}, [
        {"name": "", "inputSchema": {}},
        {"name": "bad name", "inputSchema": {}},
        {"name": "ok.name", "inputSchema": {}},
    ])
    if len(r["tools"]) == 1 and len(r["dropped"]) == 2:
        _ok("非法名丢弃")
    else:
        _bad("非法名丢弃", f"tools={len(r['tools'])} dropped={len(r['dropped'])}")

    # 8. 跨 server 前缀（双下划线，符合 _NAME_RE）
    r = discover({"name": "filesystem"}, [{"name": "read_file", "inputSchema": {}}])
    name_ok = bool(r["tools"]) and r["tools"][0]["name"] == "filesystem__read_file"
    grammar_ok = _is_valid_name("filesystem__read_file")
    if name_ok and grammar_ok:
        _ok("跨 server __ 前缀")
    else:
        _bad("跨 server __ 前缀", f"name_ok={name_ok} grammar_ok={grammar_ok} name={r['tools'][0]['name'] if r['tools'] else None}")

    # ====== health ======
    # 9. stale 判定
    r = health([{"name": "s1", "status": "starting", "started_at": 0}], now=500.0, stale_after=300.0)
    if "s1" in r["stale"]:
        _ok("stale 判定")
    else:
        _bad("stale 判定", str(r))

    # 10. failed 归类
    r = health([{"name": "s2", "status": "failed", "started_at": 0}], now=100.0)
    if "s2" in r["failed"]:
        _ok("failed 归类")
    else:
        _bad("failed 归类", str(r))

    # 11. ready 归类
    r = health([{"name": "s3", "status": "ready", "started_at": 0}], now=999.0)
    if "s3" in r["ready"]:
        _ok("ready 归类")
    else:
        _bad("ready 归类", str(r))

    # 12. unknown（非法 status + 无 started_at 的 starting）
    r = health(
        [{"name": "u1", "status": "weird", "started_at": 0}, {"name": "u2", "status": "starting"}],
        now=1.0,
    )
    if "u1" in r["unknown"] and "u2" in r["unknown"]:
        _ok("unknown 归类")
    else:
        _bad("unknown 归类", str(r))

    # 13. 空输入安全
    try:
        p1 = plan_servers(None) == {"start": [], "stop": [], "keep": [], "warnings": []}
        p2 = (discover(None, None)["tools"] == [] and discover(None, None)["dropped"] == [])
        p3 = health(None, 0.0) == {"ready": [], "stale": [], "failed": [], "unknown": []}
        if p1 and p2 and p3:
            _ok("空输入安全")
        else:
            _bad("空输入安全", f"plan={p1} discover={p2} health={p3}")
    except Exception as e:
        _bad("空输入安全", f"exception: {e!r}")

    # 14. 显式 readOnlyHint 覆盖启发
    r = discover({"name": "s"}, [{"name": "delete_file", "inputSchema": {}, "readOnlyHint": True}])
    if r["tools"][0]["read_only"]:
        _ok("readOnlyHint 优先")
    else:
        _bad("readOnlyHint 优先", str(r))

    # 15. failed + enabled=False → stop（清脏状态）
    r = plan_servers(
        [{"name": "e", "command": "x", "enabled": False}],
        running=[{"name": "e", "pid": 4, "started_at": 0, "status": "failed"}],
    )
    if "e" in r["stop"]:
        _ok("failed+disabled → stop")
    else:
        _bad("failed+disabled → stop", str(r))

    # 16. 同 server 内前缀化后仍重名 → dropped（不静默覆盖）
    # 构造：两个 raw 原始 name 相同 → 前缀化后必撞（K3 修正注释：原“不同”表述误导）。
    r = discover({"name": "s"}, [
        {"name": "read_file", "inputSchema": {}},
        {"name": "read_file", "inputSchema": {}},  # 前缀化后仍是 s__read_file → 撞
    ])
    dupe_reason = any("duplicate after prefix" in (d.get("reason") or "") for d in r["dropped"])
    if len(r["tools"]) == 1 and len(r["dropped"]) == 1 and dupe_reason:
        _ok("同 server 前缀后重名 → dropped")
    else:
        _bad("同 server 前缀后重名 → dropped",
             f"tools={len(r['tools'])} dropped={len(r['dropped'])} dupe_reason={dupe_reason}")

    # 17. K3-#3：非法 server 名（含空格）→ 整批 dropped 且根因明确，
    # 不再把非法名遇伏给下游 register_tools 静默丢弃。
    r = discover({"name": "my server"}, [
        {"name": "read_file", "inputSchema": {}},
        {"name": "write_file", "inputSchema": {}},
    ])
    bad_server = (len(r["tools"]) == 0 and len(r["dropped"]) == 2
                  and all("invalid server name" in (d.get("reason") or "") for d in r["dropped"]))
    if bad_server:
        _ok("非法 server 名 → 整批 dropped 带根因")
    else:
        _bad("非法 server 名 → 整批 dropped 带根因",
             f"tools={len(r['tools'])} dropped={len(r['dropped'])}")

    # 18. K3-#4：health(now=None) 不崩——非数字时钟归 unknown（保守方向）
    try:
        r = health([{"name": "s1", "status": "starting", "started_at": 0}], now=None)
        ok18 = "s1" in r.get("unknown", [])
        detail18 = str(r)
    except Exception as exc:  # noqa: BLE001
        ok18, detail18 = False, f"raised {type(exc).__name__}: {exc}"
    if ok18:
        _ok("health(now=None) 不崩且归 unknown")
    else:
        _bad("health(now=None) 不崩且归 unknown", detail18)

    return results


if __name__ == "__main__":
    rows = selftest()
    passed = sum(1 for _, ok, _ in rows if ok)
    for name, ok, detail in rows:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\nSELF-TEST  {passed}/{len(rows)}  ({SELFTEST_CASES} declared)")
    if passed != len(rows) or len(rows) != SELFTEST_CASES:
        raise SystemExit(1)
