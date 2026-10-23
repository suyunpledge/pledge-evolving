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
from contextlib import contextmanager

from .runtime_lock import named_resource_lock, cancellable_lock, LockCancelled
from .tools import ToolResult, ToolSpec
from .policy import ToolEffect


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

    def _capture(self, scope):
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
                not isinstance(action, str) or action not in {'click', 'type_text'}
                for action in row['actions']) for row in state['elements']):
            raise ValueError('Computer observation needs explicit supported actions')
        digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        observation = {'observation_id': uuid.uuid4().hex, 'target': self.driver.target_id,
                       'state': state, 'captured_at': self.clock(), 'revision': digest}
        self._observations[scope] = observation
        return observation

    def observe(self, ctx):
        try:
            with self.operation(ctx.cancellation):
                return self._observe(ctx)
        except LockCancelled as exc:
            return self._failure('cancelled', str(exc))

    def _observe(self, ctx):
        with self._lock:
            self._observations.pop(ctx.secret_scope, None)
            if self._revoked.is_set():
                return self._failure('capability_revoked', 'Computer capability was revoked')
            if ctx.cancellation is not None and ctx.cancellation.is_set():
                return self._failure('cancelled', 'Computer observation cancelled')
            try:
                observation = self._capture(ctx.secret_scope)
                return ToolResult(True, json.dumps(ctx.secret_scope.protect(observation)),
                                  meta={'execution_state': 'completed'})
            except Exception as exc:
                self._observations.pop(ctx.secret_scope, None)
                return self._failure('observation_failed', str(exc))

    def act(self, args, ctx):
        try:
            with self.operation(ctx.cancellation):
                return self._act(args, ctx)
        except LockCancelled as exc:
            return self._failure('cancelled', str(exc))

    def _act(self, args, ctx):
        if (set(args) - {'observation_id', 'ref', 'action', 'text'}
                or not isinstance(args.get('action'), str)
                or args.get('action') not in {'click', 'type_text'}
                or not isinstance(args.get('ref'), str)
                or not isinstance(args.get('text', ''), str)
                or len(args.get('text', '')) > 10000 or '\0' in args.get('text', '')):
            return self._failure('invalid_arguments', 'Use a current ref and click/type_text action')
        with self._lock:
            before = self._observations.pop(ctx.secret_scope, None)
            if (before is None or before['observation_id'] != args.get('observation_id')
                    or self.clock() - before['captured_at'] > self.ttl):
                return self._failure('stale_observation', 'Observe the selected target again before acting')
            element = next((row for row in before['state']['elements'] if row['ref'] == args['ref']), None)
            if element is None or args['action'] not in element.get('actions', []):
                return self._failure('unsupported_target', 'Element does not support the requested action')
            try:
                if self.approve is None or not self.approve(self.driver.target_id, dict(args)):
                    return self._failure('approval_required', 'Computer action was not approved by the host')
                if ctx.cancellation is not None and ctx.cancellation.is_set():
                    return self._failure('cancelled', 'Computer action cancelled before dispatch')
                # Approval may wait while the target moves, navigates or changes.
                current = self.driver.observe()
                digest = hashlib.sha256(json.dumps(current, sort_keys=True, allow_nan=False).encode()).hexdigest()
                if digest != before['revision']:
                    return self._failure('stale_observation', 'Target changed; observe again')
            except Exception as exc:
                return self._failure('preparation_failed', str(exc))
            try:
                if self._revoked.is_set():
                    return self._failure('capability_revoked', 'Computer capability was revoked before input')
                if ctx.cancellation is not None and ctx.cancellation.is_set():
                    return self._failure('cancelled', 'Computer action cancelled before input')
                self._observations.clear()  # Other lanes must reobserve even if the UI looks unchanged.
                self.visual.observations.clear()
                self.driver.act(args['ref'], args['action'], args.get('text', ''))
                after = self._capture(ctx.secret_scope)
                # Cancellation after dispatch must not hide possible effects.
                if ctx.cancellation is not None and ctx.cancellation.is_set():
                    self._observations.pop(ctx.secret_scope, None)
                    return self._failure('cancelled_after_dispatch', 'Action may have completed; observe before continuing', unknown=True)
                return ToolResult(True, json.dumps(ctx.secret_scope.protect(after)), meta={
                    'execution_state': 'completed', 'verification': 'post_observation',
                    'changed': after['revision'] != before['revision']})
            except Exception as exc:
                self._observations.pop(ctx.secret_scope, None)
                return self._failure('computer_outcome_unknown', str(exc), unknown=True)


def register_computer_tools(registry):
    """Trusted host opt-in only. Missing capability never exposes usable tools."""
    def observe(args, ctx):
        if args:
            return ComputerSession._failure('invalid_arguments', 'Observe takes no arguments')
        return ctx.extras['computer'].observe(ctx)
    def act(args, ctx):
        return ctx.extras['computer'].act(args, ctx)
    registry.register(ToolSpec('computer_observe', 'Observe the host-selected window; obtain fresh element refs.',
        observe, schema={'type': 'object', 'properties': {}, 'additionalProperties': False}),
        trusted=True, effect=ToolEffect.READ, requires=('computer',))
    registry.register(ToolSpec('computer_act', 'One approved action using a current observation/ref; returns fresh state.',
        act, read_only=False, schema={'type': 'object', 'properties': {
            'observation_id': {'type': 'string'}, 'ref': {'type': 'string'},
            'action': {'type': 'string', 'enum': ['click', 'type_text']}, 'text': {'type': 'string'}},
            'required': ['observation_id', 'ref', 'action'], 'additionalProperties': False}),
        trusted=True, effect=ToolEffect.DELEGATED_WRITE, requires=('computer',))
