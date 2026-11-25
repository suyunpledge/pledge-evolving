"""revon.contrib.teams — 多 agent 团队的寻址投递、共享任务推进与预算切分。

三个 hook，全部纯函数、无副作用、不读系统时钟：

  deliver(message, members, tasks=None)   @all / @role:<role> / @task:<id> / 直接点名
  advance(tasks, now, members)            共享任务列表：依赖满足 + 成员存活/预算
  budget_split(total, members)            按 weight 切分预算，绝不超出总额

契约没写死的地方，按「不丢消息、不超总额、不静默通过」取口径：

  * 发件人必须出现在 members 里，否则整条消息 denied —— 防冒充。
  * @all 不投给发件人本人，记入 skipped 并给理由。
  * 先解析寻址再过滤存活：被选中但已死 / 超预算的成员 → skipped（不是 denied）。
  * 直接点名不存在的成员、@role 无匹配、@task 不存在 → denied 并给原因。
  * 依赖未满足的任务既不 ready 也不 blocked（等待中）；assignee 缺失 / 非成员 / 已死 /
    超预算的任务进 blocked 并给原因；deadline 已过也进 blocked。
  * 缺字段、类型不对、None 一律安全降级，不抛异常。
"""

from __future__ import annotations

MODULE_API_VERSION = 1

_ADDRESS_ALL = "@all"
_ADDRESS_ROLE = "@role:"
_ADDRESS_TASK = "@task:"

_STATUSES = ("todo", "doing", "done", "blocked")


# ---------------------------------------------------------------------------
# 内部工具（全部下划线私有；模块只公开 register / selftest）
# ---------------------------------------------------------------------------

def _is_num(value: object) -> bool:
    """bool 不算数字 —— True 被当成权重 1 是最容易踩的坑。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_id(value: object) -> str | None:
    """把 id 归一成非空字符串；None / bool / 空串 / 其它类型 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, int):
        return str(value)
    return None


def _role_of(member: dict) -> str:
    role = member.get("role")
    return role.strip() if isinstance(role, str) else ""


def _member_index(members: object) -> tuple[dict[str, dict], list[dict]]:
    """成员表 → {id: member}。畸形条目进 problems，绝不抛异常。"""
    index: dict[str, dict] = {}
    problems: list[dict] = []
    if not isinstance(members, (list, tuple)):
        if members is not None:
            problems.append({"id": "", "reason": "members is not a list"})
        return index, problems
    for raw in members:
        if not isinstance(raw, dict):
            problems.append({"id": "", "reason": "member entry is not a dict"})
            continue
        mid = _as_id(raw.get("id"))
        if mid is None:
            problems.append({"id": "", "reason": "member entry has no usable id"})
            continue
        if mid in index:
            problems.append({"id": mid, "reason": "duplicate member id; first entry wins"})
            continue
        index[mid] = raw
    return index, problems


def _health(member: dict) -> tuple[bool, str]:
    """成员能否接收工作：alive 为假，或 spend >= budget → 不可用。"""
    alive = member.get("alive")
    if alive is False or alive == 0:
        return False, "member is not alive"
    spend = member.get("spend")
    spend_value = float(spend) if _is_num(spend) else 0.0
    budget = member.get("budget")
    if _is_num(budget) and spend_value >= float(budget):
        return False, f"budget exhausted (spend {spend_value:g} >= budget {float(budget):g})"
    return True, ""


def _parse_address(to: object) -> tuple[str, str]:
    """→ (kind, value)，kind ∈ all / role / task / direct / invalid。"""
    if not isinstance(to, str):
        return "invalid", ""
    text = to.strip()
    if not text:
        return "invalid", ""
    if text == _ADDRESS_ALL:
        return "all", ""
    if text.startswith(_ADDRESS_ROLE):
        role = text[len(_ADDRESS_ROLE):].strip()
        return ("role", role) if role else ("invalid", text)
    if text.startswith(_ADDRESS_TASK):
        task_id = text[len(_ADDRESS_TASK):].strip()
        return ("task", task_id) if task_id else ("invalid", text)
    if text.startswith("@"):
        return "invalid", text
    return "direct", text


def _find_task(tasks: object, task_id: str) -> dict | None:
    if not isinstance(tasks, (list, tuple)):
        return None
    for task in tasks:
        if isinstance(task, dict) and _as_id(task.get("id")) == task_id:
            return task
    return None


def _has_pending_dep(task: dict, status_of: dict[str, str]) -> bool:
    """依赖里有任何一个不是 done（含未知依赖）→ 还在等。"""
    deps = task.get("depends_on")
    if not isinstance(deps, (list, tuple)):
        return False
    for dep in deps:
        dep_id = _as_id(dep)
        if dep_id is None:
            continue
        if status_of.get(dep_id) != "done":
            return True
    return False


# ---------------------------------------------------------------------------
# Hook 1：消息投递（@寻址 + 存活/预算过滤 + 防冒充）
# ---------------------------------------------------------------------------

def deliver(message: dict, members: list[dict], tasks: list[dict] | None = None) -> dict:
    """把一条消息投给成员的子集。

    返回 {"delivered": [member_id...], "skipped": [{"id","reason"}], "denied": [{"id","reason"}]}
    denied 非空 = 整条消息被拒（发件人非成员 / 寻址不成立），此时 delivered 必为空。
    """
    delivered: list[str] = []
    skipped: list[dict] = []
    denied: list[dict] = []

    if not isinstance(message, dict):
        return {
            "delivered": [],
            "skipped": [],
            "denied": [{"id": "", "reason": "message is not a dict"}],
        }

    index, problems = _member_index(members)
    skipped.extend(problems)

    sender = message.get("from")
    sender_id = sender.strip() if isinstance(sender, str) and sender.strip() else None
    if sender_id is None:
        denied.append({"id": "", "reason": "message has no usable sender"})
        return {"delivered": delivered, "skipped": skipped, "denied": denied}
    if sender_id not in index:
        # F2-3 主键冻结兼容：发件人可能用显示名（name）而非 id 自称——
        # 仅当该名字唯一归属某个成员 id 时才接受，避免冒名歧义。
        name_hits = [mid for mid, m in index.items()
                     if isinstance(m, dict) and m.get("name") == sender_id]
        if len(name_hits) == 1:
            sender_id = name_hits[0]
        else:
            denied.append({
                "id": sender_id,
                "reason": "sender is not a member; message denied (anti-spoofing)",
            })
            return {"delivered": delivered, "skipped": skipped, "denied": denied}

    kind, value = _parse_address(message.get("to"))

    if kind == "all":
        targets = list(index)
    elif kind == "role":
        targets = [mid for mid, member in index.items() if _role_of(member) == value]
        if not targets:
            denied.append({"id": f"@role:{value}", "reason": f"no member has role {value!r}"})
            return {"delivered": delivered, "skipped": skipped, "denied": denied}
    elif kind == "task":
        task = _find_task(tasks, value)
        if task is None:
            denied.append({"id": f"@task:{value}", "reason": "no such task"})
            return {"delivered": delivered, "skipped": skipped, "denied": denied}
        assignee = _as_id(task.get("assignee"))
        if assignee is None:
            denied.append({"id": f"@task:{value}", "reason": "task has no assignee"})
            return {"delivered": delivered, "skipped": skipped, "denied": denied}
        targets = [assignee]
    elif kind == "direct":
        if value not in index:
            denied.append({"id": value, "reason": "no such member"})
            return {"delivered": delivered, "skipped": skipped, "denied": denied}
        targets = [value]
    else:
        denied.append({
            "id": _as_id(message.get("to")) or "",
            "reason": "unrecognized address; expected @all, @role:<role>, @task:<id> or a member id",
        })
        return {"delivered": delivered, "skipped": skipped, "denied": denied}

    for mid in targets:
        if mid == sender_id:
            skipped.append({"id": mid, "reason": "sender does not receive its own message"})
            continue
        ok, why = _health(index[mid])
        if not ok:
            skipped.append({"id": mid, "reason": why})
            continue
        delivered.append(mid)

    return {"delivered": delivered, "skipped": skipped, "denied": denied}


# ---------------------------------------------------------------------------
# Hook 2：共享任务列表推进
# ---------------------------------------------------------------------------

def advance(tasks: list[dict], now: float, members: list[dict]) -> dict:
    """算出「现在能开工的任务」「被卡住的任务」「整盘是否做完」。

    返回 {"ready": [task_id...], "blocked": [{"id","reason"}], "complete": bool}
    ready = 依赖已满足、assignee 存活且在预算内、状态为 todo 的任务（按输入顺序）。
    依赖未满足的任务既不 ready 也不 blocked —— 它只是在等。
    """
    now_value = float(now) if _is_num(now) else 0.0
    index, _ = _member_index(members)
    items = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, (list, tuple)) else []

    status_of: dict[str, str] = {}
    for task in items:
        tid = _as_id(task.get("id"))
        if tid is None:
            continue
        status = task.get("status")
        status_of[tid] = status if status in _STATUSES else "todo"

    ready: list[str] = []
    blocked: list[dict] = []
    seen: set[str] = set()

    for task in items:
        tid = _as_id(task.get("id"))
        if tid is None:
            blocked.append({"id": "", "reason": "task has no usable id"})
            continue
        if tid in seen:
            blocked.append({"id": tid, "reason": "duplicate task id; earlier row wins"})
            continue
        seen.add(tid)

        status = status_of[tid]
        if status == "done":
            continue
        if status == "blocked":
            blocked.append({"id": tid, "reason": "task status is 'blocked'"})
            continue

        deadline = task.get("deadline")
        if _is_num(deadline) and float(deadline) <= now_value:
            blocked.append({"id": tid, "reason": f"past deadline ({float(deadline):g} <= {now_value:g})"})
            continue

        assignee = _as_id(task.get("assignee"))
        if assignee is None:
            blocked.append({"id": tid, "reason": "task has no assignee"})
            continue
        if assignee not in index:
            blocked.append({"id": tid, "reason": f"assignee {assignee!r} is not a member"})
            continue
        ok, why = _health(index[assignee])
        if not ok:
            blocked.append({"id": tid, "reason": f"assignee {assignee!r}: {why}"})
            continue

        if _has_pending_dep(task, status_of):
            continue

        if status == "doing":
            continue

        ready.append(tid)

    complete = not blocked and all(status_of[tid] == "done" for tid in status_of)
    return {"ready": ready, "blocked": blocked, "complete": complete}


# ---------------------------------------------------------------------------
# Hook 3：成员预算切分
# ---------------------------------------------------------------------------

def budget_split(total: float, members: list[dict]) -> dict:
    """按成员 weight 切分预算。返回 {"allocation": {...}, "unallocated": amount, "warnings": [...]}。

    不变量：sum(allocation.values()) + unallocated == total（浮点误差内），且分配额绝不超总额。
    weight 缺省 1；非正数 / 非数字 / bool → 按 1 处理并记入 warnings。
    """
    warnings: list[str] = []

    if not _is_num(total):
        if total is not None:
            warnings.append(f"total {total!r} is not a number; treated as 0")
        total_value = 0.0
    else:
        total_value = float(total)
        if total_value < 0:
            warnings.append(f"total {total_value:g} is negative; treated as 0")
            total_value = 0.0

    index, problems = _member_index(members)
    for problem in problems:
        warnings.append(f"member {problem['id'] or '?'}: {problem['reason']}")

    weights: dict[str, float] = {}
    for mid, member in index.items():
        weight = member.get("weight", 1)
        if not _is_num(weight) or float(weight) <= 0:
            warnings.append(f"member {mid}: invalid weight {weight!r}; treated as 1")
            weight = 1
        weights[mid] = float(weight)

    if not weights:
        warnings.append("no usable member; nothing allocated")
        return {"allocation": {}, "unallocated": round(total_value, 6), "warnings": warnings}

    total_weight = sum(weights.values())
    allocation = {mid: round(total_value * w / total_weight, 6) for mid, w in weights.items()}
    allocated = round(sum(allocation.values()), 6)
    if allocated > total_value:
        biggest = max(allocation, key=lambda mid: allocation[mid])
        allocation[biggest] = round(allocation[biggest] - (allocated - total_value), 6)
        allocated = round(sum(allocation.values()), 6)
    unallocated = round(total_value - allocated, 6)
    if unallocated < 0:
        unallocated = 0.0
    return {"allocation": allocation, "unallocated": unallocated, "warnings": warnings}


# ---------------------------------------------------------------------------
# register / selftest
# ---------------------------------------------------------------------------

def register(api) -> dict:
    return {
        "name": "teams",
        "version": "1.0.0",
        "capabilities": [
            "team.deliver",
            "team.address.at",
            "team.tasks.advance",
            "team.budget.split",
        ],
        "hooks": {
            "deliver": deliver,
            "advance": advance,
            "budget_split": budget_split,
        },
        "author": "WorkBuddy (CodeBuddy Code)",
        "summary": "Team messaging with @addressing, shared task-list advancement and budget splitting.",
    }


def selftest() -> list[tuple[str, bool, str]]:
    now = 1_700_000_000.0

    members = [
        {"id": "lead", "role": "planner", "capabilities": ["plan"], "depth": 0,
         "spend": 1.0, "budget": 10.0, "alive": True},
        {"id": "rev", "role": "reviewer", "capabilities": ["review"], "depth": 1,
         "spend": 2.0, "budget": 10.0, "alive": True},
        {"id": "rev2", "role": "reviewer", "capabilities": ["review"], "depth": 1,
         "spend": 10.0, "budget": 10.0, "alive": True},
        {"id": "dead", "role": "coder", "capabilities": ["code"], "depth": 1,
         "spend": 0.0, "budget": 5.0, "alive": False},
    ]
    members_before = [dict(m) for m in members]

    msg = {"from": "lead", "to": "@all", "body": "sync", "thread": "t1"}
    msg_before = dict(msg)

    out_all = deliver(msg, members)
    skipped_ids = [row["id"] for row in out_all["skipped"]]
    aliased = any(row is m for row in out_all["skipped"] for m in members)

    out_role = deliver({"from": "lead", "to": "@role:reviewer", "body": "r", "thread": "t1"}, members)
    out_role_none = deliver({"from": "lead", "to": "@role:ops", "body": "r", "thread": "t1"}, members)
    out_ghost = deliver({"from": "lead", "to": "ghost", "body": "?", "thread": "t1"}, members)
    out_spoof = deliver({"from": "mallory", "to": "@all", "body": "!", "thread": "t1"}, members)
    out_no_msg = deliver(None, None)

    deliver_tasks = [{"id": "T2", "assignee": "rev", "status": "todo",
                      "depends_on": [], "updated_at": now}]
    out_task = deliver({"from": "lead", "to": "@task:T2", "body": "go", "thread": "t1"},
                       members, deliver_tasks)
    out_task_missing = deliver({"from": "lead", "to": "@task:T9", "body": "go", "thread": "t1"},
                               members, deliver_tasks)

    tasks = [
        {"id": "T1", "assignee": "lead", "status": "todo", "depends_on": [], "updated_at": now - 100},
        {"id": "T2", "assignee": "rev", "status": "todo", "depends_on": ["T1"], "updated_at": now - 50},
        {"id": "T3", "assignee": "dead", "status": "todo", "depends_on": [], "updated_at": now - 10},
        {"id": "T4", "assignee": "rev2", "status": "doing", "depends_on": [], "updated_at": now - 10},
        {"id": "T5", "assignee": "rev", "status": "doing", "depends_on": [], "updated_at": now - 5},
        {"id": "T6", "assignee": "rev", "status": "todo", "depends_on": [], "updated_at": now - 5,
         "deadline": now - 1},
    ]
    tasks_before = [dict(t) for t in tasks]

    adv = advance(tasks, now, members)
    blocked_ids = [row["id"] for row in adv["blocked"]]

    adv_dep_met = advance(
        [{"id": "A", "assignee": "lead", "status": "done", "depends_on": [], "updated_at": now},
         {"id": "B", "assignee": "rev", "status": "todo", "depends_on": ["A"], "updated_at": now}],
        now, members)
    adv_done = advance(
        [{"id": "A", "assignee": "lead", "status": "done", "depends_on": [], "updated_at": now},
         {"id": "B", "assignee": "rev", "status": "done", "depends_on": ["A"], "updated_at": now}],
        now, members)
    adv_empty = advance(None, now, None)

    split = budget_split(100.0, [{"id": "a", "weight": 1}, {"id": "b", "weight": 3}])
    split_third = budget_split(100.0, [{"id": "x"}, {"id": "y"}, {"id": "z"}])
    split_bad = budget_split(90.0, [{"id": "a", "weight": 0},
                                    {"id": "b", "weight": "x"},
                                    {"id": "c", "weight": 2}])
    split_empty = budget_split(50.0, [])
    split_none = budget_split("many", None)
    third_sum = sum(split_third["allocation"].values()) + split_third["unallocated"]

    manifest = register(None)

    return [
        ("all-expands-live-members", out_all["delivered"] == ["rev"], str(out_all["delivered"])),
        ("all-excludes-sender", "lead" not in out_all["delivered"] and "lead" in skipped_ids,
         str(skipped_ids)),
        ("role-match-delivers", out_role["delivered"] == ["rev"], str(out_role["delivered"])),
        ("role-no-match-denied", bool(out_role_none["denied"]) and out_role_none["delivered"] == [],
         str(out_role_none["denied"])),
        ("unknown-member-denied", any(r["id"] == "ghost" for r in out_ghost["denied"]),
         str(out_ghost["denied"])),
        ("sender-not-member-denied", bool(out_spoof["denied"]) and out_spoof["delivered"] == [],
         str(out_spoof["denied"])),
        ("dead-member-skipped", "dead" in skipped_ids and "dead" not in out_all["delivered"],
         str(skipped_ids)),
        ("over-budget-member-skipped", "rev2" in skipped_ids, str(skipped_ids)),
        ("task-address-resolves-assignee", out_task["delivered"] == ["rev"], str(out_task)),
        ("unknown-task-address-denied", bool(out_task_missing["denied"]), str(out_task_missing["denied"])),
        ("skipped-rows-are-fresh", not aliased, "skipped rows alias member dicts"),
        ("deliver-input-unchanged", msg == msg_before and members == members_before, ""),
        ("deps-unmet-not-ready", "T2" not in adv["ready"], str(adv["ready"])),
        ("deps-met-ready", adv_dep_met["ready"] == ["B"], str(adv_dep_met)),
        ("dead-assignee-blocked", "T3" in blocked_ids, str(blocked_ids)),
        ("over-budget-assignee-blocked", "T4" in blocked_ids, str(blocked_ids)),
        ("doing-task-not-redispatched", "T5" not in adv["ready"] and "T5" not in blocked_ids,
         str(adv)),
        ("past-deadline-blocked", "T6" in blocked_ids, str(blocked_ids)),
        ("not-complete-while-blocked", adv["complete"] is False, str(adv["complete"])),
        ("all-done-complete", adv_done["complete"] is True and adv_done["ready"] == [],
         str(adv_done)),
        ("advance-input-unchanged", tasks == tasks_before, ""),
        ("empty-input-safe",
         adv_empty == {"ready": [], "blocked": [], "complete": True} and out_no_msg["delivered"] == [],
         str(adv_empty)),
        ("budget-split-basic", split["allocation"] == {"a": 25.0, "b": 75.0}
         and split["unallocated"] == 0.0, str(split)),
        ("budget-split-never-exceeds-total",
         sum(split_third["allocation"].values()) <= 100.0 + 1e-9 and abs(third_sum - 100.0) < 1e-6,
         str(split_third)),
        ("budget-split-invalid-weight-downgraded",
         len(split_bad["warnings"]) >= 2
         and sum(split_bad["allocation"].values()) <= 90.0 + 1e-9, str(split_bad)),
        ("budget-split-empty-returns-all",
         split_empty["allocation"] == {} and split_empty["unallocated"] == 50.0, str(split_empty)),
        ("budget-split-bad-total-safe",
         split_none["allocation"] == {} and split_none["unallocated"] == 0.0, str(split_none)),
        ("manifest-name-ok", manifest["name"] == "teams", str(manifest["name"])),
        ("manifest-has-capabilities", "team.deliver" in manifest["capabilities"],
         str(manifest["capabilities"])),
    ]


__all__ = ["MODULE_API_VERSION", "register", "selftest"]
