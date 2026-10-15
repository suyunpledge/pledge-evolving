"""The superstep loop: run a compiled graph to completion or a pause.

Ported from langgraph/pregel/_loop.py tick()/after_tick() (upstream
5eb60ae), reduced to the synchronous loop forge's kernel discipline
expects. Each iteration:

1. ``prepare_next_tasks`` — which nodes fire this step
2. execute every task (node callable or Send target)
3. interrupt check — ``interrupt()`` calls and ``interrupt_before``
   breakpoints pause here with the checkpoint already saved
4. ``apply_writes`` — fold outputs into channels, resolve routing
5. checkpoint — persist state for resume
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .algo import (
    Task,
    apply_writes,
    channel_versions,
    mark_seen,
    prepare_next_tasks,
    reset_versions,
    set_channel_versions,
)
from .channels import BaseChannel, EmptyChannelError
from .checkpoint import BaseGraphSaver, ExecutionCheckpoint
from .state import CompiledGraph, _branch_channel
from .types import END, START, Command, GraphInterrupt, Interrupt, MISSING, Send
from .stream import StreamEvent, emit_event


@dataclass
class RunResult:
    """Outcome of one invoke/resume call (upstream's values + metadata)."""

    values: dict[str, Any] = field(default_factory=dict)
    interrupts: list[Interrupt] = field(default_factory=list)
    steps: int = 0
    status: str = "done"  # done | paused | out_of_steps
    thread_id: str = ""


class GraphRunner:
    """Execute a CompiledGraph with optional persistence and streaming."""

    def __init__(
        self,
        graph: CompiledGraph,
        *,
        config: dict | None = None,
        checkpointer: BaseGraphSaver | None = None,
        max_steps: int = 100,
        interrupt_before: list[str] | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> None:
        self.graph = graph
        self.config = config or {}
        self.checkpointer = checkpointer
        self.max_steps = max_steps
        self.interrupt_before = interrupt_before or []
        self.on_event = on_event
        self.thread_id = str(self.config.get("thread_id", "")) or _gen_id()

    # -- state ------------------------------------------------------------
    def _fresh_channels(self) -> dict[str, BaseChannel]:
        return {
            key: ch.from_checkpoint(ch.checkpoint())
            if False
            else ch.__class__.__new__(ch.__class__)
            for key, ch in self.graph.channels.items()
        }

    def _init_channels(self) -> dict[str, BaseChannel]:
        """Build fresh channel instances from the compiled graph."""
        out: dict[str, BaseChannel] = {}
        for key, proto in self.graph.channels.items():
            fresh = proto.__class__.__new__(proto.__class__)
            fresh.__init__(proto.typ, proto.key)
            # preserve custom operator for BinaryOperatorAggregate
            if hasattr(proto, "operator"):
                fresh.operator = proto.operator  # type: ignore[attr-defined]
            out[key] = fresh
        return out

    # -- invoke --------------------------------------------------------------
    def invoke(self, input: Any, *, resume: Any = MISSING) -> RunResult:
        """Run to completion, a breakpoint, or an interrupt.

        ``resume`` carries the human response for a prior interrupt
        (``Command(resume=...)`` payload unwrapped by the caller).
        """
        # restore or initialise
        ckpt = self.checkpointer.get(self.thread_id) if self.checkpointer else None
        if ckpt is not None:
            channels = self._restore_channels(ckpt)
            versions_seen = {n: dict(v) for n, v in ckpt.versions_seen.items()}
            versions = dict(ckpt.channel_versions)
            step = ckpt.step
            resume_value = resume
            input_value = MISSING
        else:
            channels = self._init_channels()
            versions_seen: dict[str, dict[str, int]] = {}
            versions = {}
            step = 0
            resume_value = MISSING
            input_value = input

        set_channel_versions(channels, versions)
        entry_targets = self.graph.entry_targets()
        pending_sends: list[Send] = []
        branch_results: dict[str, list[str | Send]] = {}
        result = RunResult(thread_id=self.thread_id)
        result.status = "running"

        while step < self.max_steps:
            # -- 1. schedule ------------------------------------------------
            if step == 0 and input_value is not MISSING:
                # seed input into state channels (LastValue/reducer semantics)
                writes = _input_writes(self.graph, input_value)
                for key, value in writes:
                    ch = channels.get(key)
                    if ch is not None:
                        ch.update([value])
                        self._bump(channels, key)
                # fire entry branch channels
                for target in entry_targets:
                    if target in self.graph.nodes:
                        bc = channels.get(_branch_channel(target))
                        if bc is not None:
                            bc.update([START])
                            self._bump(channels, _branch_channel(target))
                tasks = prepare_next_tasks(
                    self.graph, channels, versions_seen, step,
                    entry_targets=entry_targets,
                )
                # mark input handled
                input_value = MISSING
            elif resume_value is not MISSING:
                # replay: re-run the interrupted node with the response
                tasks = self._resume_tasks(channels, versions_seen, resume_value)
                resume_value = MISSING
                if not tasks:
                    tasks = prepare_next_tasks(
                        self.graph, channels, versions_seen, step,
                        pending_sends=pending_sends,
                        entry_targets=entry_targets,
                    )
            else:
                tasks = prepare_next_tasks(
                    self.graph, channels, versions_seen, step,
                    pending_sends=pending_sends,
                    entry_targets=entry_targets,
                )

            if not tasks:
                result.status = "done"
                break

            # -- 2. interrupt_before breakpoint -------------------------------
            hits = [t for t in tasks if t.name in self.interrupt_before]
            if hits and not self._is_resume(step):
                self._save(channels, versions_seen, step, result)
                result.status = "paused"
                result.interrupts = [Interrupt({"breakpoint": t.name}, ns=(t.name,)) for t in hits]
                result.steps = step
                return result

            # -- 3. execute ------------------------------------------------
            executed_any = False
            for task in tasks:
                emit_event(self.on_event, "tasks", name=task.name, step=step)
                writes, branches, node_interrupt = self._execute_task(task, resume_value)
                executed_any = True
                if node_interrupt is not None:
                    self._save(channels, versions_seen, step, result)
                    result.status = "paused"
                    result.interrupts = [node_interrupt]
                    result.steps = step
                    return result
                task.writes = writes
                task.branches = branches
                mark_seen(versions_seen, task, channels)

            if not executed_any:
                result.status = "done"
                break

            # -- 4a. state writes first: routers must see the fresh state --
            for task in tasks:
                for key, value in task.writes:
                    ch = channels.get(key)
                    if ch is not None:
                        ch.update([value])
                        self._bump(channels, key)

            # -- 4b. routing: conditional routers + node-resolved branches --
            branch_results = {}
            for task in tasks:
                targets = list(task.branches)
                router = self.graph._branches.get(task.name)
                if router is not None:
                    state_now = self._read_state(channels)
                    routed = router(state_now)
                    if routed is None:
                        routed = []
                    elif not isinstance(routed, (list, tuple)):
                        routed = [routed]
                    targets = list(routed) + [
                        t for t in targets if t not in routed
                    ]
                branch_results[task.name] = targets

            # -- 4c. control flow only (state already applied in 4a) ------
            pending_sends = apply_writes(
                self.graph, channels, tasks,
                branch_results=branch_results,
                entry_targets=entry_targets,
                apply_state=False,
            )
            emit_event(
                self.on_event, "values",
                values=self._read_state(channels), step=step,
            )

            # -- 5. checkpoint ----------------------------------------------
            step += 1
            self._save(channels, versions_seen, step, result)

        else:
            result.status = "out_of_steps"

        result.steps = step
        result.values = self._read_state(channels)
        return result

    # -- resume ----------------------------------------------------------------
    def resume(self, response: Any) -> RunResult:
        """Resume a paused graph with the human's response."""
        return self.invoke(MISSING, resume=response)

    # -- helpers ---------------------------------------------------------------
    def _execute_task(
        self, task: Task, resume_value: Any
    ) -> tuple[list[tuple[str, Any]], list[str | Send], Interrupt | None]:
        """Run the node callable; capture writes, routing, interrupts."""
        from .types import _NodeInterrupt

        node_input = task.input
        try:
            output = self.graph.nodes[task.name].fn(node_input)
        except _NodeInterrupt as ni:
            intr = Interrupt(ni.value, ns=(task.name,))
            return [], [], intr
        except Exception as exc:
            # surface node failures as error writes; upstream routes to
            # error handlers, forge surfaces to the run result
            raise

        writes: list[tuple[str, Any]] = []
        branches: list[str | Send] = []
        if isinstance(output, Command):
            writes.extend(output._update_as_tuples())
            if output.goto:
                gotos = output.goto if isinstance(output.goto, (list, tuple)) else [output.goto]
                branches.extend(gotos)
            elif output.resume is not None:
                pass  # resume handled by the runner
        elif isinstance(output, dict):
            writes.extend(output.items())
        elif isinstance(output, (list, tuple)) and output and all(
            isinstance(o, Command) for o in output
        ):
            for cmd in output:
                writes.extend(cmd._update_as_tuples())
                if cmd.goto:
                    gotos = cmd.goto if isinstance(cmd.goto, (list, tuple)) else [cmd.goto]
                    branches.extend(gotos)
        elif output is not None:
            writes.append(("__root__", output))

        # static edges when no explicit goto
        if not branches:
            branches.extend(self.graph._static_successors(task.name))
        return writes, branches, None

    def _resume_tasks(
        self, channels: dict[str, BaseChannel], versions_seen: dict, resume_value: Any
    ) -> list[Task]:
        """Re-run interrupted nodes, injecting the response as input."""
        ckpt = self.checkpointer.get(self.thread_id) if self.checkpointer else None
        if ckpt is None or not ckpt.pending_writes:
            return []
        tasks: list[Task] = []
        for node_name, writes in ckpt.pending_writes.items():
            spec = self.graph.nodes.get(node_name)
            if spec is None:
                continue
            view = self._read_state(channels)
            view["__resume__"] = resume_value
            tasks.append(Task(name=node_name, input=view))
        return tasks

    def _is_resume(self, step: int) -> bool:
        return self.config.get("__is_resume__", False)

    def _read_state(self, channels: dict[str, BaseChannel]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in self.graph.state_keys:
            ch = channels.get(key)
            if ch is not None:
                try:
                    out[key] = ch.get()
                except EmptyChannelError:
                    pass
        return out

    def _bump(self, channels: dict[str, BaseChannel], key: str) -> None:
        from .algo import bump_channel

        bump_channel(channels, key)

    def _save(
        self,
        channels: dict[str, BaseChannel],
        versions_seen: dict[str, dict[str, int]],
        step: int,
        result: RunResult,
    ) -> None:
        if self.checkpointer is None:
            return
        values = {
            key: ch.checkpoint()
            for key, ch in channels.items()
            if ch.checkpoint() is not MISSING
        }
        ckpt = ExecutionCheckpoint(
            thread_id=self.thread_id,
            step=step,
            channel_values=values,
            channel_versions=channel_versions(channels),
            versions_seen={n: dict(v) for n, v in versions_seen.items()},
        )
        self.checkpointer.put(ckpt)

    def _restore_channels(self, ckpt: ExecutionCheckpoint) -> dict[str, BaseChannel]:
        channels = self._init_channels()
        for key, ch in channels.items():
            if key in ckpt.channel_values:
                fresh = self.graph.channels[key].from_checkpoint(ckpt.channel_values[key])
                channels[key] = fresh
        return channels


def _input_writes(graph: CompiledGraph, input: Any) -> list[tuple[str, Any]]:
    if isinstance(input, dict):
        return [(k, v) for k, v in input.items() if k in graph.state_keys]
    if input is not None:
        return [("__root__", input)]
    return []


def _gen_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


__all__ = ["GraphRunner", "RunResult"]
