import * as vscode from 'vscode';
import * as path from 'node:path';
import { realpath } from 'node:fs/promises';
import { ForgeBridge, BridgeEvent, Context, Ready, Result, Turn } from './bridge';
import { inside, validMessage } from './paths';
import { viewHtml } from './view';

type VisibleTurn = { role: 'user' | 'assistant' | 'error' | 'plan'; text: string; usage?: string; id?: string;
  review?: { loading: boolean; text?: string; error?: string; truncated?: boolean; usage?: string } };
type Credential = { vendor: string; configured: boolean; name: string; env: string[] };

export class ForgeView implements vscode.WebviewViewProvider, vscode.Disposable {
  private view?: vscode.WebviewView;
  private bridge?: ForgeBridge;
  private connecting?: Promise<void>;
  private workspace?: vscode.WorkspaceFolder;
  private ready?: Ready & { onboarding?: Credential[]; config_dir?: string };
  private history: Turn[] = [];
  private transcript: VisibleTurn[] = [];
  private contexts: Context[] = [];
  private busy = false;
  private mode = 'read-only';
  private model = '';
  private planning = 'none';
  private planningModel = '';
  private reviewModel = '';
  private reviewEnabled = false;
  private generation = 0;
  private status: 'offline' | 'connecting' | 'ready' = 'offline';
  private activity = '';
  private pythonSaved?: string;       // human readable feedback after python_path_set
  private keyStatus?: 'saved';       // human readable feedback after key_save
  private pythonChecked?: string;    // result of last python_path_check (used by view)
  private editor = vscode.window.activeTextEditor;
  private bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 80);

  constructor(private context: vscode.ExtensionContext) {
    const saved = context.workspaceState?.get<string>('forge.planning', 'none');
    if (['none', 'low', 'medium', 'high'].includes(saved || '')) { this.planning = saved!; }
    this.planningModel = context.workspaceState?.get<string>('forge.planningModel', '') || '';
    this.reviewModel = context.workspaceState?.get<string>('forge.reviewModel', '') || '';
    this.reviewEnabled = context.workspaceState?.get<boolean>('forge.reviewEnabled', false) === true;
    this.bar.text = '$(comment-discussion) Forge'; this.bar.command = 'forge.open'; this.bar.show();
    context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(editor => {
      if (editor?.document.uri.scheme === 'file') { this.editor = editor; }
    }));
  }
  private chinese(): boolean {
    const lang = vscode.workspace.getConfiguration('forge').get<string>('language', 'auto');
    return lang === 'zh-cn' || lang === 'auto' && vscode.env.language.startsWith('zh');
  }
  private text(zh: string, en: string): string { return this.chinese() ? zh : en; }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, 'media')] };
    view.webview.html = viewHtml(view.webview, this.context.extensionUri);
    view.webview.onDidReceiveMessage(message => {
      if (!validMessage(message)) { return; }
      void this.receive(message).catch(error => this.showError(error));
    }, undefined, this.context.subscriptions);
    view.onDidDispose(() => { if (this.view === view) { this.view = undefined; } }, undefined, this.context.subscriptions);
    this.refresh();
  }
  private refresh(): void {
    void this.view?.webview.postMessage({ type: 'state', language: this.chinese() ? 'zh-cn' : 'en',
      busy: this.busy, status: this.status, activity: this.activity, trusted: vscode.workspace.isTrusted,
      workspace: this.workspace?.name || '', models: this.ready?.models || [], model: this.model, mode: this.mode, planning: this.planning,
      planningModel: this.planningModel, reviewModel: this.reviewModel, reviewEnabled: this.reviewEnabled,
      transcript: this.transcript, attachments: this.contexts.map((c, index) => ({ index, name: path.basename(c.path), selection: c.selection })),
      onboarding: this.ready?.onboarding || [],
      pythonChecked: this.pythonChecked, pythonSaved: this.pythonSaved, keyStatus: this.keyStatus });
    this.bar.text = this.busy ? '$(sync~spin) Forge' : '$(comment-discussion) Forge';
  }

  // --- friendly error mapping: pick the right recovery action ---------------------
  private showError(err: unknown): void {
    const message = err instanceof Error ? err.message : this.text('Forge 操作失败', 'Forge operation failed');
    this.transcript.push({ role: 'error', text: message });
    this.status = this.bridge && this.ready ? 'ready' : 'offline';
    const normalized = message.toLowerCase();
    const is401 = /401|api.?key|credentials?/i.test(normalized);
    const isPython = /python|interpreter|enginesourceerror|fork|execnum|filenotfound/i.test(normalized);
    const button = is401 ? this.text('配置 API 密钥', 'Set up API key')
                  : isPython ? this.text('检查 Python', 'Check Python')
                  : this.text('重连', 'Connect to engine');
    void vscode.window.showErrorMessage(message, button).then(choice => {
      if (!choice) { this.refresh(); return; }
      if (choice === button) {
        if (is401 || choice.includes('API') || choice.includes('密钥')) { void this.open(); }
        else if (isPython || choice.includes('Python')) { void this.open(); }
        else { void this.reconnect(); }
      }
    });
  }

  // ----------------------------------------------------------------
  open(): void { vscode.commands.executeCommand('workbench.view.extension.forge.chat'); this.refresh(); }
  async connect(): Promise<void> {
    // Restricted Mode must never start the engine: an untrusted workspace gets
    // no Python process, no tool execution and no credential reads. This guard
    // was lost when the file was rewritten; host.test.cjs pins it.
    if (!vscode.workspace.isTrusted) { throw new Error(this.text('请先信任当前工作区，再启动 Forge。', 'Trust this workspace before starting Forge.')); }
    if (this.connecting) { return this.connecting; }
    if (this.bridge && this.status === 'ready') { return; }
    const connecting = this.initialize(this.generation); this.connecting = connecting;
    try { await connecting; }
    finally { if (this.connecting === connecting) { this.connecting = undefined; } }
  }
  private async initialize(generation: number): Promise<void> {
    const folders = (vscode.workspace.workspaceFolders || []).filter(folder => folder.uri.scheme === 'file');
    if (!folders.length) { throw new Error(this.text('请打开本地工作区文件夹。', 'Open a local workspace folder.')); }
    this.workspace = this.workspace && folders.find(f => f.uri.toString() === this.workspace?.uri.toString()) ||
      (folders.length === 1 ? folders[0] : await vscode.window.showWorkspaceFolderPick());
    if (!this.workspace) { return; }
    if (generation !== this.generation) { return; }
    const config = vscode.workspace.getConfiguration('forge');
    let engine = config.get('enginePath', '').trim();
    const python = config.get('pythonPath', 'python').trim();
    if (!python || python.toLowerCase().endsWith('pythonw.exe')) { throw new Error(this.text('请使用 python.exe，以便读取 Forge 响应。', 'Use python.exe so Forge responses can be read.')); }
    if (engine && !path.isAbsolute(engine)) { throw new Error(this.text('Forge 源码目录必须为绝对路径。', 'Forge source directory must be an absolute path.')); }
    const bridge = new ForgeBridge(); this.bridge = bridge; this.status = 'connecting'; this.refresh();
    bridge.on('event', (event: BridgeEvent) => {
      if (this.bridge !== bridge) { return; }
      if (event.event === 'user' && typeof event.content === 'string' && typeof event.display === 'string') {
        this.history.push({ role: 'user', content: event.content });
        this.transcript.push({ role: 'user', text: event.display }); this.contexts = [];
        void this.view?.webview.postMessage({ type: 'accepted' });
      } else if (event.event === 'progress') {
        const data = event.data;
        if (data && data.type === 'tool_call') { this.activity = this.text('工具：', 'Tool: ') + String(event.data.tool || '') + (event.data.ok === false ? ' · ' + this.text('未执行或失败', 'denied or failed') : ''); }
        else if (data && data.type === 'compaction') { this.activity = this.text('正在整理上下文', 'Compacting context'); }
        else if (data && data.type === 'secret_protection') { this.activity = this.text('敏感配置已保护', 'Sensitive configuration protected'); }
        else if (data && data.type === 'thinking_engaged') { this.activity = this.text('正在分析任务', 'Analyzing task'); }
        else if (data && data.type === 'workflow_state') {
          this.activity = data.phase === 'planning' ? this.text('正在生成事前规划', 'Preparing task plan')
            : data.phase === 'executing' ? this.text('按计划执行；权限仍由 Policy 判定', 'Executing; Policy controls permissions')
            : this.activity;
        } else if (data && data.type === 'task_plan' && data.plan && typeof data.plan === 'object') {
          const plan = data.plan as Record<string, unknown>;
          const lines = ['requirements', 'design', 'tasks', 'acceptance', 'risks'].flatMap(key =>
            Array.isArray(plan[key]) ? [key + ':', ...(plan[key] as unknown[]).filter(v => typeof v === 'string').map(v => '• ' + v)] : []);
          this.transcript.push({ role: 'plan', text: lines.join('\n') });
        }
      }
      this.refresh();
    });
    bridge.on('closed', () => { if (this.bridge === bridge) { this.ready = undefined; this.status = 'offline'; this.refresh(); } });
    try {
      bridge.start({ python, engine, launcher: vscode.Uri.joinPath(this.context.extensionUri, 'python', 'launch.py').fsPath, cwd: this.workspace.uri.fsPath });
      const ready = await bridge.request<Ready>('initialize', { workspace: this.workspace.uri.fsPath, home: config.get('homePath', ''), history: this.history }, 30000);
      if (generation !== this.generation || this.bridge !== bridge) { bridge.dispose(); return; }
      if (!ready || ready.protocol !== 1 || !Array.isArray(ready.models) || !ready.ready) { throw new Error('Forge bridge protocol is incompatible. Update Forge and the extension.'); }
      this.ready = ready as Ready & { onboarding?: Credential[]; config_dir?: string };
      if (this.model && !ready.models.some(model => model.id === this.model)) { this.model = ''; }
      this.status = 'ready'; this.pythonChecked = undefined; this.refresh();
    } catch (error) { bridge.dispose(); if (this.bridge === bridge) { this.bridge = undefined; this.ready = undefined; } throw error; }
  }
  async attach(selection = false): Promise<void> {
    if (this.busy) { throw new Error(this.text('请先停止当前任务。', 'Stop the current task first.')); }
    const generation = this.generation;
    await this.connect();
    if (generation !== this.generation) { return; }
    const editor = vscode.window.activeTextEditor || this.editor;
    if (!editor || editor.document.isClosed || editor.document.uri.scheme !== 'file' || !this.workspace) { throw new Error(this.text('请打开工作区内的代码文件。', 'Open a code file in the workspace.')); }
    if (selection && editor.selection.isEmpty) { throw new Error(this.text('请先选择需要解释或审查的代码。', 'Select code to explain or review first.')); }
    const candidate = await realpath(editor.document.uri.fsPath);
    const root = await realpath(this.workspace.uri.fsPath);
    if (generation !== this.generation) { return; }
    if (!inside(root, candidate)) { throw new Error(this.text('当前文件不属于此工作区。', 'The current file is outside this workspace.')); }
    const content = editor.document.getText();
    const item: Context = { path: candidate, content };
    if (selection) { item.selection = [editor.selection.start.line + 1, editor.selection.end.line + 1]; }
    const contexts = [...this.contexts.filter(c => c.path !== candidate), item];
    if (contexts.length > 4 || contexts.reduce((n, c) => n + Buffer.byteLength(c.content), 0) > 128 * 1024) {
      throw new Error(this.text('上下文最多 4 个文件、共 128 KiB。请选择较小的文件。', 'Context is limited to four files and 128 KiB. Choose smaller files.'));
    }
    this.contexts = contexts; await this.open(); this.refresh();
  }
  async send(prompt: string): Promise<void> {
    if (this.busy || !prompt.trim()) { return; }
    if (Buffer.byteLength(prompt) > 16384) { throw new Error(this.text('输入超过 16 KiB，请缩短任务。', 'Prompt exceeds 16 KiB. Shorten the task.')); }
    this.busy = true; this.activity = this.text('正在保护输入并准备请求', 'Protecting input and preparing request'); this.refresh();
    const generation = this.generation;
    try {
      await this.connect();
      if (!this.bridge || !this.ready || !this.workspace || generation !== this.generation) { return; }
      const bridge = this.bridge;
      if (this.mode === 'workspace-write') {
        const dirty = vscode.workspace.textDocuments.filter(d => d.isDirty && d.uri.scheme === 'file' && inside(this.workspace!.uri.fsPath, d.uri.fsPath));
        if (dirty.length) {
          const save = this.text('保存并继续', 'Save and continue');
          const choice = await vscode.window.showWarningMessage(this.text('Forge 编辑前需要保存工作区内未保存的文件，以避免冲突。', 'Save unsaved workspace files before Forge edits them to avoid conflicts.'), { modal: true }, save);
          if (choice !== save || !(await Promise.all(dirty.map(d => d.save()))).every(Boolean)) { return; }
        }
      }
      for (const document of vscode.workspace.textDocuments) {
        if (document.uri.scheme !== 'file' || document.isClosed) { continue; }
        const candidate = await realpath(document.uri.fsPath).catch(() => '');
        const attached = this.contexts.find(context => context.path === candidate);
        if (attached) { attached.content = document.getText(); }
      }
      if (generation !== this.generation || this.bridge !== bridge) { return; }
      const seconds = Math.min(1800, Math.max(30, vscode.workspace.getConfiguration('forge').get('requestTimeoutSeconds', 300)));
      const result = await bridge.request<Result>('run', { prompt, contexts: this.contexts, model: this.model, mode: this.mode,
        planning: this.planning, planningModel: this.planningModel }, seconds * 1000);
      if (generation !== this.generation) { return; }
      if (!result || typeof result.text !== 'string') { throw new Error('Invalid Forge result'); }
      this.history.push({ role: 'assistant', content: result.text });
      this.history = this.history.slice(-40);
      this.transcript.push({ role: result.ok ? 'assistant' : 'error', text: result.text,
        id: result.messageId,
        usage: `${Number(result.usage?.prompt_tokens || 0)} + ${Number(result.usage?.completion_tokens || 0)} tokens · ${result.toolCalls || 0} tools` });
      this.transcript = this.transcript.slice(-80);
      this.activity = result.stopped === 'max_steps' ? this.text('已达工具步骤上限，可继续描述下一步。', 'Tool step limit reached. Describe the next step to continue.') : '';
    } catch (error) { if (generation === this.generation) { this.showError(error); } }
    finally { if (generation === this.generation) { this.busy = false; this.refresh(); } }
  }
  async reviewLast(messageId: string): Promise<void> {
    if (this.busy || !this.reviewEnabled || !this.bridge || !this.ready || !vscode.workspace.isTrusted) { return; }
    const target = this.transcript[this.transcript.length - 1];
    if (!target || target.role !== 'assistant' || !target.id || target.id !== messageId || !target.text.trim()) { return; }
    const bridge = this.bridge, generation = this.generation;
    this.busy = true; target.review = { loading: true }; this.activity = this.text('复审中…', 'Reviewing…'); this.refresh();
    try {
      const result = await bridge.request<{ text: string; messageId: string; truncated: boolean; usage: Record<string, unknown> }>(
        'review', { messageId, model: this.reviewModel, enabled: true }, 120000);
      if (generation !== this.generation || this.bridge !== bridge || this.transcript.at(-1) !== target) { return; }
      if (result.messageId !== messageId || typeof result.text !== 'string') { throw new Error('Invalid review target/result'); }
      target.review = { loading: false, text: result.text, truncated: result.truncated,
        usage: `${Number(result.usage?.prompt_tokens || 0)} + ${Number(result.usage?.completion_tokens || 0)} tokens` };
    } catch (error) {
      if (generation === this.generation && this.transcript.at(-1) === target) {
        target.review = { loading: false, error: error instanceof Error ? error.message : 'Review failed' };
      }
    } finally {
      if (generation === this.generation) { this.busy = false; this.activity = ''; this.refresh(); }
    }
  }
  stop(): void {
    for (const turn of this.transcript) {
      if (turn.review?.loading) { turn.review = { loading: false, error: this.text('复审已停止。', 'Review stopped.') }; }
    }
    const running = this.busy; this.generation++; this.bridge?.dispose(); this.bridge = undefined; this.ready = undefined;
    this.connecting = undefined; this.busy = false; this.status = 'offline';
    this.activity = running ? this.text('执行已终止；已完成的文件修改会保留。', 'Execution terminated. Completed file changes are retained.') : '';
    this.refresh();
  }
  newChat(): void { this.stop(); this.history = []; this.transcript = []; this.contexts = []; this.activity = ''; this.refresh(); }
  async configure(): Promise<void> { await vscode.commands.executeCommand('workbench.action.openSettings', '@ext:forge-local.forge-agent'); }
  async reconnect(): Promise<void> { this.stop(); await this.connect(); }
  async action(review: boolean): Promise<void> {
    const generation = this.generation;
    await this.attach(true);
    if (generation !== this.generation) { return; }
    await this.send(review ? this.text('审查选中代码，指出有证据的问题和修复建议。', 'Review the selected code and identify evidence-backed problems and fixes.') :
      this.text('解释选中代码的作用、数据流和重要边界。', 'Explain the selected code, its data flow and important boundaries.'));
  }

  // ---------- onboarding helpers ----------
  /**
   * Refuse to spawn anything while the workspace is untrusted.
   *
   * `forge.pythonPath` is listed in package.json `restrictedConfigurations`, so
   * in Restricted Mode the *setting* is already inert. But the wizard handlers
   * take a path straight from a webview message, which would sidestep that
   * protection. This is the host-side equivalent of the gate in connect().
   */
  private assertTrustedForSpawn(what: string): boolean {
    if (vscode.workspace.isTrusted) { return true; }
    void vscode.window.showErrorMessage(
      this.text('请先信任当前工作区，Forge 才能启动 Python 或写入密钥。',
                'Trust this workspace before Forge can start Python or write credentials.'));
    this.transcript.push({ role: 'error',
      text: this.text(`已拒绝 ${what}：工作区未受信任。`, `${what} refused: workspace is not trusted.`) });
    this.refresh();
    return false;
  }

  private async scanPythonCandidates(): Promise<string[]> {
    const candidates: string[] = [];
    const seen = new Set<string>();
    const push = (p: string) => {
      const trimmed = p.trim();
      if (!trimmed || seen.has(trimmed.toLowerCase())) { return; }
      if (trimmed.toLowerCase().endsWith('pythonw.exe')) { return; }
      seen.add(trimmed.toLowerCase());
      candidates.push(trimmed);
    };
    // PATH 查询：Windows 用 where，其它平台用 which -a。
    // 都包在 try 里：查不到不致命，后面还有常见路径兜底。
    const isWin = process.platform === 'win32';
    try {
      const probe = await new Promise<string>((resolve) => {
        const cp = require('node:child_process').spawn(isWin ? 'where' : 'which',
          isWin ? ['python'] : ['-a', 'python3'], { windowsHide: true, shell: false });
        let stdout = '';
        const timer = setTimeout(() => cp.kill(), 5000);
        cp.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
        cp.on('error', () => { clearTimeout(timer); resolve(''); });
        cp.on('close', () => { clearTimeout(timer); resolve(stdout); });
      });
      for (const line of probe.split(/\r?\n/)) { push(line); }
    } catch { /* ignore */ }
    // 常见安装位置（覆盖绝大多数小白环境）
    const home = process.env['USERPROFILE'] || process.env['HOME'] || '';
    const guesses = isWin ? [
      ...['313', '312', '311', '310'].map(v => `${home}\\AppData\\Local\\Programs\\Python\\Python${v}\\python.exe`),
      ...['313', '312', '311', '310'].map(v => `C:\\Python${v}\\python.exe`),
    ] : ['/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3'];
    for (const guess of guesses) { push(guess); }
    // 存在性预过滤：不对不存在的路径 spawn（WindowsApps 的 python 存根
    // 会在被调用时弹商店卡住，绝不能进入探测阶段）。
    const { existsSync } = require('node:fs') as typeof import('node:fs');
    return candidates.filter(c => {
      try { return existsSync(c); } catch { return false; }
    });
  }

  private async checkPython(pythonPath: string): Promise<{ ok: boolean; reason?: string }> {
    // 不走 bridge：向导的「选 Python」必须能在还没有可用连接时运行，
    // 否则就是鸡生蛋——连不上内核恰恰是因为 Python 配错了。
    // 直接 spawn 候选解释器：①能跑起来 ②版本 ≥ 3.10 ③（若能找到 engine）可导入 forge。
    const engine = vscode.workspace.getConfiguration('forge').get('enginePath', '').trim();
    const code = [
      'import importlib.util, json, sys',
      'out = {"version": "%d.%d.%d" % sys.version_info[:3]}',
      'ok = sys.version_info >= (3, 10)',
      `root = ${JSON.stringify(engine)}`,
      'if root: sys.path.insert(0, root)',
      'found = {n: bool(importlib.util.find_spec(n)) for n in ["forge.config","forge.loop","forge.policy","forge.secrets","forge.routing"]}',
      'out["forge"] = found',
      'out["ok"] = ok and (not root or found.get("forge.config", False))',
      'print(json.dumps(out))',
    ].join('; ');
    try {
      const result = await new Promise<{ code: number; stdout: string; stderr: string }>((resolve) => {
        const cp = require('node:child_process').spawn(pythonPath, ['-I', '-c', code],
          { windowsHide: true, shell: false });
        let stdout = ''; let stderr = '';
        // 存根解释器（WindowsApps 商店别名）可能永不退出：硬超时杀掉。
        const timer = setTimeout(() => { try { cp.kill(); } catch { /* ignore */ } }, 8000);
        cp.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
        cp.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
        cp.on('error', () => { clearTimeout(timer); resolve({ code: -1, stdout, stderr }); });
        cp.on('close', (c: number) => { clearTimeout(timer); resolve({ code: c ?? -1, stdout, stderr }); });
      });
      if (result.code !== 0) {
        return { ok: false, reason: (result.stderr || 'python exited non-zero').split('\n')[0].slice(0, 160) };
      }
      const last = result.stdout.trim().split(/\r?\n/).pop() || '';
      const info = JSON.parse(last) as { ok: boolean; version: string; forge: Record<string, boolean> };
      if (!info.ok) {
        return { ok: false, reason: info.forge && !info.forge['forge.config']
          ? this.text('这个 Python 导入不了 forge——Forge 未安装或 forge.enginePath 指错。', 'This Python cannot import forge — install Forge or fix forge.enginePath.')
          : this.text('需要 Python 3.10+（当前 ' + info.version + '）。', 'Requires Python 3.10+ (found ' + info.version + ').') };
      }
      return { ok: true, reason: 'Python ' + info.version };
    } catch (error) {
      return { ok: false, reason: error instanceof Error ? error.message : String(error) };
    }
  }

  private async saveSecret(vendor: string, value: string): Promise<{ ok: boolean; error?: string }> {
    // 与 checkPython 同理由：不能依赖 bridge。密钥值走 stdin 不进 argv
    // （argv 对同机任意进程可见）；落盘逻辑在受信任的 save_secret.py 里，
    // 与 forge.secrets._store_set_secret 同一套路径保护与原子写入。
    const config = vscode.workspace.getConfiguration('forge');
    const python = config.get('pythonPath', 'python').trim() || 'python';
    const home = config.get('homePath', '').trim();
    const engine = config.get('enginePath', '').trim();
    const script = vscode.Uri.joinPath(this.context.extensionUri, 'python', 'save_secret.py').fsPath;
    const args = ['-I', script, '--name', vendor];
    if (home) { args.push('--home', home); }
    if (engine) { args.push('--engine-root', engine); }
    try {
      return await new Promise<{ ok: boolean; error?: string }>((resolve) => {
        const cp = require('node:child_process').spawn(python, args, { windowsHide: true, shell: false });
        let stdout = ''; let stderr = '';
        cp.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
        cp.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
        const timer = setTimeout(() => { try { cp.kill(); } catch { /* ignore */ } }, 10000);
        cp.on('error', (e: Error) => { clearTimeout(timer); resolve({ ok: false, error: e.message }); });
        cp.on('close', (code: number) => {
          clearTimeout(timer);
          const line = (stdout.trim().split(/\r?\n/).pop() || '');
          try {
            const parsed = JSON.parse(line) as { ok: boolean; error?: string };
            resolve(parsed);
          } catch {
            resolve({ ok: code === 0, error: code === 0 ? undefined : (stderr || line || 'save failed').split('\n')[0].slice(0, 160) });
          }
        });
        cp.stdin.end(value);
      });
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) };
    }
  }

  private async receive(message: Record<string, unknown>): Promise<void> {
    switch (message.type) {
      case 'ready': this.refresh(); break;
      case 'send': if (typeof message.prompt === 'string') { await this.send(message.prompt); } break;
      case 'connect': await this.connect(); break;
      case 'settings': await this.configure(); break;
      case 'stop': this.stop(); break;
      case 'attach': await this.attach(); break;
      case 'selection': await this.attach(true); break;
      case 'explain': await this.action(false); break;
      case 'review': await this.action(true); break;
      case 'reviewLast': if (typeof message.messageId === 'string') { await this.reviewLast(message.messageId); } break;
      case 'changes': await vscode.commands.executeCommand('workbench.view.scm'); break;
      case 'remove': if (!this.busy && typeof message.index === 'number' && Number.isInteger(message.index) && message.index >= 0 && message.index < this.contexts.length) { this.contexts.splice(message.index, 1); this.refresh(); } break;
      case 'options':
        if (!this.busy && (message.mode === 'read-only' || message.mode === 'workspace-write') && typeof message.model === 'string' &&
            (!message.model || this.ready?.models.some(model => model.id === message.model)) &&
            ['planningModel', 'reviewModel'].every(key => message[key] === undefined || typeof message[key] === 'string' &&
              (!message[key] || this.ready?.models?.some(model => model.id === message[key]))) &&
            (message.reviewEnabled === undefined || typeof message.reviewEnabled === 'boolean') &&
            (message.planning === undefined || ['none', 'low', 'medium', 'high'].includes(String(message.planning)))) {
          this.mode = message.mode; this.model = message.model;
          if (typeof message.planning === 'string') {
            this.planning = message.planning;
            await this.context.workspaceState?.update('forge.planning', this.planning);
          }
          for (const key of ['planningModel', 'reviewModel'] as const) {
            if (typeof message[key] === 'string') {
              this[key] = message[key] as string;
              await this.context.workspaceState?.update('forge.' + key, this[key]);
            }
          }
          if (typeof message.reviewEnabled === 'boolean') {
            this.reviewEnabled = message.reviewEnabled;
            await this.context.workspaceState?.update('forge.reviewEnabled', this.reviewEnabled);
          }
          this.refresh();
        }
        break;
      case 'python_scan': {
        if (!this.assertTrustedForSpawn('python_scan')) { break; }
        const candidates = await this.scanPythonCandidates();
        let chosen: string | undefined;
        let lastReason: string | undefined;
        for (const candidate of candidates) {
          const check = await this.checkPython(candidate);
          if (check.ok) { chosen = candidate; break; }
          lastReason = check.reason;
        }
        if (chosen) {
          await vscode.workspace.getConfiguration('forge').update('pythonPath', chosen, vscode.ConfigurationTarget.Global);
          this.pythonChecked = 'ok';
          this.pythonSaved = chosen;
        } else {
          this.pythonChecked = 'fail';
          this.pythonSaved = lastReason || this.text('未找到可用的 Python。', 'No usable Python was found.');
        }
        this.refresh();
        break;
      }
      case 'python_path_set': {
        if (!this.assertTrustedForSpawn('python_path_set')) { break; }
        if (typeof message.path === 'string' && message.path.trim()) {
          const candidate = message.path.trim();
          const check = await this.checkPython(candidate);
          if (check.ok) {
            await vscode.workspace.getConfiguration('forge').update('pythonPath', candidate, vscode.ConfigurationTarget.Global);
            this.pythonChecked = 'ok';
            this.pythonSaved = candidate;
          } else {
            this.pythonChecked = 'fail';
            this.pythonSaved = check.reason || candidate;
          }
          this.refresh();
        }
        break;
      }
      case 'key_save': {
        if (!this.assertTrustedForSpawn('key_save')) { break; }
        const vendor = String(message.vendor || '');
        const value = String(message.value || '');
        if (!vendor || !value) { this.keyStatus = undefined; this.refresh(); return; }
        const saved = await this.saveSecret(vendor, value);
        if (saved.ok) {
          this.keyStatus = 'saved';
          // 密钥已落盘；若引擎已连，重连一次让它重新加载凭据。
          if (this.bridge && this.ready) { void this.reconnect(); }
        } else {
          this.keyStatus = undefined;
          void vscode.window.showErrorMessage(saved.error || this.text('密钥保存失败', 'Failed to save the key'));
        }
        this.refresh();
        break;
      }
    }
  }
  settingsChanged(): void { this.stop(); this.refresh(); }
  dispose(): void { this.stop(); this.bar.dispose(); }
}

export function activate(context: vscode.ExtensionContext): void {
  const view = new ForgeView(context);
  const register = (name: string, action: () => void | Promise<void>) => context.subscriptions.push(vscode.commands.registerCommand(name, async () => {
    try { await action(); } catch (error) { void vscode.window.showErrorMessage(error instanceof Error ? error.message : 'Forge operation failed'); }
  }));
  context.subscriptions.push(view, vscode.window.registerWebviewViewProvider('forge.chat', view));
  register('forge.open', () => view.open()); register('forge.newChat', () => view.newChat()); register('forge.stop', () => view.stop());
  register('forge.attachFile', () => view.attach()); register('forge.attachSelection', () => view.attach(true));
  register('forge.explainSelection', () => view.action(false)); register('forge.reviewSelection', () => view.action(true));
  register('forge.configure', () => view.configure()); register('forge.reconnect', () => view.reconnect());
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(event => { if (event.affectsConfiguration('forge')) { view.settingsChanged(); } }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => view.newChat()),
    vscode.workspace.onDidGrantWorkspaceTrust(() => view.settingsChanged()));
}
