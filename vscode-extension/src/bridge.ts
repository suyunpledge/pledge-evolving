import { ChildProcessWithoutNullStreams, spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { StringDecoder } from 'node:string_decoder';

export type Turn = { role: 'user' | 'assistant'; content: string };
export type Context = { path: string; content: string; selection?: [number, number] };
export type Ready = { protocol: number; models: { id: string; label: string }[]; workspace: string; sessionPath: string; ready: boolean };
export type Result = { ok: boolean; text: string; stopped: string; toolCalls: number; usage: Record<string, unknown>; sessionPath: string };
export type BridgeEvent = { event: 'user'; request: string; content: string; display: string }
  | { event: 'progress'; request: string; data: Record<string, unknown> };

/** JSONL framing is independent of pipe chunks and split UTF-8 characters. */
export class FrameReader {
  private decoder = new StringDecoder('utf8');
  private buffer = '';
  constructor(private emit: (frame: Record<string, unknown>) => void, private limit = 2 * 1024 * 1024) {}
  feed(chunk: Buffer): void {
    this.buffer += this.decoder.write(chunk);
    let newline: number;
    while ((newline = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, newline);
      if (Buffer.byteLength(line) > this.limit) { throw new Error('Forge response exceeds the protocol limit'); }
      this.buffer = this.buffer.slice(newline + 1);
      if (!line.trim()) { continue; }
      const value: unknown = JSON.parse(line);
      if (!value || typeof value !== 'object' || Array.isArray(value)) { throw new Error('Invalid Forge response'); }
      this.emit(value as Record<string, unknown>);
    }
    if (Buffer.byteLength(this.buffer) > this.limit) { throw new Error('Forge response exceeds the protocol limit'); }
  }
}

type Pending = { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout };
export class ForgeBridge extends EventEmitter {
  private child?: ChildProcessWithoutNullStreams;
  private pending = new Map<string, Pending>();
  private closing = false;

  constructor(private launch: typeof spawn = spawn) { super(); }

  start(options: { python: string; launcher: string; engine: string; cwd: string }): void {
    if (this.child) { return; }
    this.closing = false;
    const args = ['-I', '-u', options.launcher];
    if (options.engine) { args.push('--engine-root', options.engine); }
    const child = this.launch(options.python, args, { cwd: options.cwd, shell: false, windowsHide: true, stdio: 'pipe' }) as ChildProcessWithoutNullStreams;
    this.child = child;
    const frames = new FrameReader(value => this.frame(value));
    child.stdout.on('data', chunk => {
      try { frames.feed(Buffer.from(chunk)); }
      catch { this.stop('Forge returned an invalid or oversized response. Reconnect the engine.'); }
    });
    // Raw diagnostics are never forwarded to the webview or persisted. The
    // structured response contains the trusted layer's sanitized error.
    child.stderr.on('data', () => {});
    child.on('error', () => this.stop('Cannot start Forge. Check the Python executable and Forge source directory.'));
    child.on('exit', () => {
      if (this.child !== child) { return; }
      this.child = undefined;
      this.rejectAll(new Error('Forge engine exited. Check configuration and reconnect.'));
      this.emit('closed');
    });
    child.stdin.on('error', () => this.stop('Forge engine disconnected.'));
  }

  request<T>(method: 'initialize' | 'run' | 'set_credential' | 'python_path_check', params: Record<string, unknown>, timeout: number): Promise<T> {
    const child = this.child;
    if (!child || this.closing) { return Promise.reject(new Error('Forge is not connected.')); }
    const id = randomBytes(16).toString('hex');
    const frame = JSON.stringify({ id, method, params }) + '\n';
    if (Buffer.byteLength(frame) > 2 * 1024 * 1024) { return Promise.reject(new Error('Editor context is too large.')); }
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => this.stop('Forge task timed out; execution was terminated.'), timeout);
      this.pending.set(id, { resolve: value => resolve(value as T), reject, timer });
      child.stdin.write(frame, error => { if (error) { this.stop('Cannot send a request to Forge.'); } });
    });
  }

  private frame(frame: Record<string, unknown>): void {
    if (frame.event === 'user' || frame.event === 'progress') {
      if (typeof frame.request === 'string' && this.pending.has(frame.request)) { this.emit('event', frame); }
      return;
    }
    const pending = typeof frame.id === 'string' ? this.pending.get(frame.id) : undefined;
    if (!pending) { return; }
    clearTimeout(pending.timer);
    this.pending.delete(frame.id as string);
    if (typeof frame.error === 'string') { pending.reject(new Error(frame.error)); }
    else { pending.resolve(frame.result); }
  }

  private rejectAll(error: Error): void {
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
    this.pending.clear();
  }

  stop(reason = 'Execution stopped by the user.'): void {
    const child = this.child;
    this.child = undefined;
    this.closing = true;
    this.rejectAll(new Error(reason));
    // SIGKILL terminates the process and all its Python threads, including a
    // blocked HTTP request. No timeout merely abandons a running promise.
    if (child && !child.killed) { child.kill('SIGKILL'); }
    this.emit('closed');
  }
  dispose(): void { this.stop(); this.removeAllListeners(); }
}
