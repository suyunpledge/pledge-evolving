"""Scheduler: turn ready channels into runnable tasks; apply node writes.

Ported from langgraph/pregel/_algo.py (upstream 5eb60ae) — the
``prepare_next_tasks`` / ``apply_writes`` pair that sits at the heart of
the Pregel superstep loop, reduced to the synchronous single-process
form forge needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .channels import BaseChannel, EmptyChannelError
from .state import CompiledGraph, _barrier_channel, _branch_channel
from .types import END, START, Command, MISSING, Send


@dataclass
class Task:
    """One executable unit in a superstep (upstream PregelExecutableTask)."""

    name: str
    input: Any = None
    writes: list[tuple[str, Any]] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    # routing resolved by the runner: static edges / Command.goto targets
    branches: list[Any] = field(default_factory=list)
    # private state for Send tasks (map-reduce fan-out)
    is_send: bool = False

    def path(self) -> str:
        return self.name


class _BarrierPending(Exception):
    pass


def _node_reads(graph: CompiledGraph, channels: dict[str, BaseChannel], name: str) -> dict[str, Any]:
    """Materialise the node's view of the state (state keys only)."""
    view: dict[str, Any] = {}
    for key in graph.state_keys:
        ch = channels.get(key)
        if ch is not None:
            try:
                view[key] = ch.get()
            except EmptyChannelError:
                pass
    return view


def prepare_next_tasks(
    graph: CompiledGraph,
    channels: dict[str, BaseChannel],
    versions_seen: dict[str, dict[str, int]],
    step: int,
    pending_sends: list[Send] | None = None,
    entry_targets: list[str] | None = None,
) -> list[Task]:
    """Decide which nodes run in this superstep.

    A node runs when one of its trigger channels was updated since the
    node last saw it (upstream ``versions_seen`` bookkeeping). ``Send``
    tasks from the previous step's conditional edges run alongside
    channel-triggered nodes.
    """
    tasks: list[Task] = []
    sent_names: set[str] = set()

    # 1. Send tasks (fan-out): each becomes a task with a private input
    for send in pending_sends or []:
        if send.node in graph.nodes:
            tasks.append(Task(name=send.node, input=send.arg, is_send=True))
            sent_names.add(send.node)

    # 2. Channel-triggered nodes
    for name, spec in graph.nodes.items():
        if name in sent_names:
            continue  # a Send to this node already queued it this step
        for trigger in spec.triggers:
            ch = channels.get(trigger)
            if ch is None:
                continue
            seen = versions_seen.get(name, {})
            version = _channel_version(channels, trigger)
            if version > seen.get(trigger, -1) and _channel_ready(ch):
                tasks.append(
                    Task(
                        name=name,
                        input=_node_reads(graph, channels, name),
                        triggers=[trigger],
                    )
                )
                break

    # 3. START: entry branch channels fire on step 0 (or after a reset)
    if step == 0 and entry_targets:
        for target in entry_targets:
            if target == START:
                continue  # conditional START resolved by the caller
            if not any(t.name == target for t in tasks):
                tasks.append(
                    Task(
                        name=target,
                        input=_node_reads(graph, channels, target),
                        triggers=[_branch_channel(target)],
                    )
                )
    return tasks


_VERSION_KEY = "__version__"


def _channel_version(channels: dict[str, BaseChannel], key: str, cache: dict | None = None) -> int:
    """Monotonic per-channel version tracked in a side table."""
    versions = _versions_entry(channels)
    return versions.get(key, 0)


# Side table keyed by the channels dict itself (not id()) so a recycled
# id() can never hand a fresh graph stale version numbers. Plain dict keys
# are not weakref-able on CPython, so hold the channels set alive for the
# table's lifetime via an explicit strong key — correctness over GC-timing:
# entries are dropped deterministically in reset_versions()/run() teardown,
# which forge's runner already calls (loop.py imports reset_versions).
_VERSIONS: dict[int, tuple[dict, dict[str, int]]] = {}


def _versions_entry(channels: dict[str, BaseChannel]) -> dict[str, int]:
    entry = _VERSIONS.get(id(channels))
    if entry is None or entry[0] is not channels:
        # id collision after GC (or first sight): fresh version table
        entry = (channels, {})
        _VERSIONS[id(channels)] = entry
    return entry[1]


def bump_channel(channels: dict[str, BaseChannel], key: str) -> None:
    entry = _versions_entry(channels)
    entry[key] = entry.get(key, 0) + 1


def channel_versions(channels: dict[str, BaseChannel]) -> dict[str, int]:
    return dict(_versions_entry(channels))


def set_channel_versions(channels: dict[str, BaseChannel], versions: dict[str, int]) -> None:
    _VERSIONS[id(channels)] = (channels, dict(versions))


def reset_versions(channels: dict[str, BaseChannel]) -> None:
    entry = _VERSIONS.get(id(channels))
    if entry is not None and entry[0] is channels:
        del _VERSIONS[id(channels)]


def _channel_ready(ch: BaseChannel) -> bool:
    try:
        return ch.is_available()
    except Exception:
        return False


def apply_writes(
    graph: CompiledGraph,
    channels: dict[str, BaseChannel],
    tasks: list[Task],
    *,
    branch_results: dict[str, list[str | Send]] | None = None,
    entry_targets: list[str] | None = None,
    apply_state: bool = True,
) -> list[Send]:
    """Fold task writes into channels; return next-step Sends.

    * plain dict writes go through each key's channel reducer
    * ``Command(goto=...)`` overrides the node's static edges
    * conditional-edge routers resolved by the caller arrive as
      ``branch_results[node] = [targets...]``
    * barriers receive the start-node name and fire on completion
    * every write bumps the channel version so the next
      ``prepare_next_tasks`` sees it as new
    """
    sends: list[Send] = []
    branch_results = branch_results or {}

    for task in tasks:
        node = task.name

        # -- state writes (runner may pre-apply so routers see fresh state) --
        if apply_state:
            for key, value in task.writes:
                ch = channels.get(key)
                if ch is None:
                    continue  # unknown key: schema mismatch, dropped later
                ch.update([value])
                bump_channel(channels, key)

        # -- control flow ---------------------------------------------------
        targets: list[str | Send] = []
        if node in branch_results:
            targets = list(branch_results[node])
        else:
            targets = graph._static_successors(node)

        for target in targets:
            if isinstance(target, Send):
                sends.append(target)
            elif target == END:
                pass  # run completes when no tasks remain
            else:
                bc = channels.get(_branch_channel(target))
                if bc is not None:
                    bc.update([node])
                    bump_channel(channels, _branch_channel(target))
                # fan-in barrier publish
                for starts, end in graph._fanins_for(target):
                    barrier = channels.get(_barrier_channel(starts, target))
                    if barrier is not None:
                        barrier.update([node])
                        bump_channel(channels, _barrier_channel(starts, target))

    return sends


def mark_seen(versions_seen: dict[str, dict[str, int]], task: Task, channels: dict[str, BaseChannel]) -> None:
    versions_seen.setdefault(task.name, {})
    for trigger in task.triggers:
        versions_seen[task.name][trigger] = _channel_version(channels, trigger)


__all__ = [
    "Task",
    "prepare_next_tasks",
    "apply_writes",
    "mark_seen",
    "bump_channel",
    "channel_versions",
    "set_channel_versions",
    "reset_versions",
]

