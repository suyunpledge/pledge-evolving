"""Observe/act contracts and real native controls in an owned hidden window."""
import copy
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from forge.gateway import GatewayConfig, serve
from forge.execution import ActionExecutor, ActionRequest, create_execution_context

from forge.computer_use import ComputerSession, register_computer_tools
from forge.computer_win32 import WindowsControlDriver
from forge.tools import ToolRegistry, ToolContext
from forge.policy import Policy, Mode


class Driver:
    target_id = 'fixture-window'
    def __init__(self):
        self.text, self.inputs, self.fail_after, self.fail_observe = '', [], False, False
    def observe(self):
        if self.fail_observe: raise TimeoutError('capture unavailable')
        return {'elements': [{'ref': 'edit', 'text': self.text, 'actions': ['type_text']}]}
    def act(self, ref, action, text):
        self.inputs.append(text); self.text = text
        if self.fail_after: raise TimeoutError('input receipt lost')


class ComputerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.driver = Driver()
        self.session = ComputerSession(self.driver, approve=lambda *a: True)
        self.registry = ToolRegistry(); register_computer_tools(self.registry)
        self.ctx = self.context()

    def context(self, mode=Mode.ACCEPT_EDITS, attached=True):
        ctx = ToolContext(Policy(workspace=Path(self.tmp.name), mode=mode, allow=('computer_observe',)), Path(self.tmp.name),
                          extras={'computer': self.session} if attached else {}, cancellation=threading.Event())
        self.addCleanup(ctx.secret_scope.close)
        return ctx

    def observe(self, ctx=None):
        result = self.registry.invoke('computer_observe', {}, ctx or self.ctx)
        self.assertTrue(result.ok, result.error)
        return json.loads(result.content)

    def act(self, observation, ctx=None, **extra):
        return self.registry.invoke('computer_act', {'observation_id': observation['observation_id'],
            'ref': 'edit', 'action': 'type_text', 'text': 'hello', **extra}, ctx or self.ctx)

    def test_observe_act_returns_fresh_state_and_consumes_ref(self):
        observation = self.observe()
        result = self.act(observation)
        self.assertTrue(result.ok, result.error)
        after = json.loads(result.content)
        self.assertNotEqual(after['observation_id'], observation['observation_id'])
        self.assertEqual(after['state']['elements'][0]['text'], 'hello')
        self.assertFalse(self.act(observation).ok)
        self.assertEqual(self.driver.inputs, ['hello'])

    def test_stale_state_and_expiry_do_not_send_input(self):
        observation = self.observe(); self.driver.text = 'external edit'
        self.assertEqual(self.act(observation).meta['error_code'], 'stale_observation')
        self.session.clock = lambda: 0
        observation = self.observe(); self.session.clock = lambda: 100
        self.assertFalse(self.act(observation).ok)
        self.assertFalse(self.driver.inputs)

    def test_approval_denial_and_change_during_approval(self):
        observation = self.observe(); self.session.approve = None
        self.assertEqual(self.act(observation).meta['error_code'], 'approval_required')
        observation = self.observe()
        def change(*args): self.driver.text = 'changed'; return True
        self.session.approve = change
        self.assertEqual(self.act(observation).meta['error_code'], 'stale_observation')
        self.assertFalse(self.driver.inputs)

    def test_policy_and_missing_host_never_send_input(self):
        for mode in (Mode.READ_ONLY, Mode.PLAN):
            ctx = self.context(mode)
            observation = self.observe(ctx)
            self.assertFalse(self.act(observation, ctx).ok)
        missing = self.context(attached=False)
        self.assertFalse(self.registry.invoke('computer_observe', {}, missing).ok)
        self.assertFalse(self.driver.inputs)

    def test_cancel_before_dispatch_and_after_dispatch(self):
        observation = self.observe(); self.ctx.cancellation.set()
        self.assertFalse(self.act(observation).ok)
        self.assertFalse(self.driver.inputs)
        self.ctx.cancellation.clear(); observation = self.observe()
        original = self.driver.act
        def cancelled(*args): original(*args); self.ctx.cancellation.set()
        self.driver.act = cancelled
        result = self.act(observation)
        self.assertEqual(result.meta['execution_state'], 'unknown')
        self.assertEqual(self.driver.inputs, ['hello'])

    def test_input_or_post_observation_failure_never_replays(self):
        for failure in ('input', 'capture'):
            with self.subTest(failure=failure):
                self.driver.fail_observe = False; self.driver.fail_after = False
                observation = self.observe()
                if failure == 'input': self.driver.fail_after = True
                else:
                    original = self.driver.act
                    def fail_capture(*args): original(*args); self.driver.fail_observe = True
                    self.driver.act = fail_capture
                result = self.act(observation)
                self.assertEqual(result.meta['execution_state'], 'unknown')
                count = len(self.driver.inputs)
                self.assertFalse(self.act(observation).ok)
                self.assertEqual(len(self.driver.inputs), count)

    def test_scope_isolation_and_other_lane_invalidated_after_input(self):
        other = self.context()
        one, two = self.observe(), self.observe(other)
        self.assertFalse(self.act(one, other).ok)
        two = self.observe(other)
        self.assertTrue(self.act(one).ok)
        self.assertFalse(self.act(two, other).ok)
        self.assertEqual(self.driver.inputs, ['hello'])

    def test_concurrent_reuse_has_only_one_input(self):
        observation = self.observe(); results = []
        workers = [threading.Thread(target=lambda: results.append(self.act(observation))) for _ in range(3)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual(self.driver.inputs, ['hello'])

    def test_bad_ref_and_action_are_not_guessed(self):
        for extra in ({'ref': 'unknown'}, {'action': 'shell'}, {'text': '\0'}):
            self.assertFalse(self.act(self.observe(), **extra).ok)
        self.assertFalse(self.driver.inputs)

    def test_revocation_during_approval_blocks_input(self):
        observation = self.observe()
        def revoke(*args): self.session.revoke(); return True
        self.session.approve = revoke
        self.assertEqual(self.act(observation).meta['error_code'], 'capability_revoked')
        self.assertFalse(self.registry.invoke('computer_observe', {}, self.ctx).ok)
        self.assertFalse(self.driver.inputs)

    def test_real_gateway_scope_and_capability_revocation(self):
        cfg = GatewayConfig(upstream='http://127.0.0.1:1', port=0,
            home=Path(self.tmp.name) / 'control', workspace=self.tmp.name,
            registry=self.registry, policy=self.ctx.policy, computer=self.session, gateway_token='test-key')
        server = serve(cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def call(name, args, run, scope='a'*32):
            req = urllib.request.Request('http://127.0.0.1:' + str(server.server_address[1]) + '/v1/tools/call',
                data=json.dumps({'name': name, 'arguments': args, 'run_id': run}).encode(),
                headers={'Authorization': 'Bearer test-key', 'Content-Type': 'application/json',
                         'X-Forge-Secret-Scope': scope})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as response:
                return json.loads(response.read())
        try:
            observation = json.loads(call('computer_observe', {}, 'observe-run')['content'])
            args = {'observation_id': observation['observation_id'], 'ref': 'edit', 'action': 'type_text', 'text': 'HTTP'}
            self.assertFalse(call('computer_act', args, 'other-run', 'b'*32)['ok'])
            result = call('computer_act', args, 'act-run')
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['meta']['action_id'])
            observation = json.loads(call('computer_observe', {}, 'observe-run')['content'])
            args['observation_id'] = observation['observation_id']
            cfg.computer = None
            revoked = call('computer_act', args, 'act-run')
            self.assertFalse(revoked['ok'])
            self.assertEqual(self.driver.inputs, ['HTTP'])
        finally:
            server.shutdown(); server.server_close(); thread.join(5)
            for ctx in cfg._contexts.values(): ctx.session.close()


@unittest.skipUnless(os.name == 'nt', 'Windows native controls')
class NativeControlTests(unittest.TestCase):
    def test_real_observe_type_click_and_stale_geometry(self):
        # A private hidden fixture: no global input, foreground changes or user apps.
        u = c.WinDLL('user32', use_last_error=True)
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
            c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        u.CreateWindowExW.restype = w.HWND
        u.DestroyWindow.argtypes = [w.HWND]
        parent = u.CreateWindowExW(0, 'Static', 'Forge private computer fixture', 0, 0,0,300,200,None,None,None,None)
        self.assertTrue(parent)
        try:
            edit = u.CreateWindowExW(0, 'Edit', '', 0x40000000, 10,10,180,25,parent,None,None,None)
            button = u.CreateWindowExW(0, 'Button', 'Toggle', 0x40000003, 10,50,150,25,parent,None,None,None)
            self.assertTrue(edit and button)
            driver = WindowsControlDriver(parent, os.getpid())
            with self.assertRaises(ValueError): WindowsControlDriver(parent, os.getpid()+1)
            state = driver.observe()
            self.assertEqual(len(state['elements']), 2)
            driver.act(str(edit), 'type_text', 'Forge 中文验证')
            driver.act(str(button), 'click')
            after = driver.observe()
            self.assertEqual(next(r['text'] for r in after['elements'] if r['ref'] == str(edit)), 'Forge 中文验证')
            self.assertEqual(next(r['checked'] for r in after['elements'] if r['ref'] == str(button)), 1)
            with tempfile.TemporaryDirectory() as tmp:
                registry = ToolRegistry(); register_computer_tools(registry)
                computer = ComputerSession(driver, approve=lambda *a: True)
                ctx = create_execution_context(home=Path(tmp)/'control', workspace=tmp, registry=registry,
                    policy=Policy(workspace=Path(tmp), mode=Mode.ACCEPT_EDITS, allow=('computer_observe',)),
                    capabilities={'computer': computer})
                try:
                    executor = ActionExecutor()
                    observed = executor.execute(ctx, ActionRequest('computer_observe', {}))
                    observation = json.loads(observed.content)
                    written = executor.execute(ctx, ActionRequest('computer_act', {
                        'observation_id': observation['observation_id'], 'ref': str(edit),
                        'action': 'type_text', 'text': 'Journal-linked 中文'}))
                    self.assertEqual(written.outcome, 'succeeded', written.error)
                    self.assertEqual(written.meta['verification'], 'post_observation')
                    self.assertEqual(next(r['text'] for r in json.loads(written.content)['state']['elements'] if r['ref'] == str(edit)), 'Journal-linked 中文')
                    self.assertEqual(sum(e.type == 'tool_completed' for e in ctx.session.replay()), 2)
                finally: ctx.session.close(); ctx.secret_scope.close()
        finally: u.DestroyWindow(parent)
        with self.assertRaises(ValueError): driver.observe()


if __name__ == '__main__': unittest.main()
