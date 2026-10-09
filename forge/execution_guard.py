"""Bounded loop diagnostics and projections of the existing session journal.

Inspired by OpenHands' action/observation cycle detector and LangGraph's
explicit state/checkpoint boundaries. This is not an exactly-once scheduler:
an interrupted action is of unknown outcome and must never be auto-replayed.
"""
from collections import deque
import hashlib
import json


class ExecutionGuard:
    def __init__(self):
        self.recent = deque(maxlen=12)

    def observe(self, name, args, ok, result):
        # Hash observations instead of retaining another copy of tool output.
        payload = json.dumps([name, args, bool(ok), result], sort_keys=True,
                             ensure_ascii=False, default=str)
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        self.recent.append((digest, bool(ok)))
        history = list(self.recent)
        for period in (1, 2):
            repeats = 5 if all(row[1] for row in history[-period:]) else 3
            count = period * repeats
            if len(history) >= count and all(history[-count + i] == history[-period + i % period]
                                             for i in range(count)):
                return True
        return False


def workflow_status(events, run_id, *, assume_interrupted=True):
    """Read-only recovery evidence; never load/replay callables from a journal."""
    phase = 'unknown'
    pending = {}
    for event in events:
        if hasattr(event, 'data'):
            event = {'type': event.type, **event.data}
        if event.get('run_id') != run_id:
            continue
        if event.get('type') == 'workflow_state':
            phase = event.get('phase', phase)
        elif event.get('type') == 'tool_started':
            pending[event['action_id']] = event.get('tool', '')
        elif event.get('type') == 'tool_completed':
            pending.pop(event['action_id'], None)
    status = phase if phase in {'completed', 'failed', 'stopped'} else (
        'interrupted' if assume_interrupted else 'unfinished')
    return {'run_id': run_id, 'status': status, 'last_phase': phase,
            'pending_actions': list(pending), 'replay_safe': False}
