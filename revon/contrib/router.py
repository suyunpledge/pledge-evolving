# -*- coding: utf-8 -*-
"""revon.contrib.router —— 派单路由：把「任务该派给谁」做成可验证的纯函数。

按能力标签、成本档、权限档、健康状态与配额从候选 worker 里选出可派发者，
给出确定性的降级顺序。全部输入输出都是普通 dict / list：零第三方依赖、
不做任何 I/O、同样的输入永远得到同样的输出，字段缺失或畸形一律按保守
默认处理，绝不向外抛异常。

输入形状（缺字段按保守默认处理）::

    task   = {"id": "T-1", "requires": ["code-review"], "maxCost": "cheap",
              "needsWrite": False, "estTokens": 1200}
    worker = {"name": "reviewer-1", "capabilities": ["code-review"],
              "cost": "cheap", "permission": "workspace-write",
              "healthy": True, "spend": 12.0, "budget": 100.0}
    limits = {"globalBudget": 500.0, "globalSpend": 380.0}

对外只暴露五个入口：

- :func:`match`         task × workers → 派发顺序 + 逐个拒绝原因 + 全局约束。
- :func:`estimate`      task × worker → 预估 token / 成本档 / 是否还有预算。
- :func:`fallback_plan` 派发顺序 → 逐个尝试的降级计划（末项标注 exhausted）。
- :func:`register`      宿主注册入口（name / version / capabilities / hooks）。
- :func:`selftest`      闸门自检入口，逐条返回 ``(断言名, 是否通过, 失败说明)``。

规则速记：不健康 / 配额耗尽（spend ≥ budget）/ 成本超 maxCost / 能力不全 /
needsWrite 但权限不足 → 进 ``rejected`` 并给一条原因（按此顺序取第一条）；
通过者按 ``(成本档升序, 名字)`` 排序；全局预算耗尽 → ``order`` 清空且
``constrained_by`` 含 ``"globalBudget"``。

直接运行本文件可执行内置自检::

    python revon/contrib/router.py
"""

from __future__ import annotations

MODULE_API_VERSION = 1

__all__ = ["match", "estimate", "fallback_plan", "register", "selftest"]

# estTokens 缺失（或非法）时的默认预估。
_DEFAULT_TOKENS = 2000

# 成本档从低到高；未知档位按「最贵」处理（有 maxCost 时必被拒，无上限时排最后）。
_COST_RANK = {"free": 0, "cheap": 1, "standard": 2, "premium": 3}

# 权限档从低到高；未知档位按「最保守」处理（等同只读）。
_PERM_RANK = {"read-only": 0, "workspace-write": 1, "full-access": 2}
_WRITE_FLOOR = _PERM_RANK["workspace-write"]

# 拒绝原因文案；自检只断言关键词，不锁全文。
_R_UNHEALTHY = "worker 不健康（healthy=False）"
_R_QUOTA = "配额已耗尽（spend ≥ budget）"
_R_COST = "成本档 {cost} 高于任务上限 {max}"
_R_CAPS = "缺少能力：{missing}"
_R_PERM = "任务需要写入，但 worker 权限不足（permission={perm}）"
_R_BAD = "worker 数据非法（{why}）"


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _is_int(value) -> bool:
    """是 int 且不是 bool（Python 里 bool 是 int 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _num(value, default: float = 0.0) -> float:
    return float(value) if _is_num(value) else default


def _cost_rank(cost) -> int:
    """成本档 → 序数；未知档位视为最贵（排在所有已知档之后）。"""
    if isinstance(cost, str) and cost in _COST_RANK:
        return _COST_RANK[cost]
    return len(_COST_RANK)


def _perm_rank(permission) -> int:
    """权限档 → 序数；未知档位按最保守的只读处理。"""
    if isinstance(permission, str) and permission in _PERM_RANK:
        return _PERM_RANK[permission]
    return _PERM_RANK["read-only"]


def _anon_name(index: int) -> str:
    return f"<worker#{index}>"


def _global_budget(limits) -> tuple[float | None, float]:
    """读全局预算，返回 ``(globalBudget 或 None, globalSpend)``。

    ``globalBudget`` 缺失/非法 → 不设限（返回 None）；``globalSpend``
    缺失按 0 计，负数按 0 计。
    """
    if not isinstance(limits, dict):
        return None, 0.0
    budget = limits.get("globalBudget")
    spend = max(0.0, _num(limits.get("globalSpend"), 0.0))
    if not _is_num(budget):
        return None, spend
    return float(budget), spend


def _judge(task: dict, row, index: int) -> tuple[str, bool, str]:
    """单个 worker 的准入判定，返回 ``(主键, 是否通过, 拒绝原因)``。

    F2-3 主键冻结：``id`` 为主键（与 teams.deliver 成员寻址同一键空间），
    ``name`` 只作显示与排序；两者都缺 → 判非法；只给 name → 回落 name
    （legacy 名册兼容）。命中即拒，检查顺序固定：
    数据非法 → 健康 → 配额 → 成本上限 → 能力 → 写权限。
    """
    if not isinstance(row, dict):
        return _anon_name(index), False, _R_BAD.format(why="非 dict")
    key = row.get("id")
    if not (isinstance(key, str) and key):
        key = row.get("name")
        if not (isinstance(key, str) and key):
            return _anon_name(index), False, _R_BAD.format(why="缺 id/name")
    if row.get("healthy") is False:
        return key, False, _R_UNHEALTHY
    budget = row.get("budget")
    if _is_num(budget) and _num(row.get("spend"), 0.0) >= budget:
        return key, False, _R_QUOTA
    max_cost = task.get("maxCost")
    if isinstance(max_cost, str) and max_cost in _COST_RANK:
        if _cost_rank(row.get("cost")) > _COST_RANK[max_cost]:
            shown = row.get("cost")
            shown = shown if isinstance(shown, str) and shown else "未知"
            return key, False, _R_COST.format(cost=shown, max=max_cost)
    requires = task.get("requires")
    wanted = ([r for r in requires if isinstance(r, str)]
              if isinstance(requires, (list, tuple)) else [])
    caps = row.get("capabilities")
    have = ({c for c in caps if isinstance(c, str)}
            if isinstance(caps, (list, tuple, set)) else set())
    missing = list(dict.fromkeys(r for r in wanted if r not in have))
    if missing:
        return key, False, _R_CAPS.format(missing=", ".join(missing))
    if task.get("needsWrite"):
        if _perm_rank(row.get("permission")) < _WRITE_FLOOR:
            perm = row.get("permission")
            shown = perm if isinstance(perm, str) else repr(perm)
            return key, False, _R_PERM.format(perm=shown)
    return key, True, ""


# --------------------------------------------------------------------------- #
# 三个纯函数
# --------------------------------------------------------------------------- #

def match(task: dict, workers: list[dict], limits: dict | None = None) -> dict:
    """从候选 worker 里挑出能接这个任务的，并给出派发顺序。

    返回::

        {"order": [worker_name, ...],
         "rejected": [{"name": ..., "reason": ...}, ...],
         "constrained_by": ["globalBudget", ...]}

    逐个 worker 依次检查（命中即拒，原因取第一条）：不健康 → 配额耗尽
    （spend ≥ budget）→ 成本超 maxCost → 能力不全 → needsWrite 但权限不足。
    通过者按 ``(成本档升序, 名字)`` 排序。``limits`` 里 globalSpend ≥
    globalBudget 视为全局预算耗尽：``order`` 清空、``constrained_by`` 记
    ``["globalBudget"]``；此时合格者不进 ``rejected``（那是全局约束，
    不是个体淘汰）。任何字段缺失/畸形都不抛异常。

    F2-3 主键冻结：worker 以 ``id`` 为主键（与 teams.deliver 的成员寻址
    同一键空间），``name`` 只作显示与排序。两个键都缺 → 判非法；只给
    ``name`` → 派单回落到 name（legacy 名册兼容，排序语义不变）。
    """
    task = task if isinstance(task, dict) else {}
    rows = workers if isinstance(workers, (list, tuple)) else []

    rejected: list[dict] = []
    passing: list[tuple[str, dict]] = []
    for i, row in enumerate(rows):
        name, ok, reason = _judge(task, row, i)
        if ok:
            passing.append((name, row))
        else:
            rejected.append({"name": name, "reason": reason})

    passing.sort(key=lambda item: (_cost_rank(item[1].get("cost")), item[0]))
    order = [name for name, _ in passing]

    constrained: list[str] = []
    budget, spend = _global_budget(limits)
    if budget is not None and spend >= budget:
        order = []
        constrained.append("globalBudget")
    return {"order": order, "rejected": rejected, "constrained_by": constrained}


def estimate(task: dict, worker: dict) -> dict:
    """预估一个任务在某个 worker 上的开销与预算余量。

    返回 ``{"tokens": int, "costTier": str, "withinBudget": bool}``。
    ``estTokens`` 缺失或非法按 2000 计（显式 0 有效）；``budget`` 缺失按 0
    计，因此「预算为 0 或负」以及「spend 已达 budget」都判
    ``withinBudget=False``；成本档未知时 ``costTier`` 返回 ``"unknown"``。
    """
    task = task if isinstance(task, dict) else {}
    worker = worker if isinstance(worker, dict) else {}
    tokens = task.get("estTokens")
    if not _is_int(tokens) or tokens < 0:
        tokens = _DEFAULT_TOKENS
    cost = worker.get("cost")
    tier = cost if isinstance(cost, str) and cost in _COST_RANK else "unknown"
    budget = worker.get("budget")
    within = bool(_is_num(budget) and budget > 0
                  and _num(worker.get("spend"), 0.0) < budget)
    return {"tokens": int(tokens), "costTier": tier, "withinBudget": within}


def fallback_plan(order: list[str], workers: list[dict]) -> list[dict]:
    """把派发顺序展开成逐个尝试的降级计划。

    返回 ``[{"attempt": 1, "worker": name, "on": "retryable"}, ...]``：
    上一项失败且失败属于可重试类时才前进到下一项；最后一项已是计划尽头，
    标注 ``"on": "exhausted"``（只有一项时也标 exhausted）。``order`` 里
    不在 ``workers`` 名册上的名字会被剔除，重复名字只保留首次出现
    （对同一 worker 原样重试不构成降级）。
    """
    known: set[str] = set()
    if isinstance(workers, (list, tuple)):
        for row in workers:
            if isinstance(row, dict):
                key = row.get("id")
                if isinstance(key, str) and key:
                    known.add(key)
                elif not key:
                    # legacy 兼容：无 id 的名册允许 name 进 known
                    key = row.get("name")
                    if isinstance(key, str) and key:
                        known.add(key)
    seq: list[str] = []
    if isinstance(order, (list, tuple)):
        for name in order:
            if isinstance(name, str) and name in known and name not in seq:
                seq.append(name)
    total = len(seq)
    return [
        {"attempt": i + 1, "worker": name,
         "on": "exhausted" if i == total - 1 else "retryable"}
        for i, name in enumerate(seq)
    ]


# --------------------------------------------------------------------------- #
# 宿主注册入口
# --------------------------------------------------------------------------- #

def register(api) -> dict:
    """向宿主注册本 contrib 模块。

    ``api`` 为宿主注入的注册上下文，本模块是纯函数库，目前不依赖它。
    ``hooks`` 的键名与任务书里的函数名一一对应。
    """
    return {
        "name": "router",
        "version": "1.0.0",
        "capabilities": ["router.match", "router.estimate", "router.fallback_plan"],
        "hooks": {"match": match, "estimate": estimate,
                  "fallback_plan": fallback_plan},
        "author": "ZCode",
        "summary": "把「任务该派给谁」做成纯函数：按能力/成本/权限/健康/配额选 worker，附降级计划。",
    }


def selftest() -> list[tuple[str, bool, str]]:
    """闸门自检入口：跑与 :func:`_selftest` 相同的 13 条判定，逐条给出结论。

    返回 ``[(断言名, 是否通过, 失败说明), ...]``；通过项的失败说明为空串。
    任何异常（不止 AssertionError）都记为该条失败，不向外抛。
    """
    def w(name, *, caps=("code-review",), cost="cheap", perm="workspace-write",
          healthy=True, spend=0.0, budget=100.0):
        return {"name": name, "capabilities": list(caps), "cost": cost,
                "permission": perm, "healthy": healthy,
                "spend": spend, "budget": budget}

    def t(**kw):
        task = {"id": "T-1", "requires": ["code-review"], "maxCost": "standard",
                "needsWrite": False, "estTokens": 1000}
        task.update(kw)
        return task

    checks: list[tuple[str, object]] = []

    def case(name):
        def wrap(fn):
            checks.append((name, fn))
            return fn
        return wrap

    @case("能力过滤：能力不全的进 rejected 并点名缺失项，能力足的进 order")
    def _():
        workers = [w("ok", caps=("code-review", "security")),
                   w("half", caps=("code-review",)),
                   w("none", caps=())]
        r = match(t(requires=["code-review", "security"]), workers)
        assert r["order"] == ["ok"], r["order"]
        rej = {x["name"]: x["reason"] for x in r["rejected"]}
        assert set(rej) == {"half", "none"}, rej
        assert "security" in rej["half"], rej["half"]
        assert "code-review" in rej["none"], rej["none"]
        r2 = match(t(requires=[]), workers)  # 任务不要求能力时人人可过
        assert r2["order"] == ["half", "none", "ok"], r2["order"]
        assert r2["rejected"] == [], r2["rejected"]

    @case("成本上限：高于 maxCost 剔除并说明档位；maxCost 缺省不设限")
    def _():
        workers = [w("wf", cost="free"), w("wc", cost="cheap"),
                   w("ws", cost="standard"), w("wp", cost="premium")]
        r = match(t(maxCost="cheap"), workers)
        assert r["order"] == ["wf", "wc"], r["order"]
        rej = {x["name"]: x["reason"] for x in r["rejected"]}
        assert set(rej) == {"ws", "wp"}, rej
        assert all("成本" in v for v in rej.values()), rej
        r2 = match(t(maxCost=None), workers)
        assert r2["order"] == ["wf", "wc", "ws", "wp"], r2["order"]
        assert r2["rejected"] == [], r2["rejected"]

    @case("只读拒绝写入任务；任务不需要写时只读 worker 可用")
    def _():
        workers = [w("ro", perm="read-only", cost="free"),
                   w("ws", perm="workspace-write"),
                   w("fa", perm="full-access")]
        r = match(t(needsWrite=True), workers)
        assert r["order"] == ["fa", "ws"], r["order"]  # 同档按名，fa < ws
        rej = {x["name"]: x["reason"] for x in r["rejected"]}
        assert set(rej) == {"ro"} and "写入" in rej["ro"], rej
        r2 = match(t(needsWrite=False), workers)
        assert r2["order"] == ["ro", "fa", "ws"], r2["order"]  # free 先，同档按名
        assert r2["rejected"] == [], r2["rejected"]

    @case("配额耗尽剔除（spend ≥ budget，含 budget=0）")
    def _():
        workers = [w("fresh", spend=0.0, budget=100.0),
                   w("part", spend=99.9, budget=100.0),
                   w("full", spend=100.0, budget=100.0),
                   w("over", spend=120.0, budget=100.0),
                   w("nobudget", spend=0.0, budget=0.0)]
        r = match(t(), workers)
        assert r["order"] == ["fresh", "part"], r["order"]
        rej = {x["name"]: x["reason"] for x in r["rejected"]}
        assert set(rej) == {"full", "over", "nobudget"}, rej
        assert all("配额" in v for v in rej.values()), rej
        assert r["constrained_by"] == [], r["constrained_by"]

    @case("不健康剔除（healthy=False）")
    def _():
        workers = [w("good"), w("sick", healthy=False)]
        r = match(t(), workers)
        assert r["order"] == ["good"], r["order"]
        rej = {x["name"]: x["reason"] for x in r["rejected"]}
        assert set(rej) == {"sick"}, rej

    @case("排序：成本档升序、同档按名字；与输入顺序无关且可复现")
    def _():
        pool = [w("beta"), w("zed", cost="free"),
                w("alpha"), w("mid", cost="standard")]
        r1 = match(t(), pool)
        assert r1["order"] == ["zed", "alpha", "beta", "mid"], r1["order"]
        r2 = match(t(), list(reversed(pool)))
        assert r2["order"] == r1["order"], r2["order"]
        same = [w("c"), w("b"), w("a")]
        assert match(t(), same)["order"] == ["a", "b", "c"]

    @case("全局预算耗尽：order 清空、constrained_by 含 globalBudget，合格者不进 rejected")
    def _():
        workers = [w("a"), w("b", healthy=False)]
        r = match(t(), workers, limits={"globalBudget": 100.0, "globalSpend": 100.0})
        assert r["order"] == [], r
        assert r["constrained_by"] == ["globalBudget"], r["constrained_by"]
        rej = {x["name"] for x in r["rejected"]}
        assert rej == {"b"} and "a" not in rej, rej
        r2 = match(t(), workers, limits={"globalBudget": 100.0, "globalSpend": 99.99})
        assert r2["order"] == ["a"] and r2["constrained_by"] == [], r2
        assert match(t(), workers, limits=None)["constrained_by"] == []
        assert match(t(), workers)["constrained_by"] == []
        r3 = match(t(), workers, limits={"globalBudget": 0.0, "globalSpend": 0.0})
        assert r3["order"] == [] and r3["constrained_by"] == ["globalBudget"], r3

    @case("estimate：estTokens 缺省 2000、显式 0 有效；预算 0/负/耗尽 → withinBudget=False")
    def _():
        e = estimate({"id": "T"}, w("x", budget=50.0, spend=10.0))
        assert e == {"tokens": 2000, "costTier": "cheap", "withinBudget": True}, e
        e2 = estimate({"estTokens": 3500}, w("x", cost="free", budget=1.0))
        assert e2["tokens"] == 3500 and e2["costTier"] == "free", e2
        assert estimate({"estTokens": 0}, w("x", budget=1.0))["tokens"] == 0
        assert estimate({}, w("x", budget=0.0))["withinBudget"] is False
        assert estimate({}, w("x", budget=-5.0, spend=0.0))["withinBudget"] is False
        assert estimate({}, w("x", budget=10.0, spend=10.0))["withinBudget"] is False
        assert estimate({}, w("x", budget=10.0, spend=9.5))["withinBudget"] is True

    @case("fallback：attempt 连续编号，未知/重复 worker 被剔除，顺序保持 order")
    def _():
        workers = [w("a"), w("b"), w("c")]
        plan = fallback_plan(["a", "ghost", "b", "a", "c"], workers)
        assert plan == [
            {"attempt": 1, "worker": "a", "on": "retryable"},
            {"attempt": 2, "worker": "b", "on": "retryable"},
            {"attempt": 3, "worker": "c", "on": "exhausted"},
        ], plan

    @case("fallback：末项标注 exhausted（单项亦是），空 order 返回空表")
    def _():
        plan = fallback_plan(["a", "b"], [w("a"), w("b")])
        assert plan[0]["on"] == "retryable" and plan[0]["attempt"] == 1, plan
        assert plan[-1]["on"] == "exhausted" and plan[-1]["attempt"] == 2, plan
        single = fallback_plan(["solo"], [w("solo")])
        assert single == [{"attempt": 1, "worker": "solo", "on": "exhausted"}], single
        assert fallback_plan([], [w("a")]) == []

    @case("空输入安全：空 task / 空 workers / 空 limits 全不抛")
    def _():
        assert match(t(), []) == {"order": [], "rejected": [], "constrained_by": []}
        r = match({}, [])
        assert r["order"] == [] and r["rejected"] == [], r
        assert estimate({}, {}) == {"tokens": 2000, "costTier": "unknown",
                                    "withinBudget": False}
        assert fallback_plan([], []) == []

    @case("拒绝原因按固定顺序取第一条：健康 > 配额 > 成本 > 能力 > 权限")
    def _():
        bad_task = t(needsWrite=True, maxCost="cheap", requires=["nope"])
        sick = w("sick", healthy=False, spend=999.0, cost="premium",
                 caps=(), perm="read-only")
        r = match(bad_task, [sick])
        assert len(r["rejected"]) == 1 and "不健康" in r["rejected"][0]["reason"], r
        quota = w("quota", spend=100.0, budget=100.0, cost="premium",
                  caps=(), perm="read-only")
        r2 = match(bad_task, [quota])
        assert "配额" in r2["rejected"][0]["reason"], r2
        r3 = match(bad_task, [w("cost", cost="premium", caps=(), perm="read-only")])
        assert "成本" in r3["rejected"][0]["reason"], r3
        r4 = match(bad_task, [w("cap", caps=(), perm="read-only")])
        assert "能力" in r4["rejected"][0]["reason"], r4
        r5 = match(bad_task, [w("perm", caps=("nope",), perm="read-only")])
        assert "权限" in r5["rejected"][0]["reason"], r5

    @case("F2-3 主键冻结：id 主键 worker 进 order，order 即 teams 可寻址的 id")
    def _():
        workers = [{"id": "w-1", "name": "alpha-display", "capabilities": ["code-review"],
                    "cost": "cheap", "permission": "workspace-write", "healthy": True,
                    "spend": 0.0, "budget": 100.0},
                   {"id": "w-2", "name": "beta-display", "capabilities": ["code-review"],
                    "cost": "standard", "permission": "workspace-write", "healthy": True,
                    "spend": 0.0, "budget": 100.0}]
        r = match(t(), workers)
        assert r["order"] == ["w-1", "w-2"], r["order"]
        # id 与 name 不同值时，order 必须是 id（teams 寻址键），不是显示名
        assert "alpha-display" not in r["order"], r["order"]
        # legacy：只给 name 的名册仍可派单
        r2 = match(t(), [w("legacy-1")])
        assert r2["order"] == ["legacy-1"], r2["order"]
        # 两个键都缺 → 判非法
        r3 = match(t(), [{"capabilities": ["code-review"]}])
        assert r3["order"] == [] and r3["rejected"], r3

    @case("F2-3 fallback_plan：id 主键名册的派单不被剔除")
    def _():
        workers = [{"id": "w-1", "name": "alpha-display", "capabilities": ["code-review"],
                    "cost": "cheap", "permission": "workspace-write", "healthy": True,
                    "spend": 0.0, "budget": 100.0}]
        r = match(t(), workers)
        plan = fallback_plan(r["order"], workers)
        assert [p["worker"] for p in plan] == ["w-1"], plan

    @case("F2-4 契约面：hooks 只含 canonical 可达名，无 selftest 死钩子")
    def _():
        manifest = register(None)
        hooks = set(manifest["hooks"])
        assert "selftest" not in hooks, f"selftest 不是运行时钩子，不得声明: {sorted(hooks)}"
        known = {"match", "estimate", "fallback_plan"}
        assert hooks == known, f"hooks 面漂移: {sorted(hooks)}"

    @case("畸形输入安全：非 dict worker / 缺 name / 非法 estTokens / limits 非 dict")
    def _():
        r = match(t(), [None, "x", 42, {"capabilities": []}, w("ok")])
        assert r["order"] == ["ok"], r
        assert len(r["rejected"]) == 4, r["rejected"]
        assert all(x["reason"] for x in r["rejected"]), r["rejected"]
        assert estimate({"estTokens": "many"}, w("ok"))["tokens"] == 2000
        assert estimate({"estTokens": -3}, w("ok"))["tokens"] == 2000
        assert match(t(), [w("ok")], limits="oops")["order"] == ["ok"]
        assert match(None, None)["order"] == []
        assert fallback_plan("a,b", [w("a")]) == []
        assert fallback_plan(None, None) == []

    results: list[tuple[str, bool, str]] = []
    for name, fn in checks:
        try:
            fn()
        except AssertionError as exc:
            results.append((name, False, str(exc) or "断言失败（无详细信息）"))
        except Exception as exc:  # 闸门要求逐条给结论，任何异常都算该条失败
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
        else:
            results.append((name, True, ""))
    return results


# --------------------------------------------------------------------------- #
# 自检（调试用；闸门走 selftest()）
# --------------------------------------------------------------------------- #

def _selftest() -> None:
    import sys

    try:  # Windows 控制台可能是 GBK，统一按 UTF-8 输出
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    results = selftest()
    for name, ok, msg in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" —— {msg}" if msg else ""))
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"selftest(): {len(results) - failed}/{len(results)} 项通过")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    _selftest()
