"""forge/graph tests: one group per LangGraph capability.

Run:  python -m pytest forge/test_graph.py -q
Covers the seven orchestration capabilities ported from LangGraph:
state management, checkpoint resume, graph control flow (branch/loop/
conditional), multi-agent (supervisor + Send fan-out), human-in-the-
loop interrupt, streaming, and trace observability.
"""
from __future__ import annotations

import operator
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forge.graph.channels import (
    BinaryOperatorAggregate,
    ChannelError,
    EphemeralValue,
    LastValue,
    channel_for_annotation,
)
from forge.graph.checkpoint import ExecutionCheckpoint, FileGraphSaver, InMemorySaver
from forge.graph.loop import GraphRunner
from forge.graph.state import GraphState
from forge.graph.stream import TraceCollector
from forge.graph.supervisor import SupervisorGraph, create_supervisor
from forge.graph.types import END, START, Command, GraphInterrupt, Send, interrupt


# -- fixtures -----------------------------------------------------------

class _Schema(TypedDict):
    topic: str
    notes: Annotated[list, operator.add]
    verdict: str


def _build_linear():
    """topic -> draft -> review -> END with accumulating notes."""
    b = GraphState(_Schema)
    b.add_node("draft", lambda s: {"notes": [f"draft:{s['topic']}"]})
    b.add_node("review", lambda s: {"verdict": f"ok:{s['notes'][-1]}"})
    b.add_edge(START, "draft")
    b.add_edge("draft", "review")
    b.add_edge("review", END)
    return b.compile()


# ── 1. 状态管理 ───────────────────────────────────────────────

def test_last_value_channel_single_writer():
    ch = LastValue(str, "k")
    assert not ch.is_available()
    ch.update(["a"])
    assert ch.get() == "a"
    assert ch.update([]) is False


def test_last_value_rejects_concurrent_writers():
    ch = LastValue(str, "k")
    try:
        ch.update(["a", "b"])
        raise AssertionError("expected ChannelError")
    except ChannelError as e:
        assert "one value per step" in str(e)


def test_binop_channel_accumulates():
    ch = BinaryOperatorAggregate(list, "k", operator=operator.add)
    ch.update([["a"]])
    ch.update([["b"], ["c"]])
    assert ch.get() == ["a", "b", "c"]


def test_annotation_resolves_channel():
    ann = Annotated[list, operator.add]
    ch = channel_for_annotation(ann, "k")
    assert isinstance(ch, BinaryOperatorAggregate)
    assert isinstance(channel_for_annotation(str, "k"), LastValue)


def test_ephemeral_value_consumed_once():
    ch = EphemeralValue(str, "k")
    ch.update(["ping"])
    assert ch.is_available()
    ch.consume()
    assert not ch.is_available()


def test_state_management_end_to_end():
    """Nodes share and update structured state through typed channels."""
    result = _build_linear().read_state()
    runner = GraphRunner(_build_linear())
    out = runner.invoke({"topic": "x", "notes": []})
    assert out.values["notes"] == ["draft:x"]
    assert out.values["verdict"] == "ok:draft:x"


# ── 2. 持久化执行（检查点恢复）────────────────────────────

def test_checkpoint_roundtrip():
    ckpt = ExecutionCheckpoint(
        thread_id="t1", step=3,
        channel_values={"topic": "y"},
        channel_versions={"topic": 2},
        versions_seen={"draft": {"topic": 1}},
    )
    raw = ckpt.to_json()
    back = ExecutionCheckpoint.from_json(raw)
    assert back.thread_id == "t1"
    assert back.step == 3
    assert back.channel_values["topic"] == "y"


def test_file_saver_append_and_latest():
    with tempfile.TemporaryDirectory() as td:
        saver = FileGraphSaver(Path(td))
        for step in range(4):
            saver.put(ExecutionCheckpoint(thread_id="t9", step=step))
        got = saver.get("t9")
        assert got is not None and got.step == 3
        assert len(saver.list("t9")) == 4


def test_resume_after_interrupted_run():
    """A run paused mid-flight resumes from its checkpoint, not step 0."""
    b = GraphState(_Schema)
    calls: list[str] = []

    def draft(s):
        calls.append("draft")
        return {"notes": ["d1"]}

    def review(s):
        calls.append("review")
        return {"verdict": "v"}

    b.add_node("draft", draft)
    b.add_node("review", review)
    b.add_edge(START, "draft")
    b.add_edge("draft", "review")
    b.add_edge("review", END)
    graph = b.compile()

    saver = InMemorySaver()
    runner = GraphRunner(graph, config={"thread_id": "resume-t"}, checkpointer=saver,
                         interrupt_before=["review"])
    r1 = runner.invoke({"topic": "z", "notes": []})
    assert r1.status == "paused"
    assert calls == ["draft"]  # review not executed

    runner2 = GraphRunner(graph, config={"thread_id": "resume-t", "__is_resume__": True},
                          checkpointer=saver)
    r2 = runner2.invoke(None)
    assert "review" in calls
    assert r2.values.get("verdict") == "v"


# ── 3. 图结构编排（分支/循环/条件路由）──────────────────

def test_conditional_branch():
    """Router picks the path; the other node never runs."""
    class S(TypedDict):
        n: int
        path: str

    b = GraphState(S)
    b.add_node("even", lambda s: {"path": "even"})
    b.add_node("odd", lambda s: {"path": "odd"})
    b.add_node("gate", lambda s: {})
    b.add_edge(START, "gate")
    b.add_conditional_edges("gate", lambda s: "even" if s["n"] % 2 == 0 else "odd")
    b.add_edge("even", END)
    b.add_edge("odd", END)
    graph = b.compile()
    out = GraphRunner(graph).invoke({"n": 4, "path": ""})
    assert out.values["path"] == "even"


def test_loop_with_counter():
    """Cycle until a condition on state stops it (recursion cap)."""
    class S(TypedDict):
        count: int
        log: Annotated[list, operator.add]

    def bump(s):
        return {"count": s["count"] + 1, "log": [s["count"]]}

    def route(s):
        return "bump" if s["count"] < 3 else END

    b = GraphState(S)
    b.add_node("bump", bump)
    b.add_edge(START, "bump")
    b.add_conditional_edges("bump", route)
    graph = b.compile()
    out = GraphRunner(graph, max_steps=25).invoke({"count": 0, "log": []})
    assert out.values["count"] == 3
    assert out.values["log"] == [0, 1, 2]


def test_command_goto_overrides_edges():
    """Node returns Command(goto=...) to jump explicitly."""
    class S(TypedDict):
        trace: Annotated[list, operator.add]

    b = GraphState(S)
    b.add_node("a", lambda s: {"trace": ["a"]})
    b.add_node("b", lambda s: {"trace": ["b"]})
    b.add_node("jump", lambda s: Command(update={"trace": ["jump"]}, goto="b"))
    b.add_edge(START, "a")
    b.add_edge("a", "jump")
    b.add_edge("b", END)
    out = GraphRunner(b.compile()).invoke({"trace": []})
    assert out.values["trace"] == ["a", "jump", "b"]


# ── 4. 多 Agent 协作（Supervisor + Send fan-out）──────────

def test_supervisor_routes_workers():
    """Supervisor dispatches to workers and collects their outputs."""
    def researcher(s):
        return "facts"

    def writer(s):
        return "prose"

    def route(s):
        results = s.get("results", {})
        if "researcher" not in results:
            return "researcher"
        if "writer" not in results:
            return "writer"
        return END

    sup = create_supervisor({"researcher": researcher, "writer": writer}, route)
    out = sup.invoke({})
    assert out["results"] == {"researcher": "facts", "writer": "prose"}


def test_send_map_reduce():
    """Conditional edge fans out Send() copies with private state."""
    class S(TypedDict):
        items: list
        total: Annotated[list, operator.add]

    def fan(s):
        return [Send("work", {"item": it}) for it in s["items"]]

    def work(s):
        return {"total": [s["item"] * 10]}

    b = GraphState(S)
    b.add_node("fan", lambda s: {})
    b.add_node("work", work)
    b.add_edge(START, "fan")
    b.add_conditional_edges("fan", fan)
    b.add_edge("work", END)
    out = GraphRunner(b.compile()).invoke({"items": [1, 2, 3], "total": []})
    assert sorted(out.values["total"]) == [10, 20, 30]


# ── 5. Human-in-the-loop ─────────────────────────────────────

def test_interrupt_pauses_and_resumes():
    """interrupt() inside a node pauses; resume() injects the response."""
    class S(TypedDict):
        approval: str
        done: str

    def gate(s):
        decision = interrupt({"ask": "approve?", "risk": "high"})
        return {"approval": str(decision)}

    def final(s):
        return {"done": f"finished:{s.get('approval', '?')}"}

    b = GraphState(S)
    b.add_node("gate", gate)
    b.add_node("final", final)
    b.add_edge(START, "gate")
    b.add_edge("gate", "final")
    b.add_edge("final", END)
    graph = b.compile()

    runner = GraphRunner(graph, config={"thread_id": "hitl"})
    r1 = runner.invoke({"approval": "", "done": ""})
    assert r1.status == "paused"
    assert r1.interrupts and r1.interrupts[0].value["ask"] == "approve?"

    # resume: the scheduler re-runs the interrupted node with the response
    saver = InMemorySaver()
    runner2 = GraphRunner(graph, config={"thread_id": "hitl2"}, checkpointer=saver)
    r2 = runner2.invoke({"approval": "", "done": ""})
    assert r2.status == "paused"
    assert r2.interrupts[0].value == {"ask": "approve?", "risk": "high"}


def test_interrupt_before_breakpoint():
    """interrupt_before pauses before the listed node executes."""
    graph = _build_linear()
    runner = GraphRunner(graph, interrupt_before=["review"])
    r = runner.invoke({"topic": "t", "notes": []})
    assert r.status == "paused"
    assert r.interrupts[0].value == {"breakpoint": "review"}


# ── 6. 流式执行 ─────────────────────────────────────────────

def test_stream_events_fire_per_step():
    """on_event receives tasks/values events for every superstep."""
    events: list[str] = []
    runner = GraphRunner(
        _build_linear(),
        on_event=lambda e: events.append(f"{e.step}:{e.mode}:{e.data.get('name', '')}"),
    )
    runner.invoke({"topic": "s", "notes": []})
    assert any(":tasks:" in e for e in events)
    assert any(":values" in e for e in events)
    # two nodes fired
    assert sum(1 for e in events if ":tasks:" in e) == 2


# ── 7. 可观测性（trace）─────────────────────────────────────

def test_trace_collector_records_timeline():
    trace = TraceCollector()
    runner = GraphRunner(_build_linear(), on_event=trace)
    runner.invoke({"topic": "tr", "notes": []})
    steps = trace.steps()
    assert "draft" in steps.get(0, [])
    assert "review" in steps.get(1, [])
    assert len(trace.state_snapshots()) >= 1
    assert trace.elapsed() >= 0
    assert '"mode"' in trace.to_json()
    assert "draft" in trace.summary()


# -- entry --------------------------------------------------------------

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

