"""The agent loop and its subagent runtime.

The kernel is deliberately small and single-threaded per session (OpenClaw
serialises runs per session lane; Codex makes the rollout log the source of
truth). Everything else hangs off it:

* layered system prompt (OpenClaw)
* tool protocol that is wire-agnostic and therefore testable offline
* subagents with an isolated context, a depth cap, a spawn budget and a
  permission ceiling inherited from the parent
* de-poisoning of subagent output: a child cannot smuggle system directives
  back into the parent (WorkBuddy)
* checkpoints before every write (Hermes)
* a compaction threshold with an auditable event (OpenClaw)
* contribution seats that serve the runtime at their stage — due-check before
  every iteration, dispatch before a subagent spawn, compaction at the
  threshold, metabolism at the end of a run — with a per-seat call ledger so
  "mounted" can be audited as "actually called"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .capability import CapabilityLibrary
from .checkpoint import CheckpointStore
from .memory import ContextBudget, MemoryStore  # noqa: F401
from .model import Completion, ModelRouter, TransportError
from .pricing import CostLedger, cost_of, rate_for
from .policy import Decision, Mode, Policy, WRITE_TOOLS
from .session import Session
from .tools import ToolContext, ToolRegistry, ToolResult
from . import toolwire
from .types import Outcome, OutcomeClock, as_outcome  # noqa: F401  (re-export)
import time

# A seat that *raised* gets this marker (not None): callers must be able to
# tell "the seat failed" apart from "the seat legitimately returned nothing".
_SEAT_FAILED = object()

# thinking.mode（rev.4.1 沉思引擎）：多钩子套件席位。四个钩子必须整组来自同
# 一模块（半组装套件一律不挂）；converged / RoundSpec 是模块契约公开的库/类型
# 面（spec：供调用方直接使用，不登记 hooks），由挂载层直接取属性。计数写在
# "thinking.<hook>" 名下，"挂载"与"真被调用"因此可分。
THINKING_HOOKS = ("should_think", "build_thinking_task", "split_thinking", "estimate_budget")

# 三档沉思模式（用户可选开关；smart 的判据 = 模块库函数 looks_complex）。
THINKING_MODES = ("off", "smart", "on")


def _normalise_thinking_mode(value: Any) -> str:
    """bool 兼容（True→on / False→off）；未知值 → off（保守，不猜）。"""
    if isinstance(value, bool):
        return "on" if value else "off"
    mode = str(value or "off").strip().lower()
    return mode if mode in THINKING_MODES else "off"


@dataclass(frozen=True)
class ThinkingSuite:
    """Runtime handle to a mounted thinking engine (capability thinking.mode)."""

    module: str
    should_think: Any
    build_thinking_task: Any
    split_thinking: Any
    estimate_budget: Any
    converged: Any = None
    round_spec: Any = None
    looks_complex: Any = None

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)

# Subagent output is untrusted: a child must not be able to smuggle a directive
# back into the parent by impersonating a system turn. Regex alone was too
# narrow (full-width angle brackets, chat-template tokens and role prefixes all
# slipped through), so the pattern covers the shapes other frameworks' models
# actually emit.
INJECTION_RE = re.compile(
    r"</?(?:system-reminder|system|instructions|assistant|im_start|im_end|s)\b[^>]*>"
    r"|＜/?(?:system|assistant|instructions)[^＞]*＞"
    r"|^\s*(?:SYSTEM|ASSISTANT|USER|DEVELOPER)\s*[:：]"
    r"|\[/?INST\]|<<\s*/?SYS\s*>>",
    re.I | re.M,
)

DEFAULT_SYSTEM = """You are {name}, a local agent working inside {workspace}.

Operating rules:
1. Prefer evidence over recall. Read the file before you describe it.
2. Call one tool at a time. Emit exactly one block:
   <tool_call>{{"tool": "<name>", "args": {{...}}}}</tool_call>
3. When you are done, answer in plain prose with no tool block.
4. Available tools: {tools}
"""


class _ModelProbe:
    """T4: minimal model-name source for the pricing scale.

    Wraps the agent's router: reports the FIRST chain entry (the effective
    primary). Only the model name is needed — rate lookups are pure-table.
    """

    def __init__(self, router: ModelRouter) -> None:
        order = router._order(None) if isinstance(router, ModelRouter) else []
        self.model = order[0][1] if order else ""


def _pricing_scale(probe: Any, *, ledger: CostLedger | None = None) -> float:
    """T4 (rev.T4-3.6): compaction-budget multiplier from effective rates.

    scale = clamp(0.30 / rate, 1.0, 2.0):
    - unknown / zero-rate (review-tier) models -> 1.0: the default floor is
      never lowered — capability regressions are not for sale.
    - cheaper models raise the ceiling: at the 2026-09-14 invoice rates,
      deepseek (¥0.1595/M) → ~1.88, mimo (¥0.0754/M) → 2.0 (capped).
    - a ledger with heavy recent spend tightens: scale *= max(0.5, 1 -
      recent_cost / 50) over the last 10 entries, so a runaway loop visibly
      dents the budget instead of being priced away.

    The seat (if mounted) still sees the RAW limit in its budget dict, so a
    contributed policy can always tighten further; T4 never loosens a seat
    verdict, only the builtin budget's ceiling.
    """
    model = str(getattr(probe, "model", "") or "")
    rate = rate_for(model)
    if rate <= 0.0:
        return 1.0
    scale = min(2.0, max(1.0, 0.30 / rate))
    if ledger is not None and ledger.entries:
        recent = ledger.entries[-10:]
        recent_cost = sum(e.cost for e in recent)
        scale *= max(0.5, 1.0 - recent_cost / 50.0)
    return scale


@dataclass
class LoopLimits:
    max_steps: int = 12
    max_depth: int = 2
    spawn_budget: int = 8
    max_tool_result: int = 4000
    context_chars: int = 24000


@dataclass
class Step:
    index: int
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    decision: str = ""
    note: str = ""


@dataclass
class RunReport:
    text: str
    steps: list[Step]
    usage: dict[str, Any]
    stopped: str = "final"          # final | max_steps | error
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.tool)


class Agent:
    """One agent runtime. Nested agents are created through ``spawn``."""

    # T4: tests and external callers reach the scale through the class;
    # implementation lives at module level (single source of truth).
    _pricing_scale = staticmethod(_pricing_scale)

    def __init__(
        self,
        *,
        home: Path,
        workspace: Path,
        router: ModelRouter,
        registry: ToolRegistry,
        policy: Policy,
        memory: MemoryStore | None = None,
        capabilities: CapabilityLibrary | None = None,
        checkpoints: CheckpointStore | None = None,
        session: Session | None = None,
        limits: LoopLimits | None = None,
        name: str = "forge",
        depth: int = 0,
        budget: list[int] | None = None,
        extra_system: str = "",
        wire_hint: str = "openai",
        extensions: dict[str, Any] | None = None,
        jobs: Iterable[dict[str, Any]] | None = None,
        accounting: dict[str, int] | None = None,
        mount_warnings: Iterable[str] | None = None,
        cost_ledger: "CostLedger | None" = None,
        thinking: bool | str = False,
    ) -> None:
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.router = router
        self.registry = registry
        self.policy = policy
        self.memory = memory
        self.capabilities = capabilities
        self.checkpoints = checkpoints
        self.session = session
        self.limits = limits or LoopLimits()
        self.name = name
        self.depth = depth
        self.budget = budget if budget is not None else [self.limits.spawn_budget]
        self.extra_system = extra_system
        self.wire_hint = wire_hint if wire_hint in ("openai", "anthropic") else "openai"
        # Contribution modules that passed the gate and whose hooks the runtime
        # actually calls. Empty dict = pure builtin behaviour (smoke, tests).
        self.extensions: dict[str, Any] = dict(extensions or {})
        # H11：挂载期接管/保底通知，首次 run() 时以 mount_warning 事件浮出，
        # 不允许静默换席。 WB P2：标记防止重复发送。
        self.mount_warnings: list[str] = [str(w) for w in (mount_warnings or [])]
        self._mount_warnings_emitted = False
        # H18b: a pinned-only compaction no-op is reported once per run;
        # repeated same-size events every step are noise, not progress.
        self._compaction_noop_reported = False
        # Last message list seen this run (post-compaction), for evidence.
        self._last_messages: list[dict[str, Any]] | None = None
        # Host-registered periodic duties. The scheduler seat's due-set is
        # computed against this list; empty by default, because an interactive
        # run schedules nothing on its own.
        self.jobs: list[dict[str, Any]] = [dict(job) for job in (jobs or [])]
        # Per-seat consultation ledger. "Mounted" is a claim; these counters
        # are the evidence — a seat only gets a count when the runtime calls
        # it. Children share the root's ledger so the root audit sees the
        # whole tree's calls, not just the root's own.
        self.extension_calls: dict[str, int] = accounting if accounting is not None else {}
        # T4 (rev.T4-3.6): optional pricing ledger (pricing.CostLedger) — heavy
        # recent spend tightens the compaction threshold. None = scale on
        # list rates only.
        self.cost_ledger = cost_ledger
        # thinking.mode 三档：off / smart（按任务复杂度智能选择，判据=模块
        # 库函数 looks_complex）/ on；bool 兼容（True→on）。默认 off，成本护栏。
        self.thinking_mode = _normalise_thinking_mode(thinking)
        self.thinking = self.thinking_mode != "off"
        self._run_task = ""
        self._run_thinking_tokens = 0
        self._run_thinking_active = False
        # H14: run-scoped pricing state (set per run()); _run_scope marks "an
        # un-recorded run is in flight on this agent" so nested/exited runs
        # cannot double-record one delta.
        self._run_probe: _ModelProbe | None = None
        self._run_model = "unknown"
        self._run_scope = False
        # T4 (rev.T4-3.6): the builtin compaction budget scales with the
        # active model's effective blended rate (pricing.py). Expensive
        # models keep the default floor; cheap models may run a larger
        # context. H14: the scale is applied around the RAW limit as an
        # actual multiplier (both directions) — a floor `max(raw, …)` would
        # have made ledger tightening (scale < 1) unreachable in production.
        # Applied once at construction — the seat still sees the RAW limit in
        # its budget dict and can always tighten, never loosen.
        self.compactor = ContextBudget(
            max_chars=max(2000, int(self.limits.context_chars * self._pricing_scale(
                _ModelProbe(self.router),
                ledger=self.cost_ledger))))
        self.events: list[dict[str, Any]] = []

    # -- context ---------------------------------------------------------
    def system_prompt(self) -> str:
        parts = [
            DEFAULT_SYSTEM.format(
                name=self.name,
                workspace=self.workspace,
                tools=", ".join(self.registry.names()),
            )
        ]
        if self.capabilities is not None:
            index = self.capabilities.context_injection()
            if index:
                parts.append("Capabilities available (load one only when needed):\n" + index)
        if self.memory is not None:
            slice_ = self.memory.context_slice(max_chars=2000)
            if slice_:
                parts.append("Long-term memory (bounded slice):\n" + slice_)
        parts.append(f"Permission mode: {self.policy.mode.value}; sandbox: {self.policy.sandbox.value}.")
        if self.depth:
            parts.append(f"You are a subagent at depth {self.depth}; report findings, do not plan the whole job.")
        if self.extra_system:
            parts.append(self.extra_system)
        return "\n\n".join(parts)

    # -- events ----------------------------------------------------------
    def _emit(self, **event: Any) -> None:
        record = dict(event)
        self.events.append(record)
        if self.session is not None:
            payload = dict(record)
            etype = str(payload.pop("type", "event"))
            self.session.append(etype, **payload)

    # -- contribution seats ----------------------------------------------
    def _use_extension(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Consult a mounted contribution seat; returns its verdict, None, or _SEAT_FAILED.

        Counting happens here — at the only call sites the runtime owns — so
        an audit can tell "a module is mounted" apart from "a module is
        actually serving". A hook that raises emits an ``extension_error``
        event and returns ``_SEAT_FAILED``; the runtime then keeps its builtin
        path. Mounting must never be able to take a run down.
        """
        hook = self.extensions.get(name)
        if not callable(hook):
            return None
        self.extension_calls[name] = self.extension_calls.get(name, 0) + 1
        try:
            return hook(*args, **kwargs)
        except Exception as exc:
            self._emit(type="extension_error", module=name,
                       error=f"{type(exc).__name__}: {exc}")
            return _SEAT_FAILED

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            policy=self.policy,
            workspace=self.workspace,
            session=self.session,
            emit=self._emit,
            # ``agent`` is deliberately NOT exposed here. Handing tools a
            # reference to the runtime would let any tool (including a
            # third-party capability) mutate the spawn budget or reach the
            # policy object — an in-process privilege-escalation channel.
            extras={
                "registry": self.registry,
                "memory": self.memory,
                "capabilities": self.capabilities,
                "spawn": self._spawn_handler,
            },
        )

    # -- subagents -------------------------------------------------------
    def _spawn_handler(self, task: str, mode: str) -> ToolResult:
        if self.depth >= self.limits.max_depth:
            return ToolResult(ok=False, error=f"max subagent depth {self.limits.max_depth} reached")
        if self.budget[0] <= 0:
            return ToolResult(ok=False, error="subagent spawn budget exhausted")
        child_mode = Mode(mode) if mode else None
        child_policy = self.policy.child(child_mode)
        child_name = f"{self.name}/sub{self.depth + 1}"

        # Dispatch stage: router seat orders the candidates, teams seat
        # delivers the task message to the chosen member. Both verdicts land
        # on the event stream; a router that admits no candidate refuses the
        # dispatch instead of spawning work it says cannot be handled.
        dispatch = self._dispatch_consult(task, child_name, child_policy)
        if dispatch["order"] is not None:
            self._emit(type="subagent_dispatch", member=child_name,
                       order=dispatch["order"][:4], rejected=(dispatch["rejected"] or [])[:4])
            if not dispatch["order"]:
                return ToolResult(ok=False,
                                  error="dispatch refused by router seat: no candidate passed match()")
        if dispatch["delivery"] is not None:
            self._emit(type="subagent_delivery", to=child_name,
                       delivered=dispatch["delivery"]["delivered"][:4],
                       denied=dispatch["delivery"]["denied"][:4])

        self.budget[0] -= 1
        child = Agent(
            home=self.home,
            workspace=self.workspace,
            router=self.router,
            # a child gets its own activation set, otherwise a child's
            # tool_search silently widens the parent's tool surface too
            registry=self.registry.clone(),
            policy=child_policy,
            memory=self.memory,
            capabilities=self.capabilities,
            checkpoints=self.checkpoints,
            session=self.session,
            limits=self.limits,
            name=child_name,
            depth=self.depth + 1,
            budget=self.budget,
            extensions=self.extensions,
            jobs=self.jobs,
            accounting=self.extension_calls,
            # H14: children share the parent ledger and the parent SESSION —
            # their tokens land in the parent's run delta, so a child must not
            # record its own entry (double counting).
            cost_ledger=None,
            # 三档模式随树继承：子代理同样按模式在运行首尾起沉思。
            thinking=self.thinking_mode,
        )
        self._emit(type="subagent_spawn", task=task[:300], mode=child_policy.mode.value,
                   depth=child.depth, budget_left=self.budget[0])
        try:
            report = child.run(task)
        except Exception as exc:  # a failed child must not kill the parent
            self._emit(type="subagent_error", error=f"{type(exc).__name__}: {exc}")
            return ToolResult(ok=False, error=f"subagent failed: {exc}")
        # A child's events are part of the parent's audit story: fold them
        # into the parent stream (marked), so "covers subagents" is
        # verifiable from the root report, not just the shared session log.
        # ``setdefault`` keeps an inner subagent's own origin marker.
        # 瘦身 C：折叠时重字段裁成 preview——子代理的完整叙事留在子会话，
        # 父流只保留线索（类型/序号/agent 标记 + ≤260 字符预览）。
        for child_event in report.events:
            folded = dict(child_event)
            folded.setdefault("agent", child.name)
            for key, value in list(folded.items()):
                if isinstance(value, str) and len(value) > 260:
                    folded[key] = value[:260] + "…[fold-preview]"
            self.events.append(folded)
        clean = sanitise_child_output(report.text)
        self._emit(type="subagent_result", chars=len(clean), steps=len(report.steps))
        return ToolResult(ok=True, content=clean[: self.limits.max_tool_result], meta={"depth": child.depth})

    def _dispatch_consult(self, task: str, child_name: str, child_policy: Policy) -> dict[str, Any]:
        """Ask the router/teams seats about an upcoming subagent dispatch.

        The worker candidate below describes the child this runtime is about
        to create, so the seats judge the real object — not a placeholder.
        Verdicts are returned for the caller to record; ``None`` means the
        seat is not mounted (or broke — it then emitted ``extension_error``).
        """
        outcome: dict[str, Any] = {"order": None, "rejected": [], "delivery": None}

        if "router" in self.extensions:
            permission = {"read-only": "read-only", "workspace-write": "workspace-write",
                          "danger-full-access": "full-access"}.get(
                              child_policy.sandbox.value, "read-only")
            request = {"id": f"spawn@depth{self.depth + 1}", "requires": ["subagent"],
                       "maxCost": "standard", "needsWrite": False, "estTokens": 0}
            worker = {"id": child_name, "name": child_name, "capabilities": ["subagent"],
                      "cost": "cheap", "permission": permission, "healthy": True,
                      "spend": 0.0, "budget": float(max(1, self.budget[0]))}
            verdict = self._use_extension("router", request, [worker])
            if isinstance(verdict, dict):
                outcome["order"] = [str(name) for name in (verdict.get("order") or [])]
                outcome["rejected"] = verdict.get("rejected") or []

        if "teams" in self.extensions:
            message = {"from": self.name, "to": child_name, "body": task[:400]}
            members = [{"id": self.name, "role": "lead", "alive": True,
                        "spend": 0.0, "budget": 1000.0},
                       {"id": child_name, "role": "worker", "alive": True,
                        "spend": 0.0, "budget": float(max(1, self.budget[0]))}]
            delivery = self._use_extension("teams", message, members, [])
            if isinstance(delivery, dict):
                outcome["delivery"] = {"delivered": list(delivery.get("delivered") or []),
                                       "denied": list(delivery.get("denied") or [])}
        return outcome

    # -- runtime stage helpers (contribution seats) -----------------------
    def _due_check(self, index: int) -> None:
        """Scheduler seat, before every iteration: what registered work is due?

        The due-set is recomputed against a live clock each iteration (a run
        can outlive an interval boundary). The first iteration always records
        the consultation — even with nothing registered — so a production run
        can prove the seat was consulted; later iterations record only a
        non-empty due-set, keeping the event stream quiet.
        """
        if "scheduler" not in self.extensions:
            return
        due = self._use_extension("scheduler", self.jobs, time.time())
        if not isinstance(due, list):
            return
        due_ids = [str(row.get("id")) for row in due
                   if isinstance(row, dict) and row.get("id") is not None]
        if due_ids or index == 1:
            self._emit(type="schedule_check", step=index,
                       registered=len(self.jobs), due=due_ids)

    def _effective_budget(self) -> int:
        """H15 (rev.H15-3.8): live compaction ceiling, recomputed per check.

        The construction-time max_chars is the frozen snapshot baseline; the
        ledger is a live object, so the runaway run that FILLS the ledger
        must hit the tightened ceiling itself — not merely the next
        build_agent. Pure table lookups, once per loop step.
        """
        ceiling = max(2000, int(self.limits.context_chars * self._pricing_scale(
            _ModelProbe(self.router), ledger=self.cost_ledger)))
        # never loosen below the frozen construction snapshot: within one
        # agent, the budget can only drift tighter over time.
        return min(self.compactor.max_chars, ceiling)

    def _should_compact(self, messages: list[dict[str, Any]]) -> bool:
        """Compaction policy: builtin budget check OR the mounted compactor seat.

        The seat was contributed for exactly this decision. A "compact" verdict
        from either the builtin budget or the seat wins, so a stricter
        contributed policy can never be talked out of firing; the builtin
        ContextBudget still performs the actual compaction.
        """
        verdict = self.compactor.should_compact(messages) or self.compactor.size(messages) > self._effective_budget()
        if "compactor" not in self.extensions:
            return verdict
        seat = self._use_extension(
            "compactor", messages,
            {"maxChars": self.limits.context_chars, "maxMessages": 200, "keepTail": 6})
        if isinstance(seat, dict) and seat.get("compact") is True:
            return True
        return verdict

    # -- thinking suite (capability thinking.mode) -------------------------
    def _thinking_call(self, hook: str, *args: Any, **kwargs: Any) -> Any:
        """Consult one hook of the mounted thinking suite; counted per hook.

        与 _use_extension 同款纪律：套件缺席返回 None；钩子抛错记
        extension_error 并返回 _SEAT_FAILED——沉思故障永远不拖垮主跑。
        """
        suite = self.extensions.get("thinking")
        if suite is None:
            return None
        hook_fn = getattr(suite, hook, None)
        if not callable(hook_fn):
            return None
        key = f"thinking.{hook}"
        self.extension_calls[key] = self.extension_calls.get(key, 0) + 1
        try:
            return hook_fn(*args, **kwargs)
        except Exception as exc:
            self._emit(type="extension_error", module=key,
                       error=f"{type(exc).__name__}: {exc}")
            return _SEAT_FAILED

    def _contemplate(self, phase: str, task: str, *, last_outputs: list[str]) -> "str | None":
        """Run one phase's contemplation rounds (estimate→should→build→split→converged).

        轮型序列取自模块 Budget（单一序列源）；相邻轮收敛判定消费模块公开的
        库函数 converged（spec：供调用方直接使用——这里就是它的消费者）。
        Returns contemplation text; None when not mounted / not engaged.
        Emits thinking_engaged(phase, rounds, budget, stopped, chars, tokens).
        """
        suite = self.extensions.get("thinking")
        if suite is None:
            return None
        budget = self._thinking_call(
            "estimate_budget", phase,
            task_size=len(str(task or "")), history_len=len(last_outputs))
        if budget is None or budget is _SEAT_FAILED:
            return None
        engage = self._thinking_call("should_think", phase, budget=budget, last_result=None)
        if engage is not True:
            return None
        round_types = tuple(getattr(budget, "round_types", ()) or ())
        if not round_types:
            self._emit(type="thinking_engaged", phase=phase, rounds=0, budget=0,
                       stopped="empty-budget", chars=0, tokens=0)
            return None
        spec_cls = getattr(suite, "round_spec", None)
        texts: list[str] = []
        tokens = 0
        stopped = "budget"
        prev = ""
        for round_type in round_types:
            if callable(spec_cls):
                spec: Any = spec_cls(round_type=str(round_type), prompt="", role="")
            else:  # 模块未暴露 RoundSpec 类时走鸭子 dict（v2.1 兼容）
                spec = {"round_type": str(round_type)}
            row = self._thinking_call(
                "build_thinking_task", phase, spec,
                {"task": str(task or ""), "prior_outputs": list(texts)})
            if not isinstance(row, dict) or row is _SEAT_FAILED:
                break
            msgs = row.get("messages") or []
            if not msgs:
                break
            try:
                completion = self.router.complete(msgs, small=True)
            except TransportError as exc:
                self._emit(type="thinking_transport_error", phase=phase, error=str(exc)[:200])
                break
            usage = getattr(completion, "usage", None)
            if usage is not None:
                tokens += int(getattr(usage, "prompt_tokens", 0)) + int(getattr(usage, "completion_tokens", 0))
            split = self._thinking_call("split_thinking", getattr(completion, "text", "") or "")
            if isinstance(split, tuple) and len(split) == 2:
                thinking_part, answer_part = str(split[0]), str(split[1])
            else:
                thinking_part, answer_part = "", str(getattr(completion, "text", "") or "")
            produced = (answer_part or thinking_part).strip()
            if produced:
                texts.append(produced)
            if prev:
                verdict_c = self._thinking_call("converged", prev, produced)
                if verdict_c is True:
                    stopped = "converged"
                    break
            prev = produced
        notes = "\n\n".join(texts).strip()
        self._run_thinking_tokens += tokens
        self._emit(type="thinking_engaged", phase=phase, rounds=len(texts),
                   budget=len(round_types), stopped=stopped, chars=len(notes), tokens=tokens)
        return notes or None

    # -- main loop -------------------------------------------------------
    def run(self, task: str) -> RunReport:
        if self.session is not None:
            self._emit(type="user_message", content=task)
        # H14 (rev.H14-3.7): one un-recorded run in flight per agent. The
        # ledger entry is written in _finish from report.usage — session
        # events carry no token data (only RunReport.usage does), and the
        # once-flag prevents nested/exited runs from double-recording. The
        # pricing facts are resolved up front so the entry and the emitted
        # scale always describe the model this run was shaped for.
        probe = _ModelProbe(self.router)
        self._run_probe = probe
        self._run_model = probe.model or "unknown"
        self._run_scope = self.cost_ledger is not None
        self._run_task = str(task)
        self._run_thinking_tokens = 0
        # H11：挂载期席位接管/保底记录浮出到事件流，而不是静默换席。
        # WB P2：只在首次 run() 时发。
        if not self._mount_warnings_emitted:
            for warning in self.mount_warnings:
                self._emit(type="mount_warning", warning=str(warning)[:300])
            self._mount_warnings_emitted = True
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": task},
        ]
        # 启动沉思（phase=thinking）：三档门控——off 不起；on 直接起；smart 由
        # 模块库函数 looks_complex 决定（有运行期消费者，非装饰）。沉思文本
        # 注入任务消息的 <contemplation> 段——消费者不是摆设，主跑真的用到它。
        self._run_thinking_active = False
        if self.thinking and "thinking" in self.extensions:
            active = True
            if self.thinking_mode == "smart":
                active = self._thinking_call("looks_complex", str(task)) is True
            self._run_thinking_active = active
            self._emit(type="thinking_mode", mode=self.thinking_mode, engaged=active)
            if active:
                try:
                    notes = self._contemplate("thinking", str(task), last_outputs=[])
                except Exception as exc:
                    notes = None
                    self._emit(type="extension_error", module="thinking",
                               error=f"{type(exc).__name__}: {exc}")
                if notes:
                    messages[1] = {"role": "user",
                                   "content": f"{task}\n\n<contemplation>\n{notes[:4000]}\n</contemplation>"}
        steps: list[Step] = []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        stopped = "max_steps"

        for index in range(1, self.limits.max_steps + 1):
            self._due_check(index)
            if self._should_compact(messages):
                before = self.compactor.size(messages)
                # H16: the live ceiling (pricing-aware, ledger-tightened) must
                # drive the actual compaction too, not just the trigger —
                # otherwise the event fires with before == after (a lie).
                # H18 (rev.H18-3.11): a pinned-only no-op must not spin the
                # event every step — report it once per run (compaction
                # skipped at size X), then stay silent while sizes creep.
                messages = self.compactor.compact(messages,
                                                  max_chars=self._effective_budget())
                after = self.compactor.size(messages)
                if after < before or not self._compaction_noop_reported:
                    self._emit(type="compaction", before=before, after=after,
                               shrunk=after < before)
                    if after >= before:
                        self._compaction_noop_reported = True
            self._last_messages = messages
            try:
                completion: Completion = self.router.complete(
                    messages, small=False,
                    tools=toolwire.tool_declarations(self.registry.visible(), self.wire_hint),
                )
            except TransportError as exc:
                self._emit(type="model_error", error=str(exc)[:300])
                return self._finish(RunReport(text=f"[model error] {exc}", steps=steps,
                                              usage=usage_total, stopped="error", events=self.events))
            usage_total["prompt_tokens"] += completion.usage.prompt_tokens
            usage_total["completion_tokens"] += completion.usage.completion_tokens

            text = completion.text or ""
            native_raw = list(getattr(completion, "tool_calls", None) or [])
            wire = str(getattr(completion, "wire", None) or self.wire_hint)
            native_calls: list[toolwire.ToolCall] = []

            if native_raw:
                # answer EVERY call the model issued this turn: real models batch
                # several tool calls into one assistant message, and OpenAI-shaped
                # APIs reject the replay unless each id gets its own tool message
                for first in native_raw:
                    native_calls.append(toolwire.ToolCall(
                        id=str(first.get("id") or ""),
                        name=str(first.get("name") or ""),
                        args=dict(first.get("args") or {}),
                        wire=str(first.get("wire") or wire),
                    ))
                prose = text.strip()
                # 瘦身 B：tool_call_native 不再单独成事件——wire/call_id 并入
                # 对应的 tool_call 事件（下方合并），三元拍平为一元
            else:
                match = TOOL_CALL_RE.search(text)
                if not match:
                    if self.session is not None:
                        self._emit(type="assistant_message", content=text)
                    return self._finish(RunReport(text=text.strip(), steps=steps, usage=usage_total,
                                                  stopped="final", events=self.events))

                prose = TOOL_CALL_RE.sub("", text).strip()
                try:
                    call = json.loads(match.group(1))
                    tool_name = str(call.get("tool", ""))
                    args = dict(call.get("args") or {})
                except json.JSONDecodeError as exc:
                    steps.append(Step(index=index, note=f"bad tool block: {exc}"))
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": f"Tool block was not valid JSON: {exc}. Retry."})
                    continue

            if native_calls:
                answered: list[tuple[toolwire.ToolCall, str, bool]] = []
                for call in native_calls:
                    step = Step(index=len(steps) + 1, tool=call.name, args=call.args,
                                note=f"native/{wire} id={call.id or '-'}")
                    if self.checkpoints is not None and call.name in WRITE_TOOLS:
                        point = self.checkpoints.snapshot(f"before {call.name}")
                        if point is not None:
                            step.note = f"checkpoint {point.commit[:8]}"
                            self._emit(type="checkpoint", commit=point.commit, label=point.label)
                    result = self.registry.invoke(call.name, call.args, self._tool_context())
                    step.result = (result.content or result.error)[: self.limits.max_tool_result]
                    step.decision = ("deny" if not result.ok and "denied" in result.error
                                     else ("ok" if result.ok else "error"))
                    steps.append(step)
                    answered.append((call, step.result, result.ok))
                    authz = (result.meta or {}).get("authorization") if isinstance(result.meta, dict) else None
                    self._emit(type="tool_call", tool=call.name, args=call.args,
                               result=step.result[:1500], ok=result.ok,
                               decision=step.decision, step=step.index,
                               call_id=call.id, wire=wire, authorization=authz)
                messages.append(completion.assistant_message
                                or toolwire.assistant_message(prose, native_calls, wire))
                messages.extend(toolwire.tool_result_messages(answered, wire))
                continue

            step = Step(index=index, tool=tool_name, args=args)
            if self.checkpoints is not None and tool_name in WRITE_TOOLS:
                point = self.checkpoints.snapshot(f"before {tool_name}")
                if point is not None:
                    step.note = f"checkpoint {point.commit[:8]}"
                    self._emit(type="checkpoint", commit=point.commit, label=point.label)

            result = self.registry.invoke(tool_name, args, self._tool_context())
            step.result = (result.content or result.error)[: self.limits.max_tool_result]
            step.decision = "deny" if not result.ok and "denied" in result.error else ("ok" if result.ok else "error")
            steps.append(step)
            authz = (result.meta or {}).get("authorization") if isinstance(result.meta, dict) else None
            self._emit(type="tool_call", tool=tool_name, args=args,
                       result=step.result[:1500], ok=result.ok, decision=step.decision, step=index,
                       authorization=authz, native=False)

            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": f"<tool_result tool=\"{tool_name}\" ok=\"{result.ok}\">{step.result}</tool_result>",
            })

        self._emit(type="loop_stop", reason="max_steps", steps=len(steps))
        return self._finish(RunReport(
            text=f"[stopped: max_steps={self.limits.max_steps}] last tool: {steps[-1].tool if steps else 'none'}",
            steps=steps,
            usage=usage_total,
            stopped=stopped,
            events=self.events,
        ))

    def _finish(self, report: RunReport) -> RunReport:
        """Run-end stage: metabolism seats + the runtime's call audit.

        Called on every exit path (final / error / max_steps) so the end of a
        run is always metabolised and always accounted for.
        """
        try:
            outcome = self.metabolise(report)
            if outcome:
                errors = sorted(key for key, value in outcome.items()
                                if isinstance(value, dict) and "error" in value)
                self._emit(type="run_metabolised", modules=sorted(outcome), errors=errors)
        except Exception as exc:  # metabolism must never take the run down
            self._emit(type="extension_error", module="metabolise",
                       error=f"{type(exc).__name__}: {exc}")
        # 结束反思（phase=reflection）：在 extension_calls 审计事件之前完成；
        # 反思轮 token 并入账本 delta（诚实计费：沉思不是免费午餐）。smart
        # 模式下未启用的跑次同样跳过反思（_run_thinking_active 在 run() 判定）。
        if self._run_thinking_active and "thinking" in self.extensions:
            try:
                self._contemplate("reflection", self._run_task or "",
                                  last_outputs=[str(report.text or "")])
            except Exception as exc:
                self._emit(type="extension_error", module="thinking",
                           error=f"{type(exc).__name__}: {exc}")
        if self.extension_calls:
            self._emit(type="extension_calls", counts=dict(sorted(self.extension_calls.items())))
        # H14 (rev.H14-3.7): the ledger is written on EVERY exit path (final /
        # error / max_steps) — cost accounting that only records happy paths
        # undercounts exactly the runaway runs T4 exists to catch. The token
        # source is report.usage (the run's own completion rollup; session
        # events carry no token data). Children share the parent session but
        # hold cost_ledger=None, so their usage is not double-recorded here.
        if self.cost_ledger is not None and self._run_scope:
            delta = int(report.usage.get("prompt_tokens", 0)) + int(report.usage.get("completion_tokens", 0))
            delta += self._run_thinking_tokens  # 沉思轮 token 并入本跑步账
            self._run_thinking_tokens = 0
            if delta > 0:
                entry = self.cost_ledger.record(self._run_model, delta, note=f"run {self.name}")
                self._emit(type="ledger_recorded", model=entry.model, tokens=delta,
                           cost=entry.cost, scale=self._pricing_scale(self._run_probe,
                                                                      ledger=self.cost_ledger))
            self._run_scope = False
        return report

    def metabolise(self, report: "RunReport") -> dict[str, Any] | None:
        """After-run hook: feed the run through the mounted metabolism seats.

        curator metabolises knowledge state; replay renders the session into a
        human timeline. ``run()`` calls this on every exit path (host loops may
        also call it); each consultation is counted on ``extension_calls``.
        Exceptions are contained per seat — a broken extension can never take
        the run down with it.
        """
        outcome: dict[str, Any] = {}
        if "curator" in self.extensions:
            result = self._use_extension(
                "curator", [], time.time(),
                {"staleAfterSeconds": 30 * 86400,
                 "archiveAfterSeconds": 90 * 86400,
                 "protectCreatedBy": ["user", "installed"]})
            outcome["curator"] = ({"error": "seat raised; see the extension_error event"}
                                  if result is _SEAT_FAILED else result)
        if "replay" in self.extensions:
            events = [e.__dict__ if hasattr(e, "__dict__") else dict(e) for e in self.events]
            result = self._use_extension(
                "replay", [{**ev, "ordinal": i + 1} for i, ev in enumerate(events)])
            outcome["replay"] = ({"error": "seat raised; see the extension_error event"}
                                 if result is _SEAT_FAILED else result)
        return outcome or None


def sanitise_child_output(text: str) -> str:
    """Strip anything a child could use to impersonate a system directive."""
    cleaned = INJECTION_RE.sub("[redacted-directive]", text or "")
    return cleaned.strip()


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
) -> Agent:
    """Wire a full agent from a composed config tree.

    ``mount_contrib`` decides whether verified contribution modules actually
    *serve* the runtime (not just sit validated in the index): their hooks are
    resolved through the registry's alias table and handed to the Agent as an
    extension surface. A quarantined or missing module simply leaves its slot
    empty — mounting never fails the boot.
    """
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
    from .session import Session as _Session

    session = _Session(session_dir / "current.jsonl", meta={
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
    # F2-2：传真实工作区，贡献模块经 api.workspace 看到沙箱边界。
    # H11：接管/保底警告转 mount_warning 事件，不再静默。
    extensions: dict[str, Any] = {}
    mount_warnings: list[str] = []
    if mount_contrib:
        extensions, mount_warnings = mount_contrib_extensions(
            home=home, workspace=workspace, return_warnings=True)
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
        extensions=extensions,
        mount_warnings=mount_warnings,
        cost_ledger=ledger,
        # 三档模式来自配置树（bundle 行 thinking.mode；off/smart/on，默认 off）。
        thinking=config.get("thinking", "mode", "off"),
    )


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
    for module_name, canonical in (("scheduler", "due_jobs"),
                                   ("router", "match"),
                                   ("teams", "deliver"),
                                   ("curator", "curate"),
                                   ("compactor", "should_compact"),
                                   ("replay", "replay")):
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


__all__ = ["Agent", "LoopLimits", "RunReport", "Step", "THINKING_HOOKS", "THINKING_MODES",
           "ThinkingSuite", "build_agent", "mount_contrib_extensions", "sanitise_child_output"]
