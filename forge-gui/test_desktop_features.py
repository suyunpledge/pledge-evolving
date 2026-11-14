import copy
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.secrets import SecretScope
from forge.policy import Policy, Mode, Sandbox
from forge.tools import build_builtin_registry, ToolContext
from change_preview import prepare_change, check_revisions
import sub_agent as team
import secret_store


class FeatureBoundaryTests(unittest.TestCase):
    def test_explicit_agent_model_is_not_silently_rerouted(self):
        rows=[{'id':'a','config':{'baseURL':'https://a.invalid','model':'same'}},
              {'id':'b','config':{'baseURL':'https://b.invalid','model':'same'}}]
        original=copy.deepcopy(rows)
        with self.assertRaises(ValueError): team.resolve_selection(rows,{'model':'same'},rows[0]['config'])
        with self.assertRaises(ValueError): team.resolve_selection(rows,{'provider':'missing','model':'same'},rows[0]['config'])
        provider,model=team.resolve_selection(rows,{'provider':'b','model':'same'},rows[0]['config'])
        provider['model']='changed'
        self.assertEqual(rows,original)
        self.assertEqual(model,'same')

    def test_peer_communication_is_opt_in_and_ordinary_user_data(self):
        agents=[team.SubAgent('a','reader','system','task'),team.SubAgent('b','critic','system','task')]
        seen=[]
        def fake(provider,env,messages,**kw):
            seen.append(copy.deepcopy(messages)); provider['model']='mutated'; env['key']='mutated'
            return 'actual finding'
        provider={'model':'m'}; env={'key':'original'}
        with patch.object(team,'provider_chat',side_effect=fake):
            team.run_sub_agents(agents,env,default_provider=provider,default_model='m')
            self.assertEqual(len(seen),2)
            seen.clear()
            team.run_sub_agents(agents,env,default_provider=provider,default_model='m',communication=True,communication_rounds=1)
        self.assertEqual(len(seen),4)
        peer=[messages for messages in seen if 'Peer findings' in messages[-1]['content']]
        self.assertEqual(len(peer),2)
        self.assertTrue(all(m[-1]['role']=='user' and 'actual finding' in m[-1]['content'] for m in peer))
        self.assertEqual(provider,{'model':'m'}); self.assertEqual(env,{'key':'original'})

    def test_connector_credentials_never_enter_gateway_environment(self):
        env=secret_store.env_for({'model':'normal','connector.github':'github-token',
                                  'connector.microsoft':'token-json'})
        self.assertEqual(env,{'FORGE_MODEL_KEY':'normal'})

    def test_preview_does_not_write_and_detects_external_changes_at_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'main.py'; path.write_text('old\n')
            scope=SecretScope(); self.addCleanup(scope.close)
            change=prepare_change('edit_file',{'path':'main.py','old':'old','new':'new'},root,scope)
            self.assertEqual(path.read_text(),'old\n')
            self.assertIn('+new',change['preview'])
            path.write_text('external edit\n')
            with self.assertRaises(ValueError): check_revisions(change['arguments']['_expected_revisions'])
            ctx=ToolContext(Policy(mode=Mode.ACCEPT_EDITS,sandbox=Sandbox.WORKSPACE_WRITE,workspace=root),root,secret_scope=scope)
            result=build_builtin_registry().invoke('edit_file',change['arguments'],ctx)
            self.assertFalse(result.ok)
            self.assertEqual(path.read_text(),'external edit\n')

    def test_preview_blocks_escape_and_never_displays_real_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); scope=SecretScope(); self.addCleanup(scope.close)
            with self.assertRaises(PermissionError):
                prepare_change('write_file',{'path':'../escape','content':'x'},root,scope)
            path=root/'.env'; key='sk-preview-synthetic-secret-9876543210'
            path.write_text('API_KEY='+key+'\nMODEL=old\n')
            change=prepare_change('edit_file',{'path':'.env','old':'MODEL=old','new':'MODEL=new'},root,scope)
            self.assertNotIn(key,change['preview'])
            self.assertIn('SECRET_REF',change['preview'])

    def test_append_preview_matches_the_actual_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'file.txt'; path.write_text('first\n')
            scope=SecretScope(); self.addCleanup(scope.close)
            change=prepare_change('write_file',{'path':str(path),'content':'second\n','append':True},tmp,scope)
            self.assertNotIn('-first',change['preview']); self.assertIn('+second',change['preview'])

    def test_atomic_replace_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'file.txt'; path.write_text('original')
            context=ToolContext(Policy(mode=Mode.ACCEPT_EDITS,sandbox=Sandbox.WORKSPACE_WRITE,workspace=root),root)
            with patch('os.replace',side_effect=OSError('simulated disk failure')):
                result=build_builtin_registry().invoke('write_file',{'path':str(path),'content':'replacement'},context)
            self.assertFalse(result.ok); self.assertEqual(path.read_text(),'original')
            self.assertEqual(list(root.glob('.forge-edit-*')),[])

    def test_change_preview_never_silently_truncates_approved_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope=SecretScope(); self.addCleanup(scope.close)
            with self.assertRaisesRegex(ValueError,'complete preview'):
                prepare_change('write_file',{'path':'large.txt','content':'x'*130000},tmp,scope)
            self.assertFalse((Path(tmp)/'large.txt').exists())

    def test_windows_crlf_preview_uses_the_same_anchors_as_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'windows.txt'; path.write_bytes(b'first\r\nsecond\r\n')
            scope=SecretScope(); self.addCleanup(scope.close)
            change=prepare_change('edit_file',{'path':'windows.txt','old':'first\nsecond','new':'changed'},root,scope)
            self.assertIn('-second',change['preview']); self.assertIn('+changed',change['preview'])
            result=build_builtin_registry().invoke('edit_file',change['arguments'],
                ToolContext(Policy(mode=Mode.ACCEPT_EDITS,sandbox=Sandbox.WORKSPACE_WRITE,workspace=root),root,secret_scope=scope))
            self.assertTrue(result.ok,result.error)
            self.assertEqual(path.read_text(),'changed\n')


class DesktopUiTests(unittest.TestCase):
    def test_schedule_created_in_form_records_cancellation(self):
        from datetime import datetime, timedelta
        from desktop_features import DesktopFeatures
        from test_layout_dpi import isolated_app, pump

        def simulate_run(job, rows, scope, cancel):
            if not cancel.wait(8): raise RuntimeError('test run was not cancelled')
            return 'done', 'simulated output'

        with patch.object(DesktopFeatures, '_run_scheduled_job', side_effect=simulate_run), \
             isolated_app() as (root, app, errors):
            app._show_view('schedules')
            features = app.desktop_features
            view = features.jobs_views['workspace']
            view._open_form()
            view.name.set('Review job')
            view.due.set((datetime.now()-timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M'))
            view.prompt.insert('1.0', 'Review README')
            view.save_button.invoke()

            def wait_for_status(status):
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and not errors:
                    if [row['status'] for row in view.runs.values()] == [status]: return
                    pump(root, .02)
                self.fail(f'Expected {status!r}, got {view.runs!r} and Tk errors {errors!r}')

            wait_for_status('running')
            features.cancel_job()
            wait_for_status('cancelled')
            self.assertIsNone(view.form)
            self.assertEqual(view.result.get('1.0', 'end-1c'), 'simulated output')

    def test_schedule_history_keeps_selection_when_refreshing_both_surfaces(self):
        from test_layout_dpi import isolated_app, pump
        with isolated_app() as (root, app, errors):
            features = app.desktop_features
            self.assertTrue(features.ready.wait(3))
            features.stop.set()  # Use local fixture runs; no model requests.
            store = features.store
            key = store.add_job('Review', 'review README', app.home, ['p', 'm'], 100, 60)
            empty = store.add_job('Future', 'review later', app.home, ['p', 'm'], 600, 60)
            for now, output in ((100, 'first result'), (160, 'second result')):
                run = store.claim_due(now=now)[0]
                store.finish_job(key, 'done', output, run_id=run['run_id'])

            def wait_for(predicate):
                deadline = time.monotonic() + 3
                while not predicate() and not errors and time.monotonic() < deadline:
                    pump(root, .02)
                self.assertFalse(errors, errors)
                self.assertTrue(predicate())

            def output(view):
                return view.result.get('1.0', 'end-1c')

            app._show_view('schedules')
            main = features.jobs_views['workspace']
            wait_for(lambda: output(main) == 'second result')
            first = main.run_list.get_children()[-1]
            main.run_list.selection_set(first)
            wait_for(lambda: output(main) == 'first result')
            features.open(0)
            popup = features.jobs_views['popup']
            wait_for(lambda: output(popup) == 'second result')
            popup.job_list.selection_set(empty)
            wait_for(lambda: output(popup) == '' and not popup.run_list.get_children())
            self.assertEqual(main.job_list.selection(), (key,))
            self.assertEqual(main.run_list.selection(), (first,))
            features._close_window()

            run = store.claim_due(now=220)[0]
            store.finish_job(key, 'failed', 'third result', run_id=run['run_id'])
            features._refresh_jobs()
            wait_for(lambda: len(main.run_list.get_children()) == 3)
            self.assertEqual(output(main), 'first result')
            main.run_list.selection_set(main.run_list.get_children()[0])
            wait_for(lambda: output(main) == 'third result')
            app._change_language('en')
            self.assertEqual(main.run_list.heading('started', 'text'), 'Started at')
            self.assertEqual(output(main), 'third result')

    def test_chat_routes_connector_write_and_never_sends_credential_to_model(self):
        from test_layout_dpi import isolated_app,pump
        from forge_client import CompletionResult
        from forge.desktop_services import OPERATIONS
        import time
        with isolated_app() as (root,app,errors):
            desktop=app.desktop_features
            self.assertTrue(desktop.ready.wait(3))
            desktop.store.set_account('github',{'agent':True,'write':True,'authenticated':True})
            token='synthetic-github-integration-credential-012345'
            sent=[]; payloads=[]
            desktop.connectors.credential=lambda _:token
            desktop.connectors.transport=lambda *args,**kw:sent.append(args) or {'number':42}
            class Client:
                def health(self,**kw): return True,'ok'
                def list_tools(self,**kw): return []
                def stream_chat(self,messages,**kw):
                    payloads.append({'messages':[message.to_dict() for message in messages],'tools':kw.get('tools')})
                    if len(payloads)==1:
                        return CompletionResult('',tool_calls=[{'id':'operation','type':'function','function':{
                            'name':'connector_github_issue_create','arguments':json.dumps({'owner':'owner','repo':'repo','title':'Requested issue'})}}])
                    kw['on_chunk']('Confirmed result')
                    return CompletionResult('Confirmed result')
            app.client=Client(); app.send_var.set('Create the requested issue')
            with patch.object(desktop,'confirm',return_value=True) as confirm, \
                 patch.object(app,'_reload_plugin_tools',return_value=([],None)) as plugins, \
                 patch.object(app,'_write_sessions'):
                app._do_send()
                deadline=time.monotonic()+5
                while app._sending and time.monotonic()<deadline: pump(root,.03)
                self.assertFalse(app._sending)
                confirm.assert_called_once()
                self.assertTrue(set(OPERATIONS).issubset(plugins.call_args.kwargs['reserved_names']))
            self.assertEqual(len(sent),1)
            self.assertEqual(sent[0][2]['Authorization'],'Bearer '+token)
            self.assertEqual(len(payloads),2)
            self.assertTrue(json.loads(payloads[-1]['messages'][-1]['content'])['ok'])
            self.assertNotIn(token,json.dumps(payloads))
            self.assertFalse(errors,errors)

    def test_project_is_independent_of_forge_executable_and_connector_titles_are_unique(self):
        from test_layout_dpi import isolated_app,pump
        from forge.desktop_services import OPERATIONS
        from interaction_model import task_command
        with isolated_app() as (root,app,errors):
            features=app.desktop_features; features.open(2); pump(root,.1)
            old_executable=app.run_py
            project=app.home/'project'; project.mkdir(); project=project.resolve()
            features._set_project(project); pump(root,.1)
            self.assertEqual(app.run_py,old_executable)
            self.assertEqual(app._active_workspace(),project)
            self.assertEqual(app.workspace._repo_root,project)
            command=task_command('python','forge/run.py',app.home,'task','standard',workspace=project,profile='conservative')
            self.assertEqual(command[command.index('--workspace')+1],str(project))
            titles=[features._operation_title(name) for name in OPERATIONS]
            self.assertEqual(len(titles),len(set(titles)))
            features._close_window(); self.assertFalse(errors)

    def test_workspace_restart_preserves_draft_and_cannot_use_old_gateway(self):
        from test_layout_dpi import isolated_app
        with isolated_app() as (root,app,errors):
            app.send_var.set('Keep this draft')
            for flag in ('_gateway_starting','_restart_pending','_project_switch_pending'):
                with self.subTest(flag=flag),patch.object(app,flag,True,create=True),patch.object(app.client,'stream_chat') as call:
                    app._do_send()
                    call.assert_not_called()
                    self.assertEqual(app.send_var.get(),'Keep this draft')
                    self.assertFalse(app._sending)
            self.assertFalse(errors)

    def test_new_surface_supports_languages_scrolling_and_escape(self):
        from test_layout_dpi import isolated_app,pump,SCALES
        for scale in SCALES:
            with self.subTest(scale=scale),isolated_app(scale) as (root,app,errors):
                features=app.desktop_features; features.open(2)
                features.window.geometry('600x500'); pump(root,.1)
                self.assertTrue(features.window.bind('<Escape>'))
                self.assertTrue(app._change_language('en')); pump(root,.1)
                self.assertEqual(features.notebook.tab(2,'text'),'Code preview')
                self.assertFalse(app._team_dirty)
                canvas=features.preview_text.master.master.master
                self.assertIsInstance(canvas,tk.Canvas)
                self.assertTrue(canvas.cget('yscrollcommand'))
                canvas.yview_moveto(1); pump(root,.1)
                features._close_window(); self.assertFalse(errors)
    def test_dialog_return_preserves_sidebar_and_pending_change_is_rejectable(self):
        from test_layout_dpi import isolated_app,pump
        from test_navigation import NavigationTests
        with isolated_app() as (root,app,errors):
            features=app.desktop_features
            self.assertTrue(features.ready.wait(3))
            before=NavigationTests.main_layout(app)
            features.open(); pump(root,.1)
            self.assertTrue(features.window.winfo_exists())
            features._close_window(); pump(root,.1)
            self.assertEqual(before,NavigationTests.main_layout(app))
            path=app.home/'example.txt'; path.write_text('old')
            scope=SecretScope(); self.addCleanup(scope.close)
            outcome=[]
            def propose():
                try: features.review_change('write_file',{'path':str(path),'content':'new'},app.home,scope,threading.Event())
                except PermissionError: outcome.append('denied')
            worker=threading.Thread(target=propose); worker.start()
            for _ in range(50):
                pump(root,.02)
                if features.pending: break
            self.assertIsNotNone(features.pending)
            self.assertIn('+new',features.preview_text.get('1.0','end'))
            features._resolve_preview(False); worker.join(2)
            self.assertEqual(outcome,['denied']); self.assertEqual(path.read_text(),'old')
            features._close_window()
            self.assertFalse(errors)


if __name__=='__main__': unittest.main()
