"""Thinking engine: contemplation rounds with budget control and convergence.

Extracted from loop.py (0.7.0) to separate the "deep thinking" subsystem
from the agent's main iteration. This module owns:

* ``ThinkingMode`` — off / smart / on
* ``ThinkingSuite`` — runtime handle to a mounted thinking engine
* ``ContemplationEngine`` — manages thinking rounds with convergence detection
* ``looks_complex`` — heuristic task complexity check for smart mode

The Agent class in loop.py delegates contemplation to ContemplationEngine;
this keeps loop.py focused on the main iteration cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

THINKING_MODES = ("off", "smart", "on")


def normalise_thinking_mode(value: Any) -> str:
    """bool compat (True->on / False->off); unknown -> off."""
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


# Hooks that must ALL be present for a thinking suite to mount.
THINKING_HOOKS = ("should_think", "build_thinking_task", "split_thinking", "estimate_budget")


@dataclass
class ContemplationEngine:
    """Manages thinking rounds with convergence detection.

    Usage:
        engine = ContemplationEngine(suite, call_hook_fn, emit_fn)
        notes = engine.contemplate("thinking", task, last_outputs=[])
    """

    suite: Any  # ThinkingSuite | None
    call_hook: Callable[[str, Any, Any], Any]  # hook_name, *args -> result
    emit: Callable[..., None]  # emit event

    def contemplate(self, phase: str, task: str, *,
                    last_outputs: list[str],
                    run_thinking_tokens: int = 0) -> tuple[str | None, int]:
        """Run one phase's contemplation rounds.

        Returns (contemplation_text_or_None, tokens_used).
        """
        if self.suite is None:
            return None, 0

        suite = self.suite
        # estimate budget
        budget = self.call_hook(
            "estimate_budget", phase,
            task_size=len(str(task or "")), history_len=len(last_outputs))
        if budget is None or budget is _SEAT_FAILED:
            return None, 0

        # should we think?
        engage = self.call_hook("should_think", phase, budget=budget, last_result=None)
        if engage is not True:
            return None, 0

        round_types = tuple(getattr(budget, "round_types", ()) or ())
        if not round_types:
            self._emit_engaged(phase, 0, 0, "empty-budget", 0, 0)
            return None, 0

        spec_cls = getattr(suite, "round_spec", None)
        texts: list[str] = []
        tokens = 0
        stopped = "budget"
        prev = ""

        for round_type in round_types:
            # build spec
            if callable(spec_cls):
                spec: Any = spec_cls(round_type=str(round_type), prompt="", role="")
            else:
                spec = {"round_type": str(round_type)}

            row = self.call_hook(
                "build_thinking_task", phase, spec,
                {"task": str(task or ""), "prior_outputs": list(texts)})
            if not isinstance(row, dict) or row is _SEAT_FAILED:
                break

            msgs = row.get("messages") or []
            if not msgs:
                break

            # completion (caller must inject router.complete)
            try:
                completion = row["_router_complete"](msgs, small=True)
            except Exception:
                break

            usage = getattr(completion, "usage", None)
            if usage is not None:
                tokens += int(getattr(usage, "prompt_tokens", 0)) + int(getattr(usage, "completion_tokens", 0))

            split = self.call_hook("split_thinking", getattr(completion, "text", "") or "")
            if isinstance(split, tuple) and len(split) == 2:
                thinking_part, answer_part = str(split[0]), str(split[1])
            else:
                thinking_part, answer_part = "", str(getattr(completion, "text", "") or "")

            produced = (answer_part or thinking_part).strip()
            if produced:
                texts.append(produced)

            if prev:
                verdict_c = self.call_hook("converged", prev, produced)
                if verdict_c is True:
                    stopped = "converged"
                    break
            prev = produced

        notes = "\n\n".join(texts).strip()
        self._emit_engaged(phase, len(texts), len(round_types), stopped, len(notes), tokens)
        return notes or None, tokens

    def _emit_engaged(self, phase: str, rounds: int, budget: int,
                      stopped: str, chars: int, tokens: int) -> None:
        self.emit(type="thinking_engaged", phase=phase, rounds=rounds,
                  budget=budget, stopped=stopped, chars=chars, tokens=tokens)


# Sentinel for seat failure (importable for consumers)
_SEAT_FAILED = object()


def looks_complex_heuristic(task: str) -> bool:
    """Simple heuristic: tasks with multi-step indicators or long text are complex."""
    indicators = ("分析", "审查", "重构", "优化", "设计", "实现", "对比",
                  "analyze", "review", "refactor", "optimize", "design",
                  "implement", "compare", "compare", "架构", "框架")
    task_lower = task.lower()
    score = sum(1 for ind in indicators if ind in task_lower)
    score += len(task) // 500  # longer tasks are more complex
    return score >= 2


__all__ = [
    "THINKING_HOOKS",
    "THINKING_MODES",
    "ContemplationEngine",
    "ThinkingSuite",
    "looks_complex_heuristic",
    "normalise_thinking_mode",
]
