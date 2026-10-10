"""Lazy desktop surfaces for local schedules, connectors and change previews.

Tk only owns widgets and snapshots. SQL, OAuth, HTTP and agent runs execute in
workers. Scheduled agents are read-only, independent of the foreground chat.
"""
import copy
import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

import i18n
from i18n import tr, dialogs
import secret_store
from gui_theme import C, FONT_SMALL, FONT_MONO_SM, pill_button, flow_controls
from phase_client import PhaseClient, model_catalog
from forge.desktop_services import DesktopStore, ConnectorService, OPERATIONS
from forge.secrets import SecretScope, redact
from forge.policy import Policy, Mode, Sandbox
from change_preview import prepare_change, check_revisions
from gui_theme import bind_scoped_wheel


class DesktopFeatures:
    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        self.auth_cancel = threading.Event()
        self.job_cancel = threading.Event()
        self.ready = threading.Event()
        self.window = None
        self.pending = None
        self.last_preview = None
        self.sync = False
        self.rows = copy.deepcopy(app.user_rows)
        self.lock = threading.Lock()
        self._snapshot_after = app.root.after(2000,self._snapshot)
        self.thread = threading.Thread(target=self._scheduler, name='Forge-Desktop-Jobs',daemon=True)
        self.thread.start()
        app.root.bind('<Destroy>',lambda event:self.close() if event.widget is app.root else None,add='+')
        locale=getattr(app.root,'_forge_locale',None)
        if locale: locale.register(self)

    def _snapshot(self):
        if self.stop.is_set(): return
        with self.lock: self.rows = copy.deepcopy(self.app.user_rows)
        self._snapshot_after = self.app.root.after(2000,self._snapshot)

    def close(self):
        self.stop.set(); self.auth_cancel.set(); self.job_cancel.set()
        try: self.app.root.after_cancel(self._snapshot_after)
        except tk.TclError: pass
        if self.pending: self.pending['done'].set()

    def _credential(self, account):
        value = secret_store.get_provider('connector.'+account)
        if account=='microsoft' and value:
            scope = SecretScope()
            for key in ('access_token','refresh_token'):
                token = json.loads(value).get(key)
                if token: scope.reference(token)
            scope.close()
        return value

    def _save_credential(self, account, value):
        if self.stop.is_set(): raise PermissionError('Forge is closing')
        secret_store.set_provider('connector.'+account, value)

    def _scheduler(self):
        try:
            self.store = DesktopStore(self.app.home / 'desktop-services.sqlite3')
            from http_transport import open_response
            self.connectors = ConnectorService(self.store,self._credential,save_credential=self._save_credential,response_opener=open_response)
        except Exception as exc:
            self.error = str(redact(str(exc))); self.ready.set(); return
        self.ready.set()
        while not self.stop.wait(2):
            try:
                jobs = self.store.claim_due()
                if not jobs: continue
                job = jobs[0]
                self.active_job=job['id']
                self.job_cancel = threading.Event()
                timer = threading.Timer(180,self.job_cancel.set); timer.daemon = True; timer.start()
                scope = SecretScope()
                agent = None
                try:
                    with self.lock: rows = copy.deepcopy(self.rows)
                    if str(Path(job['workspace']).resolve())!=job['workspace'] or not Path(job['workspace']).is_dir():
                        raise PermissionError('Scheduled workspace changed or was removed')
                    environment = {**os.environ,**secret_store.env_for()}
                    client = PhaseClient(rows, job['model'],environment,scope,self.job_cancel)
                    from forge.config import Config
                    from forge.loop import build_agent, LoopLimits
                    config = Config()
                    policy = Policy(mode=Mode.READ_ONLY,sandbox=Sandbox.READ_ONLY,workspace=Path(job['workspace']),non_interactive=True)
                    agent = build_agent(home=self.app.home, workspace=Path(job['workspace']),config=config,
                        router=client.router, policy=policy,limits=LoopLimits(max_steps=6,spawn_budget=0),
                        mount_contrib=False,expose=('read_file','list_dir','grep','edit_config'),
                        session_path=self.app.home/'sessions'/('scheduled-'+job['id']+'-'+str(time.time_ns())+'.jsonl'))
                    complete = client.router.complete
                    def bounded(messages, **options):
                        if self.stop.is_set() or self.job_cancel.is_set(): raise RuntimeError('Scheduled task cancelled')
                        options['max_tokens'] = min(int(options.get('max_tokens') or 2048),2048)
                        return complete(messages,**options)
                    client.router.complete = bounded
                    report = agent.run(job['prompt'])
                    status = 'cancelled' if self.job_cancel.is_set() or self.stop.is_set() else ('done' if report.stopped=='final' else 'failed')
                    self.store.finish_job(job['id'],status,report.text,started=job['started'])
                except Exception as exc:
                    self.store.finish_job(job['id'],'cancelled' if self.job_cancel.is_set() else 'failed',str(redact(str(exc))),started=job['started'])
                finally:
                    if agent is not None: agent.secret_scope.close()
                    scope.close(); timer.cancel(); self.active_job=None
                self.app._post_ui(self._refresh_jobs)
            except Exception as exc:
                self.app._post_ui(self.app._set_status,str(redact(str(exc))),'warn')

    def work(self, fn, done=None):
        def worker():
            try:
                if not self.ready.wait(3): raise RuntimeError('Desktop service is still starting')
                if hasattr(self,'error'): raise RuntimeError(self.error)
                value = fn()
                if done: self.app._post_ui(done,value)
            except Exception as exc:
                self.app._post_ui(self.app._set_status,str(redact(str(exc))),'error')
        threading.Thread(target=worker,name='Forge-Desktop-Action',daemon=True).start()

    def open(self, tab=0):
        if self.window is not None and self.window.winfo_exists():
            self.notebook.select(tab); self.window.lift(); return
        window = self.window = tk.Toplevel(self.app.root)
        window.title(i18n.resolve(tr('自动化与连接器'),self.app.root))
        window.geometry('820x720'); window.minsize(600,500)
        window.configure(bg=C['bg'])
        window.transient(self.app.root)
        window.protocol('WM_DELETE_WINDOW',self._close_window)
        window.bind('<Escape>',lambda _:self._close_window())
        head = tk.Frame(window,bg=C['bg']); head.pack(fill='x',padx=16,pady=10)
        pill_button(head,tr('返回工作区'),self._close_window,icon='◀').pack(side='left')
        self.notebook = ttk.Notebook(window); self.notebook.pack(fill='both',expand=True,padx=16,pady=(0,16))
        jobs = self._page(tr('定时任务'))
        connectors = self._page(tr('连接器'))
        preview = self._page(tr('代码预览'))
        self._jobs_ui(jobs); self._connectors_ui(connectors); self._preview_ui(preview)
        self.notebook.select(tab)

    def _close_window(self):
        self.auth_cancel.set()
        if self.pending:
            self.pending['approved']=False; self.pending['done'].set()
        if self.window is not None: self.window.destroy()
        self.window = None

    def _page(self, title):
        host=tk.Frame(self.notebook,bg=C['surface'])
        self.notebook.add(host,text=i18n.resolve(title,self.app.root))
        canvas=tk.Canvas(host,bg=C['surface'],highlightthickness=0)
        bar=ttk.Scrollbar(host,command=canvas.yview); bar.pack(side='right',fill='y')
        canvas.configure(yscrollcommand=bar.set); canvas.pack(fill='both',expand=True)
        page=tk.Frame(canvas,bg=C['surface'],padx=14,pady=12)
        item=canvas.create_window((0,0),window=page,anchor='nw')
        page.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(item,width=e.width))
        bind_scoped_wheel(canvas,page)
        return page

    def _l10n_refresh(self):
        if self.window is None or not self.window.winfo_exists(): return
        self.window.title(i18n.resolve(tr('自动化与连接器'),self.app.root))
        for i,title in enumerate((tr('定时任务'),tr('连接器'),tr('代码预览'))):
            self.notebook.tab(i,text=i18n.resolve(title,self.app.root))
        for field,title in (('name',tr('任务名称')),('status',tr('状态')),('due',tr('下次执行'))):
            self.job_list.heading(field,text=i18n.resolve(title,self.app.root))
        selected=self.operation_names.get(self.operation.get())
        self.operation_names={self._operation_title(name):name for name,op in OPERATIONS.items() if op.account==self.account.get()}
        self.operation.configure(values=list(self.operation_names))
        self.operation.set(next((title for title,name in self.operation_names.items() if name==selected),next(iter(self.operation_names))))

    def label(self,parent,text):
        label = i18n.Label(parent,text=text,bg=C['surface'],fg=C['subtext'],font=FONT_SMALL,anchor='w',justify='left')
        label.pack(fill='x',pady=(4,5))
        label.bind('<Configure>',lambda e: label.configure(wraplength=max(80,e.width)))
        return label

    def entry(self,parent,label,value='',secret=False):
        self.label(parent,label)
        var = tk.StringVar(value=value)
        tk.Entry(parent,textvariable=var,show='•' if secret else '',bg=C['input_bg'],fg=C['text'],insertbackground=C['text'],
                 font=FONT_SMALL,relief='flat',highlightthickness=1,highlightbackground=C['border_hi'],highlightcolor=C['accent2']).pack(fill='x',ipady=4)
        return var

    def text(self,parent,height=6):
        frame = tk.Frame(parent,bg=C['surface']); frame.pack(fill='both',expand=True,pady=6)
        text = tk.Text(frame,height=height,wrap='word',bg=C['input_bg'],fg=C['body'],insertbackground=C['text'],
                       font=FONT_MONO_SM,relief='flat',padx=10,pady=8,spacing3=4,
                       highlightthickness=1,highlightbackground=C['border_hi'])
        bar = ttk.Scrollbar(frame,command=text.yview); bar.pack(side='right',fill='y')
        text.configure(yscrollcommand=bar.set); text.pack(fill='both',expand=True)
        return text

    def workspace(self):
        panel = getattr(self.app,'workspace',None)
        return Path(panel._repo_root if panel is not None else self.app._repo_root()).resolve()

    def _jobs_ui(self, page):
        self.label(page,tr('仅在 Forge 打开时执行。使用独立只读 Agent；不会发送到当前对话。每次最多六步、三分钟。'))
        self.job_name = self.entry(page,tr('任务名称'))
        self.job_workspace = self.entry(page,tr('工作区路径'),str(self.workspace()))
        self.job_when = self.entry(page,tr('首次执行时间（本地时间 YYYY-MM-DD HH:MM）'),(dt.datetime.now()+dt.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'))
        self.job_interval = self.entry(page,tr('重复间隔（分钟；0 表示仅一次）'),'0')
        self.label(page,tr('独立任务模型'))
        self.job_models = {label:pair for pair,label in model_catalog(self.app.user_rows)}
        self.job_model = ttk.Combobox(page,values=list(self.job_models),state='readonly')
        self.job_model.pack(fill='x')
        if self.job_models: self.job_model.current(0)
        self.job_prompt = self.text(page,3)
        actions = tk.Frame(page,bg=C['surface']); actions.pack(fill='x')
        pill_button(actions,tr('添加定时任务'),self._add_job,kind='primary',icon='＋').pack(side='left',padx=(0,6))
        pill_button(actions,tr('暂停 / 恢复'),self._toggle_job,icon='⟳').pack(side='left',padx=(0,6))
        pill_button(actions,tr('停止当前任务'),lambda:self.job_cancel.set(),icon='■').pack(side='left')
        pill_button(actions,tr('刷新'),self._refresh_jobs,icon='⟳').pack(side='left',padx=6)
        flow_controls(actions)
        self.job_list = ttk.Treeview(page,columns=('name','status','due'),show='headings',height=4)
        for field,title,width in [('name',tr('任务名称'),240),('status',tr('状态'),100),('due',tr('下次执行'),160)]:
            self.job_list.heading(field,text=i18n.resolve(title,self.app.root)); self.job_list.column(field,width=width,minwidth=60)
        self.job_list.pack(fill='both',expand=True,pady=8)
        self.job_list.bind('<<TreeviewSelect>>',self._job_result)
        self.job_result = self.text(page,3); self.job_result.configure(state='disabled')
        self._refresh_jobs()

    def _add_job(self):
        try:
            model = self.job_models[self.job_model.get()]
            due = dt.datetime.strptime(self.job_when.get(),'%Y-%m-%d %H:%M').timestamp()
            interval = float(self.job_interval.get())*60
            name,prompt,workspace = self.job_name.get(),self.job_prompt.get('1.0','end-1c'),self.job_workspace.get()
            self.work(lambda:self.store.add_job(name,prompt,workspace,model,due,interval),lambda _:self._refresh_jobs())
        except Exception as exc: self.app._set_status(str(exc),'error')

    def _refresh_jobs(self):
        if self.window is None or not self.window.winfo_exists(): return
        def read():
            if not self.ready.wait(3) or not hasattr(self,'store'): raise RuntimeError('Desktop service unavailable')
            return self.store.jobs()
        self.app._submit_background('desktop-jobs',read,self._show_jobs)

    def _show_jobs(self, rows):
        if self.window is None or not self.window.winfo_exists(): return
        self.job_rows = {r['id']:r for r in rows}
        self.job_list.delete(*self.job_list.get_children())
        for row in rows:
            status={'waiting':tr('等待执行'),'running':tr('运行中…'),'done':tr('已完成'),
                    'failed':tr('执行失败'),'cancelled':tr('已停止'),'interrupted':tr('运行中断')}.get(row['status'],row['status'])
            if not row['enabled'] and row['status']=='waiting': status=tr('已暂停')
            self.job_list.insert('', 'end',iid=row['id'],values=(row['name'],i18n.resolve(status,self.app.root),
                dt.datetime.fromtimestamp(row['due']).strftime('%m-%d %H:%M')))

    def _toggle_job(self):
        selected = self.job_list.selection()
        if not selected: return
        key=selected[0]; enabled=not self.job_rows[key]['enabled']
        if not enabled and getattr(self,'active_job',None)==key: self.job_cancel.set()
        self.work(lambda:self.store.enable_job(key,enabled),lambda _:self._refresh_jobs())

    def _job_result(self,_event=None):
        selected = self.job_list.selection()
        if selected:
            self.job_result.configure(state='normal'); self.job_result.delete('1.0','end')
            self.job_result.insert('1.0',self.job_rows[selected[0]]['result']); self.job_result.configure(state='disabled')

    def _connectors_ui(self,page):
        self.label(page,tr('读取、写入和 Agent 使用分别授权；写入逐次确认。Microsoft 使用你自己的 Entra 公共客户端应用。'))
        self.account = tk.StringVar(value='github')
        ttk.Combobox(page,textvariable=self.account,values=['github','microsoft'],state='readonly').pack(fill='x')
        self.github_fields=tk.Frame(page,bg=C['surface']); self.github_fields.pack(fill='x')
        self.microsoft_fields=tk.Frame(page,bg=C['surface']); self.microsoft_fields.pack(fill='x')
        self.token = self.entry(self.github_fields,tr('GitHub Token（仅保存在受保护凭据存储）'),secret=True)
        self.client_id = self.entry(self.microsoft_fields,tr('Microsoft Client ID'))
        self.tenant = self.entry(self.microsoft_fields,tr('Microsoft Tenant'),'common')
        self.grants = {}
        row = self.grant_row = tk.Frame(page,bg=C['surface']); row.pack(fill='x',pady=10)
        for key,title in [('read',tr('允许读取')),('write',tr('允许写入')),('agent',tr('允许 Agent 使用'))]:
            var = self.grants[key] = tk.BooleanVar(value=False)
            i18n.Checkbutton(row,text=title,variable=var,bg=C['surface'],fg=C['text'],selectcolor=C['sel']).pack(side='left',padx=(0,12))
        self.scope_label=self.label(page,tr('Microsoft 权限范围（按需选择；更改后重新登录）'))
        self.scopes = sorted({op.scope for op in OPERATIONS.values() if op.scope})
        self.scope_list = tk.Listbox(page,selectmode='multiple',exportselection=False,height=5,bg=C['input_bg'],fg=C['body'],
                                     selectbackground=C['accent_soft'],selectforeground=C['accent_text'],relief='flat')
        self.scope_list.pack(fill='x')
        for scope in self.scopes: self.scope_list.insert('end',scope)
        actions=tk.Frame(page,bg=C['surface']); actions.pack(fill='x',pady=8)
        pill_button(actions,tr('保存权限'),self._save_grants,kind='primary',icon='💾').pack(side='left',padx=(0,6))
        self.login_button=pill_button(actions,tr('登录 Microsoft'),self._login,icon='🔑')
        self.login_button.pack(side='left',padx=(0,6))
        pill_button(actions,tr('断开 / 撤销'),self._disconnect,kind='danger',icon='✕').pack(side='left')
        flow_controls(actions)
        self.connection_status = self.label(page,tr('尚未连接'))
        self.operation = ttk.Combobox(page,values=[],state='readonly'); self.operation.pack(fill='x',pady=4)
        self.operation.bind('<<ComboboxSelected>>',lambda _:self._operation_fields())
        self.operation_frame = tk.Frame(page,bg=C['surface']); self.operation_frame.pack(fill='both',expand=True)
        self.account.trace_add('write',lambda *_:self._load_account())
        self._load_account()

    def _load_account(self):
        name = self.account.get()
        self.github_fields.pack_forget(); self.microsoft_fields.pack_forget()
        (self.github_fields if name=='github' else self.microsoft_fields).pack(fill='x',before=self.grant_row)
        self.login_button.configure(state='normal' if name=='microsoft' else 'disabled')
        self.scope_list.configure(state='normal' if name=='microsoft' else 'disabled')
        self.token.set('')
        values=[n for n,o in OPERATIONS.items() if o.account==name]
        self.operation_names={self._operation_title(n):n for n in values}
        self.operation.configure(values=list(self.operation_names)); self.operation.set(next(iter(self.operation_names))); self._operation_fields()
        def read():
            if not self.ready.wait(3) or not hasattr(self,'store'): raise RuntimeError('Desktop service unavailable')
            return name,self.store.account(name),bool(self._credential(name))
        self.app._submit_background('desktop-account',read,self._show_account)

    def _show_account(self,data):
        name,conf,connected=data
        if self.window is None or not self.window.winfo_exists() or self.account.get()!=name: return
        for key,var in self.grants.items(): var.set(conf.get(key) is True)
        self.client_id.set(conf.get('client_id','')); self.tenant.set(conf.get('tenant','common'))
        self.scope_list.selection_clear(0,'end')
        for i,scope in enumerate(self.scopes):
            if scope in conf.get('scopes',[]): self.scope_list.selection_set(i)
        connected=connected and conf.get('authenticated') is not False and not conf.get('updating')
        self.connection_status.configure(text=tr('已保存凭据') if connected else tr('尚未连接'))

    def _account_config(self):
        return {**{k:v.get() for k,v in self.grants.items()},'client_id':self.client_id.get().strip(),
                'tenant':self.tenant.get().strip(), 'scopes':[self.scopes[i] for i in self.scope_list.curselection()]}

    def _save_grants(self):
        name,conf,token=self.account.get(),self._account_config(),self.token.get()
        def save():
            previous=self.store.account(name)
            changed=name=='microsoft' and any(previous.get(k)!=conf.get(k) for k in ('tenant','client_id','scopes'))
            ready_conf=dict(conf,authenticated=(True if name=='github' and token else False if changed else previous.get('authenticated',False)))
            self.store.set_account(name,dict(ready_conf,updating=True))
            expected=self.store.account(name)
            def save_token():
                if name=='github' and token: self._save_credential(name,token)
                elif changed:
                    self._delete_credential(name)
            self.store.finish_account(name,expected,ready_conf,save_token)
        self.token.set(''); self.work(save,lambda _:self._load_account())

    def _login(self):
        conf=self._account_config()
        self.auth_cancel.set(); cancel=self.auth_cancel=threading.Event()
        notify=lambda code,url:self.app._post_ui(self._device_notice,code,url)
        self.work(lambda:self.connectors.device_login(conf,notify,cancel),lambda _:self._load_account())

    def _device_notice(self,code,url):
        if self.window is not None and self.window.winfo_exists():
            self.connection_status.configure(text=url+'  '+code)

    def _disconnect(self):
        name=self.account.get(); self.auth_cancel.set()
        def disconnect():
            self.store.set_account(name,{'updating':True})
            expected=self.store.account(name)
            self.store.finish_account(name,expected,{},lambda:self._delete_credential(name))
        self.work(disconnect,lambda _:self._load_account())

    def _delete_credential(self,name):
        secret_store.delete_provider('connector.'+name)

    def _operation_fields(self):
        for widget in self.operation_frame.winfo_children(): widget.destroy()
        self.fields={}
        for field in OPERATIONS[self.operation_names[self.operation.get()]].fields:
            row=tk.Frame(self.operation_frame,bg=C['surface']); row.pack(fill='x',pady=2)
            i18n.Label(row,text=field,width=10,anchor='w',bg=C['surface'],fg=C['subtext']).pack(side='left')
            if field in {'body','content','values'}:
                text=tk.Text(row,height=3,wrap='word',bg=C['input_bg'],fg=C['text'],insertbackground=C['text'],
                             relief='flat',highlightthickness=1,highlightbackground=C['border_hi'])
                text.pack(fill='x',expand=True)
                self.fields[field]=lambda text=text:text.get('1.0','end-1c')
            else:
                var=tk.StringVar(); self.fields[field]=var.get
                tk.Entry(row,textvariable=var,bg=C['input_bg'],fg=C['text'],insertbackground=C['text'],
                         relief='flat',highlightthickness=1,highlightbackground=C['border_hi']).pack(fill='x',expand=True,ipady=3)
        pill_button(self.operation_frame,tr('执行操作'),self._invoke_manual,kind='accent_soft',icon='▶').pack(anchor='w',pady=6)

    def _invoke_manual(self):
        name=self.operation_names[self.operation.get()]; args={k:get() for k,get in self.fields.items() if get()}
        self.current_workspace=self.workspace()
        self.work(lambda:self._invoke_registry(name,args,agent=False),
                  lambda result:dialogs.showinfo(tr('连接器结果'),json.dumps(result,ensure_ascii=False,indent=2)[:8000],parent=self.window))

    def policy(self):
        from forge.config import Config
        with self.lock: rows=copy.deepcopy(self.rows)
        cfg=Config(); cfg.apply_patch(rows)
        return Policy.from_config(cfg,workspace=self.workspace_snapshot,non_interactive=False)

    @property
    def workspace_snapshot(self):
        # Worker-safe; foreground sends capture the selected workspace below.
        return getattr(self,'current_workspace',Path(self.app._repo_root()))

    def schemas(self):
        if not self.ready.is_set() or not hasattr(self,'connectors'): return []
        return self.connectors.schemas()

    def call(self,name,args,cancel):
        return self._invoke_registry(name,args,cancel=cancel)

    def _invoke_registry(self,name,args,*,agent=True,cancel=None):
        from forge.tools import ToolContext
        scope=SecretScope()
        try:
            registry=self.connectors.registry(approve=lambda n,a:self.confirm(n,a,cancel),cancel_event=cancel,agent=agent)
            context=ToolContext(self.policy(),self.workspace_snapshot,secret_scope=scope)
            return registry.invoke(name,args,context).as_dict()
        finally: scope.close()

    def confirm(self,name,args,cancel=None):
        done=threading.Event(); result=[]
        def show():
            if self.stop.is_set() or cancel is not None and cancel.is_set(): done.set(); return
            title=self._operation_title(name) if name in OPERATIONS else str(name)
            result.append(dialogs.askyesno(tr('确认外部操作'),title+'\n\n'+json.dumps(redact(args),ensure_ascii=False,indent=2)[:12000],parent=self.app.root))
            done.set()
        self.app._post_ui(show)
        while not done.wait(.1):
            if self.stop.is_set() or cancel is not None and cancel.is_set(): return False
        return bool(result and result[0])

    def _preview_ui(self,page):
        pill_button(page,tr('选择项目工作区'),self._choose_project,icon='📂').pack(anchor='w',pady=(0,8))
        self.project_label=self.label(page,tr('当前项目：{path}',path=str(self.workspace())))
        self.label(page,tr('AI 文件修改先预览；确认后仍经过 Forge Policy 和敏感配置校验。文件发生外部变化时拒绝覆盖。'))
        self.label(page,tr('任务模式关闭同步时只读；开启后可按 Policy 修改文件。'))
        self.sync_var=tk.BooleanVar(value=self.sync)
        i18n.Checkbutton(page,text=tr('自动同步 AI 文件修改（仍受 Policy 限制）'),variable=self.sync_var,
            command=lambda:setattr(self,'sync',self.sync_var.get()),bg=C['surface'],fg=C['text'],selectcolor=C['sel']).pack(anchor='w')
        self.preview_text=self.text(page,16); self.preview_text.configure(state='disabled')
        actions=tk.Frame(page,bg=C['surface']); actions.pack(fill='x')
        pill_button(actions,tr('同意并应用'),lambda:self._resolve_preview(True),kind='primary',icon='✓').pack(side='left',padx=(0,6))
        pill_button(actions,tr('拒绝修改'),lambda:self._resolve_preview(False),kind='danger',icon='✕').pack(side='left')
        flow_controls(actions)
        self._show_preview()

    def _operation_title(self,name):
        op=OPERATIONS[name]; suffix=name.removeprefix('connector_'+op.account+'_')
        action=tr('读取') if not op.write else tr('发送') if suffix.endswith('_send') else tr('创建') if suffix.endswith('_create') else tr('重命名') if suffix.endswith('_rename') else tr('创建 / 修改') if suffix.endswith('_file_write') or suffix=='file_write' else tr('修改')
        resource=(tr('仓库') if suffix=='repos' else 'Issue' if 'issue' in suffix else 'PR' if suffix.startswith(('pull','pulls'))
                  else 'Excel' if suffix.startswith('excel') else tr('邮件草稿') if 'draft' in suffix else tr('邮件') if suffix.startswith('mail')
                  else tr('日历') if suffix.startswith(('event','events')) else 'Teams' if suffix=='teams'
                  else tr('频道消息') if suffix.startswith('channel_') else tr('频道') if suffix=='channels'
                  else tr('SharePoint 文件') if suffix.startswith('site_') else tr('SharePoint 站点') if suffix=='sites' else tr('文件'))
        return i18n.resolve(tr('{service} · {action} · {resource}',service='GitHub' if op.account=='github' else 'Microsoft',action=action,resource=resource),self.app.root)

    def _choose_project(self):
        if self.app._sending or self.app._task_running or self.app._gateway_starting:
            self.app._set_status(tr('请在当前请求结束后切换项目'),'warn'); return
        selected=filedialog.askdirectory(parent=self.window,title=tr('选择项目工作区'))
        if not selected: return
        def inspect():
            root=Path(selected).resolve()
            if not root.is_dir(): raise ValueError('Project directory does not exist')
            return root
        self.work(inspect,self._set_project)

    def _set_project(self,root):
        if self.app._sending or self.app._task_running or self.app._gateway_starting:
            self.app._set_status(tr('请在当前请求结束后切换项目'),'warn'); return
        root=Path(root).resolve()
        if not root.is_dir():
            self.app._set_status(tr('项目目录不存在'),'warn'); return
        from forge_gui_v2 import save_desktop_config
        if not save_desktop_config(project_workspace=str(root)):
            self.app._set_status(tr('项目设置保存失败'),'error'); return
        self.app._project_workspace=root; self.current_workspace=root
        if self.app.workspace is not None: self.app.workspace.set_repo_root(root)
        self.app._project_name_var.set(root.name)
        self.app._refresh_work_context()
        self.project_label.configure(text=tr('当前项目：{path}',path=str(root)))
        if self.app.gateway_proc is not None:
            self.app._project_switch_pending=True
            self.app._restart_gateway()

    def _show_preview(self):
        if self.window is None or not self.window.winfo_exists(): return
        self.preview_text.configure(state='normal'); self.preview_text.delete('1.0','end')
        self.preview_text.insert('1.0',self.pending['change']['preview'] if self.pending else str(tr('暂无待确认的文件修改')))
        self.preview_text.configure(state='disabled')

    def _resolve_preview(self,approved):
        if not self.pending: return
        self.pending['approved']=approved; self.pending['done'].set()

    def review_change(self,name,args,workspace,scope,cancel):
        change=prepare_change(name,args,workspace,scope)
        if change is None: return args
        if self.sync:
            self.app._post_ui(self.app._set_status,tr('正在同步 AI 文件修改'),'info')
            return change['arguments']
        pending={'change':change,'done':threading.Event(),'approved':False}
        def show():
            if cancel.is_set() or self.stop.is_set(): pending['done'].set(); return
            self.pending=pending; self.last_preview=change
            panel=getattr(self.app,'workspace',None)
            if panel is not None:
                self.app._open_workspace('diff')
                panel.show_change_preview(change['preview'])
            self.open(2); self._show_preview()
            self.app._set_request_status(tr('等待确认文件修改'))
        self.app._post_ui(show)
        deadline=time.monotonic()+300
        while not pending['done'].wait(.1):
            if cancel.is_set() or self.stop.is_set() or time.monotonic()>deadline: break
        if self.pending is pending: self.pending=None
        self.app._post_ui(self._clear_workspace_preview)
        if not pending['approved'] or cancel.is_set() or self.stop.is_set():
            raise PermissionError('File change was not approved')
        check_revisions(change['arguments']['_expected_revisions'])
        return change['arguments']

    def refresh_workspace(self):
        panel=getattr(self.app,'workspace',None)
        if panel is not None: panel.refresh_async()

    def _clear_workspace_preview(self):
        panel=getattr(self.app,'workspace',None)
        if panel is not None: panel.clear_change_preview()
