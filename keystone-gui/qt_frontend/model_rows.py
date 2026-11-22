"""编辑已有的模型行：接口地址、模型 ID、显示名称、协议、思考档位与密钥。

和「添加模型」走同一条路：用户层经 ``keystone.compat.user_layer(home)`` 原子写入
（``config_model.save_user_layer`` 带 expected_rows 防并发覆盖），明文密钥只进
密钥库（``secret_store``），模型行里只留 ``env.KEYSTONE_*`` 引用。

旧安装的 ``env.FORGE_*`` 引用：不换密钥时原样保留；换密钥时若引用本来就指向
这一行（``FORGE_<ID>_KEY`` / ``KEYSTONE_<ID>_KEY``）也保留原表达式——密钥库按行
id 存，两个名字都会注入；引用别的行（共用密钥）时，改写成本行自己的
``KEYSTONE_<ID>_KEY``，被共用的那一行不受影响。

密钥永不回传给 QML：行数据只给「已保存 / 未设置 / 环境变量」这样的状态。
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Property, Signal, Slot

from config_model import load_user_layer, save_user_layer
import model_onboarding
import secret_store
from keystone.compat import default_home

WIRES = ('openai', 'anthropic')
EFFORTS = ('low', 'medium', 'high', 'max')
_ENV = re.compile(r"""^get\(\s*['"]env\.([A-Za-z_][A-Za-z0-9_]*)['"]\s*(?:,\s*['"]{2}\s*)?\)$""")


def is_model_row(row):
    return isinstance(row, dict) and isinstance(row.get('config'), dict) and 'baseURL' in row['config']


def env_ref(config):
    """The environment variable an ``apiKey`` expression reads, or ''."""
    value = config.get('apiKey') if isinstance(config, dict) else None
    if isinstance(value, dict) and isinstance(value.get('$expr'), str):
        match = _ENV.match(value['$expr'].strip())
        return match.group(1) if match else ''
    return ''


def own_env_names(row_id):
    stem = row_id.upper().replace('-', '_')
    return {f'KEYSTONE_{stem}_KEY', f'FORGE_{stem}_KEY'}


def key_state(config, env):
    """Human status of a row's credential, never the value."""
    value = config.get('apiKey')
    if value is None or value == '':
        return 'none'
    name = env_ref(config)
    if name:
        return 'saved' if env.get(name) or os.environ.get(name) else 'missing'
    return 'inline' if isinstance(value, str) else 'expr'


def efforts_text(config):
    caps = config.get('routeCapabilities')
    if isinstance(caps, dict) and isinstance(caps.get('reasoningEfforts'), list):
        return ','.join(caps['reasoningEfforts']) or 'none'
    return 'auto'


def parse_efforts(text):
    """'auto' (follow the vendor profile), 'none', or a comma list of known levels."""
    text = str(text or '').strip().lower().replace('，', ',')
    if text in ('', 'auto'):
        return None
    if text == 'none':
        return []
    levels = [part.strip() for part in text.split(',') if part.strip()]
    if not levels or any(level not in EFFORTS for level in levels) or len(set(levels)) != len(levels):
        raise ValueError('思考档位只能是 auto、none，或 low/medium/high/max 中的若干项（逗号分隔）。')
    return [level for level in EFFORTS if level in levels]


def project(row, env):
    config = row['config']
    return {
        'id': row['id'], 'label': str(config.get('modelLabel') or ''),
        'title': str(config.get('modelLabel') or config.get('model') or row['id']),
        'model': str(config.get('model') or ''), 'baseURL': str(config.get('baseURL') or ''),
        'wire': str(config.get('wire') or 'openai'),
        'temperature': '' if config.get('temperature') is None else str(config['temperature']),
        'efforts': efforts_text(config), 'disabled': bool(row.get('disabled')),
        'keyState': key_state(config, env), 'keyRef': env_ref(config),
    }


def edited_rows(rows, row_id, values, *, new_key=False):
    """Copy of ``rows`` with one model row edited. Returns (rows, env name to fill or '')."""
    rows = copy.deepcopy(rows)
    row = next((r for r in rows if r.get('id') == row_id), None)
    if row is None or not is_model_row(row):
        raise ValueError('这条模型配置已不存在，请刷新后重试。')
    config = row['config']
    address = model_onboarding.check_address(values.get('baseURL', ''))
    model = str(values.get('model', '')).strip()
    if not model or any(ch in model for ch in ('\r', '\n', ',')):
        raise ValueError('请输入有效的模型 ID。')
    label = str(values.get('label', '')).strip()
    if any(ch in label for ch in ('\r', '\n')) or len(label) > 80:
        raise ValueError('显示名称需为一行、不超过 80 个字符。')
    wire = str(values.get('wire', config.get('wire', 'openai')))
    if wire not in WIRES:
        raise ValueError('协议只能是 OpenAI 兼容或 Anthropic。')
    temperature = str(values.get('temperature', '')).strip()
    if temperature:
        try:
            value = float(temperature)
        except ValueError:
            value = -1
        if not 0 <= value <= 2:
            raise ValueError('温度需是 0 到 2 之间的数字。')
        config['temperature'] = value
    else:
        config.pop('temperature', None)
    if config.get('smallModel') == config.get('model'):
        config['smallModel'] = model
    config.update(baseURL=address, model=model, wire=wire)
    if label:
        config['modelLabel'] = label
    else:
        config.pop('modelLabel', None)
    efforts = parse_efforts(values.get('efforts', 'auto'))
    caps = config.get('routeCapabilities')
    caps = dict(caps) if isinstance(caps, dict) else {}
    if efforts is None:
        caps.pop('reasoningEfforts', None)
    else:
        caps['reasoningEfforts'] = efforts
    if caps:
        config['routeCapabilities'] = caps
    else:
        config.pop('routeCapabilities', None)
    target = ''
    if new_key:
        current = env_ref(config)
        own = 'KEYSTONE_' + row_id.upper().replace('-', '_') + '_KEY'
        target = current if current in own_env_names(row_id) else own
        config['apiKey'] = {'$expr': f"get('env.{target}', '')"}
    return rows, target


def removed_rows(rows, row_id):
    """Copy of ``rows`` without one model row; refuses rows other rows still name."""
    row = next((r for r in rows if r.get('id') == row_id), None)
    if row is None or not is_model_row(row):
        raise ValueError('这条模型配置已不存在，请刷新后重试。')
    others = [r for r in rows if r.get('id') != row_id]
    users = [r['id'] for r in others if f'"{row_id}"' in json.dumps(r.get('config', {}), ensure_ascii=False)]
    if users:
        raise ValueError('「' + '、'.join(users) + '」仍引用这个模型，请先修改引用再删除。')
    return copy.deepcopy(others)


class ModelRows(QObject):
    changed = Signal()
    saved = Signal(str)
    _finished = Signal(object)

    def __init__(self, home, parent=None, *, demo=False):
        super().__init__(parent)
        self.home = Path(home)
        self.demo = demo
        self.credential_path = self.home / 'secrets.json' if self.home != default_home() else None
        self.can_save = lambda: True
        self._rows, self._status, self._ok, self._busy = [], '', False, False
        self._finished.connect(self._complete)

    rows = Property('QVariantList', lambda self: self._rows, notify=changed)
    status = Property(str, lambda self: self._status, notify=changed)
    success = Property(bool, lambda self: self._ok, notify=changed)
    busy = Property(bool, lambda self: self._busy, notify=changed)
    wires = Property('QVariantList', lambda self: [
        {'value': 'openai', 'label': 'OpenAI 兼容'}, {'value': 'anthropic', 'label': 'Anthropic'}], constant=True)

    def _secrets_env(self):
        try:
            return secret_store.env_for(secret_store.load(path=self.credential_path))
        except OSError:
            return {}

    def _report(self, ok, message):
        self._ok, self._status = ok, message
        self.changed.emit()

    @Slot()
    def reload(self):
        try:
            rows = load_user_layer(self.home)
        except (OSError, ValueError) as exc:
            self._rows = []
            self._report(False, f'读取模型配置失败：{exc}')
            return
        env = self._secrets_env()
        self._rows = [project(r, env) for r in rows if is_model_row(r)]
        self._ok, self._status = False, ''
        self.changed.emit()

    @Slot()
    def clearStatus(self):
        if not self._busy and self._status:
            self._status = ''
            self.changed.emit()

    def _guard(self):
        if self.demo:
            self._report(False, '预览模式不修改模型配置。')
            return False
        if self._busy or not self.can_save():
            self._report(False, '请先完成当前任务或保存工作台的修改。')
            return False
        return True

    @Slot(str, 'QVariantMap', str)
    def save(self, row_id, values, key):
        if not self._guard():
            return
        key = str(key or '').strip()
        try:
            if key:
                model_onboarding.check_key(key)
            latest = load_user_layer(self.home)
            rows, target = edited_rows(latest, row_id, dict(values), new_key=bool(key))
            if key:
                with secret_store.update_provider(row_id, key, path=self.credential_path):
                    save_user_layer(self.home, rows, expected_rows=latest)
            else:
                save_user_layer(self.home, rows, expected_rows=latest)
        except (OSError, ValueError) as exc:
            self._report(False, str(exc) or '保存失败，请检查配置文件并重试。')
            return
        self.reload()
        self._report(True, '已保存，正在更新网关。')
        self.saved.emit(row_id)

    @Slot(str)
    def remove(self, row_id):
        if not self._guard():
            return
        try:
            latest = load_user_layer(self.home)
            row = next((r for r in latest if r.get('id') == row_id), {})
            rows = removed_rows(latest, row_id)
            save_user_layer(self.home, rows, expected_rows=latest)
            ref = env_ref(row.get('config', {}))
            still_used = any(env_ref(r.get('config', {})) == ref for r in rows) if ref else False
            if ref in own_env_names(row_id) and not still_used:
                self._drop_secret(row_id)
        except (OSError, ValueError) as exc:
            self._report(False, str(exc) or '删除失败，请检查配置文件并重试。')
            return
        self.reload()
        self._report(True, '已删除。')
        self.saved.emit('')

    def _drop_secret(self, name):
        with secret_store._LOCK:
            current = secret_store.load(strict=True, path=self.credential_path)
            if name in current:
                current.pop(name)
                secret_store.save(current, path=self.credential_path)

    @Slot(str, 'QVariantMap', str)
    def test(self, row_id, values, key):
        if self._busy:
            return
        if self.demo:
            self._report(False, '预览模式不发送网络请求。')
            return
        key = str(key or '').strip()
        try:
            if key:
                model_onboarding.check_key(key)
            address = model_onboarding.check_address(values.get('baseURL', ''))
            wire = str(values.get('wire', 'openai'))
            if wire not in WIRES:
                raise ValueError('协议只能是 OpenAI 兼容或 Anthropic。')
            if not key:
                row = next((r for r in load_user_layer(self.home) if r.get('id') == row_id), None)
                ref = env_ref((row or {}).get('config', {}))
                key = (self._secrets_env().get(ref) or os.environ.get(ref, '')) if ref else ''
        except (OSError, ValueError) as exc:
            self._report(False, str(exc))
            return
        self._busy, self._ok, self._status = True, False, '正在测试连接…'
        self.changed.emit()

        def worker():
            try:
                result = model_onboarding.probe(address, key, wire=wire)
            except Exception:
                result = {'ok': False, 'error': '测试失败，请检查本机网络与配置。'}
            self._finished.emit(result)

        threading.Thread(target=worker, daemon=True, name='Keystone-Qt-model-test').start()

    @Slot(object)
    def _complete(self, result):
        self._busy = False
        self._report(bool(result.get('ok')), result.get('message') or result.get('error') or '')
