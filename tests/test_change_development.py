import unittest
import test_development_execution as fixtures
from keystone.execution import ActionRequest


class CodingReplyChangeTests(unittest.TestCase):
    setUp = fixtures.ExecutionTests.setUp
    runtime = fixtures.ExecutionTests.runtime

    def write(self, runtime):
        agent = runtime.bridge.agent
        agent.policy = runtime.bridge.policy('workspace-write')
        agent.action_executor.execute(agent.execution_context(), ActionRequest('read_file', {'path': 'value.py'}))
        response = agent.action_executor.execute(agent.execution_context(), ActionRequest('write_file',
            {'path': 'value.py', 'content': 'VALUE = 2\n'})).as_dict()
        self.assertTrue(response['ok'], response)
        return response['meta']['change_ids']

    def test_desktop_permission_profiles_restore_exact_receipts(self):
        for permission in ('desktop-sandbox', 'manual-approval', 'full-access'):
            with self.subTest(permission=permission):
                rt = self.runtime(permission)
                original = self.path.read_bytes()
                ids = self.write(rt)
                approvals = []
                rt.bridge.agent.action_executor.approver = lambda name, args, cancel: approvals.append(name) or True
                preview = rt.review_recorded_changes(ids)
                self.assertTrue(preview['canRevert'])
                result = rt.review_recorded_changes(ids, revert=True, permission=permission)
                self.assertTrue(result['restored'])
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(approvals, ['write_file'] if permission == 'manual-approval' else [])

    def test_manual_rejection_and_read_only_leave_file_intact(self):
        rt = self.runtime()
        ids = self.write(rt)
        rt.bridge.agent.action_executor.approver = lambda *_: False
        for permission in ('manual-approval', 'read-only'):
            with self.assertRaises(PermissionError):
                rt.review_recorded_changes(ids, revert=True, permission=permission)
        self.assertEqual(self.path.read_text(), 'VALUE = 2\n')

    def test_later_edits_during_approval_are_not_overwritten(self):
        rt = self.runtime()
        ids = self.write(rt)
        def approve(*_):
            self.path.write_text('USER_EDIT = True\n')
            return True
        rt.bridge.agent.action_executor.approver = approve
        with self.assertRaises(ValueError):
            rt.review_recorded_changes(ids, revert=True, permission='manual-approval')
        self.assertEqual(self.path.read_text(), 'USER_EDIT = True\n')


if __name__ == '__main__':
    unittest.main()
