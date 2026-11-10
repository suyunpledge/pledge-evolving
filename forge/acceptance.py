"""Host-defined executable acceptance, inspired by Loom's independent verifier.

No model/tool can install or weaken checks. File checks use the read permission
boundary; commands use the ordinary recorded action executor, never a new shell.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path, PureWindowsPath

from .secrets import read_public_bytes


_KIND_FIELDS = {
    'file': {'path', 'exists', 'contains'},
    'command': {'command', 'expect_exit', 'timeout'},
    'env_probe': {'env', 'probe', 'input', 'expect', 'timeout'},
}


@dataclass(frozen=True)
class Criterion:
    id: str
    kind: str
    required: bool = True
    path: str = ""
    exists: bool = True
    contains: str | None = None
    command: str = ""
    expect_exit: int = 0
    timeout: int = 30
    env: str = ""
    probe: str = ""
    input: dict = field(default_factory=dict)
    expect: object = None

    @classmethod
    def parse(cls, row):
        if not isinstance(row, dict) or row.get('kind') not in {'file', 'command', 'env_probe'}:
            raise ValueError('Acceptance criterion kind must be file, command or env_probe')
        allowed = {'id', 'kind', 'required'} | _KIND_FIELDS[row['kind']]
        if set(row) - allowed:
            raise ValueError('Unknown acceptance criterion fields')
        try:
            value = cls(**row)
        except TypeError as exc:
            raise ValueError('Acceptance criterion requires id and kind') from exc
        if not isinstance(value.id, str) or not 1 <= len(value.id) <= 80:
            raise ValueError('Criterion id must be 1–80 characters')
        if type(value.required) is not bool or type(value.exists) is not bool:
            raise ValueError('required and exists must be booleans')
        if value.kind == 'file':
            if (not isinstance(value.path, str) or not value.path or len(value.path) > 1024
                    or '\0' in value.path or Path(value.path).is_absolute()
                    or PureWindowsPath(value.path).drive
                    or '..' in value.path.replace('\\', '/').split('/')):
                raise ValueError('Acceptance file path must stay relative to the workspace')
            if value.contains is not None and (not isinstance(value.contains, str)
                                               or len(value.contains) > 4000 or not value.exists):
                raise ValueError('contains requires exists=true and at most 4000 characters')
        elif value.kind == 'env_probe':
            from .probes import identifier, json_value
            identifier(value.env)
            identifier(value.probe)
            if 'expect' not in row or type(value.input) is not dict:
                raise ValueError('env_probe requires expect and object input')
            if type(value.timeout) is not int or not 1 <= value.timeout <= 60:
                raise ValueError('Probe timeout must be 1–60 seconds')
            value = cls(**{**row, 'input': json_value(value.input), 'expect': json_value(value.expect)})
        elif (not isinstance(value.command, str) or not value.command.strip()
              or len(value.command) > 4000 or type(value.expect_exit) is not int
              or type(value.timeout) is not int or not 1 <= value.timeout <= 60):
            raise ValueError('Command criterion needs a command, integer exit code and 1–60s timeout')
        return value


@dataclass(frozen=True)
class AcceptanceContract:
    criteria: tuple[Criterion, ...]
    max_attempts: int = 3

    @classmethod
    def from_raw(cls, raw):
        if isinstance(raw, cls):
            raw = raw.to_raw()
        if not isinstance(raw, dict) or set(raw) - {'criteria', 'max_attempts'}:
            raise ValueError('Acceptance contract requires criteria and optional max_attempts')
        rows = raw.get('criteria')
        attempts = raw.get('max_attempts', 3)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
            raise ValueError('Acceptance contract requires 1–16 criteria')
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError('max_attempts must be 1–3')
        criteria = tuple(Criterion.parse(row) for row in rows)
        if len({c.id for c in criteria}) != len(criteria):
            raise ValueError('Acceptance criterion ids must be unique')
        if not any(c.required for c in criteria):
            raise ValueError('Acceptance contract needs a required criterion')
        return cls(criteria, attempts)

    def to_raw(self):
        rows = []
        for criterion in self.criteria:
            keys = {'id', 'kind', 'required'} | _KIND_FIELDS[criterion.kind]
            rows.append({k: v for k, v in asdict(criterion).items() if k in keys})
        return {'criteria': rows, 'max_attempts': self.max_attempts}

    def projection(self):
        return '[Host acceptance checks; only the verifier can mark these passed]\n' + json.dumps(
            self.to_raw(), ensure_ascii=False, sort_keys=True)


def verify(contract, *, workspace, registry, context, execute, check_cancel, probes=None):
    """Return fresh evidence. Unknown command outcomes stop the whole verification."""
    from .probes import ProbeRegistry
    probes = probes if probes is not None else ProbeRegistry.defaults()
    results = []
    unknown = False
    root = Path(workspace).resolve()
    # Commands may modify files: inspect final file state after all commands.
    for criterion in sorted(contract.criteria, key=lambda c: c.kind != 'command'):
        check_cancel()
        passed, detail, evidence = False, '', {}
        if criterion.kind == 'command':
            result = execute('shell_exec', {'command': criterion.command, 'timeout': criterion.timeout})
            check_cancel()
            unknown = result.meta.get('execution_state') == 'unknown'
            code = result.meta.get('exit')
            passed = (not unknown and result.meta.get('execution_state') == 'completed'
                      and type(code) is int and code == criterion.expect_exit
                      and result.meta.get('authorization') == 'allow')
            detail = (result.error or result.content or 'No command exit evidence')[:2000]
            evidence = {'action_id': result.meta.get('action_id'), 'exit': code,
                        'execution_state': result.meta.get('execution_state')}
        elif criterion.kind == 'env_probe':
            observed = probes.check(criterion, workspace=workspace, context=context,
                                    registry=registry, check_cancel=check_cancel)
            passed, detail, evidence = observed['passed'], observed['detail'], observed['evidence']
            unknown = observed['unknown_outcome']
        else:
            try:
                path = (root / criterion.path).resolve()
                if not path.is_relative_to(root):
                    raise ValueError('Acceptance path/symlink escapes workspace')
                denial = registry.authorize('read_file', {'path': str(path)}, context)
                if denial is not None:
                    raise PermissionError(denial.error or 'File read not authorized')
                try:
                    path.stat()
                    exists = True
                except FileNotFoundError:
                    exists = False
                passed = (path.is_file() if criterion.exists else not exists)
                if passed and criterion.exists:
                    data = read_public_bytes(path, limit=8 * 1024 * 1024)
                    evidence = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                    if criterion.contains is not None:
                        text = context.secret_scope.document_text(path, data.decode('utf-8-sig'))
                        passed = criterion.contains in text
                detail = 'File criterion passed' if passed else 'File criterion failed'
            except (OSError, ValueError, UnicodeError) as exc:
                passed, detail = False, str(exc)[:2000]
        results.append({'id': criterion.id, 'required': criterion.required,
                        'passed': passed, 'detail': detail, 'evidence': evidence})
        if unknown:
            break
    check_cancel()
    return {'passed': not unknown and all(any(r['id'] == c.id and r['passed'] for r in results)
                for c in contract.criteria if c.required),
            'unknown_outcome': unknown, 'results': results}
