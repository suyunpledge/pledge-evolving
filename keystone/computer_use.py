"""Host-selected computer target: observe -> single-use ref -> act -> observe.

Drivers own target identity and bounded I/O. No global desktop enumeration,
implicit grants, coordinate guessing, or automatic input retries.
"""
import hashlib
import json
import threading
import time
import uuid
import weakref
from contextlib import contextmanager, nullcontext

from .runtime_lock import named_resource_lock, cancellable_lock, LockCancelled
from .tools import ToolResult, ToolSpec
from .policy import ToolEffect

NATIVE_ACTIONS = ('click', 'type_text')
BACKGROUND_ACTIONS = (*NATIVE_ACTIONS, 'expand', 'collapse')


class TargetCancellation:
    """Event-like cancellation combining one request and host revocation."""
    def __init__(self, cancel, revoked):
        self.cancel, self.revoked = cancel, revoked

    def is_set(self):
        return self.revoked.is_set() or self.cancel is not None and self.cancel.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0: return False
            self.revoked.wait(.05 if remaining is None else min(.05, remaining))
        return True


class ComputerSession:
    def __init__(self, driver, *, approve=None, ttl=30, clock=time.monotonic):
        if not 0 < ttl <= 120:
            raise ValueError('Observation ttl must be in (0, 120]')
        self.driver, self.approve, self.ttl, self.clock = driver, approve, ttl, clock
        self.grant_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._revoked = threading.Event()
        self._observations = weakref.WeakKeyDictionary()
        from .computer_coordinates import VisualClickSession
        self.visual = VisualClickSession(self)

    def revoke(self):
        """Host stop/revoke must not wait for a pending approval lock."""
        self._revoked.set()

    @property
    def revoked(self):
        """Worker-safe host cancellation signal; revocation never waits for I/O."""
        return self._revoked

    @contextmanager
    def operation(self, cancelled=None):
        token = TargetCancellation(cancelled, self.revoked)
        with cancellable_lock(self._lock, token), named_resource_lock(
                'computer-target', self.driver.target_id, cancelled=token):
            yield token

    @staticmethod
    def _failure(code, message, *, unknown=False):
        return ToolResult(False, error=message, meta={'error_code': code,
            'execution_state': 'unknown' if unknown else 'not_started'})

    def _snapshot(self, background):
        if background:
            return self.driver.background_controls()
        return nullcontext((self.driver.observe(), self.driver.act))

    def _capture(self, scope, background=False):
        if background:
            with self.driver.background_controls() as (snapshot, _):
                state = dict(snapshot)
        else:
            state = self.driver.observe()
        # Capture only JSON contracts; never silently stringify native handles.
        state = json.loads(json.dumps(state, allow_nan=False))
        if (not isinstance(state, dict) or not isinstance(state.get('elements'), list)
                or len(state['elements']) > 256):
            raise ValueError('Invalid computer observation')
        refs = [row.get('ref') for row in state['elements'] if isinstance(row, dict)]
        if len(refs) != len(state['elements']) or any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
            raise ValueError('Computer observation needs unique element refs')
        if any(not isinstance(row.get('actions'), list) or any(
                not isinstance(action, str) or action not in (BACKGROUND_ACTIONS if background else NATIVE_ACTIONS)
                for action in row['actions']) for row in state['elements']):
            raise ValueError('Computer observation needs explicit supported actions')
        digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        state.pop('_value_revision', None)
        observation = {'observation_id': uuid.uuid4().hex, 'target': self.driver.target_id,
                       'state': state, 'captured_at': self.clock(), 'revision': digest}
        if background:
            observation['input_mode'] = 'background'
        if not state['elements'] or state.get('truncated'):
            # Browsers, Electron and Office draw their own controls; this list cannot see them.
            observation['hint'] = ('No further background actions are available in this observation. '
                                   'Do not use foreground input.' if background else
                                   'Native control list is empty or partial for this window. '
                                   'Where available, use computer_screenshot(list_controls=true) or '
                                   'computer_screenshot(target="visible label") for local VisFunc recognition.')
        self._observations[scope] = observation
        return observation

    @staticmethod
    def _receipt(ctx, observation, offset=0):
        payload = ctx.secret_scope.protect(observation)
        def encode(data):
            return json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        if payload.get('input_mode') != 'background':
            return encode(payload)
        rows = payload['state']['elements']
        limit = ctx.extras.get('artifact_inline_limit', 4000)
        if offset == 0:
            complete = encode(payload)
            if len(complete) <= limit:
                return complete
        if offset >= len(rows) and offset:
            raise ValueError('Control page changed. Read again from offset 0.')
        page = []
        payload = {**payload, 'state': {**payload['state'], 'elements': page}, 'offset': offset}
        # Budget the complete serialized receipt so artifact retention never replaces
        # actionable refs with a plain-text preview. The revision covers all pages.
        for index in range(offset, len(rows)):
            payload['next_offset'] = index + 1
            if index + 1 == len(rows):
                del payload['next_offset']
            page.append(rows[index])
            if len(encode(payload)) > limit:
                page.pop()
                if not page:
                    raise ValueError('A control exceeds the inline receipt budget')
                payload['next_offset'] = index
                break
        return encode(payload)

    def observe(self, ctx, *, background=False, offset=0):
        try:
            with self.operation(ctx.cancellation):
                return self._observe(ctx, background, offset)
        except LockCancelled as exc:
            return self._failure('cancelled', str(exc))

    def observed_control(self, scope, observation_id, ref):
        with self._lock:
            before = self._observations.get(scope)
            if (not before or before['observation_id'] != observation_id
                    or self.clock() - before['captured_at'] > self.ttl):
                raise ValueError('控件观察已失效，请重新调用 computer_observe。')
            for element in before['state']['elements']:
                if element['ref'] == ref and element['actions']:
                    return element
            raise ValueError('当前观察中没有可操作的对应控件。')

    def _observe(self, ctx, background, offset):
        with self._lock:
            self._observations.pop(ctx.secret_scope, None)
            if self._revoked.is_set():
                return self._failure('capability_revoked', 'Computer capability was revoked')
            if ctx.cancellation is not None and ctx.cancellation.is_set():
                return self._failure('cancelled', 'Computer observation cancelled')
            try:
                observation = self._capture(ctx.secret_scope, background)
                if self._revoked.is_set() or ctx.cancellation is not None and ctx.cancellation.is_set():
                    self._observations.pop(ctx.secret_scope, None)
                    return self._failure('cancelled', 'Computer observation cancelled')
                return ToolResult(True, self._receipt(ctx, observation, offset),
                                  meta={'execution_state': 'completed'})
            except Exception as exc:
                self._observations.pop(ctx.secret_scope, None)
                return self._failure('observation_failed', str(exc))

    def act(self, args, ctx, *, background=False, approve=None):
        try:
            with self.operation(ctx.cancellation):
                return self._act(args, ctx, background, approve)
        except LockCancelled as exc:
            return self._failure('cancelled', str(exc))

    def _act(self, args, ctx, background, approve):
        if (set(args) - {'observation_id', 'ref', 'action', 'text'}
                or not isinstance(args.get('action'), str)
                or args.get('action') not in (BACKGROUND_ACTIONS if background else NATIVE_ACTIONS)
                or background and (args.get('action') == 'type_text') != ('text' in args)
                or not isinstance(args.get('ref'), str)
                or not isinstance(args.get('text', ''), str)
                or len(args.get('text', '')) > 10000 or '\0' in args.get('text', '')):
            return self._failure('invalid_arguments', 'Use a current ref and one of its advertised actions')
        with self._lock:
            before = self._observations.get(ctx.secret_scope)
            if (before is None or before['observation_id'] != args.get('observation_id')
                    or background != (before.get('input_mode') == 'background')
                    or self.clock() - before['captured_at'] > self.ttl):
                return self._failure('stale_observation', 'Observe the selected target again before acting')
            del self._observations[ctx.secret_scope]
            element = next((row for row in before['state']['elements'] if row['ref'] == args['ref']), None)
            if element is None or args['action'] not in element.get('actions', []):
                return self._failure('unsupported_target', 'Element does not support the requested action')
            dispatched = False
            try:
                approver = self.approve if approve is None else approve
                details = dict(args)
                if background:
                    details.update(control=element.get('name', ''), role=element.get('class', ''))
                if approver is None or not approver(self.driver.target_id, details):
                    return self._failure('approval_required', 'Computer action was not approved by the host')
                if ctx.cancellation is not None and ctx.cancellation.is_set():
                    return self._failure('cancelled', 'Computer action cancelled before dispatch')
                # Approval may wait while the target moves, navigates or changes.
                with self._snapshot(background) as (current, dispatch):
                    digest = hashlib.sha256(json.dumps(current, sort_keys=True, allow_nan=False).encode()).hexdigest()
                    if digest != before['revision']:
                        return self._failure('stale_observation', 'Target changed; observe again')

                    def start_input():
                        nonlocal dispatched
                        if self._revoked.is_set() or ctx.cancellation is not None and ctx.cancellation.is_set():
                            raise LockCancelled('Computer action cancelled before input')
                        if self.clock() - before['captured_at'] > self.ttl:
                            raise TimeoutError('Observation expired before input; observe again')
                        self._observations.clear()
                        self.visual.observations.clear()
                        dispatched = True

                    arguments = (args['ref'], args['action'], args.get('text', ''))
                    if background:
                        readback_refresh = dispatch(*arguments, before_input=start_input)
                    else:
                        start_input()
                        dispatch(*arguments)
                after = self._capture(ctx.secret_scope, background)
                # Cancellation after dispatch must not hide possible effects.
                if ctx.cancellation is not None and ctx.cancellation.is_set():
                    self._observations.pop(ctx.secret_scope, None)
                    return self._failure('cancelled_after_dispatch', 'Action may have completed; observe before continuing', unknown=True)
                verification = 'post_observation'
                if background:
                    verification = 'value_readback' if args['action'] == 'type_text' else 'dispatched_with_post_observation'
                meta = {
                    'execution_state': 'completed', 'verification': verification,
                    'changed': after['revision'] != before['revision']}
                if background and args['action'] == 'type_text' and readback_refresh == 'scroll_into_view':
                    meta['readback_refresh'] = readback_refresh
                return ToolResult(True, self._receipt(ctx, after), meta=meta)
            except Exception as exc:
                self._observations.pop(ctx.secret_scope, None)
                if not dispatched:
                    if self._revoked.is_set():
                        return self._failure('capability_revoked', 'Computer capability was revoked before input')
                    if isinstance(exc, LockCancelled):
                        return self._failure('cancelled', str(exc))
                    if self.clock() - before['captured_at'] > self.ttl:
                        return self._failure('stale_observation', 'Observation expired; observe again')
                    return self._failure('preparation_failed', str(exc))
                return self._failure('computer_outcome_unknown', str(exc), unknown=True)


def register_computer_tools(registry, *, background=False, approve=None):
    """Trusted host opt-in only. Missing capability never exposes usable tools."""
    def observe(args, ctx):
        offset = args.get('offset', 0)
        if set(args) - ({'offset'} if background else set()) or type(offset) is not int or not 0 <= offset < 256:
            return ComputerSession._failure('invalid_arguments', 'Use an advertised background control page offset')
        return ctx.extras['computer'].observe(ctx, background=background, offset=offset)
    def act(args, ctx):
        return ctx.extras['computer'].act(args, ctx, background=background, approve=approve)
    registry.register(ToolSpec('computer_observe',
        'List actionable accessibility controls in the selected background window, with fresh single-use refs. '
        'Pass next_offset as offset for more controls. Use computer_act only for actions listed on a current ref.' if background else
        'List classic native controls (buttons, text boxes) of the host-selected window with fresh refs. '
        'For Qt or other custom windows, use computer_screenshot with list_controls=true or target when available.',
        observe, schema={'type': 'object', 'properties': {
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 255}} if background else {},
            'additionalProperties': False}),
        trusted=True, effect=ToolEffect.READ, requires=('computer',))
    registry.register(ToolSpec('computer_act', 'One approved action on the selected control using a current observation/ref. '
        'type_text replaces the entire value. Returns fresh controls. Button effects may arrive asynchronously; '
        'verify the application result before repeating. If outcome is unknown, inspect before continuing.',
        act, read_only=False, schema={'type': 'object', 'properties': {
            'observation_id': {'type': 'string'}, 'ref': {'type': 'string'},
            'action': {'type': 'string', 'enum': list(BACKGROUND_ACTIONS if background else NATIVE_ACTIONS)},
            'text': {'type': 'string'}},
            'required': ['observation_id', 'ref', 'action'], 'additionalProperties': False}),
        trusted=True, effect=ToolEffect.DELEGATED_WRITE, requires=('computer',))
