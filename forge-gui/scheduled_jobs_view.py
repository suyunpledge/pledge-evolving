"""Schedule management and run history for a single Tk surface."""
import datetime as dt
import tkinter as tk
from tkinter import ttk

import i18n
from gui_theme import C, FONT_TITLE, flow_controls, pill_button
from i18n import tr
from phase_client import model_catalog


JOB_STATUS = {
    'waiting': tr('等待执行'), 'running': tr('运行中…'), 'done': tr('已完成'),
    'failed': tr('执行失败'), 'cancelled': tr('已停止'), 'interrupted': tr('运行中断'),
}
JOB_COLUMNS = (('name', tr('任务名称'), 210), ('status', tr('状态'), 110),
               ('due', tr('下次执行'), 155))
RUN_COLUMNS = (('started', tr('开始时间'), 210), ('status', tr('状态'), 110))


class ScheduledJobsView(tk.Frame):
    def __init__(self, parent, features, slot):
        super().__init__(parent, bg=C['surface'])
        self.features = features
        self.app = features.app
        self.slot = slot
        self.jobs = {}
        self.runs = {}
        self.selected_job = None
        self.new_job = None
        self.rendered_result = None
        self.form = None
        self.saving = False

        self.overview = tk.Frame(self, bg=C['surface'])
        self.overview.pack(fill='both', expand=True)
        features.label(self.overview, tr('仅在 Forge 打开时执行。使用独立只读 Agent；不会发送到当前对话。每次最多六步、三分钟。'))
        actions = tk.Frame(self.overview, bg=C['surface'])
        actions.pack(fill='x')
        for label, action, icon, kind in (
            (tr('添加定时任务'), self._open_form, '＋', 'primary'),
            (tr('暂停 / 恢复'), self._toggle_job, '⟳', 'ghost'),
            (tr('停止当前任务'), features.cancel_job, '■', 'ghost'),
            (tr('刷新'), features._refresh_jobs, '⟳', 'ghost'),
        ):
            pill_button(actions, label, action, kind=kind, icon=icon).pack(side='left', padx=(0, 6))
        flow_controls(actions)
        self.job_list = self._tree(JOB_COLUMNS, height=4)
        self.job_list.bind('<<TreeviewSelect>>', self._select_job)
        i18n.Label(self.overview, text=tr('运行记录'), bg=C['surface'], fg=C['text'],
                   font=FONT_TITLE, anchor='w').pack(fill='x', pady=(6, 0))
        self.run_list = self._tree(RUN_COLUMNS, height=4)
        self.run_list.bind('<<TreeviewSelect>>', self._show_result)
        self.history_hint = features.label(self.overview, tr('请选择定时任务'))
        self.result = features.text(self.overview, 4)
        self.result.configure(state='disabled')
        self._l10n_refresh()

    def _tree(self, columns, height):
        frame = tk.Frame(self.overview, bg=C['surface'])
        frame.pack(fill='both', expand=True, pady=5)
        tree = ttk.Treeview(frame, columns=[key for key, _, _ in columns], show='headings',
                            height=height, selectmode='browse')
        bar = ttk.Scrollbar(frame, command=tree.yview)
        bar.pack(side='right', fill='y')
        tree.configure(yscrollcommand=bar.set)
        tree.pack(fill='both', expand=True)
        for key, _, width in columns:
            tree.column(key, width=width, minwidth=65)
        return tree

    @staticmethod
    def _sync_rows(tree, values):
        old = tree.get_children()
        removed = set(old) - values.keys()
        if removed: tree.delete(*removed)
        for key, cells in values.items():
            if tree.exists(key): tree.item(key, values=cells)
            else: tree.insert('', 'end', iid=key, values=cells)
        if tree.get_children() != tuple(values):
            tree.set_children('', *values)

    @staticmethod
    def _selected(tree):
        return next(iter(tree.selection()), None)

    @staticmethod
    def _format_time(timestamp):
        return dt.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S') if timestamp else '—'

    def _status(self, row):
        status = row['status']
        return i18n.resolve(JOB_STATUS.get(status, status), self)

    def _render_jobs(self):
        self._sync_rows(self.job_list, {
            key: (row['name'], i18n.resolve(tr('已暂停'), self)
                  if not row['enabled'] and (row['status'] == 'waiting'
                      or row['interval'] and row['status'] not in ('running', 'interrupted')) else self._status(row),
                  self._format_time(row['due']) if row['enabled'] else '—')
            for key, row in self.jobs.items()
        })

    def _render_runs(self):
        self._sync_rows(self.run_list, {
            key: (self._format_time(row['started']), self._status(row))
            for key, row in self.runs.items()
        })

    def _l10n_refresh(self):
        for tree, columns in ((self.job_list, JOB_COLUMNS), (self.run_list, RUN_COLUMNS)):
            for key, title, _ in columns:
                tree.heading(key, text=i18n.resolve(title, self))
        self._render_jobs()
        self._render_runs()

    def show_jobs(self, rows):
        self.jobs = {row['id']: row for row in rows}
        self._render_jobs()
        if self.new_job in self.jobs:
            self.job_list.selection_set(self.new_job)
            self.job_list.see(self.new_job)
            self.new_job = None
        elif self.jobs and not self._selected(self.job_list):
            self.job_list.selection_set(next(iter(self.jobs)))
        self._select_job(force=True)

    def _select_job(self, _event=None, *, force=False):
        key = self._selected(self.job_list)
        if key != self.selected_job:
            self.selected_job = key
            self.runs = {}
            self._render_runs()
            self._show_result()
        elif not force:
            return
        if key is None: return

        def show(rows):
            if not self.winfo_exists() or self._selected(self.job_list) != key: return
            self.runs = {str(row['id']): row for row in rows}
            self._render_runs()
            if self.runs and not self._selected(self.run_list):
                self.run_list.selection_set(next(iter(self.runs)))
            self._show_result()

        self.app._submit_background('desktop-job-runs-' + self.slot,
                                    lambda: self.features.store.job_runs(key), show)

    def _show_result(self, _event=None):
        row = self.runs.get(self._selected(self.run_list))
        if self.selected_job is None: hint = tr('请选择定时任务')
        elif row is None: hint = tr('暂无运行记录')
        elif row['result']: hint = tr('执行结果')
        elif row['status'] == 'running': hint = tr('运行中…')
        else: hint = tr('本次运行没有输出')
        self.history_hint.configure(text=hint)
        result = (row['id'], row['result']) if row else (None, '')
        if result == self.rendered_result: return
        self.rendered_result = result
        self.result.configure(state='normal')
        self.result.delete('1.0', 'end')
        self.result.insert('1.0', result[1])
        self.result.configure(state='disabled')

    def _toggle_job(self):
        key = self._selected(self.job_list)
        if key not in self.jobs: return
        enabled = not self.jobs[key]['enabled']
        self.features.work(lambda: self.features.set_job_enabled(key, enabled),
                           lambda _: self.features._refresh_jobs())

    def _open_form(self):
        self.overview.pack_forget()
        self.form = form = tk.Frame(self, bg=C['surface'])
        form.pack(fill='both', expand=True)
        f = self.features
        self.name = f.entry(form, tr('任务名称'))
        self.workspace = f.entry(form, tr('工作区路径'), str(f.workspace()))
        due = (dt.datetime.now() + dt.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M')
        self.due = f.entry(form, tr('首次执行时间（本地时间 YYYY-MM-DD HH:MM）'), due)
        self.interval = f.entry(form, tr('重复间隔（分钟；0 表示仅一次）'), '0')
        f.label(form, tr('独立任务模型'))
        self.models = {label: pair for pair, label in model_catalog(self.app.user_rows)}
        self.model = ttk.Combobox(form, values=list(self.models), state='readonly')
        self.model.pack(fill='x')
        if self.models: self.model.current(0)
        f.label(form, tr('任务指令'))
        self.prompt = f.text(form, 3)
        actions = tk.Frame(form, bg=C['surface']); actions.pack(fill='x')
        self.save_button = pill_button(actions, tr('保存'), self._add_job, kind='primary')
        self.save_button.pack(side='left', padx=(0, 6))
        self.cancel_button = pill_button(actions, tr('取消'), self._close_form)
        self.cancel_button.pack(side='left')

    def _close_form(self):
        self.form.destroy()
        self.form = None
        self.overview.pack(fill='both', expand=True)

    def _add_job(self):
        if self.saving: return
        model = self.models.get(self.model.get())
        if model is None:
            self.app._set_status(tr('选择模型'), 'error')
            return
        try:
            due = dt.datetime.strptime(self.due.get(), '%Y-%m-%d %H:%M').timestamp()
            interval = float(self.interval.get()) * 60
        except (ValueError, OSError, OverflowError) as exc:
            self.app._set_status(str(exc), 'error')
            return
        name, prompt, workspace = self.name.get(), self.prompt.get('1.0', 'end-1c'), self.workspace.get()
        self.saving = True
        for button in (self.save_button, self.cancel_button): button.configure(state='disabled')

        def failed():
            if not self.winfo_exists(): return
            self.saving = False
            for button in (self.save_button, self.cancel_button): button.configure(state='normal')

        def done(key):
            if self.winfo_exists():
                self.saving = False
                self.new_job = key
                self._close_form()
            self.features._refresh_jobs()

        self.features.work(lambda: self.features.store.add_job(name, prompt, workspace, model, due, interval),
                           done, failed=failed)
