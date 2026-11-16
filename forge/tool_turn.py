"""Protocol-independent tool turns over the host's recorded action boundary.

Like Loom's kernel/environment boundary, this layer consumes normalized calls.
It neither parses provider messages nor grants permissions or retries actions.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from .runtime_types import Step
if TYPE_CHECKING:
    from .toolwire import ToolCall


@dataclass
class TurnResult:
    answered: list[tuple[ToolCall, str, bool]] = field(default_factory=list)
    supervisor_rows: list[dict[str, Any]] = field(default_factory=list)
    stopped: str = ''
    detection: dict[str, Any] = field(default_factory=dict)


def execute_turn(calls, *, native: bool, wire: str, steps: list[Step],
                 usage_delta: dict, max_result: int, invoke: Callable,
                 emit: Callable, offloader, guard, supervisor_row: Callable) -> TurnResult:
    """Execute sequentially; stop before the next call on unknown/stalled output."""
    turn = TurnResult()
    for call in calls:
        step = Step(index=len(steps) + 1, tool=call.name, args=call.args,
                    note=f"native/{wire} id={call.id or '-'}" if native else '')
        result = (invoke(call.name, call.args, tool_call_id=call.id or '') if native
                  else invoke(call.name, call.args))
        step.action_id, step.meta = result.meta['action_id'], dict(result.meta)
        if native:
            step.meta['native_call'] = True
        step.meta['usage_delta'] = dict(usage_delta)
        if result.meta.get('checkpoint'):
            step.note = 'checkpoint ' + result.meta['checkpoint'][:8]
        full_text = result.content or result.error
        if ('artifact' not in result.meta and 'artifact_retention' not in result.meta
                and offloader.policy.should_offload(call.name, full_text)):
            full_text = offloader.wrap_result_text(call.name, step.action_id, full_text)
            emit(type='tool_result_offloaded', tool=call.name,
                 path=offloader.last_path, chars=len(result.content or result.error))
        step.result = full_text[:max_result]
        if result.meta.get('artifact_retention', {}).get('status') == 'failed':
            warning = '[Output retention failed; the following preview may be incomplete.]\n'
            step.result = (warning + full_text)[:max_result]
        if len(full_text) > max_result:
            step.meta['result_full'] = full_text
        step.decision = ('deny' if not result.ok and result.meta.get('authorization') == 'deny'
                         else 'ok' if result.ok else 'error')
        steps.append(step)
        turn.answered.append((call, step.result, result.ok))
        turn.supervisor_rows.append(supervisor_row(call.name, call.args, result))
        protocol = {'call_id': call.id, 'wire': wire} if native else {'native': False}
        emit(type='tool_call', tool=call.name, args=call.args, action_id=step.action_id,
             meta=result.meta, result=step.result[:1500], ok=result.ok,
             decision=step.decision, step=step.index,
             authorization=result.meta.get('authorization'), **protocol)
        if result.meta.get('execution_state') == 'unknown':
            turn.stopped = 'unknown_outcome'
            break
        if guard.observe(call.name, call.args, result.ok, result.content or result.error):
            turn.stopped = 'stalled'
            turn.detection = dict(getattr(guard, 'detection', None) or {})
            break
    return turn
