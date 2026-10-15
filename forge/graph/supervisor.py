"""Supervisor / worker pattern on the graph core.

Ported from langgraph/libs/prebuilt supervisor semantics (upstream
5eb60ae): one supervisor node routes work to named worker nodes, each
worker is a full node (or subgraph) that returns its result into a
shared state key; the supervisor decides next-worker / END / handoff.

Upstream's prebuilt supervisor wraps an LLM call for routing. Forge
keeps that pluggable: ``route`` is any callable ``state -> decision``;
the GUI / model layer can bind an LLM there while tests bind a table.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .channels import LastValue
from .state import CompiledGraph, GraphState
from .types import END, START, Command


class SupervisorGraph:
    """A supervisor graph over worker callables.

    Shape (upstream create_supervisor equivalent)::

        START -> supervisor -> (worker | END)   (loop until route says END)

    Workers write into ``result_key`` (default ``results``) as a
    LastValue channel — the supervisor reads the accumulated state to
    decide the next hop. Each worker's return value is stored under its
    own name in the results dict so parallel workers never clobber each
    other (dict-merge reducer on the results key).
    """

    def __init__(
        self,
        workers: Mapping[str, Callable[[dict], Any]],
        route: Callable[[dict], str],
        *,
        result_key: str = "results",
    ) -> None:
        if not workers:
            raise ValueError("supervisor needs at least one worker")
        if route is None:
            raise ValueError("supervisor needs a route callable")
        if "supervisor" in workers:
            raise ValueError("worker name 'supervisor' is reserved")
        self.workers = dict(workers)
        self.route = route
        self.result_key = result_key

    def compile(self) -> CompiledGraph:
        from typing import Annotated

        # state schema: results accumulates per-worker outputs (dict-merge
        # reducer — each worker writes {name: output}, peers never clobber)
        schema = {self.result_key: Annotated[dict, dict_merge]}

        def supervisor_node(state: dict) -> Command:
            decision = self.route(state)
            if decision in (END, "__end__", None, ""):
                return Command(goto=END)
            if decision not in self.workers:
                raise ValueError(
                    f"supervisor route returned unknown worker {decision!r}"
                )
            return Command(goto=decision)

        builder = GraphState(schema)
        builder.add_node("supervisor", supervisor_node)
        for name, fn in self.workers.items():
            def make_worker(worker_fn, worker_name):
                def worker(state: dict) -> dict:
                    output = worker_fn(state)
                    return {self.result_key: {worker_name: output}}
                worker.__name__ = worker_name
                return worker
            builder.add_node(name, make_worker(fn, name))
            # worker -> supervisor (loop back for the next decision)
            builder.add_edge(name, "supervisor")
        builder.add_edge(START, "supervisor")
        return builder.compile()

    def invoke(self, input: dict, *, config: dict | None = None) -> dict[str, Any]:
        from .loop import GraphRunner

        runner = GraphRunner(self.compile(), config=config)
        return runner.invoke(input).values


def create_supervisor(
    workers: Mapping[str, Callable[[dict], Any]],
    route: Callable[[dict], str],
    *,
    result_key: str = "results",
) -> SupervisorGraph:
    """Factory matching upstream ``create_supervisor`` naming."""
    return SupervisorGraph(workers, route, result_key=result_key)


def dict_merge(left: dict | None, right: dict | None) -> dict:
    """Standalone reducer users can reference in their own schemas."""
    out = dict(left or {})
    out.update(right or {})
    return out


__all__ = ["SupervisorGraph", "create_supervisor", "dict_merge"]
