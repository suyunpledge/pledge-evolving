"""UI-independent Coding session, used by the Qt service process."""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

from forge.development.models import ModelSettingsSource
from forge.development.runtime import DevelopmentRuntime
from forge.development.state import ModeState
from forge.secrets import redact


class CodingService:
    def __init__(self, host):
        self.host = host
        self.state = ModeState(host.home / 'gui/qt/coding')
        self.state.switch('development')
        self.runtime = None
        self.catalog = []
        self.draft, self.version = '', 0
        self.role, self.model, self.simple = 'default', '继承配置', True
        self.contexts, self.events, self.queue = [], [], []
        self.last_prompt, self.status = '', ''
        self.cancel = threading.Event()

    def initialize(self):
        if self.runtime and self.runtime.ready:
            return
        root = self.host.home.parent / ('.forge-development' if self.host.home.name == '.forge' else self.host.home.name + '-development')
        self.runtime = DevelopmentRuntime(self.state, emit=self.event, runtime_home=root,
                                         model_router_factory=ModelSettingsSource(self.host.home))
        result = self.runtime.initialize(self.host.workspace)
        self.catalog = result['models']
        self.runtime.bridge.agent.action_executor.approver = self.host.approve
        self.publish()

    def event(self, event):
        data = event.get('data', event)
        self.events.append(json.dumps(redact(data), ensure_ascii=False))
        self.events = self.events[-100:]
        # Progress can arrive per token; coalesce at the service boundary.
        now = time.monotonic()
        if now - getattr(self, '_last_event', 0) > .12:
            self._last_event = now
            self.publish()

    @staticmethod
    def identifiers(record):
        return list(dict.fromkeys(i for a in record.actions.values()
            for i in (a.get('meta') or {}).get('change_ids', []) if isinstance(i, str)))

    def publish(self):
        rt = self.runtime
        changes = []
        for key, task in list(rt.tasks.items())[-50:] if rt else []:
            record = task['evidence']
            ids = self.identifiers(record)
            if record.changes or record.diffs:
                restored = bool(ids) and set(ids).issubset(record.reverted_change_ids)
                changes.append(dict(id=key, count=len(record.changes), restored=restored,
                    canRevert=bool(ids) and not restored and not any((a.get('meta') or {}).get('change_review_incomplete') for a in record.actions.values()),
                    files=[Path(c['path']).name for c in list(record.changes.values())[:4]]))
        labels = {rt.catalog[r['id']]: r['label'] for r in self.catalog} if rt else {}
        self.host.emit('coding_state', state=dict(ready=True, status=self.status,
            project=self.host.workspace.name, locked='', busy=self.host.running, loading=False,
            queue=len(self.queue), planning=self.state.value('planning'), model=self.model,
            role=self.role, simple=self.simple, models=['继承配置', *labels.values()],
            roleModels={r: labels.get(tuple(p or ()), '继承配置') for r, p in self.state.value('roles').items()},
            contexts=[Path(p).name for p in self.contexts], hasDocument=bool(self.host.pages.active_document),
            tasks='\n'.join(self.events[-10:])[-12000:], events='\n'.join(self.events)[-12000:],
            agents=[{'name': a.get('agent', a.get('name', 'Agent')), 'status': a.get('status', ''), 'output': str(a.get('output', ''))[:4000]}
                    for a in list(rt.evidence.agents.values())[-32:]] if rt else [], changes=changes))

    def message(self, role, body):
        self.host.emit('coding_message', message=dict(role=role, body=str(redact(body)), stamp=time.strftime('%H:%M')))

    def command(self, row):
        action, value = row.get('action'), row.get('value', '')
        if action == 'draft':
            version = row.get('version', 0)
            if isinstance(version, int) and version >= self.version:
                self.draft, self.version = value, version
            return
        if action == 'cancel':
            self.cancel.set()
            self.host.cancel.set()
            self.queue.clear()
            if self.runtime:
                self.runtime.cancel()
            return
        if action in {'send', 'delegate', 'retry'}:
            prompt = self.last_prompt if action == 'retry' else self.draft.strip()
            if not prompt:
                return
            if len(self.queue) >= 3:
                raise ValueError('最多排队三个任务。')
            request = (prompt, action == 'delegate', self.model, self.role, self.state.value('planning'), tuple(self.contexts))
            if self.host.running:
                self.queue.append(request)
            else:
                self.host.work(lambda: self.run(request))
            self.draft = ''
            self.host.emit('coding_draft', text='', version=self.version)
        elif action == 'planning':
            self.state.configure_planning(value)
        elif action == 'role' and value in self.state.ROLES:
            self.role = value
        elif action == 'model':
            self.pair(value)
            self.model = value
        elif action == 'roleModel':
            role, _, label = value.partition('\n')
            self.state.configure_role(role, self.pair(label))
        elif action == 'density':
            self.simple = not self.simple
        elif action == 'template':
            prompts = ['检查当前项目，说明结构和需要优先修复的问题。', '实现以下功能，先阅读相关代码，再做小范围修改并验证：\n', '审查当前修改，列出真实缺陷及验证结果。']
            self.draft = prompts[int(value)]
            self.host.emit('coding_draft', text=self.draft, version=self.version)
        elif action == 'attach':
            path = self.host.pages.active_document
            if path and path not in self.contexts:
                if len(self.contexts) >= 4:
                    raise ValueError('每次最多附加四个文档。')
                self.contexts.append(path)
        elif action == 'removeContext':
            self.contexts.pop(int(value))
        elif action == 'setup':
            self.host.emit('model_setup')
        elif action == 'approvals':
            self.host.emit('permissions')
        elif action == 'openFile':
            self.host.pages.action('development', 'open', {'path': value})
        elif action in {'new', 'history', 'restore', 'review', 'tests', 'diff', 'revert'}:
            self.host.work(lambda: self.operation(action, value))
        self.publish()

    def pair(self, label):
        if label == '继承配置':
            return None
        row = next((r for r in self.catalog if r['label'] == label), None)
        if row is None:
            raise ValueError('所选模型已移除，请重新选择。')
        return self.runtime.catalog[row['id']]

    def run(self, request):
        if self.host.demo:
            raise ValueError('预览模式不发送模型请求。')
        self.initialize()
        self.cancel = self.host.cancel
        while request and not self.cancel.is_set():
            prompt, delegate, label, role, planning, contexts = request
            if self.host.pages.unsaved:
                raise ValueError('请先保存或放弃编辑器修改，再运行 Agent。')
            self.last_prompt = prompt
            self.message('user', prompt)
            self.runtime.bridge.agent.action_executor.trusted_native = self.host.profile != 'sandbox'
            self.status = '正在处理…'
            self.publish()
            result = self.runtime.run(prompt, explicit_model=self.pair(label), role=role,
                permission=self.host.permission_mode, planning=planning,
                contexts=[{'path': p, 'content': self.host.pages.documents[p].text} for p in contexts if p in self.host.pages.documents],
                delegate=delegate, cancellation=self.cancel)
            self.message('assistant', result.get('text') or result.get('output') or json.dumps(result, ensure_ascii=False, indent=2))
            self.status = '已停止' if self.cancel.is_set() else '任务结束'
            self.host.pages.refresh_documents()
            with self.host.lock:
                request = self.queue.pop(0) if self.queue and not self.cancel.is_set() else None
        self.publish()

    def operation(self, action, value):
        self.initialize()
        rt = self.runtime
        if self.host.pages.unsaved and action in {'revert', 'restore', 'new'}:
            raise ValueError('请先保存或放弃编辑器修改。')
        if action == 'new':
            from forge.session import Session
            from forge.development.evidence import TaskEvidence
            old = rt.bridge.agent.session
            fresh = Session(old.path.parent / (uuid.uuid4().hex + '.jsonl'), meta={'cwd': str(self.host.workspace)})
            with fresh:
                fresh.sync()
            old.close()
            rt.bridge.agent.session, rt.bridge.history, rt.bridge.last_result = fresh, [], None
            rt.evidence, rt.last_task_id, rt.last_snapshot, rt._resume_evidence = TaskEvidence(), '', None, None
            rt.tasks.clear()
            self.state.remember_session(fresh.path)
            self.host.emit('coding_message', message=None)
            self.events.clear()
        elif action == 'history':
            self.host.pages.show_history(rt.home / 'sessions')
        elif action == 'restore':
            result = rt.restore_history(value)
            self.host.emit('coding_message', message=None)
            self.message('assistant', json.dumps(result, ensure_ascii=False, indent=2))
        elif action == 'review':
            self.message('assistant', json.dumps(rt.review(), ensure_ascii=False, indent=2))
        elif action == 'tests':
            if self.host.profile == 'sandbox':
                raise PermissionError('执行项目代码需要手动批准或完全访问权限。')
            directory = self.host.pages.boundary.path(value or 'tests')
            argv = [sys.executable, '-m', 'unittest', 'discover', '-s', str(directory), '-v']
            if self.host.approve('执行项目测试', {'command': argv, 'workspace': str(self.host.workspace)}):
                self.host.pages.output('development', rt.execute_tests(argv, trusted=True))
        elif action in {'diff', 'revert'}:
            record = rt.tasks[value]['evidence']
            ids = self.identifiers(record)
            if not ids:
                self.host.pages.output('development', list(record.changes.values()) or record.diffs)
                return
            result = rt.review_recorded_changes(ids)
            self.host.pages.output('development', result)
            if action == 'revert' and result['canRevert'] and self.host.approve('回退本次代码修改', result):
                result = rt.review_recorded_changes(ids, revert=True, permission=self.host.permission_mode)
                record.reverted_change_ids.update(ids)
                self.host.pages.output('development', result)
                self.host.pages.refresh_documents()
        self.publish()

    def close(self):
        self.cancel.set()
        if self.runtime:
            self.runtime.cancel()
            if not self.runtime.busy:
                self.runtime.close()
