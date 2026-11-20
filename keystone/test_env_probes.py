"""Independent environment acceptance through the real Agent lifecycle."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from .acceptance import AcceptanceContract
from .loop import Agent, LoopLimits
from .policy import Policy, Mode
from .probes import ProbeRegistry, json_value
from .test_task_planning import Router
from .tools import build_builtin_registry


def criterion(**values):
    return {'id': 'environment', 'kind': 'env_probe', 'env': 'workspace',
            'probe': 'json_value', 'input': {'path': 'result.json', 'pointer': '/ready'},
            'expect': True, **values}


def contract(**values):
    return {'max_attempts': 1, 'criteria': [criterion(**values)]}


class EnvironmentProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'project'
        self.ws.mkdir()
        self.file = self.ws / 'result.json'
        self.file.write_text('{"ready":false}', encoding='utf-8')

    def agent(self, *responses, probes=None, deny=(), cancellation=None):
        return Agent(home=self.root / 'home', workspace=self.ws,
            policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS,
                          deny=deny, non_interactive=True),
            registry=build_builtin_registry(), router=Router(responses or ['done']),
            limits=LoopLimits(max_steps=6), project_rules=False, probes=probes,
            cancellation_event=cancellation)

    def result(self, report):
        return next(e for e in reversed(report.events) if e['type'] == 'acceptance_checked')

    def test_model_claim_cannot_override_actual_state(self):
        agent = self.agent('Everything is done and ready=true')
        report = agent.run('finish', acceptance=contract())
        self.assertEqual(report.stopped, 'acceptance_failed')
        result = self.result(report)['results'][0]
        self.assertFalse(result['passed'])
        self.assertIs(result['evidence']['actual'], False)
        self.assertTrue(result['evidence']['files'][0]['sha256'])
        self.assertEqual(agent.task_contract.status, 'blocked')

    def test_failed_probe_repairs_then_observes_fresh_state(self):
        agent = self.agent('done', json.dumps({'tool': 'write_file', 'args': {
            'path': 'result.json', 'content': '{"ready":true}'}}), 'done')
        report = agent.run('finish', acceptance={**contract(), 'max_attempts': 3})
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(agent.task_contract.status, 'ready_to_deliver')
        observed = [e['results'][0]['evidence']['actual'] for e in report.events
                    if e['type'] == 'acceptance_checked']
        self.assertEqual(observed, [False, True])

    def test_json_comparison_distinguishes_bool_number_and_string(self):
        for actual in [1, 'true', None, []]:
            self.file.write_text(json.dumps({'ready': actual}), encoding='utf-8')
            self.assertEqual(self.agent().run('finish', acceptance=contract()).stopped, 'acceptance_failed')

    def test_json_pointer_escaping_arrays_and_null(self):
        self.file.write_text('{"a/b":{"~key":[null,true]}}', encoding='utf-8')
        report = self.agent().run('finish', acceptance=contract(
            input={'path': 'result.json', 'pointer': '/a~1b/~0key/0'}, expect=None))
        self.assertEqual(report.stopped, 'final')

    def test_missing_fields_and_duplicate_or_invalid_json_fail(self):
        for text in ['{"missing":true}', '{"ready":true,"ready":false}', '{"ready":NaN}', 'broken']:
            self.file.write_text(text, encoding='utf-8')
            report = self.agent().run('finish', acceptance=contract())
            self.assertEqual(report.stopped, 'acceptance_failed')

    def test_digest_matches_exact_bytes_including_bom_and_newlines(self):
        data = b'\xef\xbb\xbf{"ready":true}\r\n'
        self.file.write_bytes(data)
        report = self.agent().run('finish', acceptance=contract(probe='file_digest',
            input={'path': 'result.json'}, expect=hashlib.sha256(data).hexdigest()))
        self.assertEqual(report.stopped, 'final')

    def test_policy_denial_never_becomes_probe_success(self):
        self.file.write_text('{"ready":true}', encoding='utf-8')
        report = self.agent(deny=('read_file',)).run('finish', acceptance=contract())
        self.assertEqual(report.stopped, 'acceptance_failed')
        self.assertEqual(self.result(report)['results'][0]['evidence']['status'], 'error')

    def test_unregistered_probe_and_bad_input_rejected_before_model(self):
        for raw in [contract(env='unregistered'), contract(probe='exec'),
                    contract(input={'path': '../secret'}), contract(input={'path': 'result.json', 'extra': True})]:
            agent = self.agent()
            with self.assertRaises(ValueError):
                agent.run('finish', acceptance=raw)
            self.assertFalse(agent.router.calls)

    def test_schema_roundtrip_and_limits(self):
        raw = contract(expect={'nested': [True, None, 2]})
        parsed = AcceptanceContract.from_raw(raw)
        self.assertEqual(AcceptanceContract.from_raw(parsed.to_raw()), parsed)
        for bad in [float('nan'), {'non_json': object()}, 'x'*32769]:
            with self.assertRaises(ValueError):
                AcceptanceContract.from_raw(contract(expect=bad))
        for bad in [contract(timeout=True), contract(timeout=0), contract(input=[]),
                    contract(env='../unsafe')]:
            with self.assertRaises(ValueError):
                AcceptanceContract.from_raw(bad)
        with self.assertRaises(ValueError):
            json_value([[[[[[[[[[[[[[[[[None]]]]]]]]]]]]]]]]])

    def test_custom_probe_is_host_registered_and_receives_run_context(self):
        observations = []
        def observe(ctx, args):
            observations.append(ctx.run_id)
            return {'url': 'http://local.test/done', 'count': args['expected_count']}
        probes = ProbeRegistry.defaults().with_probe('browser', 'state', observe)
        report = self.agent(probes=probes).run('finish', acceptance=contract(env='browser', probe='state',
            input={'expected_count': 2}, expect={'count': 2, 'url': 'http://local.test/done'}))
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(observations, [report.run_id])
        with self.assertRaises(ValueError):
            probes.with_probe('browser', 'state', observe)

    def test_probe_exception_and_non_json_result_do_not_pass(self):
        for callback in [lambda c, i: object(), lambda c, i: 1/0]:
            probes = ProbeRegistry.defaults().with_probe('fixture', 'state', callback)
            report = self.agent(probes=probes).run('finish', acceptance=contract(env='fixture',
                probe='state', input={}, expect=True))
            self.assertEqual(report.stopped, 'acceptance_failed')

    def test_registry_replacement_during_run_cannot_weaken_verification(self):
        probes = ProbeRegistry.defaults().with_probe('fixture', 'state', lambda c, i: False)
        agent = self.agent(probes=probes)
        class ReplaceRouter(Router):
            def complete(self, messages, **options):
                agent.probes = ProbeRegistry.defaults().with_probe('fixture', 'state', lambda c, i: True)
                return super().complete(messages, **options)
        agent.router = ReplaceRouter(['done'])
        report = agent.run('finish', acceptance=contract(env='fixture', probe='state', input={}))
        self.assertEqual(report.stopped, 'acceptance_failed')

    def test_timeout_stops_without_replay_and_blocks_busy_probe(self):
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        def observe(ctx, args):
            entered.set()
            try:
                release.wait(5)
                return True
            finally:
                finished.set()
        probes = ProbeRegistry.defaults().with_probe('fixture', 'slow', observe)
        self.addCleanup(release.set)
        raw = contract(env='fixture', probe='slow', input={}, timeout=1)
        first = self.agent(probes=probes).run('finish', acceptance=raw)
        self.assertTrue(entered.is_set())
        self.assertEqual(first.stopped, 'unknown_outcome')
        second = self.agent(probes=probes).run('finish', acceptance=raw)
        self.assertEqual(self.result(second)['results'][0]['evidence']['status'], 'busy')
        release.set()
        self.assertTrue(finished.wait(2))

    def test_cancel_during_observation_cannot_deliver(self):
        cancel = threading.Event()
        def observe(ctx, args):
            cancel.set()
            return True
        probes = ProbeRegistry.defaults().with_probe('fixture', 'state', observe)
        report = self.agent(probes=probes, cancellation=cancel).run('finish', acceptance=contract(
            env='fixture', probe='state', input={}))
        self.assertEqual(report.stopped, 'cancelled')

    def test_sensitive_probe_evidence_is_redacted_before_log_and_feedback(self):
        secret = 'fixture-api-key-7349308'
        self.file.write_text(json.dumps({'api_key': secret, 'ready': False}), encoding='utf-8')
        report = self.agent().run('finish', acceptance=contract(input={'path': 'result.json'}, expect={}))
        self.assertNotIn(secret, json.dumps(report.events))


if __name__ == '__main__':
    unittest.main()
