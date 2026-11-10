"""MiMo 实测回执驱动的请求约束，覆盖直连、网关和真实 Agent 执行边界。"""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from . import model
from .cache_state import CacheWarmth
from .gateway import GatewayConfig
from .loop import Agent, LoopLimits
from .policy import Mode, Policy
from .tools import build_builtin_registry
from .toolwire import ToolCall, replay_assistant_message


BASE = 'https://token-plan-cn.xiaomimimo.com/v1'
MODEL = 'mimo-v2.6-flash'


class MiMoAdapterTests(unittest.TestCase):
    def shape(self, payload, **kwargs):
        provider = model.Provider('mimo-fixture', BASE, default_model=MODEL, **kwargs)
        return model._plan_and_shape(payload, payload.get('messages', []), provider, MODEL,
                                     store=CacheWarmth())

    def test_default_and_enabled_reasoning_remove_ignored_sampling(self):
        for thinking in (None, {'type': 'enabled'}):
            payload = {'model': MODEL, 'messages': [], 'temperature': .7, 'top_p': .8}
            if thinking:
                payload['thinking'] = thinking
            before = copy.deepcopy(payload)
            shaped, plan = self.shape(payload)
            self.assertNotIn('temperature', shaped)
            self.assertNotIn('top_p', shaped)
            self.assertEqual(payload, before)
            self.assertTrue(any('固定采样' in a for a in plan['actions']))

    def test_disabled_reasoning_preserves_supported_sampling(self):
        shaped, _ = self.shape({'messages': [], 'enable_thinking': False,
                                'temperature': .7, 'top_p': .8})
        self.assertEqual(shaped['thinking'], {'type': 'disabled'})
        self.assertEqual(shaped['temperature'], .7)
        self.assertEqual(shaped['top_p'], .8)

    def test_disabled_reasoning_rejects_out_of_range_temperature(self):
        for value in (1.6, -1, float('nan'), True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'temperature'):
                self.shape({'messages': [], 'thinking': {'type': 'disabled'}, 'temperature': value})

    def test_unsupported_tool_choice_fails_before_http(self):
        provider = model.Provider('fixture', BASE, default_model=MODEL)
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(True)
            raise AssertionError('must not send a misleading tool constraint')
        transport = model.HttpTransport(response_opener=forbidden)
        for choice in ('none', 'required', {'type': 'function', 'function': {'name': 'add'}}):
            with self.subTest(choice=choice), self.assertRaisesRegex(model.BadRequest, 'tool_choice=auto'):
                transport.complete(provider, MODEL, [{'role': 'user', 'content': 'hi'}], tool_choice=choice)
        self.assertEqual(calls, [])

    def test_auto_tools_and_message_prefix_are_unchanged(self):
        payload = {'messages': [{'role': 'user', 'content': 'hi'}], 'tool_choice': 'auto',
                   'tools': [{'type': 'function', 'function': {'name': 'add'}}]}
        shaped, _ = self.shape(payload)
        for key in ('messages', 'tools', 'tool_choice'):
            self.assertEqual(shaped[key], payload[key])

    def test_provider_configuration_reaches_real_agent_http_requests(self):
        from .config import Config
        cfg = Config()
        cfg.apply_patch([{'id': 'fixture', 'name': 'provider:mimo', 'config': {
            'baseURL': BASE, 'model': MODEL, 'reasoningMode': 'off'}}])
        captured = []
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            if len(captured) == 1:
                message = {'role': 'assistant', 'content': '', 'reasoning_content': '',
                    'tool_calls': [{'id': 'read-original', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"a.txt"}'}}]}
                finish = 'tool_calls'
            else:
                message, finish = {'role': 'assistant', 'content': 'answer'}, 'stop'
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': finish, 'message': message}]}).encode())
        router = model.ModelRouter.from_config(cfg, transport=model.HttpTransport(response_opener=opener))
        self.assertEqual(router.providers['fixture'].reasoning_mode, 'off')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.txt').write_text('fixture content', encoding='utf-8')
            agent = Agent(home=root, workspace=root, router=router, registry=build_builtin_registry(),
                policy=Policy(workspace=root, mode=Mode.READ_ONLY), limits=LoopLimits(max_steps=3))
            self.addCleanup(agent.secret_scope.close)
            first = agent.run('first')
            self.assertEqual(captured[-1]['thinking'], {'type': 'disabled'})
            receipt = first.usage['requests'][0]['plan']['execution']['reasoning_default']
            self.assertEqual(receipt, {'mode': 'off', 'source': 'provider'})
            router.providers['fixture'].reasoning_mode = 'on'
            before = copy.deepcopy(captured[-1]['messages'])
            second = agent.run('second', history=[{'role': 'user', 'content': 'first'},
                {'role': 'assistant', 'content': first.text}])
            self.assertEqual(captured[-1]['thinking'], {'type': 'enabled'})
            self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in second.events))
            self.assertEqual(captured[-1]['messages'][:len(before)], before)
            self.assertIn('read-original', str(captured[-1]['messages']))

    def test_explicit_request_overrides_provider_reasoning_default(self):
        for requested, configured in [(True, 'off'), (False, 'on')]:
            shaped, plan = self.shape({'messages': [], 'enable_thinking': requested}, reasoning_mode=configured)
            self.assertEqual(shaped['thinking'], {'type': 'enabled' if requested else 'disabled'})
            self.assertEqual(plan['execution']['reasoning_default']['source'], 'request')

    def test_gateway_honours_same_reasoning_default(self):
        original = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'hello'}]}
        direct, left = self.shape(original, reasoning_mode='off')
        cfg = GatewayConfig(upstream=BASE, api_key='', models=[MODEL], wire='openai', upstream_wire='openai',
                            provider_options={'reasoningMode': 'off'})
        cfg.cache_warmth = CacheWarmth()
        gateway, right, _ = cfg.prepare_request(original)
        self.assertEqual(direct, gateway)
        self.assertEqual(left['fingerprint'], right['fingerprint'])

    def test_anthropic_mimo_uses_native_auto_shape_in_transport_and_gateway(self):
        tools = [{'name': 'read_file', 'description': 'read', 'input_schema': {'type': 'object'}}]
        provider = model.Provider('fixture', BASE, default_model=MODEL, wire='anthropic', reasoning_mode='off')
        captured = []
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': 'ok'}],
                                         'stop_reason': 'end_turn'}).encode())
        model.HttpTransport(response_opener=opener).complete(provider, MODEL, [], tools=tools)
        self.assertEqual(captured[0]['tool_choice'], {'type': 'auto'})
        self.assertEqual(captured[0]['thinking'], {'type': 'disabled'})
        cfg = GatewayConfig(upstream=BASE, api_key='', models=[MODEL], wire='anthropic', upstream_wire='anthropic',
                            provider_options={'reasoningMode': 'off'})
        cfg.cache_warmth = CacheWarmth()
        payload, _, _ = cfg.prepare_request({'model': MODEL, 'messages': [], 'tools': tools,
                                            'tool_choice': {'type': 'auto'}})
        self.assertEqual(payload, captured[0])
        choice = {'type': 'auto', 'disable_parallel_tool_use': True}
        serial, _, _ = cfg.prepare_request({'model': MODEL, 'messages': [], 'tools': tools, 'tool_choice': choice})
        self.assertEqual(serial['tool_choice'], choice)
        with self.assertRaisesRegex(ValueError, 'tool_choice=auto'):
            cfg.prepare_request({'model': MODEL, 'messages': [], 'tool_choice': {'type': 'any'}})
        with self.assertRaisesRegex(ValueError, 'tool_choice=auto'):
            cfg.prepare_request({'model': MODEL, 'messages': [],
                                 'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': 'true'}})

    def test_invalid_reasoning_default_and_unverified_models_fail_clearly(self):
        for value in ('low', '', None, False):
            with self.subTest(value=value), self.assertRaisesRegex(model.BadRequest, 'reasoningMode'):
                model.Provider('fixture', BASE, reasoning_mode=value)
        provider = model.Provider('fixture', 'https://api.deepseek.com', reasoning_mode='off')
        with self.assertRaisesRegex(ValueError, 'verified only for MiMo'):
            model._plan_and_shape({'messages': []}, [], provider, 'deepseek-unverified')

    def test_bound_alias_honours_explicit_reasoning_default(self):
        provider = model.Provider('fixture', 'https://gateway.invalid', default_model='opaque',
            model_kind_id='mimo.v2.6.pro', reasoning_mode='off')
        shaped, _ = model._plan_and_shape({'messages': []}, [], provider, 'opaque')
        self.assertEqual(shaped['thinking'], {'type': 'disabled'})

    def test_equivalent_defaults_share_canonical_payload_and_fingerprint(self):
        payload = {'messages': [{'role': 'user', 'content': 'hello'}],
                   'tools': [{'type': 'function', 'function': {'name': 'add'}}]}
        default, left = self.shape({**payload, 'temperature': .7})
        explicit, right = self.shape({**payload, 'thinking': {'type': 'enabled'}, 'tool_choice': 'auto'})
        self.assertEqual(default, explicit)
        self.assertEqual(left['fingerprint'], right['fingerprint'])

    def test_reasoning_hop_does_not_claim_a_guaranteed_cache_miss(self):
        payload = {'messages': [{'role': 'system', 'content': 'stable hop diagnostic'}]}
        self.shape({**payload, 'enable_thinking': False})
        _, plan = self.shape({**payload, 'enable_thinking': True})
        self.assertIn('reasoning_hop', plan)
        self.assertTrue(any('不预断零命中' in note for note in plan['notes']))

    def test_gateway_applies_same_mimo_constraints_and_fingerprint(self):
        original = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'hello'}],
                    'enable_thinking': True, 'temperature': .7, 'top_p': .9}
        direct, direct_plan = self.shape(original)
        cfg = GatewayConfig(upstream=BASE, api_key='', models=[MODEL], wire='openai', upstream_wire='openai')
        cfg.cache_warmth = CacheWarmth()
        gateway, gateway_plan, _ = cfg.prepare_request(original)
        self.assertEqual(direct, gateway)
        self.assertEqual(direct_plan['fingerprint'], gateway_plan['fingerprint'])
        with self.assertRaisesRegex(ValueError, 'tool_choice=auto'):
            cfg.prepare_request({**original, 'tool_choice': 'none'})

    def test_model_kind_binding_applies_to_opaque_alias(self):
        provider = model.Provider('fixture', 'https://gateway.invalid/v1', default_model='deployment-7',
                                  model_kind_id='mimo.v2.6.flash')
        shaped, _ = model._plan_and_shape({'messages': [], 'temperature': .7}, [], provider, 'deployment-7')
        self.assertNotIn('temperature', shaped)

    def test_unverified_mimo_generation_is_not_restricted(self):
        provider = model.Provider('fixture', BASE, default_model='mimo-future-model')
        shaped, _ = model._plan_and_shape({'messages': [], 'temperature': .7, 'tool_choice': 'none'},
                                         [], provider, 'mimo-future-model')
        self.assertEqual(shaped['temperature'], .7)
        self.assertEqual(shaped['tool_choice'], 'none')

    def test_disabled_adapter_preserves_explicit_wire_parameters(self):
        payload = {'messages': [], 'temperature': .7, 'tool_choice': 'none'}
        shaped, _ = self.shape(payload, adapt=False)
        self.assertEqual(shaped, payload)

    def test_nullable_cache_receipts_remain_unknown_and_raw_usage_is_retained(self):
        provider = model.Provider('fixture', BASE, default_model=MODEL)
        for details, expected in [(None, False), ({'cached_tokens': 0}, True), ({'cached_tokens': 128}, True)]:
            raw = {'prompt_tokens': 134, 'completion_tokens': 25, 'prompt_tokens_details': details}
            response = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}],
                        'usage': raw}
            def opener(*args, **kwargs):
                return io.BytesIO(json.dumps(response).encode())
            _, usage, _ = model.HttpTransport(response_opener=opener).complete(provider, MODEL, [])
            self.assertEqual(usage.cache_observed, expected)
            self.assertEqual(usage.cached_tokens, (details or {}).get('cached_tokens', 0))

    def test_final_reasoning_is_kept_without_native_tool_calls(self):
        original = {'role': 'assistant', 'content': '42', 'reasoning_content': 'final thought', 'tool_calls': None}
        self.assertEqual(replay_assistant_message(original, '42', [], 'openai'), original)

    def test_repetition_and_filter_never_execute_or_report_success(self):
        for reason in ('repetition_truncation', 'content_filter'):
            provider = model.Provider('fixture', BASE, default_model=MODEL)
            response = {'choices': [{'finish_reason': reason, 'message': {'role': 'assistant',
                'content': 'Done', 'tool_calls': [{'id': 'bad', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': '{"path":"bad.txt","content":"bad"}'}}]}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 30}}
            def opener(*args, **kwargs):
                return io.BytesIO(json.dumps(response).encode())
            transport = model.HttpTransport(response_opener=opener)
            _, _, extra = transport.complete(provider, MODEL, [])
            self.assertEqual(extra['tool_calls'], [])
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                router = model.ModelRouter([provider], transport=transport, retries_per_provider=0)
                agent = Agent(home=root, workspace=root, router=router, registry=build_builtin_registry(),
                    policy=Policy(workspace=root, mode=Mode.ACCEPT_EDITS, non_interactive=True),
                    limits=LoopLimits(max_steps=2))
                self.addCleanup(agent.secret_scope.close)
                report = agent.run('write bad.txt')
                self.assertEqual(report.stopped, reason)
                self.assertEqual(report.as_result().outcome, 'failed')
                self.assertEqual(report.usage['completion_tokens'], 30)
                self.assertFalse((root / 'bad.txt').exists())
                self.assertTrue(any(e['type'] == 'model_output_rejected' for e in report.events))


class NativeSurfaceTests(unittest.TestCase):
    def message(self, arguments):
        return {'role': 'assistant', 'reasoning_content': 'thought', 'tool_calls': [
            {'id': 'call-original', 'type': 'function', 'function': {'name': 'read_file', 'arguments': arguments}}]}

    def test_valid_native_json_lexical_surface_and_reasoning_are_preserved(self):
        arguments = '{  "path" : "\\u4e2d.txt" }'
        original = self.message(arguments)
        call = ToolCall('call-local', 'read_file', {'path': '中.txt'}, 'openai')
        projected = replay_assistant_message(original, '', [call], 'openai')
        self.assertEqual(projected['tool_calls'][0]['function']['arguments'], arguments)
        self.assertEqual(projected['tool_calls'][0]['id'], 'call-local')
        self.assertEqual(projected['reasoning_content'], 'thought')
        self.assertEqual(original['tool_calls'][0]['id'], 'call-original')

    def test_repaired_or_changed_arguments_are_encoded_from_validated_values(self):
        for arguments, value in [('{"path":"x",}', {'path': 'x'}),
                                 ('{"path":"old"}', {'path': 'new'}),
                                 ('{"value":true}', {'value': 1})]:
            projected = replay_assistant_message(self.message(arguments), '',
                [ToolCall('call-local', 'read_file', value, 'openai')], 'openai')
            wire_args = projected['tool_calls'][0]['function']['arguments']
            self.assertEqual(wire_args, json.dumps(value, ensure_ascii=False))

    def test_anthropic_input_is_detached_from_mutable_tool_arguments(self):
        args = {'path': 'x'}
        original = {'role': 'assistant', 'content': [
            {'type': 'thinking', 'thinking': 'thought', 'signature': 'signed'},
            {'type': 'tool_use', 'id': 'original', 'name': 'read_file', 'input': {'path': 'x'}}]}
        projected = replay_assistant_message(original, '', [ToolCall('local', 'read_file', args, 'anthropic')], 'anthropic')
        args['path'] = 'mutated'
        self.assertEqual(projected['content'][1]['input'], {'path': 'x'})
        self.assertEqual(projected['content'][0], original['content'][0])


if __name__ == '__main__':
    unittest.main()
