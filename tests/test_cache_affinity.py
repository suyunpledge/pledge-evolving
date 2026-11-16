"""Routing hints stay scoped, optional, and stable through real HTTP continuation."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from forge.cache_state import CacheWarmth, prefix_fingerprint
from forge.config import Config
from forge.loop import Agent
from forge.model import BadRequest, HttpTransport, ModelRouter, Provider, _plan_and_shape
from forge.policy import Mode, Policy
from forge.tools import build_builtin_registry

BASE='https://ark.cn-beijing.volces.com/api/coding/v3'
MODEL='doubao-seed-evolving'


class CacheAffinityTests(unittest.TestCase):
    def shape(self,scope='workspace-scope',**provider_options):
        provider=Provider('fixture',BASE,default_model=MODEL,**provider_options)
        payload={'model':MODEL,'messages':[{'role':'user','content':'hello'}]}
        shaped,plan=_plan_and_shape(payload,payload['messages'],provider,MODEL,
            cache_scope=scope,store=CacheWarmth())
        self.assertNotIn('prompt_cache_key',payload)
        return shaped,plan

    def test_scope_account_reasoning_and_tools_partition_hint_and_warmth(self):
        first,plan=self.shape();same,_=self.shape()
        self.assertEqual(first['prompt_cache_key'],same['prompt_cache_key'])
        self.assertFalse(plan['execution']['cache_affinity']['guaranteed_hit'])
        for options in ({'scope':'other-workspace'},{'api_key':'fixture-key-other-account'}):
            changed,_=self.shape(**options)
            self.assertNotEqual(first['prompt_cache_key'],changed['prompt_cache_key'])
        provider=Provider('fixture',BASE,default_model=MODEL)
        variants=[]
        for thinking,tools in [(False,[]),(True,[]),(True,[{'type':'function','function':{'name':'read'}}])]:
            payload={'messages':[],'enable_thinking':thinking,'tools':tools}
            variants.append(_plan_and_shape(payload,[],provider,MODEL,cache_scope='scope',store=CacheWarmth())[0])
        self.assertEqual(len({v['prompt_cache_key'] for v in variants}),3)
        self.assertNotEqual(prefix_fingerprint(first),prefix_fingerprint({**first,'prompt_cache_key':'other'}))

    def test_off_unknown_routes_and_no_scope_keep_original_wire(self):
        self.assertNotIn('prompt_cache_key',self.shape(cache_affinity='off')[0])
        self.assertNotIn('prompt_cache_key',self.shape(scope=None)[0])
        self.assertNotIn('prompt_cache_key',self.shape(adapt=False)[0])
        for base,model,wire in [('https://reseller.invalid/api/coding/v3',MODEL,'openai'),
                (BASE,'doubao-seed-code','openai'),(BASE,MODEL,'anthropic'),
                ('https://token-plan-cn.xiaomimimo.com/v1','mimo-v2.6-flash','openai')]:
            provider=Provider('fixture',base,default_model=model,wire=wire)
            out,_=_plan_and_shape({'messages':[]},[],provider,model,cache_scope='scope',store=CacheWarmth())
            self.assertNotIn('prompt_cache_key',out)
        with self.assertRaises(BadRequest):Provider('fixture',BASE,cache_affinity='yes')
        with self.assertRaises(ValueError):self.shape(scope='x'*513)

    def test_existing_caller_key_is_preserved(self):
        provider=Provider('fixture',BASE,default_model=MODEL)
        payload={'messages':[],'prompt_cache_key':'caller-owned'}
        shaped,plan=_plan_and_shape(payload,[],provider,MODEL,cache_scope='scope',store=CacheWarmth())
        self.assertEqual(shaped['prompt_cache_key'],'caller-owned')
        self.assertEqual(plan['execution']['cache_affinity']['status'],'caller_supplied')

    def test_configured_real_agent_disk_continuation_keeps_hint_and_replay_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'value.txt').write_text('ACTUAL_VALUE',encoding='utf-8');sent=[]
            cfg=Config();cfg.apply_patch([{'id':'fixture','name':'provider:seed','config':{
                'baseURL':BASE,'model':MODEL,'cacheAffinity':'auto'}}])
            def opener(request,**kw):
                sent.append(json.loads(request.data))
                message={'role':'assistant','content':'done'}
                if len(sent)==1:
                    message={'role':'assistant','content':'','reasoning_content':'ACTUAL_REASONING',
                        'encrypted_content':'OPAQUE_REPLAY','tool_calls':[{'id':'read','type':'function',
                            'function':{'name':'read_file','arguments':'{ "path" : "value.txt" }'}}]}
                return io.BytesIO(json.dumps({'choices':[{'finish_reason':'tool_calls' if len(sent)==1 else 'stop',
                    'message':message}],'usage':{'prompt_tokens':100,'completion_tokens':10}}).encode())
            router=ModelRouter.from_config(cfg,transport=HttpTransport(response_opener=opener))
            def agent():
                value=Agent(home=root/'home',workspace=root,router=router,
                    registry=build_builtin_registry(expose=['read_file']),
                    policy=Policy(workspace=root,mode=Mode.READ_ONLY),project_rules=False)
                self.addCleanup(value.secret_scope.close);return value
            first=agent().run('Read value.txt.')
            old=copy.deepcopy(sent[-1]['messages'])
            second=agent().run('Continue.',history=[{'role':'user','content':'Read value.txt.'},
                {'role':'assistant','content':first.text}])
            self.assertEqual(second.stopped,'final')
            self.assertTrue(any(e['type']=='cache_prefix_reused' for e in second.events))
            self.assertEqual(len({p['prompt_cache_key'] for p in sent}),1)
            self.assertTrue(all('cache_scope' not in p for p in sent))
            self.assertEqual(sent[-1]['messages'][:len(old)],old)
            replay=next(m for m in sent[-1]['messages'] if m.get('tool_calls'))
            self.assertEqual(replay['encrypted_content'],'OPAQUE_REPLAY')
            self.assertEqual(replay['reasoning_content'],'ACTUAL_REASONING')
            router.providers['fixture'].cache_affinity='off'
            agent().run('New task.')
            self.assertNotIn('prompt_cache_key',sent[-1])


if __name__=='__main__':unittest.main()
