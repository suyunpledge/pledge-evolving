from contextlib import contextmanager
from email.message import Message
from io import BytesIO
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from .mcp_catalog import McpCatalog
from .mcp_presets import AWS_KNOWLEDGE_TOOLS
from .test_backend import until


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        method = request['method']
        self.server.calls.append((method, self.headers.get('Authorization'), self.headers.get('Mcp-Session-Id')))
        identifier = request.get('id')
        reply = {'jsonrpc': '2.0', 'id': identifier}
        if method == 'notifications/initialized':
            self.send_response(202)
            self.end_headers()
            return
        if method == 'initialize':
            reply['result'] = {'protocolVersion': '2025-11-25'}
        elif self.path == '/legacy' and not self.headers.get('Mcp-Session-Id'):
            reply['error'] = {'code': -32022}
        elif method == 'tools/call':
            self.server.tool_call = request['params']
            reply['result'] = {'content': [{'type': 'text', 'text': 'fixture result'}]}
        else:
            cursor = request['params'].get('cursor')
            reply['result'] = {'tools': [{'name': 'second' if cursor else 'first',
                                         'inputSchema': {'type': 'object'}}]}
            if not cursor:
                reply['result']['nextCursor'] = 'page-2'
        sse = method == 'tools/list' and 'result' in reply
        body = json.dumps(reply).encode()
        if sse:
            body = b'event: message\ndata: ' + body + b'\n\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if sse else 'application/json')
        if method == 'initialize':
            self.send_header('Mcp-Session-Id', 'fixture-session')
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()


class McpCatalogTests(unittest.TestCase):
    def test_new_presets_connect_with_provider_headers(self):
        from .mcp_http import McpHttp
        requests = []

        class Remote(McpHttp):
            def tools(self):
                requests.append((self.url, self.headers.copy()))
                return [{'name': 'provider_fixture', 'description': '', 'schema': {}}]

        def authorize(client_id, secret, scopes, notify, cancel, request):
            self.assertEqual(scopes, {'https://www.googleapis.com/auth/cloud-platform'})
            notify('https://accounts.google.com/o/oauth2/v2/auth')
            return {'access_token': 'fixture-google-token', 'refresh_token': 'fixture-refresh', 'expires_in': 3600}

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with (patch('secret_store.SECRETS_FILE', home / 'secrets.json'),
                  patch('qt_frontend.mcp_catalog.McpHttp', Remote),
                  patch('qt_frontend.mcp_catalog.meeting_config', return_value={'version': 'v1.0.15'}),
                  patch('qt_frontend.mcp_catalog.google_authorize', authorize),
                  patch('qt_frontend.mcp_catalog.QDesktopServices') as browser):
                catalog = McpCatalog(home)
                try:
                    catalog.load()
                    until(lambda: catalog.loaded)
                    for key, host in [('zhihu', 'developer.zhihu.com'), ('tencent-meeting', 'meeting.tencent.com')]:
                        catalog.openCredentialPage('preset.' + key)
                        self.assertEqual(browser.openUrl.call_args.args[0].host(), host)
                        catalog.connectWithCredential('preset.' + key, 'fixture-token')
                        until(lambda: not catalog.busy)
                        self.assertFalse(catalog.error, catalog.error)
                    self.assertEqual(requests[0], ('https://developer.zhihu.com/api/mcp/v1', {
                        'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
                        'Authorization': 'Bearer fixture-token'}))
                    self.assertEqual(requests[1][1]['X-Skill-Version'], 'v1.0.15')
                    self.assertEqual(requests[1][1]['X-Tencent-Meeting-Token'], 'fixture-token')
                    catalog.savePreset('preset.google-maps', 'fixture-map-key')
                    until(lambda: not catalog.busy)
                    self.assertEqual(requests[2][1]['X-Goog-Api-Key'], 'fixture-map-key')
                    catalog.connectGoogleCloud('preset.google-bigquery', '100000-fixture.apps.googleusercontent.com',
                                               'fixture-secret', 'fixture-project')
                    until(lambda: not catalog.busy)
                    self.assertFalse(catalog.error, catalog.error)
                    self.assertEqual(requests[-1][1]['X-Goog-User-Project'], 'fixture-project')
                    self.assertEqual(requests[-1][1]['Authorization'], 'Bearer fixture-google-token')
                    catalog.test('preset.google-bigquery')
                    until(lambda: not catalog.busy)
                    self.assertFalse(catalog.error, catalog.error)
                    self.assertEqual(requests[-2], requests[-1])
                finally:
                    catalog.shutdown()

    def test_weibo_oauth_discovery_login_and_refresh(self):
        from . import mcp_oauth

        origin = 'https://cli.weibo.com'
        resource = origin + '/mcp'
        authorization = 'https://open.weibo.com/oauth2/authorize'
        secret = 'registered-fixture-secret'
        seen = []

        @contextmanager
        def challenge(request, **_):
            raise HTTPError(resource, 401, 'Unauthorized', {
                'WWW-Authenticate': f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"'
            }, None)
            yield

        def route(url, method, headers, body=None, **_):
            if url.endswith('oauth-protected-resource/mcp'):
                return {'resource': resource, 'authorization_servers': [origin]}
            if url.endswith('oauth-authorization-server'):
                return {'issuer': origin, 'registration_endpoint': origin + '/register',
                        'authorization_endpoint': authorization, 'token_endpoint': origin + '/token',
                        'code_challenge_methods_supported': ['S256'],
                        'token_endpoint_auth_methods_supported': ['client_secret_post']}
            if url.endswith('/register'):
                self.assertEqual(body['token_endpoint_auth_method'], 'client_secret_post')
                self.assertTrue(body['redirect_uris'][0].startswith('http://127.0.0.1:'))
                return {'client_id': 'client-fixture', 'client_secret': secret}
            self.assertEqual(url, origin + '/token')
            self.assertEqual(body['client_secret'], secret)
            self.assertEqual(body['client_id'], 'client-fixture')
            self.assertEqual(body['resource'], resource)
            seen.append(body['grant_type'])
            if body['grant_type'] == 'authorization_code':
                self.assertEqual(body['code_verifier'], 'fixture-verifier')
                return {'access_token': 'token-fixture', 'refresh_token': 'refresh-fixture', 'expires_in': 8}
            return {'access_token': 'refreshed-fixture', 'expires_in': 3600}

        def login(build, notify, cancel):
            redirect = 'http://127.0.0.1:62345/oauth/callback'
            url = build(redirect, 'state-fixture', 'challenge-fixture')
            self.assertEqual(url.split('?')[0], authorization)
            self.assertEqual(parse_qs(urlsplit(url).query)['state'], ['state-fixture'])
            notify(url)
            return 'code-fixture', redirect, 'fixture-verifier'

        opened = []
        cancel = threading.Event()
        with (patch.object(mcp_oauth, 'open_response', challenge), patch.object(mcp_oauth, 'request_json', route),
              patch.object(mcp_oauth, 'browser_code', login)):
            session = mcp_oauth.authorize_preset('weibo', resource, opened.append, cancel)
            fresh = mcp_oauth.refresh_preset('weibo', resource, session, cancel)
        self.assertEqual(fresh['access'], 'refreshed-fixture')
        self.assertEqual(fresh['refresh'], 'refresh-fixture')
        self.assertEqual(seen, ['authorization_code', 'refresh_token'])
        self.assertEqual(len(opened), 1)
        with self.assertRaises(ValueError):
            mcp_oauth._provider_url('https://cli.weibo.com@example.com/token', {urlsplit(origin).netloc})

    def test_baidu_result_and_legacy_stream(self):
        from .mcp_oauth import baidu_access_token
        from .mcp_sse import McpSse
        value = 'https://openapi.baidu.com/oauth/2.0/login_success#access_token=fixture-access-token&expires_in=3600'
        token = baidu_access_token(value)
        self.assertEqual(token, 'fixture-access-token')
        with self.assertRaises(ValueError):
            baidu_access_token(value.replace('openapi.baidu.com', 'example.com'))

        def message(identifier, result):
            return b'event: message\ndata: ' + json.dumps({
                'jsonrpc': '2.0', 'id': identifier, 'result': result}).encode() + b'\n\n'

        stream = BytesIO(b'event: endpoint\ndata: /messages?session=fixture\n\n'
                         + message(1, {'protocolVersion': '2024-11-05'})
                         + message(3, {'tools': [{'name': 'files'}]})
                         + message(4, {'tools': [{'name': 'files'}]})
                         + message(5, {'content': [{'type': 'text', 'text': 'files fixture'}]}))
        stream.headers = Message()
        stream.headers['Content-Type'] = 'text/event-stream'
        posts = []

        @contextmanager
        def open_fixture(request, **_):
            if request.get_method() == 'GET':
                self.assertEqual(parse_qs(urlsplit(request.full_url).query), {'access_token': [token]})
                yield stream
            else:
                self.assertEqual(request.full_url, 'https://mcp-pan.baidu.com/messages?session=fixture')
                posts.append(json.loads(request.data)['method'])
                yield BytesIO()

        with (patch('qt_frontend.mcp_sse.open_response', open_fixture),
              patch('qt_frontend.mcp_http.open_response', open_fixture)):
            client = McpSse('https://mcp-pan.baidu.com/sse', token, threading.Event())
            try:
                with self.assertRaises(ValueError):
                    client._post_address('https://example.com/collect')
                self.assertEqual(client.tools()[0]['name'], 'files')
                self.assertEqual(client.invoke('files', {})['content'][0]['text'], 'files fixture')
            finally:
                client.close()
        self.assertEqual(posts, ['initialize', 'notifications/initialized', 'tools/list', 'tools/list', 'tools/call'])

    def test_feishu_catalog_browser_login_and_restored_credentials(self):
        home_context = tempfile.TemporaryDirectory()
        self.addCleanup(home_context.cleanup)
        home = Path(home_context.name)
        session = {'client': 'cli_fixture00', 'secret': 'fixture-app-secret', 'access': 'fixture-user-token',
                   'expires': time.time() + 3600, 'refresh': 'fixture-refresh'}
        calls = []

        def authorize(app_id, app_secret, notify, cancel, *, scopes):
            self.assertEqual((app_id, app_secret), (session['client'], session['secret']))
            self.assertEqual(scopes, 'docx:document:readonly offline_access')
            notify('https://accounts.feishu.cn/open-apis/authen/v1/authorize?fixture=1')
            return session

        class Local:
            def __init__(self, _, args, root, cancel, *, env, **kwargs):
                calls.append((args, env))
            def tools(self):
                return [{'name': 'docs'}]
            def close(self):
                pass

        catalog = McpCatalog(home)
        with (patch('secret_store.SECRETS_FILE', home / 'secrets.json'),
              patch('qt_frontend.mcp_catalog.authorize_feishu', authorize),
              patch('qt_frontend.mcp_catalog.local_command', return_value=('node', home / 'cli.js', home)),
              patch('qt_frontend.mcp_catalog.McpStdio', Local),
              patch('qt_frontend.mcp_catalog.QDesktopServices') as browser):
            try:
                catalog.load()
                until(lambda: catalog.loaded)
                catalog.connectFeishu(session['client'], session['secret'], 'docx:document:readonly')
                until(lambda: not catalog.busy)
                self.assertFalse(catalog.error, catalog.error)
                self.assertTrue(browser.openUrl.called)
                self.assertEqual(browser.openUrl.call_args.args[0].host(), 'accounts.feishu.cn')
                catalog.test('preset.feishu')
                until(lambda: not catalog.busy)
                self.assertFalse(catalog.error, catalog.error)
                self.assertEqual(len(calls), 2)
                self.assertIn('user_access_token', calls[1][0])
                self.assertEqual(calls[1][1]['USER_ACCESS_TOKEN'], session['access'])
                catalog.clearPreset('preset.feishu')
                until(lambda: not catalog.busy)
                self.assertFalse(next(row for row in catalog.presets if row['id'] == 'preset.feishu')['configured'])
            finally:
                catalog.shutdown()

    def test_wecom_cli_scan_discovery_schema_and_dispatch(self):
        from .mcp_wecom import WeCom
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            observed, opened = [], []
            valid_url = 'https://work.weixin.qq.com/ai/qc/gen?source=fixture&scode=fixture'

            def run(_program, args, _root, _cancel, _timeout, *, env, failure, on_line):
                observed.append(args)
                self.assertTrue(env['WECOM_CLI_CONFIG_DIR'].startswith(str(home)))
                if args[:2] == ['auth', 'init']:
                    self.assertIn('--no-browser', args)
                    on_line('https://example.com/collect')
                    on_line(valid_url)
                    return ''
                if args[:2] == ['auth', 'show']:
                    return 'authorized'
                if args == ['schema', 'list']:
                    return json.dumps([{'name': 'doc', 'methods': [{'name': 'search', 'description': '查询'}]}])
                if args == ['schema', 'get', 'doc.search']:
                    return json.dumps({'request': {'type': 'object'}, 'schemas': {}})
                self.assertEqual(args, ['doc', 'search', '--json={"keywords": ["周报"]}'])
                return '{"items": []}'

            with (patch('qt_frontend.mcp_wecom.local_command', return_value=('node', home / 'wecom', home)),
                  patch('qt_frontend.mcp_wecom._run', run)):
                profile, tools = WeCom.connect(home, threading.Event(), opened.append)
                self.assertEqual(opened, [valid_url])
                self.assertEqual([tool['name'] for tool in tools], ['doc.search'])
                client = WeCom(home, profile, threading.Event())
                try:
                    self.assertEqual(client.schema('doc.search')['request'], {'type': 'object'})
                    self.assertEqual(client.invoke('doc.search', {'keywords': ['周报']})['content'][0]['text'], '{"items": []}')
                    with self.assertRaises(ValueError):
                        client.invoke('auth.init', {})
                finally:
                    client.close()
                self.assertEqual(observed[-1][0], 'doc')

    def test_china_preset_auth_and_setup_only_entries(self):
        from .mcp_http import McpHttp
        requests = []

        class Remote(McpHttp):
            def tools(self):
                requests.append((self.url, self.headers.copy()))
                return [{'name': 'read_fixture'}]

        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with (patch('secret_store.SECRETS_FILE', home / 'secrets.json'),
                  patch('qt_frontend.mcp_catalog.McpHttp', Remote)):
                catalog = McpCatalog(home)
                try:
                    catalog.load()
                    until(lambda: catalog.loaded)
                    catalog.savePreset('preset.tencent-map', 'fixture+map&key')
                    until(lambda: not catalog.busy)
                    self.assertFalse(catalog.error)
                    query = parse_qs(urlsplit(requests[0][0]).query)
                    self.assertEqual(query, {'format': ['0'], 'key': ['fixture+map&key']})
                    self.assertNotIn('Authorization', requests[0][1])
                    self.assertNotIn('?', catalog._find('preset.tencent-map')['url'])
                    catalog.savePreset('preset.tencent-docs', 'fixture-doc-key')
                    until(lambda: not catalog.busy)
                    self.assertEqual(requests[1][1]['Authorization'], 'fixture-doc-key')
                    catalog.savePreset('preset.caiyun', 'fixture-weather-key')
                    until(lambda: not catalog.busy)
                    self.assertEqual(requests[2][1]['X-Caiyun-API-Key'], 'fixture-weather-key')
                    catalog.test('setup.modelscope')
                    self.assertFalse(catalog.busy)
                    self.assertEqual(len(requests), 3)
                    self.assertFalse(next(row for row in catalog.presets if row['id'] == 'setup.modelscope')['configured'])
                    address = 'https://mcp-gw.dingtalk.com/mserver/fixture'
                    catalog.save('', '钉钉', address + '?key=secret-fixture', '', 'dingtalk')
                    self.assertTrue(catalog.error)
                    catalog.save('', '钉钉', address, 'fixture-ding-key', 'dingtalk')
                    until(lambda: not catalog.busy)
                    self.assertFalse(catalog.error)
                    saved = catalog._read_servers()[0]
                    self.assertEqual((saved['url'], saved['auth']), (address, 'dingtalk'))
                    self.assertNotIn('fixture-ding-key', catalog.path.read_text(encoding='utf-8'))
                    catalog.test(saved['id'])
                    until(lambda: not catalog.busy)
                    self.assertEqual(requests[-1][0], address + '?key=fixture-ding-key')
                finally:
                    catalog.shutdown()

    def test_notion_browser_link_and_session_stay_with_the_preset(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            from .mcp_http import McpHttp

            class Remote(McpHttp):
                def tools(self):
                    self_test.assertEqual(self.headers['Authorization'], 'Bearer fixture-access-key')
                    return [{'name': 'notion-search'}]

            self_test = self

            def authorize(key, url, publish, cancel):
                self.assertEqual((key, url), ('notion', 'https://mcp.notion.com/mcp'))
                publish('https://mcp.notion.com/authorize?fixture=1')
                return {'access': 'fixture-access-key', 'client': 'fixture-client-id',
                        'refresh': 'fixture-refresh-key', 'expires': time.time() + 600}

            with (patch('secret_store.SECRETS_FILE', home / 'secrets.json'),
                  patch('qt_frontend.mcp_catalog.McpHttp', Remote),
                  patch('qt_frontend.mcp_catalog.authorize_preset', authorize),
                  patch('qt_frontend.mcp_catalog.QDesktopServices') as browser):
                catalog = McpCatalog(home)
                try:
                    catalog.load()
                    until(lambda: catalog.loaded)
                    catalog.connectOAuth('preset.notion')
                    until(lambda: not catalog.busy)
                    self.assertFalse(catalog.error, catalog.error)
                    self.assertEqual(browser.openUrl.call_args.args[0].host(), 'mcp.notion.com')
                    catalog.test('preset.notion')
                    until(lambda: not catalog.busy)
                    self.assertFalse(catalog.error, catalog.error)
                    catalog.clearPreset('preset.notion')
                    until(lambda: not catalog.busy)
                    self.assertFalse(next(row for row in catalog.presets if row['key'] == 'notion')['configured'])
                finally:
                    catalog.shutdown()

    def test_official_presets_validate_and_keep_credentials_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            calls = []
            from .mcp_http import McpHttp

            class Remote(McpHttp):
                def tools(self):
                    calls.append((self.url, self.headers.copy(), self.allowed_tools))
                    if self.headers.get('Authorization') == 'Sentry-Bearer rejected-fixture':
                        raise HTTPError(self.url, 401, 'Unauthorized', {}, None)
                    return [{'name': 'read_fixture'}]

            with patch('secret_store.SECRETS_FILE', home / 'secrets.json'), patch(
                    'qt_frontend.mcp_catalog.McpHttp', Remote):
                catalog = McpCatalog(home)
                self.addCleanup(catalog.shutdown)
                catalog.load()
                until(lambda: catalog.loaded)
                catalog.savePreset('preset.sentry', 'accepted-fixture-token')
                until(lambda: not catalog.busy)
                self.assertFalse(catalog.error)
                self.assertEqual(calls[-1][1]['Authorization'], 'Sentry-Bearer accepted-fixture-token')
                catalog.savePreset('preset.sentry', 'rejected-fixture')
                until(lambda: not catalog.busy)
                self.assertIn('401', catalog.error)
                catalog.test('preset.sentry')
                until(lambda: not catalog.busy)
                self.assertEqual(calls[-1][1]['Authorization'], 'Sentry-Bearer accepted-fixture-token')
                catalog.savePreset('preset.developer-knowledge', 'knowledge-fixture')
                until(lambda: not catalog.busy)
                self.assertEqual(calls[-1][1]['X-Goog-Api-Key'], 'knowledge-fixture')
                self.assertNotIn('Authorization', calls[-1][1])
                catalog.test('preset.aws')
                until(lambda: not catalog.busy)
                self.assertNotIn('Authorization', calls[-1][1])
                self.assertEqual(calls[-1][2], AWS_KNOWLEDGE_TOOLS)
                catalog.clearPreset('preset.sentry')
                until(lambda: not catalog.busy)
                self.assertFalse(next(row for row in catalog.presets if row['key'] == 'sentry')['configured'])
                catalog.shutdown()

    def test_persisted_endpoint_discovers_paginated_tools_and_does_not_reuse_token_on_url_change(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with patch('secret_store.SECRETS_FILE', home / 'secrets.json'):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                server.calls = []
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                endpoint = f'http://127.0.0.1:{server.server_address[1]}'
                directory = McpCatalog(home)
                try:
                    directory.load()
                    until(lambda: directory.loaded)
                    directory.save('', 'Local fixture', endpoint + '/legacy', 'fixture-token-123')
                    until(lambda: not directory.busy)
                    self.assertFalse(directory.error)
                    self.assertNotIn('fixture-token-123', directory.path.read_text(encoding='utf-8'))
                    identifier = directory.rows[0]['id']
                    reopened = McpCatalog(home)
                    reopened.load()
                    until(lambda: reopened.loaded)
                    reopened.test(identifier)
                    until(lambda: not reopened.busy)
                    self.assertFalse(reopened.error, reopened.error)
                    self.assertEqual([tool['name'] for tool in reopened.rows[0]['tools']], ['first', 'second'])
                    self.assertIn(('tools/list', 'Bearer fixture-token-123', 'fixture-session'), server.calls)
                    reopened.save(identifier, 'Local fixture', endpoint + '/modern', '')
                    until(lambda: not reopened.busy)
                    server.calls.clear()
                    reopened.test(identifier)
                    until(lambda: not reopened.busy)
                    self.assertFalse(reopened.error, reopened.error)
                    self.assertEqual(server.calls, [('tools/list', None, None), ('tools/list', None, None)])
                    reopened.invoke(identifier, 'first', '{"number": 7}')
                    until(lambda: not reopened.busy)
                    self.assertFalse(reopened.error, reopened.error)
                    self.assertIn('fixture result', reopened.result)
                    self.assertEqual(server.tool_call['arguments'], {'number': 7})
                    self.assertEqual(server.calls[-1], ('tools/call', None, None))
                    reopened.remove(identifier)
                    until(lambda: not reopened.busy)
                    self.assertEqual(reopened.rows, [])
                    reopened.shutdown()
                finally:
                    directory.shutdown()
                    server.shutdown()
                    server.server_close()
                    worker.join(2)
