"""Coding 新手层：上手清单、Agent 空态锁定与模板、大白话权限入口。"""
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import secret_store
from forge.development.state import ModeState
from gui_theme import apply_ttk_theme

from development_mode.app import DevelopmentWorkbench
from development_mode.agent_panel import TASK_TEMPLATES
from development_mode.test_layout_regression import _WorkbenchFixture


class _Base(_WorkbenchFixture):
    open_project_on_start = False
    with_models_home = True

    def setUp(self):
        self.temp_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_home.cleanup)
        self.models_home = Path(self.temp_home.name)
        patcher = patch.object(secret_store, 'SECRETS_FILE', self.models_home / 'secrets.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.changed = []
        original = DevelopmentWorkbench.__init__
        home, changed = (self.models_home if self.with_models_home else None), self.changed

        def init(bench, *args, **kwargs):
            kwargs.setdefault('model_settings_home', home)
            kwargs.setdefault('models_changed', lambda: changed.append(1))
            original(bench, *args, **kwargs)
        init_patch = patch.object(DevelopmentWorkbench, '__init__', init)
        init_patch.start()
        self.addCleanup(init_patch.stop)
        super().setUp()


class ChecklistTests(_Base, unittest.TestCase):
    def test_first_run_shows_model_step_and_saving_unlocks_next_step(self):
        welcome = self.app.welcome
        self.pump(lambda: welcome.setup.winfo_ismapped())
        setup = welcome.setup
        self.assertFalse(setup.model_ready())
        self.assertFalse(welcome._action_row.winfo_ismapped())
        self.assertIs(welcome.open_button, setup.open_button)
        self.assertIn('AI 模型', self.app.chat._locked_reason)

        setup.key_var.set('sk-onboard')
        setup.save()
        self.pump(lambda: setup._model_done.winfo_ismapped())
        self.assertTrue(setup.model_ready())
        self.assertEqual(self.changed, [1])
        self.assertIn('打开项目', self.app.chat._locked_reason)
        self.assertEqual(secret_store.get_provider('deepseek'), 'sk-onboard')
        rows = json.loads((self.models_home / 'forge.patch.json').read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['id'], 'deepseek')

    def test_save_error_is_shown_and_nothing_is_written(self):
        setup = self.app.welcome.setup
        setup.save()
        self.assertIn('密钥', setup.feedback.cget('text'))
        self.assertFalse((self.models_home / 'forge.patch.json').exists())
        self.assertEqual(self.changed, [])

    def test_selecting_local_model_disables_key_entry(self):
        setup = self.app.welcome.setup
        setup._select_vendor('ollama')
        self.assertEqual(str(setup.key_entry.cget('state')), 'disabled')
        setup.save()
        self.assertTrue(setup.model_ready())

    def test_returning_user_with_model_and_recent_sees_action_cards(self):
        setup = self.app.welcome.setup
        setup.key_var.set('sk-x')
        setup.save()
        welcome = self.app.welcome
        welcome._recent_provider = lambda: [str(self.project)]
        welcome.refresh()
        self.pump(lambda: welcome._action_row.winfo_ismapped())
        self.assertFalse(setup.winfo_ismapped())
        self.assertIs(self.app.welcome.open_button, self.app.welcome.action_cards[0].action_button)


class NoModelSourceTests(_Base, unittest.TestCase):
    with_models_home = False

    def test_without_model_source_only_project_step_guides(self):
        setup = self.app.welcome.setup
        self.assertTrue(setup.model_ready())
        self.assertFalse(setup._model_step.winfo_manager())
        self.assertIn('打开项目', self.app.chat._locked_reason)


class ProjectOpenTests(_Base, unittest.TestCase):
    open_project_on_start = True

    def test_read_only_hint_grants_through_existing_confirmation(self):
        panel = self.app.agent_panel
        self.pump(lambda: panel._perm_hint.winfo_ismapped())
        self.assertEqual(panel._perm_chip.cget('text'), '只看不改')
        with patch('development_mode.app.messagebox.askyesno', return_value=False):
            panel._request_permission('workspace-write')
        self.assertEqual(self.app.permission.get(), 'read-only')
        with patch('development_mode.app.messagebox.askyesno', return_value=True) as ask:
            panel._request_permission('workspace-write')
        ask.assert_called_once()
        self.assertEqual(self.app.permission.get(), 'workspace-write')
        self.assertEqual(panel._perm_chip.cget('text'), '可改项目文件')
        self.pump(lambda: not panel._perm_hint.winfo_ismapped())
        panel._request_permission('read-only')
        self.assertEqual(self.app.permission.get(), 'read-only')

    def test_missing_model_after_project_open_offers_direct_setup(self):
        chat = self.app.chat
        self.pump(lambda: bool(chat._locked_reason))
        self.assertIn('模型', chat._locked_reason)
        self.assertEqual(chat._locked_action[0], '选择 AI 模型')
        chat._locked_action[1]()
        window = self.app._model_setup_window
        self.addCleanup(lambda: window.winfo_exists() and window.destroy())
        panel = window.winfo_children()[0]
        self.assertFalse(hasattr(panel, '_project_step'))
        panel.key_var.set('sk-later')
        panel.save()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(self.changed, [1])

    def test_templates_fill_draft_without_sending_or_overwriting(self):
        chat = self.app.chat
        with patch.object(self.app.runtime, 'readiness', return_value={'ready': True, 'code': 'ready'}):
            self.app._sync_agent_readiness()
        self.assertEqual(chat._locked_reason, '')
        prompt = TASK_TEMPLATES[0][3]
        chat.on_template(prompt)
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), prompt)
        chat.on_template(TASK_TEMPLATES[1][3])
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), prompt)


if __name__ == '__main__':
    unittest.main()
