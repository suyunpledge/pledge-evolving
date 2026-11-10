"""Paid, isolated Seed/MiMo coding challenge with immutable host checks."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from verify_seed_execution import SeedRecorder, provider_for as seed_provider
from verify_mimo_live import provider_for as mimo_provider, router_for
from verify_deepseek_execution import DeepSeekRecorder, totals
from forge.loop import Agent, LoopLimits
from forge.compaction import ContextBudget
from forge.file_revisions import FileRevisionGuard
from forge.policy import Policy, Mode, Sandbox, ToolEffect
from forge.tools import build_builtin_registry, ToolSpec, ToolResult
from forge.probes import ProbeRegistry
from forge.builtin_views import command_metadata, FIELDS

SOURCE = {
 'pricing.py': '''def line_total(unit_price, quantity, discount="0"):
    return round(float(unit_price) * quantity * (1 - float(discount) / 100), 2)
''',
 'reporting.py': '''from pricing import line_total

def build_report(orders):
    totals = {}
    for order in orders:
        currency = order["currency"]
        totals[currency] = totals.get(currency, 0) + line_total(order["price"], order["quantity"], order.get("discount", "0"))
    return {k: str(v) for k, v in totals.items()}
''',
 'SPEC.md': '''Fix this small billing library without third party packages.
line_total(unit_price, quantity, discount="0") returns Decimal rounded to 0.01 with ROUND_HALF_UP.
Convert numeric input via Decimal(str(value)); reject nonfinite or negative price, discount outside 0..100,
and quantity that is bool, non-int or <=0, with ValueError. Preserve default discount and caller inputs.
build_report(orders) returns currency->two-decimal-string sorted by currency. Skip cancelled orders.
Each paid order adds its rounded line total; each refunded order subtracts it. Never change input dictionaries.
Only edit pricing.py and reporting.py. Tests and SPEC are host fixtures, never edit them.
run_checks executes the immutable regression suite; a tool success means execution, inspect passed and exit_code.
Before editing, reproduce failures with run_checks. Inspect relevant sources using tools.
Then fix both modules and run_checks again until passed=true. Do not claim success without actual checks.
'''
}
TESTS = '''import unittest
from decimal import Decimal
from pricing import line_total
from reporting import build_report

def row(price="1.005", quantity=1, status="paid", currency="USD", **kw):
    return dict(price=price, quantity=quantity, status=status, currency=currency, **kw)

class BillingTests(unittest.TestCase):
    def test_half_up(self): self.assertEqual(line_total("1.005",1),Decimal("1.01"))
    def test_type(self): self.assertIsInstance(line_total("2.50",2),Decimal)
    def test_discount(self): self.assertEqual(line_total("12.35",3,"12.5"),Decimal("32.42"))
    def test_no_float_drift(self): self.assertEqual(line_total("0.1",3),Decimal("0.30"))
    def test_invalid_quantity(self):
        for value in [True,0,-1,1.5,"2"]:
            with self.subTest(value=value), self.assertRaises(ValueError): line_total("1",value)
    def test_invalid_money(self):
        for price,discount in [("NaN","0"),("Infinity","0"),("-1","0"),("1","-1"),("1","101"),("1","NaN")]:
            with self.subTest(price=price,discount=discount), self.assertRaises(ValueError): line_total(price,1,discount)
    def test_paid_refund_cancel(self):
        self.assertEqual(build_report([row(),row(status="refunded"),row(status="cancelled")]),{"USD":"0.00"})
    def test_per_line_rounding(self): self.assertEqual(build_report([row(),row()]),{"USD":"2.02"})
    def test_sorted_currencies(self):
        result=build_report([row(currency="USD"),row(currency="EUR")])
        self.assertEqual(list(result),["EUR","USD"])
    def test_inputs_unchanged(self):
        orders=[row(),row(status="refunded")];before=[dict(r) for r in orders]
        build_report(orders);self.assertEqual(orders,before)
    def test_empty(self): self.assertEqual(build_report([]),{})
'''
EXTRA = '''
    def test_currency_filter(self):
        self.assertEqual(build_report([row(currency="USD"),row(currency="EUR")],only_currency="USD"),{"USD":"1.01"})
    def test_missing_filter(self): self.assertEqual(build_report([row()],only_currency="JPY"),{})
    def test_none_compatible(self): self.assertEqual(build_report([row()],only_currency=None),{"USD":"1.01"})
'''


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(workspace):
    return {p.relative_to(workspace).as_posix():sha(p) for p in workspace.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts}


def run_case(provider,recorder,root,output,*,feedback_views=False,budget_cls=ContextBudget,agent_cls=Agent):
    workspace=root/'workspace';workspace.mkdir()
    for name,body in SOURCE.items(): (workspace/name).write_text(body,encoding='utf-8')
    (workspace/'tests').mkdir()
    test=workspace/'tests/test_billing.py';test.write_text(TESTS,encoding='utf-8')
    history=[];cases=[];checks=[]
    for turn in (1,2):
        if turn==2: test.write_text(TESTS+EXTRA,encoding='utf-8')
        immutable={p:sha(p) for p in [test,workspace/'SPEC.md']}
        before=snapshot(workspace)
        def check():
            if any(sha(p)!=digest for p,digest in immutable.items()):
                return {'passed':False,'exit_code':-1,'output':'Host fixture changed; restore it.'}
            env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP'}}
            env['PYTHONIOENCODING']='utf-8'
            proc=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-v'],
                cwd=workspace,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
            row={'passed':proc.returncode==0,'exit_code':proc.returncode,'output':(proc.stdout+proc.stderr)[-10000:]}
            checks.append(row)
            return row
        baseline=check()
        registry=build_builtin_registry(expose=['read_file','list_dir','find_files','grep','file_outline',
            'read_range','edit_file','apply_patch','write_file','review_changes','artifact_read','artifact_grep','run_checks'])
        def run_checks(args,ctx):
            observed=check()
            return ToolResult(True,json.dumps(observed,ensure_ascii=False),meta=(
                command_metadata(observed['output'],observed['exit_code']) if feedback_views else {}))
        registry.register(ToolSpec('run_checks','Execute the fixed local regression suite. Inspect passed and exit_code; fix code on failure.',
            run_checks,read_only=False,result_fields=FIELDS if feedback_views else (),
            schema={'type':'object','properties':{},'additionalProperties':False}),trusted=True,effect=ToolEffect.NATIVE)
        probes=ProbeRegistry.defaults().with_probe('fixture','regression',lambda ctx,args:check()['passed'])
        recorder.stage=f'challenge-turn-{turn}'
        start=len(recorder.rows);check_start=len(checks)
        agent=agent_cls(home=root/'home',workspace=workspace,router=router_for(provider,recorder),registry=registry,
            policy=Policy(workspace=workspace,mode=Mode.ACCEPT_EDITS,sandbox=Sandbox.WORKSPACE_WRITE,
                          allow=('run_checks',),non_interactive=True),project_rules=False,planning='low',probes=probes,
            file_guard=FileRevisionGuard(workspace,require_read=True),
            limits=LoopLimits(max_steps=18,context_chars=12000))
        agent.compactor=budget_cls(max_chars=12000,keep_tail=6)
        task=('Read SPEC.md and inspect the relevant sources. First reproduce the failing suite with run_checks. '
              'Fix the two billing modules according to SPEC.md using actual tools, then run checks to verify. '
              'Only pricing.py and reporting.py may change. Keep public signatures compatible. Do not edit tests. '
              'Final reply briefly describe changes and actual tests.' if turn==1 else
              'Extend build_report with optional only_currency=None. None preserves existing behavior; a string '
              'includes only that exact currency. Host tests now include this feature. Reproduce the new failure '
              'with run_checks, inspect current files, modify only reporting.py, then run_checks until passed. '
              'Keep all original behavior and do not edit tests or SPEC.md. Use actual tools.')
        try:
            report=agent.run(task,history=history,acceptance={'max_attempts':2,'criteria':[
                {'id':'regression','kind':'env_probe','env':'fixture','probe':'regression','input':{},'expect':True,'timeout':30}]})
            final=check()
            integrity=all(sha(p)==digest for p,digest in immutable.items())
            tools=[{'tool':s.tool,'args':s.args,'decision':s.decision,'result':s.result} for s in report.steps]
            executed=any(s.tool=='run_checks' for s in report.steps)
            after=snapshot(workspace)
            changed=sorted(p for p in before.keys()|after.keys() if before.get(p)!=after.get(p))
            allowed={'pricing.py','reporting.py'} if turn==1 else {'reporting.py'}
            first_check=next((i for i,s in enumerate(report.steps) if s.tool=='run_checks'),None)
            first_write=next((i for i,s in enumerate(report.steps) if s.tool in {
                'write_file','edit_file','apply_patch','edit_config','delete_file','notebook_edit'}),len(report.steps))
            reproduced=(first_check is not None and first_check<first_write
                        and bool(checks[check_start:]) and not checks[check_start]['passed'])
            ok=(report.stopped=='final' and final['passed'] and integrity and executed
                and reproduced and set(changed)<=allowed)
            directory=output.parent/(output.stem+'-sources');directory.mkdir(exist_ok=True)
            for name in ('pricing.py','reporting.py'):
                (directory/f'turn-{turn}-{name}').write_bytes((workspace/name).read_bytes())
            cases.append({'turn':turn,'ok':ok,'stopped':report.stopped,'text':report.text,'baseline':baseline,
                'final':final,'integrity':integrity,'steps':tools,'planning':report.planning,
                'changed_files':changed,'failure_reproduced_before_edit':reproduced,
                'context_summaries':[m['content'] for m in (agent._last_messages or [])
                    if isinstance(m.get('content'),str) and m['content'].startswith('[compacted ')],
                'events':[e for e in report.events if e['type'] in {'compaction','acceptance_checked','cache_prefix_reused'}],
                'checks':checks[check_start:],'totals':totals(recorder.rows[start:])})
            history.extend([{'role':'user','content':task},{'role':'assistant','content':report.text}])
        finally: agent.secret_scope.close()
        if not cases[-1]['ok']: break
    return cases


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--vendor',choices=['seed','mimo'],required=True)
    parser.add_argument('--model');parser.add_argument('--request-limit',type=int,default=48)
    parser.add_argument('--feedback-views',action='store_true')
    parser.add_argument('--thinking',choices=['auto','on','off'],default='auto')
    parser.add_argument('--baseline-dir',type=Path,help='Explicit local before-edit loop/compaction backup')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not args.live or args.output.exists(): parser.error('Requires --live and a new output path')
    chosen=args.model or ('doubao-seed-evolving' if args.vendor=='seed' else 'mimo-v2.6-flash')
    provider=(seed_provider if args.vendor=='seed' else mimo_provider)(chosen,Path.home()/'.forge')
    budget_cls=ContextBudget
    agent_cls=Agent
    if args.baseline_dir:
        def load(name):
            spec=importlib.util.spec_from_file_location('forge._challenge_baseline_'+name,args.baseline_dir/f'forge/{name}.py')
            module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
            return module
        budget_cls=load('compaction').ContextBudget
        agent_cls=load('loop').Agent
    recorder=SeedRecorder(args.request_limit) if args.vendor=='seed' else DeepSeekRecorder(args.request_limit,retries=1)
    if args.thinking!='auto':
        if args.vendor=='mimo': provider.reasoning_mode=args.thinking
        else: recorder.thinking=args.thinking=='on'
    recorder.timeout=45
    result={'vendor':args.vendor,'model':chosen,'thinking':args.thinking,'feedback_views':args.feedback_views,'cases':[]}
    result['compaction_sha256']=sha(args.baseline_dir/'forge/compaction.py' if args.baseline_dir else Path(__file__).resolve().parents[1]/'forge/compaction.py')
    project=Path(__file__).resolve().parents[1]
    result['loop_sha256']=sha(args.baseline_dir/'forge/loop.py' if args.baseline_dir else project/'forge/loop.py')
    result['compaction_notes_sha256']=None if args.baseline_dir else sha(project/'forge/compaction_notes.py')
    result['runtime_sha256']={name:sha(project/name) for name in [
        'scripts/verify_tool_challenge.py','forge/artifact_tools.py','forge/artifacts.py',
        'forge/builtin_views.py','forge/tools.py','forge/compaction.py']}
    try:
        with tempfile.TemporaryDirectory(prefix='forge-tool-challenge-') as tmp:
            result['cases']=run_case(provider,recorder,Path(tmp),args.output,feedback_views=args.feedback_views,
                                     budget_cls=budget_cls,agent_cls=agent_cls)
    except Exception as exc:
        result['error_type']=type(exc).__name__
        result['error']=str(exc)[:300]
    finally:
        result['requests']=recorder.rows;result['totals']=totals(recorder.rows)
        result['ok']=not result.get('error_type') and len(result['cases'])==2 and all(c['ok'] for c in result['cases'])
        args.output.parent.mkdir(exist_ok=True,parents=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'ok':result['ok'],'error_type':result.get('error_type'),'totals':result['totals']},ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__': raise SystemExit(main())
