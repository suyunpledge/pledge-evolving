"""Headless ownership of the desktop gateway and its selected provider."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

from config_model import load_user_layer
from interaction_model import gateway_settings, model_label
import secret_store
from revon import sampling
from revon.secrets import redact
from .store import SessionStore
from revon.compat import desktop_config


def model_rows(home):
    """Rows that can offer chat models: live plugin patch layers, then the user layer (user wins by id).

    Provider plugins (contributions.patch with provider:* rows) thus appear in the model picker
    exactly like rows the user added; a broken plugin layer is skipped, never fatal.
    """
    from revon.config import plugin_layers, plugin_patch_problems
    merged = {}
    for layer in plugin_layers(home):
        try:
            patch = json.loads(layer.read_text(encoding='utf-8-sig'))
            if plugin_patch_problems(patch):
                continue
        except (OSError, ValueError):
            continue
        for op in patch:
            for row in (op['insert'] if isinstance(op.get('insert'), list) else [op]):
                merged[str(row['id'])] = {**row, 'plugin': layer.stem}
    for row in load_user_layer(home):
        merged[row['id']] = row
    return list(merged.values())


def chat_model_routes(rows):
    """Match each displayed model to one configured provider and upstream ID."""
    routes = {}
    for row in rows:
        provider = row.get('config', {})
        primary = provider.get('model')
        name = row.get('name') or ''
        if (row.get('disabled') or (name and not name.startswith('provider:'))
                or provider.get('wire', 'openai') not in ('openai', 'google')
                or not provider.get('baseURL') or not isinstance(primary, str) or not primary.strip()):
            continue
        extras = provider.get('models')
        models = [primary, *(extras if isinstance(extras, (list, tuple)) else [])]
        models = dict.fromkeys(value for value in models if isinstance(value, str) and value.strip())
        for model in models:
            base = (model_label(provider) if model == primary else model).strip() or model
            # The gateway's existing CLI uses these two characters as separators.
            base = base.replace(',', '，').replace('=', '＝')
            label, ordinal = base, 2
            while label in routes:
                label = f'{base} ({ordinal})'
                ordinal += 1
            routes[label] = (row['id'], {**provider, 'model': model})
    return routes


class GatewayService:
    def __init__(self, home, workspace, emit, *, demo=False):
        self.home, self.workspace, self.emit = Path(home), Path(workspace), emit
        self.demo = demo
        self.process = None
        self.url = ''
        self.closed = threading.Event()
        self.lock = threading.RLock()

    def start(self, provider_id='', *, model=''):
        if self.demo or self.closed.is_set():
            return
        with self.lock:
            rows = model_rows(self.home)
            routes = chat_model_routes(rows)
            self.emit('gateway_catalog', models=list(routes))
            prefs = SessionStore.read(self.home / 'gui/qt/onboarding.json', {})
            chat = SessionStore(self.home).preferences()
            if model:
                label = model if model in routes else ''
            elif provider_id:
                label = next((name for name, (key, _) in routes.items() if key == provider_id), '')
            else:
                saved = chat.get('model', 'default')
                label = saved if saved in routes else next(
                    (name for name, (_, route) in routes.items() if route['model'] == saved), '')
                if not label:
                    label = next((name for name, (key, _) in routes.items() if key == prefs.get('provider')),
                                 next(iter(routes), ''))
            if not label:
                if model or provider_id:
                    raise ValueError('所选模型已移除、停用或不适用于对话，请重新选择。')
                self.stop()
                self.emit('gateway_error', message='请先配置模型。', active_url='')
                return
            provider_id, provider = routes[label]
            environment = {**os.environ, **secret_store.env_for()}
            base, key, upstream_model = gateway_settings(provider, environment)
            self.stop()
            environment['REVON_GATEWAY_KEY'] = key
            settings = SessionStore.read(desktop_config(self.home), {})
            port = settings.get('gateway_port', 8799)
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', int(port)))
                except (OSError, ValueError, TypeError):
                    probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            repo = Path(__file__).resolve().parents[2]
            command = [sys.executable, str(repo / 'run.py'), 'gateway',
                       '--upstream', base, '--upstream-wire', provider.get('wire', 'openai'), '--client-wire', 'openai',
                       '--models', label, '--port', str(port), '--home', str(self.home),
                       '--tools', '--workspace', str(self.workspace), '--profile', 'balanced',
                       '--read-scope', settings.get('read_scope', 'workspace')]
            if label != upstream_model:
                command += ['--model-map', label + '=' + upstream_model]
            options = {k: provider[k] for k in ('vendor', 'cacheControl', 'expectedCalls', 'callGapSeconds',
                       'adapt', 'thinkBudget', 'flex', 'defer', 'maxDeferSeconds', 'requestTimeout',
                       'routeCapabilities', 'modelCapabilities') if k in provider}
            options['providerId'] = provider_id
            command += ['--provider-options', json.dumps(options)]
            self.process = process = subprocess.Popen(
                command, env=environment, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            threading.Thread(target=self._drain, args=(process,), daemon=True).start()
            url = f'http://127.0.0.1:{port}'
            from revon_client import RevonGatewayClient
            deadline = time.monotonic() + 25
            while not self.closed.is_set() and process.poll() is None and time.monotonic() < deadline:
                try:
                    client = RevonGatewayClient(url)
                    ok, _ = client.health(cancel_event=self.closed)
                    if not ok or label not in client.list_models(timeout=3, cancel_event=self.closed):
                        self.closed.wait(.2)
                        continue
                except Exception:
                    self.closed.wait(.2)
                    continue
                self.url = url
                SessionStore.atomic(desktop_config(self.home), {**settings, 'gateway_port': port})
                self.emit('gateway', url=url, model=label, temperature=sampling.resolve(
                    upstream_model, provider.get('wire', 'openai'), provider.get('temperature')))
                return
            self.stop()
            raise RuntimeError('网关启动失败，请检查模型配置。')

    def _drain(self, process):
        for line in process.stdout:
            if 'Traceback' in line:
                self.emit('error', message='网关异常，请重新连接。')
        process.stdout.close()

    def stop(self):
        process, self.process = self.process, None
        self.url = ''
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        self.closed.set()
        with self.lock:
            self.stop()
