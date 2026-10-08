"""Adversarial contracts for the trusted secret boundary (synthetic keys only)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.policy import Policy, Mode, Sandbox, Decision
from forge.tools import ToolContext, build_builtin_registry


def _process_config_edit(root, ready, proceed, result):
    root = Path(root)
    ctx = ToolContext(Policy(mode=Mode.ACCEPT_EDITS, workspace=root), root, isolated=True)
    reg = build_builtin_registry()
    view = reg.invoke('edit_config', {'path': 'shared.json'}, ctx)
    ready.put(view.ok)
    if proceed.wait(8):
        edited = reg.invoke('edit_config', {'path': 'shared.json', 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/model', 'value': 'changed'}]}, ctx)
        result.put(edited.ok)


class SecretVirtualizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from forge.secrets import SecretScope
        self.scope = SecretScope()
        self.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.root)
        self.ctx = ToolContext(self.policy, self.root, secret_scope=self.scope,
                               isolated=True)
        self.reg = build_builtin_registry()
        self.keys = ['sk-synthetic-alpha-1234567890', 'synthetic-password-beta-67890']

    def config(self):
        p = self.root / 'models.json'
        p.write_text(json.dumps({'api_key': self.keys[0], 'password': self.keys[1],
                                'model': 'old', 'cache': {'ttl': 300}}), encoding='utf-8')
        return p

    def test_T1_patch_preserves_secrets_and_updates_model_cache(self):
        p = self.config()
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        self.assertTrue(view.ok, view.error)
        data = json.loads(view.content)
        self.assertNotIn(self.keys[0], view.content)
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/model', 'value': 'new'},
                      {'op': 'replace', 'path': '/cache/ttl', 'value': 600}]}, self.ctx)
        self.assertTrue(result.ok, result.error)
        saved = json.loads(p.read_text())
        self.assertEqual(saved['api_key'], self.keys[0])
        self.assertEqual(saved['password'], self.keys[1])
        self.assertEqual(saved['model'], 'new')
        self.assertEqual(saved['cache']['ttl'], 600)
        self.assertNotEqual(data['api_key'], data['password'])

    def test_T2_stable_refs_and_unrelated_scopes(self):
        from forge.secrets import SecretScope
        data = {'api_key': self.keys[0], 'password': self.keys[1]}
        first = self.scope.protect(data)
        self.assertEqual(first, self.scope.protect(data))
        self.assertNotEqual(first, SecretScope().protect(data))
        self.assertNotEqual(first['api_key'], first['password'])
        self.assertNotIn('1234567890', first['api_key'])

    def test_T3_resolve_export_hard_denied_even_bypass(self):
        for mode in Mode:
            policy = Policy(mode=mode, sandbox=Sandbox.FULL_ACCESS, allow=('*',))
            for cap in ('secret.resolve', 'secret.export'):
                self.assertEqual(policy.evaluate(cap), Decision.DENY)
        self.assertFalse(self.reg.invoke('resolve_secret', {'ref': 'anything'}, self.ctx).ok)

    def test_T4_exception_session_and_redactor(self):
        from forge.secrets import redact
        from forge.session import Session
        self.scope.protect({'password': self.keys[1]})
        self.reg.register(__import__('forge.tools', fromlist=['ToolSpec']).ToolSpec(
            'crash', 'host test', lambda a, c: (_ for _ in ()).throw(ValueError(self.keys[1]))))
        result = self.reg.invoke('crash', {}, self.ctx)
        self.assertNotIn(self.keys[1], result.error)
        with Session(self.root / 'session.jsonl') as session:
            session.append('error', error=self.keys[1], trace={'value': self.keys[1]})
            self.assertNotIn(self.keys[1], str(session.events))
        self.assertNotIn(self.keys[1], (self.root / 'session.jsonl').read_text())
        self.assertEqual(redact(self.keys[1]), '[REDACTED_SECRET]')

    def test_T5_nested_messages_and_plugin_outputs(self):
        data = {'messages': [{'role': 'user', 'content': 'PASSWORD=' + self.keys[1]}],
                'plugin': {'Authorization': 'Bearer ' + self.keys[0]}}
        safe = self.scope.protect(data)
        for key in self.keys:
            self.assertNotIn(key, json.dumps(safe))
        self.assertFalse(hasattr(self.scope, 'resolve'))
        self.assertFalse(hasattr(self.scope, 'values'))

    def test_T6_no_native_execution_in_protected_agent(self):
        ctx = ToolContext(Policy(mode=Mode.BYPASS, sandbox=Sandbox.FULL_ACCESS), self.root,
                          secret_scope=self.scope, isolated=True)
        with patch('forge.tools.subprocess.run') as run:
            result = self.reg.invoke('shell_exec', {'command': 'python arbitrary.py'}, ctx)
        self.assertFalse(result.ok)
        run.assert_not_called()

    def test_T7_forgery_swap_deletion_and_generic_rewrite(self):
        p = self.config()
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        data = json.loads(view.content)
        original = p.read_bytes()
        attacks = [
            {'op': 'remove', 'path': '/api_key'},
            {'op': 'replace', 'path': '/api_key', 'value': '{{SECRET_REF:FORGED}}'},
            {'op': 'replace', 'path': '/api_key', 'value': data['password']},
            {'op': 'replace', 'path': '/model', 'value': data['api_key']},
            {'op': 'replace', 'path': '/api_key', 'value': 'literal-secret'},
        ]
        for attack in attacks:
            result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
                'patch': [attack]}, self.ctx)
            self.assertFalse(result.ok, attack)
            self.assertEqual(p.read_bytes(), original)
        result = self.reg.invoke('write_file', {'path': p.name, 'content': view.content}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(p.read_bytes(), original)

    def test_T8_store_path_absolute_and_link_denied(self):
        from forge.secrets import protect_store_path
        p = self.root / 'vault.json'
        p.write_text(json.dumps({'secret': self.keys[0]}))
        protect_store_path(p)
        ctx = ToolContext(Policy(mode=Mode.BYPASS, sandbox=Sandbox.FULL_ACCESS), self.root,
                          secret_scope=self.scope, isolated=True)
        self.assertFalse(self.reg.invoke('read_file', {'path': str(p)}, ctx).ok)
        link = self.root / 'hardlink.json'
        import os
        os.link(p, link)
        self.assertFalse(self.reg.invoke('read_range', {'path': str(link), 'start': 1}, ctx).ok)
        result = self.reg.invoke('grep', {'root': str(self.root), 'pattern': '.'}, ctx)
        self.assertNotIn(self.keys[0], result.content)

    def test_whole_file_detection_before_range_or_truncation(self):
        p = self.root / '.env'
        p.write_text('PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nABCSECRET\n-----END PRIVATE KEY-----"\n'
                     'PASSWORD=' + self.keys[1])
        result = self.reg.invoke('read_range', {'path': '.env', 'start': 2, 'end': 4}, self.ctx)
        self.assertNotIn('ABCSECRET', result.content)
        self.assertNotIn(self.keys[1], result.content)

    def test_stale_patch_rejected(self):
        p = self.config()
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        p.write_text(p.read_text().replace('old', 'human'))
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/model', 'value': 'new'}]}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(json.loads(p.read_text())['model'], 'human')

    def test_config_formats_and_vendor_profile(self):
        samples = {
            'model.json': json.dumps({'vendor': 'mimo', 'key': self.keys[0], 'model': 'old'}),
            'model.yaml': 'vendor: mimo\nkey: ' + self.keys[0] + '\nmodel: old\n',
            'model.toml': 'vendor = "mimo"\nkey = "' + self.keys[0] + '"\nmodel = "old"\n',
            '.env': '# preserve me\nOPENAI_API_KEY="' + self.keys[0] + '"\nMODEL=old # note\n',
        }
        for name, content in samples.items():
            p = self.root / name
            p.write_text(content, encoding='utf-8')
            view = self.reg.invoke('edit_config', {'path': name}, self.ctx)
            self.assertTrue(view.ok, view.error)
            self.assertNotIn(self.keys[0], view.content)
            ordinary = '/MODEL' if name == '.env' else '/model'
            result = self.reg.invoke('edit_config', {'path': name, 'revision': view.meta['revision'],
                'patch': [{'op': 'replace', 'path': ordinary, 'value': 'new'}]}, self.ctx)
            self.assertTrue(result.ok, result.error)
            self.assertIn(self.keys[0], p.read_text())
            self.assertNotIn('{{SECRET_REF:', p.read_text())
            if name == '.env':
                self.assertIn('# preserve me', p.read_text())
                self.assertIn('# note', p.read_text())
            read = self.reg.invoke('read_file', {'path': name}, self.ctx)
            self.assertNotIn(self.keys[0], read.content)

    def test_malformed_and_duplicate_configs_fail_closed(self):
        samples = {'x.json': '{"model": "x", "model": "y"}',
                   'x.yaml': 'a: &a {password: foo}\nb: *a\n',
                   '.env': 'PASSWORD=first\nPASSWORD=second\n'}
        for name, text in samples.items():
            (self.root / name).write_text(text)
            result = self.reg.invoke('edit_config', {'path': name}, self.ctx)
            self.assertFalse(result.ok)
            self.assertNotIn('foo', result.content)

    def test_secret_capabilities_are_independent_of_filesystem(self):
        for mode in (Mode.READ_ONLY, Mode.BYPASS):
            policy = Policy(mode=mode, sandbox=Sandbox.FULL_ACCESS, allow=('*',))
            self.assertEqual(policy.evaluate('secret.reference'), Decision.ALLOW)
            for name in ('use', 'create', 'replace', 'delete'):
                self.assertEqual(policy.evaluate('secret.' + name), Decision.ASK)
        p = self.config()
        ctx = ToolContext(Policy(mode=Mode.READ_ONLY, workspace=self.root), self.root, secret_scope=self.scope)
        self.assertTrue(self.reg.invoke('edit_config', {'path': p.name}, ctx).ok)

    def test_concurrent_edits_one_revision_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        p = self.config()
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        def edit(model):
            return self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
                'patch': [{'op': 'replace', 'path': '/model', 'value': model}]}, self.ctx)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ['one', 'two']))
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual(json.loads(p.read_text())['api_key'], self.keys[0])

    def test_T9_http_error_and_trace_do_not_expose_auth(self):
        import io, urllib.error, traceback
        from forge.model import Provider, HttpTransport, TransportError
        provider = Provider('test', 'https://vendor.example/v1', api_key=self.keys[0])
        error = urllib.error.HTTPError(provider.chat_url(), 400, 'error', {},
            io.BytesIO(('Authorization: Bearer ' + self.keys[0]).encode()))
        with patch('forge.secret_http.open_authenticated', side_effect=error):
            try:
                HttpTransport().complete(provider, 'model', [{'role': 'user', 'content': 'hello'}])
            except TransportError:
                diagnostic = traceback.format_exc()
            else:
                self.fail('Expected HTTP failure')
        self.assertNotIn(self.keys[0], diagnostic)

    def test_T10_captured_payload_virtualized_auth_works(self):
        import io
        from forge.model import Provider, HttpTransport, ModelRouter
        provider = Provider('test', 'https://vendor.example/v1', api_key=self.keys[0])
        captured = []
        def send(request, **kwargs):
            captured.append(request.data.decode())
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.keys[0])
            return io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}],"usage":{"prompt_tokens":2,"completion_tokens":1}}')
        with patch('forge.secret_http.open_authenticated', side_effect=send):
            reply = ModelRouter([provider], primary=('test', 'model')).complete([
                {'role': 'user', 'content': 'API_KEY=' + self.keys[0] + '\nPASSWORD=' + self.keys[1]}])
        self.assertEqual(reply.text, 'OK')
        for key in self.keys:
            self.assertNotIn(key, captured[0])
        self.assertIn('{{SECRET_REF:', captured[0])
        self.assertNotIn(self.keys[0], repr(provider))
        self.assertNotIn(self.keys[0], str(provider.auth_headers()))

    def test_custom_transport_and_smart_router_cannot_receive_literals(self):
        from forge.model import Provider, Usage
        from forge.routing import SmartRouter, RoutingConfig
        seen = []
        class Transport:
            def complete(_, provider, model, messages, **opts):
                seen.append(json.dumps([messages, opts]))
                return self.keys[0], Usage()
        provider = Provider('test', 'https://vendor.example', api_key=self.keys[0])
        router = SmartRouter([provider], transport=Transport(), routing=RoutingConfig(
            tiers=[('test', 'm')], small=('test', 'm')))
        reply = router.complete([{'role': 'user', 'content': 'PASSWORD=' + self.keys[1]}], small=True)
        self.assertNotIn(self.keys[1], seen[0])
        self.assertNotIn(self.keys[0], reply.text)

    def test_stream_fragments_and_tool_indexes_do_not_leak(self):
        from forge.secret_http import SecretSSEFilter
        self.scope.reference(self.keys[0])
        fence = SecretSSEFilter()
        outputs = []
        chunks = [self.keys[0][:6], self.keys[0][6:15], self.keys[0][15:]]
        for index, chunk in enumerate(chunks):
            event = {'id': str(index), 'choices': [{'index': 0, 'delta': {'content': chunk}}]}
            outputs.append(fence.feed(('data: ' + json.dumps(event) + '\n\n').encode()))
        outputs.append(fence.feed(b'data: [DONE]\n\n'))
        text = ''.join(json.loads(line[5:])['choices'][0]['delta'].get('content', '')
                       for output in outputs for line in output.decode().splitlines()
                       if line.startswith('data:') and '[DONE]' not in line)
        self.assertEqual(text, '[REDACTED_SECRET]')
        fence = SecretSSEFilter()
        one = {'choices': [{'delta': {'tool_calls': [{'index': 3, 'function': {'name': 'read', 'arguments': self.keys[0][:5]}}]}}]}
        two = {'choices': [{'delta': {'tool_calls': [{'index': 4, 'function': {'arguments': 'safe'}},
                                                   {'index': 3, 'function': {'arguments': self.keys[0][5:]}}]}}]}
        wire = b''.join(fence.feed(('data: ' + json.dumps(e) + '\n\n').encode()) for e in (one, two)) + fence.finish()
        args = {}
        for line in wire.decode().splitlines():
            if not line.startswith('data:'): continue
            for tool in json.loads(line[5:])['choices'][0]['delta'].get('tool_calls', []):
                args[tool['index']] = args.get(tool['index'], '') + tool['function'].get('arguments', '')
        self.assertEqual(args[3], '[REDACTED_SECRET]')
        self.assertEqual(args[4], 'safe')

    def test_logging_and_vendor_endpoint_binding(self):
        import io, logging
        from forge.secrets import install_logging_redaction, VendorCredential
        self.scope.reference(self.keys[0])
        install_logging_redaction()
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        logger = logging.getLogger('forge.secret.test')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.warning('credential=%s', self.keys[0])
        try:
            raise ValueError(self.keys[0])
        except ValueError:
            logger.exception('crash')
        self.assertNotIn(self.keys[0], buffer.getvalue())
        credential = VendorCredential(self.keys[0], 'https://vendor.example')
        with self.assertRaises(PermissionError): credential._header('https://evil.example', wire='openai')
        with self.assertRaises(PermissionError): credential._header('http://vendor.example', wire='openai')

    def test_T8_symbolic_link_and_secret_scope_revocation(self):
        from forge.secrets import protect_store_path, _restore_config
        p = self.root / 'vault2.json'
        p.write_text(self.keys[0])
        protect_store_path(p)
        link = self.root / 'alias.json'
        try:
            link.symlink_to(p)
        except OSError:
            # Windows requires Developer Mode or symlink privilege.
            pass
        else:
            self.assertFalse(self.reg.invoke('read_file', {'path': str(link)}, self.ctx).ok)
        ref = self.scope.reference(self.keys[0])
        self.scope.close()
        with self.assertRaises(PermissionError): _restore_config(self.scope, ref, 'test')

    def test_terminal_delta_must_not_flush_unredacted_prefix(self):
        from forge.secret_http import SecretSSEFilter
        self.scope.reference(self.keys[0])
        fence = SecretSSEFilter()
        events = [
            {'choices': [{'delta': {'content': self.keys[0][:8]}, 'finish_reason': None}]},
            {'choices': [{'delta': {'content': self.keys[0][8:]}, 'finish_reason': 'stop'}]},
        ]
        wire = b''.join(fence.feed(('data: ' + json.dumps(e) + '\n\n').encode()) for e in events)
        content = ''.join(json.loads(line[5:])['choices'][0]['delta'].get('content', '')
                          for line in wire.decode().splitlines() if line.startswith('data:'))
        self.assertEqual(content, '[REDACTED_SECRET]')

    def test_secret_indirection_and_empty_field_cannot_be_rewritten(self):
        p = self.root / 'envref.json'
        p.write_text(json.dumps({'apiKey': {'$expr': "get('env.KEY', '')"}, 'password': '', 'model': 'old'}))
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        for operation in [
            {'op': 'replace', 'path': '/apiKey/$expr', 'value': "get('env.OTHER', '')"},
            {'op': 'remove', 'path': '/password'},
        ]:
            result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
                'patch': [operation]}, self.ctx)
            self.assertFalse(result.ok)
        self.assertFalse(self.reg.invoke('edit_file', {'path': p.name, 'old': 'KEY', 'new': 'OTHER'}, self.ctx).ok)

    def test_detection_has_a_bounded_cost_for_oversized_plugin_text(self):
        import time
        text = 'x' * (1024 * 1024)
        started = time.monotonic()
        self.assertEqual(self.scope.protect_text(text), text)
        self.assertLess(time.monotonic() - started, 3)
        value = self.scope.protect_text("PROMPT = 'PASSWORD=nested-generic-value'\n")
        self.assertNotIn('nested-generic-value', value)
        self.assertIn('{{SECRET_REF:', value)
        with self.assertRaises(ValueError): self.scope.reference('x' * 65537)

    def test_T5_parent_child_real_agent_boundary(self):
        from forge.loop import Agent, LoopLimits
        from forge.model import ModelRouter, Provider, Usage
        payloads = []
        class Transport:
            def complete(_, provider, model, messages, **opts):
                payloads.append(json.dumps(messages))
                if 'sub1' in messages[0]['content']:
                    return 'child finished', Usage()
                return '<tool_call>' + json.dumps({'tool': 'spawn_subagent', 'args': {
                    'task': 'PASSWORD=' + self.keys[1]}}) + '</tool_call>', Usage()
        router = ModelRouter([Provider('test', 'https://vendor.example', default_model='m')],
                             transport=Transport(), primary=('test', 'm'))
        agent = Agent(home=self.root, workspace=self.root, router=router, registry=self.reg,
            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=self.root),
            limits=LoopLimits(max_steps=1, max_depth=1, spawn_budget=1))
        report = agent.run('PASSWORD=' + self.keys[1])
        self.assertEqual(len(payloads), 2)
        for payload in payloads: self.assertNotIn(self.keys[1], payload)
        self.assertNotIn(self.keys[1], str(report))

    def test_gateway_all_routes_and_tool_scope_use_the_same_boundary(self):
        import io, threading, urllib.request
        from forge.gateway import GatewayConfig, serve
        from forge.secret_http import open_authenticated
        p = self.config()
        cfg = GatewayConfig(upstream='https://vendor.example/v1', api_key=self.keys[0], port=0,
                            workspace=str(self.root), policy=self.policy, registry=self.reg, upstream_wire='openai')
        server = serve(cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = 'http://127.0.0.1:' + str(server.server_address[1])
        captured = []
        class Response(io.BytesIO):
            status = 200
            headers = {'content-type': 'application/json'}
        def send(request, **kwargs):
            captured.append(request.data.decode())
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.keys[0])
            return Response(b'{"ok":true}')
        headers = {'content-type': 'application/json', 'X-Forge-Secret-Scope': 'a' * 32}
        def call(path, data):
            req = urllib.request.Request(endpoint + path, data=json.dumps(data).encode(), headers=headers)
            with open_authenticated(req, timeout=2) as response:
                return json.loads(response.read())
        with patch('forge.gateway.open_upstream', side_effect=send):
            call('/v1/custom', {'data': 'PASSWORD=' + self.keys[1]})
        self.assertNotIn(self.keys[1], captured[0])
        view = call('/v1/tools/call', {'name': 'edit_config', 'arguments': {'path': p.name}})
        result = call('/v1/tools/call', {'name': 'edit_config', 'arguments': {'path': p.name,
            'revision': view['meta']['revision'], 'patch': [{'op': 'replace', 'path': '/model', 'value': 'new'}]}})
        self.assertTrue(result['ok'], result)
        self.assertEqual(json.loads(p.read_text())['api_key'], self.keys[0])

    def test_cookie_maps_headers_and_url_passwords(self):
        data = {'cookies': {'sid': 'cookie-secret-one', 'other': 'cookie-secret-two'},
                'credentials': {'value': 'adapter-secret-value'},
                'url': 'postgres://user:url-secret-password@database/db'}
        safe = json.dumps(self.scope.protect(data))
        for value in ('cookie-secret-one', 'cookie-secret-two', 'adapter-secret-value', 'url-secret-password'):
            self.assertNotIn(value, safe)
        header = self.scope.protect_text('Cookie: sid=one-unregistered; extra=two-unregistered\n')
        self.assertNotIn('two-unregistered', header)

    def test_host_approved_reference_copy_is_scoped_and_consumed(self):
        from forge.secrets import authorize_config_secret
        p = self.config()
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        ref = json.loads(view.content)['api_key']
        with self.assertRaises(PermissionError):
            authorize_config_secret(self.scope, self.policy, path=p.name, revision=view.meta['revision'],
                                    pointer='/backup_api_key', capability='secret.use', ref=ref)
        approved = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.root, allow=('secret.use',))
        authorize_config_secret(self.scope, approved, path=p.name, revision=view.meta['revision'],
                                pointer='/backup_api_key', capability='secret.use', ref=ref)
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'add', 'path': '/backup_api_key', 'value': ref}]}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(json.loads(p.read_text())['backup_api_key'], self.keys[0])
        self.assertFalse(self.scope._config_grants)
        # A secret.use grant for an ordinary field cannot turn that field into an export channel.
        authorize_config_secret(self.scope, approved, path=p.name, revision=result.meta['revision'],
                                pointer='/model', capability='secret.use', ref=ref)
        attack = self.reg.invoke('edit_config', {'path': p.name, 'revision': result.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/model', 'value': ref}]}, self.ctx)
        self.assertFalse(attack.ok)

    def test_credential_destination_cannot_be_changed_without_grant(self):
        p = self.root / 'vendor.json'
        p.write_text(json.dumps({'baseURL': 'https://vendor.example/v1', 'api_key': self.keys[0], 'model': 'old'}))
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/baseURL', 'value': 'https://evil.example/v1'}]}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(json.loads(p.read_text())['baseURL'], 'https://vendor.example/v1')

    def test_numeric_secret_preserves_type_without_exposing_value(self):
        p = self.root / 'numeric.json'
        p.write_text(json.dumps({'password': 9384756102, 'model': 'old'}))
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        self.assertNotIn('9384756102', view.content)
        self.assertNotIn('9384756102', self.scope.file_text(p))
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/model', 'value': 'new'}]}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(json.loads(p.read_text())['password'], 9384756102)
        self.assertIsInstance(json.loads(p.read_text())['password'], int)

    def test_punctuation_secret_does_not_corrupt_json_protocol(self):
        from forge.secrets import SecretScope, _KNOWN
        scope = SecretScope()
        with patch('forge.secrets._KNOWN', set(_KNOWN)):
            scope.reference('{')
            safe = scope.protect_text(json.dumps({'messages': [{'content': 'password={' }]}))
        self.assertIsInstance(json.loads(safe), dict)
        self.assertNotIn('password={"', safe)

    def test_credential_destination_bound_host_approval(self):
        from forge.secrets import authorize_config_destination
        p = self.root / 'approved.json'
        p.write_text(json.dumps({'baseURL': 'https://vendor.example/v1', 'api_key': self.keys[0]}))
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        approved = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.root, allow=('secret.use',))
        authorize_config_destination(self.scope, approved, path=p.name, revision=view.meta['revision'],
            pointer='/baseURL', value='https://approved.example/v1')
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/baseURL', 'value': 'https://approved.example/v1'}]}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(json.loads(p.read_text())['api_key'], self.keys[0])
        self.assertFalse(self.scope._config_grants)

    def test_cooperating_processes_reject_lost_update(self):
        import multiprocessing
        p = self.root / 'shared.json'
        p.write_text(json.dumps({'api_key': self.keys[0], 'model': 'old'}))
        mp = multiprocessing.get_context('spawn')
        ready, result, proceed = mp.Queue(), mp.Queue(), mp.Event()
        workers = [mp.Process(target=_process_config_edit, args=(str(self.root), ready, proceed, result))
                   for _ in range(2)]
        try:
            for worker in workers: worker.start()
            for _ in workers: self.assertTrue(ready.get(timeout=15))
            proceed.set()
            outcomes = [result.get(timeout=15) for _ in workers]
            self.assertEqual(outcomes.count(True), 1)
        finally:
            for worker in workers:
                worker.join(timeout=3)
                if worker.is_alive(): worker.terminate(); worker.join(timeout=3)
            ready.close()
            result.close()
        self.assertEqual(json.loads(p.read_text())['api_key'], self.keys[0])

    def test_vendor_uses_owned_reference_only_after_host_grant(self):
        from forge.secrets import VendorCredential
        ref = self.scope.reference(self.keys[0])
        with self.assertRaises(PermissionError):
            VendorCredential.from_reference(self.scope, self.policy, ref=ref, endpoint='https://vendor.example')
        approved = Policy(allow=('secret.use',))
        with patch.object(self.scope._store, '_read', wraps=self.scope._store._read) as resolve:
            credential = VendorCredential.from_reference(self.scope, approved, ref=ref,
                                                        endpoint='https://vendor.example')
            resolve.assert_not_called()
            with self.assertRaises(PermissionError):
                credential._header('https://evil.example', wire='openai')
            resolve.assert_not_called()
            self.assertEqual(credential._header('https://vendor.example/chat', wire='openai')['Authorization'],
                             'Bearer ' + self.keys[0])
            self.assertEqual(resolve.call_count, 1)
        self.scope.close()
        with self.assertRaises(PermissionError):
            credential._header('https://vendor.example/chat', wire='openai')

    def test_http_boundary_ignores_global_debug_opener(self):
        import urllib.request
        from forge.secret_http import open_authenticated
        observed = []
        class Opener:
            def open(_, request, timeout):
                return 'opened'
        def build(*handlers):
            observed.extend(handlers)
            return Opener()
        with patch('urllib.request.build_opener', side_effect=build):
            self.assertEqual(open_authenticated(urllib.request.Request('https://vendor.example'), timeout=1), 'opened')
        wire_handlers = [h for h in observed if isinstance(h, (urllib.request.HTTPHandler, urllib.request.HTTPSHandler))]
        self.assertEqual(len(wire_handlers), 2)
        self.assertTrue(all(h._debuglevel == 0 for h in wire_handlers))

    def test_router_default_operations_do_not_share_refs(self):
        from forge.model import ModelRouter, Provider, Usage
        captures = []
        class Transport:
            def complete(_, provider, model, messages, **options):
                captures.append(json.dumps(messages))
                return 'OK', Usage()
        router = ModelRouter([Provider('test', 'https://vendor.example', default_model='m')], transport=Transport())
        for _ in range(2): router.complete([{'role': 'user', 'content': 'PASSWORD=' + self.keys[1]}])
        self.assertNotEqual(captures[0], captures[1])
        for payload in captures: self.assertNotIn(self.keys[1], payload)

    def test_config_cannot_escalate_forge_execution_policy(self):
        p = self.root / 'policy.json'
        p.write_text(json.dumps({'policy': {'allow': [], 'mode': 'default'}, 'model': 'old'}))
        view = self.reg.invoke('edit_config', {'path': p.name}, self.ctx)
        result = self.reg.invoke('edit_config', {'path': p.name, 'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/policy/allow', 'value': ['secret.use', '*']}]}, self.ctx)
        self.assertFalse(result.ok)
        attack = self.reg.invoke('write_file', {'path': p.name, 'content': '{"policy":{"mode":"bypass"}}'}, self.ctx)
        self.assertFalse(attack.ok)

    def test_protected_agent_cannot_plant_host_modules(self):
        path = self.root / '.forge' / 'modules' / 'injected.py'
        result = self.reg.invoke('write_file', {'path': str(path), 'content': 'import os'}, self.ctx)
        self.assertFalse(result.ok)
        self.assertFalse(path.exists())

    def test_vendor_fields_in_plain_attachment_or_atomic_temp(self):
        p = self.root / 'adapter.tmp'
        p.write_text('vendor = "mimo"\nkey = "unformatted-adapter-credential"\nmodel = "m"')
        safe = self.scope.file_text(p)
        self.assertNotIn('unformatted-adapter-credential', safe)
        self.assertIn('SECRET_REF:', safe)

    def test_fake_reference_syntax_cannot_hide_a_known_secret(self):
        self.scope.reference(self.keys[1])
        attack = '{{SECRET_REF:' + self.keys[1] + '}}'
        self.assertNotIn(self.keys[1], self.scope.protect_text(attack))
        from forge.secrets import redact
        self.assertNotIn(self.keys[1], redact(attack))
        hexadecimal = 'ABCDEF0123456789ABCDEF0123456789'
        self.scope.reference(hexadecimal)
        self.assertNotIn(hexadecimal, self.scope.protect_text('{{SECRET_REF:' + hexadecimal + '}}'))

    def test_bearer_cookie_scalar_echo_is_redacted_without_header(self):
        from forge.secrets import redact
        self.scope.protect({'Authorization': 'Bearer opaque-access-credential',
                            'Cookie': 'sid=opaque-cookie-one; refresh=opaque-cookie-two'})
        for value in ('opaque-access-credential', 'opaque-cookie-one', 'opaque-cookie-two'):
            self.assertEqual(redact(value), '[REDACTED_SECRET]')

    def test_uncaught_exception_hook_masks_registered_value(self):
        import io, sys
        from forge.secrets import install_logging_redaction
        self.scope.reference(self.keys[1])
        install_logging_redaction()
        output = io.StringIO()
        with patch('sys.stderr', output):
            sys.excepthook(ValueError, ValueError(self.keys[1]), None)
        self.assertNotIn(self.keys[1], output.getvalue())
        self.assertIn('[REDACTED_SECRET]', output.getvalue())

    def test_logger_extras_and_tk_crash_callback_are_redacted(self):
        import io, logging
        from types import SimpleNamespace
        from forge.secrets import install_logging_redaction, install_tk_exception_redaction
        self.scope.reference(self.keys[1])
        install_logging_redaction()
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(logging.Formatter('%(credential)s'))
        logger = logging.getLogger('forge.secret.extra')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.warning('safe message', extra={'credential': self.keys[1]})
        self.assertNotIn(self.keys[1], output.getvalue())
        received = []
        root = SimpleNamespace(report_callback_exception=lambda *args: received.append(args))
        install_tk_exception_redaction(root)
        root.report_callback_exception(ValueError, ValueError(self.keys[1]), None)
        self.assertNotIn(self.keys[1], str(received))
        self.assertIsNone(received[0][2])


if __name__ == '__main__':
    unittest.main()
