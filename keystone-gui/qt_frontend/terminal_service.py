"""Project terminal output for the desktop UI. Commands originate in the Code panel."""
import os
from pathlib import Path
import threading

from keystone.development.testing import run_process
from keystone.policy import Decision, ToolEffect
from keystone.secrets import redact


def shell_command(command):
    if os.name != 'nt':
        return ['/bin/sh', '-c', command]
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    # Windows PowerShell defaults to the OEM code page when stdout is redirected.
    script = ("[Console]::OutputEncoding = $OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
              + command + "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }")
    return [str(shell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script]


def coding_output(event):
    kind = event.get('type')
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


class TerminalService:
    def __init__(self, host):
        self.host = host
        self.root = host.workspace
        self.worker = None
        self.cancel = threading.Event()

    def publish(self, text, **state):
        self.host.emit('terminal', root=str(self.root), text=text, **state)

    def start(self, command, *, git_status=False):
        if self.host.closed.is_set() or self.worker and self.worker.is_alive():
            return
        command = 'git status --short --branch' if git_status else command.strip()
        if not command:
            return
        decision = self.host.policy().evaluate('git_status' if git_status else 'shell_exec',
            args={} if git_status else {'command': command}, effect=ToolEffect.READ if git_status else ToolEffect.NATIVE)
        if decision == Decision.DENY or (not git_status and (self.host.profile == 'sandbox' or self.host.demo)):
            self.publish('[当前权限不允许执行此命令]\n', running=False)
            return
        self.cancel.clear()
        self.publish('\n› ' + redact(command) + '\n', running=True)

        def run():
            try:
                if decision == Decision.ASK and not self.host.approve('运行终端命令',
                        {'command': command, 'workspace': str(self.root)}, self.cancel, independent=True):
                    self.publish('[未执行]\n')
                    return
                argv = (['git', '--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                         'status', '--short', '--branch', '--ignore-submodules=all']
                        if git_status else shell_command(command))
                result = run_process(argv, self.root, self.cancel, timeout=600, on_output=self.publish)
                if result['output_truncated']:
                    self.publish('\n[命令输出已截断]\n')
                state = {'cancelled': '已停止', 'timed_out': '已超时', 'unknown': '输出未完整返回'}.get(result['status'])
                self.publish('\n[' + (state or '退出码 ' + str(result['exit_code'])) + ']\n')
            except Exception as exc:
                self.publish('\n[终端错误] ' + str(redact(str(exc))) + '\n')
            finally:
                self.publish('', running=False)
        self.worker = threading.Thread(target=run, name='Keystone-project-terminal', daemon=True)
        self.worker.start()

    def close(self):
        self.cancel.set()
        if self.worker:
            self.worker.join(timeout=6)
