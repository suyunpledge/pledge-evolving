"""Screenshot-bound pointer and keyboard input. No guessed desktop position or replay."""
import json
import uuid
import weakref

from .computer_win32 import CoordinateNotStarted, keyboard_events, parse_key
from .policy import Mode
from .runtime_lock import named_resource_lock, LockCancelled
from .tools import ToolResult

READ_NAMES = frozenset({'computer_observe','computer_screenshot','computer_vision'})
CLICK_NAME = 'computer_click'
TYPE_NAME = 'computer_type'
KEY_NAME = 'computer_key'
INPUT_NAMES = frozenset({CLICK_NAME,TYPE_NAME,KEY_NAME})
POINTER_ACTIONS = ('click','double_click','right_click','scroll_up','scroll_down')
_STATE = ('target','width','height','rect','client_rect','dpi','client_sha256')
# Keystrokes go to the focused control, not a pixel, and a blinking caret changes the
# client hash between captures; the window identity and geometry must still match.
_GEOMETRY = _STATE[:-1]


def _reason_ok(args):
    return (isinstance(args.get('reason'),str) and args['reason'].strip() and
            len(args['reason']) <= 500 and '\0' not in args['reason'])


class VisualClickSession:
    def __init__(self, computer):
        self.computer = computer
        self.observations = weakref.WeakKeyDictionary()

    def remember(self, frame, scope):
        """Pixels stay with the caller, only bounded snapshot metadata is retained."""
        with self.computer._lock:
            self.observations.pop(scope,None)
            if not all(key in frame for key in _STATE):
                return {**frame, 'clickable':False}
            meta = {key:frame[key] for key in _STATE}
            meta.update(observation_id=uuid.uuid4().hex, captured_at=self.computer.clock())
            self.observations[scope] = meta
            return {**frame, 'observation_id':meta['observation_id'], 'clickable':True,
                    'coordinate_space':'screenshot_pixels', 'expires_in':self.computer.ttl}

    @staticmethod
    def failure(code, message, unknown=False):
        return ToolResult(False,error=message,meta={'error_code':code,
            'execution_state':'unknown' if unknown else 'not_started'}), None

    def click(self, args, ctx, approve):
        return self._guarded(self._click, args, ctx, approve)

    def type_text(self, args, ctx, approve):
        return self._guarded(self._keyboard, args, ctx, approve, 'type')

    def press_key(self, args, ctx, approve):
        return self._guarded(self._keyboard, args, ctx, approve, 'key')

    def _guarded(self, method, args, ctx, approve, *extra):
        try:
            with self.computer.operation(ctx.cancellation) as token:
                return method(args, ctx, approve, token, *extra)
        except LockCancelled as exc:
            return self.failure('cancelled', str(exc))

    def _stopped(self, ctx):
        return lambda: (self.computer.revoked.is_set() or
                        ctx.cancellation is not None and ctx.cancellation.is_set())

    def _click(self, args, ctx, approve, token):
        computer = self.computer
        stopped = self._stopped(ctx)
        action = args.get('action','click')
        if (not {'observation_id','x','y','reason'} <= set(args) <= {'observation_id','x','y','reason','action','amount'} or
                any(type(args.get(k)) is not int for k in ('x','y')) or not _reason_ok(args) or
                action not in POINTER_ACTIONS or
                'amount' in args and (not action.startswith('scroll') or type(args['amount']) is not int
                                      or not 1 <= args['amount'] <= 10)):
            return self.failure('invalid_arguments','Use an observation ID, integer screenshot pixels, a short reason '
                                'and an optional action; amount (1-10) only with scroll_up/scroll_down')
        if ctx.policy.mode in {Mode.READ_ONLY,Mode.PLAN}:
            return self.failure('policy_denied','This mode does not permit coordinate input')
        with computer._lock:
            before = self.observations.pop(ctx.secret_scope,None)
            if before is None or args['observation_id'] != before['observation_id']:
                return self.failure('stale_observation','Capture or analyze this window again before clicking')
            if not (0 <= args['x'] < before['width'] and 0 <= args['y'] < before['height']):
                return self.failure('invalid_coordinates','Click is outside the screenshot')
            if stopped(): return self.failure('cancelled','Click cancelled before preparation')
            if computer.clock()-before['captured_at'] > computer.ttl:
                return self.failure('stale_observation','Screenshot has expired; capture again')
            try:
                frame = computer.driver.screenshot(cancelled=token)
                if any(frame.get(k) != before[k] for k in _STATE):
                    return self.failure('stale_observation','Window content or geometry changed; capture again')
                prepared = computer.driver.prepare_coordinate(frame,args['x'],args['y'])
                if approve is None or not approve(computer.driver.target_id,dict(args),frame,token):
                    return self.failure('approval_required','Coordinate click was not approved')
                if stopped(): return self.failure('cancelled','Click cancelled during approval')
            except Exception as exc:
                return self.failure('preparation_failed',str(exc))
            with named_resource_lock('computer-input','desktop',cancelled=token):
                return self._confirmed(before,args,ctx,prepared,stopped,token)

    def _confirmed(self,before,args,ctx,prepared,stopped,token):
        computer=self.computer
        try:
            if stopped(): return self.failure('cancelled','Click cancelled before activation')
            activate=getattr(computer.driver,'activate_for_click',None)
            if activate is not None: activate(cancelled=token)
            frame=computer.driver.screenshot(cancelled=token)
            if (computer.clock()-before['captured_at'] > computer.ttl or
                    any(frame.get(k) != before[k] for k in _STATE)):
                return self.failure('stale_observation','Screenshot changed or expired during approval/activation')
            if computer.driver.prepare_coordinate(frame,args['x'],args['y'])!=prepared:
                return self.failure('stale_observation','Native click target changed during approval')
        except Exception as exc:
            return self.failure('preparation_failed',str(exc))
        if stopped(): return self.failure('cancelled','Click cancelled before dispatch')
        self.observations.clear()
        computer._observations.clear()
        extra = {k:args[k] for k in ('action','amount') if k in args and args.get('action','click') != 'click'}
        try:
            computer.driver.coordinate_click(prepared,frame,cancelled=token,**extra)
        except CoordinateNotStarted as exc:
            return self.failure('target_changed',str(exc))
        except Exception as exc:
            return self.failure('coordinate_outcome_unknown',str(exc),True)
        return self._finish(before,ctx,stopped,token,'send_input')

    def _finish(self, before, ctx, stopped, token, dispatch):
        """Post-input screenshot is a fresh observation, so the next step can chain on it."""
        try:
            after = self.computer.driver.screenshot(cancelled=token)
            if stopped(): return self.failure('cancelled_after_dispatch','Input may have completed; inspect target',True)
            after = self.remember(after,ctx.secret_scope)
            meta = {k:v for k,v in after.items() if k!='png'}
            return ToolResult(True,json.dumps(meta),meta={'execution_state':'completed',
                'verification':'post_screenshot', 'dispatch':dispatch,
                'changed':after['client_sha256']!=before['client_sha256']}), after
        except Exception as exc:
            return self.failure('coordinate_outcome_unknown',str(exc),True)

    def _keyboard_args(self, args, kind):
        """Returns (approval args, input events) or raises ValueError. Nothing is dispatched."""
        if kind == 'type':
            if set(args) != {'observation_id','text','reason'}:
                raise ValueError('Use observation_id, text and a short reason')
            text = args['text']
            if not isinstance(text,str) or not text or len(text) > 2000 or '\0' in text:
                raise ValueError('Text must be 1-2000 characters')
            return dict(args), keyboard_events(text=text)
        if not {'observation_id','key','reason'} <= set(args) <= {'observation_id','key','reason','repeat'}:
            raise ValueError('Use observation_id, key, optional repeat and a short reason')
        repeat = args.get('repeat',1)
        if type(repeat) is not int or not 1 <= repeat <= 20:
            raise ValueError('repeat must be an integer from 1 to 20')
        canonical,_,_ = parse_key(args['key'])
        return {**args,'key':canonical,'repeat':repeat}, keyboard_events(key=canonical,repeat=repeat)

    def _keyboard(self, args, ctx, approve, token, kind):
        computer = self.computer
        stopped = self._stopped(ctx)
        label = 'Typing' if kind == 'type' else 'Key press'
        if not isinstance(args.get('observation_id'),str) or not _reason_ok(args):
            return self.failure('invalid_arguments','Use an observation ID and a short reason')
        try:
            shown, events = self._keyboard_args(args, kind)
        except ValueError as exc:
            return self.failure('invalid_arguments',str(exc))
        if ctx.policy.mode in {Mode.READ_ONLY,Mode.PLAN}:
            return self.failure('policy_denied','This mode does not permit keyboard input')
        if getattr(computer.driver,'keyboard_input',None) is None:
            return self.failure('unsupported_target','This target does not accept keyboard input')
        with computer._lock:
            before = self.observations.pop(ctx.secret_scope,None)
            if before is None or args['observation_id'] != before['observation_id']:
                return self.failure('stale_observation','Capture or analyze this window again before typing')
            if stopped(): return self.failure('cancelled',f'{label} cancelled before preparation')
            if computer.clock()-before['captured_at'] > computer.ttl:
                return self.failure('stale_observation','Screenshot has expired; capture again')
            try:
                frame = computer.driver.screenshot(cancelled=token)
                if any(frame.get(k) != before[k] for k in _GEOMETRY):
                    return self.failure('stale_observation','Window moved, resized or changed; capture again')
                if approve is None or not approve(computer.driver.target_id,shown,frame,token):
                    return self.failure('approval_required',f'{label} was not approved')
                if stopped(): return self.failure('cancelled',f'{label} cancelled during approval')
            except Exception as exc:
                return self.failure('preparation_failed',str(exc))
            with named_resource_lock('computer-input','desktop',cancelled=token):
                try:
                    if stopped(): return self.failure('cancelled',f'{label} cancelled before activation')
                    computer.driver.activate_for_keyboard(cancelled=token)
                    frame = computer.driver.screenshot(cancelled=token)
                    if (computer.clock()-before['captured_at'] > computer.ttl or
                            any(frame.get(k) != before[k] for k in _GEOMETRY)):
                        return self.failure('stale_observation','Window changed or expired during approval/activation')
                except Exception as exc:
                    return self.failure('preparation_failed',str(exc))
                if stopped(): return self.failure('cancelled',f'{label} cancelled before dispatch')
                self.observations.clear()
                computer._observations.clear()
                try:
                    computer.driver.keyboard_input(frame,events,cancelled=token)
                except CoordinateNotStarted as exc:
                    return self.failure('target_changed',str(exc))
                except Exception as exc:
                    return self.failure('keyboard_outcome_unknown',str(exc),True)
                return self._finish(before,ctx,stopped,token,'send_input')
