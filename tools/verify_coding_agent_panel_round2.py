"""Serial real-Tk acceptance for the Agent reference-panel increment."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import _tkinter

ROOT = Path(__file__).resolve().parents[1]
BASE = '998d3fc'


def bounded_workspace_pump(root, seconds=.15):
    """Same timing/real events, but Windows update() cannot starve the deadline."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        root.tk.dooneevent(_tkinter.TIMER_EVENTS | _tkinter.DONT_WAIT)
        root.tk.dooneevent(_tkinter.IDLE_EVENTS | _tkinter.DONT_WAIT)
        for _ in range(16):
            if not root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                break
        time.sleep(.005)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = 'forge-gui/development_mode/app.py'
    before = subprocess.check_output(['git', 'show', BASE + ':' + path], cwd=ROOT).decode('utf-8')
    def methods(source):
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
        return {n.name: ast.dump(n, include_attributes=False) for n in cls.body if isinstance(n, ast.FunctionDef)}
    old, new = methods(before), methods((ROOT / path).read_text(encoding='utf-8'))
    protected = ['_build', '_build_project', '_build_editor', 'apply_layout', 'place_sashes',
                 '_panes_resized', 'capture_layout', 'toggle_ide']
    assert all(old[name] == new[name] for name in protected)
    changed = [name for name in old if old[name] != new.get(name)]
    assert set(changed) <= {'_build_agent', '_toggle_events_tab', 'send', 'render_evidence', 'handle_event'}
    files = subprocess.check_output(['git', 'diff', '--name-only', BASE], cwd=ROOT).decode('utf-8').splitlines()
    forbidden = {'layout.py', 'bottom_panel.py', 'explorer.py', 'welcome.py', 'editor_area.py',
                 'workspace.py', 'gui_theme.py', 'chat_widgets.py'}
    assert not any(Path(p).name in forbidden for p in files)
    bootstrap = ('import sys,unittest,faulthandler; faulthandler.dump_traceback_later(120,repeat=True); sys.path[:0]=' + repr([str(ROOT), str(ROOT / 'forge-gui')])
        + "; loader=unittest.TestLoader(); suite=unittest.TestSuite(); "
        + "[suite.addTests(loader.loadTestsFromName(name)) for name in sys.argv[1:]]; "
        + "result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())")
    full = bootstrap.replace('suite=unittest.TestSuite()',
        "suite=loader.discover('tests',pattern='test_coding_agent_ux.py')")
    fixture_adapter = ("import runpy,test_layout_dpi,test_workspace_wiring; "
        + "pump=runpy.run_path(" + repr(str(Path(__file__).resolve())) + ")['bounded_workspace_pump']; "
        + "test_layout_dpi.pump=test_workspace_wiring.pump=pump; ")
    full = full.replace('loader=unittest.TestLoader()', fixture_adapter + 'loader=unittest.TestLoader()')
    runs = [('agent_panel', bootstrap, ['development_mode.test_agent_panel'])]
    runs += [(f'four_suites_round_{i}', full,
        ['development_mode.test_agent_panel', 'development_mode.test_app', 'test_workspace_wiring']) for i in (1, 2)]
    paths = [path, 'forge-gui/development_mode/agent_panel.py', 'forge-gui/development_mode/test_agent_panel.py',
             'forge-gui/development_mode/test_app.py', 'tests/test_coding_agent_ux.py',
             'forge-gui/test_workspace_wiring.py', 'tools/verify_coding_agent_panel_round2.py']
    report = {'baseline': BASE, 'environment': {'python': sys.version, 'executable': sys.executable,
        'platform': platform.platform(), 'TCL_LIBRARY': os.environ.get('TCL_LIBRARY'),
        'TK_LIBRARY': os.environ.get('TK_LIBRARY'), 'model_transport': 'offline fixtures',
        'workspace_fixture_pump': 'real timer/idle callbacks plus up to 16 window events per iteration, original duration; all suite assertions unchanged'},
        'ownership': {'protected_AST_identical': protected, 'modified_existing_app_methods': changed,
            'new_app_methods': [name for name in new if name not in old], 'WorkBuddy_domain_untouched': True},
        'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}, 'runs': []}
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    for name, code, modules in runs:
        command = [sys.executable, '-B', '-c', code, *modules]
        log = output / (name + '.log')
        started = time.monotonic()
        timed_out = False
        with log.open('w', encoding='utf-8') as stream:
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                    timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
                stream.write('\nAcceptance process exceeded 600 seconds; counted as failure.\n')
        content = log.read_text(encoding='utf-8')
        counts = re.findall(r'(?m)^Ran (\d+) tests?', content)
        row = {'name': name, 'exit_code': exit_code, 'timed_out': timed_out, 'total': int(counts[-1]) if counts else 0,
               'seconds': round(time.monotonic() - started, 3), 'command': command, 'log': str(log),
               'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()}
        for field in ('failures', 'errors', 'skipped'):
            matches = re.findall(r'\b' + field + r'=(\d+)', content)
            row[field] = int(matches[-1]) if matches else 0
        row['tcl_installation_error'] = "Can't find a usable" in content
        report['runs'].append(row)
        report['accepted'] = len(report['runs']) == 3 and all(
            r['exit_code'] == 0 and r['total'] > 0 and r['skipped'] == 0 for r in report['runs'])
        (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in row.items() if k != 'command'}, ensure_ascii=False), flush=True)
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
