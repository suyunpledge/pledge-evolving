const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { ForgeBridge } = require('../out/bridge');

test('real Python bridge calls Forge tools and carries real history without secret payloads', { timeout: 60000 }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'forge-vscode-test-'));
  const home = path.join(root, 'home'); const workspace = path.join(root, 'workspace');
  await fs.mkdir(home); await fs.mkdir(workspace);
  const key = 'sk-vscode-integration-auth-synthetic-33771';
  const password = 'vscode-document-password-synthetic-33772';
  const requests = []; let invocation = 0; let block = false; let arrived;
  const upstreamBlocked = new Promise(resolve => { arrived = resolve; });
  const server = http.createServer((req, res) => {
    let data = ''; req.setEncoding('utf8'); req.on('data', chunk => { data += chunk; });
    req.on('end', () => {
      try {
        assert.equal(req.headers.authorization, 'Bearer ' + key);
        assert(!data.includes(key)); assert(!data.includes(password));
        requests.push(JSON.parse(data));
        if (block) { arrived(); return; }
        const message = invocation++ === 0 ? { role: 'assistant', content: null, tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'read_file', arguments: '{"path":"notes.txt"}' } }] }
          : { role: 'assistant', content: 'Actual offline reply ' + invocation };
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ choices: [{ message, finish_reason: message.tool_calls ? 'tool_calls' : 'stop' }], usage: { prompt_tokens: 20, completion_tokens: 3 } }));
      } catch (error) { res.writeHead(500); res.end(JSON.stringify({ error: String(error) })); }
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const bridge = new ForgeBridge();
  try {
    const port = server.address().port;
    await fs.writeFile(path.join(home, 'secrets.json'), JSON.stringify({ vscode: key }));
    await fs.writeFile(path.join(home, 'forge.patch.json'), JSON.stringify([
      { id: 'vscode', name: 'provider:vscode', config: { baseURL: `http://127.0.0.1:${port}/v1`, apiKey: { $expr: "get('env.FORGE_VSCODE_KEY', '')" }, wire: 'openai', model: 'offline-stub', adapt: false } },
      { id: 'model', config: { primary: ['vscode', 'offline-stub'], fallback: [], moa: false } },
      { id: 'thinking', config: { mode: 'off' } }
    ]));
    await fs.writeFile(path.join(workspace, 'notes.txt'), 'Real workspace note.');
    const events = []; bridge.on('event', event => events.push(event));
    bridge.start({ python: process.env.FORGE_TEST_PYTHON || 'python',
      launcher: path.resolve(__dirname, '../python/launch.py'), engine: path.resolve(__dirname, '../..'), cwd: workspace });
    const ready = await bridge.request('initialize', { home, workspace }, 20000);
    assert.equal(ready.protocol, 1); assert.equal(ready.ready, true); assert.equal(requests.length, 0);
    const model = ready.models.find(m => m.label.includes('offline-stub')).id;
    const result = await bridge.request('run', { model, mode: 'read-only', prompt: 'Read the notes and inspect this configuration.',
      contexts: [{ path: path.join(workspace, 'config.json'), content: JSON.stringify({ password, model: 'old' }) }] }, 20000);
    assert.equal(result.ok, true); assert.equal(result.toolCalls, 1);
    assert(requests[1].messages.some(message => String(message.content).includes('Real workspace note.')));
    const next = await bridge.request('run', { model, prompt: 'Continue our previous task.' }, 20000);
    assert.equal(next.ok, true);
    assert(requests.at(-1).messages.some(message => message.role === 'assistant' && message.content === result.text));
    assert(!JSON.stringify(events).includes(password)); assert(!JSON.stringify(events).includes(key));
    const audit = await fs.readFile(result.sessionPath, 'utf8');
    assert(!audit.includes(key)); assert(!audit.includes(password)); assert(audit.includes('tool_call'));
    // Cancellation is also exercised against a real blocked Python HTTP call.
    block = true;
    const child = bridge.child;
    const exited = new Promise(resolve => child.once('exit', resolve));
    const pending = bridge.request('run', { model, prompt: 'A task that will be cancelled.' }, 20000);
    const rejected = assert.rejects(pending, /stopped/);
    await upstreamBlocked;
    bridge.stop(); await rejected;
    let timer;
    try { await Promise.race([exited, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Cancelled Python process survived')), 5000); })]); }
    finally { clearTimeout(timer); }
    assert.throws(() => process.kill(child.pid, 0));
  } finally {
    bridge.dispose(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await fs.rm(root, { recursive: true, force: true });
  }
});
