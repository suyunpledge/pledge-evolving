"""Trusted desktop model settings projection; never imports execution authority.

Only provider and model-router rows cross this seam. Credentials are resolved in
a private expression context and bound by Provider to their endpoint. No store is
copied, no process environment is written, and no Work Agent is borrowed.
"""
import json
import os
from dataclasses import replace
from pathlib import Path
import re
from urllib.parse import urlsplit

from forge.config import Config, USER_LAYER_NAME, load_config
from forge.model import HttpTransport, ModelRouter
from forge.routing import SmartRouter
from forge.secrets import SecretScope, protect_store_path


def _model_row(row):
    return (row.name.startswith('provider:') or (row.id == 'model' and row.name == 'model:router')
            or (not row.name and (row.id == 'model' or 'baseURL' in row.config or 'base_url' in row.config)))


class ModelSettingsSource:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()

    def signature(self, local_home):
        result = []
        for home in (Path(local_home), self.home):
            for name in (USER_LAYER_NAME, 'secrets.json'):
                path = home / name
                try:
                    stat = path.stat()
                    result.append((str(path), stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    result.append((str(path), None, None))
        return tuple(result)

    def __call__(self, isolated_config, local_home):
        local = load_config(local_home)
        own_models = any(_model_row(r) for r in local.rows.values())
        home = Path(local_home) if own_models else self.home
        source = isolated_config if own_models else load_config(home)
        rows = [r for r in source.live_rows() if _model_row(r)]
        # The desktop's saved rows historically allow an omitted name. Match
        # that public input format without pulling GUI normalization into core.
        rows = [r if r.name else replace(r, name='model:router' if r.id == 'model' else 'provider:' + r.id)
                for r in rows]
        # Resolve only references actually used by model rows, not connector or
        # arbitrary Work secrets. Raw credentials never enter the public result.
        names = set(re.findall(r'env\.(FORGE_[A-Z0-9_]+_KEY)',
                               json.dumps([r.config for r in rows])))
        env = {name: os.environ.get(name, '') for name in names}
        path = home / 'secrets.json'
        protect_store_path(path)
        try:
            with path.open('rb') as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('Model credential store size limit')
            values = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                   for k, v in values.items()):
                raise ValueError('Invalid model credential store')
            scope = SecretScope()
            try:
                for key, value in values.items():
                    name = 'FORGE_' + key.upper().replace('-', '_') + '_KEY'
                    if name in names and not key.startswith('connector.'):
                        scope.reference(value)
                        env[name] = value
            finally:
                scope.close()
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            raise ValueError('模型密钥文件不可读或格式损坏，请在模型设置中修复：' + str(path)) from None
        projection = Config()
        projection.apply_patch([{'id': row_id, 'name': name, 'config': conf}
                                for row_id, name, conf in _resolved(rows, env)], label='host models only')
        cls = SmartRouter if projection.get('model', 'routing', None) is not None else ModelRouter
        return cls.from_config(projection)


def _resolved(rows, env):
    projection = Config()
    projection.apply_patch([r.to_raw() for r in rows], label='model projection')
    return projection.active({'env': env})


def model_readiness(router, explicit_model=None):
    """Local structural/auth-presence check; never a network or billing probe."""
    # Borrowed host/test routers can implement their own transport contract.
    if not isinstance(router, ModelRouter):
        return {'ready': True, 'code': 'ready', 'message': 'Agent 已就绪'}
    order = [explicit_model] if explicit_model else router._order(None)
    if not order:
        return {'ready': False, 'code': 'model_missing', 'message': '没有可用模型，请先在宿主模型设置中启用 Provider 和默认模型。'}
    pair = order[0]
    provider = router.providers.get(pair[0])
    if provider is None or not pair[1]:
        return {'ready': False, 'code': 'model_unavailable', 'message': '默认或所选模型已失效，请重新选择已配置模型。'}
    target = urlsplit(provider.base_url)
    if target.scheme not in {'http', 'https'} or not target.netloc:
        return {'ready': False, 'code': 'endpoint_missing', 'message': '所选模型缺少有效 API 地址，请修正模型设置。'}
    local = target.hostname in {'localhost', '127.0.0.1', '::1'}
    if (isinstance(router.transport, HttpTransport) and not local and not provider.api_key
            and not getattr(provider, '_header_credentials', {})):
        return {'ready': False, 'code': 'credential_missing', 'message': '所选模型未配置 API 密钥，请在宿主模型设置中保存后重试发送。'}
    return {'ready': True, 'code': 'ready', 'message': 'Agent 已就绪（尚未验证供应商网络）'}
