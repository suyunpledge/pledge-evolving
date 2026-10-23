"""Selected-window capture, consent and GUI lifecycle. No user windows/uploads."""
import base64
import ctypes
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import _tkinter
import unittest
from unittest.mock import Mock, patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'forge-gui'))
from computer_panel import ComputerLane, ComputerPanel
from phase_client import VisionClient
from forge.computer_capture import _chunk, capture_window
from forge.computer_use import ComputerSession
from forge.computer_win32 import WindowsControlDriver
from forge.model import Completion, Usage
from forge.policy import Policy, Mode, Sandbox
from forge.secrets import SecretScope


def fixture_png():
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', struct.pack('>IIBBBBB',2,1,8,6,0,0,0)) +
            _chunk(b'IDAT', zlib.compress(b'\0\xff\0\0\xff\0\xff\0\xff')) + _chunk(b'IEND',b''))


ROWS = [{'id':'vision', 'name':'provider:vision', 'config': {'baseURL':'https://ark.cn-beijing.volces.com/api/coding/v3',
    'wire':'openai', 'model':'doubao-seed-evolving', 'apiKey':'fixture-key-not-a-real-secret'}}]


class FakeDriver:
    target_id = 'fixture:target'
    def __init__(self): self.captures = 0
    def screenshot(self, **kwargs):
        self.captures += 1
        return {'png':fixture_png(), 'width':2, 'height':1, 'sha256':'fixture', 'target':self.target_id}
    def observe(self): return {'elements': []}


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.scope = SecretScope(); self.addCleanup(self.scope.close)
        self.cancel = threading.Event()
        self.driver = FakeDriver(); self.session = ComputerSession(self.driver)

    def lane(self, consent=True, **policy):
        return ComputerLane(self.session, rows=ROWS, selection=('vision','doubao-seed-evolving'), environment={},
            consent=consent, scope=self.scope, cancel=self.cancel, workspace=Path(self.tmp.name),
            home=Path(self.tmp.name)/'control', policy=Policy(workspace=Path(self.tmp.name), **policy))

    def test_local_capture_has_receipt_and_no_pixels_in_journal(self):
        lane = self.lane(False, mode=Mode.READ_ONLY, sandbox=Sandbox.READ_ONLY)
        self.assertNotIn('computer_vision', [s['function']['name'] for s in lane.schemas()])
        self.assertNotIn('computer_act', [s['function']['name'] for s in lane.schemas()])
        result = lane.call('computer_screenshot', {})
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['meta']['action_id']); self.assertTrue(result['meta']['run_id'])
        self.assertNotIn('png', json.loads(result['content']))
        journal = lane.ctx.session.path.read_text(encoding='utf-8')
        self.assertNotIn(base64.b64encode(fixture_png()).decode(), journal)

    def test_without_consent_no_capture_or_network(self):
        lane = self.lane(False)
        with patch('computer_panel.VisionClient') as client:
            result = lane.call('computer_vision', {'question':'describe'})
        self.assertFalse(result['ok']); client.assert_not_called()
        self.assertEqual(self.driver.captures, 0)

    def test_existing_policy_denial_is_preserved(self):
        lane = self.lane(deny=('computer_*',))
        result = lane.call('computer_screenshot', {})
        self.assertFalse(result['ok']); self.assertEqual(self.driver.captures, 0)

    def test_revocation_and_cancel_prevent_capture(self):
        for revoke in (False,True):
            self.cancel.clear(); self.session = ComputerSession(self.driver)
            lane = self.lane()
            if revoke: self.session.revoke()
            else: self.cancel.set()
            self.assertFalse(lane.call('computer_screenshot', {})['ok'])
        self.assertEqual(self.driver.captures, 0)

    def test_invalid_question_does_not_capture_or_request(self):
        for question in (None, [], '', ' '*3, 'x'*4001):
            with patch('computer_panel.VisionClient') as client:
                result=self.lane().call('computer_vision', {'question':question})
            self.assertFalse(result['ok']); client.assert_not_called()
        self.assertEqual(self.driver.captures,0)

    def test_concurrent_lanes_keep_target_and_receipt_isolated(self):
        first=self.lane(False)
        other_driver=FakeDriver(); other_driver.target_id='fixture:other'
        self.session=ComputerSession(other_driver)
        second=self.lane(False)
        results=[]
        workers=[threading.Thread(target=lambda lane=lane:results.append(lane.call('computer_screenshot',{})))
                 for lane in (first,second)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertEqual(len(results),2)
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual({json.loads(r['content'])['target'] for r in results},{'fixture:target','fixture:other'})
        self.assertEqual(len({r['meta']['run_id'] for r in results}),2)

    def test_revoke_after_capture_prevents_upload(self):
        original = self.driver.screenshot
        def revoke(**kwargs):
            frame = original(**kwargs); self.session.revoke(); return frame
        self.driver.screenshot = revoke
        with patch('computer_panel.VisionClient') as client:
            result = self.lane().call('computer_vision', {'question':'describe'})
        self.assertFalse(result['ok']); client.return_value.analyze.assert_not_called()

    def test_vision_result_has_image_identity_without_pixels(self):
        with patch('computer_panel.VisionClient') as client:
            client.return_value.analyze.return_value = {'text':'Red button', 'model':'fixture', 'usage':{}}
            lane = self.lane()
            result = lane.call('computer_vision', {'question':'describe'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(json.loads(result['content'])['image']['target'], self.driver.target_id)
        self.assertEqual(self.driver.captures, 1)
        self.assertNotIn('data:image', lane.ctx.session.path.read_text(encoding='utf-8'))

    def test_vision_wire_image_and_pinned_provider(self):
        for wire in ('openai','anthropic'):
            rows = json.loads(json.dumps(ROWS)); rows[0]['config']['wire'] = wire
            client = VisionClient(rows, ('vision','doubao-seed-evolving'), {}, self.scope, self.cancel)
            self.assertEqual(client.router.primary, ('vision','doubao-seed-evolving'))
            client.router.complete = Mock(return_value=Completion('visible',Usage(model='fixture')))
            result = client.analyze({'png':fixture_png()}, 'describe', consent=True)
            self.assertEqual(result['text'],'visible')
            content = client.router.complete.call_args.args[0][1]['content']
            image = next(c for c in content if c['type'] != 'text')
            encoded = image['source']['data'] if wire == 'anthropic' else image['image_url']['url'].split(',')[1]
            self.assertEqual(base64.b64decode(encoded),fixture_png())
            client.router.complete.reset_mock()
            with self.assertRaises(PermissionError): client.analyze({'png':fixture_png()},'describe')
            client.router.complete.assert_not_called()

    def test_vision_no_standard_billing_fallback_or_empty_result(self):
        rows = json.loads(json.dumps(ROWS)); rows[0]['config']['baseURL'] = 'https://ark.cn-beijing.volces.com/api/v3'
        with self.assertRaises(ValueError): VisionClient(rows,('vision','doubao-seed-evolving'),{},self.scope)
        client = VisionClient(ROWS,('vision','doubao-seed-evolving'),{},self.scope,self.cancel)
        client.router.complete = Mock(return_value=Completion('',Usage()))
        with self.assertRaises(ValueError): client.analyze({'png':fixture_png()},'describe',consent=True)
        self.cancel.set(); client.router.complete.reset_mock()
        with self.assertRaises(Exception): client.analyze({'png':fixture_png()},'describe',consent=True)
        client.router.complete.assert_not_called()

    def test_real_http_adapter_preserves_image_payload_for_both_wires(self):
        for wire in ('openai','anthropic'):
            received=[]
            @contextmanager
            def response(request, **kwargs):
                received.append((request.full_url,json.loads(request.data)))
                data = {'content':[{'type':'text','text':'visible'}]} if wire == 'anthropic' else {
                    'choices':[{'message':{'role':'assistant','content':'visible'}}]}
                yield io.BytesIO(json.dumps(data).encode())
            rows=json.loads(json.dumps(ROWS)); rows[0]['config']['wire']=wire
            if wire == 'anthropic': rows[0]['config']['baseURL']='https://ark.cn-beijing.volces.com/api/coding'
            with patch('phase_client.open_response', response):
                client=VisionClient(rows,('vision','doubao-seed-evolving'),{},self.scope,self.cancel)
                result=client.analyze({'png':fixture_png()},'describe',consent=True)
            self.assertEqual(result['text'],'visible')
            self.assertEqual(len(received),1)
            url,payload=received[0]
            self.assertIn('/api/coding',url)
            self.assertEqual(payload['model'],'doubao-seed-evolving')
            content=next(m['content'] for m in payload['messages'] if m['role']=='user')
            image=next(c for c in content if c['type']!='text')
            encoded=image['source']['data'] if wire=='anthropic' else image['image_url']['url'].split(',')[1]
            self.assertEqual(base64.b64decode(encoded),fixture_png())


@unittest.skipUnless(os.name == 'nt', 'Windows owned fixture')
class WindowTests(unittest.TestCase):
    def setUp(self):
        self.isolated = (self._testMethodName == 'test_real_selected_window_png_and_closed_target'
                         and os.environ.get('FORGE_NATIVE_CAPTURE_FIXTURE') != '1')
        if self.isolated: return
        self.root = tk.Tk(); self.root.title('Forge owned screenshot fixture')
        self.root.geometry('420x220+20+20')
        tk.Label(self.root, text='FORGE VISION TEST 4729',bg='#ff0000',fg='white',font=('Arial',18)).pack(fill='both',expand=True)
        self.root.update()
        self.addCleanup(self.root.destroy)

    def pump(self, done, timeout=10):
        deadline = time.monotonic()+timeout
        while not done() and time.monotonic()<deadline:
            # One mainloop-style dispatch, not update() draining an external
            # PrintWindow paint queue indefinitely in a multi-suite process.
            self.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT)
            time.sleep(.01)
        self.assertTrue(done(), 'worker did not complete')

    def test_real_selected_window_png_and_closed_target(self):
        if self.isolated:
            # Keep this real Win32/Tk paint loop separate from prior native-control
            # fixtures and their thread message state. Timeout is a test failure.
            result = subprocess.run([sys.executable,'-B','-c',
                "import sys,unittest; sys.path.insert(0,'tests'); "
                "unittest.main(module='test_computer_vision',argv=['fixture','WindowTests.test_real_selected_window_png_and_closed_target','-v'])"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ,'FORGE_NATIVE_CAPTURE_FIXTURE':'1'}, capture_output=True,
                timeout=20, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            return
        user32 = ctypes.WinDLL('user32')
        user32.GetParent.argtypes = [ctypes.c_void_p]; user32.GetParent.restype = ctypes.c_void_p
        hwnd = user32.GetParent(self.root.winfo_id())
        result, errors = [], []
        def capture():
            try: result.append(capture_window(hwnd,os.getpid()))
            except Exception as exc: errors.append(exc)
        worker=threading.Thread(target=capture); worker.start()
        self.pump(lambda:not worker.is_alive())
        self.assertFalse(errors, errors)
        frame=result[0]; self.assertGreater(frame['width'],400)
        photo=tk.PhotoImage(master=self.root,data=frame['png'])
        self.assertEqual(photo.width(),frame['width'])
        self.assertEqual(photo.get(100,100),(255,0,0))
        with self.assertRaises(RuntimeError): capture_window(hwnd,os.getpid()+1)
        cancel=threading.Event(); cancel.set()
        with self.assertRaises(RuntimeError): capture_window(hwnd,os.getpid(),cancelled=cancel)
        with self.assertRaises(TimeoutError): capture_window(hwnd,os.getpid(),timeout=0)

    def test_picker_bind_preview_revoke_and_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks=queue.Queue(); scope=SecretScope(); self.addCleanup(scope.close)
            app=Mock(root=self.root,home=Path(tmp),user_rows=json.loads(json.dumps(ROWS)))
            app._post_ui=lambda f,*args:tasks.put((f,args))
            app._input_secret_scope=lambda:scope
            app._active_workspace=lambda:Path(tmp)
            def drain():
                while not tasks.empty():
                    f,args=tasks.get(); f(*args)
                return not panel.busy
            with patch('computer_panel.list_windows', return_value=[{'window':123,'process_id':456,'title':'Owned fixture'}]), \
                 patch('computer_panel.WindowsControlDriver',return_value=FakeDriver()):
                panel=ComputerPanel(app)
                try:
                    self.pump(drain)
                    panel.tree.selection_set('0'); panel.bind()
                    self.assertIsNotNone(panel.session)
                    panel.preview(); self.pump(drain)
                    self.assertIsNotNone(panel.frame)
                    old=panel.session
                    panel.consent.set(True)
                    self.assertTrue(old._revoked.is_set())
                    app.user_rows[0]['config']['baseURL']='https://changed.invalid/v1'
                    lane=panel.manual_lane()
                    self.assertFalse(lane.consent)
                    self.assertFalse(panel.consent.get())
                    panel.consent.set(True)
                    panel.model.set('different provider')
                    self.assertFalse(panel.consent.get())
                    panel.revoke()
                    self.assertIsNone(panel.frame); self.assertIsNone(panel.session)
                    self.assertFalse(panel.consent.get())
                finally: panel.close()
                self.assertTrue(panel.stop.is_set())

    def test_close_during_capture_drops_result_without_tk_worker_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope=SecretScope(); self.addCleanup(scope.close)
            app=Mock(root=self.root,home=Path(tmp),user_rows=ROWS)
            app._input_secret_scope=lambda:scope
            app._active_workspace=lambda:Path(tmp)
            started, release, ended=threading.Event(),threading.Event(),threading.Event()
            driver=FakeDriver()
            def slow(**kwargs):
                started.set(); release.wait(3)
                try: return FakeDriver.screenshot(driver,**kwargs)
                finally: ended.set()
            driver.screenshot=slow
            with patch('computer_panel.list_windows',return_value=[]):
                panel=ComputerPanel(app)
                self.pump(lambda:not panel.busy)
                panel.session=ComputerSession(driver)
                panel.preview(); self.pump(started.is_set)
                session=panel.session
                panel.close(); release.set(); self.pump(ended.is_set)
                self.pump(lambda:not panel.worker.is_alive())
                self.assertTrue(session.revoked.is_set())
                self.assertIsNone(panel.poll_after)
                app._post_ui.assert_not_called()


if __name__ == '__main__': unittest.main()
