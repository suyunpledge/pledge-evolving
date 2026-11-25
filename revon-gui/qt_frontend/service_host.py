"""Private JSON service process. No UI toolkit, foreign window or message pump."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import uuid

GUI = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(GUI), str(GUI.parent)]

from revon.secrets import redact
from qt_frontend.store import SessionStore
from qt_frontend.gateway_service import GatewayService
from qt_frontend.workspace_service import WorkspaceService
from qt_frontend.workspace_navigation import Explorer, WorkspaceQueries
from qt_frontend.coding_service import CodingService
from qt_frontend.run_service import RunService
from qt_frontend.terminal_service import TerminalService
from revon.compat import desktop_config


class ServiceHost:
    def __init__(self, home, *, demo=False, emit=None):
        self.home, self.demo = Path(home).resolve(), demo
        self.home.mkdir(parents=True, exist_ok=True)
        import secret_store
        secret_store.SECRETS_FILE = self.home / 'secrets.json'
        secret_store.load()
        prefs = SessionStore.read(desktop_config(self.home), {})
        selected = Path(prefs.get('project_workspace') or GUI.parent).resolve()
        self.workspace = selected if selected.is_dir() else GUI.parent
        self.profile, self.page = 'sandbox', 'chat'
        self.running, self.closed = False, threading.Event()
        self.cancel = threading.Event()
        self.lock, self.out_lock = threading.RLock(), threading.Lock()
        self.waiters = {}
        self.active_call = ''
        self.worker = None
        self.pending_model = None
        self.navigation = ThreadPoolExecutor(max_workers=1, thread_name_prefix="revon-page-loader")
        self._emitter = emit
        self.gateway = GatewayService(self.home, self.workspace, self.emit, demo=demo)
        self.pages = WorkspaceService(self)
        self.queries = WorkspaceQueries(self.emit)
        self.terminal = TerminalService(self)
        self.coding = CodingService(self)
        self.runner = RunService(self)
        from qt_frontend.scheduler_service import SchedulerService
        self.scheduler = SchedulerService(self)

    @property
    def permission_mode(self):
        return {'sandbox': 'desktop-sandbox', 'manual': 'manual-approval', 'full': 'full-access'}[self.profile]

    def emit(self, event, **data):
        if self._emitter:
            self._emitter(event, **data)
            return
        with self.out_lock:
            print('KEYSTONE_UI:' + json.dumps({'event': event, **data}, ensure_ascii=False, default=str), flush=True)

    def status(self):
        self.emit('status', busy=self.running, unsaved=self.pages.unsaved, workspace=str(self.workspace), status='正在处理…' if self.running else '')

    def policy(self):
        from revon.config import load_config
        from revon.policy import Policy
        from revon.desktop_permissions import profile_policy
        return profile_policy(Policy.from_config(load_config(self.home), workspace=self.workspace, non_interactive=False), self.profile)

    def publish_tools(self):
        schemas = list(self.pages.connectors.schemas())
        computer = self.pages.computer
        lane = computer.lane if computer else None
        if lane:
            schemas.extend(lane.schemas())
        self.emit('service_tools', schemas=schemas, computerGrant=lane.session.grant_id if lane else '')

    def work(self, fn, *, cancel_event=None):
        with self.lock:
            if self.closed.is_set():
                raise RuntimeError('服务已关闭。')
            if self.running:
                raise RuntimeError('请等待当前操作结束。')
            self.running = True
            # Arm the worker's stop button at admission, before the thread starts.
            self.cancel = cancel_event if cancel_event is not None else threading.Event()
            self.cancel.clear()
        self.status()
        def run():
            try:
                fn()
            except Exception as exc:
                self.emit('error', message=str(redact(str(exc))))
            finally:
                with self.lock:
                    self.running = False
                    # A Coding request can arrive while another operation owns
                    # the worker, or just as the previous task finishes.
                    if not self.closed.is_set():
                        if self.pending_model is not None:
                            selected, self.pending_model = self.pending_model, None
                            self.command(selected)
                        elif self.coding.queue:
                            request = self.coding.queue.pop(0)
                            self.work(lambda: self.coding.run(request))
                self.coding.publish()
                self.status()
        self.worker = threading.Thread(target=run, name='revon-Service-Worker', daemon=True)
        self.worker.start()

    def approve(self, title, arguments, cancel=None, *, independent=False):
        identifier = uuid.uuid4().hex
        done = threading.Event()
        waiter = {'done': done, 'approved': False}
        self.waiters[identifier] = waiter
        self.emit('approval', id=identifier, title=str(title), detail=json.dumps(redact(arguments), ensure_ascii=False, indent=2, default=str))
        stops = [self.closed]
        if not independent:
            stops.append(self.cancel)
        if cancel is not None:
            stops.append(cancel)
        try:
            while not done.wait(.1):
                if any(event.is_set() for event in stops):
                    return False
            return waiter['approved'] and not any(event.is_set() for event in stops)
        finally:
            self.waiters.pop(identifier, None)
            self.emit('approval_closed', id=identifier)

    def navigate(self, row):
        if self.closed.is_set():
            return
        page, ticket = row['page'], row.get('ticket')
        try:
            if page == 'development':
                self.coding.initialize()
            if page not in {'chat', 'connectors', 'work'}:
                self.pages.publish(page)
            self.emit('navigate', page=page, ticket=ticket)
        except Exception as exc:
            self.emit('navigation_error', ticket=ticket, message=str(redact(str(exc))))

    def set_workspace(self, value):
        from revon.development.editor import ProjectBoundary
        target = ProjectBoundary(value)
        if self.pages.unsaved:
            raise ValueError('请先保存或放弃当前项目的编辑内容。')
        self.coding.close()
        with self.lock:
            self.terminal.close()
            self.workspace = target.root
            self.terminal = TerminalService(self)
            self.coding = CodingService(self)
            self.runner.cancel.set()
            self.runner = RunService(self)
            self.pages.results.pop('development', None)
            self.queries.reset()
            self.pages.explorer = Explorer(target)
            self.pages.search_results = None
            self.pages.revisions.clear()
            self.pages.boundary, self.pages.directory = target, ''
            self.pages.documents.clear()
            self.pages.active_document = ''
        prefs = SessionStore.read(desktop_config(self.home), {})
        SessionStore.atomic(desktop_config(self.home), {**prefs, 'project_workspace': str(target.root)})
        self.gateway.workspace = target.root
        self.gateway.start()
        if self.page == 'development':
            self.coding.initialize()
        self.status()

    def command(self, row):
        command = row.get('command')
        if command == 'navigate':
            self.page = row['page']
            # Loading never blocks stdin: cancellation/approval remains responsive.
            self.navigation.submit(self.navigate, dict(row))
        elif command == 'terminal':
            with self.lock:
                if self.page == 'development' and row.get('root') == str(self.workspace):
                    self.terminal.handle(row)
        elif command == 'workspace_query':
            with self.lock:
                self.queries.request(self.pages, row)
        elif command == 'page_action':
            page, action, values = row['page'], row['action'], row.get('values', {})
            if action == 'cancel':
                self.cancel.set()
            elif page == 'computer' and action == 'revoke' and self.pages.computer:
                self.pages.computer.close()
                self.pages.publish('computer')
                self.publish_tools()
            else:
                self.work(lambda: self.pages.action(page, action, values))
        elif command == 'document':
            self.pages.update_document(row['path'], row['text'], row['version'])
        elif command == 'call_tool':
            def invoke():
                self.active_call = row['id']
                try:
                    name, args = row['name'], row['arguments']
                    if name.startswith('computer_'):
                        computer = self.pages.computer
                        if not computer:
                            raise PermissionError('请先选择窗口并截图。')
                        result = computer.call(name, args, grant=row.get('computerGrant') or '')
                    else:
                        result = self.pages.connectors.invoke(name, args, self.policy(),
                            approve=self.approve, cancel_event=self.cancel, agent=True)
                    self.emit('call_result', id=row['id'], result=result)
                except Exception as exc:
                    self.emit('call_result', id=row['id'], result={'ok': False, 'error': str(redact(str(exc)))})
                finally:
                    self.active_call = ''
            try:
                self.work(invoke)
            except Exception as exc:
                self.emit('call_result', id=row['id'], result={'ok': False, 'error': str(redact(str(exc)))})
        elif command == 'cancel_call':
            if row.get('id') == self.active_call:
                self.cancel.set()
        elif command == 'computer_settings':
            if self.pages.computer:
                self.pages.computer.settings_changed(row.get('values'))
                self.publish_tools()
        elif command == 'coding':
            with self.lock:
                self.coding.command(row)
        elif command == 'run':
            if self.page == 'development':
                self.runner.command(row)
        elif command == 'permission':
            if not self.running and row.get('profile') in {'sandbox', 'manual', 'full'}:
                if self.profile != row['profile']:
                    self.terminal.close()
                    self.profile = row['profile']
                    if self.pages.computer:
                        self.pages.computer.settings_changed()
                        self.publish_tools()
            self.emit('permission', profile=self.profile)
        elif command == 'approval':
            waiter = self.waiters.get(row.get('id'))
            if waiter:
                waiter['approved'] = row.get('approved') is True
                waiter['done'].set()
        elif command == 'apply_model_setup':
            def setup():
                if row.get('refreshConfig'):
                    self.pages.action('config', 'reload', {})
                try:
                    self.gateway.start(row.get('provider', ''), model=row.get('model', ''))
                except Exception as exc:
                    self.emit('gateway_error', message=str(redact(str(exc))), active_url=self.gateway.url)
                    return
                if self.coding.runtime:
                    result = self.coding.runtime.refresh_models()
                    if result:
                        self.coding.catalog = result['models']
                self.coding.publish()
            with self.lock:
                if self.running:
                    pending = dict(row)
                    if self.pending_model and self.pending_model.get('refreshConfig'):
                        pending['refreshConfig'] = True
                    self.pending_model = pending
                else:
                    self.work(setup)
        elif command == 'connector_account':
            self.pages.account = row['account']
            self.emit('reset_form', page='connector_accounts')
            self.pages.publish('connector_accounts')
        elif command == 'provider_plan':
            self.emit('model_setup')
        elif command == 'close':
            self.close()

    def close(self):
        with self.lock:
            self.closed.set()
            self.cancel.set()
        for waiter in tuple(self.waiters.values()):
            waiter['done'].set()
        self.terminal.close()
        self.queries.close()
        self.navigation.shutdown(wait=False, cancel_futures=True)
        self.scheduler.close()
        self.coding.cancel.set()
        if self.coding.runtime:
            self.coding.runtime.cancel()
        if self.pages.computer:
            self.pages.computer.close()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=4)
        self.coding.close()
        self.gateway.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    host = ServiceHost(args.home, demo=args.demo)
    host.emit('ready', backend='headless')
    host.status()
    host.publish_tools()
    if not args.demo:
        host.work(host.gateway.start)
        host.scheduler.start()
    try:
        for line in sys.stdin:
            try:
                host.command(json.loads(line))
            except Exception as exc:
                host.emit('error', message=str(redact(str(exc))))
            if host.closed.is_set():
                break
    finally:
        if not host.closed.is_set():
            host.close()


if __name__ == '__main__':
    main()
