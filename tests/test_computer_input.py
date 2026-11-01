"""Approved keyboard input, pointer variants and partial observation. No real input is sent."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'forge-gui'))
from computer_panel import ComputerLane, NAMES
from click_approval import describe
from forge.computer_use import ComputerSession
from forge.computer_win32 import (WindowsControlDriver, CoordinateNotStarted, keyboard_events, parse_key)
from forge.policy import Policy, Mode
from forge.secrets import SecretScope

FRAME={'rect':[100,100,500,400],'client_rect':[110,130,490,390],'dpi':144}


class Driver:
    target_id='fixture:input'
    def __init__(self):
        self.hash='before';self.rect=[100,100,500,400]
        self.sent=[];self.keys=[];self.fail=None;self.activated=0
    def screenshot(self,**kwargs):
        return {'target':self.target_id,'png':b'fixture','width':400,'height':300,
            'rect':self.rect[:],'client_rect':[110,130,490,390],'dpi':144,
            'client_sha256':self.hash,'sha256':self.hash}
    def prepare_coordinate(self,frame,x,y): return {'x':x,'y':y}
    def coordinate_click(self,prepared,frame,**kwargs):
        self.sent.append(kwargs.get('action','click'));self.hash+='+click'
    def activate_for_keyboard(self,**kwargs): self.activated+=1
    def keyboard_input(self,frame,events,**kwargs):
        if self.fail: raise self.fail
        self.keys.append(len(events));self.hash+='+keys'


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.driver=Driver();self.computer=ComputerSession(self.driver)
        self.scope=SecretScope();self.addCleanup(self.scope.close)
        self.approve=Mock(return_value=True)

    def lane(self,clicks=True,**policy):
        return ComputerLane(self.computer,rows=[],selection=None,environment={},consent=False,
            scope=self.scope,cancel=threading.Event(),workspace=Path(self.tmp.name),home=Path(self.tmp.name)/'control',
            policy=Policy(workspace=Path(self.tmp.name),**policy),clicks=clicks,approve=self.approve)

    def shot(self,lane):
        result=lane.call('computer_screenshot',{});self.assertTrue(result['ok'],result)
        return json.loads(result['content'])['observation_id']


class KeyboardSessionTests(LaneCase):
    def test_type_then_enter_chains_on_post_input_observations(self):
        lane=self.lane();oid=self.shot(lane)
        typed=lane.call('computer_type',{'observation_id':oid,'text':'你好 VERIFY','reason':'fill search'})
        self.assertTrue(typed['ok'],typed)
        self.assertEqual(typed['meta']['verification'],'post_screenshot');self.assertTrue(typed['meta']['changed'])
        shown=self.approve.call_args.args[1]
        self.assertEqual(shown['text'],'你好 VERIFY')
        self.assertEqual(self.driver.keys,[2*len('你好 VERIFY')])
        nxt=json.loads(typed['content'])['observation_id']
        self.assertNotEqual(nxt,oid)
        pressed=lane.call('computer_key',{'observation_id':nxt,'key':'Enter','reason':'submit'})
        self.assertTrue(pressed['ok'],pressed)
        self.assertEqual(self.approve.call_args.args[1]['key'],'enter')
        self.assertEqual(self.driver.activated,2)
        self.assertFalse(lane.call('computer_type',{'observation_id':oid,'text':'again','reason':'r'})['ok'])
        self.assertEqual(len(self.driver.keys),2)

    def test_caret_blink_is_tolerated_but_moved_window_is_not(self):
        lane=self.lane();oid=self.shot(lane)
        self.driver.hash='caret-off'
        self.assertTrue(lane.call('computer_key',{'observation_id':oid,'key':'ctrl+a','reason':'r'})['ok'])
        oid=self.shot(lane);self.approve.side_effect=lambda *a:setattr(self.driver,'rect',[0,0,400,300]) or True
        moved=lane.call('computer_type',{'observation_id':oid,'text':'x','reason':'r'})
        self.assertFalse(moved['ok']);self.assertEqual(moved['meta']['execution_state'],'not_started')
        self.assertEqual(self.driver.keys,[4])

    def test_invalid_blocked_denied_and_readonly_never_reach_approval_or_input(self):
        lane=self.lane()
        for args in ({'text':''},{'text':'x'*2001},{'text':'a\x07b'},{'key':'q'},{'key':'alt+f4'},
                     {'key':'alt+tab'},{'key':'ctrl+w'},{'key':'win+r'},{'key':'ctrl+ctrl+s'},
                     {'key':'enter','repeat':0},{'key':'enter','repeat':21},{'key':'enter','extra':1},
                     {'key':'enter','reason':''}):
            oid=self.shot(lane)
            name='computer_type' if 'text' in args else 'computer_key'
            result=lane.call(name,{'observation_id':oid,'reason':'r',**args})
            self.assertFalse(result['ok'],args)
        self.approve.assert_not_called()
        for mode in (Mode.READ_ONLY,Mode.PLAN):
            lane=self.lane(mode=mode);oid=self.shot(lane)
            self.assertFalse(lane.call('computer_key',{'observation_id':oid,'key':'enter','reason':'r'})['ok'])
        self.approve.return_value=False
        lane=self.lane();oid=self.shot(lane)
        denied=lane.call('computer_type',{'observation_id':oid,'text':'secret plan','reason':'r'})
        self.assertEqual(denied['meta']['error_code'],'approval_required')
        self.assertFalse(self.driver.keys)

    def test_driver_refusal_is_not_started_and_lost_receipt_is_unknown(self):
        lane=self.lane()
        for error,state in ((CoordinateNotStarted('not in front'),'not_started'),(TimeoutError('lost'),'unknown')):
            self.driver.fail=error;oid=self.shot(lane)
            result=lane.call('computer_key',{'observation_id':oid,'key':'ctrl+s','reason':'save'})
            self.assertFalse(result['ok']);self.assertEqual(result['meta']['execution_state'],state)

    def test_pointer_actions_reach_driver_and_amount_is_scroll_only(self):
        lane=self.lane()
        for extra in ({'action':'double_click'},{'action':'right_click'},{'action':'scroll_down','amount':5},{}):
            oid=self.shot(lane)
            self.assertTrue(lane.call('computer_click',{'observation_id':oid,'x':20,'y':20,'reason':'r',**extra})['ok'],extra)
        self.assertEqual(self.driver.sent,['double_click','right_click','scroll_down','click'])
        for extra in ({'action':'click','amount':2},{'action':'scroll_up','amount':11},{'action':'drag'}):
            oid=self.shot(lane)
            self.assertFalse(lane.call('computer_click',{'observation_id':oid,'x':20,'y':20,'reason':'r',**extra})['ok'])
        self.assertEqual(len(self.driver.sent),4)

    def test_input_tools_are_opt_in_and_reserved(self):
        self.assertTrue({'computer_click','computer_type','computer_key'} <= NAMES)
        names=lambda lane:{s['function']['name'] for s in lane.schemas()}
        self.assertTrue({'computer_type','computer_key'} <= names(self.lane()))
        self.assertFalse({'computer_click','computer_type','computer_key'} & names(self.lane(clicks=False)))


class KeyEncodingTests(unittest.TestCase):
    def test_text_is_unicode_units_with_enter_for_newlines(self):
        events=keyboard_events(text='a😀\r\nb')
        keys=[(e.type,e.value.key.vk,e.value.key.scan,e.value.key.flags) for e in events]
        self.assertEqual(keys[0],(1,0,ord('a'),4));self.assertEqual(keys[1],(1,0,ord('a'),6))
        self.assertEqual([k[2] for k in keys[2:6:2]],[0xD83D,0xDE00])  # surrogate pair
        self.assertEqual(keys[6][1:],(0x0D,0,0));self.assertEqual(keys[7][1:],(0x0D,0,2))
        self.assertEqual(len(events),10)

    def test_combos_are_canonical_whitelisted_and_extended(self):
        self.assertEqual(parse_key('Shift+Ctrl+S')[0],'ctrl+shift+s')
        self.assertEqual(parse_key('ESCAPE')[0],'esc')
        events=keyboard_events(key='ctrl+shift+tab',repeat=2)
        self.assertEqual([(e.value.key.vk,e.value.key.flags) for e in events[:6]],
                         [(0x11,0),(0x10,0),(0x09,0),(0x09,2),(0x10,2),(0x11,2)])
        self.assertEqual(len(events),12)
        down=keyboard_events(key='down')
        self.assertEqual([e.value.key.flags for e in down],[1,3])
        for bad in ('a','alt+F4','ctrl+esc','win','ctrl+alt+delete','shift+',''):
            with self.assertRaises(ValueError): parse_key(bad)


def native(**overrides):
    driver=WindowsControlDriver.__new__(WindowsControlDriver)
    driver.window,driver.process_id=1,42;driver.u=Mock()
    driver.u.SetThreadDpiAwarenessContext.return_value=123
    driver.u.WindowFromPoint.return_value=2
    driver.u.GetWindowThreadProcessId.side_effect=lambda hwnd,p:setattr(p._obj,'value',overrides.get('pid',42))
    driver.u.GetAncestor.return_value=1
    driver.u.GetForegroundWindow.return_value=overrides.get('foreground',1)
    driver.u.GetAsyncKeyState.return_value=overrides.get('held',0)
    driver.u.GetGUIThreadInfo.return_value=1
    driver.u.GetSystemMetrics.side_effect=lambda index:{76:0,77:0,78:1920,79:1080}[index]
    driver.geometry=Mock(return_value=dict(FRAME))
    return driver


class NativeInputTests(unittest.TestCase):
    prepared={'pixel_x':200,'pixel_y':150,'x':200,'y':150,'window':2}

    def flags(self,driver,call=0):
        batch=driver.u.SendInput.call_args_list[call].args
        return [batch[1][i].value.mouse.flags for i in range(batch[0])]

    def test_double_right_and_scroll_sequences(self):
        for action,expected in (('double_click',[0xC001,2,4,2,4]),('right_click',[0xC001,8,16]),
                                ('scroll_down',[0xC001,0x0800])):
            driver=native();driver.prepare_coordinate=Mock(return_value=self.prepared)
            driver.u.SendInput.side_effect=lambda n,*a:n
            driver.coordinate_click(self.prepared,FRAME,action=action,amount=4)
            self.assertEqual(self.flags(driver),expected)
            if action=='scroll_down':
                data=driver.u.SendInput.call_args.args[1][1].value.mouse.data
                self.assertEqual(data,(-4*120)&0xFFFFFFFF)

    def test_partial_right_click_releases_only_the_right_button(self):
        driver=native();driver.prepare_coordinate=Mock(return_value=self.prepared)
        driver.u.SendInput.side_effect=[2,1]
        with self.assertRaises(RuntimeError): driver.coordinate_click(self.prepared,FRAME,action='right_click')
        self.assertEqual(driver.u.SendInput.call_args_list[1].args[1]._obj.value.mouse.flags,16)

    def test_keyboard_requires_target_in_front_and_idle_user(self):
        for kind in ('background','foreign','held','moved'):
            driver=native(foreground=7 if kind=='background' else 1,pid=99 if kind=='foreign' else 42,
                          held=0x8000 if kind=='held' else 0)
            if kind=='moved': driver.geometry.return_value={**FRAME,'dpi':96}
            with self.assertRaises(CoordinateNotStarted,msg=kind): driver.keyboard_input(FRAME,keyboard_events(key='enter'))
            driver.u.SendInput.assert_not_called()

    def test_keyboard_menu_mode_is_allowed_and_partial_input_releases_modifiers(self):
        driver=native()
        def info(thread,p): p._obj.menu=5; return 1
        driver.u.GetGUIThreadInfo.side_effect=info
        driver.u.SendInput.side_effect=lambda n,*a:n
        driver.keyboard_input(FRAME,keyboard_events(key='down'))
        driver.u.SendInput.side_effect=[2,1,1]  # ctrl down + s down inserted, then lost
        with self.assertRaises(RuntimeError): driver.keyboard_input(FRAME,keyboard_events(key='ctrl+s'))
        released=[driver.u.SendInput.call_args_list[i].args[1]._obj.value.key for i in (2,3)]
        self.assertEqual([(k.vk,k.flags) for k in released],[(ord('S'),2),(0x11,2)])

    def test_observe_is_partial_instead_of_failing(self):
        driver=native();driver._validate=lambda:None;driver.callback_type=lambda f:f
        owners={h:(99 if h%10==0 else 42) for h in range(2,90)}
        driver.u.GetWindowThreadProcessId.side_effect=lambda hwnd,p:setattr(p._obj,'value',owners.get(hwnd,42))
        def element(h):
            if h==5: raise ValueError('text too long')
            return {'ref':str(h),'actions':[]}
        driver._element=element
        def enum(parent,callback,lp):
            for h in range(2,90):
                if not callback(h,0): break
        driver.u.EnumChildWindows.side_effect=enum
        state=driver.observe()
        self.assertEqual(len(state['elements']),64);self.assertTrue(state['truncated'])
        self.assertEqual(state['skipped']['unreadable'],1);self.assertGreater(state['skipped']['foreign_process'],0)
        self.assertNotIn('10',[e['ref'] for e in state['elements']])


class ObserveHintTests(unittest.TestCase):
    def test_empty_native_list_points_to_screenshot_tools(self):
        class Empty:
            target_id='fixture:empty'
            def observe(self): return {'window':{},'elements':[],'truncated':False,'skipped':{}}
        scope=SecretScope();self.addCleanup(scope.close)
        ctx=Mock(secret_scope=scope,cancellation=None)
        result=ComputerSession(Empty()).observe(ctx)
        self.assertTrue(result.ok,result.error)
        self.assertIn('computer_screenshot',json.loads(result.content)['hint'])


class ApprovalTextTests(unittest.TestCase):
    def test_prompts_name_the_exact_input(self):
        self.assertEqual(describe({'x':1,'y':2,'reason':'r'})[2],'确认单击')
        self.assertIn('双击',describe({'x':1,'y':2,'action':'double_click'})[1])
        self.assertIn('5 格',describe({'x':1,'y':2,'action':'scroll_up','amount':5})[1])
        self.assertIn('回车',describe({'text':'a\nb'})[1])
        self.assertNotIn('回车',describe({'text':'ab'})[1])
        self.assertEqual(describe({'key':'ctrl+shift+s','repeat':3})[1],'按键 Ctrl+Shift+S × 3')


@unittest.skipUnless(sys.platform=='win32','Windows selected-window fixture')
class NativeKeyboardTests(unittest.TestCase):
    def test_real_unicode_typing_and_enter_reach_only_the_owned_window(self):
        from test_computer_coordinates import OwnedWindow
        fixture=OwnedWindow('keyboard',script='fixture_keyboard_window.py')
        scope=SecretScope()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                computer=ComputerSession(WindowsControlDriver(fixture.target['window'],fixture.target['process_id']))
                lane=ComputerLane(computer,rows=[],selection=None,environment={},consent=False,scope=scope,
                    cancel=threading.Event(),workspace=Path(tmp),home=Path(tmp)/'control',policy=Policy(workspace=Path(tmp)),
                    clicks=True,approve=lambda *args:bool(fixture.command('focus')))
                oid=json.loads(lane.call('computer_screenshot',{})['content'])['observation_id']
                typed=lane.call('computer_type',{'observation_id':oid,'text':'Hi 你好😀','reason':'type into owned entry'})
                self.assertTrue(typed['ok'],typed)
                pressed=lane.call('computer_key',{'observation_id':json.loads(typed['content'])['observation_id'],
                                                  'key':'enter','reason':'submit owned entry'})
                self.assertTrue(pressed['ok'],pressed)
                state=fixture.command('state')
                self.assertEqual(state['typed'],'Hi 你好😀')
                self.assertEqual(state['submitted'],['Hi 你好😀'])
        finally:
            scope.close();fixture.close()


if __name__=='__main__':unittest.main()
