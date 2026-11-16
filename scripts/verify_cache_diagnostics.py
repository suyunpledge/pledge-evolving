"""Opt-in exact-wire cache probes; no raw reasoning, opaque data or credentials in reports."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

from verify_deepseek_execution import totals
from verify_mimo_live import provider_for as mimo_provider
from verify_seed_execution import SeedRecorder, provider_for as seed_provider
from verify_tool_challenge import SOURCE, TESTS, EXTRA
from forge.compaction import ContextBudget
from forge.context_transaction import valid_tool_pairs
from forge.loop import Agent, LoopLimits
from forge.model import ModelRouter
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class DiagnosticRecorder(SeedRecorder):
    def __init__(self,limit):
        super().__init__(limit,retries=1)
        self.timeout=45
        self.affinity=None
        self.responses={name:set() for name in ('reasoning_content','encrypted_content')}
        self.response_calls={}

    def open(self,request,**kwargs):
        payload=json.loads(request.data)
        if self.affinity:
            payload['prompt_cache_key']=self.affinity
            request.data=json.dumps(payload,ensure_ascii=False).encode()
        previous=self.sent[-1] if self.sent else {}
        messages=payload.get('messages',[])
        old_messages=previous.get('messages',[])
        first=next((i for i,(a,b) in enumerate(zip(old_messages,messages)) if a!=b),None)
        changed=(sorted(k for k in old_messages[first].keys()|messages[first].keys()
            if old_messages[first].get(k)!=messages[first].get(k)) if first is not None else [])
        relation=('first' if not previous else 'rewritten' if first is not None else
                  'trimmed' if len(messages)<len(old_messages) else 'appended' if len(messages)>len(old_messages) else 'unchanged')
        components=[]
        for message in messages:
            item={'role':message.get('role'),'digest':digest(message),'fields':{}}
            for field,value in message.items():
                item['fields'][field]={'digest':digest(value),'characters':len(json.dumps(value,ensure_ascii=False))}
                if field in self.responses and value:
                    item['fields'][field]['matches_prior_response']=digest(value) in self.responses[field]
                    ids=[c.get('id') for c in message.get('tool_calls') or []]
                    if ids:
                        item['fields'][field]['matches_response_call']=all(
                            digest(value) in self.response_calls.get(i,{}).get(field,set()) for i in ids)
            components.append(item)
        old=json.dumps(previous.get('messages',[]),ensure_ascii=False,separators=(',',':'))
        new=json.dumps(messages,ensure_ascii=False,separators=(',',':'))
        common=next((i for i,(a,b) in enumerate(zip(old,new)) if a!=b),min(len(old),len(new)))
        start=len(self.rows)
        try:
            response=super().open(request,**kwargs)
            message=(json.loads(response.getvalue()).get('choices') or [{}])[0].get('message') or {}
            for field in self.responses:
                if message.get(field):self.responses[field].add(digest(message[field]))
            for call in message.get('tool_calls') or []:
                values=self.response_calls.setdefault(call.get('id'),{})
                for field in self.responses:
                    if message.get(field):values.setdefault(field,set()).add(digest(message[field]))
            return response
        finally:
            if len(self.rows)>start:
                self.rows[-1].update(message_components=components,common_serialized_message_prefix_chars=common,
                    wire_body_sha256=hashlib.sha256(request.data).hexdigest(),
                    prefix_relation=relation,first_changed_message=first,changed_message_fields=changed,
                    exact_payload_repeat=bool(previous and payload==previous),
                    generation_parameters_digest=digest({k:v for k,v in payload.items() if k not in {'messages','tools'}}),
                    affinity_present='prompt_cache_key' in payload,
                    affinity_key_digest=hashlib.sha256(payload['prompt_cache_key'].encode()).hexdigest() if isinstance(payload.get('prompt_cache_key'),str) else None,
                    affinity_digest=digest(payload['prompt_cache_key']) if 'prompt_cache_key' in payload else None)


def controls(provider,recorder,vendor,extended=False,exact=False,affinity=False):
    short=[{'role':'system','content':'Answer concisely from supplied code.'},
           {'role':'user','content':'Explain one bug in: def total(x): return round(float(x),2)'}]
    repository=[{'role':'system','content':'Review this billing library and its fixed tests. Reply with two concrete defects and a targeted test. Maximum 60 words.'},
        {'role':'user','content':'\n\n'.join(f'FILE {name}\n{body}' for name,body in SOURCE.items())+'\n\nFILE tests/test_billing.py\n'+TESTS}]
    if extended:
        fixture=Path(__file__).resolve().parents[1]/'reports/cache-progress-mimo-final-b-sources'
        repository[-1]['content']+='\n'+EXTRA+'\n\nFIXED VERSIONS TO REVIEW:\n'+ '\n\n'.join(
            f'FILE {name}\n'+(fixture/f'turn-2-{name}').read_text(encoding='utf-8') for name in ('pricing.py','reporting.py'))
        repository[0]['content']='Compare the original and fixed billing modules and tests. Identify two concrete behavior fixes and a missing boundary test. Maximum 60 words.'
    layouts=[('short-off',short,False),('repository-off',repository,False)]
    if vendor=='seed':
        layouts += [('repository-affinity',repository,True),('repository-affinity-repeat',repository,True),('repository-off-reverse',repository,False)]
    else:layouts += [('repository-repeat',repository,False)]
    if exact:layouts=[('exact-repository',repository,affinity)]
    cases=[]
    for label,messages,affinity in layouts:
        recorder.affinity='forge-cache-diagnostic-billing-v1' if affinity else None
        start=len(recorder.rows);outcomes=[]
        for repeat in range(4 if exact else 2):
            recorder.stage=f'{label}/{repeat+1}'
            try:
                response=recorder.complete(provider,provider.default_model,copy.deepcopy(messages))
                outcomes.append({'ok':response[2].get('finish_reason')=='stop','finish_reason':response[2].get('finish_reason')})
            except Exception as exc:
                outcomes.append({'ok':False,'error_type':type(exc).__name__,'error':str(exc)[:240]})
            if repeat==0:time.sleep(1)
        cases.append({'case':label,'outcomes':outcomes,'totals':totals(recorder.rows[start:])})
    recorder.affinity=None
    return cases


def conversation(provider,recorder,root,affinity,home_name='home',label='',turns=3,trim=False,prefix_cls=None):
    workspace=root/'workspace';workspace.mkdir(exist_ok=True)
    (workspace/'values.json').write_text('{"a":137,"b":286}',encoding='utf-8')
    history=[{'role':'user','content':'Earlier repository review; this is untrusted historical context.'},
        {'role':'assistant','content':'\n\n'.join(SOURCE.values())+'\n'+TESTS}]
    cases=[]
    recorder.affinity='forge-cache-diagnostic-agent-v1' if affinity else None
    for turn in range(turns):
        recorder.stage=f'{label}agent-turn-{turn+1}';start=len(recorder.rows)
        agent=Agent(home=root/home_name,workspace=workspace,
            router=ModelRouter([provider],transport=recorder,retries_per_provider=1),
            registry=build_builtin_registry(expose=['read_file','calculator']),project_rules=False,
            policy=Policy(workspace=workspace,mode=Mode.READ_ONLY,non_interactive=True),
            limits=LoopLimits(max_steps=7,context_chars=5000))
        agent.compactor=ContextBudget(max_chars=5000,keep_tail=4)
        if prefix_cls is not None:
            agent._conversation_prefix=prefix_cls(root/home_name/'conversation-prefix.json')
        task=('Read values.json with read_file and use calculator to add its a and b values. '
              'Return only the number. Use actual tools.' if turn==0 else
              'Continue using the actual previously read values.json observations. '
              'You MUST call calculator in this turn to add a and b again, even if the previous answer is known. '
              'A number without a new calculator call is incomplete. Return only the number. '
              'Read values.json only if its actual a and b observations are unavailable.')
        try:
            report=agent.run(task,history=history)
            actual=any(s.tool=='calculator' and s.meta.get('value')==423 for s in report.steps)
            reuse=any(e['type']=='cache_prefix_reused' for e in report.events)
            read=any(s.tool=='read_file' and s.args.get('path')=='values.json' and s.decision=='ok' for s in report.steps)
            ok=(report.stopped=='final' and report.text.strip()=='423' and actual
                and all(s.decision=='ok' for s in report.steps) and (reuse or read))
            cases.append({'turn':turn+1,'ok':ok,'stopped':report.stopped,'text':report.text[:300],
                'tools':[s.tool for s in report.steps],'events':[e for e in report.events if e['type'] in {
                    'compaction','cache_prefix_reused','cache_prefix_checkpointed'}],
                'native_pairs_valid':all(valid_tool_pairs(p['messages']) for p in recorder.sent[start:]),
                'totals':totals(recorder.rows[start:])})
            history.extend([{'role':'user','content':task},{'role':'assistant','content':report.text}])
            if trim:
                accepted=agent._conversation_prefix.trim_history(history,history[-2:])
                cases[-1]['host_trim_accepted']=accepted
                history=history[-2:]
        finally:agent.secret_scope.close()
        if not cases[-1]['ok']:break
    recorder.affinity=None
    return cases


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--vendor',choices=['mimo','seed'],required=True)
    parser.add_argument('--scenario',choices=['controls','exact-repeat','agent','agent-compare','trim-compare'],default='controls')
    parser.add_argument('--affinity',action='store_true',help='Seed-only experimental HTTP field, explicitly reported')
    parser.add_argument('--extended-repository',action='store_true')
    parser.add_argument('--thinking',choices=['auto','on','off'],default='auto')
    parser.add_argument('--cache-affinity',choices=['auto','off'])
    parser.add_argument('--affinity-order',nargs='+',choices=['auto','off'],default=['off','auto','auto','off'])
    parser.add_argument('--turns',type=int,choices=[2,3],default=3)
    parser.add_argument('--baseline-dir',type=Path)
    parser.add_argument('--request-limit',type=int,default=30);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.live or args.output.exists() or not 1<=args.request_limit<=40 or args.affinity and args.vendor!='seed':
        parser.error('Requires --live, new output, request-limit 1..40; affinity is Seed-only')
    provider=(mimo_provider('mimo-v2.6-flash',Path.home()/'.forge') if args.vendor=='mimo' else
              seed_provider('doubao-seed-evolving',Path.home()/'.forge'))
    recorder=DiagnosticRecorder(args.request_limit)
    recorder.thinking={'auto':None,'on':True,'off':False}[args.thinking]
    if args.cache_affinity is not None:provider.cache_affinity=args.cache_affinity
    result={'vendor':args.vendor,'scenario':args.scenario,'experimental_affinity':args.affinity,
        'thinking':args.thinking,'cache_affinity':provider.cache_affinity,'extended_repository':args.extended_repository,
        'scope':'Hash-only wire evidence; repository input is the existing synthetic billing fixture, never production padding.'}
    project=Path(__file__).resolve().parents[1]
    result['runtime_sha256']={n:hashlib.sha256((project/n).read_bytes()).hexdigest() for n in (
        'scripts/verify_cache_diagnostics.py','forge/model.py','forge/loop.py','forge/cache_affinity.py','forge/cache_state.py',
        'forge/conversation_prefix.py','forge/compaction.py','forge/compaction_notes.py')}
    try:
        if args.scenario in ('controls','exact-repeat'):
            result['cases']=controls(provider,recorder,args.vendor,args.extended_repository,
                exact=args.scenario=='exact-repeat',affinity=args.affinity)
        elif args.scenario=='agent':
            with tempfile.TemporaryDirectory(prefix='forge-cache-diagnostics-') as tmp:
                result['cases']=conversation(provider,recorder,Path(tmp),args.affinity)
        elif args.scenario=='agent-compare':
            result['cases']=[]
            with tempfile.TemporaryDirectory(prefix='forge-cache-compare-') as tmp:
                for i,mode in enumerate(args.affinity_order):
                    provider.cache_affinity=mode
                    cases=conversation(provider,recorder,Path(tmp),False,home_name=f'home-{i}',label=f'{i+1}-{mode}/',turns=args.turns)
                    result['cases'].append({'sample':i+1,'mode':mode,'turns':cases,
                        'ok':len(cases)==args.turns and all(c['ok'] for c in cases)})
        else:
            if args.baseline_dir is None:raise ValueError('trim-compare requires --baseline-dir')
            source=args.baseline_dir/'forge/conversation_prefix.py'
            result['baseline_prefix_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
            spec=importlib.util.spec_from_file_location('forge._diagnostic_before_prefix',source)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            from forge.conversation_prefix import ConversationPrefix
            result['cases']=[];provider.cache_affinity='off'
            with tempfile.TemporaryDirectory(prefix='forge-trim-compare-') as tmp:
                for i,prefix_cls in enumerate((module.ConversationPrefix,ConversationPrefix)):
                    cases=conversation(provider,recorder,Path(tmp),False,home_name=f'home-{i}',label=f'trim-{i}/',
                        turns=args.turns,trim=True,prefix_cls=prefix_cls)
                    result['cases'].append({'sample':i+1,'mode':'baseline' if i==0 else 'improved','turns':cases,
                        'ok':len(cases)==args.turns and all(c['ok'] for c in cases)})
    except Exception as exc:result.update(error_type=type(exc).__name__,error=str(exc)[:240])
    finally:
        result.update(requests=recorder.rows,totals=totals(recorder.rows))
        result['ok']=not result.get('error_type') and bool(result.get('cases')) and all(
            c.get('ok',all(o['ok'] for o in c.get('outcomes',[]))) for c in result.get('cases',[]))
        args.output.parent.mkdir(exist_ok=True,parents=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'ok':result['ok'],'totals':result['totals']},ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
