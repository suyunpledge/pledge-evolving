from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from .mcp_catalog import McpCatalog
from .test_backend import until
from .workspace_pairing import GOOGLE_TOKEN, WorkspacePairing, google_authorize, google_scopes


class WorkspacePairingTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.folder)
        self.enterContext(patch('secret_store.SECRETS_FILE', self.home / 'secrets.json'))
        self.catalog = McpCatalog(self.home)
        self.addCleanup(self.catalog.shutdown)

    def test_google_callback_requires_matching_state_and_exchanges_with_pkce(self):
        visited, requests = [], []

        def browser(url):
            query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
            visited.append(query)

            def callback():
                with self.assertRaises(HTTPError):
                    urlopen(query['redirect_uri'] + '?state=wrong&code=wrong', timeout=3)
                with urlopen(query['redirect_uri'] + '?state=' + query['state'] + '&code=valid', timeout=3) as response:
                    self.assertEqual(response.status, 200)

            responder = threading.Thread(target=callback, daemon=True)
            responder.start()
            self.addCleanup(responder.join, 3)

        def request(url, method, headers, body, **kwargs):
            requests.append((url, body))
            return {'access_token': 'demo'}

        google_authorize('desktop-client', 'test-secret', google_scopes(['gmail']),
                         browser, threading.Event(), request)
        url, body = requests[0]
        self.assertEqual(url, GOOGLE_TOKEN)
        self.assertEqual(body['code'], 'valid')
        self.assertEqual(body['redirect_uri'], visited[0]['redirect_uri'])
        self.assertEqual(visited[0]['code_challenge_method'], 'S256')
        self.assertGreater(len(body['code_verifier']), 43)

    def test_selection_and_partial_consent_control_tool_availability(self):
        scopes, calls, graph_calls = [], [], []
        token = 'workspace-fixture-token-3456278'

        def google(_, __, requested, notify, cancel, request):
            scopes.append(requested)
            notify('https://accounts.google.com/o/oauth2/v2/auth?fixture=1')
            return {'access_token': token, 'refresh_token': 'fixture-refresh-token-7845',
                    'expires_in': 3600, 'scope': ' '.join(google_scopes(['gmail']))}

        class Mcp:
            def __init__(self, url, credential, cancel, *, plain):
                self.url = url
                self.credential = credential

            def tools(self):
                calls.append((self.url, self.credential))
                return [{'name': 'show_message', 'schema': {'type': 'object'}}]

            def close(self):
                pass

        def graph(url, method, headers, body, **kwargs):
            graph_calls.append((url, method))
            return {'values': [[42]]}

        pairing = WorkspacePairing(self.home, authorize=google, mcp_client=Mcp, transport=graph)

        def microsoft(conf, notify, cancel, *, expected):
            scopes.append(set(conf['scopes']))
            notify('TEST-CODE', 'https://microsoft.com/devicelogin')
            tokens = {'access_token': 'microsoft-test', 'expires_at': time.time() + 3600}
            pairing.store.finish_account('microsoft', expected, {**conf, 'authenticated': True},
                                         lambda: pairing._save('microsoft', json.dumps(tokens)))

        pairing.microsoft.device_login = microsoft
        browser = self.enterContext(patch('qt_frontend.mcp_catalog.QDesktopServices'))
        self.catalog._workspace_pairing = pairing
        self.catalog.load()
        until(lambda: self.catalog.loaded)
        self.catalog.pair('google', ['google.gmail', 'google.drive', 'microsoft.excel'],
                          'forge-test.apps.googleusercontent.com', 'example-client-secret')
        until(lambda: not self.catalog.busy)
        self.assertFalse(self.catalog.error)
        self.assertIn('Google Drive', self.catalog.pairErrors['google'])
        self.assertEqual(scopes, [google_scopes(['gmail', 'drive'])])
        self.assertEqual([call.args[0].host() for call in browser.openUrl.call_args_list], ['accounts.google.com'])
        self.catalog.pair('microsoft', ['microsoft.excel'], 'desktop-client', 'common')
        until(lambda: not self.catalog.busy)
        self.assertFalse(self.catalog.error)
        self.assertEqual(scopes, [google_scopes(['gmail', 'drive']), {'Files.ReadWrite'}])
        self.assertEqual([call.args[0].host() for call in browser.openUrl.call_args_list],
                         ['accounts.google.com', 'microsoft.com'])
        self.assertEqual([row['id'] for row in self.catalog.families if row['paired']],
                         ['google.gmail', 'microsoft.excel'])

        self.catalog.test('google.gmail')
        until(lambda: not self.catalog.busy)
        self.assertEqual(calls, [('https://gmailmcp.googleapis.com/mcp/v1', token)])
        self.catalog.test('microsoft.onedrive')
        until(lambda: not self.catalog.busy)
        drive = next(row for row in self.catalog.families if row['id'] == 'microsoft.onedrive')
        self.assertFalse(drive['tools'])
        self.catalog.test('microsoft.excel')
        until(lambda: not self.catalog.busy)
        excel = next(row for row in self.catalog.families if row['id'] == 'microsoft.excel')
        self.assertEqual([tool['name'] for tool in excel['tools']], ['connector_microsoft_excel_range'])
        self.catalog.invoke('microsoft.excel', 'connector_microsoft_excel_range',
                            '{"id":"book","sheet":"Sheet1","address":"A1"}', str(self.home))
        until(lambda: not self.catalog.busy)
        self.assertFalse(self.catalog.error, self.catalog.error)
        self.assertEqual(json.loads(self.catalog.result)['content']['values'], [[42]])
        self.assertEqual(len(graph_calls), 1)
        self.assertNotIn(b'example-client-secret', pairing.store.path.read_bytes())

        self.catalog.disconnectGoogle()
        until(lambda: not self.catalog.busy)
        self.assertEqual([row['id'] for row in self.catalog.families if row['paired']], ['microsoft.excel'])
        self.assertFalse(pairing._credential('google'))
