"""Offline replay of exported model code against fresh immutable host tests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
from verify_tool_challenge import TESTS, EXTRA

rows = []
for report in sorted((ROOT / 'reports').glob('tool-challenge-*.json')):
    data = json.loads(report.read_text(encoding='utf-8'))
    for case in data['cases']:
        turn = case['turn']
        directory = report.parent / (report.stem + '-sources')
        sources = {name:directory / f'turn-{turn}-{name}' for name in ('pricing.py', 'reporting.py')}
        with tempfile.TemporaryDirectory(prefix='forge-tool-audit-') as tmp:
            workspace = Path(tmp)
            for name, path in sources.items():
                (workspace / name).write_bytes(path.read_bytes())
            (workspace / 'tests').mkdir()
            (workspace / 'tests/test_billing.py').write_text(TESTS + (EXTRA if turn == 2 else ''), encoding='utf-8')
            env = {k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP'}}
            env['PYTHONIOENCODING'] = 'utf-8'
            proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
                cwd=workspace, env=env, capture_output=True, text=True, encoding='utf-8', timeout=20)
        writes = [(i, s) for i, s in enumerate(case['steps']) if s['tool'] in {'write_file','edit_file','apply_patch'}]
        first_check = next((i for i,s in enumerate(case['steps']) if s['tool']=='run_checks'), None)
        reproduced = (first_check is not None and first_check < (writes[0][0] if writes else len(case['steps']))
                      and bool(case['checks']) and not case['checks'][0]['passed'])
        allowed = {'pricing.py','reporting.py'} if turn == 1 else {'reporting.py'}
        edits_allowed = all(s['tool'] != 'apply_patch' and s['args'].get('path') in allowed for _,s in writes)
        unchanged_pricing = turn == 1 or sources['pricing.py'].read_bytes() == (directory/'turn-1-pricing.py').read_bytes()
        match = ((proc.returncode == 0) == case['final']['passed'])
        verified = (match and case['integrity'] and edits_allowed and unchanged_pricing
                    and (not case['ok'] or reproduced))
        rows.append({'report':report.name,'turn':turn,'reported_ok':case['ok'],'replay_passed':proc.returncode==0,
            'reported_result_matches':match,'failure_reproduced_before_edit':reproduced,
            'observed_edit_paths_allowed':edits_allowed,'turn2_pricing_unchanged':unchanged_pricing,
            'evidence_verified':verified,'test_output':(proc.stdout+proc.stderr)[-1200:],
            'sources_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.values()}})
result = {'evidence_verified':all(r['evidence_verified'] for r in rows),'cases':rows,
          'scope':'Independent fixture replay and observed edit requests; pre-snapshot reports cannot prove absence of all other filesystem changes.'}
target = ROOT / 'reports/tool-specialization-audit.json'
target.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'evidence_verified':result['evidence_verified'],'cases':len(rows)}, ensure_ascii=False))
raise SystemExit(0 if result['evidence_verified'] else 1)
