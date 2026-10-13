"""Reproducible serial RC1 regression run; no paid model calls or source writes.

python -B tools/verify_rc1.py --output C:/path/to/isolated-results
Use a full Python installation with tkinter and optional PyYAML for GUI/config tests.
"""
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
GUI_MODULES = ['test_plugin_capabilities', 'test_plugin_skills', 'test_plugin_runtime',
    'test_plugin_lifecycle', 'test_phase_models', 'test_runtime_review', 'test_task_planning',
    'test_sub_agent', 'test_forge_client', 'test_interaction_intent']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bootstrap = ('import sys,runpy; sys.path[:0]=' + repr([str(ROOT), str(ROOT / 'forge-gui')])
                 + "; sys.argv[0]='unittest'; runpy.run_module('unittest',run_name='__main__')")
    suites = [('tests', ['-c', bootstrap, 'discover', '-s', 'tests', '-p', 'test*.py']),
              ('forge', ['-c', bootstrap, 'discover', '-s', 'forge', '-p', 'test*.py', '-t', '.']),
              ('selftest', ['run.py', 'selftest']), ('gui_adapters', ['-c', bootstrap, *GUI_MODULES])]
    report = {'environment': {'python': sys.version, 'executable': sys.executable,
        'platform': platform.platform()}, 'gui_modules': GUI_MODULES, 'suites': [],
        'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*ROOT.glob('forge/*.py'), ROOT / 'tests/test_runtime_convergence.py']}}
    try:
        import yaml
        report['environment']['PyYAML'] = yaml.__version__
    except ImportError:
        report['environment']['PyYAML'] = 'unavailable'
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    for name, arguments in suites:
        command = [sys.executable, '-B', *arguments]
        path = output / (name + '.log')
        started = time.monotonic()
        with path.open('w', encoding='utf-8') as log:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        text = path.read_text(encoding='utf-8')
        row = {'name': name, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - started, 3),
               'command': command, 'log': str(path), 'log_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if name == 'selftest':
            counts = re.findall(r'(?m)^(\d+)/(\d+) checks passed$', text)
            if counts:
                row['passed'], row['total'] = map(int, counts[-1])
                row['failures'] = row['total'] - row['passed']
        else:
            counts = re.findall(r'(?m)^Ran (\d+) tests', text)
            if counts:
                row['total'] = int(counts[-1])
                for key in ('failures', 'errors', 'skipped'):
                    match = re.findall(r'\b' + key + r'=(\d+)', text)
                    row[key] = int(match[-1]) if match else 0
                row['passed'] = row['total'] - sum(row[k] for k in ('failures', 'errors', 'skipped'))
        report['suites'].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(row['exit_code'] == 0 and row.get('total', 0) > 0 for row in report['suites']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
