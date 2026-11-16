"""Opt-in Seed Coding Plan checks, using existing credentials and isolated fixtures."""
import argparse
import json
from pathlib import Path
import tempfile

from verify_deepseek_execution import DeepSeekRecorder, task_probe, totals
from verify_mimo_live import wire_probe
from forge.model import Provider


def provider_for(chosen, home):
    rows=json.loads((home/'forge.patch.json').read_text(encoding='utf-8-sig'))
    secrets=json.loads((home/'secrets.json').read_text(encoding='utf-8-sig'))
    for row in rows:
        c=row.get('config',{})
        if (not row.get('disabled') and c.get('model')==chosen and
                c.get('baseURL','').rstrip('/')=='https://ark.cn-beijing.volces.com/api/coding/v3'):
            key=secrets.get(row.get('id'))
            if not isinstance(key,str) or not key:
                raise ValueError('Configured Seed credential missing')
            return Provider('seed-live',c['baseURL'],key,default_model=chosen,
                            cache_affinity=c.get('cacheAffinity','auto'),
                            temperature=c.get('temperature'))
    raise ValueError('No enabled Seed Coding Plan provider matches')


class SeedRecorder(DeepSeekRecorder):
    def __init__(self,limit,thinking=None,retries=1):
        super().__init__(limit,thinking=thinking,retries=retries)
        self.timeout=90
        self.encrypted=[]

    def open(self,request,**kwargs):
        response=super().open(request,**kwargs)
        data=json.loads(response.getvalue())
        msg=(data.get('choices') or [{}])[0].get('message') or {}
        row=self.rows[-1]
        row['response_message_fields']=sorted(msg)
        row['encrypted_content_chars']=len(msg.get('encrypted_content') or '')
        payload=json.loads(request.data)
        row['encrypted_replay_matches']=sum(
            m.get('encrypted_content') in self.encrypted for m in payload.get('messages',[])
            if isinstance(m.get('encrypted_content'),str) and m['encrypted_content'])
        if msg.get('encrypted_content'):
            self.encrypted.append(msg['encrypted_content'])
        return response

    def complete(self,provider,chosen,messages,**options):
        if self.thinking is not None:
            options.setdefault('enable_thinking',self.thinking)
        return super().complete(provider,chosen,messages,**options)


def wire_cases(provider,recorder):
    # Keep completed cases when a later mode encounters a transport failure.
    original=recorder.thinking
    results=[]
    try:
        for thinking in ((False,True) if original is None else (original,)):
            recorder.thinking=thinking
            try:
                results.extend(wire_probe(provider,recorder))
            except Exception as exc:
                results.append({'thinking':'on' if thinking else 'off','ok':False,
                                'error_type':type(exc).__name__})
    finally:
        recorder.thinking=original
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--model',default='doubao-seed-evolving')
    parser.add_argument('--scenario',choices=['wire','execution','planning'],default='execution')
    parser.add_argument('--thinking',choices=['auto','on','off'],default='auto')
    parser.add_argument('--planning',choices=['none','low','medium','high'],default='low')
    parser.add_argument('--request-limit',type=int,default=36)
    parser.add_argument('--retries',type=int,choices=[0,1,2],default=1)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.live: parser.error('--live required for paid requests')
    if args.output.exists(): parser.error('Use a new report path')
    provider=provider_for(args.model,Path.home()/'.forge')
    recorder=SeedRecorder(args.request_limit,{'auto':None,'on':True,'off':False}[args.thinking],args.retries)
    result={'model':args.model,'scenario':args.scenario,'thinking':args.thinking,'retries':args.retries,'cases':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='forge-seed-live-') as tmp:
            root=Path(tmp)
            if args.scenario=='wire':
                result['cases']=wire_cases(provider,recorder)
            else:
                result['cases']=task_probe(provider,recorder,root,args.planning,
                    compact=args.scenario=='execution',turns=3 if args.scenario=='execution' else 1)
                if args.scenario=='execution':
                    result['denied']=task_probe(provider,recorder,root,'none',deny=True)
    except Exception as exc:
        result['error_type']=type(exc).__name__
    finally:
        result['requests']=recorder.rows
        result['totals']=totals(recorder.rows)
        result['ok']=bool(result['cases']) and not result.get('error_type') and all(
            c['ok'] for c in result['cases']+result.get('denied',[]))
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'ok':result['ok'],'totals':result['totals']},ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
