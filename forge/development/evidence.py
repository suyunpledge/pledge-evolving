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
        self.phase = 'not_started'

    def apply(self, raw):
        event = redact(deepcopy(raw))
        kind = event.get('type')
        agent = event.get('agent', 'forge')
        run = event.get('run_id', '')
        if kind == 'workflow_state':
            row = {'agent': agent, 'run_id': run, 'status': event['phase'], 'depth': event.get('depth', 0)}
            self.agents[agent] = row
            self.runs[(agent, run)] = row
            if not row['depth']:
                self.phase = event['phase']
        elif kind == 'usage':
            row = self.runs.get((agent, run))
            if row is not None:
                row.update(model=event.get('model', ''), tokens=event.get('total_tokens'))
        elif kind == 'task_plan' and not event.get('depth', 0):
            self.plan = list(event.get('plan', {}).get('tasks', []))
        elif kind == 'tool_started':
            self.actions[event['action_id']] = {**event, 'status': 'running'}
        elif kind == 'tool_completed':
            row = self.actions.setdefault(event['action_id'], dict(event))
            row.update(event)
            row['status'] = 'completed' if event.get('ok') else 'failed'
        elif kind == 'tool_call':
            row = next((r for r in reversed(list(self.actions.values()))
                        if r.get('tool') == event.get('tool') and r.get('agent', 'forge') == agent
                        and 'result' not in r), None)
            if row is not None:
                row.update(event)
            args = event.get('args') or {}
            blocks = args.get('patches') or args.get('edits') or []
            paths = [args.get('path'), *[p.get('path') for p in blocks if isinstance(p, dict)]]
            if event.get('ok') and event.get('tool') in {'write_file', 'edit_file', 'apply_patch', 'edit_config', 'delete_file'}:
                for path in paths:
                    if isinstance(path, str):
                        self.changed[path] = event
            if event.get('tool') in {'shell_exec', 'run_tests'}:
                self.tests.append({'agent': agent, 'run_id': run,
                    'command': args.get('command', ''), 'passed': None, 'failed': None,
                    'status': 'blocked' if event.get('authorization') == 'deny' else
                              'completed' if event.get('ok') else 'failed',
                    'output': event.get('result', ''), 'tool': event['tool']})
        elif kind == 'checkpoint':
            self.checkpoints.append(event)

    @classmethod
    def from_events(cls, events, *, recovered=False):
        view = cls()
        for event in events:
            view.apply(event)
        if recovered:
            # Historical intent is not a running worker. Retain raw events in
            # Session while projecting an honest post-crash reconciliation state.
            for row in view.actions.values():
                if row['status'] == 'running':
                    row['status'] = 'unknown'
            for row in view.agents.values():
                if row['status'] in {'ready', 'planning', 'executing'}:
                    row['status'] = 'interrupted'
            if view.phase in {'ready', 'planning', 'executing'}:
                view.phase = 'interrupted'
        return view
