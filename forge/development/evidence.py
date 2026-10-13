"""Read-only projection of real runtime events; plans are never inferred completed."""
from copy import deepcopy
from forge.secrets import redact


class TaskEvidence:
    def __init__(self):
        self.plan = []
        self.actions = {}
        self.agents = {}
        self.runs = {}
        self.tests = []
        self.changed = {}
        self.checkpoints = []
        self.diffs = []
        self.reviews = {}
        self.phase = 'not_started'
        self.task_id = ''
        self.changes = {}
        self.configurations = {}
        self._seen = set()

    def apply(self, raw):
        if raw.get('type') == 'session_meta':
            return  # Trusted rollout metadata is not task execution evidence.
        event = redact(deepcopy(raw))
        identifier = event.get('event_id')
        if identifier and identifier in self._seen:
            return
        kind = event.get('type')
        task_id = event.get('task_id', '')
        if self.task_id and task_id and task_id != self.task_id:
            return
        if task_id:
            self.task_id = task_id
        if identifier:
            self._seen.add(identifier)
        agent = event.get('agent', 'forge')
        run = event.get('run_id', '')
        if kind in {'development_task_started', 'development_task_finished', 'development_recovery'}:
            self.phase = event['phase']
        elif kind == 'workflow_state':
            row = self.runs.setdefault((agent, run), {})
            row.update(agent=agent, run_id=run, status=event['phase'], depth=event.get('depth', 0),
                       parent_run_id=event.get('parent_run_id', ''), task_id=task_id)
            row.update(self.configurations.get(agent, {}))
            if event.get('reason') == 'cancelled':
                row['status'] = 'cancelled'
            self.agents[agent] = row
            self.runs[(agent, run)] = row
            if not row['depth']:
                self.phase = row['status']
        elif kind == 'usage':
            row = self.runs.get((agent, run))
            if row is not None:
                row.update(model=event.get('model', ''), tokens=event.get('total_tokens'))
        elif kind == 'development_agent_config':
            self.configurations[agent] = event
            for (name, _), row in self.runs.items():
                if name == agent:
                    row.update(event)
        elif kind == 'user_message':
            if run:
                row = self.runs.setdefault((agent, run), {'agent': agent, 'run_id': run,
                    'parent_run_id': event.get('parent_run_id', ''), 'depth': event.get('depth', 0),
                    'task_id': task_id, 'status': 'requested'})
                row['task'] = event.get('content', '')
        elif kind == 'assistant_message':
            row = self.runs.get((agent, run))
            if row is not None:
                row['output'] = event.get('content', '')
        elif kind == 'task_plan' and not event.get('depth', 0):
            self.plan = list(event.get('plan', {}).get('tasks', []))
        elif kind == 'tool_started':
            self.actions[event['action_id']] = {**event, 'status':
                'not_started' if event.get('execution_state') == 'not_started' else 'running'}
        elif kind == 'tool_completed':
            row = self.actions.setdefault(event['action_id'], dict(event))
            row.update(event)
            row['status'] = ('unknown' if event.get('execution_state') == 'unknown' else
                'awaiting_approval' if (event.get('meta') or {}).get('requires_approval') else
                'cancelled' if event.get('cancelled') or event.get('error_code') == 'cancelled' else
                'completed' if event.get('ok') else 'failed')
            for change in (event.get('meta') or {}).get('file_changes', []):
                key = (event['action_id'], change['path'])
                self.changes[key] = {**change, 'action_id': event['action_id'], 'task_id': task_id,
                    'run_id': run, 'checkpoint': (event.get('meta') or {}).get('checkpoint', '')}
            if row['status'] == 'awaiting_approval' and event.get('tool') in {
                    'write_file', 'edit_file', 'apply_patch', 'delete_file', 'edit_config'}:
                args = event.get('args') or {}
                if args.get('path') and (event.get('tool') != 'edit_config' or args.get('patch') is not None):
                    self.changes[(event['action_id'], args['path'])] = {'path': args['path'],
                        'state': 'pending', 'action_id': event['action_id'], 'task_id': task_id,
                        'run_id': run, 'diff': None, 'added': None, 'removed': None}
        elif kind == 'tool_call':
            row = self.actions.get(event.get('action_id'))
            if row is None and not event.get('action_id'):
                row = next((r for r in reversed(list(self.actions.values()))
                        if r.get('tool') == event.get('tool') and r.get('agent', 'forge') == agent
                        and r.get('run_id', '') == run and not r.get('detail_received')), None)
            if row is not None:
                row.update(event)
                row['detail_received'] = True
            args = event.get('args') or {}
            blocks = args.get('patches') or args.get('edits') or []
            paths = [args.get('path'), *[p.get('path') for p in blocks if isinstance(p, dict)]]
            if event.get('ok') and event.get('tool') in {'write_file', 'edit_file', 'apply_patch', 'edit_config', 'delete_file'}:
                for path in paths:
                    if isinstance(path, str):
                        self.changed[path] = event
            if event.get('tool') in {'shell_exec', 'run_tests'}:
                self.tests.append({'agent': agent, 'run_id': run,
                    'task_id': task_id, 'action_id': event.get('action_id'),
                    'command': args.get('command', ''), 'passed': None, 'failed': None,
                    'status': 'blocked' if event.get('authorization') == 'deny' else
                              'completed' if event.get('ok') else 'failed',
                    'output': event.get('result', ''), 'tool': event['tool']})
        elif kind in {'test_started', 'test_result'}:
            row = next((r for r in self.tests if r.get('run_id') == run and r.get('agent') == agent), None)
            if row is None:
                row = {'passed': None, 'failed': None, 'status': 'running', 'command': ''}
                self.tests.append(row)
            row.update(event)
            self.phase = 'testing' if kind == 'test_started' else event['status']
        elif kind in {'review_started', 'review_result', 'review_error', 'review_cancelled'}:
            key = (event.get('message_id'), event.get('review_id', ''))
            row = self.reviews.setdefault(key, {})
            row.update(event)
            row['status'] = {'review_started': 'running', 'review_result': 'completed',
                             'review_error': 'failed', 'review_cancelled': 'cancelled'}[kind]
            name = 'Review Agent'
            config = self.configurations.get(name, {})
            self.runs[(name, key[1])] = {**config, 'agent': name, 'run_id': key[1],
                'parent_run_id': event.get('run_id', ''), 'task_id': task_id, 'status': row['status'],
                'depth': 1, 'output': event.get('review') or event.get('error', ''),
                'model': (event.get('review') or {}).get('model', config.get('selected_model', ''))}
        elif kind == 'development_change_state':
            key = (event.get('action_id'), event.get('path'))
            if key in self.changes:
                self.changes[key].update(event)
        elif kind == 'checkpoint':
            self.checkpoints.append(event)
        elif kind == 'git_diff':
            self.diffs.append(event)

    @classmethod
    def from_events(cls, events, *, recovered=False):
        view = cls()
        view.task_id = next((e.get('task_id') for e in reversed(events) if e.get('task_id')), '')
        for event in events:
            view.apply(event)
        if recovered:
            # Historical intent is not a running worker. Retain raw events in
            # Session while projecting an honest post-crash reconciliation state.
            for row in view.actions.values():
                if row['status'] == 'running':
                    row['status'] = 'unknown'
            for row in view.runs.values():
                if row['status'] in {'ready', 'planning', 'executing'}:
                    row['status'] = 'interrupted'
            for row in view.tests:
                if row['status'] == 'running':
                    row['status'] = 'unknown'
            for row in view.reviews.values():
                if row['status'] == 'running':
                    row['status'] = 'interrupted'
            if view.phase in {'ready', 'planning', 'executing', 'testing'}:
                view.phase = 'interrupted'
        return view
