const { test } = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const dispose = () => ({ dispose() {} });
let dialog;
let dialogSeen;
const shown = new Promise(resolve => { dialogSeen = resolve; });
const vscode = {
  StatusBarAlignment: { Right: 2 }, env: { language: 'zh-cn' },
  window: { activeTextEditor: undefined, createStatusBarItem: () => ({ show() {}, dispose() {} }),
    onDidChangeActiveTextEditor: dispose,
    showWarningMessage: () => { dialogSeen(); return new Promise(resolve => { dialog = resolve; }); } },
  workspace: { isTrusted: true, textDocuments: [], getConfiguration: () => ({ get: (_, fallback) => fallback }) },
  commands: { executeCommand: async () => {} }
};
const load = Module._load;
let ForgeView;
try {
  Module._load = function (name, ...args) { return name === 'vscode' ? vscode : load.call(this, name, ...args); };
  ({ ForgeView } = require('../out/extension'));
} finally { Module._load = load; }

test('Restricted Mode cannot start the engine', async () => {
  const view = new ForgeView({ subscriptions: [] });
  vscode.workspace.isTrusted = false;
  try { await assert.rejects(view.connect(), /信任/); assert.equal(view.bridge, undefined); }
  finally { vscode.workspace.isTrusted = true; view.dispose(); }
});

test('stop during a save dialog cannot send an old task to a new engine', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'forge-vscode-host-'));
  const file = path.join(root, 'dirty.txt'); await fs.writeFile(file, 'draft');
  const view = new ForgeView({ subscriptions: [] });
  let calls = 0;
  const engine = () => ({ dispose() {}, request: async () => { calls++; return {}; } });
  view.workspace = { name: 'test', uri: { fsPath: root } }; view.bridge = engine(); view.ready = {};
  view.connect = async () => {}; view.mode = 'workspace-write';
  vscode.workspace.textDocuments = [{ isDirty: true, isClosed: false, uri: { scheme: 'file', fsPath: file }, save: async () => true }];
  try {
    const pending = view.send('old task'); await shown;
    view.stop(); view.bridge = engine(); view.ready = {}; view.busy = true;
    dialog('保存并继续'); await pending;
    assert.equal(calls, 0); assert.equal(view.busy, true);
  } finally { vscode.workspace.textDocuments = []; view.dispose(); await fs.rm(root, { recursive: true, force: true }); }
});

test('unrecognized webview messages cannot dispatch arbitrary editor commands', async () => {
  const view = new ForgeView({ subscriptions: [] }); let commands = 0;
  const previous = vscode.commands.executeCommand;
  vscode.commands.executeCommand = async () => { commands++; };
  try {
    await view.receive({ type: 'executeCommand', command: 'workbench.action.terminal.new' });
    assert.equal(commands, 0);
  } finally { vscode.commands.executeCommand = previous; view.dispose(); }
});

test('planning selection is validated, persisted and forwarded independently of permissions', async () => {
  let saved;
  const view = new ForgeView({ subscriptions: [], workspaceState: { get: (_, fallback) => fallback,
    update: async (_, value) => { saved = value; } } });
  let params;
  view.workspace = { name: 'test', uri: { fsPath: os.tmpdir() } };
  view.ready = {}; view.connect = async () => {};
  view.bridge = { dispose() {}, request: async (_, value) => { params = value; return { ok: true, text: 'done', usage: {} }; } };
  try {
    await view.receive({ type: 'options', model: '', mode: 'read-only', planning: 'high' });
    assert.equal(saved, 'high');
    await view.send('inspect');
    assert.equal(params.planning, 'high'); assert.equal(params.mode, 'read-only');
    await view.receive({ type: 'options', model: '', mode: 'workspace-write', planning: 'bypass' });
    assert.equal(view.planning, 'high'); assert.equal(view.mode, 'read-only');
    view.busy = true;
    await view.receive({ type: 'options', model: '', mode: 'read-only', planning: 'none' });
    assert.equal(view.planning, 'high');
  } finally { view.dispose(); }
});

test('independent phase selections and manual review are validated and bound to a response', async () => {
  const saved = new Map(); let params; let reviews = 0;
  const view = new ForgeView({ subscriptions: [], workspaceState: { get: (_, fallback) => fallback,
    update: async (key, value) => saved.set(key, value) } });
  view.workspace = { name: 'test', uri: { fsPath: os.tmpdir() } }; view.connect = async () => {};
  view.ready = { models: [{ id: 'writer' }, { id: 'planner' }] };
  view.bridge = { dispose() {}, request: async (method, value) => {
    params = value;
    if (method === 'review') { reviews++; return { messageId: 'response1', text: 'Review', usage: {} }; }
    return { ok: true, text: 'code', messageId: 'response1', usage: {} };
  } };
  try {
    await view.receive({ type: 'options', model: 'writer', mode: 'read-only', planning: 'high',
      planningModel: 'planner', reviewModel: 'planner', reviewEnabled: true });
    assert.equal(saved.get('forge.planningModel'), 'planner'); assert.equal(view.model, 'writer');
    await view.send('write'); assert.equal(params.planningModel, 'planner'); assert.equal(reviews, 0);
    const history = JSON.stringify(view.history);
    await view.reviewLast('wrong'); assert.equal(reviews, 0);
    await view.reviewLast('response1'); assert.equal(reviews, 1); assert.equal(params.model, 'planner');
    assert.equal(view.transcript.at(-1).review.text, 'Review'); assert.equal(JSON.stringify(view.history), history);
    await view.receive({ type: 'options', model: 'writer', mode: 'read-only', planningModel: 'missing' });
    assert.equal(view.planningModel, 'planner');
  } finally { view.dispose(); }
});

test('a cancelled review cannot attach feedback to a new chat', async () => {
  const view = new ForgeView({ subscriptions: [] }); let resolve;
  view.ready = {}; view.reviewEnabled = true;
  view.transcript = [{ role: 'assistant', id: 'old', text: 'old code' }];
  view.bridge = { dispose() {}, request: () => new Promise(done => { resolve = done; }) };
  try {
    const pending = view.reviewLast('old'); view.newChat();
    resolve({ messageId: 'old', text: 'stale review', usage: {} }); await pending;
    assert.equal(view.transcript.length, 0); assert.equal(view.busy, false);
  } finally { view.dispose(); }
});
