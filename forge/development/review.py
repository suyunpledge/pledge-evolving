"""Scoped checkpoint review/restoration through the existing ToolRegistry gates."""
import hashlib
import re
import subprocess
import uuid

from forge.secrets import assert_public_path, redact, read_public_bytes
from .editor import ProjectBoundary


def checkpoint_preview(agent, commit, value):
    path = ProjectBoundary(agent.workspace).path(value)
    if not agent.policy.allows_read(path):
        raise PermissionError('Checkpoint review is outside the granted read scope')
    assert_public_path(path)
    store = agent.checkpoints
    if store is None or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise ValueError('Select a valid existing checkpoint')
    if commit not in {point.commit for point in store.history(limit=100)}:
        raise PermissionError('Checkpoint does not belong to this project history')
    relative = path.relative_to(agent.workspace.resolve()).as_posix()
    obj = commit + ':' + relative
    size = store._git('cat-file', '-s', obj, check=False)
    if size.returncode or not size.stdout.strip().isdigit() or int(size.stdout.strip()) > 512 * 1024:
        raise ValueError('Checkpoint file is absent or exceeds the 512 KiB restore limit')
    proc = subprocess.run(['git', '--no-pager', f'--git-dir={store.git_dir}', 'cat-file', 'blob', obj],
        capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode or len(proc.stdout) > 512 * 1024:
        raise ValueError('Cannot read checkpoint file')
    raw = read_public_bytes(path, limit=512 * 1024) if path.exists() else None
    for content in (proc.stdout, raw):
        if content is not None and (b'\0' in content or content.startswith(b'\xef\xbb\xbf')
                                   or b'\r' in content.replace(b'\r\n', b'')):
            raise ValueError('Scoped restore supports UTF-8 LF/CRLF text; use the editor for other encodings')
    # The existing write_file uses native text newlines. Normalize before invoking it
    # so Windows never writes CRCRLF. Do not introduce a second write implementation.
    restored = proc.stdout.decode('utf-8', 'strict').replace('\r\n', '\n')
    current = raw.decode('utf-8', 'strict') if raw is not None else ''
    # Parse the whole configuration before making a diff: escaped JSON field
    # names cannot be detected reliably in the already-prefixed diff text.
    safe_current = agent.secret_scope.document_text(path, current) if raw is not None else ''
    safe_restored = agent.secret_scope.document_text(path, restored)
    import difflib
    diff = ''.join(difflib.unified_diff(safe_current.splitlines(True), safe_restored.splitlines(True),
                    fromfile='current-disk', tofile='checkpoint-' + commit[:8]))
    return {'path': str(path), 'commit': commit, 'expected': hashlib.sha256(raw).hexdigest() if raw is not None else None,
            'diff': redact(diff), 'content': restored}


def apply_checkpoint(agent, commit, path, expected, *, publish):
    preview = checkpoint_preview(agent, commit, path)
    if expected != preview['expected']:
        raise ValueError('File changed since review; regenerate the checkpoint preview')
    # Match Agent's existing pre-write checkpoint behavior, so an authorized
    # restoration can itself be reviewed/reversed. Never run shadow checkout.
    backup = agent.checkpoints.snapshot('before checkpoint restore')
    if backup is None:
        raise RuntimeError('Cannot protect current work with a pre-restore checkpoint')
    publish({'type': 'checkpoint', 'commit': backup.commit, 'label': backup.label,
             'agent': 'human-review', 'depth': 0, 'source': 'checkpoint_restore'})
    identifier = uuid.uuid4().hex
    target = preview['path']
    # Normal invoke enforces Policy, Secret isolation, config validation and revision guards.
    args = {'path': target, 'content': preview['content'], '_expected_revisions': {target: expected}}
    publish({'type': 'tool_started', 'run_id': identifier, 'action_id': identifier, 'agent': 'human-review',
             'depth': 0, 'tool': 'write_file', 'source': 'checkpoint_restore'})
    agent.session.sync()
    result = agent.registry.invoke('write_file', args, agent._tool_context())
    publish({'type': 'tool_completed', 'run_id': identifier, 'action_id': identifier, 'agent': 'human-review',
             'depth': 0, 'tool': 'write_file', 'ok': result.ok})
    publish({'type': 'tool_call', 'run_id': identifier, 'agent': 'human-review', 'depth': 0,
             'tool': 'write_file', 'args': {'path': target}, 'ok': result.ok,
             'authorization': (result.meta or {}).get('authorization'),
             'result': result.content or result.error, 'source': 'checkpoint_restore', 'checkpoint': commit})
    return result
