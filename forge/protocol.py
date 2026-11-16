"""Shared wire facts for Forge's CLI, editor bridge, and HTTP clients.

The execution kernel remains the only implementation of a run.  This module
is intentionally a dependency-free envelope layer: clients consume the same
version and result fields without growing independent run semantics.
"""

from __future__ import annotations

from typing import Any
from .cache_metrics import _count, cache_coverage


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024


def validate_protocol_version(value: Any) -> int:
    if value != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported Forge protocol version: {value!r}")
    return PROTOCOL_VERSION


def _cache_trajectory(usage: dict[str, Any]) -> dict[str, Any]:
    """逐请求缓存命中轨迹（2026-11-04 第三轮：把 AutoClaw 台账的命中纪律投影进 run 报告）。

    断点（bust）判据：命中率 <40% 且比上一请求低 >30pt——实测由压缩重写前缀或
    中途改写历史触发；冷启动（cold_start）= 首请求命中 <40%。数据源是上游
    usage 归一层，不猜。
    """
    reqs = usage.get("requests") or []
    rows: list[float] = []
    observed_indices: list[int] = []
    busts: list[int] = []
    prev: float | None = None
    for i, req in enumerate(reqs):
        if not isinstance(req, dict) or not isinstance(req.get("usage"), dict):
            prev = None
            continue
        u = req["usage"]
        prompt = _count(u.get("prompt_tokens"))
        cached = _count(u.get("cached_tokens"))
        if u.get('cache_observed') is False or prompt is None or prompt <= 0 or cached is None or cached > prompt:
            prev = None  # 未知观测不能伪造冷启动或跨缺口推断断点。
            continue
        ratio = min(cached, prompt) / prompt
        if prev is not None and ratio < 0.4 and (prev - ratio) > 0.3:
            busts.append(i)
        rows.append(round(ratio, 4))
        observed_indices.append(i)
        prev = ratio
    coverage = cache_coverage(reqs)
    if not rows:
        return {"coverage": coverage}
    return {"requests": len(rows), "hit_rate_by_request": rows,
            "avg_hit_rate": round(sum(rows) / len(rows), 4),
            "busts": busts, "cold_start": rows[0] < 0.4 if observed_indices[0] == 0 else None,
            "observed_request_indices": observed_indices,
            "coverage": coverage}


def acceptance_payload(report: Any) -> dict[str, Any] | None:
    for event in reversed(getattr(report, 'events', ())):
        if event.get('type') == 'acceptance_checked':
            return {key: event[key] for key in ('attempt', 'passed', 'unknown_outcome', 'results')}
    return None


def run_payload(report: Any, *, session_path: str = "", include_steps: bool = False) -> dict[str, Any]:
    """Project a ``RunReport`` into the stable client-facing result envelope."""
    result = report.as_result()
    payload: dict[str, Any] = {
        "ok": result.outcome == "succeeded",
        "text": str(report.text or ""),
        "stopped": str(report.stopped or "final"),
        "usage": {**dict(report.usage or {}),
                  **({"cache_trajectory": _cache_trajectory(report.usage or {})}
                     if (report.usage or {}).get("requests") else {})},
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
    acceptance = acceptance_payload(report)
    if acceptance is not None:
        payload['acceptance'] = acceptance
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


__all__ = ["MAX_FRAME_BYTES", "PROTOCOL_VERSION", "acceptance_payload", "run_payload", "validate_protocol_version"]
