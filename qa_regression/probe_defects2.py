"""Corrected + extended verification: session concurrency and shared budget.

Replaces the buggy D4 checks from probe_defects.py. The first version used
``event.get('ordinal')`` on a dataclass and forgot ``Session.open()``, so its
failures were harness bugs, not product defects. This version uses the real
API: ``Session(path).open()`` and ``Event.ordinal``.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from _bootstrap import check, summary  # noqa: E402

from forge.session import Session  # noqa: E402
from forge.subagent import BudgetTracker  # noqa: E402


def tmp(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"qa-{label}-"))


# --- D4 (corrected): two Session writers on the same file -----------------
def d4_concurrent_session_ordinals():
    base = tmp("sess")
    path = base / "s.jsonl"
    s1 = Session(path).open()
    s2 = Session(path).open()  # second writer, same file
    s1.append("user_message", content="a")
    s2.append("user_message", content="b")
    ords = [e.ordinal for e in s1.events] + [e.ordinal for e in s2.events]
    dup = len(ords) != len(set(ords))
    on_disk = len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])
    return not dup, f"in-memory ordinals={ords}; lines on disk={on_disk}"


def d4_control_session_roundtrip():
    base = tmp("sessc")
    path = base / "s.jsonl"
    with Session(path) as s:
        s.append("user_message", content="hello")
    back = Session(path).open()
    return len(back.events) == 1, f"events={len(back.events)}"


def d4_control_ordinals_monotonic_single_writer():
    base = tmp("sesso")
    path = base / "s.jsonl"
    with Session(path) as s:
        for i in range(5):
            s.append("user_message", content=f"m{i}")
    back = Session(path).open()
    ords = [e.ordinal for e in back.events]
    return ords == list(range(1, 6)), f"ordinals={ords}"


# --- D5: shared mutable budget is not atomic ------------------------------
def d5_budget_decrement_race():
    """BudgetTracker shares one list across a spawn tree; is consume atomic?"""
    remaining = [200]
    tracker = BudgetTracker(remaining)
    barrier = threading.Barrier(8)

    def burn():
        barrier.wait()
        for _ in range(50):
            tracker.consume(1)

    threads = [threading.Thread(target=burn) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8 threads * 50 = 400 decrements against 200 budget, clamped at 0.
    exact = tracker.remaining == 0
    return exact, f"expected 0 (clamped), got {tracker.remaining}"


def d5_control_budget_single_thread():
    t = BudgetTracker([10])
    for _ in range(4):
        t.consume(1)
    return t.remaining == 6, f"remaining={t.remaining}"


def d5_raw_list_pattern_is_racy():
    """The real budget in loop.py is a bare list decremented as budget[0] -= 1.

    Demonstrate the read-modify-write window the same way loop.py does it.
    """
    budget = [100000]

    def burn():
        for _ in range(20000):
            budget[0] -= 1

    threads = [threading.Thread(target=burn) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = 100000 - 4 * 20000
    # Under CPython's GIL with the current bytecode this often still lands
    # exactly; report the observation rather than asserting a lost update.
    return budget[0] == expected, f"expected {expected}, got {budget[0]}"


def main() -> int:
    check("D4 concurrent Session writers keep unique ordinals",
          d4_concurrent_session_ordinals)
    check("D4-control session round-trips", d4_control_session_roundtrip)
    check("D4-control single-writer ordinals are monotonic",
          d4_control_ordinals_monotonic_single_writer)
    check("D5 shared budget survives concurrent consume", d5_budget_decrement_race)
    check("D5-control budget decrements correctly single-threaded",
          d5_control_budget_single_thread)
    check("D5 raw list decrement pattern (observed)", d5_raw_list_pattern_is_racy)
    return summary("session + budget verification (main @ 119b827, pristine)")


if __name__ == "__main__":
    raise SystemExit(main())
