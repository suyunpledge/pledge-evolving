"""Project terminal output for the desktop UI. Commands originate in the Code panel."""
from queue import Empty, Full, Queue
import threading
import time

from revon.development.testing import run_process
from revon.policy import Decision, ToolEffect
from revon.secrets import redact


def coding_output(event):
    kind = event.get('type')
    if kind == 'planning_attempt':
        phase = {'json': '首次规划', 'json_repair': 'JSON 格式重试', 'lines': '逐行规划恢复'}[event['format']]
        status = {'accepted': '已采用', 'invalid_json': 'JSON 无效', 'invalid_format': '字段无效',
                  'original_task': '清单无法解析，已按用户原任务继续',
                  'failed': '未完成'}[event['status']]
        return f"[{phase}] {event['elapsed_ms'] / 1000:.2f}s · {status}\n"
    if kind == 'model_request_timing':
        header = event.get('headers_ms')
        first = f"响应头 {header / 1000:.1f}s" if header is not None else "未收到响应头"
        return f"[{event['model']}] {first} · 请求 {event['elapsed_ms'] / 1000:.1f}s · {event['status']}\n"
    if kind == 'model_retry':
        return f"[模型重试] 第 {event['step']} 步，等待 {event['wait_seconds']:g}s 后重试\n"
    if kind == 'test_started':
        return f"\n[项目测试] {event.get('command', '')}\n"
    if kind == 'test_result':
        output = event.get('output') or event.get('error') or ''
        status = event.get('status', '')
        code = event.get('exit_code')
        return f"{output}\n[{status}{' · 退出码 ' + str(code) if code is not None else ''}]\n"
    if kind == 'tool_completed' and event.get('tool') in {'shell_exec', 'run_tests'}:
        command = (event.get('args') or {}).get('command') or event['tool']
        result = event.get('action_result') or {}
        output = result.get('content') or result.get('error') or event.get('result', '')
        state = '完成' if event.get('ok') else '失败'
        code = (event.get('meta') or {}).get('exit')
        return f"\n[Agent] {command}\n{output}\n[{state}{' · 退出码 ' + str(code) if code is not None else ''}]\n"
    return ''


class TerminalSession:
    def __init__(self, service, identifier, size):
        self.service, self.id = service, identifier
        self.size = size
        self.applied_size = None
        self.queue = Queue(maxsize=32)
        self.closed = threading.Event()
        self.interrupt = threading.Event()
        self.pty = None
        self.worker = threading.Thread(target=self.loop, name=f'revon-terminal-{identifier}', daemon=True)
        self.worker.start()

    def publish(self, text='', **state):
        self.service.publish(text, session=self.id, **state)

    def submit(self, action, text):
        try:
            self.queue.put_nowait((action, text))
        except Full:
            self.publish('[输入队列已满，请稍后重试]\n')

    def frame(self):
        history, screen = self.pty.screen.frame()
        self.publish(redact(history), screen=redact(screen))

    def input(self, action, text):
        host = self.service.host
        git = action == 'git'
        decision = host.policy().evaluate('git_status' if git else 'shell_exec',
            args={} if git else {'command': text}, effect=ToolEffect.READ if git else ToolEffect.NATIVE)
        if decision == Decision.DENY or (not git and (host.profile == 'sandbox' or host.demo)):
            self.publish('[当前权限不允许执行此命令]\n')
            return
        if decision == Decision.ASK and not host.approve('运行终端命令',
                {'command': 'git status --short --branch' if git else text, 'workspace': str(self.service.root),
                 'terminal': self.id}, self.interrupt, independent=True):
            self.publish('[未执行]\n')
            return
        if self.closed.is_set():
            return
        if git:
            self.publish('› git status --short --branch\n', running=True)
            result = run_process(['git', '--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                                  'status', '--short', '--branch', '--ignore-submodules=all'],
                                 self.service.root, self.interrupt, timeout=60, on_output=self.publish)
            self.publish('[退出码 ' + str(result['exit_code']) + ']\n' if result['status'] == 'completed'
                         else '[' + result['status'] + ']\n', running=self.pty is not None)
        else:
            if self.pty is None:
                from .terminal_pty import ProjectPty
                self.pty = ProjectPty(self.service.root, self.size)
                self.applied_size = self.size
                self.publish(running=True)
            self.pty.write(text + '\r')

    def finish(self, message):
        if self.pty:
            try:
                self.pty.close()
            finally:
                self.pty = None
        self.publish(message, ended=True, running=False)

    def send_interrupt(self):
        self.interrupt.clear()
        if not self.pty:
            return
        self.pty.write('\x03')
        # Windows flushes queued input during Ctrl+C dispatch. Give the foreground
        # program a chance to redraw before delivering the next submitted line.
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline and not self.closed.wait(.025):
            if self.pty.read():
                self.frame()
                return

    def update_screen(self):
        size = self.size
        changed = size != self.applied_size
        if changed:
            self.pty.resize(*size)
            self.applied_size = size
        for _ in range(32):
            if not self.pty.read():
                break
            changed = True
        else:
            # More output may remain after the process exits.
            self.frame()
            return
        if changed:
            self.frame()
        code = self.pty.exit_code()
        if code is not None:
            self.finish(f'\n[退出码 {code}]\n')

    def loop(self):
        try:
            while not self.closed.is_set():
                try:
                    action, text = self.queue.get(timeout=.025 if self.pty else .12)
                except Empty:
                    action = ''
                if self.interrupt.is_set() and not self.closed.is_set():
                    self.send_interrupt()
                if self.closed.is_set():
                    break
                if action == 'clear':
                    if self.pty:
                        self.pty.screen.reset()
                        self.pty.screen.scrolled.clear()
                    self.publish(cleared=True)
                elif action in {'run', 'git'}:
                    self.input(action, text)
                if self.pty:
                    self.update_screen()
        except Exception as exc:
            self.publish('[终端错误] ' + redact(str(exc)) + '\n')
        finally:
            if self.pty:
                self.finish('[已停止]\n')

    def stop(self):
        self.closed.set()
        self.interrupt.set()


class TerminalService:
    def __init__(self, host):
        self.host = host
        self.root = host.workspace
        self.sessions = {}

    def publish(self, text, **state):
        self.host.emit('terminal', root=str(self.root), text=text, **state)

    def handle(self, row):
        identifier = row.get('session')
        if type(identifier) is not int or identifier < 1 or self.host.closed.is_set():
            return
        action = row.get('action')
        session = self.sessions.get(identifier)
        if action == 'close':
            if session:
                session.stop()
                session.worker.join()
                del self.sessions[identifier]
            return
        if action == 'interrupt':
            if session:
                session.interrupt.set()
            return
        if action == 'clear':
            if session:
                session.submit(action, '')
            return
        size = row.get('size', [100, 24])
        if not (isinstance(size, list) and len(size) == 2 and all(type(x) is int for x in size)):
            return
        size = max(20, min(size[0], 320)), max(3, min(size[1], 100))
        if session:
            session.size = size
        if action not in {'run', 'git'}:
            return
        text = row.get('text', '')
        if not isinstance(text, str) or len(text) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            self.publish('[请输入单行文本]\n', session=identifier)
            return
        if not session or not session.worker.is_alive():
            if not session and len(self.sessions) >= 16:
                self.publish('[终端数量已达上限]\n', session=identifier)
                return
            session = self.sessions[identifier] = TerminalSession(self, identifier, size)
        session.submit(action, text)

    def close(self):
        for session in self.sessions.values():
            session.stop()
        for session in self.sessions.values():
            session.worker.join()
        self.sessions.clear()
