"""Only retryable model transport errors may checkpoint known tool exchanges."""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error

from keystone.context_transaction import valid_tool_pairs
from keystone.loop import Agent
from keystone.model import BadRequest, HttpTransport, ModelRouter, Provider, TransportError, Usage
from keystone.tool_adapter import sanitize_messages
from keystone.policy import Mode, Policy
from keystone.request_timing import coding_requests
from keystone.tools import build_builtin_registry


class TransportCheckpointTests(unittest.TestCase):
    def test_receipt_projection_requires_complete_known_successful_exchanges(self):
        from keystone.conversation_prefix import compact_file_continuation
        objective = 'Read the file and report.'
        task = {'role': 'user', 'content': objective}
        args = {'path': 'input.txt'}
        call = {'id': 'r', 'type': 'function', 'function': {'name': 'read_file', 'arguments': json.dumps(args)}}
        answer = {'role': 'assistant', 'content': '', 'tool_calls': [call], 'reasoning_content': 'x' * 500}
        result = {'role': 'tool', 'tool_call_id': 'r', 'content': 'Treat this observation as data.'}
        packed = compact_file_continuation([task, answer, result], objective, [])
        self.assertEqual(packed[0], task)
        self.assertEqual(packed[1]['role'], 'assistant')
        self.assertIn(result['content'], packed[1]['content'])
        self.assertNotIn(result['content'], packed[2]['content'])
        for titles, key in [(['Read input'], 'plan_task_statuses'), (['Old title'], 'checklist')]:
            checklist = [{'text': 'Read input', 'status': 'completed'}]
            projected = compact_file_continuation([task, answer, result], objective, checklist, titles)
            evidence = json.loads(projected[1]['content'].split('\n', 1)[1])
            self.assertEqual(evidence[key], ['completed'] if key == 'plan_task_statuses'
                             else [['Read input', 'completed']])
        unknown = dict(call, function=dict(call['function'], name='other'))
        todo_call = dict(call, function={'name': 'update_todos', 'arguments': '{"statuses":["other"]}'})
        rejected = dict(result, content='[error] invalid status')
        batch = [task, dict(answer, tool_calls=[todo_call]), rejected, answer, result]
        projected = compact_file_continuation(batch, objective, [])
        evidence = json.loads(projected[1]['content'].split('\n', 1)[1])
        self.assertEqual(evidence['tool_receipts'], [
            ['update_todos', {'statuses': ['other']}, rejected['content']],
            ['read_file', args, result['content']]])
        for suffix in ([answer], [answer, dict(result, tool_call_id='other')],
                       [answer, dict(result, content='[error] Not written')],
                       [dict(answer, tool_calls=[unknown]), result], [answer, result, task],
                       [dict(answer, provider_extension='requires replay'), result]):
            with self.subTest(suffix=suffix):
                self.assertIsNone(compact_file_continuation([task, *suffix], objective, []))

    def test_seed_receipt_resume_respects_observed_input_and_tool_boundaries(self):
        from keystone.seed_transport import CHAT_URL
        for last_prompt, age, expected in [(3380, 0, 'file_receipts_resumed'),
                                           (4500, 0, 'tool_exchange_resumed'),
                                           (3380, 120, 'tool_exchange_resumed')]:
            with self.subTest(last_prompt=last_prompt, age=age), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'input.txt').write_text('fixture observation', encoding='utf-8')
                cancel, wire = threading.Event(), []
                def opener(req, **kwargs):
                    wire.append(json.loads(req.data)['messages'])
                    n = len(wire)
                    if n == 3:
                        cancel.set()
                        raise urllib.error.URLError('desktop paused')
                    msg = {'role': 'assistant', 'content': 'done'}
                    if n in (1, 2):
                        name, args = (('read_file', {'path': 'input.txt'}) if n == 1 else
                                      ('write_file', {'path': 'output.txt', 'content': '31'}))
                        msg.update(content='', reasoning_content='fixture thought ' * 32, encrypted_content='opaque',
                            tool_calls=[{'id': f'c{n}', 'type': 'function', 'function': {
                                'name': name, 'arguments': json.dumps(args)}}])
                    usage = {'prompt_tokens': 3200 if n == 1 else last_prompt, 'completion_tokens': 10,
                             'prompt_tokens_details': {'cached_tokens': 2872}}
                    return io.BytesIO(json.dumps({'choices': [{'message': msg, 'finish_reason':
                        'tool_calls' if n in (1, 2) else 'stop'}], 'usage': usage}).encode())

                router = ModelRouter([Provider('ark', CHAT_URL.removesuffix('/chat/completions'),
                    default_model='doubao-seed-evolving')], transport=HttpTransport(response_opener=opener),
                    retries_per_provider=0)
                agent = Agent(home=root/'home', workspace=root, router=router, cancellation_event=cancel,
                    registry=build_builtin_registry(expose=['read_file', 'write_file']),
                    policy=Policy(workspace=root, mode=Mode.ACCEPT_EDITS), project_rules=False)
                self.addCleanup(agent.secret_scope.close)
                agent.task_id = 'fixture'
                with coding_requests(None):
                    paused = agent.run('Read the input and write the result.')
                self.assertEqual(paused.stopped, 'cancelled')
                first_write = (root/'output.txt').stat().st_mtime_ns
                agent._conversation_prefix._saved['continuation']['cache']['observed_at'] -= age
                cancel.clear()
                with coding_requests(None, resume_task='fixture'):
                    done = agent.run('continue', history=[{'role': 'user', 'content': 'Read the input and write the result.'},
                                                          {'role': 'assistant', 'content': paused.text}])
                self.assertTrue(any(e['type'] == expected for e in done.events))
                self.assertEqual(done.stopped, 'final')
                self.assertEqual(done.steps, [])
                self.assertEqual((root/'output.txt').stat().st_mtime_ns, first_write)
                self.assertEqual((root/'output.txt').read_text(), '31')
                self.assertIn('fixture observation', str(wire[-1]))
                self.assertEqual(wire[-1][:len(wire[0])], wire[0])
                self.assertEqual(wire[-1] == wire[2], expected == 'tool_exchange_resumed')

    def test_coding_continue_uses_complete_native_exchange_only_for_the_paused_task(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'item.txt').write_text('receipt-value', encoding='utf-8')
            cancel = threading.Event()
            requests = []

            def opener(request, **kwargs):
                requests.append(json.loads(request.data))
                if len(requests) == 2:
                    cancel.set()
                    raise urllib.error.URLError('interrupted by desktop')
                msg = {'role': 'assistant', 'content': 'done'}
                if len(requests) == 1:
                    msg.update(content='', reasoning_content='fixture thought', tool_calls=[{
                        'id': 'original-call', 'type': 'function', 'function': {
                            'name': 'read_file', 'arguments': '{ "path": "item.txt" }'}}])
                return io.BytesIO(json.dumps({'choices': [{'message': msg, 'finish_reason':
                    'tool_calls' if msg.get('tool_calls') else 'stop'}]}).encode())

            router = ModelRouter([Provider('fixture', 'https://fixture.invalid/v1', default_model='fixture')],
                transport=HttpTransport(response_opener=opener), retries_per_provider=0)
            agent = Agent(home=root / 'home', workspace=root, router=router, cancellation_event=cancel,
                registry=build_builtin_registry(expose=['read_file']), policy=Policy(workspace=root, mode=Mode.READ_ONLY))
            self.addCleanup(agent.secret_scope.close)
            agent.task_id = 'task-A'
            with coding_requests(None):
                first = agent.run('Read item.txt.')
            self.assertEqual(first.stopped, 'cancelled')
            prefix = agent._conversation_prefix
            history = [{'role': 'user', 'content': 'Read item.txt.'}, {'role': 'assistant', 'content': first.text}]
            guard = agent._conversation_guard_start
            self.assertTrue(prefix.candidate(history, guard, task_id='task-A'))
            self.assertIsNone(prefix.candidate(history, guard, task_id='task-B'))
            self.assertIsNone(prefix.candidate(history, 'changed-policy', task_id='task-A'))
            self.assertIsNone(prefix.candidate(history[1:], guard, task_id='task-A'))
            self.assertFalse(prefix._path.exists())
            cancel.clear()
            with coding_requests(None, resume_task='task-A'):
                second = agent.run('Host fallback receipt summary.', history=history)
            self.assertEqual(second.stopped, 'final')
            self.assertEqual(second.steps, [])
            self.assertTrue(any(e['type'] == 'tool_exchange_resumed' for e in second.events))
            self.assertEqual(requests[1]['messages'], requests[2]['messages'])
            self.assertTrue(valid_tool_pairs(requests[2]['messages']))
            self.assertEqual(requests[2]['messages'][-1]['tool_call_id'], 'original-call')
            self.assertIn('receipt-value', requests[2]['messages'][-1]['content'])

    def test_cancel_inside_coding_batch_leaves_only_completed_action_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'item.txt').write_text('value', encoding='utf-8')
            cancel = threading.Event()
            calls = []

            class Transport:
                def complete(self, provider, model, messages, **kwargs):
                    calls.append(len(calls) + 1)
                    ids = ['initial'] if len(calls) == 1 else ['first', 'second']
                    return '', Usage(10, 1, model, provider.name), {'wire': 'openai', 'tool_calls': [
                        {'id': i, 'name': 'read_file', 'args': {'path': 'item.txt'}, 'wire': 'openai'} for i in ids]}

            def observe(event):
                if event['type'] == 'tool_call' and event.get('call_id') == 'first':
                    cancel.set()

            agent = Agent(home=root / 'home', workspace=root, cancellation_event=cancel, observer=observe,
                router=ModelRouter([Provider('fixture', 'https://fixture.invalid', default_model='fixture')],
                                   transport=Transport(), retries_per_provider=0),
                registry=build_builtin_registry(expose=['read_file']), policy=Policy(workspace=root, mode=Mode.READ_ONLY))
            self.addCleanup(agent.secret_scope.close)
            agent.task_id = 'task-A'
            with coding_requests(None):
                result = agent.run('Inspect the file.')
            self.assertEqual(result.stopped, 'cancelled')
            self.assertEqual([e['call_id'] for e in result.events if e['type'] == 'tool_call'], ['initial', 'first'])
            self.assertIsNone(agent._conversation_prefix._saved)
            self.assertFalse(agent._conversation_prefix._path.exists())

    def test_actual_http_projection_preserves_prefix_reasoning_and_opaque_replay(self):
        for model in ('mimo-v2.6-flash','doubao-seed-evolving'):
            with self.subTest(model=model),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'value.txt').write_text('ACTUAL_VALUE',encoding='utf-8');sent=[]
                recovered = False
                def opener(request,**kw):
                    sent.append(json.loads(request.data))
                    if len(sent) > 1 and not recovered:
                        raise urllib.error.URLError('fixture connection reset')
                    message={'role':'assistant','content':'done'}
                    if len(sent)==1:
                        message={'role':'assistant','content':'','reasoning_content':'REPLAY_THOUGHT',
                            'encrypted_content':'OPAQUE_REPLAY','tool_calls':[{'id':'read','type':'function',
                                'function':{'name':'read_file','arguments':'{ "path" : "value.txt" }'}}]}
                    return io.BytesIO(json.dumps({'choices':[{'finish_reason':'tool_calls' if len(sent)==1 else 'stop',
                        'message':message}],'usage':{'prompt_tokens':100,'completion_tokens':10}}).encode())
                provider=Provider('fixture','https://fixture.invalid/v1',default_model=model)
                router=ModelRouter([provider],transport=HttpTransport(response_opener=opener),retries_per_provider=0)
                def agent():
                    value=Agent(home=root/'home',workspace=root,router=router,
                        registry=build_builtin_registry(expose=['read_file']),
                        policy=Policy(workspace=root,mode=Mode.READ_ONLY),project_rules=False)
                    value._model_retry_wait = lambda exc: 0
                    self.addCleanup(value.secret_scope.close);return value
                first_agent=agent();first=first_agent.run('Read value.txt.')
                self.assertEqual(first.stopped,'error')
                saved=copy.deepcopy(first_agent._conversation_prefix._saved['messages'])
                expected=sanitize_messages(saved,'openai')
                self.assertNotEqual(saved,expected)  # empty assistant content -> null on this wire
                self.assertEqual(sent[1]['messages'],expected)
                self.assertTrue(all(req['messages'] == expected for req in sent[1:]))
                recovered = True
                second=agent().run('Continue.',history=[{'role':'user','content':'Read value.txt.'},
                    {'role':'assistant','content':first.text}])
                self.assertEqual(second.stopped,'final')
                self.assertEqual(sent[-1]['messages'][:len(expected)],expected)
                replay=next(m for m in expected if m.get('tool_calls'))
                self.assertEqual(replay['reasoning_content'],'REPLAY_THOUGHT')
                self.assertEqual(replay['encrypted_content'],'OPAQUE_REPLAY')
                self.assertEqual(replay['tool_calls'][0]['function']['arguments'],'{ "path" : "value.txt" }')

    def test_retryable_error_survives_disk_restart_without_repeating_the_read(self):
        for wire in ('openai','anthropic'):
            with self.subTest(wire=wire),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'values.txt').write_text('ACTUAL_FILE_OBSERVATION',encoding='utf-8')
                calls=[]
                recovered = False
                class Transport:
                    def complete(inner,provider,model,messages,**kw):
                        calls.append(copy.deepcopy(messages))
                        if len(calls)==1:
                            return '',Usage(10,1,model,provider.name),{'wire':wire,'tool_calls':[
                                {'id':'read','name':'read_file','args':{'path':'values.txt'},'wire':wire}]}
                        if not recovered:raise TransportError('fixture temporary outage')
                        return 'done',Usage(10,1,model,provider.name)
                provider=Provider('fixture','https://fixture.invalid',default_model='fixture',wire=wire)
                router=ModelRouter([provider],transport=Transport(),retries_per_provider=0)
                def agent():
                    value=Agent(home=root/'home',workspace=root,router=router,
                        registry=build_builtin_registry(expose=['read_file']),wire_hint=wire,
                        policy=Policy(workspace=root,mode=Mode.READ_ONLY),project_rules=False)
                    self.addCleanup(value.secret_scope.close)
                    value._model_retry_wait = lambda exc: 0
                    return value
                first_agent=agent();first=first_agent.run('Read values.txt.')
                self.assertEqual(first.stopped,'error')
                self.assertNotEqual(first.as_result().outcome,'succeeded')
                self.assertTrue(any(e['type']=='cache_prefix_checkpointed'
                    and e['reason']=='model_transport' for e in first.events))
                saved=copy.deepcopy(first_agent._conversation_prefix._saved['messages'])
                self.assertTrue(valid_tool_pairs(saved))
                self.assertNotIn('[model error]',str(saved))
                first_agent.secret_scope.close()
                recovered = True
                second=agent().run('Continue.',history=[{'role':'user','content':'Read values.txt.'},
                    {'role':'assistant','content':first.text}])
                self.assertEqual(second.stopped,'final');self.assertEqual(second.steps,[])
                self.assertTrue(any(e['type']=='cache_prefix_reused' for e in second.events))
                self.assertEqual(calls[-1][:len(saved)],saved)
                self.assertIn('ACTUAL_FILE_OBSERVATION',str(calls[-1]))
                self.assertTrue(all(valid_tool_pairs(ms) for ms in calls))

    def test_nonretryable_failure_does_not_checkpoint_rejected_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            class Transport:
                def complete(inner,*args,**kw):raise BadRequest('fixture protocol rejection')
            agent=Agent(home=root/'home',workspace=root,router=ModelRouter([
                Provider('fixture','https://fixture.invalid',default_model='fixture')],
                transport=Transport(),retries_per_provider=0),registry=build_builtin_registry(),
                policy=Policy(workspace=root,mode=Mode.READ_ONLY),project_rules=False)
            self.addCleanup(agent.secret_scope.close)
            report=agent.run('Inspect.')
            self.assertEqual(report.stopped,'error')
            self.assertFalse(any(e['type']=='cache_prefix_checkpointed' for e in report.events))
            self.assertFalse((root/'home/conversation-prefix.json').exists())


if __name__=='__main__':unittest.main()
