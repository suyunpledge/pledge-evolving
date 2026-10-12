"""Read-only Git adapter tests with real temporary repositories."""
import tempfile
import unittest
from pathlib import Path
from workspace import _run_git
from development_mode.project import WorkspaceSource


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        _run_git(self.root, 'init', '--quiet')
        _run_git(self.root, 'config', 'user.name', 'forge-test')
        _run_git(self.root, 'config', 'user.email', 'forge-test@example.invalid')
        self.source = WorkspaceSource(self.root)

    def test_untracked_diff_reads_real_file_and_redacts_credentials(self):
        path = self.root / 'new.env'
        secret = 'sk-git-preview-synthetic-0123456789'
        path.write_text('MODEL=example\nAPI_KEY=' + secret, encoding='utf-8')
        preview = self.source.diff(path)
        self.assertIn('+MODEL=example', preview)
        self.assertNotIn(secret, preview)
        self.assertIn('REDACTED_SECRET', preview)
        self.assertIn('not staged', self.source.diff(path, 'staged'))

    def test_staged_and_working_diff_are_separate_and_do_not_change_index(self):
        path = self.root / 'code.py'
        path.write_bytes(b'original\n')
        _run_git(self.root, 'add', 'code.py')
        _run_git(self.root, 'commit', '-qm', 'base')
        path.write_bytes(b'staged-value\n')
        _run_git(self.root, 'add', 'code.py')
        path.write_bytes(b'working-value\n')
        before = _run_git(self.root, 'ls-files', '-s')
        staged = self.source.diff(path, 'staged')
        working = self.source.diff(path, 'working')
        self.assertIn('+staged-value', staged)
        self.assertNotIn('working-value', staged)
        self.assertIn('-staged-value', working)
        self.assertIn('+working-value', working)
        self.assertEqual(before, _run_git(self.root, 'ls-files', '-s'))
        with self.assertRaises(ValueError):
            self.source.diff(path, 'unknown')
        with self.assertRaises(PermissionError):
            self.source.diff(self.root.parent / 'outside.py')

    def test_open_repository_subdirectory_scopes_git_paths_correctly(self):
        folder = self.root / 'module'
        folder.mkdir()
        (folder / 'inside.py').write_bytes(b'new-code\n')
        (self.root / 'outside.py').write_bytes(b'outside-code\n')
        source = WorkspaceSource(folder)
        self.assertEqual(source.status(), {'inside.py': '??'})
        self.assertIn('+new-code', source.diff(folder / 'inside.py'))


if __name__ == '__main__':
    unittest.main()
