"""Real Tk first-send admission, configuration repair and repeated interaction."""
import json
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from gui_theme import apply_ttk_theme
from forge.development.models import ModelSettingsSource
from forge.development.runtime import DevelopmentRuntime
from forge.development.state import ModeState
from forge.model import Usage
from development_mode.app import DevelopmentWorkbench
from development_mode import test_app


class FirstSendTests(unittest.TestCase):
    cleanup_tk = test_app.WorkbenchTests.cleanup_tk
    step = test_app.WorkbenchTests.step
    pump = test_app.WorkbenchTests.pump

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project, self.host = self.base/'project', self.base/'work'
        self.project.mkdir()
        self.host.mkdir()
        self.rows = [
            {'id':'writer','name':'provider:writer','config':{'baseURL':'http://127.0.0.1:9100/v1','model':'code-one'}},
            {'id':'model','name':'model:router','config':{'primary':['writer','code-one']}}]
        self.write_settings()
        self.calls = []
        calls = self.calls
        class Transport:
            def complete(_,provider,model,messages,**options):
                calls.append((provider.name,model,messages))
                return 'actual offline response',Usage(5,5,provider.name,model)
        self.source_original = ModelSettingsSource.__call__
        def load(source,config,home):
            router = self.source_original(source,config,home)
            router.transport = Transport()
            return router
        source_patch = patch.object(ModelSettingsSource,'__call__',load)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        initialize = DevelopmentRuntime.initialize
        def init(runtime,project):
            result = initialize(runtime,project)
            runtime.bridge.agent.thinking = False
            runtime.bridge.agent.thinking_mode = 'off'
            return result
        init_patch = patch.object(DevelopmentRuntime,'initialize',init)
        init_patch.start()
        self.addCleanup(init_patch.stop)
        self.root = tk.Tk()
        self.root.geometry('1200x760')
        self.root.attributes('-alpha',0)
        self.addCleanup(self.cleanup_tk)
        apply_ttk_theme(self.root)
        self.state = ModeState(self.base/'coding')
        self.state.switch('development')
        self.app = DevelopmentWorkbench(self.root,self.state,model_settings_home=self.host,hosted=True)
        self.app.pack(fill='both',expand=True)
        self.app.planning.set('none')
        self.errors = []
        self.root.report_callback_exception = lambda *args:self.errors.append(args)

    def write_settings(self):
        (self.host/'forge.patch.json').write_text(json.dumps(self.rows),encoding='utf-8')

    def send_first(self,draft='first task'):
        self.app.input.insert('1.0',draft)
        with patch('development_mode.app.filedialog.askdirectory',return_value=str(self.project)) as choose:
            self.app.send_button.invoke()
        self.assertEqual(choose.call_count,1)

    def finished(self,count=1):
        self.pump(lambda:len(self.app.runtime.tasks)==count and self.app._jobs==0 and not self.app._submission_active)
        self.assertEqual(self.errors,[])

    def test_first_send_selects_project_initializes_and_runs_exactly_once(self):
        self.assertFalse(self.app.runtime.ready)
        self.send_first()
        self.finished()
        self.assertEqual(self.app.source.root,self.project.resolve())
        self.assertEqual(self.app.input.get('1.0','end-1c'),'')
        self.assertEqual(self.calls[0][:2],('writer','code-one'))
        self.assertEqual(self.app.chat.get('1.0','end').count('first task'),1)
        self.assertEqual(self.app.task_evidence.phase,'completed')

    def test_cancel_project_selection_keeps_draft_and_deduplicates_notice(self):
        self.app.input.insert('1.0','keep draft')
        with patch('development_mode.app.filedialog.askdirectory',return_value=''):
            self.app.send_button.invoke()
            self.app.send_button.invoke()
        self.assertEqual(self.app.input.get('1.0','end-1c'),'keep draft')
        self.assertEqual(self.app.chat.get('1.0','end').count('发送需要项目目录'),1)
        self.assertFalse(self.app.runtime.tasks)

    def test_failed_initialization_retries_same_project_without_restart(self):
        with patch.object(ModelSettingsSource,'__call__',side_effect=ValueError('specific model configuration failure')):
            self.send_first()
            self.pump(lambda:not self.app._loading and self.app._jobs==0)
        self.assertIn('specific model configuration failure',self.app.chat.get('1.0','end'))
        self.assertEqual(self.app.input.get('1.0','end-1c'),'first task')
        with patch('development_mode.app.filedialog.askdirectory') as choose:
            self.app.send_button.invoke()
        choose.assert_not_called()
        self.finished()

    def block_loading(self):
        entered,release = threading.Event(),threading.Event()
        self.addCleanup(release.set)
        load = ModelSettingsSource.__call__
        def delayed(source,config,home):
            entered.set()
            if not release.wait(15): raise RuntimeError('test loading gate timed out')
            return load(source,config,home)
        return entered,release,patch.object(ModelSettingsSource,'__call__',delayed)

    def test_edit_during_initialization_prevents_automatic_send(self):
        entered,release,blocking = self.block_loading()
        with blocking:
            self.send_first()
            self.pump(entered.is_set)
            self.app.input.delete('1.0','end')
            self.app.input.insert('1.0','changed unsent draft')
            release.set()
            self.pump(lambda:self.app.runtime.ready and self.app._jobs==0)
            for _ in range(6): self.step()
        self.assertEqual(self.app.runtime.tasks,{})
        self.assertEqual(self.calls,[])
        self.assertEqual(self.app.input.get('1.0','end-1c'),'changed unsent draft')
        self.app.send_button.invoke()
        self.finished()

    def test_cancel_during_initialization_does_not_run_after_ready(self):
        entered,release,blocking = self.block_loading()
        with blocking:
            self.send_first()
            self.pump(entered.is_set)
            self.app.cancel_task()
            release.set()
            self.pump(lambda:self.app.runtime.ready and self.app._jobs==0)
            for _ in range(6): self.step()
        self.assertEqual(self.app.runtime.tasks,{})
        self.assertEqual(self.calls,[])
        self.assertEqual(self.app.input.get('1.0','end-1c'),'first task')

    def test_model_settings_repair_allows_send_without_losing_session(self):
        self.rows = []
        self.write_settings()
        self.send_first()
        self.pump(lambda:self.app.runtime.ready and self.app._jobs==0)
        for _ in range(6): self.step()
        self.assertIn('没有可用模型',self.app.chat.get('1.0','end'))
        self.assertEqual(self.app.input.get('1.0','end-1c'),'first task')
        session = self.app.runtime.bridge.agent.session
        self.rows = [{'id':'writer','name':'provider:writer','config':{'baseURL':'http://127.0.0.1:9100/v1','model':'repaired'}}]
        self.write_settings()
        self.app.send_button.invoke()
        self.finished()
        self.assertIs(self.app.runtime.bridge.agent.session,session)
        self.assertEqual(self.calls[0][:2],('writer','repaired'))

    def test_repeated_turns_keep_history_and_get_distinct_task_identities(self):
        self.send_first()
        self.finished()
        session = self.app.runtime.bridge.agent.session
        for index in range(2):
            self.app.input.insert('1.0','follow up '+str(index))
            self.app.send_button.invoke()
            self.finished(index+2)
        self.assertIs(self.app.runtime.bridge.agent.session,session)
        self.assertEqual(len(self.app.runtime.tasks),3)
        self.assertEqual(len(self.calls),3)
        self.assertTrue(any(m.get('content')=='first task' for m in self.calls[-1][2]))

    def test_worker_start_failure_resets_loading_and_retry_runs(self):
        with patch.object(self.app,'work',side_effect=RuntimeError('worker unavailable')):
            self.send_first()
        self.assertFalse(self.app._loading)
        self.assertIsNone(self.app._pending_initial_send)
        self.assertEqual(self.app.input.get('1.0','end-1c'),'first task')
        self.assertIn('worker unavailable',self.app.chat.get('1.0','end'))
        self.app.send_button.invoke()
        self.finished()

    def test_cancel_after_loaded_before_idle_resume_prevents_model_call(self):
        self.app.input.insert('1.0','first task')
        self.app._pending_initial_send = {'draft':'first task','delegate':False,'generation':self.app._generation+1}
        original = self.app._project_loaded
        def loaded(*args):
            original(*args)
            self.app.cancel_task()  # queued resume has not run yet
        with patch.object(self.app,'_project_loaded',loaded):
            self.app.open_project(self.project)
            self.pump(lambda:self.app.runtime.ready and self.app._jobs==0)
            for _ in range(6): self.step()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.app.runtime.tasks,{})
        self.assertEqual(self.app.input.get('1.0','end-1c'),'first task')

    def test_bad_model_refresh_preserves_draft_and_session_until_repaired(self):
        self.send_first()
        self.finished()
        session = self.app.runtime.bridge.agent.session
        (self.host/'forge.patch.json').write_text('{broken')
        self.app.input.insert('1.0','second task')
        self.app.send_button.invoke()
        self.assertEqual(self.app.input.get('1.0','end-1c'),'second task')
        self.assertEqual(len(self.calls),1)
        self.assertIn('配置文件无法解析',self.app.chat.get('1.0','end'))
        self.write_settings()
        self.app.send_button.invoke()
        self.finished(2)
        self.assertIs(self.app.runtime.bridge.agent.session,session)

    def test_interrupted_model_call_clears_busy_and_next_send_can_run(self):
        self.send_first()
        self.finished()
        transport = self.app.runtime.bridge.router.transport
        self.app.input.insert('1.0','interrupted task')
        with patch.object(transport,'complete',side_effect=KeyboardInterrupt('model request interrupted')):
            self.app.send_button.invoke()
            self.finished(2)
        self.assertFalse(self.app.runtime.busy)
        self.assertEqual(self.app.task_evidence.phase,'interrupted')
        self.assertEqual(self.app.input.get('1.0','end-1c'),'interrupted task')
        self.assertIn('KeyboardInterrupt',self.app.chat.get('1.0','end'))
        self.app.input.delete('1.0','end')
        self.app.input.insert('1.0','new task after interruption')
        self.app.send_button.invoke()
        self.finished(3)


if __name__=='__main__': unittest.main()
