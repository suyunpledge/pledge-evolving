from __future__ import annotations

"""keystone/contrib/hooks.py — 外部脚本钩子的授权白名单、指纹漂移校验与事件分发。

纯函数模块：所有状态（授权记录、脚本当前指纹、钩子返回值）都由调用方以
普通 dict/list 传入，本模块不读写文件、不起进程、不联网、不读系统时钟。

数据约定（在任务书规定字段之外的可选扩展，缺失时一律走安全分支）：
- hook 可带 "current": {"script_mtime": float, "script_sha256": str}，
  表示脚本当前指纹；dispatch 时据此校验授权；也允许把 script_mtime /
  script_sha256 直接平铺在 hook 上。
- hook 可带 "result": {"block": bool, "reason": str}，以声明式数据表示
  钩子的返回（本模块不真的运行 command）；未带则视为 {"block": False}。
- describe 判定「已授权」：hook 带 "approval" dict，或 "approved" is True。
"""

MODULE_API_VERSION = 1

_VERSION = "1.0.0"
_UNNAMED = "<unnamed>"
_NO_EVENT = "<none>"

_DECISION_ALLOW = "allow"
_DECISION_DENY = "deny"
_DECISION_ASK = "ask"


def _name(hook: dict) -> str:
    name = hook.get("name")
    if isinstance(name, str) and name:
        return name
    return _UNNAMED


def authorize_hook(hook: dict, approval: dict | None, current: dict) -> dict:
    """判定单个钩子本次是否允许运行。

    返回 {"decision": "allow"|"deny"|"ask", "reason": str,
          "drift": None|"mtime"|"hash"}。

    判定顺序：
      1. 无授权记录        -> ask
      2. 缺少 script_path  -> deny
      3. 缺少当前指纹      -> deny（无法校验，保守拒绝）
      4. sha256 不一致     -> deny, drift="hash"（mtime 同时漂移也按 hash 判）
      5. 仅 mtime 不一致   -> deny, drift="mtime"
      6. 全部一致          -> allow
    """
    verdict = {"decision": _DECISION_DENY, "reason": "", "drift": None}

    if not isinstance(hook, dict):
        verdict["reason"] = "钩子定义不是合法对象"
        return verdict

    if not isinstance(approval, dict):
        verdict["decision"] = _DECISION_ASK
        verdict["reason"] = "钩子从未授权，首次使用需要显式批准"
        return verdict

    path = hook.get("script_path")
    if not isinstance(path, str) or not path:
        verdict["reason"] = "已授权但钩子缺少 script_path，拒绝运行"
        return verdict

    if not isinstance(current, dict):
        verdict["reason"] = "缺少脚本当前指纹，无法完成授权校验"
        return verdict

    approved_mtime = approval.get("script_mtime_at_approval")
    approved_hash = approval.get("script_sha256")
    current_mtime = current.get("script_mtime")
    current_hash = current.get("script_sha256")

    if not isinstance(approved_hash, str) or not isinstance(current_hash, str) \
            or not approved_hash or not current_hash:
        verdict["reason"] = "缺少脚本 sha256 指纹，无法校验内容是否被篡改"
        return verdict

    # hash 优先：mtime 与 hash 同时漂移时按 hash 判。
    if approved_hash != current_hash:
        verdict["reason"] = "脚本内容指纹变化（sha256 与授权时不一致）"
        verdict["drift"] = "hash"
        return verdict

    if approved_mtime != current_mtime:
        verdict["reason"] = "脚本修改时间变化（mtime 与授权时不一致）"
        verdict["drift"] = "mtime"
        return verdict

    verdict["decision"] = _DECISION_ALLOW
    verdict["reason"] = "授权有效，脚本指纹与授权时一致"
    return verdict


def dispatch(event: str, payload: dict | None, hooks: list[dict],
             approvals: dict) -> dict:
    """按事件分发钩子。

    返回 {"ran": [name...],
          "skipped": [{"name": str, "reason": str}...],
          "blocked": bool, "block_reason": str}。

    规则：
    - 只处理 event 匹配的钩子，其余钩子忽略（不进 ran 也不进 skipped）；
    - 同名钩子只运行第一个，后续同名者进 skipped（reason 标明 duplicate）；
    - authorize_hook 非 allow 的钩子进 skipped、不进 ran；
    - 任一已运行钩子声明 {"block": True} -> blocked=True、记录原因并立即
      停止处理后续钩子；{"block": False} 不会解除已有的阻断。
    """
    result = {"ran": [], "skipped": [], "blocked": False, "block_reason": ""}

    if not isinstance(hooks, list):
        return result
    if not isinstance(approvals, dict):
        approvals = {}

    seen: set[str] = set()

    for hook in hooks:
        if not isinstance(hook, dict):
            result["skipped"].append(
                {"name": _UNNAMED, "reason": "skip: 钩子定义不是合法对象"})
            continue

        if hook.get("event") != event:
            continue

        name = _name(hook)

        if name in seen:
            result["skipped"].append(
                {"name": name, "reason": "skip: 重名钩子，只允许第一个运行"})
            continue
        seen.add(name)

        current = hook.get("current")
        if not isinstance(current, dict):
            current = {
                "script_mtime": hook.get("script_mtime"),
                "script_sha256": hook.get("script_sha256"),
            }

        verdict = authorize_hook(hook, approvals.get(name), current)
        if verdict["decision"] != _DECISION_ALLOW:
            reason = verdict.get("reason") or "未放行"
            drift = verdict.get("drift")
            if drift:
                reason = "%s（drift=%s）" % (reason, drift)
            result["skipped"].append({
                "name": name,
                "reason": "%s: %s" % (verdict["decision"], reason),
            })
            continue

        result["ran"].append(name)

        hook_result = hook.get("result")
        if not isinstance(hook_result, dict) and isinstance(payload, dict):
            # 也允许由调用方在 payload["results"][name] 里声明返回值。
            preset = payload.get("results")
            if isinstance(preset, dict):
                hook_result = preset.get(name)

        if isinstance(hook_result, dict) and hook_result.get("block") is True:
            result["blocked"] = True
            why = hook_result.get("reason")
            result["block_reason"] = (
                why if isinstance(why, str) and why
                else "钩子 '%s' 请求阻断后续处理" % name
            )
            break

    return result


def describe(hooks: list[dict]) -> dict:
    """描述钩子清单。

    返回 {"by_event": {event: [name...]},
          "unapproved": [name...],
          "duplicates": [name...]}。

    重名钩子只保留第一个（进入 by_event），其余每次出现都进 duplicates；
    unapproved 只统计首次出现且未带授权标记的钩子。
    """
    empty = {"by_event": {}, "unapproved": [], "duplicates": []}
    if not isinstance(hooks, list):
        return empty

    by_event: dict[str, list[str]] = {}
    unapproved: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()

    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        name = hook.get("name")
        if not isinstance(name, str) or not name:
            continue

        if name in seen:
            duplicates.append(name)
            continue
        seen.add(name)

        event = hook.get("event")
        event = event if isinstance(event, str) and event else _NO_EVENT
        by_event.setdefault(event, []).append(name)

        approved = hook.get("approved") is True or isinstance(
            hook.get("approval"), dict)
        if not approved:
            unapproved.append(name)

    return {"by_event": by_event, "unapproved": unapproved,
            "duplicates": duplicates}


def register(api) -> dict:
    """加载时调用一次，声明本模块提供的能力与钩子。"""
    return {
        "name": "hooks",
        "version": _VERSION,
        "capabilities": [
            "hooks.authorize",
            "hooks.dispatch",
            "hooks.describe",
        ],
        "hooks": {
            "authorize_hook": authorize_hook,
            "dispatch": dispatch,
            "describe": describe,
        },
        "author": "StepClaw",
        "summary": "外部脚本钩子的显式授权白名单、sha256/mtime 指纹漂移校验与事件阻断分发",
    }


def selftest() -> list[tuple[str, bool, str]]:
    """离线自检，返回 (断言名, 是否通过, 失败说明)。"""
    cases: list[tuple[str, bool, str]] = []

    def check(case_name, probe):
        try:
            ok, detail = probe()
        except Exception as exc:  # 自检本身绝不允许抛出
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        cases.append((case_name, bool(ok), "" if ok else str(detail or "断言失败")))

    base = {
        "name": "h1",
        "event": "post_write",
        "command": "python do.py",
        "script_path": "/s/do.py",
    }
    approval = {
        "approved_at": 1000.0,
        "script_mtime_at_approval": 500.0,
        "script_sha256": "abc123",
        "approved_by": "alice",
    }
    cur_ok = {"script_mtime": 500.0, "script_sha256": "abc123"}
    cur_mtime = {"script_mtime": 501.0, "script_sha256": "abc123"}
    cur_hash = {"script_mtime": 500.0, "script_sha256": "dddd"}
    cur_both = {"script_mtime": 999.0, "script_sha256": "eeee"}

    # 1. 无授权 -> ask
    def c_ask():
        r = authorize_hook(dict(base), None, dict(cur_ok))
        return (r["decision"] == "ask" and r["drift"] is None
                and bool(r["reason"])), r

    # 2. mtime 漂移 -> deny / drift=mtime
    def c_mtime():
        r = authorize_hook(dict(base), dict(approval), dict(cur_mtime))
        return r["decision"] == "deny" and r["drift"] == "mtime", r

    # 3. hash 漂移 -> deny / drift=hash
    def c_hash():
        r = authorize_hook(dict(base), dict(approval), dict(cur_hash))
        return r["decision"] == "deny" and r["drift"] == "hash", r

    # 4. 双漂移按 hash 判
    def c_both():
        r = authorize_hook(dict(base), dict(approval), dict(cur_both))
        return r["decision"] == "deny" and r["drift"] == "hash", r

    # 5. 有授权但路径缺失（缺键 / 空串）-> deny，drift=None
    def c_no_path():
        missing = dict(base)
        del missing["script_path"]
        r1 = authorize_hook(missing, dict(approval), dict(cur_ok))
        r2 = authorize_hook(dict(base, script_path=""), dict(approval),
                            dict(cur_ok))
        return (r1["decision"] == "deny" and r1["drift"] is None
                and r2["decision"] == "deny" and r2["drift"] is None), (r1, r2)

    # 6. 授权且指纹一致 -> allow
    def c_allow():
        r = authorize_hook(dict(base), dict(approval), dict(cur_ok))
        return (r["decision"] == "allow" and r["drift"] is None
                and isinstance(r["reason"], str)), r

    def mk(name, event, result=None, current=None):
        hook = {
            "name": name,
            "event": event,
            "command": "run-" + name,
            "script_path": "/s/%s.sh" % name,
            "current": dict(current) if isinstance(current, dict) else dict(cur_ok),
        }
        if result is not None:
            hook["result"] = result
        return hook

    apps = {"a": dict(approval), "b": dict(approval), "c": dict(approval)}

    # 7. 事件过滤：不匹配的钩子不运行也不记 skipped
    def c_event_filter():
        r = dispatch("commit", {}, [mk("a", "push")], apps)
        return r["ran"] == [] and r["skipped"] == [] and not r["blocked"], r

    # 8. block=True 中断后续钩子
    def c_block_stop():
        hooks = [
            mk("b", "commit", {"block": True, "reason": "策略拒绝"}),
            mk("c", "commit", {"block": False}),
        ]
        r = dispatch("commit", {}, hooks, apps)
        later = [s for s in r["skipped"] if s["name"] == "c"]
        return (r["ran"] == ["b"] and r["blocked"] is True
                and r["block_reason"] == "策略拒绝"
                and "c" not in r["ran"] and not later), r

    # 9. block=False 不解除既有阻断（且先 False 后 True 时阻断照样生效）
    def c_block_false_keeps():
        hooks = [
            mk("b", "commit", {"block": True, "reason": "x"}),
            mk("c", "commit", {"block": False}),
        ]
        r1 = dispatch("commit", {}, hooks, apps)
        r2 = dispatch("commit", {}, [
            mk("a", "commit", {"block": False}),
            mk("b", "commit", {"block": True, "reason": "y"}),
        ], apps)
        return (r1["blocked"] is True and "c" not in r1["ran"]
                and r2["blocked"] is True and r2["ran"] == ["a", "b"]
                and r2["block_reason"] == "y"), (r1, r2)

    # 10. 未授权进 skipped；指纹漂移的已授权钩子同样进 skipped 且标注 drift
    def c_skipped():
        r = dispatch("commit", {}, [mk("u", "commit")], {})
        drifted = dispatch("commit", {},
                           [mk("d", "commit", current=cur_hash)],
                           {"d": dict(approval)})
        ok = (r["ran"] == [] and not r["blocked"]
              and len(r["skipped"]) == 1 and r["skipped"][0]["name"] == "u"
              and r["skipped"][0]["reason"].startswith("ask:"))
        d_entries = [s for s in drifted["skipped"] if s["name"] == "d"]
        ok = ok and bool(d_entries) and d_entries[0]["reason"].startswith(
            "deny:") and "hash" in d_entries[0]["reason"]
        ok = ok and "d" not in drifted["ran"]
        return ok, (r, drifted)

    # 11. 重名：第一个进 by_event，其余进 duplicates
    def c_duplicates():
        d = describe([mk("x", "e"), mk("x", "e"), mk("y", "e")])
        return (d["duplicates"] == ["x"]
                and d["by_event"].get("e") == ["x", "y"]), d

    # 12. 空输入 / None / 畸形输入安全，不抛异常
    def c_empty():
        r1 = authorize_hook(None, None, None)
        r2 = authorize_hook({}, None, {})
        r3 = dispatch(None, None, None, None)
        r4 = dispatch("e", {}, [], {})
        r5 = describe(None)
        r6 = describe([None, 42, {"event": "e"}, {"name": "", "event": "e"}])
        ok = (r1["decision"] in ("allow", "deny", "ask")
              and r2["decision"] == "ask"
              and set(r3) == {"ran", "skipped", "blocked", "block_reason"}
              and r4 == {"ran": [], "skipped": [], "blocked": False,
                         "block_reason": ""}
              and r5 == {"by_event": {}, "unapproved": [], "duplicates": []}
              and r6["by_event"] == {} and r6["unapproved"] == []
              and r6["duplicates"] == [])
        return ok, (r1, r2, r3, r4, r5, r6)

    # 13. describe 的 unapproved 清单与两种授权标记
    def c_describe_unapproved():
        h1 = mk("ok1", "e")
        h1["approval"] = dict(approval)
        h2 = mk("ok2", "e")
        h2["approved"] = True
        h3 = mk("no3", "e")
        d = describe([h1, h2, h3, {"name": "no4", "event": "f"}])
        return d["unapproved"] == ["no3", "no4"], d

    # 14. 没有阻断时，同事件授权钩子按序全部运行，block_reason 为空
    def c_all_ran():
        r = dispatch("e", {}, [
            mk("a", "e", {"block": False}),
            mk("c", "e"),
        ], apps)
        return (r["ran"] == ["a", "c"] and r["skipped"] == []
                and not r["blocked"] and r["block_reason"] == ""), r

    check("无授权返回 ask", c_ask)
    check("mtime 漂移拒绝并标记 drift=mtime", c_mtime)
    check("hash 漂移拒绝并标记 drift=hash", c_hash)
    check("mtime 与 hash 双漂移按 hash 判定", c_both)
    check("有授权但脚本路径缺失拒绝", c_no_path)
    check("授权一致指纹一致放行", c_allow)
    check("dispatch 只运行事件匹配的钩子", c_event_filter)
    check("block=True 中断后续钩子并给出原因", c_block_stop)
    check("block=False 不解除既有阻断", c_block_false_keeps)
    check("未授权与漂移钩子进 skipped 不进 ran", c_skipped)
    check("describe 重名钩子除第一个外进 duplicates", c_duplicates)
    check("空输入与畸形输入安全不抛异常", c_empty)
    check("describe 正确列出未授权钩子", c_describe_unapproved)
    check("无阻断时同事件授权钩子全部运行", c_all_ran)

    return cases
