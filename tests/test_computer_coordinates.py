"""Coordinate input policy, stale pixels, unknown outcomes and real DPI mapping."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'forge-gui'))
from computer_panel import ComputerLane,TargetCancellation
from forge.computer_use import ComputerSession
from forge.computer_win32 import WindowsControlDriver, CoordinateNotStarted
from forge.execution import ActionRequest
from forge.policy import Policy,Mode,Sandbox
from forge.secrets import SecretScope
from phase_client import VisionClient
from forge.runtime_lock import named_resource_lock


class Driver:
    target_id='fixture:coordinate'
    def __init__(self):
        self.hash='before';self.rect=[100,100,500,400];self.dpi=144
        self.sent=[];self.fail=False;self.post_fail=False;self.click_target='child'
    def screenshot(self,**kwargs):
        if self.post_fail and self.sent: raise CoordinateNotStarted('readback failed')
        return {'target':self.target_id,'png':b'fixture','width':400,'height':300,
            'rect':self.rect[:],'client_rect':[110,130,490,390],'dpi':self.dpi,
            'client_sha256':self.hash,'sha256':self.hash}
    def prepare_coordinate(self,frame,x,y):
        return {'child':self.click_target,'x':x,'y':y}
    def coordinate_click(self,prepared,frame,**kwargs):
        self.sent.append(prepared);self.hash='after'
        if self.fail: raise TimeoutError('input receipt lost')


class CoordinateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.driver=Driver();self.computer=ComputerSession(self.driver)
        self.scope=SecretScope();self.addCleanup(self.scope.close)
        self.cancel=threading.Event();self.approve=Mock(return_value=True)

    def lane(self,scope=None,clicks=True,**policy):
        return ComputerLane(self.computer,rows=[],selection=None,environment={},consent=False,
            scope=scope or self.scope,cancel=self.cancel,workspace=Path(self.tmp.name),home=Path(self.tmp.name)/'control',
            policy=Policy(workspace=Path(self.tmp.name),**policy),clicks=clicks,approve=self.approve)

    def observe(self,lane):
        result=lane.call('computer_screenshot',{});self.assertTrue(result['ok'],result)
        return json.loads(result['content'])

    def args(self,frame,**extra):
        return {'observation_id':frame['observation_id'],'x':200,'y':150,'reason':'Click VERIFY',**extra}

    def test_click_has_post_observation_and_consumes_old_reference(self):
        lane=self.lane();frame=self.observe(lane)
        result=lane.call('computer_click',self.args(frame))
        self.assertTrue(result['ok'],result);self.assertEqual(len(self.driver.sent),1)
        self.assertEqual(result['meta']['verification'],'post_screenshot')
        self.assertTrue(result['meta']['changed'])
        self.assertNotEqual(json.loads(result['content'])['observation_id'],frame['observation_id'])
        self.assertFalse(lane.call('computer_click',self.args(frame))['ok'])

    def test_readonly_plan_sandbox_and_denies_cannot_be_upgraded(self):
        for policy in ({'mode':Mode.READ_ONLY},{'mode':Mode.PLAN},{'sandbox':Sandbox.READ_ONLY},
                       {'mode':Mode.PLAN,'allow':('*',)},
                       {'deny':('computer_click',)},{'ask':('computer_click',),'non_interactive':True}):
            lane=self.lane(**policy);frame=self.observe(lane)
            self.assertFalse(lane.call('computer_click',self.args(frame))['ok'])
        self.assertFalse(self.driver.sent);self.approve.assert_not_called()

    def test_disabled_capability_does_not_expose_or_execute_click(self):
        lane=self.lane(clicks=False);frame=self.observe(lane)
        self.assertNotIn('computer_click',[s['function']['name'] for s in lane.schemas()])
        self.assertFalse(lane.call('computer_click',self.args(frame))['ok']);self.assertFalse(self.driver.sent)

    def test_invalid_coordinates_and_arguments_never_dispatch(self):
        lane=self.lane()
        for bad in ({'x':True},{'x':-1},{'x':400},{'y':300},{'x':1.5},{'reason':''},{'reason':'x'*501}):
            frame=self.observe(lane)
            self.assertFalse(lane.call('computer_click',self.args(frame,**bad))['ok'])
        self.assertFalse(self.driver.sent)

    def test_move_pixels_dpi_expiry_and_changed_native_target_rejected(self):
        lane=self.lane()
        for kind in ('move','pixels','dpi','expiry','child'):
            self.computer.clock=lambda:0;self.driver.rect=[100,100,500,400]
            frame=self.observe(lane)
            if kind=='move':self.driver.rect[0]+=10
            if kind=='pixels':self.driver.hash+='changed'
            if kind=='dpi':self.driver.dpi+=24
            if kind=='expiry':self.computer.clock=lambda:100
            if kind=='child':self.approve.side_effect=lambda *a:setattr(self.driver,'click_target','new') or True
            result=lane.call('computer_click',self.args(frame))
            self.assertFalse(result['ok'],kind)
            self.assertEqual(result['meta']['execution_state'],'not_started')
        self.assertFalse(self.driver.sent)

    def test_approval_denial_change_revocation_and_cancel(self):
        for kind in ('deny','change','revoke','cancel'):
            self.cancel.clear();self.computer=ComputerSession(self.driver)
            def approval(*args):
                if kind=='change':self.driver.hash+='change'
                if kind=='revoke':self.computer.revoke()
                if kind=='cancel':self.cancel.set()
                return kind!='deny'
            self.approve.side_effect=approval
            lane=self.lane();frame=self.observe(lane)
            self.assertFalse(lane.call('computer_click',self.args(frame))['ok'])
        self.assertFalse(self.driver.sent)

    def test_dispatch_and_post_capture_failure_are_unknown_no_replay(self):
        for kind in ('input','post'):
            self.driver.fail=kind=='input';self.driver.post_fail=False;self.driver.sent=[]
            lane=self.lane();frame=self.observe(lane)
            self.driver.post_fail=kind=='post'
            result=lane.call('computer_click',self.args(frame))
            self.assertEqual(result['meta']['execution_state'],'unknown')
            self.assertFalse(lane.call('computer_click',self.args(frame))['ok'])
            self.assertEqual(len(self.driver.sent),1)

    def test_cancel_after_native_input_is_unknown(self):
        lane=self.lane();frame=self.observe(lane)
        original=self.driver.coordinate_click
        def clicked(*args,**kwargs):original(*args,**kwargs);self.cancel.set()
        self.driver.coordinate_click=clicked
        result=lane.call('computer_click',self.args(frame))
        self.assertEqual(result['meta']['execution_state'],'unknown')
        self.assertEqual(len(self.driver.sent),1)

    def test_target_cancellation_supports_router_wait(self):
        revoked=threading.Event();token=TargetCancellation(self.cancel,revoked)
        self.assertFalse(token.wait(.01))
        revoked.set();self.assertTrue(token.wait(1))

    def test_cancel_and_revoke_while_waiting_on_session_or_desktop_lock(self):
        for kind in ('session', 'desktop'):
            for signal in ('cancel', 'revoke'):
                self.cancel.clear(); self.computer=ComputerSession(self.driver)
                lane=self.lane(); frame=self.observe(lane)
                blocker=(self.computer._lock if kind=='session' else
                         named_resource_lock('computer-input','desktop'))
                results=[]; started=threading.Event()
                def run():
                    started.set()
                    results.append(lane.call('computer_click',self.args(frame)))
                with blocker:
                    worker=threading.Thread(target=run,daemon=True);worker.start()
                    self.assertTrue(started.wait(2));time.sleep(.1)
                    if signal=='cancel':self.cancel.set()
                    else:self.computer.revoke()
                    worker.join(2)
                    self.assertFalse(worker.is_alive(),(kind,signal))
                self.assertEqual(results[0]['meta']['execution_state'],'not_started',results)
                self.assertEqual(results[0]['meta']['error_code'],'cancelled',results)
        self.assertFalse(self.driver.sent)

    def test_direct_session_revocation_reaches_pending_approval_token(self):
        lane=self.lane();frame=self.observe(lane)
        entered=threading.Event();results=[]
        def approve(target,args,frame,token):
            entered.set()
            self.assertTrue(token.wait(2))
            return False
        self.approve.side_effect=approve
        worker=threading.Thread(target=lambda:results.append(
            self.computer.visual.click(self.args(frame),lane.ctx.tool_context(),self.approve)),daemon=True)
        worker.start();self.assertTrue(entered.wait(2));self.computer.revoke();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertFalse(results[0][0].ok)
        self.assertFalse(self.driver.sent)

    def test_scope_and_other_lane_invalidation(self):
        other_scope=SecretScope();self.addCleanup(other_scope.close)
        one,two=self.lane(),self.lane(scope=other_scope)
        first,second=self.observe(one),self.observe(two)
        self.assertFalse(two.call('computer_click',self.args(first))['ok'])
        second=self.observe(two)
        self.assertTrue(one.call('computer_click',self.args(first))['ok'])
        self.assertFalse(two.call('computer_click',self.args(second))['ok'])
        self.assertEqual(len(self.driver.sent),1)

    def test_distinct_targets_serialize_physical_input(self):
        first=self.lane();one=self.observe(first)
        self.driver=Driver();self.driver.target_id='fixture:other'
        self.computer=ComputerSession(self.driver)
        second=self.lane();two=self.observe(second)
        active=0;maximum=0;lock=threading.Lock()
        for lane in (first,second):
            original=lane.session.driver.coordinate_click
            def dispatch(*args,original=original,**kw):
                nonlocal active,maximum
                with lock:active+=1;maximum=max(maximum,active)
                try:time.sleep(.05);original(*args,**kw)
                finally:
                    with lock:active-=1
            lane.session.driver.coordinate_click=dispatch
        results=[]
        workers=[threading.Thread(target=lambda lane=lane,frame=frame:results.append(lane.call('computer_click',self.args(frame))))
                 for lane,frame in ((first,one),(second,two))]
        for worker in workers:worker.start()
        for worker in workers:worker.join(5)
        self.assertEqual(len(results),2);self.assertTrue(all(r['ok'] for r in results),results)
        self.assertEqual(maximum,1)

    def test_same_recorded_action_is_not_redispatched(self):
        lane=self.lane();frame=self.observe(lane)
        request=ActionRequest('computer_click',self.args(frame))
        self.assertEqual(lane.executor.execute(lane.ctx,request).outcome,'succeeded')
        self.assertEqual(lane.executor.execute(lane.ctx,request).outcome,'succeeded')
        self.assertEqual(len(self.driver.sent),1)

    def test_grounder_refuses_guessed_or_invalid_points(self):
        client=VisionClient.__new__(VisionClient)
        client.analyze=Mock()
        for point in ({'found':True,'x':True,'y':10,'label':'bad'},
                      {'found':True,'x':400,'y':10,'label':'bad'},
                      {'found':False,'x':10,'label':'absent'},'not JSON'):
            client.analyze.return_value={'text':json.dumps(point) if isinstance(point,dict) else point}
            with self.assertRaises(ValueError):client.locate(self.driver.screenshot(),'VERIFY',consent=True)
        client.analyze.return_value={'text':'{"found":true,"x":200,"y":150,"label":"VERIFY"}'}
        self.assertEqual(client.locate(self.driver.screenshot(),'VERIFY',consent=True)['point']['x'],200)

    def test_partial_native_input_only_releases_button_and_never_replays(self):
        driver=WindowsControlDriver.__new__(WindowsControlDriver)
        driver.window,driver.process_id=1,42
        driver.u=Mock()
        driver.u.SetThreadDpiAwarenessContext.return_value=123
        driver.u.WindowFromPoint.return_value=2
        driver.u.GetWindowThreadProcessId.side_effect=lambda hwnd,p:setattr(p._obj,'value',42)
        driver.u.GetAncestor.return_value=1
        driver.u.GetForegroundWindow.return_value=1
        driver.u.GetAsyncKeyState.return_value=0
        driver.u.GetGUIThreadInfo.return_value=1
        driver.u.GetSystemMetrics.side_effect=lambda index:{76:-1920,77:-1080,78:3840,79:2160}[index]
        driver.u.SendInput.side_effect=[2,1]
        prepared={'pixel_x':200,'pixel_y':150,'x':200,'y':150,'window':2}
        driver.prepare_coordinate=Mock(return_value=prepared)
        with self.assertRaises(RuntimeError): driver.coordinate_click(prepared,self.driver.screenshot())
        self.assertEqual([call.args[0] for call in driver.u.SendInput.call_args_list],[3,1])
        mouse=driver.u.SendInput.call_args_list[0].args[1][0].value.mouse
        self.assertEqual(mouse.dx,round((300+1920)*65535/3839))
        self.assertEqual(mouse.dy,round((250+1080)*65535/2159))

    def test_covered_or_user_held_input_never_dispatches(self):
        for kind in ('covered','new_child','modifier','capture'):
            driver=WindowsControlDriver.__new__(WindowsControlDriver)
            driver.window,driver.process_id=1,42;driver.u=Mock()
            driver.u.SetThreadDpiAwarenessContext.return_value=123
            driver.u.WindowFromPoint.return_value=3 if kind=='new_child' else 2
            driver.u.GetWindowThreadProcessId.side_effect=lambda hwnd,p:setattr(p._obj,'value',42)
            driver.u.GetAncestor.return_value=99 if kind=='covered' else 1
            driver.u.GetAsyncKeyState.return_value=0x8000 if kind=='modifier' else 0
            def info(thread,p):
                if kind=='capture':p._obj.capture=99
                return 1
            driver.u.GetGUIThreadInfo.side_effect=info
            prepared={'pixel_x':200,'pixel_y':150,'x':200,'y':150,'window':2}
            driver.prepare_coordinate=Mock(return_value=prepared)
            with self.assertRaises(CoordinateNotStarted):driver.coordinate_click(prepared,self.driver.screenshot())
            driver.u.SendInput.assert_not_called()


class OwnedWindow:
    def __init__(self,dpi):
        self.process=subprocess.Popen([sys.executable,'-u','-B',str(Path(__file__).with_name('fixture_coordinate_window.py')),dpi],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.output=queue.Queue()
        def read():
            for line in self.process.stdout:self.output.put(line)
        threading.Thread(target=read,daemon=True).start()
        self.target=json.loads(self.output.get(timeout=10))
    def command(self,op):
        self.process.stdin.write(json.dumps({'op':op}).encode()+b'\n');self.process.stdin.flush()
        return json.loads(self.output.get(timeout=10))
    def close(self):
        self.process.kill();self.process.communicate(timeout=5)


@unittest.skipUnless(os.name=='nt','Windows selected-window fixture')
class NativeCoordinateTests(unittest.TestCase):
    def test_real_custom_canvas_click_and_dpi_mapping(self):
        for dpi in ('aware','unaware'):
            with self.subTest(dpi=dpi),tempfile.TemporaryDirectory() as tmp:
                fixture=OwnedWindow(dpi)
                try:
                    driver=WindowsControlDriver(fixture.target['window'],fixture.target['process_id'])
                    computer=ComputerSession(driver)
                    scope=SecretScope()
                    try:
                        lane=ComputerLane(computer,rows=[],selection=None,environment={},consent=False,scope=scope,
                            cancel=threading.Event(),workspace=Path(tmp),home=Path(tmp)/'control',
                            policy=Policy(workspace=Path(tmp)),clicks=True,approve=lambda *args:bool(fixture.command('activate')))
                        observed=lane.call('computer_screenshot',{});self.assertTrue(observed['ok'],observed)
                        frame=json.loads(observed['content'])
                        with self.assertRaises(CoordinateNotStarted):driver.prepare_coordinate(frame,0,0)
                        args={'observation_id':frame['observation_id'],'x':fixture.target['x'],
                              'y':fixture.target['y'],'reason':'Click owned VERIFY button'}
                        clicked=lane.call('computer_click',args)
                        self.assertTrue(clicked['ok'],clicked)
                        self.assertEqual(fixture.command('state')['text'],'DONE')
                        self.assertTrue(clicked['meta']['changed'])
                        self.assertFalse(lane.call('computer_click',args)['ok'])
                        self.assertEqual(len(fixture.command('state')['clicks']),1)
                        frame=json.loads(lane.call('computer_screenshot',{})['content'])
                        fixture.command('move');args['observation_id']=frame['observation_id']
                        self.assertFalse(lane.call('computer_click',args)['ok'])
                    finally:scope.close()
                finally:fixture.close()


if __name__=='__main__':unittest.main()
