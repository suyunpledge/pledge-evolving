"""Explorer "Open with": path handling, project detection and single-instance hand-off."""
from pathlib import Path
import subprocess
import tempfile
import sys
import time
import unittest
import uuid

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from qt_frontend import open_paths


def app():
    if QCoreApplication.instance():
        return QCoreApplication.instance()
    from PySide6.QtGui import QGuiApplication
    return QGuiApplication([])


app()


class PathTests(unittest.TestCase):
    def test_normalize_resolves_relative_drops_missing_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'a.py'
            file.write_text('x = 1\n', encoding='utf-8')
            result = open_paths.normalize(['a.py', str(file), 'missing.py', ''], cwd=tmp)
            self.assertEqual(result, [str(file.resolve())])

    def test_project_is_git_root_for_nested_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            nested = root / 'src' / 'pkg'
            nested.mkdir(parents=True)
            file = nested / 'mod.py'
            file.write_text('', encoding='utf-8')
            self.assertEqual(Path(open_paths.project_for(str(file))), root)

    def test_project_is_folder_itself_or_parent_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve() / 'plain'
            folder.mkdir()
            file = folder / 'notes.txt'
            file.write_text('', encoding='utf-8')
            self.assertEqual(Path(open_paths.project_for(str(folder))), folder)
            project = Path(open_paths.project_for(str(file)))
            self.assertTrue(project == folder or folder.is_relative_to(project))

    def test_server_name_is_stable_per_home(self):
        self.assertEqual(open_paths.server_name('C:/Users/x/.revon'), open_paths.server_name('C:/Users/x/.revon'))
        self.assertNotEqual(open_paths.server_name('C:/Users/x/.revon'), open_paths.server_name('C:/Users/y/.revon'))


class HandOffTests(unittest.TestCase):
    def test_second_launch_forwards_paths_to_running_instance(self):
        app()
        home = Path(tempfile.gettempdir()) / ('revon-open-test-' + uuid.uuid4().hex)
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'b.md'
            file.write_text('# b\n', encoding='utf-8')
            server = open_paths.OpenRequests(home)
            self.assertTrue(server.listening)
            received, result = [], {}
            server.received.connect(received.append)
            # A real second launch is another process: run forward() there.
            code = ('import sys; from PySide6.QtCore import QCoreApplication; QCoreApplication([]);'
                    'from qt_frontend import open_paths;'
                    'sys.exit(0 if open_paths.forward([sys.argv[1]], sys.argv[2]) else 3)')
            child = subprocess.Popen([sys.executable, '-c', code, str(file), str(home)],
                                     cwd=str(Path(__file__).resolve().parents[1]))
            loop = QEventLoop()
            timer = QTimer(); timer.timeout.connect(lambda: loop.quit() if child.poll() is not None else None)
            timer.start(20)
            QTimer.singleShot(15000, loop.quit)
            loop.exec()
            result['ok'] = child.wait(5) == 0
            server.server.close()
            self.assertTrue(result.get('ok'))
            self.assertEqual(received, [[str(file.resolve())]])

    def test_forward_without_running_instance_returns_false(self):
        app()
        home = Path(tempfile.gettempdir()) / ('revon-open-none-' + uuid.uuid4().hex)
        self.assertFalse(open_paths.forward(['x'], home, timeout_ms=200))


if __name__ == '__main__':
    unittest.main()


class WorkbenchOpenTests(unittest.TestCase):
    """Real service host: queued before ready, switches project, opens the tab, shows Coding."""

    def test_open_file_switches_project_and_opens_tab(self):
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtTest import QTest
        from qt_frontend.workbench import WorkbenchController
        if not isinstance(QCoreApplication.instance(), QGuiApplication):
            self.skipTest('needs a GUI application instance (run this module on its own)')

        def until(check, seconds=60):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if check():
                    return True
                QTest.qWait(50)
            return False

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve() / 'repo'
            (project / 'src').mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(project)], check=True)
            target = project / 'src' / 'hello.py'
            target.write_text('print("hi")\n', encoding='utf-8')
            bench = WorkbenchController(home, demo=False)
            events, receive = [], bench._receive
            bench._receive = lambda row: (events.append(row.get('event')), receive(row))[1]
            try:
                bench.openPaths([str(target)])        # before the service is ready
                self.assertTrue(until(lambda: 'opened' in events), bench.error)
                self.assertEqual(Path(bench.workspace), project)
                self.assertTrue(until(lambda: bench.page in {'development', 'files'}))
                tabs = lambda: [t.get('path') for t in (bench._pages.get(bench.page) or {}).get('tabs', [])]
                self.assertTrue(until(lambda: str(target) in tabs(), 20), tabs())
                self.assertEqual(bench.error, '')
            finally:
                bench.shutdown()
