"""Shared result protocol: one shape for "how did it go".

Four modules each grew their own {ok, ...} structure (ToolResult,
WorkerResult, SmokeOutcome, Contribution.to_raw) with their own serialisation
quirks. Consumers that wanted to log "results" had to know all four dialects.
This module is the convergence point:

* ``Outcome`` — the one result envelope (ok / payload / problem / meta)
* ``as_outcome`` — coerce any of the legacy shapes into it
* ``OutcomeClock`` — injectable time source, replacing scattered wall-clock
  reads that made offline testing impossible for core modules even though the
  contribution contract demanded exactly that discipline from their authors

Nothing here changes behaviour by itself; the legacy structures now carry a
common protocol so the next consumer writes one formatter, not four.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class Outcome:
    ok: bool
    payload: Any = None
    problem: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_raw(self) -> dict[str, Any]:
        raw = {"ok": self.ok}
        if self.problem:
            raw["problem"] = self.problem
        if self.payload is not None:
            raw["payload"] = self.payload
        if self.meta:
            raw["meta"] = self.meta
        return raw

    def __bool__(self) -> bool:  # consistent with the legacy `if result:` habit
        return self.ok


class SupportsRaw(Protocol):
    def to_raw(self) -> dict[str, Any]: ...


_OK_KEYS = ("ok", "success")
_PROBLEM_KEYS = ("problem", "error", "failure")


def as_outcome(thing: Any) -> Outcome:
    """Coerce a legacy result into the common envelope.

    Understands: Outcome (pass-through), objects with .ok/.error/.content
    (ToolResult, WorkerResult), dicts with ok/success + error/problem keys,
    plain bool, and None. Never raises — a malformed result becomes an
    Outcome with a problem, not an exception in the logging path.
    """
    if isinstance(thing, Outcome):
        return thing
    if thing is None:
        return Outcome(ok=False, problem="none")
    if isinstance(thing, bool):
        return Outcome(ok=thing)
    if isinstance(thing, dict):
        ok = any(thing.get(k) for k in _OK_KEYS)
        problem = next((str(thing[k]) for k in _PROBLEM_KEYS if thing.get(k)), "")
        payload = {k: v for k, v in thing.items()
                   if k not in _OK_KEYS and k not in _PROBLEM_KEYS} or None
        return Outcome(ok=ok, payload=payload, problem=problem)
    ok = getattr(thing, "ok", None)
    if ok is None and hasattr(thing, "to_raw"):
        return as_outcome(thing.to_raw())
    error = getattr(thing, "error", "") or ""
    content = getattr(thing, "content", None)
    if content is None and not error and ok is None:
        return Outcome(ok=False, problem=f"unreadable result: {type(thing).__name__}")
    return Outcome(ok=bool(ok), payload=content, problem=str(error))


class OutcomeClock:
    """Injectable time source.

    Core modules read time.time() in 22 places while the contribution contract
    required their authors to take ``now`` as a parameter — the discipline was
    imposed outward but not kept inward. Callers pass an OutcomeClock; tests
    pass a frozen one; production passes the default.
    """

    def __init__(self, source: Callable[[], float] | None = None) -> None:
        self._source = source or _time.time

    def now(self) -> float:
        return self._source()

    @staticmethod
    def frozen(at: float = 1_000_000.0) -> "OutcomeClock":
        return OutcomeClock(source=lambda: at)


__all__ = ["Outcome", "OutcomeClock", "as_outcome"]
