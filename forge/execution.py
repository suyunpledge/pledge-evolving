"""RC1 public execution contract. No GUI or model transport dependencies.

Only trusted hosts construct contexts/register effects. Request IDs correlate
work; they are not grants. An interrupted intent is UNKNOWN, never replayable.
"""
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
import uuid

from .policy import Policy
from .runtime_lock import resource_lock
from .secrets import SecretScope, redact
from .tools import ToolContext, ToolResult

VERSION = 1


def identity():
    return uuid.uuid4().hex


def validate_id(value, *, optional=False):
    if optional and value == '':
        return
    if not isinstance(value, str) or len(value) > 128 or not value or any(
            not (c.isascii() and (c.isalnum() or c in '-_:')) for c in value):
        raise ValueError('Invalid execution identifier')


@dataclass(frozen=True)
class RunRequest:
    run_id: str = field(default_factory=identity)
    task_id: str = field(default_factory=identity)
    parent_run_id: str = ''
    entry: str = 'host'
    protocol_version: int = VERSION

    def __post_init__(self):
        for value in (self.run_id, self.task_id):
            validate_id(value)
        validate_id(self.parent_run_id, optional=True)
        if self.protocol_version != VERSION:
            raise ValueError('Unsupported execution protocol')

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ActionRequest:
    name: str
    arguments: dict
    action_id: str = field(default_factory=identity)
    tool_call_id: str = ''
    protocol_version: int = VERSION

    def __post_init__(self):
        validate_id(self.action_id)
        if self.protocol_version != VERSION:
            raise ValueError('Unsupported action protocol')
        if not isinstance(self.name, str) or not self.name or not isinstance(self.arguments, dict):
            raise ValueError('Invalid action request')

    def as_dict(self):
        return redact(asdict(self))


@dataclass(frozen=True)
class EventEnvelope:
    type: str
    run_id: str
    task_id: str
    action_id: str = ''
    parent_run_id: str = ''
    payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=identity)
    protocol_version: int = VERSION

    def as_dict(self):
        # Flatten legacy fields, keeping identity authoritative.
        return redact({**self.payload, 'type': self.type, 'run_id': self.run_id,
            'task_id': self.task_id, 'parent_run_id': self.parent_run_id,
            'action_id': self.action_id, 'event_id': self.event_id,
            'protocol_version': self.protocol_version})


@dataclass(frozen=True)
class ActionResult:
    run_id: str
    task_id: str
    action_id: str
    parent_run_id: str
    outcome: str
    error_code: str
    execution_state: str
    authorization: str
    content: str = ''
    error: str = ''
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_tool(cls, ctx, request, value):
        state = value.meta.get('execution_state', 'completed' if value.ok else 'not_started')
        outcome = ('running' if state == 'running' else 'unknown' if state == 'unknown' else 'cancelled' if
                   value.meta.get('error_code') == 'cancelled' else 'succeeded' if value.ok else 'failed')
        return cls(ctx.run.run_id, ctx.run.task_id, request.action_id, ctx.run.parent_run_id,
            outcome, value.meta.get('error_code', '' if value.ok else 'tool_rejected'), state,
            value.meta.get('authorization', 'unknown'), value.content, value.error, dict(value.meta))

    def as_tool_result(self):
        return ToolResult(self.outcome == 'succeeded', self.content, self.error, {
            **self.meta, 'run_id': self.run_id, 'task_id': self.task_id, 'action_id': self.action_id,
            'parent_run_id': self.parent_run_id, 'outcome': self.outcome,
            'error_code': self.error_code, 'execution_state': self.execution_state,
            'authorization': self.authorization, 'protocol_version': VERSION})

    def as_dict(self):
        return self.as_tool_result().as_dict()


@dataclass(frozen=True)
class RunResult:
    run_id: str
    task_id: str
    parent_run_id: str
    outcome: str
    error_code: str = ''
    execution_state: str = 'completed'
    text: str = ''

    def as_dict(self):
        return redact({**asdict(self), 'protocol_version': VERSION})

    @classmethod
    def from_report(cls, report):
        if any(e.get('type') == 'tool_completed' and (e.get('execution_state') == 'unknown'
               or (e.get('meta') or {}).get('execution_state') == 'unknown') for e in report.events):
            return cls(report.run_id, report.task_id, report.parent_run_id,
                       'unknown', 'unknown_outcome', 'unknown', report.text)
        reason = report.stopped
        return cls(report.run_id, report.task_id, report.parent_run_id,
            'succeeded' if reason == 'final' else 'cancelled' if reason == 'cancelled' else 'failed',
            '' if reason == 'final' else reason, text=report.text)


@dataclass(frozen=True)
class ExecutionContext:
    run: RunRequest
    workspace: Path
    home: Path
    policy: Policy
    registry: object
    session: object = None
    cancellation: object = None
    capabilities: object = field(default_factory=lambda: MappingProxyType({}))
    secret_scope: object = field(default_factory=SecretScope, repr=False)
    checkpoints: object = None
    file_guard: object = None
    subscriber: object = None
    tool_emit: object = None
    agent: str = 'host'
    depth: int = 0
    owns_session: bool = False
    recovered: bool = False

    def validate(self):
        if self.workspace.resolve() != Path(self.policy.workspace).resolve():
            raise ValueError('Execution workspace differs from Policy')
        if self.checkpoints is not None and Path(getattr(self.checkpoints, 'workspace', self.workspace)).resolve() != self.workspace:
            raise ValueError('Checkpoint store belongs to another workspace')
        if self.session is not None and self.session.meta.get('cwd') and (
                Path(self.session.meta['cwd']).resolve() != self.workspace):
            raise ValueError('Execution journal belongs to another workspace')
        if self.session is not None:
            with resource_lock('session', self.session.path):
                if self.session.path.exists() and self.session.path.stat().st_size:
                    from .session import Event
                    with self.session.path.open(encoding='utf-8') as stream:
                        meta = Event.from_line(stream.readline()).data
                    if meta.get('cwd') and Path(meta['cwd']).resolve() != self.workspace:
                        raise ValueError('Persisted journal belongs to another workspace')
                    for key in ('run_id', 'task_id', 'parent_run_id'):
                        if key in meta and meta[key] != getattr(self.run, key):
                            raise ValueError('Persisted journal identity mismatch: ' + key)

    @property
    def sandbox(self):
        return self.policy.sandbox

    def describe(self):
        """Safe public descriptor for adapters; no executable handles or credentials."""
        return {**self.run.as_dict(), 'workspace': str(self.workspace), 'home': str(self.home),
            'policy_mode': self.policy.mode.value, 'sandbox': self.sandbox.value,
            'tools': [{'name': spec.name, 'effect': self.registry.effect(spec.name).value,
                       'deferred': spec.is_deferred, 'active': spec.name in self.registry.names()}
                      for spec in self.registry.available_specs(self.tool_context())],
            'capabilities': sorted(k for k,v in self.capabilities.items() if v is not None),
            'cancellation_supported': self.cancellation is not None}

    def tool_context(self):
        return ToolContext(self.policy, self.workspace, self.session, self.tool_emit,
            {**self.capabilities, 'trusted_control_root': self.home, 'registry': self.registry},
            self.secret_scope, isolated=True, cancellation=self.cancellation)

    def publish(self, kind, *, action_id='', **payload):
        event = EventEnvelope(kind, self.run.run_id, self.run.task_id, action_id,
            self.run.parent_run_id, {'agent': self.agent, 'depth': self.depth, **payload}).as_dict()
        event = redact(self.secret_scope.protect(event))
        if self.session is not None:
            persisted = self.session.append(kind, **{k:v for k,v in event.items() if k != 'type'})
            self.session.sync()
            event['ordinal'] = persisted.ordinal
        if self.subscriber is not None:
            try:
                self.subscriber(event)
            except Exception:
                pass  # A destroyed view cannot change durable execution facts.
        return event


def create_execution_context(*, home, workspace, registry, policy=None, config=None,
        run=None, session=None, cancellation=None, capabilities=None, secret_scope=None,
        checkpoints=None, file_guard=None, subscriber=None, tool_emit=None,
        isolate_registry=True, agent='host', depth=0, journal=True, recovered=False):
    """Host factory: preserves supplied grants, never invents a default Policy."""
    workspace = Path(workspace).resolve()
    if policy is None:
        if config is None:
            raise ValueError('Host must supply a Policy or composed config')
        policy = Policy.from_config(config, workspace=workspace, non_interactive=True)
    run = run or RunRequest()
    owns_session = session is None and journal
    if owns_session:
        from .session import Session
        path = Path(home) / 'sessions' / 'runtime' / (hashlib.sha256(run.run_id.encode()).hexdigest() + '.jsonl')
        session = Session(path, meta={'cwd': str(workspace), 'run_id': run.run_id,
                          'task_id': run.task_id, 'parent_run_id': run.parent_run_id})
    context = ExecutionContext(run, workspace, Path(home).resolve(), policy,
        registry.clone() if isolate_registry else registry, session, cancellation,
        MappingProxyType(dict(capabilities or {})), secret_scope or SecretScope(),
        checkpoints, file_guard, subscriber, tool_emit, agent, depth, owns_session, recovered)
    context.validate()
    return context


class ActionExecutor:
    """One lifecycle shared by Agent and Gateway; no automatic retry/replay."""
    def __init__(self):
        self._memory = {}
        self._active = set()

    def query(self, ctx, action_id):
        validate_id(action_id)
        key = (ctx.run.run_id, action_id)
        if ctx.session is not None:
            started = None
            try:
                for event in ctx.session.replay():
                    if event.data.get('run_id') != key[0] or event.data.get('action_id') != key[1]:
                        continue
                    if event.type == 'tool_started':
                        started = event.data
                    if event.type == 'tool_completed' and 'action_result' in event.data:
                        value = event.data['action_result']
                        return {**value, 'meta': {**value['meta'], 'event_id': event.data.get('event_id', '')}}
            except (ValueError, UnicodeError):
                if started is None:
                    raise  # No evidence establishing this identity; require repair.
            if started is not None:
                state = 'running' if key in self._active else 'unknown'
                return {'ok': False, 'error': '' if state == 'running' else 'Action outcome unknown; inspect files before continuing',
                    'content': '', 'meta': {**started, 'outcome': state, 'execution_state': state,
                                           'error_code': '' if state == 'running' else 'unknown_outcome', 'authorization': 'unknown'}}
        value = self._memory.get(key)
        if value is not None and key in self._active and value['meta'].get('execution_state') == 'unknown':
            return {**value, 'error': '', 'meta': {**value['meta'], 'outcome': 'running',
                    'execution_state': 'running', 'error_code': ''}}
        return value

    def execute(self, ctx, request, *, preflight=None):
        if ctx.session is None:
            raise ValueError('Action execution requires a host journal')
        try:
            return self._execute(ctx, request, preflight=preflight)
        finally:
            if ctx.owns_session:
                ctx.session.close()

    def _execute(self, ctx, request, *, preflight=None):
        ctx.validate()
        # The same identity cannot be dispatched concurrently or replayed after
        # process death. This is deduplication of recorded intents, not exactly-once.
        key = (ctx.run.run_id, request.action_id)
        digest = hashlib.sha256(json.dumps([request.name, request.arguments], sort_keys=True,
                                ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        lock_path = (ctx.session.path if ctx.session is not None else ctx.home) / ('action-' + key[0] + '-' + key[1])
        with resource_lock('action', lock_path):
            previous = self.query(ctx, request.action_id)
            if previous is not None:
                if previous['meta'].get('request_digest') != digest:
                    value = ToolResult(False, error='Action identity already belongs to another request',
                        meta={'error_code': 'action_id_conflict', 'execution_state': 'not_started'})
                    return ActionResult.from_tool(ctx, request, value)
                value = ToolResult(previous['ok'], previous.get('content', ''), previous.get('error', ''), previous['meta'])
                value.meta = {**value.meta, 'deduplicated': True}
                return ActionResult.from_tool(ctx, request, value)
            self._active.add(key)
            try:
                tc = ctx.tool_context()
                denial = preflight or ctx.registry.authorize(request.name, request.arguments, tc)
                if ctx.recovered:
                    denial = ToolResult(False, error='Recovered run is evidence only; create a new run with current grants',
                        meta={'authorization': 'deny', 'error_code': 'recovery_requires_new_run', 'execution_state': 'not_started'})
                if ctx.cancellation is not None and ctx.cancellation.is_set():
                    denial = ToolResult(False, error='Action cancelled before execution',
                        meta={'error_code': 'cancelled', 'execution_state': 'not_started'})
                ctx.publish('tool_started', action_id=request.action_id, tool=request.name,
                            tool_call_id=request.tool_call_id, request_digest=digest,
                            execution_state='not_started' if denial else 'running')
                self._memory[key] = ActionResult.from_tool(ctx, request, ToolResult(False,
                    error='Action outcome unknown', meta={'request_digest': digest,
                        'execution_state': 'unknown', 'error_code': 'unknown_outcome'})).as_dict()
                value = denial
                if value is None:
                    with ctx.registry.write_guard(request.name, request.arguments, tc):
                        guard = ctx.file_guard.protect(request.name, request.arguments, tc) if ctx.file_guard is not None else nullcontext(request.arguments)
                        dispatched = False
                        try:
                            with guard as arguments:
                                # Recheck under the same lock as checkpoint and write.
                                value = ctx.registry.authorize(request.name, arguments, tc)
                                if value is None and ctx.cancellation is not None and ctx.cancellation.is_set():
                                    value = ToolResult(False, error='Action cancelled before dispatch',
                                        meta={'error_code': 'cancelled', 'execution_state': 'not_started'})
                                if value is None:
                                    point = ctx.checkpoints.snapshot('before ' + request.name) if ctx.checkpoints is not None and ctx.registry.writes(request.name, arguments) else None
                                    if point is not None:
                                        ctx.publish('checkpoint', action_id=request.action_id,
                                                    commit=point.commit, label=point.label, workspace=str(ctx.workspace))
                                    dispatched = True
                                    value = ctx.registry.invoke(request.name, arguments, tc)
                                    if point is not None:
                                        value.meta['checkpoint'] = point.commit
                                        value.meta['checkpoint_workspace'] = str(ctx.workspace)
                                    if ctx.file_guard is not None:
                                        ctx.file_guard.record(request.name, request.arguments, value)
                        except Exception as exc:
                            value = ToolResult(False, error=redact(str(exc)), meta={
                                'error_code': 'execution_unknown' if dispatched else 'preparation_failed',
                                'execution_state': 'unknown' if dispatched else 'not_started'})
                value.meta['request_digest'] = digest
                result = ActionResult.from_tool(ctx, request, value)
                # If recording fails after a side effect, the durable start remains
                # UNKNOWN. Do not report failure as safely retryable or auto-rollback.
                completed = ctx.publish('tool_completed', action_id=request.action_id, tool=request.name,
                    ok=result.outcome == 'succeeded', args=request.arguments,
                    result=(result.content or result.error)[:1500], meta=result.as_tool_result().meta,
                    outcome=result.outcome, execution_state=result.execution_state,
                    error_code=result.error_code, authorization=result.authorization,
                    action_result=result.as_dict())
                result.meta['event_id'] = completed['event_id']
                self._memory[key] = result.as_dict()
                return result
            finally:
                self._active.discard(key)

