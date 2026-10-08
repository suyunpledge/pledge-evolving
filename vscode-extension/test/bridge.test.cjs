const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { FrameReader, ForgeBridge } = require('../out/bridge');
const { inside } = require('../out/paths');
const path = require('node:path');

function fixture() {
  const child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.kill = signal => { child.killed = true; child.signal = signal; return true; };
  let options; let args; let command;
  const bridge = new ForgeBridge((cmd, argv, opts) => { command = cmd; args = argv; options = opts; return child; });
  bridge.start({ python: 'python executable', launcher: 'launcher path', engine: 'source path', cwd: 'workspace path' });
  let sent = '';
  child.stdin.on('data', chunk => { sent += chunk.toString(); });
  return { bridge, child, options, args, command, request: () => JSON.parse(sent.trim().split('\n').at(-1)) };
}

test('JSONL survives every byte boundary including Unicode and multi-frame chunks', () => {
  const frames = []; const reader = new FrameReader(frame => frames.push(frame));
  const payload = Buffer.from('{"text":"中文 😀"}\n{"text":"second"}\n');
  for (const byte of payload) { reader.feed(Buffer.from([byte])); }
  assert.deepEqual(frames, [{ text: '中文 😀' }, { text: 'second' }]);
});
test('invalid and oversized frames are rejected', () => {
  assert.throws(() => new FrameReader(() => {}).feed(Buffer.from('[]\n')));
  assert.throws(() => new FrameReader(() => {}, 4).feed(Buffer.from('12345')));
});
test('launcher disables shell interpretation and terminal windows', () => {
  const f = fixture();
  assert.equal(f.options.shell, false); assert.equal(f.options.windowsHide, true);
  assert.equal(f.command, 'python executable');
  assert.deepEqual(f.args, ['-I', '-u', 'launcher path', '--engine-root', 'source path']);
  f.bridge.dispose();
});
test('response and progress are bound to the active request', async () => {
  const f = fixture(); const events = [];
  f.bridge.on('event', event => events.push(event));
  const result = f.bridge.request('run', { prompt: 'test' }, 1000); const id = f.request().id;
  f.child.stdout.write(JSON.stringify({ event: 'progress', request: 'expired', data: { type: 'tool_call' } }) + '\n');
  f.child.stdout.write(JSON.stringify({ event: 'progress', request: id, data: { type: 'tool_call' } }) + '\n');
  f.child.stdout.write(JSON.stringify({ id, result: { ok: true, text: 'actual response' } }) + '\n');
  assert.equal((await result).text, 'actual response'); assert.equal(events.length, 1);
  f.bridge.dispose();
});
test('cancel actually terminates execution and rejects pending callers', async () => {
  const f = fixture(); const pending = f.bridge.request('run', {}, 1000);
  const rejected = assert.rejects(pending, /stopped/);
  f.bridge.stop(); await rejected;
  assert.equal(f.child.killed, true); assert.equal(f.child.signal, 'SIGKILL');
});
test('timeout kills the engine instead of merely abandoning a promise', async () => {
  const f = fixture(); await assert.rejects(f.bridge.request('run', {}, 10), /timed out/);
  assert.equal(f.child.killed, true);
});
test('exit rejects callers without forwarding raw stderr credentials', async () => {
  const f = fixture(); const pending = f.bridge.request('run', {}, 1000);
  const rejected = assert.rejects(pending, error => !error.message.includes('synthetic-raw-secret'));
  f.child.stderr.write('synthetic-raw-secret'); f.child.emit('exit', 1); await rejected;
  f.bridge.dispose();
});
test('path boundary rejects parent escapes and sibling prefix matches', () => {
  const root = path.resolve('root');
  assert(inside(root, path.join(root, 'folder', 'file.txt')));
  assert(!inside(root, path.resolve('root-other', 'file.txt')));
  assert(!inside(root, path.resolve('outside.txt')));
});
