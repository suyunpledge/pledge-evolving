"""Retain mature Forge pages inside the Qt window via a private stdio channel."""
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
import forge_gui_v2 as legacy
from forge.desktop_permissions import PROFILES
from forge.secrets import redact
from qt_frontend.permissions import authorize

_output_lock = threading.Lock()


def emit(event, **values):
    with _output_lock:
        print('FORGE_UI:' + json.dumps({'event': event, **redact(values)}, ensure_ascii=False), flush=True)


class HostedApp(legacy.ForgeGuiApp):
    def __init__(self, root, home, demo):
        self._embedded_workbench = True
        self._host_profile = 'sandbox'
        self._host_demo = demo
        self._host_waiters = {}
        self._host_closed = False
        self._host_page = 'work'
        legacy.DEFAULT_FORGE_HOME = home
        super().__init__(root)
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
        if hasattr(self, '_inline_pages'):
            emit('navigate', page='work' if key == 'chat' else key)

    def _build_coding_surface(self, parent):
        from development_mode.app import DevelopmentWorkbench
        from forge.development.state import ModeState
        dev_home = self.home / 'qt-development' if self._host_demo else Path.home() / '.forge-development'
        state = ModeState(dev_home)
        state.switch('development')
        bench = DevelopmentWorkbench(parent, state, hosted=True,
            workspace_request=lambda mode: self.host_navigate('development' if mode == 'development' else 'work'),
            model_settings_home=self.home, models_changed=self._reload_configuration)
        bench.pack(fill='both', expand=True)
        self._coding_bench = bench
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

    def host_navigate(self, page):
        if page == 'chat':
            self._host_page = page
            emit('navigate', page=page)
            return
        self._host_page = page
        if page == 'development':
            self._request_workspace(self._workspace_kind.CODING)
        else:
            self._request_workspace(self._workspace_kind.WORK)
            panes = tuple(map(str, self.split.panes()))
            if str(self.center) not in panes:
                options = {'before': self.ws_holder} if str(self.ws_holder) in panes else {}
                self.split.add(self.center, minsize=300, stretch='always', **options)
            if page != 'files' and self._ws_packed:
                self._close_workspace()
            if page in {'connectors', 'preview', 'computer'}:
                if page not in self._views:
                    frame = tk.Frame(self.center, bg=legacy.C['bg'])
                    self._views[page] = frame
                    if page == 'computer':
                        from computer_panel import ComputerPanel
                        self.computer_panel = ComputerPanel(self, parent=frame)
                    else:
                        self.desktop_features.mount_page(frame, 1 if page == 'connectors' else 2)
                self._show_view(page)
            elif page == 'files':
                self._show_view('files')
                self._open_workspace('file_tree')
                self.split.forget(self.center)
            else:
                self._show_view('chat' if page == 'work' else page)
        self._hide_chrome()
        self._sync_host_permission()
        emit('navigate', page=page)

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
            from forge.vscode_bridge import EditorBridge
            from forge.development.models import ModelSettingsSource
            bridge = EditorBridge(lambda _: None, model_router_factory=ModelSettingsSource(self.home))
            try:
                bridge.initialize({'workspace': str(workspace), 'home': str(self.home.parent / '.forge-qt-tasks')})
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
        threading.Thread(target=work, daemon=True, name='Forge-Qt-task').start()

    def _do_send(self):
        if self._host_demo:
            self._set_status('预览不发送模型请求。', 'info')
            return
        super()._do_send()

    def _gateway_up(self, proc):
        super()._gateway_up(proc)
        emit('gateway', url=self.gateway_url)

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
    root.geometry('900x700+0+0')
    root.update_idletasks()
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
                if command == 'navigate': app.host_navigate(row.get('page'))
                elif command == 'mounted':
                    # Qt owns native visibility. Tk deiconify schedules later
                    # remaps after layout/gateway updates and can cover chat.
                    emit('mounted')
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
        status = {'busy': app.host_busy(), 'workspace': str(app._active_workspace()),
                  'status': str(app.status_var.get()), 'unsaved': app.host_unsaved()}
        if status != last_status:
            emit('status', **status)
            last_status = status
        root.after(100, poll)
    emit('ready', handle=int(root.wm_frame(), 0))
    root.after(30, poll)
    root.mainloop()
    emit('host_stopped', closed=app._host_closed)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('error', message=str(redact(str(exc))))
        raise
