"""One lane over the existing EditorBridge/Agent; no scheduler or executor here."""
import hashlib
from pathlib import Path
import threading

from forge.execution_guard import workflow_status
from forge.secrets import redact, install_logging_redaction
from forge.session import Event
from .evidence import TaskEvidence
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
    def __init__(self, state, *, bridge=None, emit=None, runtime_home=None):
        install_logging_redaction()
        self.state = state
        self.home = Path(runtime_home or state.home).expanduser().resolve()
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
        self.evidence = TaskEvidence()
        self._resume_evidence = None
        self._base_extra_system = getattr(existing_agent, 'extra_system', '')
        self._base_child_factory = getattr(existing_agent, 'child_router_factory', None)
        self._base_child_observer = getattr(existing_agent, 'child_observer', None)
        # Host extension seams, not new knowledge/evolution engines.
        self.context_sources = []
        self.result_observers = []

    @property
    def busy(self):
        return self._lane.locked()

    def _emit(self, event):
        if event.get('event') == 'progress':
            self.evidence.apply(event.get('data', {}))
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
        home = self.home
        # F-09（QA）：General 存储（~/.forge）永不共享；project 内的
        # 【专用 dot 目录】home 放行（GUI 的 project-local 选址合法，
        # 不与项目源码混淆），其余 project 内位置仍拒绝。
        dedicated = home.name == '.forge-development-home' and home.parent == project
        if home.is_relative_to((Path.home() / '.forge').resolve()):
            raise ValueError('Use an independent IDE home outside the project; never share General runtime stores')
        if home.is_relative_to(project) and not dedicated:
            raise ValueError('Use an independent IDE home outside the project; never share General runtime stores')
        if not self._lane.acquire(False):
            raise RuntimeError('A task is running')
        try:
            result = self.bridge.initialize({'workspace': str(project), 'home': str(home)})
            self.catalog = {row['id']: self.bridge.models[row['id']] for row in result['models']}
            self.ready = True
            # F-09（QA）：最小宿主桥（agent 未就绪）也能初始化；
            # child seam 基线照录（Development Loop 接线需要）。
            _agent = getattr(self.bridge, 'agent', None)
            self._base_extra_system = getattr(_agent, 'extra_system', '') or ''
            self._base_child_factory = getattr(_agent, 'child_router_factory', None)
            self._base_child_observer = getattr(_agent, 'child_observer', None)
            # The existing shadow checkpoint repo is project-specific, not shared across opened projects.
            from forge.checkpoint import CheckpointStore
            identity = hashlib.sha256(str(project).encode('utf-8')).hexdigest()[:24]
            _agent = getattr(self.bridge, 'agent', None)
            if _agent is not None:
                _agent.checkpoints = CheckpointStore(home / 'projects' / identity, project)
            self.state.remember_project(project)
            self.state.remember_session(result['sessionPath'])
            return result
        finally:
            self._lane.release()

    def run(self, prompt, *, explicit_model=None, agent_model=None, role='default',
            permission='read-only', planning='low', contexts=(), snapshot=None,
            delegate=False, child_model=None):
        if not self.ready:
            raise RuntimeError('Open a project first')
        if not self._lane.acquire(False):
            raise RuntimeError('Agent is already running; configuration applies to the next task')
        try:
            snap = snapshot or self.state.task_snapshot(explicit_model=explicit_model, agent_model=agent_model,
                                                       role=role, permission=permission)
            self.last_snapshot = snap
            self.evidence = TaskEvidence()
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
                from forge.phase_models import model_pair
                child_model = model_pair(child_model)
                child_pair = child_model or roles.get('subagent')
                child_pair = model_pair(child_pair)
                phase_router(self.bridge.router, child_pair, self.bridge.router)
                overrides = dict(snap.agent_models) if snap.mode == WorkMode.DEVELOPMENT else {}
                for pair in overrides.values():
                    phase_router(self.bridge.router, pair, self.bridge.router)
                # Each actual child keeps explicit assignment > own model > role > inherited route.
                def child_route(name, policy, inherited):
                    choice = child_model or overrides.get(name)
                    if choice is None and getattr(self, '_base_child_factory', None):
                        return self._base_child_factory(name, policy, inherited)
                    choice = choice or child_pair
                    return phase_router(self.bridge.router, choice, inherited)
                agent.child_router_factory = child_route if snap.mode == WorkMode.DEVELOPMENT else getattr(self, '_base_child_factory', None)
                agent.child_observer = self.bridge.observe if snap.mode == WorkMode.DEVELOPMENT else getattr(self, '_base_child_observer', None)
                if self._resume_evidence:
                    agent.extra_system += ('\nRecovery: previous actions may have unknown outcomes. '
                                          'Re-read files and diff before continuing; never replay a pending action automatically.')
                    with agent.session:
                        agent.session.append('development_resume', source=self._resume_evidence['path'],
                                             workflow=self._resume_evidence['workflow'])
                if delegate:
                    params['prompt'] = ('User requests delegation through the existing spawn_subagent tool. '
                        'Use only authorized tools; if unavailable explain why. Do not claim a child started '
                        'unless it really did. Task:\n' + prompt)
                with agent.session:
                    agent.session.append('development_task', work_mode=snap.mode.value, role=snap.role,
                                         selected_model=snap.model, permission=snap.permission,
                                         delegated=bool(delegate))
            result = self.bridge.run(params)
            self._resume_evidence = None
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

    def checkpoint_list(self):
        if not self.ready or not self._lane.acquire(False):
            raise RuntimeError('Open a project and wait for task completion')
        try:
            return [point.to_raw() for point in self.bridge.agent.checkpoints.history(limit=100)]
        finally:
            self._lane.release()

    def checkpoint_preview(self, commit, path):
        if not self._lane.acquire(False):
            raise RuntimeError('Wait for task completion')
        try:
            from .review import checkpoint_preview
            return {k: v for k, v in checkpoint_preview(self.bridge.agent, commit, path).items() if k != 'content'}
        finally:
            self._lane.release()

    def restore_checkpoint(self, commit, path, expected, *, permission='read-only'):
        if not self._lane.acquire(False):
            raise RuntimeError('Wait for task completion')
        try:
            from .review import apply_checkpoint
            if permission not in {'read-only', 'workspace-write'}:
                raise ValueError('Unsupported permission')
            agent = self.bridge.agent
            previous = agent.policy
            agent.policy = self.bridge.policy(permission)
            def publish(event):
                data = dict(event)
                kind = data.pop('type')
                agent.session.append(kind, **data)
                self._emit({'event': 'progress', 'data': event})
            try:
                with agent.session:
                    result = apply_checkpoint(agent, commit, path, expected, publish=publish)
            finally:
                agent.policy = previous
            return {'ok': result.ok, 'error': redact(result.error), 'result': redact(result.content), 'meta': redact(result.meta)}
        finally:
            self._lane.release()

    def recovery(self, path):
        path = Path(path).resolve()
        root = (self.home / 'sessions').resolve()
        if not path.is_relative_to(root) or path.suffix != '.jsonl':
            raise PermissionError('Only isolated IDE sessions can be recovered')
        # Preserve the old log byte-for-byte. A torn final line is uncertainty, not replayable work.
        with path.open('rb') as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Recovery journal exceeds 16 MiB; use the existing session tooling')
        incomplete, events = False, []
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                event = Event.from_line(line.decode('utf-8'))
            except (ValueError, UnicodeError):
                if index != len(lines) - 1 or line.endswith(b'\n'):
                    raise
                incomplete = True
                break
            event.data = redact(event.data)
            events.append(event)
        run = next((e.data['run_id'] for e in reversed(events) if e.type == 'workflow_state'
                    and not e.data.get('depth', 0) and e.data.get('run_id')), '')
        # Real dialogue only; never turn pending tool intents into new invocations.
        history, nested = [], 0
        for e in events:
            if e.type == 'subagent_spawn':
                nested += 1
            elif e.type in {'subagent_result', 'subagent_error'}:
                nested = max(0, nested - 1)
            elif (e.type in {'user_message', 'assistant_message'} and not e.data.get('depth', 0)
                  and ('depth' in e.data or not nested)):
                history.append({'role': 'user' if e.type == 'user_message' else 'assistant',
                                'content': str(e.data.get('content', ''))})
        return {'history': history[-100:], 'events': [{'type': e.type, **e.data} for e in events],
                'workflow': workflow_status(events, run) if run else None, 'path': str(path),
                'incomplete_tail': incomplete,
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
            self.bridge.last_result = None  # Never review a response from the replaced conversation.
            self.evidence = TaskEvidence.from_events(evidence['events'], recovered=True)
            self._resume_evidence = evidence
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
