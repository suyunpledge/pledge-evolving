"""Plan model (port of ScienceClaw plan_types.py).

PlanStep gives the frontend a stable plan shape regardless of which
planner produced it. Ported verbatim in spirit; dict-based so it
round-trips through forge's JSON event bus unchanged.
"""
from __future__ import annotations

import time
from typing import Any, TypedDict

try:  # py3.11+ has NotRequired natively; older needs typing_extensions
    from typing import NotRequired
except ImportError:  # pragma: no cover
    from typing_extensions import NotRequired


class PlanStep(TypedDict):
    id: str
    content: str
    status: str          # pending | in_progress | completed | failed
    tools: list[str]
    files: list[str]
    priority: str        # low | medium | high
    inputs: NotRequired[dict]
    outputs: NotRequired[dict]
    created_at: int


def normalize_plan_steps(plan: list[dict[str, Any]]) -> list[PlanStep]:
    """Normalise planner output to the stable frontend shape.

    Same contract as upstream: missing ids get positional ids, missing
    statuses default to pending, missing priorities to medium.
    """
    now = int(time.time())
    normalized: list[PlanStep] = []
    for index, step in enumerate(plan):
        normalized.append({
            "id": step.get("id") or f"step-{index + 1}",
            "content": step.get("content", ""),
            "status": step.get("status") or "pending",
            "priority": step.get("priority") or "medium",
            "tools": step.get("tools") or [],
            "files": step.get("files") or [],
            "created_at": step.get("created_at") or now,
            "inputs": step.get("inputs") or {},
            "outputs": step.get("outputs") or {},
        })
    return normalized


__all__ = ["PlanStep", "normalize_plan_steps"]
