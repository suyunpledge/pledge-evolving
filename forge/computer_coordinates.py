"""Screenshot-bound coordinate clicks. No guessed desktop position or replay."""
import json
import uuid
import weakref

from .computer_win32 import CoordinateNotStarted
from .policy import Mode
from .runtime_lock import named_resource_lock, LockCancelled
from .tools import ToolResult

READ_NAMES = frozenset({'computer_observe','computer_screenshot','computer_vision'})
CLICK_NAME = 'computer_click'
_STATE = ('target','width','height','rect','client_rect','dpi','client_sha256')


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
        try:
            with self.computer.operation(ctx.cancellation) as token:
                return self._click(args, ctx, approve, token)
        except LockCancelled as exc:
            return self.failure('cancelled', str(exc))

    def _click(self, args, ctx, approve, token):
        computer = self.computer
        def stopped():
            return computer.revoked.is_set() or ctx.cancellation is not None and ctx.cancellation.is_set()
        if (set(args) != {'observation_id','x','y','reason'} or
                any(type(args.get(k)) is not int for k in ('x','y')) or
                not isinstance(args.get('reason'),str) or not args['reason'].strip() or
                len(args['reason']) > 500 or '\0' in args['reason']):
            return self.failure('invalid_arguments','Use an observation ID, integer screenshot pixels and a short reason')
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
        try:
            computer.driver.coordinate_click(prepared,frame,cancelled=token)
        except CoordinateNotStarted as exc:
            return self.failure('target_changed',str(exc))
        except Exception as exc:
            return self.failure('coordinate_outcome_unknown',str(exc),True)
        try:
            after = computer.driver.screenshot(cancelled=token)
            if stopped(): return self.failure('cancelled_after_dispatch','Click may have completed; inspect target',True)
            after = self.remember(after,ctx.secret_scope)
            meta = {k:v for k,v in after.items() if k!='png'}
            return ToolResult(True,json.dumps(meta),meta={'execution_state':'completed',
                'verification':'post_screenshot', 'dispatch':'send_input',
                'changed':after['client_sha256']!=before['client_sha256']}), after
        except Exception as exc:
            return self.failure('coordinate_outcome_unknown',str(exc),True)
