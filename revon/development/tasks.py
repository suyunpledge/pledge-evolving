"""Execute a planned run/build/test task through the permission policy.

Every step is evaluated as ``shell_exec`` with a native effect, exactly like a
command typed into the Code terminal: ``DENY`` stops the task, ``ASK`` is put
to the human once for the whole task (all commands listed), and only then do
the steps run, sequentially, without a shell. Output is bounded and redacted
by :func:`revon.development.testing.run_process`; problems are parsed with
the task's matchers.
"""
from concurrent.futures import CancelledError
import os
from pathlib import Path
import shlex
import subprocess
import time

from revon.policy import Decision, ToolEffect
from revon.secrets import redact
from . import problems as problem_matchers
from .testing import decode_output, run_process
from .toolchains import check_argv


def command_line(argv):
    return subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(map(str, argv))


def _environment(task):
    extra = {name: os.environ[name] for name in task.get('env', ()) if name in os.environ}
    # Put each step's tool folder first so wrappers (gradlew, npm scripts) find
    # the same java/node the task was planned with.
    folders = []
    for step in task['steps']:
        parent = str(Path(step[0]).parent)
        if Path(step[0]).is_absolute() and parent not in folders:
            folders.append(parent)
    if folders:
        extra['PATH'] = os.pathsep.join([*folders, os.environ.get('PATH', '')])
    return extra


def _check_cancelled(event):
    if event is not None and event.is_set():
        raise CancelledError()


def authorize(task, policy, approve, *, workspace, cancel=None):
    """Policy first, then one human approval when any step needs it. Returns the decisions."""
    decisions = []
    for argv in task['steps']:
        check_argv(argv)
        decision = policy.resolve_ask(policy.evaluate(
            'shell_exec', args={'command': command_line(argv)}, effect=ToolEffect.NATIVE))
        if decision == Decision.DENY:
            raise PermissionError('当前权限策略不允许执行：' + command_line(argv))
        decisions.append(decision)
    if any(d == Decision.ASK for d in decisions):
        detail = {'task': task['label'], 'commands': [command_line(a) for a in task['steps']],
                  'cwd': task['cwd'], 'workspace': str(workspace),
                  'note': '将以当前账户执行项目代码，具有文件和网络访问能力。'}
        _check_cancelled(cancel)
        approved = approve('运行项目任务：' + task['label'], detail, cancel)
        _check_cancelled(cancel)
        if not approved:
            raise PermissionError('未批准运行：' + task['label'])
    return decisions


def execute(task, *, workspace, policy, approve, cancelled, on_output=None, timeout=600):
    """Authorize and run ``task``; return status, per-step results and parsed problems."""
    workspace = Path(workspace).resolve()
    cwd = Path(task['cwd']).resolve()
    emit = on_output or (lambda text: None)
    started = time.monotonic()
    steps, outputs, status, exit_code = [], [], 'completed', 0
    try:
        _check_cancelled(cancelled)
        if not cwd.is_dir():
            raise FileNotFoundError(f'工作目录不存在：{cwd}')
        authorize(task, policy, approve, workspace=workspace, cancel=cancelled)
        started = time.monotonic()  # Report execution time separately from human approval.
        for folder in task.get('prepare', ()):
            _check_cancelled(cancelled)
            target = Path(folder).resolve()
            target.relative_to(workspace)  # outputs stay inside the project
            target.mkdir(parents=True, exist_ok=True)
        environment = _environment(task)
        emit(f"\n> {task['label']}\n")
        for argv in task['steps']:
            _check_cancelled(cancelled)
            emit(f"$ {command_line(argv)}\n")
            result = run_process(argv, cwd, cancelled, timeout=timeout, on_output=emit,
                                 extra_env=environment, decode=decode_output)
            status, exit_code = result['status'], result['exit_code']
            if result['execution_state'] != 'not_started':
                outputs.append(result['output'])
                steps.append({'command': command_line(argv), 'status': status, 'exit_code': exit_code,
                              'output_truncated': result['output_truncated']})
            if status != 'completed':
                break
    except CancelledError:
        status, exit_code = 'cancelled', None
    output = '\n'.join(outputs)
    found = problem_matchers.parse(output, task.get('matchers', ()), cwd=cwd, workspace=workspace)
    counts = problem_matchers.summary(found)
    elapsed = time.monotonic() - started
    emit(f"[{ {'completed': '完成', 'failed': '失败', 'cancelled': '已取消', 'timed_out': '超时'}.get(status, status) }"
         f"{' · 退出码 ' + str(exit_code) if exit_code is not None else ''} · {elapsed:.1f}s"
         f" · {counts['errors']} 个错误 {counts['warnings']} 个警告]\n")
    return redact({'task': {k: task[k] for k in ('id', 'label', 'language', 'action', 'scope', 'path')},
                   'status': status, 'exit_code': exit_code, 'steps': steps, 'problems': found,
                   'counts': counts, 'elapsed': round(elapsed, 2)})
