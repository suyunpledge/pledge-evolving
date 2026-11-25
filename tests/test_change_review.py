import json
import os
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from revon.change_review import ChangeReviewStore, DELETE_LIMIT, LIMIT
from revon.execution import ActionExecutor, ActionRequest, create_execution_context
from revon.policy import Policy, Mode, Sandbox
from revon.tools import build_builtin_registry


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

    def test_large_deletion_is_durable_before_unlink_and_has_one_copy(self):
        path = self.ws / 'untracked.txt'
        original = b'z ' * (LIMIT // 2) + b'!'
        path.write_bytes(original)
        prepared = []
        def observe(event):
            if event['type'] == 'delete_prepared':
                prepared.append((event['meta']['change_ids'], path.exists()))
        ctx = replace(self.context, subscriber=observe)
        request = ActionRequest('delete_file', {'path': path.name})
        result = self.executor.execute(ctx, request).as_dict()
        self.assertTrue(result['ok'], result['error'])
        ids = result['meta']['change_ids']
        self.assertEqual(prepared, [(ids, True)])
        self.assertFalse(path.exists())
        replay = self.executor.execute(self.context, request).as_dict()
        self.assertTrue(replay['meta']['deduplicated'])
        self.assertEqual([p.name for p in self.store.root.rglob('*')], [ids[0] + '.json'])
        recovered = ChangeReviewStore(self.home, self.ws)
        self.assertTrue(recovered.review(ids)['canRevert'])
        path.write_bytes(b'new file')
        self.assertFalse(recovered.review(ids)['canRevert'])
        with self.assertRaises(ValueError):
            recovered.revert(ids, lambda *_: None)
        self.assertEqual(path.read_bytes(), b'new file')
        path.unlink()
        self.assertTrue(recovered.revert(ids, lambda *_: None)['restored'])
        self.assertEqual(path.read_bytes(), original)

    def test_receipt_failure_uses_one_manual_backup(self):
        path = self.ws / 'draft.txt'
        original = b'uncommitted\r\n'
        path.write_bytes(original)
        with patch.object(ChangeReviewStore, '_save', side_effect=OSError('receipt unavailable')):
            result = self.context.registry.invoke('delete_file', {'path': path.name}, self.context.tool_context())
        self.assertTrue(result.ok, result.error)
        self.assertFalse(path.exists())
        backup = Path(result.meta['backup_path'])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(json.loads((backup.parents[1] / 'restore.json').read_bytes())['original_path'], str(path.resolve()))
        self.assertNotIn('change_ids', result.meta)
        denied = self.context.registry.invoke('read_file', {'path': str(backup)}, self.context.tool_context())
        self.assertFalse(denied.ok)

    def test_both_recovery_writes_fail_and_source_stays_intact(self):
        path = self.ws / 'original.txt'
        path.write_bytes(b'keep')
        with patch('revon.change_review.atomic_bytes', side_effect=OSError('disk full')):
            result = self.executor.execute(self.context, ActionRequest('delete_file', {'path': path.name})).as_dict()
        self.assertFalse(result['ok'])
        self.assertEqual(result['meta']['execution_state'], 'not_started')
        self.assertEqual(path.read_bytes(), b'keep')

    def test_receipt_creation_denied_falls_back_without_retrying_temp_names(self):
        path = self.ws / 'restricted.txt'
        path.write_bytes(b'user draft')
        original_open = os.open
        attempts = []
        def deny_receipt(name, flags, *args, **kwargs):
            if flags & os.O_CREAT and Path(name).parent == self.store.root:
                attempts.append(name)
                self.assertEqual(len(attempts), 1, 'ACL denial must not retry temporary names')
                raise PermissionError('receipt directory denies new files')
            return original_open(name, flags, *args, **kwargs)
        with patch('os.open', side_effect=deny_receipt):
            recovery = self.store.delete(path)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(path.exists())
        self.assertEqual(Path(recovery['backup_path']).read_bytes(), b'user draft')

    def test_interrupted_completion_exposes_prepared_receipt_without_replaying_delete(self):
        path = self.ws / 'interrupted.txt'
        path.write_bytes(b'original')
        request = ActionRequest('delete_file', {'path': path.name})
        append = self.context.session.append
        def interrupt(etype, **data):
            if etype == 'tool_completed':
                raise OSError('journal interrupted')
            return append(etype, **data)
        with patch.object(self.context.session, 'append', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'journal interrupted'):
                self.executor.execute(self.context, request)
        self.assertFalse(path.exists())
        recovered = ActionExecutor()
        unknown = recovered.query(self.context, request.action_id)
        self.assertEqual(unknown['meta']['execution_state'], 'unknown')
        self.store.revert(unknown['meta']['change_ids'], lambda *_: None)
        retry = recovered.execute(self.context, request).as_dict()
        self.assertTrue(retry['meta']['deduplicated'])
        self.assertEqual(path.read_bytes(), b'original')

    def test_restore_does_not_clobber_file_created_during_final_write(self):
        from revon.change_review import atomic_bytes
        path = self.ws / 'race.txt'
        path.write_bytes(b'original')
        ids = self.action('delete_file', path=path.name)
        def late_user_write(target, *args, **kwargs):
            if kwargs.get('create_only'):
                target.write_bytes(b'new user file')
            return atomic_bytes(target, *args, **kwargs)
        with patch('revon.change_review.atomic_bytes', side_effect=late_user_write):
            with self.assertRaises(FileExistsError):
                self.store.revert(ids, lambda *_: None)
        self.assertEqual(path.read_bytes(), b'new user file')
        self.assertFalse(self.store.review(ids)['canRevert'])

    def test_external_delete_backs_up_only_after_authorization(self):
        path = self.ws.parent / 'external.txt'
        path.write_bytes(b'outside')
        ctx = self.context.tool_context()
        ctx.policy = Policy(workspace=self.ws, mode=Mode.BYPASS, sandbox=Sandbox.WORKSPACE_WRITE)
        self.assertFalse(self.context.registry.invoke('delete_file', {'path': str(path)}, ctx).ok)
        self.assertFalse(self.store.root.exists())
        ctx.policy = Policy(workspace=self.ws, mode=Mode.BYPASS, sandbox=Sandbox.FULL_ACCESS)
        result = self.context.registry.invoke('delete_file', {'path': str(path)}, ctx)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(path.exists())
        backup = Path(result.meta['backup_path'])
        self.assertEqual(backup.read_bytes(), b'outside')
        self.assertIn(str(backup), result.content)
        self.assertFalse(self.context.registry.invoke('read_file', {'path': str(backup)}, ctx).ok)

    def test_oversize_and_replacement_during_capture_keep_source(self):
        path = self.ws / 'data.txt'
        with path.open('wb') as f:
            f.truncate(DELETE_LIMIT + 1)
        result = self.executor.execute(self.context, ActionRequest('delete_file', {'path': path.name})).as_dict()
        self.assertFalse(result['ok'])
        self.assertEqual(path.stat().st_size, DELETE_LIMIT + 1)
        path.write_bytes(b'original')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.store.delete(path, on_prepared=lambda *_: path.write_bytes(b'new user draft'))
        self.assertEqual(path.read_bytes(), b'new user draft')


if __name__ == '__main__':
    unittest.main()
