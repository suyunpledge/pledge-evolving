"""Project the existing Coding workbench into the Qt Agent panel."""
from pathlib import Path

from development_mode.agent_panel import TASK_TEMPLATES
from development_mode.policy_widgets import PlanningChoice


class CodingHost:
    def __init__(self, bench, emit, *, demo=False):
        self.bench = bench
        self.emit = emit
        self.demo = demo
        self.version = 0
        self.previous = {}
        self.changes = {}
        bench.chat.transcript_changed = self._transcript
        bench.input.focus_set = lambda: emit('coding_focus')

    def _transcript(self, message):
        if message is None:
            self.changes.clear()
        self.emit('coding_message', message=message)

    def publish(self):
        bench = self.bench
        evidence = bench.task_evidence
        if evidence.task_id and (evidence.changes or evidence.diffs):
            self.changes[evidence.task_id] = evidence
            if len(self.changes) > 50:
                self.changes.pop(next(iter(self.changes)))
        changes = []
        for key, record in self.changes.items():
            ids = bench._recorded_change_ids(record)
            restored = bool(ids) and set(ids).issubset(record.reverted_change_ids)
            incomplete = any((a.get('meta') or {}).get('change_review_incomplete') for a in record.actions.values())
            changes.append({'id': key, 'count': len(record.changes), 'restored': restored,
                            'backups': record.deletion_backups,
                            'canRevert': bool(ids) and not incomplete and not restored,
                            'files': [Path(c['path']).name for c in list(record.changes.values())[:4]]})
        routing = bench._runtime_ctx.get_routing() if bench._runtime_ctx is not None else {}
        labels = {pair: row['label'] for row in bench.catalog_rows
                  if (pair := bench.runtime.catalog.get(row['id']))}
        role_models = {role: labels.get(tuple(pair or ()), '继承配置')
                       for role, pair in bench.state.value('roles').items()}
        state = dict(
            ready=True, status=bench.status.get(),
            project=bench.source.root.name if bench.source else '', locked=bench.chat._locked_reason,
            busy=bool(bench.runtime.busy or bench._submission_active), loading=bench._loading,
            queue=len(bench._pending_prompts),
            planning=bench.planning.get(), model=routing.get('primary') or '继承配置',
            role=bench.task_role.get(), simple=bench._density == 'simple',
            models=['继承配置', *dict.fromkeys(r['label'] for r in bench.catalog_rows)],
            roleModels=role_models,
            contexts=[Path(p).name for p in bench.editor_contexts],
            hasDocument=bench.current_row() is not None,
            tasks=bench.workflow.get('1.0', 'end-1c')[-12000:],
            agents=[{'name': row.get('agent', 'Agent'), 'status': row.get('status', ''),
                     'output': str(row.get('output', ''))[:4000]}
                    for row in list(bench.agent_panel.rows.values())[-32:]],
            events=bench.events.get('end - 12001 chars', 'end-1c'), changes=changes)
        patch = {key: value for key, value in state.items() if self.previous.get(key) != value}
        if patch:
            self.previous.update(patch)
            self.emit('coding_state', state=patch)
        draft = bench.input.get('1.0', 'end-1c')
        if (draft, self.version) != getattr(self, 'last_draft', None):
            self.last_draft = draft, self.version
            self.emit('coding_draft', text=draft, version=self.version)

    def command(self, row):
        bench = self.bench
        action, value = row.get('action'), row.get('value', '')
        if action == 'draft':
            version = row.get('version')
            if not isinstance(version, int) or version < self.version or not isinstance(value, str):
                return
            self.version = version
            if value != bench.input.get('1.0', 'end-1c'):
                bench.input.delete('1.0', 'end')
                bench.input.insert('1.0', value)
            self.last_draft = value, version
        elif action in {'send', 'delegate'}:
            if self.demo:
                bench.status.set('预览模式不发送任务。')
            else:
                bench.send(delegate=action == 'delegate')
        elif action == 'planning' and value in PlanningChoice.LABELS:
            bench._on_planning_change(value)
        elif action == 'role' and value in {'default', 'complex', 'simple', 'review'}:
            bench.task_role.set(value)
            bench.agent_role.set('agent' if value == 'default' else value)
        elif action == 'model' and value in self.previous.get('models', []):
            bench._safe_set_routing_primary(value)
        elif action == 'roleModel':
            role, _, label = value.partition('\n')
            if role in bench.state.ROLES and label in self.previous.get('models', []):
                model = next((r['id'] for r in bench.catalog_rows if r['label'] == label), None)
                bench.state.configure_role(role, bench.runtime.catalog.get(model))
                bench.status.set('模型角色已保存')
        elif action == 'template' and value in {'0', '1', '2'}:
            bench._use_template(TASK_TEMPLATES[int(value)][3])
        elif action == 'removeContext' and value.isdecimal():
            index = int(value)
            if index < len(bench.editor_contexts):
                bench.editor_contexts.pop(index)
        elif action in {'diff', 'revert'} and value in self.changes:
            record = self.changes[value]
            if action == 'diff':
                bench._view_agent_changes(record)
            else:
                bench._revert_agent_changes(record)
        elif action == 'openFile':
            bench.open_agent_file(value)
        else:
            commands = {
                'new': bench._new_agent_conversation, 'history': bench.restore_session,
                'setup': bench.open_model_setup,
                'attach': bench.attach_current, 'cancel': bench.cancel_task,
                'retry': bench.retry_agent_task, 'density': bench.toggle_density,
                'review': bench.review, 'tests': bench.run_tests,
                'approvals': bench._show_approvals_tab,
            }
            if action in commands:
                commands[action]()
