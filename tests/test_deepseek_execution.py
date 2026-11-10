"""DeepSeek request semantics and planning budget regression tests (offline)."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from forge.model import Provider, HttpTransport, ModelRouter, _plan_and_shape
from forge.cache_state import CacheWarmth
from forge.loop import Agent
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry
from forge.planning import token_cap


class DeepSeekExecutionTests(unittest.TestCase):
    def shape(self, payload, chosen='deepseek-flash', mode='auto', wire='openai'):
        provider = Provider('fixture', 'https://api.deepseek.com', default_model=chosen,
                            reasoning_mode=mode, wire=wire)
        return _plan_and_shape(payload, payload.get('messages', []), provider, chosen, store=CacheWarmth())

    def test_implicit_and_explicit_defaults_have_identical_payload_and_cache_namespace(self):
        messages = [{'role':'user','content':'Task'}, {'role':'assistant','content':'Done',
                    'reasoning_content':'native opaque reasoning'}]
        tools = [{'type':'function','function':{'name':'read_file','parameters':{'type':'object'}}}]
        base = {'messages':messages, 'tools':tools, 'temperature':.7}
        before = copy.deepcopy(base)
        a, pa = self.shape(base)
        b, pb = self.shape({**base, 'thinking':{'type':'enabled'}, 'reasoning_effort':'high', 'tool_choice':'auto'})
        self.assertEqual(a,b)
        self.assertEqual(pa['fingerprint'],pb['fingerprint'])
        self.assertEqual(base,before)
        self.assertEqual(a['messages'],messages)
        self.assertNotIn('temperature',a)

    def test_model_specific_sampling_and_tool_constraints(self):
        for chosen in ('deepseek-flash','deepseek-v4-pro'):
            for mode in ('on','off'):
                for choice in ('none','required',{'type':'function','function':{'name':'read_file'}}):
                    with self.subTest(chosen=chosen,mode=mode,choice=choice):
                        out,_ = self.shape({'messages':[], 'temperature':.4, 'top_p':.97,
                                            'tool_choice':choice},chosen,mode)
                        self.assertEqual(out['thinking']['type'], 'enabled' if mode == 'on' else 'disabled')
                        self.assertEqual('temperature' in out, mode == 'off')
                        self.assertEqual('top_p' in out, (chosen == 'deepseek-flash') == (mode == 'on'))
                        self.assertEqual(out['tool_choice'],choice)

    def test_explicit_request_overrides_provider_mode_and_unknown_models_are_not_guessed(self):
        out,_ = self.shape({'messages':[], 'thinking':{'type':'disabled'}, 'reasoning_effort':'low'},mode='on')
        self.assertEqual(out['thinking'],{'type':'disabled'})
        self.assertNotIn('reasoning_effort',out)
        with self.assertRaisesRegex(ValueError,'reasoningMode'):
            self.shape({'messages':[]},chosen='deepseek-unknown',mode='off')
        with self.assertRaisesRegex(ValueError,'reasoningMode'):
            self.shape({'messages':[]},wire='anthropic',mode='off')

    def test_gateway_and_direct_adapter_share_the_same_reasoning_policy(self):
        from forge.gateway import GatewayConfig
        for mode in ('auto','on','off'):
            payload = {'model':'deepseek-flash','messages':[{'role':'user','content':'Inspect'}]}
            direct, left = self.shape(payload,mode=mode)
            cfg = GatewayConfig(upstream='https://api.deepseek.com',api_key='',models=['deepseek-flash'],
                                wire='openai',upstream_wire='openai',provider_options={'reasoningMode':mode})
            cfg.cache_warmth = CacheWarmth()
            gateway, right, _ = cfg.prepare_request(payload)
            self.assertEqual(direct,gateway)
            self.assertEqual(left['fingerprint'],right['fingerprint'])

    def test_provider_mode_reaches_actual_agent_tool_replay(self):
        sent=[]
        def opener(request, **kwargs):
            sent.append(json.loads(request.data))
            if len(sent)==1:
                message={'role':'assistant','content':'','reasoning_content':'',
                    'tool_calls':[{'id':'call-1','type':'function','function':{
                        'name':'read_file','arguments':'{"path":"input.txt"}'}}]}
                reason='tool_calls'
            else:
                message={'role':'assistant','content':'verified'};reason='stop'
            return io.BytesIO(json.dumps({'choices':[{'message':message,'finish_reason':reason}],
                 'usage':{'prompt_tokens':10,'completion_tokens':2,'prompt_cache_hit_tokens':0}}).encode())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'input.txt').write_text('observed fact')
            provider=Provider('p','https://api.deepseek.com',default_model='deepseek-flash',reasoning_mode='off')
            router=ModelRouter([provider],transport=HttpTransport(response_opener=opener),retries_per_provider=0)
            agent=Agent(home=root/'home',workspace=root,router=router,project_rules=False,
                        registry=build_builtin_registry(expose=['read_file']),policy=Policy(workspace=root,mode=Mode.READ_ONLY))
            try:
                report=agent.run('Read input.txt')
            finally:
                agent.secret_scope.close()
            self.assertEqual(report.stopped,'final')
            self.assertEqual(report.steps[0].decision,'ok')
            self.assertTrue(all(p['thinking']=={'type':'disabled'} for p in sent))
            self.assertTrue(any(m.get('tool_call_id')=='call-1' for m in sent[-1]['messages']))

    def test_truncated_even_parseable_plan_never_starts_execution(self):
        sent=[]
        def opener(request, **kwargs):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"tasks":["Write output"]}'},
                            'finish_reason':'length'}], 'usage':{'prompt_tokens':20,'completion_tokens':2048}}).encode())
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            router=ModelRouter([Provider('p','https://api.deepseek.com',default_model='deepseek-flash')],
                               transport=HttpTransport(response_opener=opener),retries_per_provider=0)
            agent=Agent(home=root,workspace=root,router=router,planning='low',project_rules=False,
                        registry=build_builtin_registry(),policy=Policy(workspace=root))
            try:
                report=agent.run('Write output')
            finally:
                agent.secret_scope.close()
            self.assertEqual(report.stopped,'planning_error')
            self.assertIn('output budget',report.text)
            self.assertEqual(len(sent),1)
            self.assertEqual(sent[0]['max_tokens'],token_cap('low'))
            self.assertGreater(sent[0]['max_tokens'],640)
            self.assertEqual(report.steps,[])


if __name__=='__main__':
    unittest.main()
