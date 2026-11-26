from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

GUI = Path(__file__).resolve().parents[1] / 'revon-gui'
sys.path.insert(0, str(GUI))

from desktop.runtime_paths import default_workspace, qt_python, service_python
from desktop.source_launcher import resolve_launch
import revon_qt


class DesktopRuntimeTests(unittest.TestCase):
    def test_qt_can_load_brand_assets_without_tk(self):
        subprocess.run([sys.executable, '-B', '-c',
            "import sys; sys.modules['tkinter'] = None; from brand_marks import mark_path; "
            "assert mark_path('chatgpt', 16).is_file()"], cwd=GUI, check=True)

    def test_platforms_can_share_the_same_checkout(self):
        root = Path('revon project')
        for platform, expected in [('win32', 'qt/Scripts/pythonw.exe'),
                                   ('darwin', 'qt-darwin/bin/python'), ('linux', 'qt-linux/bin/python')]:
            with self.subTest(platform=platform):
                executable = qt_python(root, platform=platform, windowed=True)
                self.assertEqual(executable, root / '.venv' / expected)
                self.assertEqual(service_python(executable, platform=platform),
                                 qt_python(root, platform=platform))

    def test_installed_build_runs_children_with_its_console_twin(self):
        with tempfile.TemporaryDirectory() as td:
            gui = Path(td) / 'Revon.exe'
            gui.touch()
            # Without the twin (e.g. a renamed copy) the executable itself is used.
            self.assertEqual(service_python(gui, platform='win32'), gui)
            (Path(td) / 'revon-python.exe').touch()
            self.assertEqual(service_python(gui, platform='win32'), Path(td) / 'revon-python.exe')

    def test_installed_build_starts_in_documents_not_the_program_folder(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / 'checkout'
            self.assertEqual(default_workspace(repo), repo)
            with patch.object(sys, 'frozen', True, create=True), patch.object(Path, 'home', return_value=Path(td)):
                self.assertEqual(default_workspace(repo), Path(td) / 'Documents' / 'Revon')
            self.assertTrue((Path(td) / 'Documents' / 'Revon').is_dir())

    def test_service_preserves_the_actual_posix_interpreter(self):
        executable = Path('/path with spaces/venv/bin/python3.12')
        for platform in ('darwin', 'linux'):
            self.assertEqual(service_python(executable, platform=platform), executable)

    def test_launcher_keeps_arguments_and_source_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve() / 'source tree'
            gui = root / 'revon-gui'
            runtime = qt_python(root, windowed=True)
            for file in (root / 'run.py', gui / 'revon_qt.py', root / 'revon/__init__.py', runtime):
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            command, cwd = resolve_launch(Path(td) / 'revon', [
                '--source-root', str(root), '--page', 'files', '--home', 'test home'], {})
            self.assertEqual(cwd, gui)
            self.assertEqual(command, [str(runtime), str(gui / 'revon_qt.py'),
                                       '--page', 'files', '--home', 'test home'])

    @unittest.skipIf(sys.platform == 'win32', 'POSIX venv interpreters are symlinks')
    def test_reenters_venv_even_when_both_interpreters_resolve_to_same_binary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            executable = qt_python(root)
            executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
            with (patch.object(revon_qt, 'HERE', root / 'revon-gui'),
                  patch.dict(sys.modules, {'PySide6': None}),
                  patch.object(sys, 'argv', ['revon_qt.py', '--demo']),
                  patch.object(revon_qt.subprocess, 'call', return_value=7) as launch):
                self.assertEqual(revon_qt.main(), 7)
                self.assertEqual(launch.call_args.args[0][0], str(executable))
                self.assertEqual(launch.call_args.args[0][-1], '--demo')
                with patch.object(sys, 'prefix', str(executable.parent.parent)), self.assertRaises(SystemExit):
                    revon_qt.main()
                launch.assert_called_once()
