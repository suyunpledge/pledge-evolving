"""F-1 acceptance: ten isolated refusal runs and two consecutive four-suite runs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
REFUSAL = 'development_mode.test_agent_panel.AgentPanelTests.test_revert_card_refuses_new_disk_revision'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bootstrap = ('import sys,unittest; sys.path[:0]=' + repr([str(ROOT), str(ROOT / 'forge-gui')])
        + "; loader=unittest.TestLoader(); suite=unittest.TestSuite(); "
        + "[suite.addTests(loader.loadTestsFromName(name)) for name in sys.argv[1:]]; "
        + "result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())")
    full_bootstrap = ('import sys,unittest; sys.path[:0]=' + repr([str(ROOT), str(ROOT / 'forge-gui')])
        + "; loader=unittest.TestLoader(); suite=loader.discover('tests',pattern='test_coding_agent_ux.py'); "
        + "[suite.addTests(loader.loadTestsFromName(name)) for name in sys.argv[1:]]; "
        + "result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())")
    runs = [(f'revert_{i:02}', bootstrap, [REFUSAL]) for i in range(1, 11)]
    runs += [(f'four_suites_round_{i}', full_bootstrap,
              ['development_mode.test_agent_panel', 'development_mode.test_app', 'test_workspace_wiring'])
             for i in range(1, 3)]
    paths = ['forge-gui/development_mode/app.py', 'forge-gui/development_mode/test_agent_panel.py',
             'tests/test_coding_agent_ux.py', 'forge-gui/development_mode/test_app.py',
             'forge-gui/test_workspace_wiring.py', 'tools/verify_coding_agent_f1.py']
    report = {'environment': {'python': sys.version, 'executable': sys.executable,
        'platform': platform.platform(), 'TCL_LIBRARY': os.environ.get('TCL_LIBRARY'),
        'TK_LIBRARY': os.environ.get('TK_LIBRARY'), 'model_transport': 'offline fixtures'},
        'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
        'runs': []}
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    for name, code, modules in runs:
        command = [sys.executable, '-B', '-c', code, *modules]
        log = output / (name + '.log')
        started = time.monotonic()
        with log.open('w', encoding='utf-8') as stream:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        content = log.read_text(encoding='utf-8')
        counts = re.findall(r'(?m)^Ran (\d+) tests?', content)
        row = {'name': name, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - started, 3),
            'total': int(counts[-1]) if counts else 0,
            'command': command, 'log': str(log), 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
            'tcl_installation_error': "Can't find a usable" in content and 'init.tcl' in content}
        for field in ('failures', 'errors', 'skipped'):
            counts = re.findall(r'\b' + field + r'=(\d+)', content)
            row[field] = int(counts[-1]) if counts else 0
        report['runs'].append(row)
        report['accepted'] = (len(report['runs']) == 12 and all(
            r['exit_code'] == 0 and r['total'] > 0 and r['skipped'] == 0 for r in report['runs']))
        (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
