"""Serial real-Tk send regression plus the existing complete RC1 validation."""
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
GUI_SUITES = [
    ('coding_submission', ['development_mode.test_submission']),
    ('coding_app_agent', ['development_mode.test_app', 'development_mode.test_agent_panel']),
    ('coding_layout', ['development_mode.test_layout_regression']),
    ('workspace_visual', ['test_workspace_wiring', 'test_visual_system', 'test_coding_visual_integration']),
]
SOURCES = ['forge-gui/development_mode/app.py', 'forge-gui/development_mode/policy_widgets.py',
           'forge-gui/development_mode/test_app.py', 'forge-gui/development_mode/test_submission.py',
           'forge-gui/test_coding_visual_integration.py',
           'forge/development/runtime.py']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'environment': {'python': sys.version, 'executable': sys.executable,
        'platform': platform.platform()}, 'suites': [], 'gui_modules': dict(GUI_SUITES),
        'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}}
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    bootstrap = ('import sys,runpy;sys.path[:0]=' + repr([str(ROOT), str(ROOT / 'forge-gui')])
                 + ";sys.argv[0]='unittest';runpy.run_module('unittest',run_name='__main__')")
    for name, modules in GUI_SUITES:
        command = [sys.executable, '-B', '-c', bootstrap, *modules, '-v']
        log = output / (name + '.log')
        started = time.monotonic()
        with log.open('w', encoding='utf-8') as stream:
            done = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        value = log.read_text(encoding='utf-8')
        totals = re.findall(r'(?m)^Ran (\d+) tests', value)
        row = {'name': name, 'exit_code': done.returncode, 'total': int(totals[-1]) if totals else 0,
               'seconds': round(time.monotonic() - started, 3), 'log': str(log), 'command': command,
               'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
               'tcl_deleted_callback_warnings': len(re.findall('invalid command name', value))}
        for key in ('failures', 'errors', 'skipped'):
            hits = re.findall(r'\b' + key + r'=(\d+)', value)
            row[key] = int(hits[-1]) if hits else 0
        row['passed'] = row['total'] - sum(row[k] for k in ('failures', 'errors', 'skipped'))
        report['suites'].append(row)
        (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    command = [sys.executable, '-B', 'tools/verify_rc1.py', '--output', str(output / 'full')]
    with (output / 'full-driver.log').open('w', encoding='utf-8') as stream:
        done = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    report['full_validation_exit_code'] = done.returncode
    path = output / 'full' / 'validation.json'
    if path.exists():
        report['full_validation'] = json.loads(path.read_text(encoding='utf-8'))
        for row in report['full_validation']['suites']:
            print(json.dumps(row, ensure_ascii=False), flush=True)
    report['sources_unchanged'] = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == digest
                                     for p, digest in report['source_sha256'].items())
    (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if (done.returncode == 0 and report['sources_unchanged'] and
                 all(r['exit_code'] == 0 and r['total'] for r in report['suites'])) else 1


if __name__ == '__main__':
    raise SystemExit(main())
