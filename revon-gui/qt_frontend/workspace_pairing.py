"""Built-in Workspace MCP and Microsoft Graph connectors."""
import json
import re
import time
from urllib.parse import urlencode

import secret_store
from config_model import load_user_layer
from revon.config import Config
from revon.desktop_services import ConnectorService, DesktopStore, microsoft_operations, microsoft_operation_allowed
from revon.http_transport import open_response
from revon.policy import Policy
from revon.secrets import SecretScope, redact
from revon.tools import ToolContext
from .mcp_http import McpHttp
from .mcp_oauth import browser_code


GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
GOOGLE_SCOPE = 'https://www.googleapis.com/auth/'
FORM = {'Content-Type': 'application/x-www-form-urlencoded'}
MICROSOFT_LOGIN = 'https://microsoft.com/devicelogin'

GOOGLE = {
    'gmail': ('Gmail', '邮件与会话', 'message-square', 'gmailmcp', ('gmail.readonly',)),
    'drive': ('Google Drive', '云端文件', 'folder', 'drivemcp', ('drive.readonly',)),
    'docs': ('Google Docs', '文档', 'file-text', 'docsmcp', ('documents.readonly',)),
    'sheets': ('Google Sheets', '电子表格', 'panels-top-left', 'sheetsmcp', ('spreadsheets.readonly',)),
    'slides': ('Google Slides', '演示文稿', 'layers', 'slidesmcp', ('presentations.readonly',)),
    'calendar': ('Google Calendar', '日历与日程', 'clock-3', 'calendarmcp',
                 ('calendar.calendarlist.readonly', 'calendar.events.readonly')),
    'chat': ('Google Chat', '空间与消息', 'message-square', 'chatmcp',
             ('chat.spaces.readonly', 'chat.memberships.readonly', 'chat.messages.readonly')),
    'people': ('Google Contacts', '联系人与个人资料', 'user-round', 'people',
               ('contacts.readonly', 'userinfo.profile')),
    'universal-search': ('Google Workspace 搜索', '跨 Drive、Gmail、日历和 Chat 搜索', 'search',
                         'workspacemcp', ('drive.readonly', 'gmail.readonly', 'calendar.readonly',
                                          'chat.messages.readonly')),
    'classroom': ('Google Classroom', '课程列表', 'book-open', 'classroommcp', ('classroom.courses.readonly',)),
}
MICROSOFT = {
    'outlook': ('Outlook 邮件', '邮件与草稿', 'message-square'),
    'calendar': ('Outlook 日历', '日程与事件', 'clock-3'),
    'onedrive': ('OneDrive', '云端文件', 'folder'),
    'sharepoint': ('SharePoint', '站点与共享文件', 'globe'),
    'teams': ('Microsoft Teams', '团队、频道与消息', 'message-square'),
    'excel': ('Microsoft Excel', '工作簿与单元格', 'panels-top-left'),
}
FAMILIES = {'google': 'Google Workspace', 'microsoft': 'Microsoft 365'}
ICONS = {
    'google': {'gmail': 'gmail', 'drive': 'google-drive', 'docs': 'google-docs',
               'sheets': 'google-sheets', 'slides': 'google-slides', 'calendar': 'google-calendar',
               'chat': 'google-chat', 'people': 'google-contacts', 'classroom': 'google-classroom', 'universal-search': 'google'},
    'microsoft': {'outlook': 'outlook', 'calendar': 'outlook-calendar', 'onedrive': 'onedrive',
                  'sharepoint': 'sharepoint', 'teams': 'teams', 'excel': 'excel'},
}


def directory():
    return [
        {'id': f'{family}.{key}', 'family': family, 'group': key,
         'name': name, 'detail': detail, 'glyph': glyph, 'icon': ICONS[family][key],
         'url': f'https://{extra[0]}.googleapis.com/mcp/v1' if extra else ''}
        for family, apps in (('google', GOOGLE), ('microsoft', MICROSOFT))
        for key, (name, detail, glyph, *extra) in apps.items()
    ]


def google_scopes(groups):
    return {GOOGLE_SCOPE + scope for group in groups for scope in GOOGLE[group][4]}


def google_authorize(client_id, client_secret, scopes, notify, cancel, request):
    def authorize(redirect, state, challenge):
        return GOOGLE_AUTH + '?' + urlencode({
            'client_id': client_id, 'redirect_uri': redirect, 'response_type': 'code',
            'scope': ' '.join(sorted(scopes)), 'state': state, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'access_type': 'offline', 'prompt': 'consent',
        })

    code, redirect, verifier = browser_code(authorize, notify, cancel)
    return request(GOOGLE_TOKEN, 'POST', FORM, {
        'client_id': client_id, 'client_secret': client_secret, 'code': code,
        'code_verifier': verifier, 'redirect_uri': redirect, 'grant_type': 'authorization_code',
    }, form=True, cancel_event=cancel)


class WorkspacePairing:
    def __init__(self, home, *, transport=None, authorize=google_authorize, mcp_client=McpHttp):
        self.home = home
        self.authorize_google = authorize
        self.mcp_client = mcp_client
        self.store = DesktopStore(home / 'desktop-services.sqlite3')
        options = {'transport': transport} if transport else {}
        self.microsoft = ConnectorService(self.store, self._credential, save_credential=self._save,
                                          response_opener=open_response, **options)
        self.entries = {row['id']: row for row in directory()}

    @staticmethod
    def _credential(family):
        return secret_store.get_provider('connector.' + family)

    @staticmethod
    def _save(family, value):
        secret_store.set_provider('connector.' + family, value)

    def _google_secret(self):
        raw = self._credential('google')
        return json.loads(raw) if raw else {}

    def snapshot(self):
        data = {}
        for family in FAMILIES:
            conf = self.store.account(family)
            connected = not conf.get('updating') and bool(self._credential(family))
            scopes = conf.get('scopes', []) if connected and conf.get('authenticated') is not False else []
            selected = conf.get('paired_services', MICROSOFT if family == 'microsoft' else ())
            data[family] = {'clientId': conf.get('client_id', ''), 'tenant': conf.get('tenant') or 'common',
                            'hasSecret': bool(self._google_secret().get('client_secret')) if family == 'google' else False,
                            'paired': [group for group in selected if self._supports(family, group, scopes)]}
        return data

    @staticmethod
    def _supports(family, group, scopes):
        if family == 'google':
            return group in GOOGLE and google_scopes([group]) <= set(scopes)
        return group in MICROSOFT and any(ConnectorService._scope_allowed(op.scope, {'scopes': scopes})
                                          for op in microsoft_operations(group).values())

    def pair_google(self, groups, client_id, client_secret, notify, cancel):
        conf = self.store.account('google')
        saved = self._google_secret()
        if client_id == conf.get('client_id'):
            client_secret = client_secret or saved.get('client_secret', '')
            groups = set(groups) | (set(conf.get('paired_services', ())) & GOOGLE.keys())
        if not client_secret:
            raise ValueError('请填写 Google 桌面 OAuth Client Secret。')
        requested = google_scopes(groups)
        tokens = self.authorize_google(client_id, client_secret, requested, notify, cancel, self.microsoft._request)
        granted = set(tokens.get('scope', '').split()) & requested
        paired = sorted(group for group in groups if self._supports('google', group, granted))
        if not paired:
            raise PermissionError('Google 未授予所选连接器所需的权限。')
        data = self._google_tokens(tokens, client_secret)
        self.store.finish_account('google', conf, {'client_id': client_id, 'authenticated': True,
                                  'scopes': sorted(granted), 'paired_services': paired},
                                  lambda: self._commit('google', data, cancel))

    def _commit(self, family, data, cancel):
        if cancel.is_set():
            raise PermissionError('配对已取消。')
        self._save(family, json.dumps(data))

    @staticmethod
    def _google_tokens(tokens, client_secret, refresh_token=''):
        if not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
            raise PermissionError('Google 未返回访问令牌。')
        return {'access_token': tokens['access_token'], 'refresh_token': tokens.get('refresh_token') or refresh_token,
                'client_secret': client_secret, 'expires_at': time.time() + float(tokens['expires_in'])}

    def pair_microsoft(self, groups, client_id, tenant, notify, cancel):
        previous = self.store.account('microsoft')
        same_client = client_id == previous.get('client_id') and tenant == (previous.get('tenant') or 'common')
        selected = set(groups)
        scopes = {op.scope for group in selected for op in microsoft_operations(group).values() if not op.write}
        if same_client and previous.get('authenticated') is not False and self._credential('microsoft'):
            # Keep previous permissions for this application, including write scopes
            # that the user explicitly granted in account settings.
            previous_scopes = previous.get('scopes', [])
            scopes.update(previous_scopes)
            selected.update(group for group in previous.get('paired_services', MICROSOFT)
                            if self._supports('microsoft', group, previous_scopes))
        conf = {'client_id': client_id, 'tenant': tenant, 'scopes': sorted(scopes),
                'paired_services': sorted(selected), 'read': True,
                'write': same_client and previous.get('write') is True,
                'agent': same_client and previous.get('agent') is True}
        self.microsoft.device_login(conf, notify, cancel, expected=previous)

    def _google_token(self, group, cancel):
        conf = self.store.account('google')
        if group not in conf.get('paired_services', ()) or not self._supports('google', group, conf.get('scopes', [])):
            raise PermissionError('请先配对这个 Google 连接器。')
        tokens = self._google_secret()
        if not tokens:
            raise PermissionError('Google 凭据已移除，请重新配对。')
        if self.store.account('google') != conf:
            raise PermissionError('Google 账户已更改，请重试。')
        if tokens['expires_at'] <= time.time() + 60:
            if not tokens['refresh_token']:
                raise PermissionError('Google 授权已过期，请重新配对。')
            fresh = self.microsoft._request(GOOGLE_TOKEN, 'POST', FORM, {
                'client_id': conf['client_id'], 'client_secret': tokens['client_secret'],
                'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
            }, form=True, cancel_event=cancel)
            if fresh.get('scope') and not self._supports('google', group, fresh['scope'].split()):
                raise PermissionError('Google 已撤销此项权限，请重新配对。')
            tokens = self._google_tokens(fresh, tokens['client_secret'], tokens['refresh_token'])
            self.store.finish_account('google', conf, None, lambda: self._commit('google', tokens, cancel))
        if self.store.account('google') != conf:
            raise PermissionError('Google 账户已更改，请重试。')
        return tokens['access_token']

    def request(self, row, cancel, name=None, arguments=None, workspace=''):
        group = row['group']
        if row['family'] == 'google':
            if arguments is not None and (redact(arguments) != arguments or 'SECRET_REF' in json.dumps(arguments)):
                raise PermissionError('工具参数不能包含 revon 凭据。')
            secret = SecretScope()
            try:
                token = self._google_token(group, cancel)
                secret.reference(token)
                # Google's documented Streamable HTTP requests use plain JSON-RPC.
                client = self.mcp_client(row['url'], token, cancel, plain=True)
                try:
                    result = client.tools() if name is None else client.invoke(name, arguments)
                    return secret.protect(result)
                finally:
                    client.close()
            finally:
                secret.close()
        conf = self.store.account('microsoft')
        if (not self._credential('microsoft') or conf.get('updating') or conf.get('authenticated') is False
                or not self._supports('microsoft', group, conf.get('scopes', []))):
            raise PermissionError('请先配对这个 Microsoft 连接器。')
        operations = {
            key: op for key, op in microsoft_operations(group).items()
            if microsoft_operation_allowed(key, conf) and self.microsoft._scope_allowed(op.scope, conf)
            and conf.get('write' if op.write else 'read') is True
        }
        if name is None:
            return [{'name': key, 'description': f'{op.method} {op.route}',
                     'schema': {'type': 'object', 'properties': {field: {'type': 'string'} for field in op.fields},
                                'required': list(op.required)}} for key, op in operations.items()]
        if name not in operations:
            raise PermissionError('这个 Microsoft 工具没有获得授权。')
        cfg = Config()
        cfg.apply_patch(load_user_layer(self.home))
        policy = Policy.from_config(cfg, workspace=workspace, non_interactive=False)
        secret = SecretScope()
        try:
            # The connection detail confirms the specific tool and arguments.
            registry = self.microsoft.registry(approve=lambda *_: True, cancel_event=cancel, agent=False)
            context = ToolContext(policy, policy.workspace, secret_scope=secret, cancellation=cancel)
            result = registry.invoke(name, arguments, context)
            if not result.ok:
                raise RuntimeError(result.error)
            return {'ok': True, 'content': json.loads(result.content)}
        finally:
            secret.close()

    def disconnect_google(self):
        conf = self.store.account('google')
        self.store.finish_account('google', conf, {'client_id': conf.get('client_id', '')},
                                  lambda: secret_store.delete_provider('connector.google'))
