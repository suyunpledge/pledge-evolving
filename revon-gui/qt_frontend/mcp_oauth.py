"""Browser OAuth for built-in connectors."""
import base64
import hashlib
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import time
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from revon.desktop_services import request_json
from revon.http_transport import open_response


def browser_code(authorize, notify, cancel, *, redirect_port=0):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    reply = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(1)
            super().setup()

        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                params = parse_qs(parsed.query, max_num_fields=20) if len(self.path) < 8192 else {}
            except ValueError:
                self.send_error(400)
                return
            valid = (parsed.path == '/oauth/callback' and params.get('state') == [state]
                     and self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}')
            code = params.get('code', [''])[0]
            if valid:
                reply['code'] = code
            message = ('已收到授权，请回到 revon。' if valid and code else
                       '未完成授权，请回到 revon 查看状态。').encode('utf-8')
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(message)))
            self.end_headers()
            try:
                self.wfile.write(message)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with HTTPServer(('127.0.0.1', redirect_port), Callback) as server:
        server.timeout = 0.25
        redirect = f'http://127.0.0.1:{server.server_port}/oauth/callback'
        url = authorize(redirect, state, challenge)
        if cancel.is_set():
            raise PermissionError('授权已取消。')
        notify(url)
        deadline = time.monotonic() + 300
        while not reply and not cancel.is_set() and time.monotonic() < deadline:
            server.handle_request()
    if cancel.is_set() or not reply.get('code'):
        raise PermissionError('授权已取消、被拒绝或超时。')
    return reply['code'], redirect, verifier


OAUTH = {
    'notion': ('https://mcp.notion.com', 'default'),
}


# The protected endpoint and issuer allowlist are paired with a built-in service.
DISCOVERED_OAUTH = {
    'weibo': ('', frozenset({'cli.weibo.com', 'open.weibo.com', 'weibo.com', 'api.weibo.com', 'passport.weibo.com'})),
    'gitlab': ('mcp', frozenset({'gitlab.com'})),
    'figma': ('mcp:connect', frozenset({'mcp.figma.com', 'api.figma.com', 'www.figma.com'})),
    'tavily': ('', frozenset({'mcp.tavily.com', 'auth.tavily.com'})),
    'netlify': ('', frozenset({'mcp.netlify.com', 'app.netlify.com', 'api.netlify.com'})),
    'huggingface': ('', frozenset({'huggingface.co'})),
    'posthog': ('', frozenset({'oauth.posthog.com', 'mcp.posthog.com', 'us.posthog.com', 'eu.posthog.com'})),
}


def _provider_url(value, hosts):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('MCP 授权服务返回了无效地址。')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc not in hosts or parsed.fragment
            or parsed.query or re.search(r'[\s\\]', value)):
        raise ValueError('MCP 授权服务返回了未登记的地址。')
    return value


def _oauth_document(url, hosts, cancel):
    url = _provider_url(url, hosts)
    doc = request_json(url, 'GET', {'Accept': 'application/json'},
                       opener=open_response, cancel_event=cancel)
    if not isinstance(doc, dict):
        raise ValueError('MCP 授权元数据格式无效。')
    return doc


def _discover_oauth(key, resource, cancel):
    preferred_scope, hosts = DISCOVERED_OAUTH[key]
    discovered_scope = ''
    parsed = urlsplit(_provider_url(resource, hosts))
    origin = f'https://{parsed.netloc}'
    metadata = origin + '/.well-known/oauth-protected-resource'
    deadline = threading.Event()

    class Cancel:
        def is_set(self):
            return cancel.is_set() or deadline.is_set()

    timer = threading.Timer(8, deadline.set)
    timer.daemon = True
    timer.start()
    try:
        # The standard challenge can point to a path-specific metadata document.
        request = Request(resource, headers={'Accept': 'application/json, text/event-stream'},
                          data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"revon","version":"1.0"}}}',
                          method='POST')
        request.add_header('Content-Type', 'application/json')
        with open_response(request, timeout=8, cancel_event=Cancel()):
            pass
    except HTTPError as exc:
        challenge = exc.headers.get('WWW-Authenticate', '') if exc.headers else ''
        match = re.search(r'\bresource_metadata="([^"]+)"', challenge)
        if match:
            metadata = _provider_url(match.group(1), hosts)
        scope_match = re.search(r'\bscope="([a-zA-Z0-9:._ -]{1,512})"', challenge)
        if scope_match:
            discovered_scope = scope_match.group(1)
    except (OSError, RuntimeError):
        if cancel.is_set():
            raise
    finally:
        timer.cancel()
    if cancel.is_set():
        raise PermissionError('授权已取消。')
    last = None
    candidates = [metadata] if metadata != origin + '/.well-known/oauth-protected-resource' else [
        metadata + parsed.path.rstrip('/'), metadata]
    for url in dict.fromkeys(candidates):
        try:
            protected = _oauth_document(url, hosts, cancel)
            break
        except RuntimeError as exc:
            last = exc
    else:
        raise RuntimeError('无法读取官方 MCP 授权信息。' + (f' {last}' if last else '')) from None
    identity = _provider_url(protected.get('resource'), hosts)
    if identity.rstrip('/') != resource.rstrip('/'):
        raise ValueError('MCP 授权资源与连接地址不一致。')
    issuers = protected.get('authorization_servers')
    if not isinstance(issuers, list) or not issuers:
        raise ValueError('MCP 服务未发布授权发行方。')
    issuer = _provider_url(issuers[0], hosts)
    issuer_url = urlsplit(issuer)
    issuer_base = f'https://{issuer_url.netloc}'
    path = issuer_url.path.rstrip('/')
    try:
        auth = _oauth_document(issuer_base + '/.well-known/oauth-authorization-server' + path, hosts, cancel)
    except RuntimeError:
        auth = _oauth_document(issuer.rstrip('/') + '/.well-known/openid-configuration', hosts, cancel)
    if _provider_url(auth.get('issuer'), hosts).rstrip('/') != issuer.rstrip('/'):
        raise ValueError('MCP 授权发行方校验失败。')
    if 'S256' not in auth.get('code_challenge_methods_supported', []):
        raise ValueError('MCP 授权服务未发布 S256 PKCE 支持。')
    methods = auth.get('token_endpoint_auth_methods_supported', ['client_secret_basic'])
    method = next((option for option in ('none', 'client_secret_post', 'client_secret_basic') if option in methods), None)
    if method is None:
        raise ValueError('MCP 授权服务未发布兼容的令牌认证方式。')
    return (
        _provider_url(auth.get('registration_endpoint'), hosts),
        _provider_url(auth.get('authorization_endpoint'), hosts),
        _provider_url(auth.get('token_endpoint'), hosts),
        preferred_scope or discovered_scope,
        method,
    )


def _dynamic_token(url, data, cancel, client_id, secret, method):
    fields = {**data, 'client_id': client_id}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    if method == 'client_secret_post':
        fields['client_secret'] = secret
    elif method == 'client_secret_basic':
        credential = urlencode({'u': client_id})[2:] + ':' + urlencode({'p': secret})[2:]
        headers['Authorization'] = 'Basic ' + base64.b64encode(credential.encode()).decode()
        fields.pop('client_id')
    return request_json(url, 'POST', headers, fields, form=True, opener=open_response, cancel_event=cancel)


def _authorize_discovered(key, resource, notify, cancel):
    register, authorize_url, token_url, scope, method = _discover_oauth(key, resource, cancel)
    identity = {}

    def authorize(redirect, state, challenge):
        result = _request(register, {'client_name': 'revon', 'redirect_uris': [redirect],
                                     'grant_types': ['authorization_code', 'refresh_token'],
                                     'response_types': ['code'], 'token_endpoint_auth_method': method,
                                     **({'scope': scope} if scope else {})}, cancel)
        client = result.get('client_id')
        client_secret = result.get('client_secret', '')
        if not isinstance(client, str) or not client or (method != 'none' and not client_secret):
            raise PermissionError('官方服务未能为 revon 注册 OAuth 客户端。')
        identity.update(client=client, secret=client_secret)
        params = {'response_type': 'code', 'client_id': client, 'redirect_uri': redirect, 'state': state,
                  'code_challenge_method': 'S256', 'code_challenge': challenge, 'resource': resource}
        if scope:
            params['scope'] = scope
        return authorize_url + '?' + urlencode(params)

    code, redirect, verifier = browser_code(authorize, notify, cancel)
    result = _dynamic_token(token_url, {'grant_type': 'authorization_code', 'code': code,
                                       'redirect_uri': redirect, 'code_verifier': verifier, 'resource': resource},
                            cancel, identity['client'], identity['secret'], method)
    return {**_session(result, identity['client']), 'secret': identity['secret'],
            'method': method, 'token_url': token_url}


def _refresh_discovered(key, resource, stored, cancel):
    url = _provider_url(stored['token_url'], DISCOVERED_OAUTH[key][1])
    result = _dynamic_token(url, {'grant_type': 'refresh_token', 'refresh_token': stored['refresh'],
                                  'resource': resource}, cancel, stored['client'], stored['secret'], stored['method'])
    return {**stored, **_session(result, stored['client'], stored['refresh'])}


def _request(url, body, cancel, *, form=False):
    return request_json(url, 'POST', {
        'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
    }, body, form=form, opener=open_response, cancel_event=cancel)


def _session(result, client_id, previous_refresh=''):
    token = result.get('access_token')
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise PermissionError('授权服务器未返回有效的访问令牌。')
    return {'access': token, 'client': client_id, 'refresh': result.get('refresh_token') or previous_refresh,
            'expires': time.time() + float(result.get('expires_in', 3600))}


def authorize_preset(key, resource, notify, cancel):
    if key in DISCOVERED_OAUTH:
        return _authorize_discovered(key, resource, notify, cancel)
    origin, scope = OAUTH[key]
    client = ''

    def authorize(redirect, state, challenge):
        nonlocal client
        registered = _request(origin + '/register', {
            'client_name': 'revon',
            'redirect_uris': [redirect],
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
            'token_endpoint_auth_method': 'none',
        }, cancel)
        client = registered.get('client_id')
        if not isinstance(client, str) or not client:
            raise PermissionError('服务未能为 revon 注册 OAuth 客户端。')
        params = {'response_type': 'code', 'client_id': client, 'redirect_uri': redirect, 'state': state,
                  'code_challenge_method': 'S256', 'code_challenge': challenge, 'resource': resource}
        if scope:
            params['scope'] = scope
        return origin + '/authorize?' + urlencode(params)

    code, redirect, verifier = browser_code(authorize, notify, cancel)
    result = _request(origin + '/token', {
        'grant_type': 'authorization_code', 'client_id': client, 'code': code, 'redirect_uri': redirect,
        'code_verifier': verifier, 'resource': resource,
    }, cancel, form=True)
    return _session(result, client)


def refresh_preset(key, resource, stored, cancel):
    if stored['expires'] > time.time() + 45:
        return stored
    if not stored.get('refresh'):
        raise PermissionError('登录已过期，请重新连接。')
    if key in DISCOVERED_OAUTH:
        return _refresh_discovered(key, resource, stored, cancel)
    origin, _ = OAUTH[key]
    result = _request(origin + '/token', {
        'grant_type': 'refresh_token', 'client_id': stored['client'], 'refresh_token': stored['refresh'],
        'resource': resource,
    }, cancel, form=True)
    return _session(result, stored['client'], stored['refresh'])

FEISHU_REDIRECT = 'http://127.0.0.1:3000/oauth/callback'
FEISHU_TOKEN = 'https://open.feishu.cn/open-apis/authen/v2/oauth/token'
BAIDU_AUTHORIZE = ('https://openapi.baidu.com/oauth/2.0/authorize?'
                   'client_id=QHOuRXiepJBMjtk0esLhrPoNlQyYd0mF&redirect_uri=oob&'
                   'response_type=token&scope=basic%2Cnetdisk')


def baidu_access_token(value):
    """Accept the raw token or its official out-of-band result URL."""
    value = value.strip()
    if len(value) > 8192:
        raise ValueError('百度网盘令牌过长。')
    if value.startswith(('https://', 'http://')):
        parsed = urlsplit(value)
        if parsed.netloc != 'openapi.baidu.com' or parsed.path != '/oauth/2.0/login_success':
            raise ValueError('请粘贴百度官方授权结果地址，或单独粘贴 access_token。')
        fields = parse_qs(parsed.fragment or parsed.query, max_num_fields=16)
        value = fields.get('access_token', [''])[0]
    if not re.fullmatch(r'[A-Za-z0-9._~-]{8,4096}', value):
        raise ValueError('请粘贴百度网盘授权返回的 access_token。')
    return value


def feishu_credentials(app_id, app_secret):
    app_id, app_secret = app_id.strip(), app_secret.strip()
    if not re.fullmatch(r'cli_[A-Za-z0-9]{6,64}', app_id):
        raise ValueError('请输入飞书开放平台的 App ID（cli_ 开头）。')
    if not app_secret or len(app_secret) > 256 or not app_secret.isascii() or any(c.isspace() for c in app_secret):
        raise ValueError('请输入有效的飞书 App Secret。')
    return app_id, app_secret


def feishu_scopes(value):
    scopes = list(dict.fromkeys(value.split()))
    if len(scopes) > 49 or any(not re.fullmatch(r'[a-zA-Z0-9_:.-]{1,100}', scope) for scope in scopes):
        raise ValueError('请输入以空格分隔的飞书用户权限标识，最多 49 项。')
    if scopes and 'offline_access' not in scopes:
        scopes.append('offline_access')
    return ' '.join(scopes)


def authorize_feishu(app_id, app_secret, notify, cancel, *, scopes=''):
    def authorize(redirect, state, challenge):
        params = {'client_id': app_id, 'redirect_uri': redirect, 'response_type': 'code',
                  'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        if scopes:
            params['scope'] = scopes
        return 'https://accounts.feishu.cn/open-apis/authen/v1/authorize?' + urlencode(params)

    try:
        code, redirect, verifier = browser_code(authorize, notify, cancel, redirect_port=3000)
    except OSError:
        raise OSError('飞书授权需要本机 3000 端口，请关闭占用该端口的程序后重试。') from None
    return _feishu_session(app_id, app_secret, {
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect,
        'code_verifier': verifier,
    }, cancel)


def refresh_feishu(stored, cancel):
    if stored['expires'] > time.time() + 45:
        return stored
    if not stored.get('refresh'):
        raise PermissionError('飞书登录已过期，请重新授权。')
    fresh = _feishu_session(stored['client'], stored['secret'], {
        'grant_type': 'refresh_token', 'refresh_token': stored['refresh'],
    }, cancel, previous_refresh=stored['refresh'])
    return {**fresh, 'scopes': stored.get('scopes', '')}


def _feishu_session(app_id, app_secret, data, cancel, previous_refresh=''):
    result = _request(FEISHU_TOKEN, {**data, 'client_id': app_id, 'client_secret': app_secret}, cancel)
    if result.get('code') not in (None, 0):
        raise PermissionError('飞书授权失败，请检查自建应用权限、回调地址，或重新登录。')
    return {**_session(result, app_id, previous_refresh), 'secret': app_secret}
