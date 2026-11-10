/** Verify the free Hunyuan route using WorkBuddy's authenticated local broker.
 * No access token leaves WorkBuddy; discovery ticket and proofs stay in memory.
 * Default is inventory only. --live permits bounded model requests.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const crypto = require('node:crypto');

function proof(ticket, side, transcript) {
  const parts = [];
  for (const value of [side === 'server' ? 'wbipc-s' : 'wbipc-c', '1',
    transcript.endpoint, transcript.clientNonce, transcript.serverNonce]) {
    const bytes = Buffer.from(value, 'utf8');
    const size = Buffer.alloc(4); size.writeUInt32BE(bytes.length);
    parts.push(size, bytes);
  }
  return crypto.createHmac('sha256', ticket).update(Buffer.concat(parts)).digest('base64url');
}

async function connect() {
  const discovery = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.workbuddy', 'wbipc', 'endpoint.json'), 'utf8'));
  if (!/^\\\\\.\\pipe\\wbipc-[a-z0-9]+$/i.test(discovery.endpoint)) throw new Error('Unexpected broker endpoint');
  const socket = net.connect(discovery.endpoint);
  socket.setEncoding('utf8');
  let buffer = '', nextId = 1;
  const pending = new Map();
  let waitFrame, rejectFrame;
  const inbox = [];
  const send = frame => socket.write(JSON.stringify(frame) + '\n');
  socket.on('data', chunk => {
    buffer += chunk;
    if (buffer.length > 2 * 1024 * 1024) { socket.destroy(new Error('Oversized broker frame')); return; }
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (!line.trim()) continue;
      let value;
      try { value = JSON.parse(line); } catch { socket.destroy(new Error('Invalid broker frame')); return; }
      if (value.id && pending.has(value.id)) {
        const p = pending.get(value.id); pending.delete(value.id); clearTimeout(p.timer);
        if (value.error) {
          const detail = String(value.error.data?.cause || value.error.message || '')
            .split(discovery.ticket).join('[redacted]').slice(0, 250);
          p.reject(new Error('Broker request failed: ' + value.error.code + ': ' + detail));
        }
        else p.resolve(value.result);
      } else if (waitFrame) {
        const resolve = waitFrame; waitFrame = rejectFrame = undefined; resolve(value);
      } else inbox.push(value);
    }
  });
  socket.on('error', error => {
    if (rejectFrame) rejectFrame(error);
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(error); }
    pending.clear();
  });
  socket.setTimeout(65000, () => socket.destroy(new Error('Broker timed out')));
  const receive = () => inbox.length ? Promise.resolve(inbox.shift()) :
    new Promise((resolve, reject) => { waitFrame = resolve; rejectFrame = reject; });
  const clientNonce = crypto.randomBytes(16).toString('base64url');
  send({type: 'session_hello', protocol_min: 1, protocol_max: 1, client_nonce: clientNonce,
    ticket_id: crypto.createHash('sha256').update(discovery.ticket).digest('hex').slice(0, 16),
    client: {kind: 'cli', id: 'forge-hy4-verification', version: '1'}});
  const challenge = await receive();
  const transcript = {endpoint: discovery.endpoint, clientNonce, serverNonce: challenge.server_nonce};
  const expected = proof(discovery.ticket, 'server', transcript);
  const received = String(challenge.server_proof || '');
  if (challenge.type !== 'session_challenge' || expected.length !== received.length ||
      !crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(received))) {
    socket.destroy(); throw new Error('WorkBuddy broker authentication failed');
  }
  send({type: 'session_prove', client_proof: proof(discovery.ticket, 'client', transcript)});
  const ready = await receive();
  if (ready.type !== 'session_hello_ack') { socket.destroy(); throw new Error('Broker handshake failed'); }
  const rpc = (method, params, mode) => new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Broker RPC timeout')); socket.destroy(); }, 60000);
    pending.set(id, {resolve, reject, timer});
    send({jsonrpc: '2.0', id, method, params, ...(mode ? {mode} : {})});
  });
  return {rpc, pipes: ready.pipes, close: () => socket.end()};
}

async function main() {
  const live = process.argv.includes('--live');
  if (live) {
    const root = path.join(os.homedir(), '.workbuddy', 'storage');
    let freeRoute = false;
    function inspect(value) {
      if (!value || typeof value !== 'object') return;
      if (value.id === 'hy4free' && value.credits === 'x0.00') freeRoute = true;
      for (const child of Object.values(value)) if (child && typeof child === 'object') inspect(child);
    }
    function scan(directory, depth) {
      if (depth > 5) return;
      for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
        const full = path.join(directory, entry.name);
        if (entry.isDirectory()) scan(full, depth + 1);
        else if (entry.name === 'wb-product-config.json') inspect(JSON.parse(fs.readFileSync(full, 'utf8')));
      }
    }
    scan(root, 0);
    if (!freeRoute) throw new Error('hy4free is not confirmed as x0.00 in the local WorkBuddy registry');
  }
  const broker = await connect();
  try {
    console.log(JSON.stringify({broker: 'authenticated', pipes: broker.pipes}));
    if (!live) return;
    const channel = await broker.rpc('broker/GetPipe', {pipe: 'wb.request'});
    const option = name => { const index = process.argv.indexOf(name); return index < 0 ? '' : process.argv[index + 1]; };
    const payloadFile = option('--payload'), outputFile = option('--output');
    const body = payloadFile ? JSON.parse(fs.readFileSync(payloadFile, 'utf8')) :
      {model: 'hy4free', messages: [{role: 'user', content: 'What is 17+25? Reply with the number only.'}],
      reasoning_effort: 'high', max_tokens: 512, stream: true, stream_options: {include_usage: true}};
    if (body.model !== 'hy4free' || body.reasoning_effort !== 'high' ||
        !Number.isInteger(body.max_tokens) || body.max_tokens < 1 || body.max_tokens > 1024) {
      throw new Error('Probe allows only hy4free / high / explicit output cap <= 1024');
    }
    body.stream = true;
    body.stream_options = {...body.stream_options, include_usage: true};
    const receipts = [];
    async function request(payload) {
      const result = await broker.rpc(channel.channel + '/http.fetch', {method: 'POST', path: '/v2/chat/completions',
        headers: {'content-type': 'application/json', accept: 'text/event-stream'},
        body_b64: Buffer.from(JSON.stringify(payload)).toString('base64')}, 'call');
      const text = Buffer.from(result.body_b64 || '', 'base64').toString('utf8');
      const frames = [];
      for (const line of text.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (!data || data === '[DONE]') continue;
        try { frames.push(JSON.parse(data)); } catch {}
      }
      const usage = Object.assign({}, ...frames.filter(f => f.usage).map(f => f.usage));
      const assistant = {role: 'assistant', content: ''}, tools = new Map();
      for (const frame of frames) for (const choice of frame.choices || []) {
        const delta = choice.delta || choice.message || {};
        if (typeof delta.content === 'string') assistant.content += delta.content;
        if (typeof delta.reasoning_content === 'string') assistant.reasoning_content =
          (assistant.reasoning_content || '') + delta.reasoning_content;
        for (const call of delta.tool_calls || []) {
          const index = call.index ?? 0;
          const current = tools.get(index) || {id: '', type: 'function', function: {name: '', arguments: ''}};
          if (call.id) current.id = call.id;
          if (call.function?.name) current.function.name += call.function.name;
          if (typeof call.function?.arguments === 'string') current.function.arguments += call.function.arguments;
          tools.set(index, current);
        }
      }
      if (tools.size) assistant.tool_calls = [...tools.values()];
      const receipt = {status: result.status, content_type: result.headers?.['content-type'],
        frame_count: frames.length, requested_model: payload.model,
        reasoning_effort: payload.reasoning_effort, max_tokens: payload.max_tokens,
        model: [...new Set(frames.map(f => f.model).filter(Boolean))],
        finish_reason: frames.flatMap(f => (f.choices || []).map(c => c.finish_reason).filter(Boolean)),
        usage, text: assistant.content.slice(0, 300), tool_names: [...tools.values()].map(c => c.function.name)};
      if (result.status >= 400) {
        try { const error = JSON.parse(text); receipt.error = {code: error.code, message: error.msg}; }
        catch { receipt.error = 'Non-JSON upstream error'; }
      }
      receipts.push(receipt);
      console.log(JSON.stringify(receipt));
      if (result.status === 200 && (usage.credit !== 0 || receipt.model.length !== 1 ||
          receipt.model[0] !== 'hy4-free')) throw new Error('Free hy4 route was not confirmed by the response');
      return {receipt, assistant};
    }
    let ok = false;
    try {
      const first = await request(body);
      if (process.argv.includes('--tool-roundtrip')) {
        const calls = first.assistant.tool_calls || [];
        if (first.receipt.status !== 200 || calls.length !== 1 || calls[0].function.name !== 'add')
          throw new Error('Expected one add tool call');
        const args = JSON.parse(calls[0].function.arguments);
        if (args.a !== 17 || args.b !== 25) throw new Error('Unexpected tool arguments');
        const next = {...body, messages: [...body.messages, first.assistant,
          {role: 'tool', tool_call_id: calls[0].id, content: '42'}]};
        delete next.tool_choice;
        const second = await request(next);
        ok = second.receipt.status === 200 && second.assistant.content.trim() === '42';
      } else ok = first.receipt.status === 200 && first.assistant.content.trim() === '42';
      ok = ok && receipts.every(r => r.model.length === 1 && r.model[0] === 'hy4-free' &&
        r.usage.credit === 0 && !r.finish_reason.some(f => ['length', 'max_tokens'].includes(f)));
    } finally {
      if (outputFile) {
        fs.mkdirSync(path.dirname(outputFile), {recursive: true});
        fs.writeFileSync(outputFile, JSON.stringify({mode: 'workbuddy-broker-live', ok,
          requests: receipts}, null, 2) + '\n');
      }
    }
    if (!ok) process.exitCode = 1;
  } finally { broker.close(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
