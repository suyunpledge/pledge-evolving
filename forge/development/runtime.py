"""One lane over the existing EditorBridge/Agent; no scheduler or executor here."""
import json
import hashlib
from pathlib import Path
import threading

from forge.execution_guard import workflow_status
from forge.secrets import redact, install_logging_redaction
from forge.session import Session
from forge.vscode_bridge import EditorBridge
from .state import WorkMode

DEVELOPMENT_GUIDANCE = (
    'Development Mode: inspect the actual project and relevant code before editing. '
    'For nontrivial work plan dependencies and changes, make scoped edits, run relevant tests '
    'through authorized tools, inspect the actual diff, and summarize evidence and limitations. '
    'Simplify this workflow for small tasks. Report only real tool/test events; '
    'never claim tests ran when Policy denied execution. Do not overwrite unsaved editor buffers. '
    'Mode selection grants no permissions.'
)


class DevelopmentRuntime:
    def __init__(self, state, *, bridge=None, emit=None):
        install_logging_redaction()
        self.state = state
        self.emit = emit or (lambda event: None)
        self._owns_bridge = bridge is None
        self.bridge = bridge or EditorBridge(self._emit)
        self._previous_bridge_emit = getattr(bridge, 'emit', None) if bridge is not None else None
        if self._previous_bridge_emit is not None:
            self.bridge.emit = self._emit
        existing_agent = getattr(self.bridge, 'agent', None)
        self.ready = existing_agent is not None
        self.catalog = dict(getattr(self.bridge, 'models', {})) if self.ready else {}
        self._lane = getattr(self.bridge, '_development_lane', None) or threading.Lock()
        self.bridge._development_lane = self._lane
        self.last_snapshot = None
        self._base_extra_system = getattr(existing_agent, 'extra_system', '')
        # Host extension seams, not new knowledge/evolution engines.
        self.context_sources = []
        self.result_observers = []

    @property
    def busy(self):
        return self._lane.locked()

    def _emit(self, event):
        if self._previous_bridge_emit is not None:
            self._previous_bridge_emit(redact(event))
        self.emit(redact(event))

    def initialize(self, project):
        project = Path(project).resolve(strict=True)
        if self.ready:
            if self.busy:
                raise RuntimeError('A task is running')
            if project != self.bridge.workspace:
                raise ValueError('The host must switch a borrowed Agent project explicitly')
            return {'models': [{'id': i, 'label': ' / '.join(pair)} for i, pair in self.catalog.items()],
                    'workspace': str(project), 'sessionPath': str(self.bridge.agent.session.path), 'ready': True}
        home = self.state.home
        if home.is_relative_to((Path.home() / '.forge').resolve()) or home.is_relative_to(project):
            raise ValueError('Use an independent IDE home outside the project; never share General runtime stores')
        if not self._lane.acquire(False):
            raise RuntimeError('A task is running')
        try:
            result = self.bridge.initialize({'workspace': str(project), 'home': str(home)})
            self.catalog = {row['id']: self.bridge.models[row['id']] for row in result['models']}
            self.ready = True
            self._base_extra_system = self.bridge.agent.extra_system
            # The existing shadow checkpoint repo is project-specific, not shared across opened projects.
            from forge.checkpoint import CheckpointStore
            identity = hashlib.sha256(str(project).encode('utf-8')).hexdigest()[:24]
            self.bridge.agent.checkpoints = CheckpointStore(home / 'projects' / identity, project)
            self.state.remember_project(project)
            self.state.remember_session(result['sessionPath'])
            return result
        finally:
            self._lane.release()

    def run(self, prompt, *, explicit_model=None, agent_model=None, role='default',
            permission='read-only', planning='low', contexts=(), snapshot=None):
        if not self.ready:
            raise RuntimeError('Open a project first')
        if not self._lane.acquire(False):
            raise RuntimeError('Agent is already running; configuration applies to the next task')
        try:
            snap = snapshot or self.state.task_snapshot(explicit_model=explicit_model, agent_model=agent_model,
                                                       role=role, permission=permission)
            self.last_snapshot = snap
            selected = ''
            if snap.model:
                selected = next((identifier for identifier, pair in self.catalog.items() if pair == snap.model), None)
                if selected is None:
                    raise ValueError('Model is not configured in this isolated home')
            params = {'prompt': prompt, 'model': selected, 'mode': snap.permission,
                      'planning': planning if snap.mode == WorkMode.DEVELOPMENT else 'none',
                      'contexts': list(contexts)}
            for source in tuple(self.context_sources):
                # Trusted adapters contribute documents, all validated/virtualized by EditorBridge.
                params['contexts'].extend(source(snap))
            # Review/subagent defaults bind only at the task boundary, and only to core-supported slots.
            agent = getattr(self.bridge, 'agent', None)
            if agent is not None:
                roles = dict(snap.roles) if snap.mode == WorkMode.DEVELOPMENT else {}
                from forge.phase_models import phase_router
                review = roles.get('review')
                phase_router(self.bridge.router, review, self.bridge.router)
                agent.review_model = tuple(review) if review else None
                agent.extra_system = self._base_extra_system + ('\n' + DEVELOPMENT_GUIDANCE
                                     if snap.mode == WorkMode.DEVELOPMENT else '')
                # The core has no general child-default setter; leave subagent role as an integration hint.
            result = self.bridge.run(params)
            for listener in tuple(self.result_observers):
                listener(redact(result))
            return redact(result)
        finally:
            self._lane.release()

    def review(self):
        if not self._lane.acquire(False):
            raise RuntimeError('Wait for the running task before reviewing')
        try:
            target = self.bridge.last_result
            if not target:
                raise ValueError('No completed response to review')
            pair = self.state.value('roles').get('review')
            identifier = next((i for i, p in self.catalog.items() if tuple(pair or ()) == p), '')
            if pair and not identifier:
                raise ValueError('Review model is not configured')
            return redact(self.bridge.review({'enabled': True, 'messageId': target[0], 'model': identifier}))
        finally:
            self._lane.release()

    def recovery(self, path):
        path = Path(path).resolve()
        root = (self.state.home / 'sessions').resolve()
        if not path.is_relative_to(root) or path.suffix != '.jsonl':
            raise PermissionError('Only isolated IDE sessions can be recovered')
        session = Session(path)
        events = list(session.replay())
        run = next((e.data['run_id'] for e in reversed(events) if e.data.get('run_id')), '')
        # Real dialogue only; never turn pending tool intents into new invocations.
        history = [{'role': 'user' if e.type == 'user_message' else 'assistant',
                    'content': str(e.data.get('content', ''))}
                   for e in events if e.type in {'user_message', 'assistant_message'}]
        return {'history': history[-100:], 'events': [{'type': e.type, **e.data} for e in events],
                'workflow': workflow_status(events, run) if run else None, 'path': str(path),
                'workspace': next((e.data.get('cwd', '') for e in events if e.type == 'session_meta'), '')}

    def restore_history(self, path):
        if not self._lane.acquire(False):
            raise RuntimeError('Cannot restore during a task')
        try:
            evidence = self.recovery(path)
            if evidence['workspace'] and Path(evidence['workspace']).resolve() != self.bridge.workspace:
                raise ValueError('Recover this session in its original project')
            from forge.vscode_bridge import _history
            self.bridge.history = self.bridge.agent.secret_scope.protect(_history(evidence['history']))
            # Continue with a fresh journal. Preserve uncertain old actions as read-only evidence.
            return evidence
        finally:
            self._lane.release()

    def close(self):
        if self.busy:
            raise RuntimeError('Task is running; close after completion')
        if self._owns_bridge:
            self.bridge.close()
        elif self._previous_bridge_emit is not None and self.bridge.emit == self._emit:
            self.bridge.emit = self._previous_bridge_emit
