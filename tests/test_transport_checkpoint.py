"""Only retryable model transport errors may checkpoint known tool exchanges."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from forge.context_transaction import valid_tool_pairs
from forge.loop import Agent
from forge.model import BadRequest, HttpTransport, ModelRouter, Provider, TransportError, Usage
from forge.tool_adapter import sanitize_messages
from forge.policy import Mode, Policy
from forge.tools import build_builtin_registry


class TransportCheckpointTests(unittest.TestCase):
    def test_actual_http_projection_preserves_prefix_reasoning_and_opaque_replay(self):
        for model in ('mimo-v2.6-flash','doubao-seed-evolving'):
            with self.subTest(model=model),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'value.txt').write_text('ACTUAL_VALUE',encoding='utf-8');sent=[]
                def opener(request,**kw):
                    sent.append(json.loads(request.data))
                    if len(sent)==2:raise urllib.error.URLError('fixture connection reset')
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
                    self.addCleanup(value.secret_scope.close);return value
                first_agent=agent();first=first_agent.run('Read value.txt.')
                self.assertEqual(first.stopped,'error')
                saved=copy.deepcopy(first_agent._conversation_prefix._saved['messages'])
                expected=sanitize_messages(saved,'openai')
                self.assertNotEqual(saved,expected)  # empty assistant content -> null on this wire
                self.assertEqual(sent[1]['messages'],expected)
                second=agent().run('Continue.',history=[{'role':'user','content':'Read value.txt.'},
                    {'role':'assistant','content':first.text}])
                self.assertEqual(second.stopped,'final')
                self.assertEqual(sent[2]['messages'][:len(expected)],expected)
                replay=next(m for m in expected if m.get('tool_calls'))
                self.assertEqual(replay['reasoning_content'],'REPLAY_THOUGHT')
                self.assertEqual(replay['encrypted_content'],'OPAQUE_REPLAY')
                self.assertEqual(replay['tool_calls'][0]['function']['arguments'],'{ "path" : "value.txt" }')

    def test_retryable_error_survives_disk_restart_without_repeating_the_read(self):
        for wire in ('openai','anthropic'):
            with self.subTest(wire=wire),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'values.txt').write_text('ACTUAL_FILE_OBSERVATION',encoding='utf-8')
                calls=[]
                class Transport:
                    def complete(inner,provider,model,messages,**kw):
                        calls.append(copy.deepcopy(messages))
                        if len(calls)==1:
                            return '',Usage(10,1,model,provider.name),{'wire':wire,'tool_calls':[
                                {'id':'read','name':'read_file','args':{'path':'values.txt'},'wire':wire}]}
                        if len(calls)==2:raise TransportError('fixture temporary outage')
                        return 'done',Usage(10,1,model,provider.name)
                provider=Provider('fixture','https://fixture.invalid',default_model='fixture',wire=wire)
                router=ModelRouter([provider],transport=Transport(),retries_per_provider=0)
                def agent():
                    value=Agent(home=root/'home',workspace=root,router=router,
                        registry=build_builtin_registry(expose=['read_file']),wire_hint=wire,
                        policy=Policy(workspace=root,mode=Mode.READ_ONLY),project_rules=False)
                    self.addCleanup(value.secret_scope.close)
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
