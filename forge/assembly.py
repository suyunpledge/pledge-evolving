"""Composition root, inspired by Loom profiles: mounting is outside execution."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable, Iterable, TYPE_CHECKING
import uuid
from .capability import CapabilityLibrary
from .checkpoint import CheckpointStore
from .memory import MemoryStore
from .model import ModelRouter
from .policy import Policy
from .session import Session
from .runtime_types import LoopLimits
from .local_service import service_from_config, resolve_thinking_mode
from .extensions import mount_contrib_extensions

if TYPE_CHECKING:
    from .loop import Agent


def build_agent(
    *,
    home: Path,
    workspace: Path,
    config,
    router: ModelRouter,
    policy: Policy | None = None,
    limits: LoopLimits | None = None,
    non_interactive: bool = True,
    expose: Iterable[str] | None = None,
    mount_contrib: bool = True,
    session_path: Path | None = None,
    observer: "Callable[[dict[str, Any]], None] | None" = None,
    probes=None,
) -> Agent:
    """Wire a full agent from a composed config tree.

    ``mount_contrib`` decides whether verified contribution modules actually
    *serve* the runtime (not just sit validated in the index): their hooks are
    resolved through the registry's alias table and handed to the Agent as an
    extension surface. A quarantined or missing module simply leaves its slot
    empty — mounting never fails the boot.

    ``session_path`` overrides where this agent's event log lands. The default
    uses a fresh identity to isolate concurrent CLI tasks; a chat
    channel serving several peers passes a per-peer path so conversations do
    not share one log.
    """
    from .loop import Agent
    from .tools import build_builtin_registry

    home = Path(home)
    workspace = Path(workspace)
    policy = policy or Policy.from_config(config, workspace=workspace, non_interactive=non_interactive)
    limits = limits or LoopLimits(
        max_steps=int(config.get("loop", "maxSteps", 12)),
        max_depth=int(config.get("loop", "maxDepth", 2)),
        spawn_budget=int(config.get("loop", "spawnBudget", 8)),
    )

    capabilities = CapabilityLibrary(
        [home / "capabilities", workspace / ".forge" / "capabilities"],
        state_path=home / "capabilities.state.json",
    )
    capabilities.scan()

    memory = MemoryStore(home / "MEMORY.md").load()
    checkpoints = CheckpointStore(home, workspace)
    session_dir = home / "sessions"
    session = Session(session_path or (session_dir / (uuid.uuid4().hex + ".jsonl")), meta={
        "cwd": str(workspace),
        "model": str(config.get("model", "primary", "")),
        "policy_mode": policy.mode.value,
        "framework": "forge",
    })

    registry = build_builtin_registry(expose=expose)
    # H14 (rev.H14-3.7): the cost ledger is REAL here, not optional — priced
    # compaction (T4) needs spend data, and cost accounting that only exists
    # when a host remembers to inject it never records anything. Path matches
    # `forge cost` (cli.cmd_cost) so the two views stay one ledger.
    from .pricing import CostLedger
    ledger = CostLedger(home / "cost" / "spend.jsonl")
    # service 门控：解析本次运行实际会用到的 provider 是哪个引擎。未知/云端 → ""，
    # 所有门控行为退化为不生效，保证既有 provider 行行为不变。
    service = service_from_config(config)
    # F2-2：传真实工作区，贡献模块经 api.workspace 看到沙箱边界。
    # H11：接管/保底警告转 mount_warning 事件，不再静默。
    extensions: dict[str, Any] = {}
    mount_warnings: list[str] = []
    if mount_contrib:
        extensions, mount_warnings = mount_contrib_extensions(
            home=home, workspace=workspace, return_warnings=True)
    from .context_plan import ContextPlan
    context_plan = ContextPlan.from_config(config)
    return Agent(
        home=home,
        workspace=workspace,
        router=router,
        registry=registry,
        policy=policy,
        memory=memory,
        capabilities=capabilities,
        checkpoints=checkpoints,
        session=session,
        limits=limits,
        context_mode=context_plan.default_mode if context_plan.mode == 'ask' else context_plan.mode,
        extensions=extensions,
        mount_warnings=mount_warnings,
        cost_ledger=ledger,
        # 三档模式来自配置树（bundle 行 thinking.mode；off/smart/on，默认 off），
        # 再经 service 门控：本地引擎（ollama / llamacpp / mnn）上 smart→off，
        # 云端 provider 的配置原样透传。
        thinking=resolve_thinking_mode(
            config.get("thinking", "mode", "off"), service),
        planning=config.get("planning", "level", "none"),
        planning_model=config.get("planning", "model", None),
        review_enabled=config.get("review", "enabled", False),
        review_model=config.get("review", "model", None),
        project_rules=(False if config.row('project_rules') and config.row('project_rules').disabled
                       else config.get('project_rules', 'enabled', True)),
        service=service,
        thinking_token_cap=config.get("thinking", "tokenCap", None),
        observer=observer,
        probes=probes,
    )


