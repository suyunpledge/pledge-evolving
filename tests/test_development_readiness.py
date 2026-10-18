"""Host model projection, admission diagnostics and initialization recovery."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.config import ConfigError
from forge.development.models import ModelSettingsSource
from forge.development.runtime import DevelopmentRuntime
from forge.development.state import ModeState
from forge.model import Usage
from forge.secrets import redact


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home, self.host, self.project = [self.base / s for s in ('coding', 'work', 'project')]
        for p in (self.home, self.host, self.project): p.mkdir()
        self.key = 'sk-synthetic-readiness-key-0123456789'
        self.rows = [
            {'id': 'writer', 'name': 'provider:writer', 'config': {'baseURL': 'https://example.invalid/v1',
                'model': 'code-one', 'apiKey': {'$expr': "get('env.FORGE_WRITER_KEY', '')"}}},
            {'id': 'model', 'name': 'model:router', 'config': {'primary': ['writer', 'code-one']}},
            {'id': 'policy', 'name': 'policy:core', 'config': {'mode': 'bypassPermissions', 'sandbox': 'danger-full-access'}},
            {'id': 'tools', 'name': 'tools:core', 'config': {'expose': ['shell_exec']}},
        ]
        self.settings(self.rows)
        (self.host/'secrets.json').write_text(json.dumps({'writer': self.key, 'connector.private': 'never-import-this-key'}))
        self.source = ModelSettingsSource(self.host)
        self.runtime = DevelopmentRuntime(ModeState(self.home), model_router_factory=self.source)
        self.runtime.state.switch('development')
        self.addCleanup(self.runtime.close)

    def settings(self, rows, home=None):
        ((home or self.host)/'forge.patch.json').write_text(json.dumps(rows), encoding='utf-8')

    def test_host_projection_resolves_key_without_env_disk_or_authority_sharing(self):
        before = dict(os.environ)
        files = {p:p.read_bytes() for p in self.host.iterdir()}
        ready = self.runtime.initialize(self.project)
        self.assertEqual(os.environ, before)
        self.assertEqual(self.runtime.catalog, {json.dumps(['writer','code-one']):('writer','code-one')})
        self.assertNotIn(self.key, json.dumps(redact(ready)))
        self.assertTrue(self.runtime.bridge.router.providers['writer'].api_key)
        self.assertEqual(self.runtime.bridge.agent.policy.mode.value, 'read-only')
        self.assertNotEqual(self.runtime.bridge.agent.policy.sandbox.value, 'danger-full-access')
        self.assertFalse((self.home/'forge.patch.json').exists())
        self.assertFalse((self.home/'secrets.json').exists())
        self.assertTrue(self.runtime.bridge.agent.session.path.resolve().is_relative_to(self.home.resolve()))
        self.assertEqual({p:p.read_bytes() for p in self.host.iterdir()}, files)

    def test_own_coding_models_take_precedence(self):
        self.settings([{'id':'local','name':'provider:local','config':{'baseURL':'http://127.0.0.1:9000/v1','model':'private'}},
            {'id':'model','name':'model:router','config':{'primary':['local','private']}}],self.home)
        self.runtime.initialize(self.project)
        self.assertIn('local', self.runtime.bridge.router.providers)
        self.assertNotIn('writer', self.runtime.bridge.router.providers)
        self.assertTrue(self.runtime.readiness()['ready'])

    def test_empty_host_is_model_missing_instead_of_fake_sample_catalog(self):
        (self.host/'forge.patch.json').unlink()
        self.runtime.initialize(self.project)
        self.assertEqual(self.runtime.catalog,{})
        self.assertEqual(self.runtime.readiness()['code'],'model_missing')

    def test_legacy_unnamed_desktop_provider_rows_are_accepted(self):
        self.settings([{'id':'writer','config':self.rows[0]['config']}])
        self.runtime.initialize(self.project)
        self.assertIn('writer',self.runtime.bridge.router.providers)
        self.assertTrue(self.runtime.readiness()['ready'])

    def test_borrowed_custom_router_keeps_its_own_transport_contract(self):
        from forge.test_vscode_bridge import FakeRouter
        from forge.development.models import model_readiness
        self.assertTrue(model_readiness(FakeRouter())['ready'])

    def test_missing_cloud_key_is_diagnosed_before_model_request(self):
        (self.host/'secrets.json').unlink()
        with patch.dict(os.environ, {'FORGE_WRITER_KEY':''}):
            self.runtime.initialize(self.project)
        self.assertEqual(self.runtime.readiness()['code'],'credential_missing')

    def test_refresh_models_keeps_session_policy_checkpoint_and_history(self):
        self.runtime.initialize(self.project)
        bridge = self.runtime.bridge
        session, policy, checkpoints = bridge.agent.session, bridge.agent.policy, bridge.agent.checkpoints
        bridge.history.append({'role':'user','content':'kept'})
        self.rows[0]['config']['model'] = 'code-two-longer'
        self.rows[1]['config']['primary'] = ['writer','code-two-longer']
        self.settings(self.rows)
        self.assertIsNotNone(self.runtime.refresh_models())
        self.assertEqual(bridge.router.primary,('writer','code-two-longer'))
        self.assertIs(bridge.agent.session,session)
        self.assertIs(bridge.agent.policy,policy)
        self.assertIs(bridge.agent.checkpoints,checkpoints)
        self.assertEqual(bridge.history,[{'role':'user','content':'kept'}])
        self.assertIsNone(self.runtime.refresh_models())

    def test_bad_refresh_is_atomic_and_can_recover(self):
        self.runtime.initialize(self.project)
        bridge, catalog = self.runtime.bridge, dict(self.runtime.catalog)
        router = bridge.router
        (self.host/'forge.patch.json').write_text('{broken')
        with self.assertRaises(ConfigError): self.runtime.refresh_models()
        self.assertIs(bridge.router,router)
        self.assertEqual(self.runtime.catalog,catalog)
        self.settings(self.rows)
        self.assertIsNotNone(self.runtime.refresh_models())

    def test_initialization_failure_rolls_back_and_same_runtime_retries(self):
        with patch.object(self.runtime.state,'remember_session',side_effect=OSError('state temporarily locked')):
            with self.assertRaises(OSError): self.runtime.initialize(self.project)
        self.assertFalse(self.runtime.ready)
        self.assertIsNone(self.runtime.bridge.agent)
        self.assertEqual(self.runtime.readiness()['code'],'initialization_failed')
        self.runtime.initialize(self.project)
        self.assertTrue(self.runtime.ready)
        self.assertEqual(self.runtime.initialization_error,'')

    def test_invalid_secret_store_is_repairable_without_overwrite(self):
        p = self.host/'secrets.json'
        p.write_text('{broken')
        with self.assertRaisesRegex(ValueError,'模型密钥文件'): self.runtime.initialize(self.project)
        self.assertEqual(p.read_text(),'{broken')
        p.write_text(json.dumps({'writer':self.key}))
        self.runtime.initialize(self.project)
        self.assertTrue(self.runtime.readiness()['ready'])

    def test_busy_runtime_does_not_reload_frozen_model_choices(self):
        self.runtime.initialize(self.project)
        self.runtime._lane.acquire()
        try:
            with patch.object(self.runtime.bridge,'refresh_models') as refresh:
                self.assertIsNone(self.runtime.refresh_models())
                refresh.assert_not_called()
        finally: self.runtime._lane.release()

    def test_real_two_turn_loop_uses_host_model_and_writes_only_project(self):
        self.runtime.initialize(self.project)
        p = self.project/'one.py'
        p.write_text('print(1)\n')
        answers = iter([
            '<tool_call>'+json.dumps({'tool':'read_file','args':{'path':'one.py'}})+'</tool_call>',
            '<tool_call>'+json.dumps({'tool':'write_file','args':{'path':'one.py','content':'print(2)\n'}})+'</tool_call>',
            'first finished', 'second finished'])
        calls = []
        class Transport:
            def complete(_,provider,model,messages,**options):
                calls.append((provider.name,model))
                return next(answers), Usage(5,5,provider.name,model)
        self.runtime.bridge.router.transport = Transport()
        self.runtime.bridge.agent.thinking = False
        self.runtime.bridge.agent.thinking_mode = 'off'
        one = self.runtime.run('change file',permission='workspace-write',planning='none')
        two = self.runtime.run('follow up',planning='none')
        self.assertTrue(one['ok'])
        self.assertTrue(two['ok'])
        self.assertNotEqual(one['task_id'],two['task_id'])
        self.assertEqual(p.read_text(),'print(2)\n')
        self.assertEqual(calls,[('writer','code-one')]*4)
        self.assertEqual(len(self.runtime.tasks),2)


if __name__ == '__main__': unittest.main()
