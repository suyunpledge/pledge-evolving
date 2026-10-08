(function () {
  'use strict';
  const vscode = acquireVsCodeApi();
  const $ = id => document.getElementById(id);
  const send = (type, extra = {}) => vscode.postMessage({ type, ...extra });

  const words = {
    'zh-cn': {
      offline: '未连接', connecting: '连接中', ready: '就绪', busy: '执行中',
      connect: '连接内核', settings: '设置',
      welcome: '在代码旁边，与 Forge 协作',
      intro: '三步上手：选 Python、填一次 API 密钥、连接内核。之后就能直接对话。',
      explain: '解释选中代码', review: '审查选中代码', trust: '信任当前工作区后即可连接。',
      prompt: '描述任务… Ctrl / ⌘ + Enter 发送',
      attach: '+ 当前文件', selection: '选区上下文', changes: '查看修改',
      model: '模型', mode: '权限',
      automatic: '使用 Forge 路由', readonly: '只读', write: '工作区编辑',
      hint: 'Enter 换行 · Ctrl / ⌘ + Enter 发送',
      stop: '停止', submit: '发送',
      boundary: '敏感信息由 Forge 内核保护；原生执行保持受限。',
      user: '你', assistant: 'Forge', error: '提示', remove: '移除上下文',
      selected: '选区', workspace: '工作区',
      step1Body: '选 Python 解释器——点「自动检测」即可，扫不到再手动粘贴路径。',
      step2Body: '填一次 API 密钥——只存在本机 ~/.forge/secrets.json，面板不回显。',
      step3Body: '连接内核——连接只读本地配置，不访问厂商、不扣费。',
      pythonLabel: 'Python 解释器路径', pythonScan: '自动检测',
      pythonSaved: '已保存，重新连接后生效。',
      pythonBad: '这个解释器导入不了 forge（未安装或 enginePath 指错）。',
      keyLabel: '厂商', keySave: '保存密钥',
      keySaved: '已保存。重新连接内核后生效。',
      keyBad: '请先选厂商并粘贴密钥。',
    },
    en: {
      offline: 'Disconnected', connecting: 'Connecting', ready: 'Ready', busy: 'Running',
      connect: 'Connect engine', settings: 'Settings',
      welcome: 'Work with Forge beside your code',
      intro: 'Three steps to start: pick a Python, paste an API key once, connect.',
      explain: 'Explain selection', review: 'Review selection', trust: 'Trust this workspace to connect.',
      prompt: 'Describe your task… Ctrl / ⌘ + Enter to send',
      attach: '+ Current file', selection: 'Selection context', changes: 'View changes',
      model: 'Model', mode: 'Permissions',
      automatic: 'Use Forge routing', readonly: 'Read only', write: 'Workspace edits',
      hint: 'Enter: new line · Ctrl / ⌘ + Enter: send',
      stop: 'Stop', submit: 'Send',
      boundary: 'Secrets are protected by the Forge engine. Native execution stays restricted.',
      user: 'You', assistant: 'Forge', error: 'Notice', remove: 'Remove context',
      selected: 'Selection', workspace: 'Workspace',
      step1Body: 'Pick a Python interpreter — tap "Auto-detect", or paste a path if it misses.',
      step2Body: 'Paste an API key once — stored only in ~/.forge/secrets.json, never echoed back.',
      step3Body: 'Connect the engine — connecting reads local config only; no vendor calls, no spend.',
      pythonLabel: 'Python interpreter path', pythonScan: 'Auto-detect',
      pythonSaved: 'Saved. Takes effect after reconnect.',
      pythonBad: 'This interpreter cannot import forge (not installed, or enginePath is wrong).',
      keyLabel: 'Vendor', keySave: 'Save key',
      keySaved: 'Saved. Takes effect after reconnect.',
      keyBad: 'Pick a vendor and paste a key first.',
    }
  };

  let state;
  let transcriptSignature = '';

  function t_(key) { return (words[(state && state.language) || 'en'] || words.en)[key] || key; }

  function setStep(stepEl, status, label) {
    stepEl.classList.remove('ok', 'warn', 'todo');
    stepEl.classList.add(status);
    const body = stepEl.querySelector('.step-body');
    if (body) { body.textContent = label; }
  }

  function renderOnboard(next) {
    const onboard = $('onboard');
    if (!onboard) { return; }
    const creds = next.onboarding || [];
    const allCredsOk = creds.length > 0 && creds.every(c => c.configured);
    const step1Done = next.pythonChecked === 'ok';
    const connected = next.status === 'ready';

    // Visibility: hidden until the workspace is trusted; collapses once all
    // three steps are green. (An earlier draft flipped `hidden` one way only,
    // which made the python row impossible to re-open after a failure.)
    onboard.hidden = !next.trusted || (connected && allCredsOk && step1Done);
    if (onboard.hidden) { return; }

    // Static labels refresh every render so a language switch updates them.
    $('pythonScan').textContent = t_('pythonScan');
    $('keySave').textContent = t_('keySave');
    const pythonLabel = $('pythonLabel'); if (pythonLabel) { pythonLabel.textContent = t_('pythonLabel'); }
    const keyLabel = $('keyLabel'); if (keyLabel) { keyLabel.textContent = t_('keyLabel'); }
    const keyInputLabel = $('keyInputLabel'); if (keyInputLabel) { keyInputLabel.textContent = 'API key'; }
    $('pythonPath').placeholder = 'C:\\Users\\…\\python.exe';

    // Step 1 — Python
    if (step1Done) {
      setStep($('stepPython'), 'ok', t_('step1Body') + ' · ' + (next.pythonSaved || ''));
    } else if (next.pythonChecked === 'fail') {
      setStep($('stepPython'), 'warn', next.pythonSaved || t_('pythonBad'));
    } else {
      setStep($('stepPython'), 'todo', t_('step1Body'));
    }
    $('pythonRow').hidden = step1Done;
    const pythonStatus = $('pythonStatus');
    pythonStatus.className = 'hint' + (next.pythonChecked === 'ok' ? ' ok' : next.pythonChecked === 'fail' ? ' fail' : '');
    pythonStatus.textContent = next.pythonChecked === 'ok' ? t_('pythonSaved') : '';

    // Step 2 — key
    if (allCredsOk) {
      setStep($('stepKey'), 'ok', t_('step2Body') + ' · ' + creds.map(c => c.vendor).join(', '));
    } else if (next.keyStatus === 'saved') {
      setStep($('stepKey'), 'warn', t_('keySaved'));
    } else {
      setStep($('stepKey'), 'todo', t_('step2Body'));
    }
    // The vendor list only exists after initialize(); until then there is
    // nothing meaningful to select.
    $('keyRow').hidden = allCredsOk || creds.length === 0;
    const keyStatus = $('keyStatus');
    keyStatus.className = 'hint' + (next.keyStatus === 'saved' ? ' ok' : '');
    keyStatus.textContent = next.keyStatus === 'saved' ? t_('keySaved') : '';

    const sel = $('keyVendor');
    const sig = JSON.stringify(creds.map(c => [c.name, c.configured]));
    if (sel.dataset.creds !== sig) {
      sel.replaceChildren(...creds.map(c => {
        const o = document.createElement('option');
        o.value = c.name; o.textContent = c.vendor + (c.configured ? '  ✓' : '');
        return o;
      }));
      sel.dataset.creds = sig;
    }

    // Step 3 — connect
    if (connected) { setStep($('stepConnect'), 'ok', t_('step3Body')); }
    else if (next.status === 'connecting') { setStep($('stepConnect'), 'warn', t_('connecting') + ' …'); }
    else { setStep($('stepConnect'), 'todo', t_('step3Body')); }
  }

  function render(next) {
    state = next;
    const t = words[next.language] || words.en;
    document.documentElement.lang = next.language;
    for (const [id, key] of Object.entries({ connect: 'connect', settings: 'settings', welcome: 'welcome', intro: 'intro',
      explain: 'explain', review: 'review', promptLabel: 'prompt', attach: 'attach', selection: 'selection', changes: 'changes',
      modelLabel: 'model', modeLabel: 'mode', hint: 'hint', stop: 'stop', send: 'submit', boundary: 'boundary' })) { $(id).textContent = t[key]; }
    $('trust').textContent = next.trusted ? (next.workspace ? t.workspace + ': ' + next.workspace : '') : t.trust;
    $('prompt').placeholder = t.prompt;
    $('status').textContent = t[next.busy ? 'busy' : next.status] || t.offline;
    $('status').className = next.busy ? 'busy' : next.status;
    $('activity').textContent = next.activity || '';
    $('connect').hidden = next.status === 'ready';
    $('connect').disabled = next.busy || next.status === 'connecting' || !next.trusted;
    $('stop').hidden = !next.busy;
    for (const id of ['send', 'attach', 'selection', 'model', 'mode', 'explain', 'review']) { $(id).disabled = next.busy || !next.trusted; }
    $('send').disabled = next.busy || !next.trusted || !$('prompt').value.trim();
    $('mode').options[0].textContent = t.readonly; $('mode').options[1].textContent = t.write;
    $('mode').value = next.mode;
    const models = [{ id: '', label: t.automatic }, ...(next.models || [])];
    const signature = JSON.stringify(models);
    if ($('model').dataset.models !== signature) {
      $('model').replaceChildren(...models.map(model => { const option = document.createElement('option'); option.value = model.id; option.textContent = model.label; return option; }));
      $('model').dataset.models = signature;
    }
    $('model').value = next.model || '';
    $('attachments').replaceChildren(...(next.attachments || []).map(item => {
      const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'chip'; chip.disabled = next.busy;
      chip.textContent = item.name + (item.selection ? ` · ${item.selection[0]}–${item.selection[1]}` : '') + ' ×';
      chip.title = t.remove + ': ' + item.name; chip.setAttribute('aria-label', chip.title);
      chip.addEventListener('click', () => send('remove', { index: item.index })); return chip;
    }));
    const transcript = next.transcript || [];
    const sig = JSON.stringify([next.language, transcript]);
    if (sig !== transcriptSignature) {
      transcriptSignature = sig;
      const pane = $('messages'); const nearEnd = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 100;
      for (const row of pane.querySelectorAll('.message')) { row.remove(); }
      $('empty').hidden = transcript.length > 0;
      for (const message of transcript) {
        const article = document.createElement('article'); article.className = 'message ' + message.role;
        const name = document.createElement('div'); name.className = 'message-name'; name.textContent = t[message.role] || t.assistant;
        const content = document.createElement('div'); content.className = 'message-body';
        // Model/file output is data. No HTML, arbitrary links, scripts or VS
        // Code command URIs are ever interpreted here.
        content.textContent = message.text;
        article.append(name, content);
        if (message.usage) { const usage = document.createElement('div'); usage.className = 'usage'; usage.textContent = message.usage; article.append(usage); }
        pane.append(article);
      }
      if (nearEnd) { pane.scrollTop = pane.scrollHeight; }
    }
    renderOnboard(next);
  }

  // --- onboarding controls ---
  $('pythonScan').addEventListener('click', () => send('python_scan'));
  $('pythonPath').addEventListener('change', () => {
    const value = $('pythonPath').value.trim();
    if (value) { send('python_path_set', { path: value }); }
  });
  $('keySave').addEventListener('click', () => {
    const vendor = $('keyVendor').value;
    const value = $('keyValue').value;
    const status = $('keyStatus');
    if (!vendor || !value.trim()) {
      status.className = 'hint fail';
      status.textContent = t_('keyBad');
      return;
    }
    // Clear the field immediately: the value lives in the host store from here.
    $('keyValue').value = '';
    status.className = 'hint';
    status.textContent = '';
    send('key_save', { vendor, value });
  });

  // --- existing controls ---
  for (const id of ['connect', 'settings', 'attach', 'selection', 'changes', 'stop', 'explain', 'review']) {
    $(id).addEventListener('click', () => send(id));
  }
  for (const id of ['model', 'mode']) {
    $(id).addEventListener('change', () => send('options', { model: $('model').value, mode: $('mode').value }));
  }
  $('composer').addEventListener('submit', event => {
    event.preventDefault();
    if (state && !state.busy && state.trusted && $('prompt').value.trim()) { send('send', { prompt: $('prompt').value }); }
  });
  $('prompt').addEventListener('input', () => { if (state) { $('send').disabled = state.busy || !state.trusted || !$('prompt').value.trim(); } });
  $('prompt').addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); }
  });

  window.addEventListener('message', event => {
    const message = event.data;
    if (message && message.type === 'state') { render(message); }
    else if (message && message.type === 'accepted') { $('prompt').value = ''; $('send').disabled = true; }
  });

  // No secrets, document buffers or conversations in webview localStorage or
  // VS Code's persisted webview state. Sanitized transcript lives in the host.
  send('ready');
}());