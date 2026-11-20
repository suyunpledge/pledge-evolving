"""Retain mature Keystone pages inside the Qt window via a private stdio channel."""
import argparse
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid
import webbrowser

GUI = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(GUI), str(GUI.parent)]

import tkinter as tk
import keystone_gui_v2 as legacy
import secret_store
from interaction_model import model_label
from qt_frontend.store import SessionStore
from keystone.desktop_permissions import PROFILES
from keystone.secrets import redact
from keystone import sampling
from qt_frontend.permissions import authorize
from keystone.compat import default_home
from keystone.compat import desktop_config

_output_lock = threading.Lock()


def emit(event, **values):
    with _output_lock:
        print('KEYSTONE_UI:' + json.dumps({'event': event, **redact(values)}, ensure_ascii=False), flush=True)


class HostedApp(legacy.KeystoneGuiApp):
    def __init__(self, root, home, demo):
        self._embedded_workbench = True
        self._host_profile = 'sandbox'
        self._host_demo = demo
        self._host_waiters = {}
        self._host_closed = False
        self._mounted_page = 'work'
        self._navigating = False
        self._navigation_epoch = 0
        self._pending_model_restart = False
        legacy.DEFAULT_KEYSTONE_HOME = home
        if home != default_home():
            legacy._desktop_config_path = lambda: desktop_config(home)
            secret_store.SECRETS_FILE = home / 'secrets.json'
        super().__init__(root)
        prefs = SessionStore.read(home / 'gui' / 'qt' / 'onboarding.json', {})
        if isinstance(prefs, dict):
            self.apply_onboarding_preferences(prefs)
            self.select_setup_provider(prefs.get('provider'))
        self._inline_pages = {}
        self._hide_chrome()

    def _restore_workspace_preference(self):
        pass

    def _hide_chrome(self):
        for name in ('_chrome', '_topbar_divider', 'sidebar', 'activity_bar'):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.pack_forget()
        self._sidebar_visible = False

    def _apply_responsive_layout(self):
        self._responsive_after_id = None
        self._hide_chrome()

    def _apply_workspace_chrome(self, kind):
        super()._apply_workspace_chrome(kind)
        self._hide_chrome()

    def _show_view(self, key, **kwargs):
        super()._show_view(key, **kwargs)
        if hasattr(self, '_inline_pages') and not self._navigating:
            self._mounted_page = 'work' if key == 'chat' else key
            self._page_laid_out(self._mounted_page)

    def _page_laid_out(self, page, ticket=None):
        self._navigation_epoch += 1
        epoch = self._navigation_epoch

        def ready():
            if epoch == self._navigation_epoch and not self._host_closed:
                emit('navigate', page=page, ticket=ticket)

        # Let pack/grid and then any geometry-triggered callbacks settle
        # while the parent keeps the foreign window hidden.
        self.root.after_idle(lambda: self.root.after_idle(ready))

    def _open_provider_catalog(self, query=''):
        emit('provider_catalog', query=query)

    def use_provider_plan(self, key, label):
        preset = legacy.catalog.BY_KEY.get(key)
        plan = next((p for p in preset.plans if p.label == label and p.usable), None) if preset else None
        if plan is None:
            return
        self._clear_placeholder()
        self.input_text.delete('1.0', 'end')
        self.input_text.insert('1.0', legacy.catalog.config_snippet(preset, plan))
        self._set_status(f'已填入 {preset.name} · {plan.label} 的模板，请补充模型和 API Key。', 'ok')

    def _build_coding_surface(self, parent):
        from development_mode.app import DevelopmentWorkbench
        from keystone.development.state import ModeState
        from qt_frontend.coding_host import CodingHost
        dev_home = self.home / 'qt-development' if self._host_demo else Path.home() / '.keystone-development'
        state = ModeState(dev_home)
        state.switch('development')
        bench = DevelopmentWorkbench(parent, state, hosted=True,
            workspace_request=lambda mode: self.host_navigate('development' if mode == 'development' else 'work'),
            model_settings_home=self.home, models_changed=self._reload_configuration, qt_agent=True)
        bench.pack(fill='both', expand=True)
        self._coding_bench = bench
        self._coding_view = CodingHost(bench, emit, demo=self._host_demo)
        # The Qt permission control owns this host grant. Old controls redirect to it.
        bench._request_permission = lambda *_: emit('permissions')
        bench.permission_changed = lambda *_: emit('permissions')
        bench._on_policy_change = lambda **_: emit('permissions')
        capture_submission = bench._capture_submission
        def capture(*args, **kwargs):
            self._sync_host_permission()
            return capture_submission(*args, **kwargs)
        bench._capture_submission = capture
        project_loaded = bench._project_loaded
        def loaded(*args):
            project_loaded(*args)
            self._sync_host_permission()
        bench._project_loaded = loaded
        path = state.value('project')
        if path and Path(path).is_dir() and not self._host_demo:
            bench.open_project(path)

    def host_busy(self):
        bench = getattr(self, '_coding_bench', None)
        return bool(self._sending or self._task_running or bench and bench.runtime.busy)

    def host_unsaved(self):
        bench = getattr(self, '_coding_bench', None)
        documents = getattr(bench, 'documents', {}) if bench is not None else {}
        return bool(self._feature_dirty or self._team_dirty or self._pending_rows or self._editor_is_dirty()
                    or any(row['document'].dirty or row.get('saving') for row in documents.values()))

    def apply_permission(self, profile):
        if profile not in PROFILES:
            return
        if self.host_busy() or self._host_waiters:
            emit('error', message='请先停止当前任务，再切换权限。')
        else:
            self._host_profile = profile
            self._sync_host_permission()
        emit('permission', profile=self._host_profile)

    def _sync_host_permission(self):
        bench = getattr(self, '_coding_bench', None)
        if bench is None or bench.runtime.busy:
            return
        value = {'sandbox': 'desktop-sandbox', 'manual': 'manual-approval', 'full': 'full-access'}[self._host_profile]
        if bench.permission.get() != value:
            bench.permission.set(value)
            bench._permission = value
            bench._sync_perm_badge()
        agent = getattr(bench.runtime.bridge, 'agent', None)
        if agent is not None:
            agent.action_executor.approver = self.approve
            agent.action_executor.trusted_native = self._host_profile != 'sandbox'

    def _desktop_authorize_tool(self, name, args, cancel=None):
        authorize(self._host_profile, self._active_workspace(), name, args, self.approve, cancel)

    def approve(self, name, args, cancel=None):
        identifier = uuid.uuid4().hex
        done = threading.Event()
        waiter = {'done': done, 'approved': False}
        self._host_waiters[identifier] = waiter
        emit('approval', id=identifier, title=name,
             detail=json.dumps(redact(args), ensure_ascii=False, indent=2))
        try:
            while not done.wait(.1):
                if self._host_closed or cancel is not None and cancel.is_set():
                    return False
            return waiter['approved'] and not self._host_closed and not (cancel is not None and cancel.is_set())
        finally:
            self._host_waiters.pop(identifier, None)
            emit('approval_closed', id=identifier)

    def _open_inline_feature(self, tab):
        self.host_navigate({0: 'schedules', 1: 'connectors', 2: 'preview'}[tab])

    def _open_computer_panel(self):
        self.host_navigate('computer')

    def host_navigate(self, page, ticket=None):
        self._navigating = True
        try:
            self._mount_host_page(page)
        except Exception as exc:
            emit('navigation_error', ticket=ticket, message=str(redact(str(exc))))
            return
        finally:
            self._navigating = False
        self._page_laid_out(page, ticket)

    def _mount_host_page(self, page):
        if page in {'chat', 'connectors'} or self._mounted_page == page:
            return
        if page == 'development':
            self._request_workspace(self._workspace_kind.CODING)
            if self._workspace_mode != self._workspace_kind.CODING:
                raise RuntimeError(self.status_var.get())
        else:
            self._request_workspace(self._workspace_kind.WORK)
            if self._workspace_mode != self._workspace_kind.WORK:
                raise RuntimeError(self.status_var.get())
            panes = tuple(map(str, self.split.panes()))
            if str(self.center) not in panes:
                options = {'before': self.ws_holder} if str(self.ws_holder) in panes else {}
                self.split.add(self.center, minsize=300, stretch='always', **options)
            if page != 'files' and self._ws_packed:
                self._close_workspace()
            if page in {'preview', 'computer', 'connector_accounts'}:
                if page not in self._views:
                    frame = tk.Frame(self.center, bg=legacy.C['bg'])
                    self._views[page] = frame
                    if page == 'computer':
                        from computer_panel import ComputerPanel
                        self.computer_panel = ComputerPanel(self, parent=frame)
                    else:
                        self.desktop_features.mount_page(frame, 1 if page == 'connector_accounts' else 2)
                self._show_view(page)
            elif page == 'files':
                self._show_view('files')
                previous_tab = getattr(self.workspace, '_current_tab', None)
                self._open_workspace('file_tree')
                if self.workspace is not None:
                    self.workspace._on_close = self._request_files_collapse
                    if previous_tab is not None and previous_tab != legacy.tr('文件树'):
                        self.workspace._focus_band_for_tab(previous_tab)
                if self._ws_packed:
                    self.split.paneconfigure(self.ws_holder, stretch='always')
                    self.split.forget(self.center)
            else:
                self._show_view('chat' if page == 'work' else page)
        self._hide_chrome()
        self._sync_host_permission()
        self._mounted_page = page

    def _request_files_collapse(self):
        if self._mounted_page == 'files':
            emit('collapse_files')
        else:
            self._close_workspace()

    def _restore_workspace_width(self):
        if self._mounted_page != 'files':
            super()._restore_workspace_width()

    def _run_task(self):
        """Use the same core executor so manual approvals stay in the main window."""
        if self._task_running or not self.task_var.get().strip():
            return
        if self._host_demo:
            self._set_status('预览不发送模型请求。', 'info')
            return
        task, workspace = self.task_var.get().strip(), self._active_workspace()
        profile = self._host_profile
        self._task_running = True
        self._task_started = time.time()
        self._task_cancel_event = threading.Event()
        cancel = self._task_cancel_event
        self.task_area.clear()
        self.task_area.add_user(task)
        self._task_msg = self.task_area.add_agent(role='任务执行')
        self._task_msg.set_status('运行中…')
        self.task_run_btn.configure(state=tk.DISABLED)
        planning = self._read_planning_level()
        def work():
            from keystone.vscode_bridge import EditorBridge
            from keystone.development.models import ModelSettingsSource
            bridge = EditorBridge(lambda _: None, model_router_factory=ModelSettingsSource(self.home))
            try:
                bridge.initialize({'workspace': str(workspace), 'home': str(self.home.parent / '.keystone-qt-tasks')})
                bridge.agent.cancellation_event = cancel
                bridge.agent.action_executor.approver = self.approve
                bridge.agent.action_executor.trusted_native = profile != 'sandbox'
                result = bridge.run({'prompt': task, 'planning': planning,
                    'mode': {'sandbox': 'desktop-sandbox', 'manual': 'manual-approval', 'full': 'full-access'}[profile]})
                self._post_ui(self._task_finished, 0, json.dumps(result, ensure_ascii=False), '')
            except Exception as exc:
                self._post_ui(self._task_failed, str(redact(str(exc))))
            finally:
                bridge.close()
        threading.Thread(target=work, daemon=True, name='Keystone-Qt-task').start()

    def _do_send(self):
        if self._host_demo:
            self._set_status('预览不发送模型请求。', 'info')
            return
        super()._do_send()

    def apply_onboarding_preferences(self, prefs):
        language = prefs.get('language')
        locale = self.root._keystone_locale
        if language in {'en', 'zh-CN'} and locale.language != language:
            self._change_language(language)

    def select_setup_provider(self, row_id):
        if not row_id:
            return False
        row = next((row for row in self.user_rows if row['id'] == row_id and not row.get('disabled')), None)
        if row is None or row['config'].get('wire', 'openai') != 'openai':
            return False
        self._suppress_model_trace = True
        try:
            self.model_var.set(model_label(row['config']))
        finally:
            self._suppress_model_trace = False
        return True

    def apply_model_setup(self, row_id):
        if self.host_busy() or self.host_unsaved():
            emit('error', message='模型已保存，下一次启动工作台时生效。')
            return
        self._reload_configuration()
        if self.select_setup_provider(row_id) and not self._host_demo:
            if self._gateway_starting or (self.gateway_proc and self._gateway_ready_proc is None):
                self._pending_model_restart = True
            else:
                self._restart_gateway()

    def _gateway_started(self, proc, upstream):
        super()._gateway_started(proc, upstream)
        if self._pending_model_restart:
            self._pending_model_restart = False
            self._restart_gateway()

    def _gateway_up(self, proc):
        super()._gateway_up(proc)
        if self._pending_model_restart:
            self._pending_model_restart = False
            self._restart_gateway()
            return
        provider = self._gateway_provider
        temperature = sampling.resolve(provider.get('model'), provider.get('wire', 'openai'),
                                       provider.get('temperature'))
        emit('gateway', url=self.gateway_url, temperature=temperature)

    def _on_close(self):
        emit('navigate', page='chat')

    def close_host(self, reason='requested'):
        emit('closing', reason=reason)
        self._host_closed = True
        for waiter in list(self._host_waiters.values()):
            waiter['done'].set()
        bench = getattr(self, '_coding_bench', None)
        if bench is not None:
            bench.runtime.cancel()
        self._close_for_real()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    legacy._setup_dpi()
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    app = HostedApp(root, args.home, args.demo)
    root.minsize(300, 260)
    root.geometry('900x700-20000-20000')
    webbrowser.open = lambda url, *_, **__: emit('browser', url=str(url)) or True
    webbrowser.open_new = webbrowser.open_new_tab = webbrowser.open
    commands = queue.Queue()
    def reader():
        for line in sys.stdin:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    commands.put(row)
            except ValueError:
                pass
        commands.put({'command': 'close', 'reason': 'input pipe closed'})
    threading.Thread(target=reader, daemon=True).start()
    last_status = None
    def poll():
        nonlocal last_status
        if app._host_closed:
            return
        for _ in range(40):
            try:
                row = commands.get_nowait()
            except queue.Empty:
                break
            try:
                command = row.get('command')
                if command == 'navigate': app.host_navigate(row.get('page'), row.get('ticket'))
                elif command == 'provider_plan': app.use_provider_plan(row.get('provider'), row.get('plan'))
                elif command == 'onboarding_preferences': app.apply_onboarding_preferences(row)
                elif command == 'apply_model_setup': app.apply_model_setup(row.get('provider'))
                elif command == 'connector_account' and row.get('account') in {'github', 'microsoft'}:
                    if app._mounted_page == 'connector_accounts':
                        app.desktop_features.account.set(row['account'])
                elif command == 'coding' and hasattr(app, '_coding_view'):
                    app._coding_view.command(row)
                elif command == 'permission': app.apply_permission(row.get('profile'))
                elif command == 'approval':
                    waiter = app._host_waiters.get(row.get('id'))
                    if waiter:
                        waiter['approved'] = row.get('approved') is True
                        waiter['done'].set()
                elif command == 'close':
                    app.close_host(row.get('reason', 'requested'))
                    return
            except Exception as exc:
                emit('error', message=str(redact(str(exc))))
        app._sync_host_permission()
        if hasattr(app, '_coding_view'):
            app._coding_view.publish()
        status = {'busy': app.host_busy(), 'workspace': str(app._active_workspace()),
                  'status': str(app.status_var.get()), 'unsaved': app.host_unsaved()}
        if status != last_status:
            emit('status', **status)
            last_status = status
        root.after(100, poll)
    # Map Tk before embedding it. Showing a withdrawn HWND from Qt does not
    # update Tk's mapped state, so Tk rejects keyboard focus for its children.
    root.deiconify()
    root.update_idletasks()
    handle = int(root.wm_frame(), 0)
    if sys.platform == 'win32':
        # Hide the native frame until Qt mounts it, without withdrawing Tk.
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(handle), 0)  # SW_HIDE
    emit('ready', handle=handle)
    root.after(30, poll)
    root.mainloop()
    emit('host_stopped', closed=app._host_closed)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('error', message=str(redact(str(exc))))
        raise
