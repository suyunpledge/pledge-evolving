"""Verified extension mounting, separate from execution."""
from pathlib import Path
from typing import Any
import os
from .runtime_types import ThinkingSuite

THINKING_HOOKS = ("should_think", "build_thinking_task", "split_thinking", "estimate_budget")


def mount_contrib_extensions(
        *, home: Path, workspace: Path | None = None, return_warnings: bool = False,
) -> "dict[str, Any] | tuple[dict[str, Any], list[str]]":
    """Resolve verified contribution hooks into named extension slots.

    This is the seam that turns "modules that passed the gate" into "modules
    the runtime calls": scheduler drives due-work checks, router shapes task
    dispatch, teams carries subagent messaging, compactor is consulted at the
    loop's compaction threshold, curator runs knowledge metabolism after a
    run, replay renders the run into a timeline at run end, and the thinking
    suite runs contemplation rounds at a run's start/end (capability
    thinking.mode; four hooks from one module, counted per hook). Every
    consultation is counted on ``Agent.extension_calls``, so "mounted" and
    "actually serving" can never be conflated again. Everything is
    optional-by-name — an absent or quarantined module leaves an empty slot,
    and the Agent degrades to its builtin behaviour exactly as before.

    H11（3.0 复验轮）：发现用两个独立注册表——包内 contrib 是保底席位，
    ``home/contrib`` 同名模块只有自己过闸才接管席位；被拒/钩子不可解析
    的本地文件不再静默吞掉内置席位。每次接管/保底都记警告，
    ``return_warnings=True`` 拿列表（缺省裸 dict 返回，兼容旧调用）。
    """
    from .registry import ContribAPI, ModuleRegistry

    # F2-2：workspace 缺省回落 home（兼容旧调用），但真实运行时应传入实际
    # 工作区——贡献模块经 api.workspace 看到的是沙箱边界。
    ws = Path(workspace) if workspace is not None else Path(home)
    contrib_home = home / "contrib"
    contrib_pkg = Path(__file__).resolve().parent / "contrib"
    api = ContribAPI(home=home, workspace=ws)
    warnings: list[str] = []

    # H11：两个注册表各自独立发现——绝不能共用一个 dict，否则 home 侧
    # 加载失败（如被静态线拒）会在席位循环前就把包内模块整体顶掉。
    pkg_registry = ModuleRegistry(api, contrib_pkg)
    home_registry = ModuleRegistry(api, contrib_home)
    try:
        pkg_registry.discover()
    except Exception as exc:
        warnings.append(f"package contrib discovery failed: {exc}")
    try:
        home_registry.discover()
    except Exception as exc:
        warnings.append(f"home contrib discovery failed: {exc}")

    slots: dict[str, Any] = {}
    disabled_seats = {item.strip() for item in os.environ.get("FORGE_DISABLE_SEATS", "").split(",")
                      if item.strip()}
    for module_name, canonical in (("scheduler", "due_jobs"),
                                   ("router", "match"),
                                   ("teams", "deliver"),
                                   ("supervisor", "supervise"),
                                   ("curator", "curate"),
                                   ("compactor", "should_compact"),
                                   ("replay", "replay")):
        if module_name in disabled_seats:
            warnings.append(f"contrib seat '{module_name}' disabled by FORGE_DISABLE_SEATS")
            continue
        # 包内保底席位
        base = pkg_registry.contributions.get(module_name)
        base_hook = None
        if base is not None and base.ok:
            candidate = base.implementation(canonical)
            if callable(candidate):
                base_hook = candidate
            else:
                warnings.append(
                    f"package contrib '{module_name}' passed the gate but exposes "
                    f"no usable '{canonical}' hook — seat left empty")
        # home 模块只有自己过闸才接管席位
        home_mod = home_registry.contributions.get(module_name)
        if home_mod is not None and home_mod.ok:
            home_hook = home_mod.implementation(canonical)
            if callable(home_hook):
                slots[module_name] = home_hook
                warnings.append(
                    f"contrib seat '{module_name}' taken over by home module "
                    f"{home_mod.path}")
                continue
            warnings.append(
                f"home contrib '{module_name}' passed the gate but exposes no "
                f"usable '{canonical}' hook — package module keeps serving the seat")
        elif home_mod is not None:
            warnings.append(
                f"home contrib '{module_name}' rejected by the conformance gate "
                f"({'; '.join(home_mod.problems[:2]) or 'unknown reason'}) — "
                f"package module keeps serving the seat")
        if base_hook is not None:
            slots[module_name] = base_hook

    # thinking.mode（多钩子套件席位）：四钩齐备才挂载；home 模块优先接管。
    # converged / RoundSpec 直接取模块属性（库/类型面，spec 允许调用方直用）。
    def _resolve_thinking(registry: ModuleRegistry):
        module = registry.contributions.get("thinking")
        if module is None:
            return None, "not found"
        if not module.ok:
            return None, "rejected by the conformance gate"
        hooks: dict[str, Any] = {}
        for name in THINKING_HOOKS:
            fn = module.implementation(name)
            if callable(fn):
                hooks[name] = fn
        if len(hooks) != len(THINKING_HOOKS):
            missing = ", ".join(n for n in THINKING_HOOKS if n not in hooks)
            return None, f"missing hooks: {missing}"
        surface = getattr(module, "module", None)
        converged_fn = getattr(surface, "converged", None)
        looks_fn = getattr(surface, "looks_complex", None)
        suite = ThinkingSuite(
            module=module.name,
            should_think=hooks["should_think"],
            build_thinking_task=hooks["build_thinking_task"],
            split_thinking=hooks["split_thinking"],
            estimate_budget=hooks["estimate_budget"],
            converged=converged_fn if callable(converged_fn) else None,
            round_spec=getattr(surface, "RoundSpec", None),
            looks_complex=looks_fn if callable(looks_fn) else None,
        )
        return suite, "ok"

    think_suite, think_why = _resolve_thinking(home_registry)
    if think_suite is not None:
        slots["thinking"] = think_suite
        warnings.append(
            f"contrib seat 'thinking' taken over by home module "
            f"{home_registry.contributions['thinking'].path}")
    else:
        if home_registry.contributions.get("thinking") is not None:
            warnings.append(
                f"home contrib 'thinking' cannot serve the suite ({think_why}) — "
                f"package module keeps serving")
        pkg_suite, pkg_why = _resolve_thinking(pkg_registry)
        if pkg_suite is not None:
            slots["thinking"] = pkg_suite
        elif pkg_registry.contributions.get("thinking") is not None:
            warnings.append(
                f"package contrib 'thinking' passed the gate but exposes no usable "
                f"suite ({pkg_why}) — seat left empty")

    # BS-2 收口（warning 级）：capability 被广告、但没有任何运行席位在消费的
    # 模块 → 点名（"只挂在索引里"）。判据从 BS-1 的"解析不了"改为"没人消费"：
    # 席位名 = 消费人。隔离级升级留给全栈维护师 round-3 裁定（现命中若干存量
    # 模块，先以警告暴露而非直接拒载）。
    for label, registry in (("package", pkg_registry), ("home", home_registry)):
        for name, module in sorted(registry.contributions.items()):
            if not module.ok or not module.capabilities or name in slots:
                continue
            warnings.append(
                f"{label} contrib '{name}' advertises "
                f"{', '.join(module.capabilities)} but holds no runtime seat "
                f"— index-only until wired")

    if return_warnings:
        return slots, warnings
    return slots


