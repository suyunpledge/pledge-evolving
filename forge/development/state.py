"""UI-independent work mode, layout and immutable per-task choices."""
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
import threading

from forge.phase_models import model_pair
from forge.secrets import redact


class WorkMode(str, Enum):
    GENERAL = 'general'
    DEVELOPMENT = 'development'


@dataclass(frozen=True)
class TaskSnapshot:
    mode: WorkMode
    model: tuple | None
    role: str
    # A mode can never grant permissions. The host supplies separately approved policy.
    permission: str = 'read-only'
    roles: tuple = ()
    agent_models: tuple = ()


class ModeState:
    ROLES = ('default', 'complex', 'review', 'simple', 'subagent')

    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / 'development.json'
        self._lock = threading.RLock()
        self._listeners = []
        self._data = {'version': 1, 'mode': 'general', 'project': '', 'recent': [],
                      'roles': {}, 'agent_models': {}, 'tabs': [], 'session': '',
                      'layout': {'visible': True, 'preset': 'agent', 'fractions': [.2, .6],
                                 'output': .72, 'focus': 'editor'}}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or raw.get('version') != 1:
                raise ValueError('Unsupported Development Mode state; preserve the file for repair')
            WorkMode(raw.get('mode', 'general'))
            self._validate_layout(raw.get('layout', self._data['layout']))
            for role, pair in raw.get('roles', {}).items():
                self._validate_role(role, pair)
            for name, pair in raw.get('agent_models', {}).items():
                self._validate_agent(name, pair)
            for key in self._data:
                if key in raw:
                    self._data[key] = raw[key]

    @property
    def mode(self):
        with self._lock:
            return WorkMode(self._data['mode'])

    @property
    def layout(self):
        with self._lock:
            return deepcopy(self._data['layout'])

    def value(self, key):
        with self._lock:
            return deepcopy(self._data[key])

    def subscribe(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _save(self):
        self.home.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.development-', suffix='.tmp', dir=self.home)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def switch(self, mode):
        mode = WorkMode(mode)
        with self._lock:
            if mode == self.mode:
                return
            self._data['mode'] = mode.value
            self._save()
        # Callbacks observe committed state; subscriptions are host-owned, never Agent tools.
        for listener in tuple(self._listeners):
            listener(mode)

    @staticmethod
    def _validate_layout(layout):
        if not isinstance(layout, dict) or set(layout) - {'visible', 'preset', 'fractions', 'output', 'focus'}:
            raise ValueError('Invalid layout fields')
        if layout.get('preset', 'agent') not in {'agent', 'editor', 'focus'}:
            raise ValueError('Invalid layout preset')
        if not isinstance(layout.get('visible', True), bool) or layout.get('focus', 'editor') not in {'editor', 'agent'}:
            raise ValueError('Invalid panel visibility')
        f = layout.get('fractions', [.2, .6])
        if (not isinstance(f, list) or len(f) != 2 or
                not all(type(v) in (int, float) for v in f) or not .05 <= f[0] < f[1] <= .95):
            raise ValueError('Invalid layout fractions')
        if not .15 <= layout.get('output', .72) <= .9:
            raise ValueError('Invalid output fraction')

    def set_layout(self, **changes):
        with self._lock:
            layout = {**self._data['layout'], **changes}
            self._validate_layout(layout)
            self._data['layout'] = layout
            self._save()

    def _validate_role(self, role, pair):
        if role not in self.ROLES:
            raise ValueError('Unknown development model role')
        pair = model_pair(pair)
        if pair and redact(list(pair)) != list(pair):
            raise ValueError('Model identifiers must not contain credentials')
        return pair

    def configure_role(self, role, pair):
        pair = self._validate_role(role, pair)
        with self._lock:
            self._data['roles'][role] = list(pair) if pair else None
            self._save()

    def _validate_agent(self, name, pair):
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or redact(name) != name:
            raise ValueError('Invalid concrete Agent name')
        return self._validate_role('subagent', pair)

    def configure_agent_model(self, name, pair):
        pair = self._validate_agent(name, pair)
        with self._lock:
            self._data['agent_models'][name] = list(pair) if pair else None
            self._save()

    def remember_project(self, project):
        project = str(Path(project).resolve())
        with self._lock:
            self._data['project'] = project
            self._data['recent'] = [project, *[p for p in self._data['recent'] if p != project]][:12]
            self._save()

    def remember_tabs(self, paths):
        with self._lock:
            # No document contents or unsaved buffers on disk by default.
            self._data['tabs'] = [str(p) for p in paths][:64]
            self._save()

    def remember_session(self, path):
        with self._lock:
            self._data['session'] = str(path)
            self._save()

    def task_snapshot(self, *, explicit_model=None, agent_model=None, global_model=None,
                      model=None, role='default', permission='read-only'):
        if role not in self.ROLES or permission not in {'read-only', 'workspace-write'}:
            raise ValueError('Invalid task role or permission')
        with self._lock:
            selected = explicit_model or model or agent_model
            if selected is None and self.mode == WorkMode.DEVELOPMENT:
                selected = self._data['roles'].get(role) or self._data['roles'].get('default')
            roles = tuple((key, model_pair(pair)) for key, pair in self._data['roles'].items())
            agents = tuple((name, model_pair(pair)) for name, pair in self._data['agent_models'].items())
            return TaskSnapshot(self.mode, model_pair(selected or global_model), role, permission, roles, agents)
