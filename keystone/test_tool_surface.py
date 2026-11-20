"""原生工具发现不重排既有前缀；权限和 schema 更新不因缓存被延迟。"""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from .loop import Agent, LoopLimits
from .model import HttpTransport, ModelRouter, Provider
from .policy import Mode, Policy
from .tool_surface import ToolSurface
from .tools import ToolRegistry, ToolResult, ToolSpec, build_builtin_registry
from .toolwire import tool_declarations


def spec(name, **kwargs):
    return ToolSpec(name, 'test ' + name, lambda args, ctx: ToolResult(True, 'ok'), **kwargs)


def names(declarations):
    return [(d.get('function') or d)['name'] for d in declarations]


class ToolSurfaceTests(unittest.TestCase):
    def test_new_tools_append_for_both_native_protocols(self):
        for wire in ('openai', 'anthropic'):
            surface = ToolSurface()
            before = surface.project([spec('z'), spec('m')], wire)
            after = surface.project([spec('a'), spec('z'), spec('m')], wire)
            self.assertEqual(names(after), ['m', 'z', 'a'])
            self.assertEqual(after[:len(before)], before)

    def test_removed_tools_disappear_immediately_and_readdition_is_new(self):
        surface = ToolSurface()
        before = surface.project([spec('a'), spec('z')], 'openai')
        removed = surface.project([spec('z')], 'openai')
        self.assertEqual(names(removed), ['z'])
        self.assertEqual(names(surface.project([spec('a'), spec('z')], 'openai')), ['z', 'a'])
        self.assertEqual(names(before), ['a', 'z'])

    def test_current_schema_updates_without_mutating_prior_projection(self):
        surface = ToolSurface()
        original = spec('read', schema={'path': {'type': 'string', 'enum': []}})
        source = copy.deepcopy(original.schema)
        before = surface.project([original], 'openai')
        self.assertEqual(original.schema, source)
        original.schema['path']['type'] = 'integer'
        original.description = 'updated description'
        after = surface.project([original], 'openai')
        self.assertEqual(before[0]['function']['parameters']['properties']['path']['type'], 'string')
        self.assertEqual(after[0]['function']['parameters']['properties']['path']['type'], 'integer')
        self.assertEqual(after[0]['function']['description'], 'updated description')

    def test_hidden_activated_tools_are_not_resurrected(self):
        registry = ToolRegistry(hide=('secret_tool',))
        registry.register(spec('read'), trusted=True)
        registry.register(spec('secret_tool', deferred=True), trusted=True)
        registry.activate('secret_tool')
        surface = ToolSurface()
        self.assertEqual(names(surface.project(registry.visible(), 'openai')), ['read'])
        registry.unregister('read')
        self.assertEqual(surface.project(registry.visible(), 'openai'), [])

    def test_order_is_session_and_wire_local(self):
        left, right = ToolSurface(), ToolSurface()
        left.project([spec('z')], 'openai')
        self.assertEqual(names(left.project([spec('a'), spec('z')], 'openai')), ['z', 'a'])
        self.assertEqual(names(right.project([spec('a'), spec('z')], 'openai')), ['a', 'z'])
        self.assertEqual(names(left.project([spec('a'), spec('z')], 'anthropic')), ['a', 'z'])

    def test_subagent_fork_inherits_order_but_filters_its_own_scope(self):
        parent = ToolSurface()
        parent.project([spec('z')], 'openai')
        parent.project([spec('a'), spec('z')], 'openai')
        child = parent.fork()
        self.assertEqual(names(child.project([spec('a'), spec('b')], 'openai')), ['a', 'b'])
        self.assertEqual(names(parent.project([spec('a'), spec('z')], 'openai')), ['z', 'a'])

    def test_stateless_public_projection_keeps_alphabetical_default(self):
        self.assertEqual(names(tool_declarations([spec('z'), spec('a')])), ['a', 'z'])
        self.assertEqual(names(tool_declarations([spec('z'), spec('a')], sort=False)), ['z', 'a'])


class AgentToolSurfaceTests(unittest.TestCase):
    def test_real_http_body_appends_discovered_schema_and_preserves_next_turn(self):
        captured, executed = [], []
        def opener(request, **kwargs):
            captured.append(json.loads(request.data))
            if len(captured) <= 2:
                name, args = ('tool_search', {'query': 'aaa_total', 'limit': 1}) if len(captured) == 1 else (
                    'aaa_total', {'a': 17, 'b': 25})
                message = {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'fixture-' + str(len(captured)), 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps(args)}}]}
                finish = 'tool_calls'
            else:
                message, finish = {'role': 'assistant', 'content': '42'}, 'stop'
            return io.BytesIO(json.dumps({'choices': [{'message': message, 'finish_reason': finish}]}).encode())
        def total(args, ctx):
            executed.append(args)
            return ToolResult(True, str(args['a'] + args['b']))
        registry = build_builtin_registry()
        registry.register(ToolSpec('aaa_total', 'Add integers.', total, deferred=True,
            schema={'a': 'integer', 'b': 'integer'}), trusted=True)
        router = ModelRouter([Provider('fixture', 'http://fixture.invalid', default_model='mimo-v2.6-pro')],
                             transport=HttpTransport(response_opener=opener), retries_per_provider=0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = Agent(home=root, workspace=root, router=router, registry=registry,
                policy=Policy(workspace=root, mode=Mode.READ_ONLY), limits=LoopLimits(max_steps=5))
            self.addCleanup(agent.secret_scope.close)
            head = agent.system_prompt()
            first = agent.run('discover aaa_total and add 17,25')
            self.assertEqual(first.stopped, 'final')
            self.assertEqual(executed, [{'a': 17, 'b': 25}])
            self.assertEqual(captured[0]['tools'], captured[1]['tools'][:-1])
            self.assertEqual(names(captured[1]['tools'])[-1], 'aaa_total')
            self.assertEqual(captured[1]['tools'], captured[2]['tools'])
            self.assertEqual(agent.system_prompt(), head)
            second = agent.run('continue', history=[{'role': 'user', 'content': 'discover aaa_total and add 17,25'},
                                                  {'role': 'assistant', 'content': first.text}])
            self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in second.events))
            self.assertEqual(captured[3]['messages'][:len(captured[2]['messages'])], captured[2]['messages'])


if __name__ == '__main__':
    unittest.main()
