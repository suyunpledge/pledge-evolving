"""Shared desktop selection for the existing tool-free planning phase."""
import copy
import json
import os
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Property, Signal, Slot
from config_model import load_user_layer, save_user_layer
from revon.phase_models import model_pair
from revon.planning import planning_level, planning_policy, task_planning_level
from phase_client import PhaseClient, apply_planning_key, model_catalog, planning_config, planning_key_id, planning_selection
import secret_store


def make_plan(home, rows, pair, client, messages, level, model, cancel):
    planner = client
    if pair:
        secrets = secret_store.load(path=home / 'secrets.json', strict=True)
        env = {**os.environ, **secret_store.env_for(secrets)}
        planner = PhaseClient(rows, pair, env, client.secret_scope, cancel)
        apply_planning_key(home, pair, planner.router, secrets=secrets)
    # Native tool-call blocks from earlier rounds belong to the executor's wire protocol.
    from revon_client import ChatMessage
    history = [ChatMessage(m.role, m.content) for m in messages if m.role in {'user', 'assistant'} and m.content]
    plan, _ = planner.plan_task(history, level, model=model, cancel_event=cancel)
    return plan


class PlanningSettings(QObject):
    changed = Signal()
    failed = Signal(str)

    def __init__(self, home, parent):
        super().__init__(parent)
        self.home = home
        self._preview_rows = [] if parent.demo else None
        self._level, self._model, self._options = 'none', '', []
        self._policy = 'manual'
        self._endpoint, self._has_key = '', False

    def _rows(self):
        return self._preview_rows if self._preview_rows is not None else load_user_layer(self.home)

    level = Property(str, lambda s: s._level, notify=changed)
    policy = Property(str, lambda s: s._policy, notify=changed)
    model = Property(str, lambda s: s._model, notify=changed)
    options = Property('QVariantList', lambda s: s._options, notify=changed)
    endpoint = Property(str, lambda s: s._endpoint, notify=changed)
    hasKey = Property(bool, lambda s: s._has_key, notify=changed)

    @staticmethod
    def _address(rows, pair):
        if pair and pair in dict(model_catalog(rows)):
            address = next(row['config']['baseURL'] for row in rows if row['id'] == pair[0])
            if isinstance(address, str):
                parsed = urlsplit(address)
                if parsed.scheme in {'http', 'https'} and parsed.hostname and not (
                        parsed.username is not None or parsed.query or parsed.fragment):
                    return address.rstrip('/')
        return ''

    @Slot()
    def refresh(self):
        try:
            rows = self._rows()
            level, pair = planning_selection(rows)
            self._policy = planning_policy(planning_config(rows).get('policy', 'manual'))
            self._level, self._model = level, json.dumps(pair) if pair else ''
            self._endpoint = self._address(rows, pair)
            self._has_key = bool(self._endpoint and self._preview_rows is None and
                secret_store.load(path=self.home / 'secrets.json', strict=True).get(planning_key_id(pair, self._endpoint)))
            self._options = [{'label': '沿用执行模型', 'value': ''},
                *[{'label': label, 'value': json.dumps(key)} for key, label in model_catalog(rows)]]
            if pair and self._model not in {row['value'] for row in self._options}:
                self._options.append({'label': '已移除 · ' + ' / '.join(pair), 'value': self._model})
            self.changed.emit()
        except (ValueError, OSError) as exc:
            self.failed.emit(str(exc))

    @Slot(str, str, str, result=bool)
    def setCredential(self, selection, address, key):
        if self.parent().busy or self._preview_rows is not None:
            return False
        try:
            rows = self._rows()
            _, pair = planning_selection(rows)
            if not address or not pair or selection != json.dumps(pair) or address != self._address(rows, pair):
                raise ValueError('规划模型或接口已变化，请重新打开密钥设置。')
            name = planning_key_id(pair, address)
            path = self.home / 'secrets.json'
            if key:
                if len(key) > 4096 or not key.isascii() or not key.isprintable() or ' ' in key:
                    raise ValueError('请输入有效的 API 密钥。')
                secret_store.set_provider(name, key, path=path)
            else:
                secret_store.delete_provider(name, path=path)
            self.refresh()
            return True
        except (ValueError, OSError) as exc:
            self.failed.emit(str(exc))
            return False

    @Slot(str, str)
    def save(self, key, value):
        if self.parent().busy:
            return
        try:
            latest = self._rows()
            rows = copy.deepcopy(latest)
            if key == 'level':
                value = planning_level(value)
            elif key == 'policy':
                value = planning_policy(value)
            elif key == 'model':
                value = model_pair(json.loads(value)) if value else None
                if value and value not in dict(model_catalog(rows)):
                    raise ValueError('规划模型已移除，请重新选择。')
            else:
                return
            row = next((row for row in rows if row['id'] == 'planning'), None)
            if row is None:
                row = {'id': 'planning', 'name': 'planning:settings', 'config': {}}
                rows.append(row)
            row.pop('disabled', None)
            row.setdefault('config', {})[key] = value
            if key == 'policy' and value != self._policy:
                row['config']['level'] = 'none'
            if self._preview_rows is not None:
                self._preview_rows = rows
            else:
                save_user_layer(self.home, rows, expected_rows=latest)
            self.refresh()
        except (ValueError, OSError) as exc:
            self.failed.emit(str(exc))

    def take_level(self, text):
        latest = self._rows()
        cfg = planning_config(latest)
        policy = cfg.get('policy', 'manual')
        level = task_planning_level(policy, cfg.get('level', 'none'), text)
        if policy == 'manual' and level != 'none':
            rows = copy.deepcopy(latest)
            planning_config(rows)['level'] = 'none'
            if self._preview_rows is not None:
                self._preview_rows = rows
            else:
                save_user_layer(self.home, rows, expected_rows=latest)
            self.refresh()
        return level
