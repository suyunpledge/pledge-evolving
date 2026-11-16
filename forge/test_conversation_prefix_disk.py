# -*- coding: utf-8 -*-
"""ConversationPrefix 磁盘持久化：跨实例第二轮前缀延续的回归锚点。"""
import json
import tempfile
import unittest
from pathlib import Path

from .conversation_prefix import ConversationPrefix


class PersistenceTests(unittest.TestCase):
    def _commit(self, path, history):
        cp = ConversationPrefix(path)
        ok = cp.commit(history=history,
                       messages=[{'role': 'system', 'content': 'S'},
                                 {'role': 'user', 'content': 'u1'}],
                       guard='g1', rule_context='', rule_sources=(),
                       targets=[], run_envelope='E')
        self.assertTrue(ok)
        return cp

    def test_roundtrip_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cp.json'
            self._commit(path, [{'role': 'user', 'content': 'u1'}])
            fresh = ConversationPrefix(path)
            saved = fresh.candidate([{'role': 'user', 'content': 'u1'}], 'g1')
            self.assertIsNotNone(saved)
            self.assertEqual(saved['messages'][0]['content'], 'S')
            # guard 不匹配 / 历史被编辑 → 不复用（与内存态同语义）
            self.assertIsNone(fresh.candidate([{'role': 'user', 'content': 'u1'}], 'g2'))
            self.assertIsNone(fresh.candidate([{'role': 'user', 'content': 'EDITED'}], 'g1'))

    def test_corrupt_file_falls_back_silently(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cp.json'
            path.write_text('{ not json', encoding='utf-8')
            self.assertIsNone(ConversationPrefix(path).candidate(
                [{'role': 'user', 'content': 'u1'}], 'g1'))
            path.write_text(json.dumps({'history_digest': 'x', 'guard': 'g1',
                                        'messages': 'not-a-list'}), encoding='utf-8')
            self.assertIsNone(ConversationPrefix(path).candidate(
                [{'role': 'user', 'content': 'u1'}], 'g1'))

    def test_clear_removes_disk_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cp.json'
            cp = self._commit(path, [{'role': 'user', 'content': 'u1'}])
            self.assertTrue(path.exists())
            cp.clear()
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
