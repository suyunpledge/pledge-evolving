"""Replay all exported billing source and verify recorded acceptance independently."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT))
from verify_tool_challenge import TESTS, EXTRA

rows=[]
names=['mimo-a2','mimo-b','mimo-final-b','seed-a','seed-final-b']
for name in names:
    report=ROOT/'reports'/f'cache-progress-{name}.json'
    data=json.loads(report.read_text(encoding='utf-8'))
    for case in data['cases']:
        turn=case['turn'];directory=report.parent/(report.stem+'-sources')
        files={n:directory/f'turn-{turn}-{n}' for n in ('pricing.py','reporting.py')}
        with tempfile.TemporaryDirectory(prefix='forge-cache-audit-') as tmp:
            ws=Path(tmp)
            for n,p in files.items():(ws/n).write_bytes(p.read_bytes())
            (ws/'tests').mkdir()
            (ws/'tests/test_billing.py').write_text(TESTS+(EXTRA if turn==2 else ''),encoding='utf-8')
            env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP'}}
            env['PYTHONIOENCODING']='utf-8'
            proc=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-q'],
                cwd=ws,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
        allowed={'pricing.py','reporting.py'} if turn==1 else {'reporting.py'}
        pricing_unchanged=turn==1 or files['pricing.py'].read_bytes()==(directory/'turn-1-pricing.py').read_bytes()
        verified=((proc.returncode==0)==case['final']['passed'] and case['integrity']
            and set(case['changed_files'])<=allowed and pricing_unchanged
            and (not case['ok'] or case['failure_reproduced_before_edit']))
        rows.append({'report':report.name,'turn':turn,'reported_ok':case['ok'],'replay_passed':proc.returncode==0,
            'evidence_verified':verified,'test_output':(proc.stdout+proc.stderr)[-600:],
            'sources_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files.values()}})
result={'evidence_verified':bool(rows) and all(r['evidence_verified'] for r in rows),'cases':rows,
        'scope':'Fresh immutable fixture execution; failed experiments must reproduce their recorded failure.'}
(ROOT/'reports/cache-progress-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'evidence_verified':result['evidence_verified'],'cases':len(rows)}))
raise SystemExit(0 if result['evidence_verified'] else 1)
