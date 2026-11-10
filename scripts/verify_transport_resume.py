"""Paid API reads/calculation around an explicitly injected pre-dispatch outage."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

from verify_seed_execution import SeedRecorder, provider_for as seed_provider
from verify_mimo_live import provider_for as mimo_provider
from verify_deepseek_execution import DeepSeekRecorder, totals
from forge.context_transaction import valid_tool_pairs
from forge.loop import Agent, LoopLimits
from forge.model import ModelRouter, TransportError
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry
from forge.tool_adapter import sanitize_messages


class PauseAfterRead:
    def __init__(self, recorder):
        self.recorder=recorder
        self.active=True
        self.injected=0
        self.paused_messages=None

    def complete(self,provider,model,messages,**options):
        if self.active and any(m.get('role')=='tool' for m in messages):
            self.injected+=1
            self.paused_messages=copy.deepcopy(messages)
            raise TransportError('Host fixture: injected model transport interruption before HTTP dispatch')
        return self.recorder.complete(provider,model,messages,**options)


def load_before(directory):
    spec=importlib.util.spec_from_file_location('forge._transport_before_loop',directory/'forge/loop.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module.Agent


def run_sample(cls,provider,recorder,root,strategy,index):
    workspace=root/'workspace';workspace.mkdir(exist_ok=True)
    (workspace/'values.json').write_text('{"a":137,"b":286}',encoding='utf-8')
    home=root/f'home-{index}';transport=PauseAfterRead(recorder)
    router=ModelRouter([provider],transport=transport,retries_per_provider=1)
    def agent():
        return cls(home=home,workspace=workspace,router=router,
            registry=build_builtin_registry(expose=['read_file','calculator']),project_rules=False,
            policy=Policy(workspace=workspace,mode=Mode.READ_ONLY,non_interactive=True),
            limits=LoopLimits(max_steps=8,context_chars=24000))
    start=len(recorder.rows);first_agent=agent()
    task=('Read values.json using read_file. Use calculator to add its a and b integers. '
          'Return only the numeric sum after actual calculation. Do not guess the file contents.')
    try:
        recorder.stage=f'{index}-{strategy}-before'
        first=first_agent.run(task)
        saved=copy.deepcopy(first_agent._conversation_prefix._saved)
    finally:first_agent.secret_scope.close()
    transport.active=False
    second_agent=agent();resume=len(recorder.rows)
    try:
        recorder.stage=f'{index}-{strategy}-resume'
        second=second_agent.run('Continue the original task. Use actual previous tool observations if '
            'available; otherwise read values.json. Use calculator and return only the numeric sum.',
            history=[{'role':'user','content':task},{'role':'assistant','content':first.text}])
        native_valid=all(valid_tool_pairs(p['messages']) for p in recorder.sent[start:])
        steps=first.steps+second.steps
        paused=(transport.injected>0 and first.stopped=='error'
                and [s.tool for s in first.steps]==['read_file'])
        expected=sanitize_messages(saved['messages'],provider.wire) if saved else []
        matched=bool(saved and len(recorder.sent)>resume and
            recorder.sent[resume]['messages'][:len(expected)]==expected)
        host_matched=bool(saved and transport.paused_messages==saved['messages'])
        return {'strategy':strategy,'sample':index,'ok':(paused and second.stopped=='final'
            and second.text.strip()=='423' and native_valid
            and any(s.tool=='calculator' and s.meta.get('value')==423 for s in second.steps)
            and all(s.decision=='ok' for s in steps)
            and (strategy=='baseline' or matched and host_matched)),
            'injected_pause':paused,'injected_attempts_no_http':transport.injected,
            'first_stopped':first.stopped,'second_stopped':second.stopped,'final':second.text,
            'file_reads':sum(s.tool=='read_file' for s in steps),'tools':[s.tool for s in steps],
            'checkpointed':any(e['type']=='cache_prefix_checkpointed' for e in first.events),
            'prefix_reused':any(e['type']=='cache_prefix_reused' for e in second.events),
            'saved_messages_match_first_resume_wire':matched,'native_pairs_valid':native_valid,
            'checkpoint_matches_interrupted_host_request':host_matched,
            'prefix_projection':'sanitize_messages(checkpoint, provider.wire)',
            'totals':totals(recorder.rows[start:]),'resume_totals':totals(recorder.rows[resume:])}
    finally:second_agent.secret_scope.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--vendor',choices=['mimo','seed'],required=True)
    parser.add_argument('--baseline-dir',type=Path,required=True)
    parser.add_argument('--strategies',nargs='+',choices=['baseline','improved'],default=['baseline','improved'])
    parser.add_argument('--request-limit',type=int,default=24)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.live or args.output.exists() or not 1<=args.request_limit<=40:
        parser.error('Requires --live, request-limit 1..40 and a new output path')
    chosen='mimo-v2.6-flash' if args.vendor=='mimo' else 'doubao-seed-evolving'
    provider=(mimo_provider if args.vendor=='mimo' else seed_provider)(chosen,Path.home()/'.forge')
    recorder=(DeepSeekRecorder(args.request_limit,retries=1) if args.vendor=='mimo' else SeedRecorder(args.request_limit))
    recorder.timeout=45
    before=load_before(args.baseline_dir)
    project=Path(__file__).resolve().parents[1]
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    result={'vendor':args.vendor,'model':chosen,'samples':[],
        'scope':'Only the pause is injected before HTTP dispatch; all reads, calculator calls and model responses are real.',
        'runtime_sha256':{'before_loop':sha(args.baseline_dir/'forge/loop.py'),
            'after_loop':sha(project/'forge/loop.py'),'script':sha(Path(__file__))}}
    try:
        with tempfile.TemporaryDirectory(prefix='forge-transport-resume-') as tmp:
            for index,strategy in enumerate(args.strategies,1):
                sample=run_sample(before if strategy=='baseline' else Agent,provider,recorder,Path(tmp),strategy,index)
                result['samples'].append(sample)
                print(json.dumps({k:sample[k] for k in ('strategy','sample','ok','file_reads','checkpointed','prefix_reused')},ensure_ascii=False),flush=True)
    except Exception as exc:
        result.update(error_type=type(exc).__name__,error=str(exc)[:300])
    finally:
        result.update(requests=recorder.rows,totals=totals(recorder.rows))
        result['ok']=(not result.get('error_type') and len(result['samples'])==len(args.strategies)
                      and all(s['ok'] for s in result['samples']))
        args.output.parent.mkdir(exist_ok=True,parents=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'ok':result['ok'],'totals':result['totals']},ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
