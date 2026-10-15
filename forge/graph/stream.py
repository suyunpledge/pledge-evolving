"""Streaming + trace: observe execution without changing it.

Upstream LangGraph's stream modes (values / updates / tasks / debug)
and the LangSmith-style trace discipline, reduced to a callback
protocol forge can wire into its own event bus (enqueue_event) or the
GUI's evidence feed.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class StreamEvent:
    """One observation of the run (upstream StreamMode payload)."""

    mode: str  # tasks | values | updates | debug | error
    step: int = 0
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    # convenience passthrough for emit_event kwargs
    def __init__(self, mode: str, **data: Any) -> None:
        self.mode = mode
        self.step = int(data.pop("step", 0))
        self.ts = time.time()
        self.data = data

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "step": self.step, "ts": self.ts, **self.data}


def emit_event(
    sink: Callable[[StreamEvent], None] | None, mode: str, **data: Any
) -> None:
    if sink is None:
        return
    try:
        sink(StreamEvent(mode, **data))
    except Exception:
        pass  # observation must never fail the run


class TraceCollector:
    """Collect every event into a queryable run trace.

    The observability primitive: attach one to a GraphRunner and after
    the run you have a complete execution timeline — which nodes ran in
    which superstep, what each wrote, how long each took. The trace is
    plain data; renderers (GUI evidence tab, JSON export) consume it.
    """

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self._t0 = time.time()

    def __call__(self, event: StreamEvent) -> None:
        self.events.append(event)

    # -- queries ------------------------------------------------------------
    def node_runs(self, node: str | None = None) -> list[StreamEvent]:
        return [
            e for e in self.events
            if e.mode == "tasks" and (node is None or e.data.get("name") == node)
        ]

    def steps(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = defaultdict(list)
        for e in self.events:
            if e.mode == "tasks":
                out[e.step].append(str(e.data.get("name")))
        return dict(out)

    def state_snapshots(self) -> list[dict[str, Any]]:
        return [e.data.get("values", {}) for e in self.events if e.mode == "values"]

    def errors(self) -> list[StreamEvent]:
        return [e for e in self.events if e.mode == "error"]

    def elapsed(self) -> float:
        return time.time() - self._t0

    # -- export ------------------------------------------------------------
    def to_json(self) -> str:
        import json

        return json.dumps(
            [e.as_dict() for e in self.events], ensure_ascii=False, indent=2,
            default=str,
        )

    def summary(self) -> str:
        step_map = self.steps()
        lines = [f"trace: {len(self.events)} events, {len(step_map)} steps, {self.elapsed():.3f}s"]
        for step in sorted(step_map):
            lines.append(f"  step {step}: {' -> '.join(step_map[step])}")
        return "\n".join(lines)


__all__ = ["StreamEvent", "emit_event", "TraceCollector"]
