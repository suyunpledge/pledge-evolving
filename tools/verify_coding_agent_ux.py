"""Serial reproducible Agent UX regression; full Python/Tk, offline model fixtures."""
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
        ('development_gui', ['-c', bootstrap, 'development_mode.test_app', 'development_mode.test_agent_panel', 'development_mode.test_project']),
        ('workspace_wiring', ['-c', bootstrap, 'test_workspace_wiring']),
        ('gui_adapters', ['-c', bootstrap, 'test_phase_models', 'test_task_planning', 'test_runtime_review',
            'test_sub_agent', 'test_forge_client', 'test_plugin_capabilities', 'test_plugin_runtime']),
        ('selftest', ['run.py', 'selftest'])]
    paths = [*ROOT.glob('forge/development/*.py'), *ROOT.glob('forge-gui/development_mode/*.py'),
        ROOT / 'tests/test_coding_agent_ux.py', Path(__file__)]
    report = {'environment': {'python': sys.version, 'executable': sys.executable,
        'platform': platform.platform(), 'model_transport': 'offline fixtures; no paid provider calls',
        'gui_verification': 'real Windows Tk, hidden test windows; no manual flicker observation'},
        'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, 'suites': []}
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    for name, arguments in suites:
        log = output / (name + '.log')
        command = [sys.executable, '-B', *arguments]
        started = time.monotonic()
        with log.open('w', encoding='utf-8') as stream:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        content = log.read_text(encoding='utf-8')
        row = {'name': name, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - started, 3),
            'command': command, 'log': str(log), 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}
        if name == 'selftest':
            counts = re.findall(r'(?m)^(\d+)/(\d+) checks passed$', content)
            if counts:
                row['passed'], row['total'] = map(int, counts[-1])
                row['failures'] = row['total'] - row['passed']
        else:
            counts = re.findall(r'(?m)^Ran (\d+) tests', content)
            if counts:
                row['total'] = int(counts[-1])
                for field in ('failures', 'errors', 'skipped'):
                    match = re.findall(r'\b' + field + r'=(\d+)', content)
                    row[field] = int(match[-1]) if match else 0
                row['passed'] = row['total'] - sum(row[field] for field in ('failures', 'errors', 'skipped'))
        report['suites'].append(row)
        (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if all(row['exit_code'] == 0 and row.get('total', 0) > 0 for row in report['suites']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
