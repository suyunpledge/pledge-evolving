import tempfile
from pathlib import Path
import unittest

from forge.change_review import ChangeReviewStore
from forge.execution import ActionExecutor, ActionRequest, create_execution_context
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry


class ChangeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ws, self.home = root / 'workspace', root / 'home'
        self.ws.mkdir()
        self.store = ChangeReviewStore(self.home, self.ws)
        self.context = create_execution_context(home=self.home, workspace=self.ws,
            policy=Policy(mode=Mode.BYPASS, workspace=self.ws, non_interactive=True),
            registry=build_builtin_registry())
        self.addCleanup(self.context.secret_scope.close)
        self.addCleanup(self.context.session.close)
        self.executor = ActionExecutor()

    def action(self, name, **arguments):
        result = self.executor.execute(self.context, ActionRequest(name, arguments)).as_dict()
        self.assertTrue(result['ok'], result.get('error'))
        return result['meta'].get('change_ids', [])

    def test_modified_created_deleted_and_preexisting_dirty_bytes(self):
        changed, deleted, untouched = [self.ws / p for p in ('a.py', 'b.py', 'user.txt')]
        original = b'# already dirty\r\nx = 1\r\n'
        changed.write_bytes(original)
        deleted.write_bytes(b'old file\n')
        untouched.write_bytes(b'user change')
        ids = self.action('write_file', path='a.py', content='x = 2\n')
        ids += self.action('write_file', path='new.py', content='new = True\n')
        ids += self.action('delete_file', path='b.py')
        preview = self.store.review(ids)
        self.assertTrue(preview['canRevert'])
        self.assertEqual({f['kind'] for f in preview['files']}, {'created', 'modified', 'deleted'})
        self.assertEqual(preview['files'][0]['removed'], 2)
        result = ChangeReviewStore(self.home, self.ws).revert(ids, lambda *_: None)
        self.assertTrue(result['restored'])
        self.assertEqual(changed.read_bytes(), original)
        self.assertEqual(deleted.read_bytes(), b'old file\n')
        self.assertFalse((self.ws / 'new.py').exists())
        self.assertEqual(untouched.read_bytes(), b'user change')
        self.assertFalse((self.ws / '.git').exists())
        self.assertTrue(self.store.revert(ids, lambda *_: self.fail('already restored'))['restored'])

    def test_conflict_prevents_entire_batch_and_preserves_later_edits(self):
        ids = self.action('write_file', path='a.py', content='agent')
        ids += self.action('write_file', path='b.py', content='agent')
        (self.ws / 'b.py').write_text('user later', encoding='utf-8')
        self.assertFalse(self.store.review(ids)['canRevert'])
        with self.assertRaisesRegex(ValueError, '后续修改'):
            self.store.revert(ids, lambda *_: None)
        self.assertEqual((self.ws / 'a.py').read_text(), 'agent')
        self.assertEqual((self.ws / 'b.py').read_text(), 'user later')

    def test_multiple_writes_merge_against_turn_start(self):
        (self.ws / 'a.py').write_text('original\n')
        ids = self.action('write_file', path='a.py', content='middle\n')
        ids += self.action('write_file', path='a.py', content='final\n')
        files = self.store.review(ids)['files']
        self.assertEqual(len(files), 1)
        self.assertIn('-original', files[0]['diff'])
        self.assertNotIn('middle', files[0]['diff'])
        self.store.revert(ids, lambda *_: None)
        self.assertEqual((self.ws / 'a.py').read_text(), 'original\n')

    def test_intervening_non_turn_edit_disables_revert(self):
        ids = self.action('write_file', path='a.py', content='first')
        (self.ws / 'a.py').write_text('user edit')
        self.action('read_file', path='a.py')
        ids += self.action('write_file', path='a.py', content='second')
        self.assertFalse(self.store.review(ids)['canRevert'])
        with self.assertRaises(ValueError):
            self.store.revert(ids, lambda *_: None)

    def test_preview_is_not_authorization_and_current_policy_is_rechecked(self):
        ids = self.action('write_file', path='a.py', content='new')
        self.assertTrue(self.store.review(ids)['canRevert'])
        def deny(*_):
            raise PermissionError('read only')
        with self.assertRaises(PermissionError):
            self.store.revert(ids, deny)
        self.assertEqual((self.ws / 'a.py').read_text(), 'new')

    def test_backup_store_is_not_model_readable(self):
        ids = self.action('write_file', path='a.py', content='new')
        result = self.executor.execute(self.context, ActionRequest('read_file', {'path': str(self.store.root / (ids[0]+'.json'))}))
        self.assertFalse(result.as_dict()['ok'])

    def test_binary_and_empty_file_are_restored_exactly(self):
        path = self.ws / 'data.bin'
        path.write_bytes(b'\x00\xff\r\n')
        before = self.store.capture(path)
        path.unlink()
        ids = [self.store.record(path, before)]
        self.assertTrue(self.store.review(ids)['files'][0]['binary'])
        self.store.revert(ids, lambda *_: None)
        self.assertEqual(path.read_bytes(), b'\x00\xff\r\n')
        ids = self.action('write_file', path='empty.py', content='')
        self.store.revert(ids, lambda *_: None)
        self.assertFalse((self.ws / 'empty.py').exists())

    def test_noop_and_create_then_delete_have_no_net_diff(self):
        ids = self.action('write_file', path='temp.py', content='x')
        self.assertEqual(self.action('write_file', path='temp.py', content='x'), [])
        ids += self.action('delete_file', path='temp.py')
        self.assertEqual(self.store.review(ids)['files'], [])
        self.assertFalse(self.store.revert(ids, lambda *_: None)['canRevert'])

    def test_ids_and_workspace_boundaries(self):
        for ids in (['../escape'], [{}], ['a'*32, 'a'*32]):
            with self.assertRaises(ValueError):
                self.store.review(ids)
        for path in ('../outside', '.git/config'):
            with self.assertRaises(ValueError):
                self.store.capture(path)


if __name__ == '__main__':
    unittest.main()
