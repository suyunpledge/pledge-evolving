"""Plan a project verification before approval and execute exactly those argv vectors."""
import re
import threading
import time
from pathlib import Path

from . import toolchains
from .tasks import command_line, _environment
from .testing import run_process, decode_output

DESCRIPTION = "Run a detected project test, lint or build task. Each invocation requires human approval. No shell command input."
SCHEMA = {
    'task': {'type': 'string', 'description': 'test, lint, build, or a detected project task ID such as python.test.'},
    'filter': {'type': 'string', 'description': 'Optional Python test name substring, e.g. test_parser.'},
}
TIMEOUT = 300


def plan(workspace, arguments, policy):
    if set(arguments) - SCHEMA.keys() or not isinstance(arguments.get('task'), str):
        raise ValueError('Expected task and an optional Python test name filter.')
    tasks, notes = toolchains.project_tasks(toolchains.Toolchains(workspace))
    tasks = [t for t in tasks if t['action'] in {'test', 'lint', 'build'}]
    name = arguments['task']
    task = next((t for t in tasks if t['id'] == name), None) or toolchains.choose(tasks, name)
    if task is None:
        raise ValueError('No such verification task. Available: ' + ', '.join(t['id'] for t in tasks) +
                         ('. ' + ' '.join(map(str, notes)) if notes else ''))
    if 'filter' in arguments:
        text = arguments['filter']
        if task['id'] != 'python.test' or not isinstance(text, str) or not re.fullmatch(r'[\w.]{1,100}', text):
            raise ValueError('filter must be a Python test name (letters, digits, underscores or dots).')
        task['steps'][0].extend(['-k', text])
    commands = [command_line(toolchains.check_argv(step)) for step in task['steps']]
    if policy._matches(policy.deny, 'shell_exec') or any(policy._command_denied(cmd) for cmd in commands):
        raise PermissionError('The host policy denies this project command.')
    detail = dict(task=task['id'], commands=commands, cwd=str(workspace), timeout_seconds=TIMEOUT,
                  note='测试进程可读取当前账户能访问的文件，包括工作区外的文件。已登记的密钥不会通过环境变量传入。')
    return task, detail


def run(task, ctx):
    from ..tools import ToolResult
    if task is None:
        return ToolResult(False, error='需要用户逐次审批。', meta={'execution_state': 'not_started'})
    stop = ctx.cancellation or threading.Event()
    deadline = time.monotonic() + TIMEOUT
    outputs = []
    for folder in task['prepare']:
        target = Path(folder).resolve()
        target.relative_to(ctx.workspace.resolve())
        target.mkdir(parents=True, exist_ok=True)
    for argv in task['steps']:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ToolResult(False, error='Project check timed out.', meta={'status': 'timed_out'})
        result = run_process(argv, ctx.workspace, stop, timeout=remaining, extra_env=_environment(task),
                             decode=decode_output, protect_environment=True)
        outputs.append(result['output'])
        if result['status'] != 'completed':
            break
    body = ctx.secret_scope.protect_text('\n'.join(outputs))
    meta = {k: result[k] for k in ('status', 'exit_code', 'execution_state', 'output_truncated')}
    from ..builtin_views import command_metadata
    meta['task'] = task['id']
    if result['status'] in ('completed', 'failed') and result['exit_code'] is not None:
        meta.update(command_metadata(body, result['exit_code']))
    return ToolResult(result['status'] == 'completed', content=body or f"{task['id']} · {result['status']} · exit {result['exit_code']}", meta=meta)


def attach_console(agent):
    """Expose only per-invocation project-check approvals on a real command terminal."""
    import sys
    if not (sys.stdin and sys.stdin.isatty() and sys.stderr and sys.stderr.isatty()):
        return False

    def approve(name, details, cancel):
        if name != 'run_check' or (cancel and cancel.is_set()):
            return False
        commands = '\n'.join(details['commands'])
        print(f"{commands}\n{details['cwd']} · {details['timeout_seconds']} s\n{details['note']}", file=sys.stderr)
        print("批准本次项目验证？[y/N] ", end='', file=sys.stderr, flush=True)
        try:
            return input().strip().casefold() == 'y' and not (cancel and cancel.is_set())
        except (EOFError, OSError):
            return False

    agent.action_executor.approver = approve
    agent.interactive_checks = True
    return True
