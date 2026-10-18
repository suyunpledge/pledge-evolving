"""Delivery-client projections, backed by Forge's existing append-only journal.

Execution evidence remains in the Agent session. These records persist the HTTP
view and its SSE cursor; they never restore permissions or replay an action.
One server owns live workers. Reloaded unfinished records are detached/unknown.
"""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time
import uuid

from .execution import RunRequest
from .secrets import redact
from .session import Session

TERMINAL_STATES = frozenset({'done', 'error', 'cancelled'})
RUN_ID_RE = re.compile(r'^(?:[0-9a-f]{12}|[0-9a-f]{32})$')
EVENT_LIMIT = 5000


class RunStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self._lock = threading.RLock()
        self._runs = {}
        self._sessions = {}
        self._tokens = {}
        self._claimed = set()
        self._seen = {}
        if self.directory is not None and self.directory.is_dir():
            for path in sorted(self.directory.glob('client-*.jsonl')):
                rid = path.stem.removeprefix('client-')
                if not RUN_ID_RE.fullmatch(rid):
                    continue
                with Session(path) as journal:
                    for event in journal.events:
                        if event.type == 'client_record':
                            self._apply(rid, event.data, event.ordinal, event.ts)
                rec = self._runs.get(rid)
                if rec and rec['state'] not in TERMINAL_STATES:
                    # This host has no worker/token for this execution. Do not
                    # infer that a remote handler exited, or manufacture success.
                    rec.update(state='error', outcome='unknown', execution_state='unknown',
                               error_code='execution_detached', requires_inspection=True,
                               recovered=True, error='Execution is not attached to this server; '
                               'inspect the session and Diff before starting a new task.')

    def _apply(self, rid, data, cursor, ts):
        if 'created' in data:
            self._runs[rid] = deepcopy(data['created'])
            self._seen[rid] = set()
        rec = self._runs[rid]
        rec.update(deepcopy(data.get('patch', {})))
        rec['cursor'] = cursor
        if 'event' in data:
            event = deepcopy(data['event'])
            event.update(cursor=cursor, ts=ts, client_run_id=rid)
            event.setdefault('run_id', rid)
            event.setdefault('task_id', rec['task_id'])
            event.setdefault('parent_run_id', '')
            event.setdefault('protocol_version', 1)
            rec['events'].append(event)
            self._seen[rid].add(event['event_id'])
            if event.get('type') == 'step':
                rec['steps'].append(deepcopy(event))
            if len(rec['events']) > EVENT_LIMIT:
                removed = rec['events'][:-EVENT_LIMIT]
                del rec['events'][:-EVENT_LIMIT]
                rec['events_dropped_through'] = removed[-1]['cursor']
                self._seen[rid].difference_update(e['event_id'] for e in removed)

    def _commit(self, rid, *, created=None, patch=None, event=None):
        data = {}
        if created is not None:
            data['created'] = created
        if patch is not None:
            data['patch'] = patch
        if event is not None:
            data['event'] = {**event, 'event_id': event.get('event_id') or uuid.uuid4().hex}
        data = redact(data)
        if self.directory is None:
            cursor = self._runs.get(rid, {}).get('cursor', 0) + 1
            stamp = time.time()
        else:
            journal = self._sessions.get(rid)
            if journal is None:
                journal = self._sessions[rid] = Session(self.directory / f'client-{rid}.jsonl')
            stored = journal.append('client_record', **data)
            journal.sync()  # Publish only after the projection is durable.
            cursor, stamp = stored.ordinal, stored.ts
        self._apply(rid, data, cursor, stamp)
        if self._runs[rid]['state'] in TERMINAL_STATES:
            journal = self._sessions.pop(rid, None)
            if journal is not None:
                journal.close()

    def create(self, task):
        request = RunRequest(entry='client')
        rid = request.run_id
        rec = {'id': rid, **request.as_dict(), 'task': task, 'state': 'queued',
               'created': time.time(), 'started': 0.0, 'finished': 0.0,
               'steps': [], 'text': '', 'delivery': [], 'usage': {}, 'error': '',
               'traceback': '', 'model': '', 'strategy': '', 'thinking': '',
               'bundle': '', 'policy_mode': '', 'cancelled': False, 'events': [],
               'outcome': 'running', 'execution_state': 'not_started', 'error_code': ''}
        with self._lock:
            self._commit(rid, created=rec, event={'type': 'state', 'state': 'queued'})
            self._tokens[rid] = threading.Event()
            return deepcopy(self._runs[rid])

    def get(self, rid):
        with self._lock:
            return deepcopy(self._runs.get(rid))

    def list_recent(self, n=50):
        with self._lock:
            return deepcopy(sorted(self._runs.values(), key=lambda r: r['created'], reverse=True)[:n])

    def update(self, rid, **values):
        with self._lock:
            self._commit(rid, patch=values)

    def transition(self, rid, **values):
        with self._lock:
            self._commit(rid, patch=values, event={'type': 'state', **values})

    def push_event(self, rid, event):
        with self._lock:
            if event.get('event_id') in self._seen[rid]:
                return
            self._commit(rid, event=event)

    def push_step(self, rid, step):
        with self._lock:
            if step.get('action_id') and any(s.get('action_id') == step['action_id']
                    and s.get('run_id') == step.get('run_id', rid) for s in self._runs[rid]['steps']):
                return
            self.push_event(rid, {**step, 'type': 'step'})

    def claim(self, rid):
        with self._lock:
            rec = self._runs[rid]
            if (rid in self._claimed or rec['state'] in TERMINAL_STATES or rec.get('recovered')
                    or rec['execution_state'] != 'not_started'):
                return None
            self._claimed.add(rid)
            return self._tokens[rid]

    def release(self, rid):
        with self._lock:
            self._claimed.discard(rid)
            if self._runs[rid]['state'] in TERMINAL_STATES:
                self._tokens.pop(rid, None)

    def request_cancel(self, rid):
        with self._lock:
            rec = self._runs.get(rid)
            if rec is None:
                raise KeyError(rid)
            if rec['state'] in TERMINAL_STATES:
                raise ValueError('run already ' + rec['state'])
            self.transition(rid, state='cancelling', cancel_requested=True)
            self._tokens[rid].set()

    def event_page(self, rid, after=0, limit=200):
        with self._lock:
            rec = self._runs[rid]
            events = rec['events']
            reset = after < rec.get('events_dropped_through', 0)
            if reset and self.directory is not None:
                # The bounded live projection is a cache, not an SSE index.
                # Fall back to durable records so reconnects cannot skip events.
                rows = []
                journal = Session(self.directory / f'client-{rid}.jsonl')
                for stored in journal.replay():
                    if stored.type != 'client_record' or stored.ordinal <= after or 'event' not in stored.data:
                        continue
                    event = deepcopy(stored.data['event'])
                    event.update(cursor=stored.ordinal, ts=stored.ts, client_run_id=rid)
                    event.setdefault('run_id', rid)
                    event.setdefault('task_id', rec['task_id'])
                    event.setdefault('parent_run_id', '')
                    event.setdefault('protocol_version', 1)
                    rows.append(event)
                    if len(rows) > limit:
                        break
                events, reset = rows, False
            rows = [e for e in events if e['cursor'] > after]
            page = rows[:limit]
            return {'events': deepcopy(page), 'next_after': page[-1]['cursor'] if page else after,
                    'has_more': len(rows) > limit, 'reset_required': reset,
                    'state': rec['state']}

    def cleanup(self, max_age=7200):
        # Durable history is retained. This only expires ephemeral test/client stores.
        if self.directory is not None:
            return
        with self._lock:
            cutoff = time.time() - max_age
            for rid in list(self._runs):
                if self._runs[rid]['created'] < cutoff and self._runs[rid]['state'] in TERMINAL_STATES:
                    del self._runs[rid]
                    self._tokens.pop(rid, None)
                    self._seen.pop(rid, None)

    def close(self):
        with self._lock:
            for journal in self._sessions.values():
                journal.close()
            self._sessions.clear()
