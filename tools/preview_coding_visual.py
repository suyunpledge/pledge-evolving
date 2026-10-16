"""Render the real Coding workbench with an isolated, explicitly labelled fixture.

No user home, provider credentials, fabricated progress or network calls.
The screenshot is a real Tk window capture, never a painted mockup.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'forge-gui')]

SOURCE = '''"""Local inspection pipeline — isolated visual fixture."""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Inspection:
    workspace: Path
    read_only: bool = True

    def collect(self, pattern: str = "*.py") -> list[str]:
        # Inspect real files without changing the workspace.
        files = sorted(self.workspace.glob(pattern))
        return [path.name for path in files if path.is_file()]

    def describe(self) -> str:
        count = len(self.collect())
        return f"{count} source files · read only"


def build_report(workspace: Path) -> dict:
    inspection = Inspection(workspace=workspace)
    return {
        "workspace": workspace.name,
        "files": inspection.collect(),
        "summary": inspection.describe(),
        "read_only": inspection.read_only,
    }


if __name__ == "__main__":
    report = build_report(Path(__file__).parent)
    print(report["summary"])
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--size', default='1440x960')
    parser.add_argument('--work-snapshot', type=Path)
    parser.add_argument('--source-root', type=Path, default=ROOT,
                        help='Render another checkout for an equivalent before/after fixture')
    args = parser.parse_args()
    source_root = args.source_root.resolve(strict=True)
    sys.path[:0] = [str(source_root), str(source_root / 'forge-gui')]
    import tkinter as tk
    import _tkinter
    from PIL import ImageGrab
    from gui_theme import apply_ttk_theme
    from forge.development.state import ModeState
    from forge.test_task_planning import Router
    from development_mode.app import DevelopmentWorkbench
    with tempfile.TemporaryDirectory(prefix='forge-visual-') as temporary:
        base = Path(temporary)
        project = base / 'forge-inspection'
        (project / 'src').mkdir(parents=True)
        (project / 'tests').mkdir()
        (project / 'docs').mkdir()
        (project / 'src/pipeline.py').write_text(SOURCE, encoding='utf-8')
        (project / 'src/__init__.py').write_text('', encoding='utf-8')
        (project / 'tests/test_pipeline.py').write_text('import unittest\nfrom pathlib import Path\n'
            'from src.pipeline import build_report\nclass PipelineTests(unittest.TestCase):\n'
            '    def test_read_only(self): self.assertTrue(build_report(Path("src"))["read_only"])\n', encoding='utf-8')
        (project / 'README.md').write_text('# Forge inspection\nIsolated visual fixture.\n', encoding='utf-8')
        (project / 'docs/design.md').write_text('# Design\nNo user data or external services.\n', encoding='utf-8')
        root = tk.Tk()
        root.title('Forge · Coding visual verification · isolated fixture')
        root.geometry(args.size + '+24+24')
        apply_ttk_theme(root)
        state = ModeState(base / 'home')
        state.switch('development')
        state.set_layout(preset='editor')
        app = DevelopmentWorkbench(root, state)
        app.pack(fill='both', expand=True)
        errors = []
        root.report_callback_exception = lambda *exc: errors.append(str(exc[1]))

        def pump(condition, timeout=35):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for flags in (_tkinter.TIMER_EVENTS, _tkinter.IDLE_EVENTS, _tkinter.ALL_EVENTS):
                    root.tk.dooneevent(flags | _tkinter.DONT_WAIT)
                if condition():
                    return
                time.sleep(.01)
            raise RuntimeError('Preview condition timed out: ' + app.status.get())

        try:
            app.open_project(project)
            pump(lambda: app.source is not None and not app._loading)
            app.expanded.add(str(project / 'src'))
            app.refresh_project()
            app.open_file(project / 'src/pipeline.py')
            pump(lambda: bool(app.documents) and app._jobs == 0)
            app.runtime.bridge.router = Router([json.dumps({'tool': 'read_file', 'args': {'path': 'src/pipeline.py'}}),
                '## 文件检查完成\n\n已通过 `read_file` 读取 [pipeline.py](src/pipeline.py)。\n\n'
                '- 文件定义了 Inspection 和 build_report。\n- 本次是只读检查，没有修改文件。\n\n'
                '> 当前为隔离视觉夹具，使用离线模型传输。'])
            agent = app.runtime.bridge.agent
            agent.thinking, agent.thinking_mode = False, 'off'
            app.planning.set('none')
            ctx = app._runtime_ctx
            if ctx is not None:
                ctx.get_task_controls()._planning.set('none')
            app.input.insert('1.0', '检查 pipeline.py 的结构，不修改文件。')
            app.send()
            pump(lambda: app._jobs == 0 and not app.runtime.busy)
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                cwd=project, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True,
                timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            app.write_output('测试', '隔离视觉夹具 · 实际 unittest 输出（非用户项目测试）\n\n'
                + result.stdout + result.stderr, clear=True)
            app.bottom.select('测试')
            app.input.insert('1.0', '继续描述这个模块的调用关系…')
            root.lift()
            pump(lambda: root.winfo_ismapped() and app.chat.winfo_width() > 100)
            settle = time.monotonic() + 2
            while time.monotonic() < settle:
                for _ in range(80):
                    if not root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                        break
                root.tk.dooneevent(_tkinter.IDLE_EVENTS | _tkinter.DONT_WAIT)
                time.sleep(.01)
            root.update_idletasks()
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            # Capture ONLY this process-owned HWND; desktop crops can contain an
            # unrelated foreground app when Windows refuses foreground activation.
            ImageGrab.grab(window=int(root.frame(), 0)).save(args.snapshot)
            metadata = {'size': [root.winfo_width(), root.winfo_height()], 'callback_errors': errors,
                'fixture_test_exit_code': result.returncode, 'task_id': app.runtime.last_task_id,
                'screenshot': str(args.snapshot), 'transport': 'offline fixture',
                'panes': list(app.panes.panes()), 'style': str(app.tk.call('ttk::style', 'theme', 'use'))}
            args.snapshot.with_suffix('.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(metadata, ensure_ascii=False))
            if errors or result.returncode:
                raise RuntimeError('Preview or fixture test failed')
        finally:
            root.destroy()
    if args.work_snapshot:
        # The existing main-window fixture uses temporary preferences and no
        # daemon/model calls. This captures the real Work surface as well.
        from test_layout_dpi import isolated_app, pump as pump_main
        with isolated_app(size=(1440, 960)) as (root, app, errors):
            root.attributes('-alpha', 1)
            root.title('Forge · Work visual verification · isolated fixture')
            pump_main(root, .8)
            args.work_snapshot.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(window=int(root.frame(), 0)).save(args.work_snapshot)
            args.work_snapshot.with_suffix('.json').write_text(json.dumps({
                'surface': 'production Work Mode / isolated main-window fixture',
                'callback_errors': [str(e) for e in errors], 'screenshot': str(args.work_snapshot)},
                ensure_ascii=False, indent=2), encoding='utf-8')
            if errors:
                raise RuntimeError('Main-window callbacks failed')


if __name__ == '__main__':
    main()
