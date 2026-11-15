"""Private JSON service process. No UI toolkit, foreign window or message pump."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import uuid

GUI = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(GUI), str(GUI.parent)]

from forge.secrets import redact
from qt_frontend.store import SessionStore
from qt_frontend.gateway_service import GatewayService
from qt_frontend.workspace_service import WorkspaceService
from qt_frontend.coding_service import CodingService


class ServiceHost:
    def __init__(self, home, *, demo=False, emit=None):
        self.home, self.demo = Path(home).resolve(), demo
        self.home.mkdir(parents=True, exist_ok=True)
        import secret_store
        secret_store.SECRETS_FILE = self.home / 'secrets.json'
        secret_store.load()
        prefs = SessionStore.read(self.home / 'forge-desktop.json', {})
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
        self.navigation = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Forge-page-loader")
        self._emitter = emit
        self.gateway = GatewayService(self.home, self.workspace, self.emit, demo=demo)
        self.pages = WorkspaceService(self)
        self.coding = CodingService(self)
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
            print('FORGE_UI:' + json.dumps({'event': event, **data}, ensure_ascii=False, default=str), flush=True)

    def status(self):
        self.emit('status', busy=self.running, unsaved=self.pages.unsaved, workspace=str(self.workspace), status='正在处理…' if self.running else '')

    def policy(self):
        from forge.config import load_config
        from forge.policy import Policy
        from forge.desktop_permissions import profile_policy
        return profile_policy(Policy.from_config(load_config(self.home), workspace=self.workspace, non_interactive=False), self.profile)

    def publish_tools(self):
        schemas = list(self.pages.connectors.schemas())
        computer = self.pages.computer
        if computer and computer.lane:
            schemas.extend(computer.lane.schemas())
        self.emit('service_tools', schemas=schemas)

    def work(self, fn):
        with self.lock:
            if self.running:
                raise RuntimeError('请等待当前操作结束。')
            self.running = True
            self.cancel = threading.Event()
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
                            self.command({'command': 'apply_model_setup', 'provider': selected})
                        elif self.coding.queue:
                            request = self.coding.queue.pop(0)
                            self.work(lambda: self.coding.run(request))
                self.coding.publish()
                self.status()
        self.worker = threading.Thread(target=run, name='Forge-Service-Worker', daemon=True)
        self.worker.start()

    def approve(self, title, arguments, cancel=None):
        identifier = uuid.uuid4().hex
        done = threading.Event()
        waiter = {'done': done, 'approved': False}
        self.waiters[identifier] = waiter
        self.emit('approval', id=identifier, title=str(title), detail=json.dumps(redact(arguments), ensure_ascii=False, indent=2, default=str))
        try:
            while not done.wait(.1):
                if self.closed.is_set() or self.cancel.is_set() or cancel is not None and cancel.is_set():
                    return False
            return waiter['approved'] and not self.closed.is_set() and not self.cancel.is_set()
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
        from forge.development.editor import ProjectBoundary
        target = ProjectBoundary(value)
        if self.pages.unsaved:
            raise ValueError('请先保存或放弃当前项目的编辑内容。')
        self.coding.close()
        self.coding = CodingService(self)
        self.workspace = target.root
        self.pages.boundary, self.pages.directory = target, ''
        self.pages.documents.clear()
        self.pages.active_document = ''
        prefs = SessionStore.read(self.home / 'forge-desktop.json', {})
        SessionStore.atomic(self.home / 'forge-desktop.json', {**prefs, 'project_workspace': str(target.root)})
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
        elif command == 'page_action':
            page, action, values = row['page'], row['action'], row.get('values', {})
            if action == 'cancel':
                self.cancel.set()
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
                        if not computer or not computer.lane:
                            raise PermissionError('请先选择窗口并截图。')
                        # A fresh cancellation token for this explicit turn; the target
                        # revocation token still remains authoritative inside the lane.
                        from forge.computer_use import TargetCancellation
                        computer.lane.cancel = TargetCancellation(self.cancel, computer.session.revoked)
                        computer.lane.ctx = replace(computer.lane.ctx, cancellation=computer.lane.cancel)
                        result = computer.lane.call(name, args)
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
                self.pages.computer.settings_changed()
                self.publish_tools()
        elif command == 'coding':
            with self.lock:
                self.coding.command(row)
        elif command == 'permission':
            if not self.running and row.get('profile') in {'sandbox', 'manual', 'full'}:
                self.profile = row['profile']
            self.emit('permission', profile=self.profile)
        elif command == 'approval':
            waiter = self.waiters.get(row.get('id'))
            if waiter:
                waiter['approved'] = row.get('approved') is True
                waiter['done'].set()
        elif command == 'apply_model_setup':
            if self.running:
                self.pending_model = row.get('provider', '')
                return
            def setup():
                self.gateway.start(row.get('provider', ''))
                if self.coding.runtime:
                    result = self.coding.runtime.refresh_models()
                    if result:
                        self.coding.catalog = result['models']
                self.coding.publish()
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
        self.closed.set()
        self.cancel.set()
        for waiter in tuple(self.waiters.values()):
            waiter['done'].set()
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
