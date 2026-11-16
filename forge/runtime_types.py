"""Shared runtime values, independent of Agent assembly and protocols."""
from dataclasses import dataclass, field
from typing import Any


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
    action_id: str = ''
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    text: str
    steps: list[Step]
    usage: dict[str, Any]
    stopped: str = "final"          # final | max_steps | error | planning_error | format_error | stalled | converged | unknown_outcome | cancelled
    events: list[dict[str, Any]] = field(default_factory=list)
    planning: dict[str, Any] | None = None
    run_id: str = ''
    task_id: str = ''
    parent_run_id: str = ''

    def as_result(self):
        from .execution import RunResult
        return RunResult.from_report(self)

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.tool)


