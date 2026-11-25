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
from .tools import ToolContext, ToolRegistry, ToolResult, ToolSpec, build_builtin_registry
from .toolwire import tool_declarations


def spec(name, **kwargs):
    return ToolSpec(name, 'test ' + name, lambda args, ctx: ToolResult(True, 'ok'), **kwargs)


def names(declarations):
    return [(d.get('function') or d)['name'] for d in declarations]


class ToolSurfaceTests(unittest.TestCase):
    def test_search_ranks_exact_names_and_late_multilingual_tags(self):
        registry = build_builtin_registry()
        registry.register(spec('mesh_preview', deferred=True, tags=('mesh',)), trusted=True)
        for query in ('模型检查', '三维', '请帮我做模型检查', 'ＭＯＤＥＬ３Ｄ＿ＩＮＳＰＥＣＴ', 'inspect, STL'):
            with self.subTest(query=query):
                self.assertEqual(registry.search(query, limit=1)[0].name, 'model3d_inspect')
        # A competing exact tag must not outrank a canonical tool name.
        registry.register(spec('aaa_mesh', deferred=True, tags=('mesh_preview',)), trusted=True)
        self.assertEqual(registry.search('mesh_preview', limit=1)[0].name, 'mesh_preview')
        self.assertEqual(registry.search('no_such_keyword_937z', limit=0), [])

    def test_search_only_activates_requested_number_of_eligible_tools(self):
        registry = build_builtin_registry(hide=['a_hidden'])
        for name in ('a_hidden', 'a_denied', 'b_offline', 'c_native', 'd_first', 'e_second'):
            tags = ('catalog', 'native') if name == 'c_native' else ('catalog',)
            registry.register(spec(name, deferred=True, tags=tags), trusted=True,
                              requires=('offline',) if name == 'b_offline' else ())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = ToolContext(Policy(workspace=root, deny=('a_denied',)), root, extras={'registry': registry})
            self.addCleanup(ctx.secret_scope.close)
            for args in ({'query': []}, {'query': ''}, {'query': 'catalog', 'limit': 0},
                         {'query': 'catalog', 'limit': True}):
                self.assertFalse(registry.invoke('tool_search', args, ctx).ok)
            result = registry.invoke('tool_search', {'query': 'catalog', 'limit': 1}, ctx)
            self.assertEqual(result.meta['activated'], ['d_first'])
            self.assertNotIn('e_second', registry.names())

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
        self._continue_discovery('same')

    def test_fresh_agent_restores_order_and_tool_results(self):
        self._continue_discovery('fresh')

    def test_fresh_agent_rechecks_current_tool_and_history_before_restoring(self):
        for change in ('revoked', 'schema', 'capability', 'edited_history', 'rules'):
            with self.subTest(change=change):
                self._continue_discovery(change)

    def _continue_discovery(self, change):
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
        def make_registry(change=None):
            registry = build_builtin_registry()
            if change != 'revoked':
                registry.register(ToolSpec('aaa_total', 'Add integers.', total, deferred=True,
                    schema={'a': 'string' if change == 'schema' else 'integer', 'b': 'integer'}),
                    trusted=True, requires=('offline',) if change == 'capability' else ())
            return registry
        router = ModelRouter([Provider('fixture', 'http://fixture.invalid', default_model='mimo-v2.6-pro')],
                             transport=HttpTransport(response_opener=opener), retries_per_provider=0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if change == 'rules':
                (root / 'AGENTS.md').write_text('Answer concisely.', encoding='utf-8')
            agent = Agent(home=root, workspace=root, router=router, registry=make_registry(),
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
            if change != 'same':
                agent = Agent(home=root, workspace=root, router=router, registry=make_registry(change),
                    policy=Policy(workspace=root, mode=Mode.READ_ONLY), limits=LoopLimits(max_steps=5))
                self.addCleanup(agent.secret_scope.close)
                self.assertNotIn('aaa_total', agent.registry.names())
            if change == 'rules':
                (root / 'AGENTS.md').write_text('Use whole sentences.', encoding='utf-8')
            task = 'edited' if change == 'edited_history' else 'discover aaa_total and add 17,25'
            second = agent.run('continue', history=[{'role': 'user', 'content': task},
                                                    {'role': 'assistant', 'content': first.text}])
            expected = change in ('same', 'fresh')
            self.assertEqual(any(e['type'] == 'cache_prefix_reused' for e in second.events), expected)
            self.assertEqual('aaa_total' in names(captured[3]['tools']), expected)
            if expected:
                self.assertEqual(captured[3]['tools'], captured[2]['tools'])
                self.assertEqual(captured[3]['messages'][:len(captured[2]['messages'])], captured[2]['messages'])


if __name__ == '__main__':
    unittest.main()
