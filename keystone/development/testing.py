"""Explicitly approved host tests, with bounded output and real process status."""
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from keystone.secrets import redact


def _stop(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def execute(argv, workspace, home, cancelled, *, timeout=120, task_id='', run_id=''):
    result = run_process(argv, workspace, cancelled, timeout=timeout)
    return _persist(argv, workspace, home, task_id, run_id, result)


def run_process(argv, workspace, cancelled, *, timeout=120, on_output=None):
    """Run an approved, non-interactive command with bounded, merged output."""
    if (not isinstance(argv, (list, tuple)) or not argv or
            any(not isinstance(x, str) or '\0' in x for x in argv) or not 0 < timeout <= 600):
        raise ValueError('Use an argument list and a timeout of at most 600 seconds')
    if cancelled.is_set():
        return {'status': 'cancelled', 'exit_code': None, 'output': '', 'output_truncated': False,
                'passed': None, 'failed': None, 'execution_state': 'not_started'}
    environment = {k: v for k, v in os.environ.items()
                   if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'LANG'}}
    environment['PYTHONIOENCODING'] = 'utf-8'
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(argv, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **options)
    chunks, size, truncated = [], 0, False
    def drain():
        nonlocal size, truncated
        pending = bytearray()
        try:
            while raw := process.stdout.read1(16384):
                room = max(0, 1024 * 1024 - size)
                if room:
                    chunk = raw[:room]
                    chunks.append(chunk)
                    if on_output:
                        pending.extend(chunk)
                        end = pending.rfind(b'\n') + 1
                        if end:
                            on_output(redact(pending[:end].decode('utf-8', 'replace')))
                            del pending[:end]
                size += min(len(raw), room)
                truncated |= len(raw) > room
            if pending and not truncated:
                on_output(redact(pending.decode('utf-8', 'replace')))
        finally:
            process.stdout.close()
    reader = threading.Thread(target=drain, daemon=True, name='keystone-test-output')
    reader.start()
    deadline = time.monotonic() + timeout
    status = 'completed'
    try:
        while process.poll() is None:
            if cancelled.wait(.05):
                status = 'cancelled'
                break
            if time.monotonic() >= deadline:
                status = 'timed_out'
                break
        if status != 'completed':
            _stop(process)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            _stop(process)
        reader.join(timeout=5)
    if reader.is_alive():
        # A descendant retained the output handle. Terminate the owned tree.
        _stop(process)
        reader.join(timeout=5)
        truncated = True
        if status == 'completed':
            status = 'unknown'  # Exit 0 does not establish a complete test receipt.
    output = redact(b''.join(chunks).decode('utf-8', 'replace'))
    if status == 'completed' and process.returncode != 0:
        status = 'failed'
    return {'status': status, 'exit_code': process.returncode, 'output': output,
            'output_truncated': truncated, 'passed': None, 'failed': None,
            'execution_state': 'unknown' if status == 'unknown' else 'completed'}


def _persist(argv, workspace, home, task_id, run_id, result):
    folder = Path(home) / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    artifact = folder / ('test-' + uuid.uuid4().hex + '.json')
    artifact.write_text(json.dumps(redact({'task_id': task_id, 'run_id': run_id,
                        'argv': list(argv), 'workspace': str(workspace), **result}),
                        ensure_ascii=False, indent=2), encoding='utf-8')
    return {**result, 'artifact': str(artifact)}
