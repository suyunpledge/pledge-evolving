"""One lane over the existing EditorBridge/Agent; no scheduler or executor here."""
import hashlib
from pathlib import Path
import threading
import uuid

from forge.execution_guard import workflow_status
from forge.secrets import redact, install_logging_redaction
from forge.session import Event, _redact_record
from .evidence import TaskEvidence
from forge.vscode_bridge import EditorBridge
from .state import WorkMode

DEVELOPMENT_GUIDANCE = (
    'Development Mode: inspect the actual project and relevant code before editing. '
    'For nontrivial work plan dependencies and changes, make scoped edits, run relevant tests '
    'through authorized tools, inspect the actual diff, and summarize evidence and limitations. '
    'Simplify this workflow for small tasks. Report only real tool/test events; '
    'Before editing an existing file, read it through a file tool in this Agent run; '
    'read it again after a revision conflict. '
    'Never claim tests ran when Policy denied execution. Do not overwrite unsaved editor buffers. '
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
        self.tasks = {}
        self.last_task_id = ''
        self._control = threading.RLock()
        self._cancel_event = None
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
            data = event.get('data', {})
            task = self.tasks.get(data.get('task_id'))
            (task['evidence'] if task else self.evidence).apply(data)
        for listener in (self._previous_bridge_emit, self.emit):
            if listener is not None:
                try:
                    listener(redact(event))
                except Exception:
                    # A destroyed view must not change execution or journal truth.
                    pass

    def _publish(self, kind, **data):
        event = redact({'type': kind, 'task_id': self.last_task_id, **data})
        agent = getattr(self.bridge, 'agent', None)
        if agent is not None:
            with agent.session:
                agent.session.append(kind, **{k: v for k, v in event.items() if k != 'type'})
                agent.session.sync()
        self._emit({'event': 'progress', 'data': event})

    def cancel(self):
        """Request a stop at the next boundary; do not pretend a worker has exited."""
        with self._control:
            if self._cancel_event is None:
                return False
            self._cancel_event.set()
            return True

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
        agent = getattr(self.bridge, 'agent', None)
        attributes = ('router', 'policy', 'planning_level', 'planning_model', 'review_model',
                      'review_enabled', 'extra_system', 'child_router_factory', 'child_observer',
                      'cancellation_event', 'file_guard', 'task_id')
        previous = {key: getattr(agent, key) for key in attributes if agent is not None and hasattr(agent, key)}
        with self._control:
            self._cancel_event = threading.Event()
        started = False
        try:
            snap = snapshot or self.state.task_snapshot(explicit_model=explicit_model, agent_model=agent_model,
                                                       role=role, permission=permission)
            self.last_snapshot = snap
            self.evidence = TaskEvidence()
            self.last_task_id = uuid.uuid4().hex
            self.tasks[self.last_task_id] = {'snapshot': snap, 'evidence': self.evidence}
            self.bridge.last_result = None
            started = True
            self._publish('development_task_started', phase='executing', work_mode=snap.mode.value)
            selected = ''
            if snap.model:
                selected = next((identifier for identifier, pair in self.catalog.items() if pair == snap.model), None)
                if selected is None:
                    raise ValueError('Model is not configured in this isolated home')
            params = {'prompt': prompt, 'model': selected, 'mode': snap.permission,
                      'planning': planning if snap.mode == WorkMode.DEVELOPMENT else 'none',
                      'contexts': list(contexts)}
            for source in tuple(self.context_sources) if snap.mode == WorkMode.DEVELOPMENT else ():
                # Trusted adapters contribute documents, all validated/virtualized by EditorBridge.
                params['contexts'].extend(source(snap))
            # Review/subagent defaults bind only at the task boundary, and only to core-supported slots.
            agent = getattr(self.bridge, 'agent', None)
            if agent is not None:
                agent.task_id = self.last_task_id
                agent.cancellation_event = self._cancel_event
                if snap.mode == WorkMode.DEVELOPMENT:
                    from .guard import FileRevisionGuard
                    agent.file_guard = FileRevisionGuard(self.bridge.workspace)
                roles = dict(snap.roles) if snap.mode == WorkMode.DEVELOPMENT else {}
                from forge.phase_models import phase_router
                review = roles.get('review') if snap.mode == WorkMode.DEVELOPMENT else previous.get('review_model')
                phase_router(self.bridge.router, review, self.bridge.router)
                agent.review_model = tuple(review) if review else None
                self._publish('development_agent_config', agent=agent.name,
                    selected_model=snap.model or getattr(self.bridge.router, 'primary', None),
                    model_source='independent task' if explicit_model or agent_model else 'independent role' if snap.model else 'inherited',
                    permission=snap.permission, tool_surface=agent.registry.names(), role=snap.role)
                self._publish('development_agent_config', agent='Review Agent',
                    selected_model=review or snap.model, model_source='independent role' if review else 'inherited',
                    permission='no tool execution', tool_surface=[], role='review')
                agent.extra_system = previous.get('extra_system', '') + ('\n' + DEVELOPMENT_GUIDANCE
                                     if snap.mode == WorkMode.DEVELOPMENT else '')
                from forge.phase_models import model_pair
                child_model = model_pair(child_model) if snap.mode == WorkMode.DEVELOPMENT else None
                child_pair = child_model or roles.get('subagent')
                child_pair = model_pair(child_pair)
                phase_router(self.bridge.router, child_pair, self.bridge.router)
                overrides = dict(snap.agent_models) if snap.mode == WorkMode.DEVELOPMENT else {}
                for pair in overrides.values():
                    phase_router(self.bridge.router, pair, self.bridge.router)
                # Each actual child keeps explicit assignment > own model > role > inherited route.
                def child_route(name, policy, inherited):
                    choice = child_model or overrides.get(name)
                    if choice is None and previous.get('child_router_factory'):
                        route = previous['child_router_factory'](name, policy, inherited)
                        source = 'host configured'
                    else:
                        choice = choice or child_pair
                        route = phase_router(self.bridge.router, choice, inherited)
                        source = 'explicit' if child_model else 'independent' if overrides.get(name) else 'role' if child_pair else 'inherited'
                    self._publish('development_agent_config', agent=name,
                        selected_model=choice or getattr(route, 'primary', None) or snap.model, model_source=source,
                        permission=policy.mode.value, tool_surface=agent.registry.names(), role='subagent')
                    return route
                agent.child_router_factory = child_route if snap.mode == WorkMode.DEVELOPMENT else previous.get('child_router_factory')
                agent.child_observer = self.bridge.observe if snap.mode == WorkMode.DEVELOPMENT else previous.get('child_observer')
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
                                         task_id=self.last_task_id,
                                         selected_model=snap.model, permission=snap.permission,
                                         delegated=bool(delegate))
            result = self.bridge.run(params)
            if agent is not None:
                self.tasks[self.last_task_id]['router'] = agent.router
            result = {**result, 'task_id': self.last_task_id}
            self.tasks[self.last_task_id]['result'] = redact(result)
            self._publish('development_task_finished', phase='completed' if result.get('ok') else
                          'cancelled' if result.get('stopped') == 'cancelled' else 'failed',
                          run_id=result.get('messageId', ''), result=result)
            self._resume_evidence = None
            for listener in tuple(self.result_observers):
                try:
                    listener(redact(result))
                except Exception as exc:
                    self._emit({'event': 'progress', 'data': {'type': 'observer_error',
                               'task_id': self.last_task_id, 'error': redact(str(exc))}})
            return redact(result)
        except BaseException as exc:
            if started:
                self._publish('development_task_finished', phase='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit))
                              else 'failed', error=redact(str(exc)))
            raise
        finally:
            for key, value in previous.items():
                setattr(agent, key, value)
            with self._control:
                self._cancel_event = None
            self._lane.release()

    def review(self):
        if not self._lane.acquire(False):
            raise RuntimeError('Wait for the running task before reviewing')
        try:
            target = self.bridge.last_result
            if not target:
                raise ValueError('No completed response to review')
            snap = self.last_snapshot
            pair = dict(snap.roles).get('review') if snap and snap.mode == WorkMode.DEVELOPMENT else self.bridge.agent.review_model
            identifier = next((i for i, p in self.catalog.items() if tuple(pair or ()) == p), '')
            if pair and not identifier:
                raise ValueError('Review model is not configured')
            agent = self.bridge.agent
            previous = agent.review_model, agent.review_enabled, agent.task_id, agent.cancellation_event, agent.router
            with self._control:
                self._cancel_event = threading.Event()
            try:
                agent.task_id = self.last_task_id
                agent.cancellation_event = self._cancel_event
                agent.router = self.tasks.get(self.last_task_id, {}).get('router', agent.router)
                return redact({**self.bridge.review({'enabled': True, 'messageId': target[0], 'model': identifier}),
                               'task_id': self.last_task_id})
            except Exception as exc:
                from forge.loop import RunCancelled
                if not isinstance(exc, RunCancelled):
                    raise
                return {'ok': False, 'status': 'cancelled', 'messageId': target[0], 'task_id': self.last_task_id}
            finally:
                agent.review_model, agent.review_enabled, agent.task_id, agent.cancellation_event, agent.router = previous
                with self._control:
                    self._cancel_event = None
        finally:
            self._lane.release()

    def execute_tests(self, argv, *, trusted=False, timeout=120):
        """Human/host execution only. Never registered as an Agent tool.

        trusted means the human approved this exact native command and project code.
        This is account-level execution, not a Secret-safe or OS-sandboxed capability.
        """
        if not self.ready or not self._lane.acquire(False):
            raise RuntimeError('Open a project and wait for task completion')
        try:
            snap = self.last_snapshot
            if (not trusted or not snap or snap.mode != WorkMode.DEVELOPMENT or
                    self.state.mode != WorkMode.DEVELOPMENT or snap.permission != 'workspace-write'):
                raise PermissionError('Explicit human approval and a Development workspace-write task are required')
            from .testing import execute
            from forge.policy import Decision
            import subprocess
            command = subprocess.list2cmdline(argv)
            decision = self.bridge.policy(snap.permission).evaluate('shell_exec', args={'command': command})
            if decision == Decision.DENY:
                raise PermissionError('Test command denied by Policy')
            with self._control:
                self._cancel_event = threading.Event()
            run_id = uuid.uuid4().hex
            self._publish('test_started', run_id=run_id, agent='human-test', command=command,
                          authorization='human-approved')
            try:
                result = execute(argv, self.bridge.workspace, self.home, self._cancel_event,
                                 timeout=timeout, task_id=self.last_task_id, run_id=run_id)
            except BaseException as exc:
                self._publish('test_result', run_id=run_id, agent='human-test', command=command,
                              status='interrupted', error=redact(str(exc)), exit_code=None)
                raise
            self._publish('test_result', run_id=run_id, agent='human-test', command=command, **result)
            return {**result, 'task_id': self.last_task_id, 'run_id': run_id}
        finally:
            with self._control:
                self._cancel_event = None
            self._lane.release()

    def inspect_diff(self, source, path, view='combined'):
        if not self._lane.acquire(False):
            raise RuntimeError('Wait for task completion before capturing Diff')
        try:
            if source.root != self.bridge.workspace:
                raise ValueError('Diff belongs to another project')
            from .guard import FileRevisionGuard
            guard = FileRevisionGuard(source.root)
            with guard.lock:
                target = source.boundary.path(path)
                value = source.diff(target, view)
                if self.last_task_id:
                    self._publish('git_diff', path=str(target), view=view, diff=value,
                                  revision=guard.digest(target), source='workspace-snapshot')
            return value
        finally:
            self._lane.release()

    def checkpoint_list(self):
        if not self.ready or not self._lane.acquire(False):
            raise RuntimeError('Open a project and wait for task completion')
        try:
            return [point.to_raw() for point in self.bridge.agent.checkpoints.history(limit=100)]
        finally:
            self._lane.release()

    def refresh_change_states(self):
        """Read Git objects, never infer commits from a clean status or modify Git."""
        if not self.ready or not self._lane.acquire(False):
            raise RuntimeError('Wait for task completion before checking change versions')
        try:
            import subprocess
            from .guard import FileRevisionGuard
            guard = FileRevisionGuard(self.bridge.workspace)
            for change in list(self.evidence.changes.values()):
                if change['state'] == 'pending':
                    continue
                path = guard.boundary.path(change['path'])
                current = guard.digest(path)
                committed, commit = False, ''
                relative = path.relative_to(self.bridge.workspace).as_posix()
                options = dict(cwd=self.bridge.workspace, capture_output=True, timeout=10,
                    stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                head = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], **options)
                if head.returncode == 0:
                    commit = head.stdout.decode('ascii').strip()
                    size = subprocess.run(['git', 'cat-file', '-s', 'HEAD:' + relative], **options)
                    if size.returncode == 0 and size.stdout.strip().isdigit() and int(size.stdout) <= 512 * 1024:
                        blob = subprocess.run(['git', 'cat-file', 'blob', 'HEAD:' + relative], **options)
                        committed = blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() == change.get('revision')
                    elif change.get('revision') is None:
                        # An absent object only proves deletion when the pre-write
                        # HEAD existed; no automatic committed classification.
                        commit = ''
                self._publish('development_change_state', path=change['path'], action_id=change['action_id'],
                    state='committed' if committed else 'written', commit=commit if committed else '',
                    revision_changed=current != change.get('revision'))
            return list(self.evidence.changes.values())
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
                data = {'task_id': self.last_task_id, **event}
                kind = data.pop('type')
                agent.session.append(kind, **data)
                self._emit({'event': 'progress', 'data': {'type': kind, **data}})
            try:
                with agent.session:
                    from .guard import FileRevisionGuard
                    with FileRevisionGuard(self.bridge.workspace).lock:
                        result = apply_checkpoint(agent, commit, path, expected, publish=publish)
                        if result.ok:
                            guard = FileRevisionGuard(self.bridge.workspace)
                            target = str(guard.boundary.path(path))
                            for change in list(self.evidence.changes.values()):
                                if change['path'] == target and change['state'] != 'pending':
                                    self._publish('development_change_state', path=target,
                                        action_id=change['action_id'], state=change['state'],
                                        revision_changed=guard.digest(guard.boundary.path(path)) != change.get('revision'))
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
            # Session owns the public rollout identity exemption. It is not an
            # HTTP session credential and must not poison known-value detection.
            event.data = _redact_record(event.data) if event.type == 'session_meta' else redact(event.data)
            events.append(event)
        latest_task = next((e.data.get('task_id') for e in reversed(events) if e.data.get('task_id')), '')
        task_events = [e for e in events if not latest_task or e.data.get('task_id') == latest_task]
        run = next((e.data['run_id'] for e in reversed(task_events) if e.type == 'workflow_state'
                    and not e.data.get('depth', 0) and e.data.get('run_id')), '')
        # Real dialogue only; never turn pending tool intents into new invocations.
        history, nested = [], 0
        for e in events:
            if e.type == 'development_recovery':
                from forge.vscode_bridge import _history
                history = _history(e.data.get('history', []))
            elif e.type == 'subagent_spawn':
                nested += 1
            elif e.type in {'subagent_result', 'subagent_error'}:
                nested = max(0, nested - 1)
            elif (e.type in {'user_message', 'assistant_message'} and not e.data.get('depth', 0)
                  and ('depth' in e.data or not nested)):
                from forge.loop import sanitise_child_output
                history.append({'role': 'user' if e.type == 'user_message' else 'assistant',
                                'content': sanitise_child_output(str(e.data.get('content', '')))})
        return {'history': history[-100:], 'events': [{'type': e.type, **e.data} for e in events],
                'workflow': workflow_status(task_events, run) if run else None, 'path': str(path),
                'task_id': latest_task,
                'task_status': TaskEvidence.from_events([{'type': e.type, **e.data} for e in task_events], recovered=True).phase,
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
            self.last_task_id = self.evidence.task_id
            self.last_snapshot = None  # Recovery cannot reinstate a write grant.
            if self.last_task_id:
                self.tasks[self.last_task_id] = {'evidence': self.evidence, 'recovered': True}
            self._resume_evidence = evidence
            # Continue with a fresh journal. Preserve uncertain old actions as read-only evidence.
            from forge.session import Session
            old_session = self.bridge.agent.session
            fresh = old_session.path.parent / (uuid.uuid4().hex + '.jsonl')
            self.bridge.agent.session = Session(fresh, meta={'cwd': str(self.bridge.workspace),
                                                            'resumed_from': evidence['path']})
            old_session.close()
            with self.bridge.agent.session as journal:
                journal.append('development_recovery', task_id=self.last_task_id,
                               source=evidence['path'], history=self.bridge.history,
                               phase=evidence['task_status'], workflow=evidence['workflow'])
                journal.sync()
            self.state.remember_session(fresh)
            evidence['continuation_path'] = str(fresh)
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
