"""Graph orchestration layer for forge.

A dependency-free port of the LangGraph orchestration semantics (state
channels, Pregel-style superstep loop, interrupt/resume, Send/Command)
adapted to forge's philosophy: stdlib only, testable offline, single
thread per session.

Layers:
    types        - Send / Command / Interrupt / GraphInterrupt / sentinels
    channels     - state containers with per-key reducers (LastValue, BinOp)
    state        - GraphState builder (add_node / add_edge / compile)
    algo         - task preparation + write application (the scheduler)
    loop         - GraphRunner: the superstep loop with checkpoint hooks
    checkpoint   - execution checkpoints (state-level, JSON) + savers
    supervisor   - supervisor / worker pattern on top of the core
    stream       - streaming modes + trace events

Differences from upstream LangGraph (deliberate):
    * no langchain-core / pydantic / xxhash dependency; reducers are
      declared via Annotated[...] exactly like upstream, but schema
      validation is duck-typed
    * checkpoints are plain JSON documents (channel_values +
      channel_versions + versions_seen + pending_writes) addressed by
      thread_id, persisted by FileGraphSaver as JSONL append
    * the runner is synchronous; forge serialises runs per session lane
      already, and async would only add event-loop plumbing that the
      host does not need
    * workspace-level shadow-git checkpoints (forge/checkpoint.py) are
      orthogonal: they snapshot *files*; this module snapshots *graph
      state* so an interrupted run can resume exactly where it stopped
"""

from .types import START, END, Command, GraphInterrupt, Interrupt, Send
from .channels import (
    BinaryOperatorAggregate,
    EphemeralValue,
    LastValue,
    channel_for_annotation,
)
from .state import CompiledGraph, GraphState
from .algo import apply_writes, prepare_next_tasks
from .loop import GraphRunner, RunResult
from .checkpoint import (
    ExecutionCheckpoint,
    FileGraphSaver,
    InMemorySaver,
)
from .supervisor import SupervisorGraph, create_supervisor
from .stream import StreamEvent, TraceCollector

__all__ = [
    "START", "END", "Command", "GraphInterrupt", "Interrupt", "Send",
    "BinaryOperatorAggregate", "EphemeralValue", "LastValue",
    "channel_for_annotation",
    "GraphState", "CompiledGraph",
    "apply_writes", "prepare_next_tasks",
    "GraphRunner", "RunResult",
    "ExecutionCheckpoint", "FileGraphSaver", "InMemorySaver",
    "SupervisorGraph", "create_supervisor",
    "StreamEvent", "TraceCollector",
]


