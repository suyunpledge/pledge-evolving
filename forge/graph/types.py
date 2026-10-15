"""Core graph types: control-flow packets, interrupts, sentinels.

Ported from langgraph/types.py and langgraph/constants.py (upstream
5eb60ae), stripped to the sync subset forge needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# -- graph sentinels ---------------------------------------------------

START = "__start__"
END = "__end__"
ROOT = "__root__"
INTERRUPT = "__interrupt__"


class _Missing:
    """Sentinel for 'no value yet' (distinct from None which is a value)."""

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


class GraphInterrupt(Exception):
    """Raised by the runner when execution pauses at a breakpoint.

    The graph state is already checkpointed; resume by invoking the
    runner again with a ``Command(resume=...)`` payload.
    """

    def __init__(self, interrupts: list["Interrupt"] | None = None) -> None:
        super().__init__("graph interrupted")
        self.interrupts = interrupts or []


class GraphBubble(Exception):
    """Raised inside a node to surface an error to the nearest error
    handler rather than failing the whole run. Forge maps this onto the
    existing seat-error discipline (a failed seat is visible, not fatal)."""


@dataclass
class Interrupt:
    """A pause request emitted by a node via ``interrupt()``.

    ``value`` is whatever the node wants the human to review; ``id``
    identifies the interrupt within its node namespace so a resume can
    target it precisely (upstream v0.6 semantics, simplified sync form).
    """

    value: Any
    id: str = ""
    ns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _interrupt_id(self.ns)


def _interrupt_id(ns: tuple[str, ...]) -> str:
    import hashlib

    raw = "|".join(ns) if ns else "root"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def interrupt(value: Any) -> Any:
    """Pause the graph and wait for a human response.

    Inside a node::

        decision = interrupt({"ask": "approve deploy?", "run": run_id})

    On the paused run, the value surfaces in ``RunResult.interrupts``.
    On resume, the call returns the human's response and the node re-
    executes from its start with prior writes replayed. This mirrors
    upstream's ``langgraph.types.interrupt`` (v0.6 API, sync form).
    """
    raise _NodeInterrupt(value)


class _NodeInterrupt(BaseException):
    """Internal: carries the interrupt payload out of the node body."""

    def __init__(self, value: Any) -> None:
        super().__init__()
        self.value = value


@dataclass
class Send:
    """Dispatch a node with a private input state (map-reduce fan-out).

    Ported from upstream ``langgraph.types.Send``: usable from
    conditional edges to invoke the same node N times in parallel with
    per-copy states that may differ from the graph state.
    """

    node: str
    arg: Any

    def __repr__(self) -> str:
        return f"Send({self.node!r}, ...)"


@dataclass
class Command:
    """State update + routing instruction returned by a node.

    ``update``  – partial state update (same semantics as a plain dict
                  return, kept for symmetry with upstream)
    ``goto``    – node name(s) or Send(s) to run next; overrides the
                  node's static edges for this transition
    ``resume``  – resume payload for ``interrupt()``; used when the
                  run is re-invoked after a pause
    ``graph``   – ``None`` for current graph; ``Command.PARENT`` routes
                  through the nearest supervisor (multi-agent handoff)
    """

    update: Any = None
    goto: Any = ()
    resume: Any = None
    graph: str | None = None

    PARENT = "__parent__"

    def _update_as_tuples(self) -> list[tuple[str, Any]]:
        if isinstance(self.update, dict):
            return list(self.update.items())
        if (
            isinstance(self.update, (list, tuple))
            and self.update
            and all(
                isinstance(t, tuple) and len(t) == 2 and isinstance(t[0], str)
                for t in self.update
            )
        ):
            return list(self.update)
        if self.update is not None:
            return [(ROOT, self.update)]
        return []


__all__ = [
    "START", "END", "ROOT", "INTERRUPT", "MISSING",
    "GraphInterrupt", "GraphBubble",
    "Interrupt", "interrupt",
    "Send", "Command",
]
