"""A retained, asynchronously loaded directory of local MCP configurations."""
import hashlib
import json
import re
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import quote_plus, urlsplit

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication

import secret_store
from forge.desktop_services import request_json
from forge.http_transport import open_response
from forge.secrets import SecretScope, redact
from .mcp_china import QUERY_KEYS
from .mcp_http import McpHttp, endpoint_url
from .mcp_local import OBSIDIAN_READ_TOOLS, local_command, McpStdio, vault_path
from .mcp_oauth import (FEISHU_REDIRECT, authorize_feishu, authorize_preset,
                        baidu_access_token, feishu_credentials, feishu_scopes, refresh_feishu, refresh_preset)
from .mcp_presets import AWS_KNOWLEDGE_TOOLS, CATEGORIES, MANUAL_LOGIN, PRESETS, preset_rows
from .mcp_meeting import READ_TOOLS as MEETING_READ_TOOLS, meeting_config
from .mcp_sse import McpSse
from .mcp_wecom import WeCom
from .store import SessionStore
from .workspace_pairing import (FAMILIES, FORM, GOOGLE_SCOPE, GOOGLE_TOKEN, MICROSOFT_LOGIN,
                                WorkspacePairing, directory, google_authorize)


class McpCatalog(QObject):
    changed = Signal()
    serverSaved = Signal(str)
    _done = Signal(object)
    _authorization = Signal(object)

    def __init__(self, home, parent=None, *, demo=False):
        super().__init__(parent)
        self.path = home / 'gui' / 'qt' / 'mcp-servers.json'
        self.home = home
        self.demo = demo
        self._workspace_pairing = None
        self._directory = directory()
        self._presets = {row['id']: row for row in preset_rows()}
        self._preset_credentials = set()
        self._oauth_identifier = ""
        self._vault = ''
        self._accounts = {}
        self._pair_errors = {}
        self._pair_cancel = None
        self._login = {}
        self._accounts_error = ''
        self._servers = []
        self._tools = {}
        self._states = {}
        self._loaded = False
        self._busy = False
        self._operation = ''
        self._error = ''
        self._result = ''
        self._tool_schema = ''
        self._cancel = threading.Event()
        self._worker = None
        self._done.connect(self._finish, Qt.ConnectionType.QueuedConnection)
        self._authorization.connect(self._show_authorization, Qt.ConnectionType.QueuedConnection)

    rows = Property('QVariantList', lambda s: s._rows(), notify=changed)
    loaded = Property(bool, lambda s: s._loaded, notify=changed)
    busy = Property(bool, lambda s: s._busy, notify=changed)
    saving = Property(bool, lambda s: s._busy and s._operation in {'save', 'preset', 'vault'}, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)
    result = Property(str, lambda s: s._result, notify=changed)
    toolSchema = Property(str, lambda s: s._tool_schema, notify=changed)
    families = Property('QVariantList', lambda s: [
        {**row, 'paired': row['group'] in s._accounts.get(row['family'], {}).get('paired', []),
         'status': s._states.get(row['id'], ''), 'tools': s._tools.get(row['id'], [])}
        for row in s._directory], notify=changed)
    accounts = Property('QVariantMap', lambda s: s._accounts, notify=changed)
    pairErrors = Property('QVariantMap', lambda s: s._pair_errors, notify=changed)
    pairing = Property(bool, lambda s: s._busy and s._operation == 'pair', notify=changed)
    authorizing = Property(bool, lambda s: s._busy and s._operation == 'oauth', notify=changed)
    feishuRedirect = Property(str, lambda s: FEISHU_REDIRECT, constant=True)
    login = Property('QVariantMap', lambda s: s._login, notify=changed)
    vault = Property(str, lambda s: s._vault, notify=changed)
    accountsError = Property(str, lambda s: s._accounts_error, notify=changed)
    categories = Property('QStringList', lambda s: CATEGORIES, constant=True)
    presets = Property('QVariantList', lambda s: [
        {**row, 'hasCredential': row['id'] in s._preset_credentials,
         'configured': s._preset_ready(row),
         'status': s._states.get(row['id'], ''), 'tools': s._tools.get(row['id'], [])}
        for row in s._presets.values()], notify=changed)

    def _rows(self):
        return [{**server, 'status': self._states.get(server['id'], '未测试'),
                 'tools': self._tools.get(server['id'], [])} for server in self._servers]

    def _preset_ready(self, row):
        if row.get('setup'):
            return False
        if row['key'] == 'obsidian':
            return bool(self._vault)
        if row['oauth']:
            return row['id'] in self._preset_credentials
        return not row['credential'] or row['optionalCredential'] or row['id'] in self._preset_credentials

    def _start(self, kind, operation):
        if self._busy or self._cancel.is_set():
            return
        self._busy, self._error = True, ''
        self._operation = kind
        self.changed.emit()

        def run():
            try:
                result = (kind, operation(), '')
            except HTTPError as exc:
                result = (kind, None, f'MCP 服务返回 HTTP {exc.code}')
            except Exception as exc:
                result = (kind, None, str(redact(str(exc))))
            if not self._cancel.is_set():
                self._done.emit(result)

        self._worker = threading.Thread(target=run, daemon=True, name='Forge-MCP')
        self._worker.start()

    @Slot()
    def load(self):
        if not self._loaded:
            self._start('load', lambda: (self._read_servers(), self._account_state(), self._read_presets(),
                                         self._read_vault()))
        elif not self._busy:
            self._start('accounts', self._account_state)

    def _account_state(self):
        try:
            return self._read_accounts(), ''
        except Exception:
            return {}, '暂时无法读取连接器账户。'

    def _read_accounts(self):
        if self.demo:
            return {}
        if self._workspace_pairing is None:
            self._workspace_pairing = WorkspacePairing(self.home)
        return self._workspace_pairing.snapshot()

    def _read_presets(self):
        if self.demo:
            return set()
        credentials = secret_store.load()
        return {row['id'] for row in self._presets.values()
                if credentials.get('connector.mcp.' + row['id'])}

    def _read_vault(self):
        if self.demo:
            return ''
        try:
            path = self.path.with_name('mcp-obsidian.json')
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, str) else ''
        except (OSError, ValueError):
            return ''

    @Slot(str, 'QStringList', str, str)
    def pair(self, family, identifiers, client_id, credential):
        if self._busy or not self._loaded or self._accounts_error or family not in FAMILIES:
            return
        selected = set(identifiers)
        choices = [row['group'] for row in self._directory if row['id'] in selected and row['family'] == family]
        client_id, credential = client_id.strip(), credential.strip()
        errors = {}
        if family == 'google':
            account = self._accounts.get('google', {})
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}\.apps\.googleusercontent\.com', client_id):
                errors['google'] = '请填写 Google 桌面 OAuth Client ID。'
            elif not credential and not (account.get('hasSecret') and account.get('clientId') == client_id):
                errors['google'] = '请填写 Google 桌面 OAuth Client Secret。'
            elif len(credential) > 512 or any(c.isspace() for c in credential):
                errors['google'] = 'Google Client Secret 必须为单行文本。'
        else:
            credential = credential or 'common'
            if (not re.fullmatch(r'[A-Za-z0-9-]{1,100}', client_id)
                    or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', credential)):
                errors['microsoft'] = '请填写 Microsoft Entra Client ID 和有效的 Tenant。'
        self._pair_errors = errors
        if errors or not choices:
            self.changed.emit()
            return
        cancel = self._pair_cancel = threading.Event()
        self._login = {}

        def pair():
            errors = {}
            if self.demo:
                return family, {}, {family: '预览模式不会发起外部授权。'}
            account_service = self._workspace_pairing
            try:
                if family == 'google':
                    account_service.pair_google(choices, client_id, credential,
                                                lambda url: self._publish_authorization(cancel, family, url), cancel)
                else:
                    account_service.pair_microsoft(choices, client_id, credential,
                                                   lambda code, _: self._publish_authorization(
                                                       cancel, family, MICROSOFT_LOGIN, code), cancel)
            except Exception as exc:
                errors[family] = '配对已取消。' if cancel.is_set() else str(redact(str(exc)))
            accounts = self._read_accounts()
            if not errors:
                paired = accounts[family]['paired']
                missing = [row['name'] for row in self._directory
                           if row['family'] == family and row['group'] in choices and row['group'] not in paired]
                if missing:
                    errors[family] = '未获得权限 · ' + '、'.join(missing)
            return family, accounts, errors

        self._start('pair', pair)

    def _publish_authorization(self, cancel, family, url, code=''):
        if not self._cancel.is_set() and not cancel.is_set():
            self._authorization.emit((cancel, family, url, code))

    @Slot(object)
    def _show_authorization(self, challenge):
        cancel, family, url, code = challenge
        if self._cancel.is_set() or cancel is not self._pair_cancel or cancel.is_set():
            return
        self._login = {'family': family, 'url': url, 'code': code}
        self.changed.emit()
        self.openAuthorization()

    @Slot()
    def openAuthorization(self):
        if self._login.get('url'):
            QDesktopServices.openUrl(QUrl(self._login['url']))

    @Slot()
    def copyAuthorizationCode(self):
        if self._login.get('code'):
            QGuiApplication.clipboard().setText(self._login['code'])

    @Slot()
    def cancelPairing(self):
        if self._pair_cancel is not None:
            self._pair_cancel.set()
            self._login = {}
            self.changed.emit()

    @Slot(str)
    def openCredentialPage(self, identifier):
        row = self._presets.get(identifier)
        login = MANUAL_LOGIN.get(row['key']) if row else None
        if login and not self.demo and self._loaded and not self._busy:
            QDesktopServices.openUrl(QUrl(login[0]))

    @Slot(str, str)
    def connectWithCredential(self, identifier, value):
        row = self._presets.get(identifier)
        if not row or row.get('loginMode') != 'manual-token' or not self._loaded or self._busy:
            return
        try:
            token = baidu_access_token(value) if row['key'] == 'baidu-netdisk' else value.strip()
            if not token or len(token) > 8192 or not re.fullmatch(r'[!-~]+', token):
                raise ValueError('请输入有效的单行访问凭据。')
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return

        def connect(cancel):
            tools = self._request(row, lambda client: client.tools(), credential=token, cancel=cancel)
            if cancel.is_set():
                raise PermissionError('授权已取消。')
            secret_store.set_provider('connector.mcp.' + identifier, token)
            return tools

        stage = '正在准备腾讯会议官方连接配置…' if row['key'] == 'tencent-meeting' else '正在验证访问凭据…'
        self._connect_browser(identifier, connect, stage)

    @Slot(str, str, str, str)
    def connectGoogleCloud(self, identifier, client_id, client_secret, project):
        row = self._presets.get(identifier)
        if not row or row.get('loginMode') != 'google-cloud' or not self._loaded or self._busy:
            return
        client_id, client_secret, project = client_id.strip(), client_secret.strip(), project.strip()
        if (not re.fullmatch(r'[a-zA-Z0-9._-]{5,180}\.apps\.googleusercontent\.com', client_id)
                or not re.fullmatch(r'[a-zA-Z0-9_-]{8,256}', client_secret)
                or not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', project)):
            self._error = '请输入 Google 桌面 OAuth Client ID、Client Secret 和 Google Cloud 项目 ID。'
            self.changed.emit()
            return

        def request(url, method, headers, body, **options):
            return request_json(url, method, headers, body, opener=open_response, **options)

        def connect(cancel):
            issued = google_authorize(client_id, client_secret, {GOOGLE_SCOPE + 'cloud-platform'},
                                      lambda url: self._publish_authorization(cancel, row['key'], url),
                                      cancel, request)
            session = WorkspacePairing._google_tokens(issued, client_secret)
            session.update(client=client_id, project=project)
            tools = self._request(row, lambda client: client.tools(), credential=session, cancel=cancel)
            if cancel.is_set():
                raise PermissionError('授权已取消。')
            secret_store.set_provider('connector.mcp.' + identifier, json.dumps(session))
            return tools

        self._connect_browser(identifier, connect, '正在准备 Google Cloud 授权…')

    @Slot(str, str, str)
    def connectFeishu(self, app_id, app_secret, scopes):
        try:
            app_id, app_secret = feishu_credentials(app_id, app_secret)
            scopes = feishu_scopes(scopes)
        except ValueError as exc:
            if not self._busy:
                self._error = str(exc)
                self.changed.emit()
            return
        self._connect_feishu(app_id, app_secret, scopes)

    def _connect_feishu(self, app_id, app_secret, scopes):
        identifier = 'preset.feishu'

        def connect(cancel):
            local_command(self.home, 'feishu', cancel)
            session = authorize_feishu(app_id, app_secret,
                                       lambda url: self._publish_authorization(cancel, 'feishu', url), cancel,
                                       scopes=scopes)
            session['scopes'] = scopes
            tools = self._request(self._presets[identifier], lambda client: client.tools(),
                                  credential=session, cancel=cancel)
            if cancel.is_set():
                raise PermissionError('授权已取消。')
            secret_store.set_provider('connector.mcp.' + identifier, json.dumps(session))
            return tools

        self._connect_browser(identifier, connect, '正在准备飞书本机服务…')

    @Slot(str)
    def connectOAuth(self, identifier):
        row = self._presets.get(identifier)
        if not row or not row['oauth'] or self._busy or not self._loaded or self.demo:
            return
        key = row['key']
        if key == 'feishu':
            try:
                stored = json.loads(secret_store.get_provider('connector.mcp.' + identifier) or '{}')
                app_id, app_secret = feishu_credentials(stored['client'], stored['secret'])
            except (ValueError, KeyError, TypeError):
                self._error = '飞书本地应用配置不可用。请点「更换飞书应用」重新配置。'
                self.changed.emit()
            else:
                self._connect_feishu(app_id, app_secret, stored.get('scopes', ''))
            return
        if row['loginMode'] == 'manual-token':
            self.openCredentialPage(identifier)
            return
        if row['loginMode'] == 'google-cloud':
            try:
                stored = json.loads(secret_store.get_provider('connector.mcp.' + identifier) or '{}')
                client_id, client_secret, project = stored['client'], stored['client_secret'], stored['project']
            except (ValueError, KeyError, TypeError):
                self._error = 'Google Cloud 授权配置不可用。请点「更换 Google Cloud 项目」。'
                self.changed.emit()
            else:
                self.connectGoogleCloud(identifier, client_id, client_secret, project)
            return
        if key == 'wecom':
            def connect(cancel):
                secret_key = 'connector.mcp.' + identifier
                previous = secret_store.get_provider(secret_key)
                profile, tools = WeCom.connect(
                    self.home, cancel,
                    notify=lambda url: self._publish_authorization(cancel, key, url))
                try:
                    if cancel.is_set():
                        raise PermissionError('授权已取消。')
                    secret_store.set_provider(secret_key, profile)
                except Exception:
                    WeCom.disconnect(self.home, profile)
                    raise
                if previous:
                    try:
                        WeCom.disconnect(self.home, previous)
                    except OSError:
                        pass
                return tools

            self._connect_browser(identifier, connect, '正在准备企业微信本机服务…')
            return

        def connect(cancel):
            session = authorize_preset(key, row['url'],
                                       lambda url: self._publish_authorization(cancel, key, url), cancel)
            tools = self._request(row, lambda client: client.tools(), credential=session['access'], cancel=cancel)
            if cancel.is_set():
                raise PermissionError('授权已取消。')
            secret_store.set_provider('connector.mcp.' + identifier, json.dumps(session))
            return tools

        self._connect_browser(identifier, connect, '正在准备浏览器授权…')

    def _connect_browser(self, identifier, connect, stage):
        if self._busy or not self._loaded:
            return
        cancel = self._pair_cancel = threading.Event()
        self._oauth_identifier = identifier
        self._login = {'family': self._presets[identifier]['key'], 'stage': stage}

        def work():
            if self.demo:
                raise PermissionError('预览模式不会发起外部授权。')
            return identifier, connect(cancel)

        self._start('oauth', work)

    @Slot(QUrl)
    def connectVault(self, folder):
        value = folder.toLocalFile()
        if self._busy or not self._loaded or not value:
            return
        row = self._presets['preset.obsidian']
        self._states[row['id']] = '正在准备本机服务'

        def connect():
            vault = vault_path(value)
            tools = self._request(row, lambda client: client.tools(), vault=vault)
            if not self.demo:
                SessionStore.atomic(self.path.with_name('mcp-obsidian.json'), vault)
            return vault, tools

        self._start('vault', connect)

    @Slot()
    def disconnectGoogle(self):
        if self._workspace_pairing is not None:
            def disconnect():
                self._workspace_pairing.disconnect_google()
                return self._read_accounts()
            self._start('disconnect', disconnect)

    @Slot(str, str)
    def savePreset(self, identifier, token):
        row = self._presets.get(identifier)
        if not row or not row['credential'] or (row['oauth'] and not row.get('dualAuth')) or self._busy or not self._loaded:
            return
        token = token.strip()
        if len(token) > 8192 or not re.fullmatch(r'[!-~]+', token):
            self._error = '请输入有效的单行凭据。'
            self.changed.emit()
            return

        def save():
            if self.demo:
                raise PermissionError('预览模式不会启动连接器。')
            if row['key'] == 'square':
                request_json('https://connect.squareupsandbox.com/v2/locations', 'GET',
                             {'Authorization': 'Bearer ' + token, 'Square-Version': '2026-09-16'},
                             opener=open_response, cancel_event=self._cancel)
            elif row['key'] == 'discogs':
                request_json('https://api.discogs.com/oauth/identity', 'GET',
                             {'Authorization': 'Discogs token=' + token, 'User-Agent': 'Forge-MCP/1.0'},
                             opener=open_response, cancel_event=self._cancel)
            tools = self._request(row, lambda client: client.tools(), credential=token)
            secret_store.set_provider('connector.mcp.' + identifier, token)
            return identifier, tools, True

        self._start('preset', save)

    @Slot(str)
    def clearPreset(self, identifier):
        if identifier not in self._preset_credentials:
            return

        def clear():
            if not self.demo:
                secret_key = 'connector.mcp.' + identifier
                if identifier == 'preset.wecom':
                    WeCom.disconnect(self.home, secret_store.get_provider(secret_key) or '')
                secret_store.delete_provider(secret_key)
            return identifier, [], False

        self._start('preset', clear)

    def _read_servers(self):
        if self.demo:
            return []
        try:
            rows = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return []
        if not isinstance(rows, list):
            raise ValueError('MCP 配置文件格式无效。')
        output = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier, name, url = (row.get(key) for key in ('id', 'name', 'url'))
            if not all(isinstance(value, str) for value in (identifier, name, url)):
                continue
            if not re.fullmatch(r'[a-f0-9]{32}', identifier):
                continue
            try:
                url = endpoint_url(url)
            except ValueError:
                continue
            output.append({'id': identifier, 'name': name[:64], 'url': url,
                           'auth': 'dingtalk' if row.get('auth') == 'dingtalk' and self._dingtalk_url(url) else ''})
        return output

    @staticmethod
    def _dingtalk_url(url):
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.netloc.lower() == 'mcp-gw.dingtalk.com'
                and re.fullmatch(r'/mserver/[a-zA-Z0-9_-]+', parsed.path) is not None)

    @Slot(str, str, str, str)
    @Slot(str, str, str, str, str)
    def save(self, identifier, name, url, token, auth=''):
        if self._busy or not self._loaded:
            return
        try:
            name = name.strip()
            if not name or len(name) > 64 or any(c in name for c in '\n\r'):
                raise ValueError('名称须为 1 到 64 个字符。')
            url = endpoint_url(url)
            if len(token) > 8192 or any(c in token for c in '\n\r'):
                raise ValueError('Bearer 令牌必须为单行文本。')
            old = next((row for row in self._servers if row['id'] == identifier), None)
            if identifier and old is None:
                raise ValueError('这个 MCP 接口已不存在。')
            if auth:
                if auth != 'dingtalk' or not self._dingtalk_url(url):
                    raise ValueError('钉钉地址应为 https://mcp-gw.dingtalk.com/mserver/…，请把 key 单独填入凭据栏。')
                if any(c.isspace() for c in token) or (not token and (old is None or old['url'] != url)):
                    raise ValueError('请输入有效的钉钉 MCP Key。')
            identifier = identifier or uuid.uuid4().hex
            row = {'id': identifier, 'name': str(redact(name)), 'url': url, 'auth': auth}
            rows = [row if item['id'] == identifier else item for item in self._servers]
            if old is None:
                rows.append(row)
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return

        def save():
            if not self.demo:
                key = 'connector.mcp.' + identifier
                previous_token = secret_store.get_provider(key) if old is not None else None
                same_url = old is not None and old['url'] == url and old.get('auth', '') == auth
                if not same_url and previous_token:
                    secret_store.delete_provider(key)
                try:
                    SessionStore.atomic(self.path, rows)
                except OSError:
                    if not same_url and previous_token:
                        secret_store.set_provider(key, previous_token)
                    raise
                if token:
                    try:
                        secret_store.set_provider(key, token)
                    except OSError:
                        SessionStore.atomic(self.path, self._servers)
                        if not same_url and previous_token:
                            secret_store.set_provider(key, previous_token)
                        raise
            return rows, identifier

        self._start('save', save)

    @Slot(str)
    def remove(self, identifier):
        if not any(row['id'] == identifier for row in self._servers):
            return
        rows = [row for row in self._servers if row['id'] != identifier]

        def remove():
            if not self.demo:
                key = 'connector.mcp.' + identifier
                if secret_store.get_provider(key):
                    secret_store.delete_provider(key)
                SessionStore.atomic(self.path, rows)
            return rows, identifier

        self._start('save', remove)

    @Slot(str)
    def test(self, identifier):
        if self._busy:
            return
        row = self._find(identifier)
        if row is None or row.get('setup'):
            return
        self._states[identifier] = '正在准备本机服务' if row.get('local') else '连接中'
        self._tools.pop(identifier, None)

        self._start(identifier, lambda: self._request(row, lambda client: client.tools()))

    def _find(self, identifier):
        return self._presets.get(identifier) or next(
            (row for row in self._servers + self._directory if row['id'] == identifier), None)

    def _request(self, row, action, *, name=None, arguments=None, workspace='', credential=None,
                 vault=None, cancel=None):
        cancel = self._cancel if cancel is None else cancel
        if 'family' in row:
            if self._workspace_pairing is None:
                raise PermissionError('请先配对这个连接器。')
            return self._workspace_pairing.request(row, cancel, name, arguments, workspace)
        if row.get('setup'):
            raise ValueError('请按连接文档配置服务。')
        key = row.get('key', '')
        if key and self.demo:
            raise PermissionError('预览模式不会启动连接器。')
        secret_key = 'connector.mcp.' + row['id']
        token = credential if credential is not None else (
            '' if self.demo else secret_store.get_provider(secret_key))
        if row.get('oauth') and not token:
            raise PermissionError('请先在浏览器完成授权。')
        if arguments is not None and (redact(arguments) != arguments or 'SECRET_REF' in json.dumps(arguments)):
            raise PermissionError('工具参数不能包含 Forge 凭据。')
        options = {}
        if row.get('auth') == 'dingtalk':
            if not self._dingtalk_url(row['url']) or not token:
                raise PermissionError('请填写钉钉 MCP 地址和 Key。')
            options['query_auth'] = ('key', {})
        if key:
            if row['credential'] and not row['optionalCredential'] and not token:
                raise PermissionError('请先填写这个服务的凭据。')
            if key == 'feishu':
                stored = credential if credential is not None else json.loads(token)
                fresh = refresh_feishu(stored, cancel)
                if fresh is not stored and credential is None:
                    self._replace_session(secret_key, token, fresh)
                app_id, app_secret, token = fresh['client'], fresh['secret'], fresh['access']
            elif row['loginMode'] == 'google-cloud':
                session = credential if credential is not None else json.loads(token)
                if session['expires_at'] <= time.time() + 60:
                    if not session['refresh_token']:
                        raise PermissionError('Google Cloud 授权已过期，请重新授权。')
                    issued = request_json(GOOGLE_TOKEN, 'POST', FORM, {
                        'client_id': session['client'], 'client_secret': session['client_secret'],
                        'grant_type': 'refresh_token', 'refresh_token': session['refresh_token'],
                    }, form=True, opener=open_response, cancel_event=cancel)
                    session = {**session, **WorkspacePairing._google_tokens(
                        issued, session['client_secret'], session['refresh_token'])}
                    if credential is None:
                        self._replace_session(secret_key, token, session)
                token = session['access_token']
            elif row['loginMode'] == 'oauth' and credential is None and (
                    not row.get('dualAuth') or token.startswith('{')):
                stored = json.loads(token)
                fresh = refresh_preset(key, row['url'], stored, cancel)
                if fresh is not stored:
                    self._replace_session(secret_key, token, fresh)
                token = fresh['access']
            _, _, _, _, header, prefix, _ = PRESETS[key]
            options = {'credential_header': header, 'credential_prefix': prefix}
            if key == 'aws':
                options['allowed_tools'] = AWS_KNOWLEDGE_TOOLS
            if key in QUERY_KEYS:
                options['query_auth'] = QUERY_KEYS[key]
            if key == 'tencent-meeting':
                config = meeting_config(self.home, cancel)
                options['fixed_headers'] = {'X-Skill-Version': config['version']}
                options['plain'] = True
            if row['loginMode'] == 'google-cloud':
                options['fixed_headers'] = {'X-Goog-User-Project': session['project']}
            if key == 'developer-knowledge':
                options['plain'] = True
        if key == 'wecom':
            client = WeCom(self.home, token, cancel)
        elif key == 'baidu-netdisk':
            client = McpSse(row['url'], token, cancel)
        elif row.get('local'):
            node, entry, root = local_command(self.home, key, cancel)
            args, env = [], {}
            if key == 'obsidian':
                target = vault or self._vault
                if not target:
                    raise ValueError('请先选择 Obsidian 仓库。')
                args = ['serve', '--vault', 'notes=' + vault_path(target)]
            elif key == 'discogs':
                env = {'DISCOGS_PERSONAL_ACCESS_TOKEN': token}
            elif key == 'square':
                args, env = ['start'], {'ACCESS_TOKEN': token, 'SANDBOX': 'true', 'DISALLOW_WRITES': 'true'}
            elif key == 'feishu':
                args = ['mcp', '--token-mode', 'user_access_token', '--language', 'zh']
                env = {'APP_ID': app_id, 'APP_SECRET': app_secret, 'USER_ACCESS_TOKEN': token}
            elif key == 'yuque':
                env = {'YUQUE_PERSONAL_TOKEN': token}
            elif key == 'minimax-cn':
                env = {'MINIMAX_API_KEY': token, 'MINIMAX_API_HOST': 'https://api.minimax.chat',
                       'MINIMAX_MCP_BASE_PATH': str(root), 'MINIMAX_RESOURCE_MODE': 'url'}
            client = McpStdio(node, [str(entry), *args], root, cancel, env=env,
                              allowed_tools=OBSIDIAN_READ_TOOLS if key == 'obsidian' else None)
        else:
            client = McpHttp(row['url'], token, cancel, **options)
        secret = SecretScope()
        try:
            if token:
                secret.reference(token)
                if key == 'feishu':
                    secret.reference(app_secret)
                if row.get('loginMode') == 'google-cloud':
                    secret.reference(session['client_secret'])
                if (options.get('query_auth') or key == 'baidu-netdisk') and quote_plus(token) != token:
                    secret.reference(quote_plus(token))
            return secret.protect(action(client))
        except HTTPError:
            raise
        except Exception as exc:
            sanitized = str(redact(str(exc)))
            if sanitized != str(exc):
                raise RuntimeError(sanitized) from None
            raise
        finally:
            client.close()
            secret.close()

    @staticmethod
    def _replace_session(key, previous, current):
        if secret_store.get_provider(key) != previous:
            raise PermissionError('连接凭据已变更，请重试。')
        secret_store.set_provider(key, json.dumps(current))

    @Slot(str, str, str)
    @Slot(str, str, str, str)
    def invoke(self, identifier, name, source, workspace=''):
        if self._busy:
            return
        row = self._find(identifier)
        if row is None or name not in {tool['name'] for tool in self._tools.get(identifier, [])}:
            return
        self._result = ''
        try:
            if len(source.encode('utf-8')) > 64 * 1024:
                raise ValueError('工具参数不能超过 64 KiB。')
            arguments = json.loads(source)
            if not isinstance(arguments, dict):
                raise ValueError('工具参数必须是 JSON 对象。')
        except (ValueError, RecursionError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return
        self._start('invoke', lambda: self._request(row, lambda client: client.invoke(name, arguments),
                                                    name=name, arguments=arguments, workspace=workspace))

    @Slot(str)
    def inspectWeCom(self, name):
        identifier = 'preset.wecom'
        if self._busy or name not in {tool['name'] for tool in self._tools.get(identifier, [])}:
            return
        self._tool_schema = ''
        self._start('schema', lambda: self._request(self._presets[identifier], lambda client: client.schema(name)))

    def chat_tools(self, workspace, *, remote_only=False):
        """Snapshot discovered tools for the current conversation."""
        output = {}
        if self.demo or self._busy:
            return output
        for identifier, tools in self._tools.items():
            row = self._find(identifier)
            if (not row or row.get('family') or row.get('setup')
                    or row.get('key') in {'google-maps', 'google-maps-code'}
                    or (row.get('key') and not self._preset_ready(row))):
                continue
            if remote_only and not self._remote_chat_endpoint(row):
                continue
            for tool in tools:
                if row.get('key') == 'tencent-meeting' and tool['name'] not in MEETING_READ_TOOLS:
                    continue
                if len(output) >= 64:
                    return output
                key = hashlib.sha256((identifier + ':' + tool['name']).encode()).hexdigest()[:20]
                slug = re.sub(r'[^a-zA-Z0-9_-]', '_', tool['name'])[:36]
                name = f'mcp_{slug}_{key}'
                schema = tool.get('schema', {})
                if schema.get('type', 'object') != 'object':
                    continue
                function = {'name': name, 'description': f"{row['name']} · {tool['description']}",
                            'parameters': {**schema, 'type': 'object'}}
                output[name] = ({'type': 'function', 'function': function},
                                (row, tool['name'], workspace))
        return output

    @staticmethod
    def _remote_chat_endpoint(row):
        from ipaddress import ip_address
        if row.get('local') or row.get('key') == 'wecom':
            return False
        try:
            endpoint = urlsplit(row.get('url', ''))
            host = (endpoint.hostname or '').rstrip('.')
        except ValueError:
            return False
        if endpoint.scheme != 'https' or not host:
            return False
        try:
            return ip_address(host).is_global
        except ValueError:
            return '.' in host and not host.endswith(('.localhost', '.local', '.lan'))

    def call_chat_tool(self, target, arguments, cancel):
        row, name, workspace = target
        if self._cancel.is_set() or cancel.is_set():
            return {'ok': False, 'error': '连接器调用已取消。',
                    'meta': {'execution_state': 'not_started'}}
        if row.get('key') == 'tencent-meeting' and name not in MEETING_READ_TOOLS:
            return {'ok': False, 'error': '请在腾讯会议连接器详情确认并执行会议操作。',
                    'meta': {'execution_state': 'not_started'}}
        if self._find(row['id']) != row or not any(tool['name'] == name for tool in self._tools.get(row['id'], [])):
            return {'ok': False, 'error': 'MCP 服务配置已变更。请刷新工具清单后重试。',
                    'meta': {'execution_state': 'not_started'}}
        if len(json.dumps(arguments, ensure_ascii=False).encode('utf-8')) > 64 * 1024:
            return {'ok': False, 'error': 'MCP 工具参数超过 64 KiB。',
                    'meta': {'execution_state': 'not_started'}}
        result = self._request(row, lambda client: client.invoke(name, arguments),
                               name=name, arguments=arguments, workspace=workspace, cancel=cancel)
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 128 * 1024:
            return {'ok': False, 'error': 'MCP 结果超过对话可接收的大小。',
                    'meta': {'execution_state': 'completed'}}
        receipt = {'ok': not bool(result.get('isError')), 'result': result,
                   'meta': {'execution_state': 'completed'}}
        if not receipt['ok']:
            receipt['error'] = 'MCP 工具返回错误。'
        return receipt

    def _apply_accounts(self, accounts):
        for row in self._directory:
            family = row['family']
            if accounts.get(family) != self._accounts.get(family):
                self._tools.pop(row['id'], None)
                self._states.pop(row['id'], None)
        self._accounts = accounts

    @Slot(object)
    def _finish(self, outcome):
        if self._cancel.is_set():
            return
        kind, result, self._error = outcome
        self._busy = False
        if kind == 'load':
            if result is not None:
                self._loaded = True
                self._servers, (accounts, self._accounts_error), self._preset_credentials, self._vault = result
                self._apply_accounts(accounts)
        elif kind == 'pair':
            self._login = {}
            self._pair_cancel = None
            if result is not None:
                family, accounts, self._pair_errors = result
                self._apply_accounts(accounts)
                for row in self._directory:
                    if row['family'] == family:
                        self._tools.pop(row['id'], None)
                        self._states.pop(row['id'], None)
        elif kind == 'oauth':
            self._login = {}
            self._pair_cancel = None
            identifier, self._oauth_identifier = self._oauth_identifier, ''
            if result is not None:
                identifier, tools = result
                self._preset_credentials.add(identifier)
                self._tools[identifier] = tools
                self._states[identifier] = f'{len(tools)} 项工具'
            elif identifier not in self._preset_credentials:
                self._states[identifier] = '连接失败'
        elif kind == 'vault':
            if result is not None:
                self._vault, tools = result
                self._tools['preset.obsidian'] = tools
                self._states['preset.obsidian'] = f'{len(tools)} 项工具'
            else:
                self._states['preset.obsidian'] = '连接失败'
        elif kind == 'accounts':
            if result is not None:
                accounts, self._accounts_error = result
                self._apply_accounts(accounts)
        elif kind == 'disconnect':
            if result is not None:
                self._apply_accounts(result)
        elif kind == 'save':
            if result is not None:
                self._servers, identifier = result
                self._states.pop(identifier, None)
                self._tools.pop(identifier, None)
                if any(row['id'] == identifier for row in self._servers):
                    self.serverSaved.emit(identifier)
        elif kind == 'preset':
            if result is not None:
                identifier, tools, configured = result
                if configured:
                    self._preset_credentials.add(identifier)
                    self._tools[identifier] = tools
                    self._states[identifier] = f'{len(tools)} 项工具'
                else:
                    self._preset_credentials.discard(identifier)
                    self._tools.pop(identifier, None)
                    self._states.pop(identifier, None)
        elif kind == 'schema':
            if result is not None:
                self._tool_schema = json.dumps(result, ensure_ascii=False, indent=2)[:64000]
        elif kind == 'invoke':
            if result is not None:
                self._result = json.dumps(result, ensure_ascii=False, indent=2)[:64000]
        else:
            self._states[kind] = '连接失败' if self._error else f'{len(result)} 项工具'
            if result is not None:
                self._tools[kind] = result
        self.changed.emit()

    def shutdown(self):
        self._cancel.set()
        if self._pair_cancel is not None:
            self._pair_cancel.set()
        if self.saving and self._worker is not None:
            self._worker.join(2)
