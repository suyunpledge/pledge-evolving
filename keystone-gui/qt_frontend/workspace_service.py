"""Headless workbench operations. QML owns every page and editor buffer view."""
import copy
import json
import os
from pathlib import Path
import subprocess
import time

from config_model import load_user_layer, save_user_layer
from keystone.development.editor import Document, ProjectBoundary
from keystone.desktop_services import DesktopStore, ConnectorService, OPERATIONS
from keystone.desktop_permissions import profile_policy
from keystone.policy import Policy, override_warnings
from keystone.secrets import MARKER, redact
from .store import SessionStore
from .workspace_navigation import Explorer
from keystone.compat import desktop_config


def field(key, label, value='', kind='text', options=None):
    return dict(key=key, label=label, value=value, kind=kind, options=options or [])


def button(key, label, *, danger=False):
    return dict(key=key, label=label, danger=danger)


def restore_config_secrets(edited, original):
    """Round-trip protected fields without ever projecting their values to QML."""
    projected = redact(original)
    if edited == projected:
        return copy.deepcopy(original)
    if isinstance(edited, dict):
        previous = original if isinstance(original, dict) else {}
        return {key: restore_config_secrets(value, previous.get(key)) for key, value in edited.items()}
    if isinstance(edited, list):
        previous = original if isinstance(original, list) else []
        return [restore_config_secrets(value, previous[i] if i < len(previous) else None) for i, value in enumerate(edited)]
    if isinstance(edited, str) and MARKER in edited:
        raise ValueError('已隐藏的凭据只能保留原值；请通过模型配置更换密钥。')
    return edited


def configured_steps(rows):
    """The step limit the user set in their own layer; 0 (no limit) when they never set one."""
    row = next((r for r in rows if r.get('id') == 'loop'), None)
    value = (row or {}).get('config', {}).get('maxSteps', 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_max_steps(text):
    text = str(text).strip()
    if text in {'', '不限', '无', 'none', 'unlimited'}:
        return 0
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 1000:
        raise ValueError('步数上限须为 0（不限）或 1–1000 的整数。')
    return value


def with_max_steps(rows, value):
    """Copy of the user layer with loop.maxSteps set; other loop settings are kept."""
    edited = copy.deepcopy(rows)
    row = next((r for r in edited if r.get('id') == 'loop'), None)
    if row is None:
        row = {'id': 'loop', 'name': 'loop:core', 'config': {}}
        edited.append(row)
    row.setdefault('config', {})['maxSteps'] = value
    return edited


class WorkspaceService:
    def __init__(self, host):
        self.host = host
        self.home = host.home
        self.boundary = ProjectBoundary(host.workspace)
        self.explorer = Explorer(self.boundary)
        self.documents = {}
        self.active_document = ''
        self.directory = ''
        self.search_results = None
        self.results, self.snapshots, self.revisions = {}, {}, {}
        self.task_todos = []
        self.store = DesktopStore(self.home / 'desktop-services.sqlite3')
        from http_transport import open_response
        import secret_store
        self.connectors = ConnectorService(self.store,
            lambda name: secret_store.get_provider('connector.' + name),
            save_credential=lambda name, value: secret_store.set_provider('connector.' + name, value),
            response_opener=open_response)
        self.account = 'github'
        self.computer = None

    @property
    def unsaved(self):
        return any(d.dirty for d in self.documents.values())

    def output(self, page, value, *, publish=True):
        if isinstance(value, dict) and isinstance(value.get('files'), list) and all(isinstance(f, dict) and 'diff' in f for f in value['files']):
            value = '\n\n'.join(f.get('name', '') + '\n' + (f.get('diff') or '无文本差异') for f in value['files'])
        self.results[page] = str(redact(value)) if isinstance(value, str) else json.dumps(redact(value), ensure_ascii=False, indent=2, default=str)
        if publish:
            self.publish(page)

    def publish(self, page):
        data = self.view(page)
        data['result'] = self.results.get(page, '')
        self.host.emit('page', page=page, data=data)
        runner = getattr(self.host, 'runner', None)
        if page == 'development' and runner is not None and runner._for != (self.active_document or ''):
            # Run/build tasks follow the active editor, like VS Code's ${file}.
            runner.publish()

    def directory_rows(self):
        if self.search_results is not None:
            return self.search_results
        target = self.boundary.path(self.directory or '.')
        entries = []
        for p in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if p.name in {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.keystone-build'}:
                continue
            try:
                self.boundary.path(p)
            except PermissionError:
                continue
            entries.append(dict(id=str(p.relative_to(self.host.workspace)), title=p.name,
                subtitle='文件夹' if p.is_dir() else f'{p.stat().st_size:,} B', directory=p.is_dir(), actions=[button('open', '打开')]))
            if len(entries) >= 1000:
                break
        return entries

    def view(self, page):
        data = dict(fields=[], actions=[], rows=[], hint='')
        rows = load_user_layer(self.home)
        if page in {'files', 'development'}:
            entries = self.explorer.rows() if page == 'development' else self.directory_rows()
            doc = self.documents.get(self.active_document)
            data.update(hint=str(self.host.workspace), directory=self.directory, rows=entries,
                actions=[button('chooseProject', '选择项目'), button('parent', '上一级'), button('git', 'Git 变更')],
                document=dict(path=self.active_document, relative=doc.path.relative_to(self.boundary.root).as_posix(),
                              text=doc.text, dirty=doc.dirty, status=doc.status,
                              original=doc.original, encoding=doc.encoding, newline=repr(doc.newline),
                              version=self.revisions.get(self.active_document, 0)) if doc else {},
                tabs=[dict(path=p, name=d.path.name, dirty=d.dirty) for p, d in self.documents.items()])
        elif page == 'task':
            from phase_client import model_catalog, planning_config, planning_selection
            level, planner = planning_selection(rows)
            if planning_config(rows).get('policy') == 'auto':
                level = 'auto'
            models = [{'label': '沿用执行模型', 'value': ''}, *[
                {'label': label, 'value': json.dumps(pair, ensure_ascii=False)} for pair, label in model_catalog(rows)]]
            selected = json.dumps(planner, ensure_ascii=False) if planner else ''
            if planner and not any(m['value'] == selected for m in models):
                models.append({'label': '已移除 · ' + ' / '.join(planner), 'value': selected})
            data.update(hint='在当前工作区运行任务，沿用当前权限；输出与执行状态保留在此页。',
                todos=self.task_todos,
                fields=[field('prompt', '任务内容', kind='multiline'), field('planning', '本次规划', level, 'choice', [
                            {'label': label, 'value': value} for value, label in [('auto', '自动判断'), ('none', '关闭'),
                                ('low', '简要'), ('medium', '标准'), ('high', '深入')]]),
                        field('planningModel', '规划模型', selected, 'choice', models)],
                actions=[button('run', '开始任务'), button('cancel', '停止')])
        elif page == 'config':
            from keystone.config import load_config
            self.snapshots.setdefault(page, copy.deepcopy(rows))
            policy = Policy.from_config(load_config(self.home), workspace=self.host.workspace)
            data['policyWarnings'] = override_warnings(policy)
            steps = configured_steps(rows)
            data.update(hint='模型按行管理；图片与视频模型使用右上方的生成模型配置。高级配置支持原有 Keystone JSON 格式。'
                             '步数上限默认不限；填 1–1000 可限制每个任务最多调用多少次工具，填 0 或留空恢复不限。',
                actions=[button('modelSetup', '添加或配置模型'), button('providerCatalog', '提供商目录'), button('reload', '重新读取')],
                rows=[dict(id=r['id'], title=r['config'].get('modelLabel') or r['config'].get('model') or r['id'],
                    subtitle=(r['config'].get('baseURL') or r.get('name', '')) + (' · 已停用' if r.get('disabled') else ''),
                    actions=[button('editModel', '编辑'), button('toggle', '启用' if r.get('disabled') else '停用')])
                    for r in rows if 'baseURL' in r['config']],
                fields=[field('json', '高级配置', json.dumps(redact(rows), ensure_ascii=False, indent=2), 'code')])
            data['fields'][0]['advanced'] = True
            data['fields'].append(field('max_steps', f"步数上限（当前：{'不限' if steps == 0 else f'{steps} 步'}）",
                                        str(steps)))
            data['fields'][1]['advanced'] = True
            data['actions'].append(button('save', '保存高级配置'))
            data['actions'].append(button('saveSteps', '保存步数上限'))
        elif page == 'agents':
            import sub_agent
            conf = sub_agent.load_config(self.home / 'agent-cluster.json')
            self.snapshots.setdefault(page, copy.deepcopy(conf))
            data.update(hint='Work 的并行方案、子 Agent、记忆和通信配置。开启后在 Work 对话前执行。',
                fields=[field('mode', '协作方式', SessionStore.read(desktop_config(self.home), {}).get('team_mode', 'off'), 'choice',
                              [{'label': '关闭', 'value': 'off'}, {'label': '按任务自动决定', 'value': 'auto'}, {'label': '开启', 'value': 'on'}]),
                    field('memory_mode', '记忆模式', conf['memory_mode'], 'choice', ['isolated', 'unified']),
                    field('cluster', '并行方案', conf['cluster']['enabled'], 'bool'),
                    field('count', '方案数量（1–4）', str(conf['cluster']['count'])),
                    field('communication', 'Agent 相互评议', conf['communication']['enabled'], 'bool'),
                    field('rounds', '评议轮数（1–3）', str(conf['communication']['rounds'])),
                    field('subagents', '子 Agent', conf['sub_agents']['enabled'], 'bool'),
                    field('presets', '角色预设（JSON）', json.dumps(conf['sub_agents']['presets'], ensure_ascii=False, indent=2), 'code'),
                    field('lanes', '独立方案模型（JSON）', json.dumps(conf['cluster']['lanes'], ensure_ascii=False, indent=2), 'code')],
                actions=[button('save', '保存配置'), button('templates', '填入内置角色模板')])
        elif page == 'knowledge':
            from keystone.knowledge import KnowledgeBase
            kb = KnowledgeBase(self.home / 'MEMORY.md')
            data.update(hint=f'共 {kb.stats()["total"]} 条知识，可在任务中检索。',
                fields=[field('text', '知识内容 / 检索词', kind='multiline'), field('kind', '类型', 'pitfall', 'choice', ['pitfall', 'lesson', 'note'])],
                actions=[button('record', '添加知识'), button('search', '检索')],
                rows=[dict(id=e.entry_id, title=e.text, subtitle=e.kind, actions=[button('archive', '归档')]) for e in kb.entries()])
        elif page == 'evolution':
            from keystone.evolution import EvolutionEngine
            engine = EvolutionEngine(self.home, workspace=self.host.workspace)
            data.update(hint='从使用反馈提炼改进候选，审查后应用；保留演化记录与回滚。',
                fields=[field('text', '反馈与观察', kind='multiline')], actions=[button('nominate', '提炼候选'), button('history', '历史记录')],
                rows=[dict(id=c.id, title=c.content, subtitle=f'{c.status} · {c.target}',
                    actions=([button('inspect', '详情'), button('evaluate', '评估'), button('apply', '批准并应用'), button('reject', '拒绝', danger=True)]
                    if c.status != 'applied' else [button('inspect', '详情'), button('rollback', '回滚', danger=True)])) for c in [*engine.pending(), *engine.applied()]])
        elif page == 'tools':
            from plugin_market import Marketplace
            from . import plugin_page
            data.update(plugin_page.view(self.home, Marketplace(self.home), self.host.workspace,
                                         getattr(self, 'plugin_query', '')))
        elif page == 'schedules':
            from phase_client import model_catalog
            models = [{'label': label, 'value': json.dumps(pair)} for pair, label in model_catalog(rows)]
            data.update(hint='定时任务在 Keystone 运行时执行，独立会话、只读权限；不会自动重放未完成的运行。',
                fields=[field('name', '任务名称'), field('prompt', '任务内容', kind='multiline'),
                    field('model', '执行模型', models[0]['value'] if models else '', 'choice', models),
                    field('delay', '多久后开始（分钟）', '60'), field('interval', '重复间隔（分钟，0 为单次）', '0')],
                actions=[button('add', '创建任务')],
                rows=[dict(id=j['id'], title=j['name'], subtitle=f"{j['status']} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(j['due']))}",
                    actions=[button('pause' if j['enabled'] else 'resume', '暂停' if j['enabled'] else '恢复'), button('runs', '执行记录')]) for j in self.store.jobs()])
        elif page == 'connector_accounts':
            import secret_store
            conf = self.store.account(self.account)
            data.update(hint=('已连接' if secret_store.get_provider('connector.' + self.account) and conf.get('authenticated') else '尚未连接') + ' · 服务授权与 Keystone 权限分别生效。',
                fields=[field('account', '账户', self.account, 'choice', ['github', 'microsoft']),
                    field('token', 'GitHub Token（仅填写时替换）', kind='secret'), field('client_id', 'Microsoft 应用 ID', conf.get('client_id', '')),
                    field('tenant', '租户', conf.get('tenant', 'common')), field('scopes', 'Microsoft scopes（空格分隔）', ' '.join(conf.get('scopes', []))),
                    *[field(k, label, conf.get(k) is True, 'bool') for k, label in [('read', '允许读取'), ('write', '允许写入'), ('agent', '允许 Agent 调用')]],
                    field('operation', '接口', '', 'choice', list(OPERATIONS)), field('arguments', '接口参数（JSON）', '{}', 'code')],
                actions=[button('select', '切换账户'), button('save', '保存授权'), button('login', 'Microsoft 登录'), button('disconnect', '断开', danger=True), button('invoke', '执行接口')])
        elif page == 'preview':
            data.update(hint='查看当前项目 Git 差异。对话中的“查看修改 / 回退”继续使用原有快照，回退前检查文件版本。', actions=[button('git', '刷新差异')])
        elif page == 'computer':
            if self.computer is None:
                from .computer_service import ComputerService
                self.computer = ComputerService(self.host)
            data.update(self.computer.view())
        return data

    def action(self, page, action, values):
        key = str(values.get('id', ''))
        if action == 'refresh':
            if page == 'development':
                self.explorer.children.clear()
            self.publish(page)
            return
        if page in {'development', 'files'}:
            self.file_action(page, action, values)
            if action == 'save':
                self.host.emit('saved', page=page)
            self.host.status()
            return
        elif action == 'git' and page == 'preview':
            self.git(page)
        elif page == 'task':
            from keystone.vscode_bridge import EditorBridge
            from keystone.development.models import ModelSettingsSource
            if self.host.demo:
                raise ValueError('预览模式不发送模型请求。')
            if not values.get('prompt', '').strip():
                raise ValueError('请填写任务内容。')
            from keystone import todos
            self.task_todos = []
            def event_received(event):
                items = todos.event_items(event.get('data') or {}) if event.get('event') == 'progress' else None
                if items is not None:
                    self.task_todos = items
                self.output(page, json.dumps(event, ensure_ascii=False))
            bridge = EditorBridge(event_received, model_router_factory=ModelSettingsSource(self.home))
            try:
                bridge.initialize({'workspace': str(self.host.workspace), 'home': str(self.home.parent / (self.home.name + '-qt-tasks'))})
                from functools import partial
                from phase_client import apply_planning_key
                bridge.agent.prepare_planning_route = partial(apply_planning_key, self.home)
                bridge.agent.cancellation_event = self.host.cancel
                bridge.agent.action_executor.approver = self.host.approve
                bridge.agent.action_executor.trusted_native = self.host.profile != 'sandbox'
                level = values.get('planning', 'none')
                if level == 'auto':
                    from keystone.planning import task_planning_level
                    level = task_planning_level('auto', 'none', values['prompt'])
                self.output(page, bridge.run({'prompt': values['prompt'], 'planning': level,
                    'planningModel': values.get('planningModel', '') if level != 'none' else '', 'mode': self.host.permission_mode}))
            finally:
                bridge.close()
        elif page == 'config':
            rows = load_user_layer(self.home)
            if action == 'reload':
                self.snapshots[page] = copy.deepcopy(rows)
                self.host.emit('reset_form', page=page)
            elif action == 'toggle':
                edited = copy.deepcopy(rows)
                target = next(r for r in edited if r['id'] == key)
                target['disabled'] = not target.get('disabled', False)
                save_user_layer(self.home, edited, expected_rows=rows)
                self.snapshots[page] = copy.deepcopy(edited)
            elif action == 'save':
                edited = json.loads(values['json'])
                if not isinstance(edited, list) or any(not isinstance(r, dict) or not isinstance(r.get('config', {}), dict) for r in edited):
                    raise ValueError('高级配置必须是包含 id 和 config 的配置数组。')
                for r in edited:
                    if isinstance(r.get('config', {}).get('apiKey'), str) and r['config']['apiKey'] not in {'', MARKER}:
                        raise ValueError('请通过模型配置保存密钥；高级配置只接受密钥引用。')
                previous = {r['id']: r for r in self.snapshots[page]}
                edited = [restore_config_secrets(r, previous.get(r.get('id'))) for r in edited]
                save_user_layer(self.home, edited, expected_rows=self.snapshots[page])
                self.snapshots[page] = copy.deepcopy(edited)
            elif action == 'saveSteps':
                edited = with_max_steps(rows, parse_max_steps(values.get('max_steps', '')))
                save_user_layer(self.home, edited, expected_rows=rows)
                self.snapshots[page] = copy.deepcopy(edited)
                self.host.emit('reset_form', page=page)
            elif action in {'modelSetup', 'providerCatalog'}:
                self.host.emit('model_setup' if action == 'modelSetup' else 'provider_catalog', query='')
            if action in {'save', 'toggle'}:
                self.host.gateway.start()
        elif page == 'agents':
            import sub_agent
            path = self.home / 'agent-cluster.json'
            old = sub_agent.load_config(path)
            if action == 'templates':
                self.host.emit('form_values', page=page, values={'presets': json.dumps(old['templates'], ensure_ascii=False, indent=2)})
            else:
                if old != self.snapshots[page]:
                    raise ValueError('Agent 配置在外部发生变化，请刷新后再保存。')
                conf = copy.deepcopy(old)
                conf.update(memory_mode=values['memory_mode'], communication=dict(enabled=bool(values['communication']), rounds=max(1, min(3, int(values['rounds'])))))
                presets, lanes = json.loads(values['presets']), json.loads(values['lanes'])
                if not isinstance(presets, list) or not isinstance(lanes, list) or any(not isinstance(x, dict) for x in presets + lanes):
                    raise ValueError('角色与模型配置必须为对象数组。')
                conf['cluster'] = dict(enabled=bool(values['cluster']), count=max(1, min(4, int(values['count']))), lanes=lanes)
                conf['sub_agents'] = dict(enabled=bool(values['subagents']), presets=presets)
                sub_agent.save_config(conf, path)
                prefs = SessionStore.read(desktop_config(self.home), {})
                if values.get('mode') not in {'off', 'auto', 'on'}:
                    raise ValueError('未知协作方式。')
                SessionStore.atomic(desktop_config(self.home), {**prefs, 'team_mode': values['mode']})
                self.snapshots[page] = copy.deepcopy(conf)
        elif page == 'knowledge':
            from keystone.knowledge import KnowledgeBase
            kb = KnowledgeBase(self.home / 'MEMORY.md')
            if action == 'record':
                if not values.get('text', '').strip():
                    raise ValueError('请填写知识内容。')
                kb.record(values['text'], source='user', kind=values.get('kind', 'pitfall'))
            elif action == 'search':
                self.results[page] = '\n\n'.join(e.text for e, _ in kb.search(values.get('text', '')))
            elif action == 'archive':
                entry = next(e for e in kb.store.entries if e.entry_id == key)
                entry.archived = True
                kb.store.save()
        elif page == 'evolution':
            from keystone.evolution import EvolutionEngine
            engine = EvolutionEngine(self.home, workspace=self.host.workspace)
            if action == 'nominate':
                result = [c.to_raw() for c in engine.nominate(engine.observe_text(values.get('text', '')))]
            elif action == 'history':
                result = engine.history()
            else:
                candidate = engine._require(key)
                if action == 'inspect':
                    result = candidate.to_raw()
                elif action == 'evaluate':
                    result = engine.evaluate(candidate)
                elif action == 'apply':
                    if not self.host.approve('应用演化候选', candidate.to_raw()):
                        return
                    engine.evaluate(candidate)
                    if engine.requires_approval(candidate):
                        engine.approve(key)
                    result = engine.apply(key, by='human').to_raw()
                else:
                    if not self.host.approve('回滚演化' if action == 'rollback' else '拒绝演化', candidate.to_raw()):
                        return
                    result = getattr(engine, action)(key).to_raw()
            self.results[page] = json.dumps(result, ensure_ascii=False, indent=2)
        elif page == 'tools':
            from plugin_market import Marketplace
            from . import plugin_page
            if action == 'filter':
                self.plugin_query = str(values.get('query', ''))
            result = plugin_page.act(self.home, Marketplace(self.home), self.host.workspace, action, values,
                                     self.host.approve)
            if result is not None:
                self.results[page] = plugin_page.render(result)
        elif page == 'schedules':
            if action == 'add':
                delay, interval = float(values['delay']), float(values['interval'])
                if not 0 <= delay <= 525600 or not 0 <= interval <= 525600:
                    raise ValueError('时间须在 0 到 525600 分钟之间。')
                self.store.add_job(values['name'], values['prompt'], str(self.host.workspace), json.loads(values['model']), time.time() + delay * 60, interval * 60)
            elif action in {'pause', 'resume'}:
                self.store.enable_job(key, action == 'resume')
                if action == 'pause':
                    self.host.scheduler.cancel_job(key)
            elif action == 'runs':
                self.results[page] = json.dumps(self.store.job_runs(key), ensure_ascii=False, indent=2)
        elif page == 'connector_accounts':
            self.account_action(action, values)
        elif page == 'computer':
            self.computer.action(action, values)
        if action == 'save':
            self.host.emit('saved', page=page)
        if page in {'connector_accounts', 'computer'}:
            self.host.publish_tools()
        self.publish(page)
        self.host.status()

    def file_action(self, page, action, values):
        path = values.get('path') or values.get('id') or self.active_document
        if action in {'toggleFolder', 'collapseFolders'} and page == 'development':
            if action == 'collapseFolders':
                self.explorer.expanded.clear()
            else:
                self.explorer.toggle(path)
            self.publish(page)
            return
        if action == 'open':
            target = self.boundary.path(path)
            if target.is_dir():
                self.search_results = None
                self.directory = str(target.relative_to(self.host.workspace))
            else:
                path = str(target)
                if path not in self.documents:
                    self.documents[path] = Document.open(self.boundary, target)
                self.active_document = path
                self.explorer.reveal(target)
        elif action == 'parent':
            self.search_results = None
            target = self.boundary.path(self.directory or '.')
            self.directory = str(target.parent.relative_to(self.host.workspace)) if target != self.host.workspace else ''
        elif action == 'project':
            self.host.set_workspace(values['path'])
        elif action == 'git':
            self.git(page)
        elif action == 'search':
            query = values.get('query', '').strip().casefold()
            self.search_results = None
            if query:
                result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=self.host.workspace,
                    capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                names = result.stdout.decode('utf-8', 'replace').split('\0') if result.returncode == 0 else []
                if not names:
                    for base, dirs, files in os.walk(self.host.workspace, followlinks=False):
                        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', '__pycache__', 'venv'} and not (Path(base) / d).is_symlink()]
                        names.extend(str((Path(base) / name).relative_to(self.host.workspace)) for name in files)
                        if len(names) > 20000:
                            break
                found = []
                for name in names:
                    if not name or query not in name.casefold():
                        continue
                    try:
                        target = self.boundary.path(name)
                    except PermissionError:
                        continue
                    if target.is_file():
                        found.append(dict(id=name, title=name, directory=False, subtitle='文件', actions=[button('open', '打开')]))
                    if len(found) >= 300:
                        break
                self.search_results = found
        elif action == 'saveAs':
            from keystone.development.editor import save_document_as
            doc = self.documents[path]
            target = self.boundary.path(values['target'])
            if target.exists() and not self.host.approve('另存为并覆盖文件', {'path': str(target)}):
                return
            save_document_as(self.boundary, doc, target)
            del self.documents[path]
            self.documents[str(target)] = doc
            self.active_document = str(target)
            self.explorer.children.clear()
            self.explorer.reveal(target)
            self.revisions[str(target)] = self.revisions.pop(path, 0)
        elif action in {'save', 'discard', 'close', 'merge', 'compare'}:
            doc = self.documents[path]
            if action == 'save':
                doc.save()
            elif action == 'merge':
                doc.reconcile(doc.inspect_external())
                doc.merge_external()
            elif action == 'compare':
                doc.reconcile(doc.inspect_external())
                self.results[page] = doc.external_diff(buffer=True)
            elif action == 'discard':
                if doc.dirty and not self.host.approve('放弃未保存修改', {'path': path}):
                    return
                self.documents[path] = Document.open(self.boundary, path)
            elif action == 'close':
                if doc.dirty and not self.host.approve('关闭并放弃未保存修改', {'path': path}):
                    return
                if path == self.active_document:
                    tabs = list(self.documents)
                    index = tabs.index(path)
                    remaining = tabs[:index] + tabs[index + 1:]
                    self.active_document = remaining[min(index, len(remaining) - 1)] if remaining else ''
                del self.documents[path]
        for target in ('development', 'files'):
            self.publish(target)

    def update_document(self, path, text, version):
        path = str(self.boundary.path(path))
        if path in self.documents and version >= self.revisions.get(path, 0):
            self.documents[path].update(text)
            self.revisions[path] = version
            self.host.status()

    def refresh_documents(self, paths=None):
        if paths is None:
            self.explorer.children.clear()
            documents = list(self.documents.values())
        else:
            documents = []
            for value in dict.fromkeys(paths):
                try:
                    target = self.boundary.path(value)
                except (OSError, ValueError):
                    continue
                relative = target.relative_to(self.boundary.root)
                for parent in (relative, *relative.parents):
                    self.explorer.children.pop(parent.as_posix(), None)
                if str(target) in self.documents:
                    documents.append(self.documents[str(target)])
        for doc in documents:
            doc.reconcile(doc.inspect_external())
        for page in ('development', 'files'):
            self.publish(page)

    def git(self, page):
        outputs = []
        safe_diff = ['diff', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames']
        for args in (['status', '--short'], [*safe_diff, '--stat'], [*safe_diff, '--', '.'], [*safe_diff, '--cached', '--', '.']):
            result = subprocess.run(['git', '--no-pager', *args], cwd=self.host.workspace, capture_output=True,
                encoding='utf-8', errors='replace', timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            outputs.append(result.stdout or result.stderr)
        self.results[page] = str(redact('\n'.join(outputs)))[:200000]

    def show_history(self, directory):
        entries = sorted(directory.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        self.host.emit('history', rows=[dict(path=str(p), label=p.stem, stamp=time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))) for p in entries])

    def account_action(self, action, values):
        import secret_store
        name = values.get('account', self.account)
        if name not in {'github', 'microsoft'}:
            raise ValueError('未知账户类型。')
        if action == 'select':
            self.account = name
            self.host.emit('reset_form', page='connector_accounts')
            return
        previous = self.store.account(name)
        conf = {k: values.get(k) is True for k in ('read', 'write', 'agent')}
        conf.update(client_id=values.get('client_id', '').strip(), tenant=values.get('tenant', 'common').strip(), scopes=values.get('scopes', '').split())
        if name == 'microsoft' and 'paired_services' in previous:
            conf['paired_services'] = previous['paired_services']
        if action == 'save':
            token = values.get('token', '')
            changed = name == 'microsoft' and any(previous.get(k) != conf.get(k) for k in ('tenant', 'client_id', 'scopes'))
            conf['authenticated'] = True if name == 'github' and token else False if changed else previous.get('authenticated', False)
            self.store.set_account(name, dict(conf, updating=True))
            expected = self.store.account(name)
            def save():
                if name == 'github' and token:
                    secret_store.set_provider('connector.' + name, token)
                elif changed:
                    secret_store.delete_provider('connector.' + name)
            self.store.finish_account(name, expected, conf, save)
            self.host.emit('form_values', page='connector_accounts', values={'token': ''})
        elif action == 'login':
            if name != 'microsoft':
                raise ValueError('GitHub 请填写 Token 后保存。')
            def notice(code, url):
                self.output('connector_accounts', f'请在官方授权页输入代码：{code}')
                self.host.emit('browser', url=url)
            self.connectors.device_login(conf, notice, self.host.cancel, expected=previous)
        elif action == 'disconnect':
            self.store.set_account(name, {'updating': True})
            self.store.finish_account(name, self.store.account(name), {}, lambda: secret_store.delete_provider('connector.' + name))
        elif action == 'invoke':
            policy = self.host.policy()
            result = self.connectors.invoke(values['operation'], json.loads(values['arguments']), policy,
                approve=self.host.approve, cancel_event=self.host.cancel, agent=False)
            self.results['connector_accounts'] = json.dumps(redact(result), ensure_ascii=False, indent=2, default=str)
