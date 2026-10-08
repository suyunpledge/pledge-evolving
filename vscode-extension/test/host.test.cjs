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
