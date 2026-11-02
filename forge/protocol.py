"""Shared wire facts for Forge's CLI, editor bridge, and HTTP clients.

The execution kernel remains the only implementation of a run.  This module
is intentionally a dependency-free envelope layer: clients consume the same
version and result fields without growing independent run semantics.
"""

from __future__ import annotations

from typing import Any


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024


def validate_protocol_version(value: Any) -> int:
    if value != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported Forge protocol version: {value!r}")
    return PROTOCOL_VERSION


def run_payload(report: Any, *, session_path: str = "", include_steps: bool = False) -> dict[str, Any]:
    """Project a ``RunReport`` into the stable client-facing result envelope."""
    result = report.as_result()
    payload: dict[str, Any] = {
        "ok": result.outcome == "succeeded",
        "text": str(report.text or ""),
        "stopped": str(report.stopped or "final"),
        "usage": dict(report.usage or {}),
        "toolCalls": int(report.tool_calls),
        "planning": report.planning,
        "messageId": report.run_id,
        "run_id": report.run_id,
        "task_id": report.task_id,
        "parent_run_id": report.parent_run_id,
        "protocol_version": PROTOCOL_VERSION,
        "outcome": result.outcome,
        "error_code": result.error_code,
        "execution_state": result.execution_state,
    }
    if session_path:
        payload["sessionPath"] = str(session_path)
    if include_steps:
        payload["steps"] = [{
            "index": step.index,
            "tool": step.tool,
            "decision": step.decision,
            "note": step.note,
            "result": str(step.result or "")[:400],
            "action_id": step.action_id,
            "meta": step.meta,
        } for step in report.steps]
    return payload


__all__ = ["MAX_FRAME_BYTES", "PROTOCOL_VERSION", "run_payload", "validate_protocol_version"]
