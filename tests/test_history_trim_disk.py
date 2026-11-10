"""Host display-history trims must remain acknowledged after Agent restart."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from forge.conversation_prefix import ConversationPrefix
from forge.loop import Agent
from forge.model import HttpTransport, ModelRouter, Provider
from forge.policy import Mode, Policy
from forge.tools import build_builtin_registry


class TrimPersistenceTests(unittest.TestCase):
    def test_exact_host_suffix_is_persisted_but_edited_or_fabricated_trim_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prefix.json';prefix=ConversationPrefix(path)
            history=[{'role':'user','content':'earlier'},{'role':'assistant','content':'answer'}]
            prefix.commit(history=history,messages=history,guard='guard',rule_context='',rule_sources=[],targets=[])
            self.assertTrue(prefix.trim_history(history,history[-1:]))
            fresh=ConversationPrefix(path)
            self.assertIsNotNone(fresh.candidate(history[-1:],'guard'))
            before=path.read_bytes()
            self.assertFalse(fresh.trim_history(history[-1:],[{'role':'assistant','content':'edited'}]))
            self.assertFalse(fresh.trim_history([{'role':'assistant','content':'fabricated'}],history[-1:]))
            self.assertEqual(path.read_bytes(),before)
            self.assertIsNone(ConversationPrefix(path).candidate(history[-1:],'wrong guard'))

    def test_real_agent_wire_keeps_tool_and_reasoning_history_after_trim_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'value.txt').write_text('ACTUAL_VALUE',encoding='utf-8');sent=[]
            def opener(request,**kw):
                sent.append(json.loads(request.data));message={'role':'assistant','content':'done'}
                if len(sent)==1:
                    message={'role':'assistant','content':'','reasoning_content':'REPLAY_THOUGHT',
                        'encrypted_content':'OPAQUE_REPLAY','tool_calls':[{'id':'read','type':'function',
                            'function':{'name':'read_file','arguments':'{"path":"value.txt"}'}}]}
                return io.BytesIO(json.dumps({'choices':[{'finish_reason':'tool_calls' if len(sent)==1 else 'stop',
                    'message':message}]}).encode())
            router=ModelRouter([Provider('fixture','https://fixture.invalid',default_model='mimo-v2.6-flash')],
                transport=HttpTransport(response_opener=opener),retries_per_provider=0)
            def agent():
                value=Agent(home=root/'home',workspace=root,router=router,
                    registry=build_builtin_registry(expose=['read_file']),
                    policy=Policy(workspace=root,mode=Mode.READ_ONLY),project_rules=False)
                self.addCleanup(value.secret_scope.close);return value
            old=[{'role':'user','content':'old task'},{'role':'assistant','content':'old answer'}]
            first_agent=agent();first=first_agent.run('Read value.txt.',history=old)
            full=[*old,{'role':'user','content':'Read value.txt.'},{'role':'assistant','content':first.text}]
            before=copy.deepcopy(sent[-1]['messages'])
            self.assertTrue(first_agent._conversation_prefix.trim_history(full,full[-2:]))
            first_agent.secret_scope.close()
            second=agent().run('Continue.',history=full[-2:])
            self.assertEqual(second.stopped,'final')
            self.assertTrue(any(e['type']=='cache_prefix_reused' for e in second.events))
            self.assertEqual(sent[-1]['messages'][:len(before)],before)
            self.assertIn('ACTUAL_VALUE',str(sent[-1]['messages']))
            self.assertIn('REPLAY_THOUGHT',str(sent[-1]['messages']))
            self.assertIn('OPAQUE_REPLAY',str(sent[-1]['messages']))


if __name__=='__main__':unittest.main()
