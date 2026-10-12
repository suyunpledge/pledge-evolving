"""Development host contracts; isolated homes/projects and offline runtime."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from forge.development.state import ModeState, WorkMode
from forge.development.editor import Document, ConflictError, ProjectBoundary
from forge.development.runtime import DevelopmentRuntime


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.home = self.base / 'ide-home'

    def test_mode_layout_and_snapshot_are_independent(self):
        state = ModeState(self.home)
        notices = []
        state.subscribe(notices.append)
        state.switch(WorkMode.DEVELOPMENT)
        state.set_layout(visible=False, preset='editor', fractions=[.2, .7])
        snap = state.task_snapshot(model=('writer', 'one'))
        state.switch(WorkMode.GENERAL)
        state.configure_role('default', ('writer', 'two'))
        self.assertEqual(snap.mode, WorkMode.DEVELOPMENT)
        self.assertEqual(snap.model, ('writer', 'one'))
        restored = ModeState(self.home)
        self.assertEqual(restored.mode, WorkMode.GENERAL)
        self.assertFalse(restored.layout['visible'])
        restored.switch(WorkMode.DEVELOPMENT)
        self.assertEqual(restored.layout['preset'], 'editor')
        self.assertEqual(len(notices), 2)

    def test_role_priority(self):
        s = ModeState(self.home)
        s.switch('development')
        s.configure_role('default', ('dev', 'a'))
        s.configure_role('simple', ('simple', 'b'))
        self.assertEqual(s.task_snapshot(global_model=('g', 'x')).model, ('dev', 'a'))
        self.assertEqual(s.task_snapshot(role='simple').model, ('simple', 'b'))
        self.assertEqual(s.task_snapshot(agent_model=('agent', 'c')).model, ('agent', 'c'))
        self.assertEqual(s.task_snapshot(explicit_model=('task', 'd'), agent_model=('agent', 'c')).model, ('task', 'd'))
        s.switch('general')
        self.assertEqual(s.task_snapshot(global_model=('g', 'x')).model, ('g', 'x'))

    def test_all_role_models_are_part_of_task_snapshot(self):
        s = ModeState(self.home)
        s.configure_role('review', ('reviewer', 'old'))
        frozen = s.task_snapshot()
        s.configure_role('review', ('reviewer', 'new'))
        self.assertEqual(dict(frozen.roles)['review'], ('reviewer', 'old'))

    def test_state_does_not_persist_drafts_or_arbitrary_data(self):
        s = ModeState(self.home)
        with self.assertRaises(ValueError):
            s.set_layout(preset='bad')
        with self.assertRaises(ValueError):
            s.set_layout(fractions=[.8, .1])
        with self.assertRaises(ValueError):
            s.configure_role('default', ('unknown', 'sk-secret-test-123456789'))
        self.assertFalse((self.home / 'development.json').exists())

    def test_document_encoding_newlines_and_atomic_save(self):
        p = self.project / 'source.py'
        p.write_bytes(b'\xef\xbb\xbfprint(1)\r\n')
        d = Document.open(ProjectBoundary(self.project), p)
        self.assertEqual(d.text, 'print(1)\n')
        d.update('print(2)\n')
        self.assertTrue(d.dirty)
        d.save()
        self.assertEqual(p.read_bytes(), b'\xef\xbb\xbfprint(2)\r\n')
        self.assertEqual(d.status, 'saved')

    def test_conflict_never_overwrites_user_or_agent_changes(self):
        p = self.project / 'x.txt'
        p.write_text('before', encoding='utf-8')
        d = Document.open(ProjectBoundary(self.project), p)
        d.update('user unsaved')
        p.write_text('agent external', encoding='utf-8')
        self.assertTrue(d.check_external())
        with self.assertRaises(ConflictError):
            d.save()
        self.assertEqual(p.read_text(), 'agent external')
        self.assertEqual(d.text, 'user unsaved')

    def test_same_size_same_mtime_external_change_is_detected(self):
        import os
        p = self.project / 'x.txt'
        p.write_bytes(b'AAA')
        d = Document.open(ProjectBoundary(self.project), p)
        stat = p.stat()
        p.write_bytes(b'BBB')
        os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertTrue(d.check_external())

    def test_boundary_binary_and_secret_store(self):
        outside = self.base / 'outside.txt'
        outside.write_text('no')
        with self.assertRaises(PermissionError):
            Document.open(ProjectBoundary(self.project), outside)
        binary = self.project / 'binary'
        binary.write_bytes(b'\x00\xff')
        with self.assertRaises(ValueError):
            Document.open(ProjectBoundary(self.project), binary)
        from forge.secrets import protect_store_path
        store = self.project / 'store.json'
        store.write_text('{}')
        protect_store_path(store)
        with self.assertRaises(PermissionError):
            Document.open(ProjectBoundary(self.project), store)

    def test_deleted_file_requires_explicit_reload(self):
        p = self.project / 'x.txt'
        p.write_text('original')
        d = Document.open(ProjectBoundary(self.project), p)
        p.unlink()
        with self.assertRaises(ConflictError):
            d.save()
        self.assertFalse(p.exists())

    def test_save_failure_retains_original_and_cleans_temporary_file(self):
        from unittest.mock import patch
        p = self.project / 'x.txt'
        p.write_text('old')
        d = Document.open(ProjectBoundary(self.project), p)
        d.update('new')
        with patch('forge.development.editor.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                d.save()
        self.assertEqual(d.status, 'failed')
        self.assertTrue(d.dirty)
        self.assertEqual(p.read_text(), 'old')
        self.assertEqual(list(self.project.iterdir()), [p])

    def test_project_boundary_rejects_symlink_escape(self):
        outside = self.base / 'private.txt'
        outside.write_text('outside')
        link = self.project / 'linked.txt'
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest('Windows account cannot create symlinks')
        # QA-10：无开发者模式的 Windows 会静默创建 0 字节普通文件——
        # 逃逸根本没发生，断言 PermissionError 必挂。先验真链接。
        if not link.is_symlink():
            self.skipTest('symlink_to silently created a regular file (no developer mode)')
        with self.assertRaises(PermissionError):
            Document.open(ProjectBoundary(self.project), link)

    def test_unencodable_save_reports_failure_without_corrupting_file(self):
        p = self.project / 'latin.txt'
        p.write_bytes(b'plain')
        d = Document.open(ProjectBoundary(self.project), p, encoding='ascii')
        d.update('中文')
        with self.assertRaises(UnicodeError):
            d.save()
        self.assertEqual(d.status, 'failed')
        self.assertEqual(p.read_bytes(), b'plain')

    def test_recovery_preserves_actual_history_and_unknown_actions(self):
        from forge.session import Session
        s = Session(self.home / 'sessions' / 'development' / 'old.jsonl')
        with s:
            s.append('user_message', content='real task')
            s.append('workflow_state', run_id='r', phase='executing')
            s.append('tool_started', run_id='r', action_id='a', tool='write_file')
        rt = DevelopmentRuntime(ModeState(self.home))
        recovery = rt.recovery(s.path)
        self.assertEqual(recovery['workflow']['pending_actions'], ['a'])
        self.assertFalse(recovery['workflow']['replay_safe'])
        self.assertEqual(recovery['history'], [{'role': 'user', 'content': 'real task'}])

    def test_runtime_rejects_shared_or_project_local_home(self):
        rt = DevelopmentRuntime(ModeState(self.project / '.forge'))
        with self.assertRaises(ValueError):
            rt.initialize(self.project)

    def test_borrowed_agent_mount_preserves_session_and_host_ownership(self):
        owner = DevelopmentRuntime(ModeState(self.home))
        self.addCleanup(owner.close)
        owner.initialize(self.project)
        owner.bridge.agent.extra_system = 'host instructions'
        host_events, view_events = [], []
        owner.emit = host_events.append
        borrowed = DevelopmentRuntime(ModeState(self.base / 'view-prefs'), bridge=owner.bridge, emit=view_events.append)
        agent, journal, checkpoint = owner.bridge.agent, owner.bridge.agent.session, owner.bridge.agent.checkpoints
        ready = borrowed.initialize(self.project)
        owner.bridge.emit({'event': 'progress', 'data': {'type': 'real-event'}})
        self.assertEqual(host_events, view_events)
        self.assertIs(owner._lane, borrowed._lane)
        borrowed.close()
        self.assertIs(borrowed.bridge.agent, agent)
        self.assertIs(borrowed.bridge.agent.session, journal)
        self.assertIs(borrowed.bridge.agent.checkpoints, checkpoint)
        self.assertEqual(borrowed._base_extra_system, 'host instructions')
        self.assertEqual(ready['sessionPath'], str(journal.path))

    def test_switch_does_not_mutate_running_runtime(self):
        class FakeBridge:
            def run(self, params):
                started.set()
                proceed.wait(3)
                calls.append(params)
                return {'text': 'real result', 'ok': True}
        started, proceed = threading.Event(), threading.Event()
        calls = []
        state = ModeState(self.home)
        state.switch('development')
        rt = DevelopmentRuntime(state, bridge=FakeBridge())
        rt.ready = True
        rt.catalog = {'writer / one': ('writer', 'one')}
        thread = threading.Thread(target=lambda: rt.run('task', explicit_model=('writer', 'one')))
        thread.start()
        self.assertTrue(started.wait(2))
        state.switch('general')
        with self.assertRaises(RuntimeError):
            rt.run('concurrent')
        proceed.set()
        thread.join(3)
        self.assertEqual(calls[0]['mode'], 'read-only')
        self.assertEqual(calls[0]['model'], 'writer / one')
        self.assertEqual(rt.last_snapshot.mode, WorkMode.DEVELOPMENT)

    def test_payload_never_contains_secret_value(self):
        from forge.test_vscode_bridge import FakeRouter
        rt = DevelopmentRuntime(ModeState(self.home))
        self.addCleanup(rt.close)
        rt.initialize(self.project)
        fake = rt.bridge.router = FakeRouter()
        rt.bridge.agent.thinking_mode = 'off'
        rt.bridge.agent.thinking = False
        key = 'sk-development-synthetic-123456789'
        p = self.project / '.env'
        p.write_text('API_KEY=' + key)
        rt.run('explain config', contexts=[{'path': str(p), 'content': p.read_text()}], planning='none')
        self.assertNotIn(key, json.dumps(fake.messages))
        self.assertIn('SECRET_REF', json.dumps(fake.messages))

    def test_mode_guide_only_changes_on_next_task_not_permissions(self):
        from forge.test_vscode_bridge import FakeRouter
        from forge.development.runtime import DEVELOPMENT_GUIDANCE
        state = ModeState(self.home)
        rt = DevelopmentRuntime(state)
        self.addCleanup(rt.close)
        rt.initialize(self.project)
        rt.bridge.router = fake = FakeRouter()
        rt.bridge.agent.thinking_mode = 'off'
        rt.bridge.agent.thinking = False
        state.switch('development')
        frozen = state.task_snapshot()
        state.switch('general')
        rt.run('inspect', planning='none', snapshot=frozen)
        self.assertTrue(any(DEVELOPMENT_GUIDANCE in str(messages) for messages in fake.messages))
        self.assertFalse(rt.bridge.agent.policy.allows_write(self.project / 'x.py'))
        rt.run('general request', planning='none')
        self.assertNotIn(DEVELOPMENT_GUIDANCE, fake.messages[-1][0]['content'])


if __name__ == '__main__':
    unittest.main()
