"""Brain + specialists: catalog/expand, routing, handoff envelope, permission clamp, fallback."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from keystone import model as kmodel
from keystone.artifacts import ArtifactStore
from keystone.loop import Agent
from keystone.model import Usage
from keystone.policy import Mode, Policy
from keystone.retrieval_catalog import CatalogIndex, EvidenceLedger, build_catalog, render_catalog
from keystone.runtime_types import LoopLimits
from keystone.specialists import (Handoff, SpecialistTable, narrow_registry, parse_receipt,
                                  specialist_limits, specialist_policy)
from keystone.tools import ToolContext, build_builtin_registry

SECRET_BODY = 'ZEBRA-PAYLOAD-' + 'x' * 3000   # marker that must never reach the brain unexpanded


def call(name, args):
    return '<tool_call>' + json.dumps({'tool': name, 'args': args}, ensure_ascii=False) + '</tool_call>'


class Scripted:
    """One transport, one reply queue per model; records what each model saw."""

    def __init__(self, scripts):
        self.scripts = {name: list(replies) for name, replies in scripts.items()}
        self.seen = {name: [] for name in scripts}

    def complete(self, provider, model, messages, **options):
        self.seen[model].append(json.loads(json.dumps(messages, default=str)))
        queue = self.scripts[model]
        reply = queue.pop(0) if queue else 'done'
        if callable(reply):
            reply = reply(messages)
        return reply, Usage(prompt_tokens=10, completion_tokens=5, model=model, provider=provider.name), {}


def router_for(transport):
    brain = kmodel.Provider('brainp', 'http://127.0.0.1:9', 'k', 'openai', ('brain',), 'brain', 'brain')
    spec = kmodel.Provider('specp', 'http://127.0.0.1:9', 'k', 'openai', ('spec',), 'spec', 'spec')
    return kmodel.ModelRouter([brain, spec], transport=transport, primary=('brainp', 'brain'),
                              retries_per_provider=0)


def table(**domains):
    return SpecialistTable.from_mapping({'enabled': True, 'domains': domains})


class SpecialistCase(unittest.TestCase):
    def make(self, scripts, specialists=None, *, mode=Mode.ACCEPT_EDITS, deny=(), max_steps=10):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ws = root / 'ws'
        ws.mkdir()
        (ws / 'notes.md').write_text('# Notes\nline two\nThe launch date is 2026-03-14.\n' + SECRET_BODY,
                                     encoding='utf-8')
        (ws / 'other.md').write_text('unrelated text about llamas\n', encoding='utf-8')
        transport = Scripted(scripts)
        agent = Agent(home=root / 'home', workspace=ws, router=router_for(transport),
                      registry=build_builtin_registry(),
                      policy=Policy(workspace=ws, mode=mode, allow=('spawn_subagent',), deny=tuple(deny),
                                    non_interactive=True),
                      limits=LoopLimits(max_steps=max_steps), specialists=specialists)
        self.addCleanup(agent.secret_scope.close)
        return agent, transport


class TableTests(unittest.TestCase):
    def test_off_by_default_and_unconfigured_domains_absent(self):
        self.assertFalse(SpecialistTable.from_mapping({}).active)
        off = SpecialistTable.from_mapping({'enabled': False, 'domains': {'coding': {'model': ['p', 'm']}}})
        self.assertFalse(off.active)
        self.assertIsNone(off.route_for('coding'))
        t = table(coding={'model': ['p', 'm']}, math={})
        self.assertEqual(set(t.routes), {'coding'})
        self.assertIsNone(t.route_for('math'))
        self.assertIn('model3d', t.declared)
        self.assertIn('video', t.declared)

    def test_validation(self):
        for bad in ({'enabled': 'yes'}, {'enabled': True, 'domains': []},
                    {'enabled': True, 'domains': {'coding': {'model': ['p']}}},
                    {'enabled': True, 'domains': {'coding': {'model': ['p', 'm'], 'tools': ['spawn_subagent']}}},
                    {'enabled': True, 'domains': {'coding': {'model': ['p', 'm'], 'maxSteps': 0}}},
                    {'enabled': True, 'domains': {'coding': {'model': ['p', 'm'], 'mode': 'root'}}},
                    {'enabled': True, 'domains': {'Bad Name': {'model': ['p', 'm']}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SpecialistTable.from_mapping(bad)

    def test_auto_route_only_when_enabled_and_unambiguous(self):
        t = SpecialistTable.from_mapping({'enabled': True, 'autoRoute': True, 'domains': {
            'retrieval': {'model': ['p', 'm']}, 'coding': {'model': ['p', 'm']}}})
        self.assertEqual(t.route_for('', '帮我调研一下这几个资料来源').domain, 'retrieval')
        self.assertEqual(t.route_for('', 'fix the failing unit test').domain, 'coding')
        self.assertIsNone(t.route_for('', 'hello'))
        manual = table(retrieval={'model': ['p', 'm']})
        self.assertIsNone(manual.route_for('', '调研资料来源'))   # autoRoute off: brain must ask

    def test_shipped_bundle_declares_row_but_stays_off(self):
        from keystone.config import load_config
        base = Path(__file__).resolve().parent / 'bundles' / 'base.json'
        with tempfile.TemporaryDirectory() as home:
            config = load_config(home, bundles=[base], include_user_layer=False)
            self.assertIsNotNone(config.row('specialists'))
            self.assertFalse(SpecialistTable.from_config(config).active)

    def test_custom_domain_and_guidance(self):
        t = table(legal={'model': ['p', 'm'], 'tools': ['read_file'], 'label': '法务'})
        self.assertEqual(t.routes['legal'].tools, ('read_file',))
        self.assertIn('legal', t.guidance())
        self.assertEqual(SpecialistTable().guidance(), '')
        prefer = SpecialistTable.from_mapping({'enabled': True, 'delegation': 'prefer',
                                               'domains': {'coding': {'model': ['p', 'm']}}})
        self.assertIn('优先委派', prefer.guidance())
        with self.assertRaises(ValueError):
            SpecialistTable.from_mapping({'enabled': True, 'delegation': 'always'})


class PermissionTests(unittest.TestCase):
    def test_policy_clamped_and_deny_preserved(self):
        route = table(coding={'model': ['p', 'm'], 'mode': 'bypassPermissions'}).routes['coding']
        parent = Policy(workspace=Path('.'), mode=Mode.PLAN, deny=('shell_exec',), ask=('fetch_url',))
        child = specialist_policy(parent, route)
        self.assertEqual(child.mode, Mode.PLAN)
        self.assertEqual(child.deny, ('shell_exec',))
        self.assertEqual(child.ask, ('fetch_url',))
        self.assertEqual(specialist_policy(parent, route, 'bypassPermissions').mode, Mode.PLAN)

    def test_registry_narrowed_and_parent_untouched(self):
        parent = build_builtin_registry()
        before = set(s.name for s in parent.all_specs())
        child = narrow_registry(parent, ('read_file', 'grep', 'not_a_tool'))
        self.assertEqual({s.name for s in child.all_specs()}, {'read_file', 'grep'})
        self.assertEqual(child.search('shell'), [])
        self.assertFalse(child.activate('shell_exec'))
        self.assertEqual(set(s.name for s in parent.all_specs()), before)

    def test_steps_never_exceed_parent(self):
        route = table(coding={'model': ['p', 'm'], 'maxSteps': 50}).routes['coding']
        self.assertEqual(specialist_limits(LoopLimits(max_steps=7), route).max_steps, 7)
        route = table(coding={'model': ['p', 'm'], 'maxSteps': 3}).routes['coding']
        self.assertEqual(specialist_limits(LoopLimits(max_steps=7), route).max_steps, 3)


class EnvelopeTests(unittest.TestCase):
    def test_render_and_receipt(self):
        route = table(coding={'model': ['p', 'm']}).routes['coding']
        text = Handoff.from_args('fix add()', {'constraints': 'no new deps', 'prior': ['bug is in calc.py'],
                                               'acceptance': 'tests pass'}).render(route)
        for part in ('fix add()', 'no new deps', '- bug is in calc.py', 'tests pass', '"open_questions"'):
            self.assertIn(part, text)
        receipt = parse_receipt('done.\n```json\n{"result": "fixed", "evidence": ["calc.py:3"], '
                                '"open_questions": ["perf?"]}\n```', touched=['calc.py', 'x.py'])
        self.assertTrue(receipt['complied'])
        self.assertEqual(receipt['result'], 'fixed')
        self.assertEqual(receipt['evidence'], ['calc.py:3', 'changed: x.py'])
        self.assertEqual(receipt['open_questions'], ['perf?'])
        prose = parse_receipt('I fixed it.')
        self.assertFalse(prose['complied'])
        self.assertEqual(prose['result'], 'I fixed it.')


class CatalogUnitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'ws').mkdir()
        self.store = ArtifactStore(self.root / 'home', self.root / 'ws')
        self.ledger = EvidenceLedger(self.store)
        reg = narrow_registry(build_builtin_registry(), ('read_range',))
        self.ledger.wrap(reg, ('read_range',))
        (self.root / 'ws' / 'doc.txt').write_text('\n'.join(f'line {i}' for i in range(1, 200)), encoding='utf-8')
        ctx = ToolContext(policy=Policy(workspace=self.root / 'ws', mode=Mode.PLAN), workspace=self.root / 'ws')
        self.result = reg._specs['read_range'].handler({'path': 'doc.txt', 'start': 10, 'end': 120}, ctx)

    def test_ledger_tags_result_and_retains_full_text(self):
        self.assertTrue(self.result.content.startswith('[evidence e1 · doc.txt:10-120]'))
        row = self.ledger.rows[0]
        self.assertEqual(row['source'], 'doc.txt:10-120')
        text, _ = self.store._load(row['artifact'])
        self.assertIn('120\tline 120', text)

    def test_catalog_binds_ids_and_expand_reads_range(self):
        answer = json.dumps({'catalog': [
            {'evidence': 'e1', 'title': 'Doc', 'source': 'doc.txt', 'lines': [50, 60],
             'summary': 'lines about things', 'relevance': 'high'},
            {'title': 'Ghost', 'source': 'https://example.invalid/x', 'summary': 'not retrieved'}],
            'open_questions': ['is it current?']})
        catalog = build_catalog(answer, self.ledger, question='q')
        self.assertTrue(catalog['complied'])
        doc, ghost = catalog['entries']
        self.assertRegex(doc['id'], r'c_[0-9a-f]{10}\Z')
        self.assertEqual(doc['source'], 'doc.txt:50-60')
        self.assertEqual(doc['relevance'], 0.9)
        self.assertGreater(doc['chars'], 0)
        self.assertEqual(ghost['artifact'], '')
        shown = render_catalog(catalog)
        self.assertIn(doc['id'], shown)
        self.assertNotIn('line 100', shown)          # full text is not in the catalog
        self.assertIn('is it current?', shown)
        index = CatalogIndex(self.store)
        index.add(catalog['entries'])
        out = index.expand([doc['id'], ghost['id'], 'c_0000000000'])
        self.assertTrue(out['text'].startswith(f"[{doc['id']}] Doc — 来源: doc.txt:50-60"))
        self.assertIn('50\tline 50', out['text'])     # opens on the cited lines
        self.assertNotIn('10\tline 10', out['text'])
        self.assertEqual([r['found'] for r in out['entries']], [True, True, False])
        self.assertFalse(out['entries'][1]['retained'])
        again = CatalogIndex(self.store).expand(doc['id'], offset=0, limit=50)   # survives a rebuilt index
        self.assertIn('10\tline 10', again['text'])
        self.assertIsNotNone(again['entries'][0]['next_offset'])
        with self.assertRaises(ValueError):
            index.expand([])

    def test_prose_answer_falls_back_to_ledger(self):
        catalog = build_catalog('I read doc.txt and it has lines.', self.ledger, question='q')
        self.assertFalse(catalog['complied'])
        self.assertEqual(len(catalog['entries']), 1)
        self.assertEqual(catalog['entries'][0]['evidence'], 'e1')


class EndToEndTests(SpecialistCase):
    def retrieval_table(self, **extra):
        return table(retrieval={'model': ['specp', 'spec'], **extra})

    def test_retrieval_catalog_then_expand(self):
        catalog_ids = []

        def brain_after_catalog(messages):
            content = messages[-1]['content'] if isinstance(messages[-1]['content'], str) else json.dumps(messages[-1])
            import re
            catalog_ids.extend(re.findall(r'c_[0-9a-f]{10}', content))
            return call('catalog_expand', {'ids': catalog_ids[:1]})

        spec_answer = json.dumps({'catalog': [{'evidence': 'e1', 'title': 'Launch notes', 'source': 'notes.md',
                                               'lines': [3, 3], 'summary': 'States the launch date.',
                                               'relevance': 0.95}]})
        agent, t = self.make({
            'brain': [call('spawn_subagent', {'task': 'When is the launch?', 'domain': 'retrieval',
                                              'acceptance': 'a dated source'}),
                      brain_after_catalog, 'Launch is 2026-03-14 [c_… notes.md:3].'],
            'spec': [call('read_file', {'path': 'notes.md'}), spec_answer]},
            self.retrieval_table())
        report = agent.run('When is the launch? Cite sources.')
        self.assertEqual(report.stopped, 'final')
        spawn, expand = report.steps[0], report.steps[1]
        self.assertEqual(spawn.tool, 'spawn_subagent')
        self.assertEqual(spawn.meta['specialist']['domain'], 'retrieval')
        self.assertEqual(spawn.meta['catalog'][0]['source'], 'notes.md:3-3')
        # the specialist used its own model, the brain never saw the full text before expanding
        self.assertEqual(len(t.seen['spec']), 2)
        self.assertIn('[专家交接 handoff · 领域 retrieval', json.dumps(t.seen['spec'][0], ensure_ascii=False))
        brain_before_expand = json.dumps(t.seen['brain'][:2], ensure_ascii=False)
        self.assertNotIn('ZEBRA-PAYLOAD', brain_before_expand)
        self.assertIn('Launch notes', brain_before_expand)
        self.assertEqual(expand.tool, 'catalog_expand')
        self.assertIn('2026-03-14', expand.result)
        self.assertIn('来源: notes.md:3-3', expand.result)
        kinds = [e['type'] for e in agent.events]
        self.assertIn('specialist_route', kinds)
        self.assertIn('specialist_catalog', kinds)
        # the specialist's surface is retrieval-only
        route_event = next(e for e in agent.events if e['type'] == 'specialist_route')
        self.assertNotIn('write_file', route_event['tools'])
        self.assertNotIn('spawn_subagent', route_event['tools'])
        self.assertEqual(route_event['mode'], 'plan')

    def test_parent_deny_wins_inside_specialist(self):
        agent, t = self.make({
            'brain': [call('spawn_subagent', {'task': 'read notes', 'domain': 'retrieval'}), 'ok'],
            'spec': [call('read_file', {'path': 'notes.md'}), '{"catalog": []}']},
            self.retrieval_table(), deny=('read_file',))
        agent.run('go')
        denied = [e for e in agent.events if e.get('type') == 'tool_call'
                  and e.get('agent') == 'keystone/retrieval' and e.get('decision') == 'deny']
        self.assertEqual(len(denied), 1)
        spec_view = json.dumps(t.seen['spec'][-1], ensure_ascii=False)
        self.assertIn('denied by policy: read_file', spec_view)
        self.assertNotIn('ZEBRA-PAYLOAD', spec_view)

    def test_specialist_cannot_write_in_plan_mode_parent(self):
        coding = table(coding={'model': ['specp', 'spec'], 'mode': 'bypassPermissions'})
        agent, _ = self.make({
            'brain': [call('spawn_subagent', {'task': 'write x', 'domain': 'coding'}), 'ok'],
            'spec': [call('write_file', {'path': 'x.txt', 'content': 'hi'}),
                     '{"result": "tried", "evidence": [], "open_questions": []}']},
            coding, mode=Mode.PLAN)
        agent.run('go')
        self.assertFalse((agent.workspace / 'x.txt').exists())
        route_event = next(e for e in agent.events if e['type'] == 'specialist_route')
        self.assertEqual(route_event['mode'], 'plan')

    def test_coding_receipt_and_shared_budgets(self):
        coding = table(coding={'model': ['specp', 'spec'], 'maxSteps': 3})
        agent, t = self.make({
            'brain': [call('spawn_subagent', {'task': 'create x.txt', 'domain': 'coding',
                                              'constraints': 'only x.txt', 'acceptance': 'file exists'}), 'ok'],
            'spec': [call('write_file', {'path': 'x.txt', 'content': 'hi'}),
                     '{"result": "wrote x.txt", "evidence": ["x.txt:1"], "open_questions": ["style?"]}']},
            coding)
        budget = agent.budget[0]
        report = agent.run('go')
        self.assertEqual((agent.workspace / 'x.txt').read_text(), 'hi')
        step = report.steps[0]
        self.assertEqual(step.meta['handoff']['result'], 'wrote x.txt')
        self.assertEqual(step.meta['handoff']['evidence'], ['x.txt:1'])   # cited path not duplicated
        self.assertIn('[专家回执 handoff result · 领域 coding · 模型 specp/spec', step.result)
        self.assertIn('style?', step.result)
        self.assertEqual(agent.budget[0], budget - 1)
        route_event = next(e for e in agent.events if e['type'] == 'specialist_route')
        self.assertEqual(route_event['max_steps'], 3)
        self.assertIn('only x.txt', json.dumps(t.seen['spec'][0], ensure_ascii=False))
        self.assertGreater(agent._child_tokens, 0)       # specialist tokens roll into the brain's run

    def test_unconfigured_domain_falls_back_to_plain_subagent(self):
        agent, t = self.make({
            'brain': [call('spawn_subagent', {'task': 'compute', 'domain': 'math'}), 'child says 4', 'done'],
            'spec': []}, self.retrieval_table())
        report = agent.run('go')
        self.assertEqual(report.steps[0].decision, 'ok')
        self.assertEqual(t.seen['spec'], [])            # brain's own model did the work
        self.assertIn('specialist_fallback', [e['type'] for e in agent.events])
        self.assertNotIn('specialist', report.steps[0].meta)

    def test_unknown_model_falls_back(self):
        broken = table(retrieval={'model': ['specp', 'not-listed']})
        agent, t = self.make({'brain': [call('spawn_subagent', {'task': 'look', 'domain': 'retrieval'}),
                                        'child', 'done'], 'spec': []}, broken)
        agent.run('go')
        fallback = next(e for e in agent.events if e['type'] == 'specialist_fallback')
        self.assertEqual(fallback['reason'], 'model_unavailable')
        self.assertEqual(t.seen['spec'], [])

    def test_require_moves_domain_tools_from_brain_to_specialist(self):
        strict = SpecialistTable.from_mapping({'enabled': True, 'delegation': 'require',
                                               'domains': {'retrieval': {'model': ['specp', 'spec']}}})
        spec_answer = json.dumps({'catalog': [{'evidence': 'e1', 'title': 'Notes', 'source': 'notes.md',
                                               'summary': 'launch date', 'relevance': 1}]})
        agent, t = self.make({'brain': [call('read_file', {'path': 'notes.md'}),
                                        call('spawn_subagent', {'task': 'launch date?', 'domain': 'retrieval'}),
                                        'done'],
                              'spec': [call('read_file', {'path': 'notes.md'}), spec_answer]}, strict)
        brain_tools = set(agent.registry.names())
        self.assertNotIn('read_file', brain_tools)
        self.assertNotIn('grep', brain_tools)
        self.assertTrue({'spawn_subagent', 'catalog_expand'} <= brain_tools)
        self.assertIn('write_file', brain_tools)        # not owned by a configured domain
        self.assertIn('必须委派', agent.run_envelope())
        report = agent.run('go')
        self.assertNotEqual(report.steps[0].decision, 'ok')          # brain cannot read itself
        self.assertEqual(report.steps[1].meta['specialist']['domain'], 'retrieval')
        self.assertEqual(len(t.seen['spec']), 2)                     # specialist could read

    def test_feature_off_is_todays_behaviour(self):
        agent, t = self.make({'brain': [call('spawn_subagent', {'task': 'look', 'domain': 'retrieval'}),
                                        'child', 'done'], 'spec': []}, SpecialistTable())
        self.assertIsNone(agent.specialists)
        self.assertIsNone(agent.catalog_index)
        self.assertNotIn('catalog_expand', [s.name for s in agent.registry.available_specs(agent._tool_context())])
        self.assertNotIn('[专家路由]', agent.run_envelope())
        agent.run('go')
        self.assertEqual(t.seen['spec'], [])
        self.assertNotIn('specialist_route', [e['type'] for e in agent.events])


if __name__ == '__main__':
    unittest.main()
