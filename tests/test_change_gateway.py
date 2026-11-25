import unittest
import test_runtime_convergence as fixtures
from revon.policy import Policy, Mode


class ChangeGatewayTests(unittest.TestCase):
    setUp = fixtures.RuntimeConvergenceTests.setUp
    gateway = fixtures.RuntimeConvergenceTests.gateway
    http = fixtures.RuntimeConvergenceTests.http

    def test_http_receipt_review_conflict_revert_and_auth(self):
        cfg, base = self.gateway()
        code, response = self.http(base, '/v1/tools/call', {'name': 'write_file',
            'arguments': {'path': 'value.txt', 'content': 'agent changed\n'}})
        self.assertTrue(response['ok'], response)
        body = {'change_ids': response['meta']['change_ids']}
        code, preview = self.http(base, '/v1/changes/review', body)
        self.assertEqual(code, 200, preview)
        self.assertTrue(preview['canRevert'])
        self.assertIn('-old', preview['files'][0]['diff'])
        written = self.path.read_bytes()
        try:
            self.assertEqual(self.http(base, '/v1/changes/revert', body, key='wrong')[0], 401)
        except ConnectionResetError:
            pass  # Windows may reset unread POST bodies on auth rejection.
        self.assertEqual(self.path.read_text(), 'agent changed\n')
        self.path.write_text('later edit', encoding='utf-8')
        code, result = self.http(base, '/v1/changes/revert', body)
        self.assertEqual(code, 409, result)
        self.assertEqual(self.path.read_text(), 'later edit')
        self.path.write_bytes(written)
        cfg.policy = Policy(mode=Mode.READ_ONLY, workspace=self.ws, non_interactive=True)
        code, result = self.http(base, '/v1/changes/revert', body)
        self.assertEqual(code, 409, result)
        self.assertEqual(self.path.read_text(), 'agent changed\n')
        cfg.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws, non_interactive=True)
        code, result = self.http(base, '/v1/changes/revert', body)
        self.assertEqual(code, 200, result)
        self.assertTrue(result['restored'])
        self.assertEqual(self.path.read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
