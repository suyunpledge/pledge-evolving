"""Reference contribution — read this before writing your own.

This file is the shape every contributed module must take. It is small on
purpose: your module can be a scheduler, a replayer, a tool host, a model
catalog, anything — as long as this skeleton holds.

Rules (enforced mechanically by ``revon/registry.py``):

1. ``MODULE_API_VERSION`` must equal the value the registry expects.
2. Exactly two public functions: ``register(api)`` and ``selftest()``.
3. ``register`` returns a manifest with ``name`` (== file stem),
   ``version``, ``capabilities``; optionally ``hooks``.
4. ``selftest()`` returns ≥ 8 ``(name, ok, detail)`` tuples and passes offline.
5. Pure standard library. No network, no subprocess, no destructive file calls.
6. Never import another contribution — talk through ``api`` instead.
"""

from __future__ import annotations

import time
from typing import Any

MODULE_API_VERSION = 1


def register(api) -> dict[str, Any]:
    """Declare what this module offers. Called once, at load time."""

    def due(jobs: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Which periodic jobs are due at ``now``.

        Deliberately pure: no threads, no sleeping, no wall-clock reads. A
        scheduler you cannot test is a scheduler you cannot trust.
        """
        out: list[dict[str, Any]] = []
        for job in jobs or []:
            every = float(job.get("everySeconds") or 0)
            last = float(job.get("lastRunAt") or 0)
            if every <= 0:
                continue
            if last <= 0 or (now - last) >= every:
                out.append(dict(job))
        return out

    return {
        "name": "heartbeat",
        "version": "1.0.0",
        "capabilities": ["schedule.periodic", "schedule.due-check"],
        "hooks": {"on_tick": due},
        "author": "revon reference (written by the integrator as the template)",
        "summary": "Pure due-time evaluation for periodic jobs.",
    }


def selftest() -> list[tuple[str, bool, str]]:
    manifest = register(None)
    now = 1_000_000.0
    jobs = [
        {"id": "fresh", "everySeconds": 60, "lastRunAt": 0},
        {"id": "due", "everySeconds": 60, "lastRunAt": now - 61},
        {"id": "not-due", "everySeconds": 60, "lastRunAt": now - 10},
        {"id": "broken", "everySeconds": 0, "lastRunAt": now - 9999},
    ]
    fired = manifest["hooks"]["on_tick"](jobs, now)
    ids = [job["id"] for job in fired]
    return [
        ("manifest-name", manifest["name"] == "heartbeat", ""),
        ("manifest-has-capabilities", bool(manifest["capabilities"]), ""),
        ("never-run-is-due", "fresh" in ids, str(ids)),
        ("past-interval-is-due", "due" in ids, str(ids)),
        ("within-interval-is-not-due", "not-due" not in ids, str(ids)),
        ("zero-interval-job-skipped", "broken" not in ids, str(ids)),
        ("result-is-a-copy", fired and fired[0] is not jobs[0], ""),
        ("empty-input-is-safe", manifest["hooks"]["on_tick"]([], now) == [], ""),
        ("none-input-is-safe", manifest["hooks"]["on_tick"](None, now) == [], ""),
        ("deterministic", [j["id"] for j in manifest["hooks"]["on_tick"](jobs, now)] == ids, ""),
    ]


__all__ = ["MODULE_API_VERSION", "register", "selftest"]
