"""Graph builder: nodes, edges, conditional branches, compilation.

Ported from langgraph/graph/state.py (upstream 5eb60ae) with the
edge-as-channel-subscription model intact:

* a static edge ``a -> b`` makes ``a`` write the per-node branch channel
  ``branch:to:b`` which ``b`` subscribes to as its trigger
* a conditional edge runs a router that returns node names or ``Send``
  objects; ``Send`` pushes a private-state task (map-reduce)
* fan-in ``[a, b] -> c`` becomes a NamedBarrier channel that only
  updates once every start has published (join semantics)

The builder is deliberately dumb: it collects declarations and resolves
them at ``compile()`` time into channels + node specs the scheduler
understands.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any, get_type_hints

from .channels import (
    BaseChannel,
    BinaryOperatorAggregate,
    EphemeralValue,
    LastValue,
    channel_for_annotation,
)
from .types import END, START, Command, MISSING, Send


class GraphError(Exception):
    """Invalid graph declaration or compilation."""


def _branch_channel(node: str) -> str:
    return f"branch:to:{node}"


def _barrier_channel(starts: Sequence[str], end: str) -> str:
    return f"join:{'+'.join(starts)}:{end}"


class NodeSpec:
    """A declared node: its callable, trigger channels, and writers."""

    __slots__ = ("name", "fn", "triggers", "defer", "metadata")

    def __init__(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        triggers: list[str] | None = None,
        defer: bool = False,
        metadata: dict | None = None,
    ) -> None:
        self.name = name
        self.fn = fn
        self.triggers: list[str] = triggers or [_branch_channel(name)]
        self.defer = defer
        self.metadata = metadata or {}


class CompiledGraph:
    """Immutable graph: channels + node specs, executed by GraphRunner."""

    def __init__(
        self,
        channels: dict[str, BaseChannel],
        nodes: dict[str, NodeSpec],
        state_keys: list[str],
        *,
        edges: list[tuple[list[str], str]] | None = None,
        entry_targets: list[str] | None = None,
        branches: dict[str, Callable] | None = None,
    ) -> None:
        self.channels = channels
        self.nodes = nodes
        self.state_keys = state_keys
        self._edges = edges or []
        self._entry_targets = entry_targets or []
        self._branches = branches or {}

    # -- topology queries (used by the scheduler) ------------------------
    def _static_successors(self, node: str) -> list[str]:
        """Static-edge targets from ``node`` (control flow it owns)."""
        return [end for starts, end in self._edges if node in starts and end != END]

    def _fanins_for(self, target: str) -> list[tuple[list[str], str]]:
        """Fan-in edge groups whose barrier publishes into ``target``."""
        return [
            (starts, end)
            for starts, end in self._edges
            if end == target and len(starts) > 1
        ]

    def entry_targets(self) -> list[str]:
        return list(self._entry_targets)

    def read_state(self, channels: dict[str, BaseChannel] | None = None) -> dict[str, Any]:
        """Snapshot the readable state (for tests and debugging)."""
        live = channels if channels is not None else self.channels
        out: dict[str, Any] = {}
        for key in self.state_keys:
            ch = live.get(key)
            if ch is not None and ch.is_available():
                out[key] = ch.get()
        return out


class GraphState:
    """Declarative builder, upstream ``StateGraph`` equivalent.

    ``state_schema`` is a TypedDict (or plain class annotations mapping)
    whose ``Annotated[T, reducer]`` keys declare merge semantics — same
    contract as upstream, resolved through ``get_type_hints``.
    """

    def __init__(self, state_schema: type | dict[str, Any]) -> None:
        self._schema: dict[str, Any] = _resolve_schema(state_schema)
        self._nodes: dict[str, NodeSpec] = {}
        self._edges: list[tuple[Sequence[str], str]] = []
        self._branches: list[tuple[str, str, Callable]] = []  # (start, name, router)
        self._entry: str | None = None

    # -- declaration -----------------------------------------------------
    def add_node(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        defer: bool = False,
        metadata: dict | None = None,
    ) -> "GraphState":
        if name in (START, END):
            raise GraphError(f"node name {name!r} is reserved")
        if name in self._nodes:
            raise GraphError(f"duplicate node {name!r}")
        if not callable(fn):
            raise GraphError(f"node {name!r}: fn must be callable")
        self._nodes[name] = NodeSpec(name, fn, defer=defer, metadata=metadata)
        return self

    def add_edge(self, start: str | Sequence[str], end: str) -> "GraphState":
        starts = [start] if isinstance(start, str) else list(start)
        for s in starts:
            if s != START and s not in self._nodes:
                raise GraphError(f"edge references unknown node {s!r}")
        if end != END and end not in self._nodes:
            raise GraphError(f"edge references unknown node {end!r}")
        if len(starts) > 1 and end == END:
            # fan-out to END: no barrier needed, each path finishes alone
            for s in starts:
                self._edges.append(([s], end))
            return self
        self._edges.append((starts, end))
        return self

    def add_conditional_edges(
        self,
        start: str,
        router: Callable[[Any], Any],
        path_map: dict[str, str] | None = None,
    ) -> "GraphState":
        """Conditional routing: ``router(state) -> node name | list | Send.

        ``path_map`` optionally remaps router outputs to node names
        (upstream ``path_map`` contract).
        """
        if start != START and start not in self._nodes:
            raise GraphError(f"branch references unknown node {start!r}")
        if not callable(router):
            raise GraphError("router must be callable")
        self._branches.append((start, f"branch:{start}", router))
        if path_map:
            self._path_maps[start] = path_map
        return self

    _path_maps: dict[str, dict[str, str]]

    def set_entry_point(self, name: str) -> "GraphState":
        if name not in self._nodes:
            raise GraphError(f"entry point {name!r} is not a node")
        self._entry = name
        return self

    # -- compilation -------------------------------------------------------
    def compile(self) -> CompiledGraph:
        channels: dict[str, BaseChannel] = {}
        # state channels from schema annotations
        for key, annotation in self._schema.items():
            channels[key] = channel_for_annotation(annotation, key)
        # per-node branch channels (triggers)
        for name in self._nodes:
            channels[_branch_channel(name)] = EphemeralValue(Any, _branch_channel(name))
        # static edges: single-start wires the writer; fan-in uses barriers
        for starts, end in self._edges:
            if end == END:
                continue
            if len(starts) == 1:
                # start -> end: end's trigger channel gains start as writer
                # (writer set tracked implicitly: START publishes all
                #  branch channels it edges to; node completion publishes
                #  its own branch channel — see algo.prepare_next_tasks)
                if starts[0] == START:
                    self._require_entry_default(starts[0])
            else:
                barrier = _barrier_channel(starts, end)
                channels[barrier] = _NamedBarrier(barrier, set(starts))
        # conditional branches need no extra channel: the router result is
        # resolved by the scheduler at write time
        # START node: publishes entry branch channels
        entry_targets = self._entry_targets()
        if not entry_targets:
            raise GraphError("graph has no edges from START; add_edge(START, ...) first")
        # branch channels for conditional edges' targets are declared on
        # demand (the router result decides which fire); collect routers
        branch_routers = {start: router for start, _n, router in self._branches}
        return CompiledGraph(
            channels,
            dict(self._nodes),
            list(self._schema.keys()),
            edges=[(list(s), e) for s, e in self._edges],
            entry_targets=entry_targets,
            branches=branch_routers,
        )

    def _require_entry_default(self, start: str) -> None:
        if start == START and self._entry is not None:
            raise GraphError("cannot combine add_edge(START, x) with set_entry_point")

    def _entry_targets(self) -> list[str]:
        targets: list[str] = []
        if self._entry:
            targets.append(self._entry)
        for starts, end in self._edges:
            if starts == [START] and end != END:
                targets.append(end)
        for start, _name, _router in self._branches:
            if start == START:
                targets.append(start)  # resolved dynamically
        return targets

    # -- invocation sugar ----------------------------------------------------
    def invoke(self, input: Any, *, config: dict | None = None) -> dict[str, Any]:
        """Compile-and-run convenience (single-shot, no checkpointer)."""
        from .loop import GraphRunner

        graph = self.compile()
        runner = GraphRunner(graph, config=config)
        result = runner.invoke(input)
        return result.values


def _resolve_schema(state_schema: type | dict[str, Any]) -> dict[str, Any]:
    if isinstance(state_schema, dict):
        return dict(state_schema)
    try:
        hints = get_type_hints(state_schema, include_extras=True)
    except Exception:
        hints = dict(getattr(state_schema, "__annotations__", {}) or {})
    return dict(hints)


class _NamedBarrier(BaseChannel):
    """Fan-in barrier: updates only after every start node published.

    Ported from upstream NamedBarrierValue: the channel collects the set
    of start-node names it has seen in this superstep and becomes
    available (triggering the join target) only when the declared set is
    complete.
    """

    __slots__ = ("names", "seen")

    def __init__(self, key: str, names: set[str]) -> None:
        super().__init__(Any, key)
        self.names = frozenset(names)
        self.seen: set[str] = set()

    def checkpoint(self) -> Any:
        return sorted(self.seen)

    def from_checkpoint(self, checkpoint: Any) -> "_NamedBarrier":
        ch = _NamedBarrier(self.key, set(self.names))
        if checkpoint is not MISSING:
            ch.seen = set(checkpoint)
        return ch

    def get(self) -> Any:
        if not self.names or not self.names <= self.seen:
            raise _BarrierPending(self.key)
        return True

    def is_available(self) -> bool:
        return bool(self.names) and self.names <= self.seen

    def update(self, values: Sequence[Any]) -> bool:
        before = self.names <= self.seen
        for v in values:
            if isinstance(v, str):
                self.seen.add(v)
        after = self.names <= self.seen
        if after and not before:
            return True
        # reset for the next join round once consumed
        return False

    def consume(self) -> bool:
        if self.names <= self.seen:
            self.seen.clear()
            return True
        return False


class _BarrierPending(Exception):
    """Internal: barrier not yet complete (not an error for the caller)."""


__all__ = ["GraphState", "CompiledGraph", "NodeSpec", "GraphError"]
