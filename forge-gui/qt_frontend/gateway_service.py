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
from interaction_model import gateway_settings, model_label, select_provider
import secret_store
from forge import sampling
from forge.secrets import redact
from .store import SessionStore


class GatewayService:
    def __init__(self, home, workspace, emit, *, demo=False):
        self.home, self.workspace, self.emit = Path(home), Path(workspace), emit
        self.demo = demo
        self.process = None
        self.url = ''
        self.closed = threading.Event()
        self.lock = threading.RLock()

    def start(self, provider_id=''):
        if self.demo or self.closed.is_set():
            return
        with self.lock:
            rows = load_user_layer(self.home)
            prefs = SessionStore.read(self.home / 'gui/qt/onboarding.json', {})
            selected = provider_id or prefs.get('provider', '')
            row = next((r for r in rows if r['id'] == selected and not r.get('disabled')), None)
            chat = SessionStore(self.home).preferences()
            provider = row['config'] if row else select_provider(rows, chat.get('model', 'default'))
            provider = provider or select_provider(rows)
            if not provider:
                self.emit('error', message='请先配置模型。')
                return
            environment = {**os.environ, **secret_store.env_for()}
            base, key, model = gateway_settings(provider, environment)
            self.stop()
            environment['FORGE_GATEWAY_KEY'] = key
            settings = SessionStore.read(self.home / 'forge-desktop.json', {})
            port = settings.get('gateway_port', 8799)
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', int(port)))
                except (OSError, ValueError, TypeError):
                    probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            label = model_label(provider)
            repo = Path(__file__).resolve().parents[2]
            command = [sys.executable, str(repo / 'run.py'), 'gateway',
                       '--upstream', base, '--upstream-wire', 'openai', '--client-wire', 'openai',
                       '--models', label, '--port', str(port), '--home', str(self.home),
                       '--tools', '--workspace', str(self.workspace), '--profile', 'balanced',
                       '--read-scope', settings.get('read_scope', 'workspace')]
            if label != model:
                command += ['--model-map', label + '=' + model]
            options = {k: provider[k] for k in ('vendor', 'cacheControl', 'expectedCalls', 'callGapSeconds',
                       'adapt', 'thinkBudget', 'flex', 'defer', 'maxDeferSeconds', 'requestTimeout') if k in provider}
            if options:
                command += ['--provider-options', json.dumps(options)]
            self.process = process = subprocess.Popen(
                command, env=environment, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            threading.Thread(target=self._drain, args=(process,), daemon=True).start()
            url = f'http://127.0.0.1:{port}'
            from forge_client import ForgeGatewayClient
            deadline = time.monotonic() + 25
            while not self.closed.is_set() and process.poll() is None and time.monotonic() < deadline:
                try:
                    ok, _ = ForgeGatewayClient(url).health(cancel_event=self.closed)
                    if not ok:
                        self.closed.wait(.2)
                        continue
                except Exception:
                    self.closed.wait(.2)
                    continue
                self.url = url
                SessionStore.atomic(self.home / 'forge-desktop.json', {**settings, 'gateway_port': port})
                self.emit('gateway', url=url, temperature=sampling.resolve(
                    model, provider.get('wire', 'openai'), provider.get('temperature')))
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
