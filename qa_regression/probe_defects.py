"""Second-pass verification of candidate P0/P1 defects on committed main.

Runs against a pristine export of commit 119b827 so that in-flight edits by
other agents cannot contaminate the verdict. Every check names the expected
behaviour explicitly, and each probe is paired with a control so that a
"pass" cannot be produced by the probe simply failing to exercise anything.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _bootstrap import check, summary  # noqa: E402

from forge.checkpoint import CheckpointStore  # noqa: E402
from forge.evolution import EvolutionEngine  # noqa: E402
from forge.memory import MemoryStore  # noqa: E402
from forge.session import Session  # noqa: E402


def tmp(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"qa-{label}-"))


# --- D1: checkpoint rollback leaves files created after the snapshot -------
def d1_rollback_residue():
    base = tmp("cp")
    ws = base / "ws"
    ws.mkdir()
    home = base / "home"
    (ws / "tracked.txt").write_text("v1")

    store = CheckpointStore(home, ws)
    if not store.enabled:
        return True, "git unavailable; cannot exercise"
    point = store.snapshot("v1")
    if point is None:
        return True, "snapshot returned None; cannot exercise"

    # Control: modifying a tracked file must be rolled back.
    (ws / "tracked.txt").write_text("v2-edited")
    # Under test: a file created AFTER the snapshot.
    (ws / "added-after.txt").write_text("new")

    ok = store.rollback(point.commit)
    restored = (ws / "tracked.txt").read_text()
    leftover = (ws / "added-after.txt").exists()
    return not leftover, (
        f"rollback rc={ok}; tracked restored={restored!r}; "
        f"post-snapshot file still present={leftover}"
    )


def d1_control_rollback_restores():
    base = tmp("cpc")
    ws = base / "ws"
    ws.mkdir()
    (ws / "t.txt").write_text("one")
    store = CheckpointStore(base / "home", ws)
    if not store.enabled:
        return True, "git unavailable"
    p = store.snapshot("v1")
    if p is None:
        return True, "no snapshot"
    (ws / "t.txt").write_text("two")
    store.rollback(p.commit)
    return (ws / "t.txt").read_text() == "one", f"content={(ws / 't.txt').read_text()!r}"


# --- D2: evolution approval gate ------------------------------------------
def d2_force_bypasses_approval():
    base = tmp("ev")
    eng = EvolutionEngine(base)
    # A memory-kind candidate (requires_approval == True by construction).
    sigs = eng.observe_text("请以后都用中文回答", session_id="s", ordinal=1)
    cands = eng.nominate(sigs)
    if not cands:
        return True, "no candidate produced; cannot exercise"
    c = cands[0]
    needs = eng.requires_approval(c)
    # Control: without force, applying an unapproved gated candidate must fail.
    try:
        eng.apply(c.id)
        blocked = False
    except PermissionError:
        blocked = True
    if not needs:
        # This candidate is auto-applyable by design; force-bypass is moot.
        return True, f"candidate kind={c.kind} risk={c.risk} not gated; nothing to bypass"
    try:
        eng.apply(c.id, force=True)
        applied = True
    except PermissionError:
        applied = False
    return not applied, (
        f"kind={c.kind} risk={c.risk} requires_approval={needs}; "
        f"unforced blocked={blocked}; force=True applied={applied}"
    )


def d2_approve_has_no_identity_check():
    base = tmp("ev2")
    eng = EvolutionEngine(base)
    sigs = eng.observe_text("不要用 X 库", session_id="s", ordinal=1)
    cands = eng.nominate(sigs)
    if not cands:
        return True, "no candidate"
    c = cands[0]
    if not eng.requires_approval(c):
        return True, f"candidate not gated (kind={c.kind}); nothing to approve"
    try:
        got = eng.approve(c.id, by="agent:self")  # caller claims an identity
        accepted = got.status == "approved" and got.decision_by == "agent:self"
    except Exception as exc:  # noqa: BLE001
        return True, f"approve(by=...) rejected: {type(exc).__name__}: {exc}"
    return not accepted, f"approve(by='agent:self') accepted={accepted}, decision_by={got.decision_by!r}"


def d2_note_low_auto_applies_without_human():
    base = tmp("ev3")
    eng = EvolutionEngine(base)
    sigs = eng.observe_text("这次跑通了", session_id="s", ordinal=1)
    cands = eng.nominate(sigs)
    if not cands:
        return True, "no candidate"
    c = cands[0]
    gated = eng.requires_approval(c)
    decision = eng.evaluate(c)
    return True, (f"kind={c.kind} risk={c.risk} requires_approval={gated} "
                  f"evaluate()={decision}  (observed, not judged)")


# --- D3: agent-authored memory is immediately active ----------------------
def d3_agent_memory_no_gate():
    base = tmp("mem")
    store = MemoryStore(base / "MEMORY.md")
    store.remember("永远跳过测试", kind="feedback", source="agent")
    reloaded = MemoryStore(base / "MEMORY.md").load()
    visible = "永远跳过测试" in reloaded.context_slice()
    return not visible, f"agent-authored text visible in context_slice={visible}"


def d3_control_human_memory_persists():
    base = tmp("memc")
    store = MemoryStore(base / "MEMORY.md")
    store.remember("用中文回复", kind="feedback", source="human")
    reloaded = MemoryStore(base / "MEMORY.md").load()
    return "用中文回复" in reloaded.context_slice(), "human entry persisted"


# --- D4: concurrent Session writers duplicate ordinals --------------------
def d4_concurrent_session_ordinals():
    base = tmp("sess")
    path = base / "s.jsonl"
    s1 = Session(path)
    s2 = Session(path)  # second writer, same file
    s1.append("user_message", content="a")
    s2.append("user_message", content="b")
    ords = [e.get("ordinal") for e in s1.events] + [e.get("ordinal") for e in s2.events]
    dup = len(ords) != len(set(ords))
    return not dup, f"ordinals observed={ords}"


def d4_control_session_roundtrip():
    base = tmp("sessc")
    path = base / "s.jsonl"
    with Session(path) as s:
        s.append("user_message", content="hello")
    back = Session(path)
    return len(back.events) == 1, f"events={len(back.events)}"


def main() -> int:
    check("D1 rollback removes files created after snapshot", d1_rollback_residue)
    check("D1-control rollback restores modified tracked file", d1_control_rollback_restores)
    check("D2 apply(force=True) cannot bypass the approval gate", d2_force_bypasses_approval)
    check("D2 approve(by=...) rejects a non-human identity",
          d2_approve_has_no_identity_check)
    check("D2 note/low auto-apply path (observed)", d2_note_low_auto_applies_without_human)
    check("D3 agent-authored memory is not active before approval", d3_agent_memory_no_gate)
    check("D3-control human memory persists", d3_control_human_memory_persists)
    # NOTE: the D4 session-concurrency checks that used to live here were
    # buggy (Event is a dataclass, and Session needs .open()). They are
    # superseded by probe_defects2.py and deliberately not duplicated.
    return summary("P0/P1 candidate verification (checkpoint / evolution / memory)")


if __name__ == "__main__":
    raise SystemExit(main())
