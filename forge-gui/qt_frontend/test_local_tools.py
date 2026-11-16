"""Regression checks for bundled 7-Zip and ComfyUI's explicit download decision."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from . import local_tool_runtime as tools
from .archive_reader import library_path
from .permissions import authorize


# A single UTF-16-named file archived with 7-Zip. The test needs no 7-Zip executable.
SEVEN_FIXTURE = base64.b64decode(
    'N3q8ryccAAQHvESfJAAAAAAAAABiAAAAAAAAAJFYm1cBAB9Gb3JnZSBuYXRpdmUgcmVhZGVyIHZlcmlmaWNhdGlvbgABBAYAAQkkAAcLAQABISEBAAwgAAgKAdpXPX8AAAUBGQwAAAAAAAAAAAAAAAARHQBuAGEAdABpAHYAZQAtADp5i08uAHQAeAB0AAAAFAoBAGIAd/y/dN0BFQYBACAAAAAAAA=='
)


class LocalToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cancel = threading.Event()
        self.choices = []
        self.runtime = tools.LocalToolRuntime(self.root / 'home', self.choose, lambda *_: None)

    def choose(self, kind, cancel):
        self.choices.append(kind)
        return ''

    def workflow(self):
        graph = {'2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'initial'}},
                 '3': {'class_type': 'SaveImage', 'inputs': {}}}
        (self.root / 'workflow.json').write_text(json.dumps(graph), encoding='utf-8')
        return {'path': 'workflow.json', 'destination': 'output'}

    @unittest.skipUnless(library_path().is_file(), 'requires the native bundle for this platform')
    def test_native_7z_extracts_without_an_installer_or_subprocess(self):
        (self.root / 'fixture.7z').write_bytes(SEVEN_FIXTURE)
        args = {'path': 'fixture.7z', 'destination': 'output'}
        with patch.object(tools.subprocess, 'Popen', side_effect=AssertionError('used an external executable')):
            authorize('sandbox', self.root, 'forge_7zip_extract', args, self.choose)
            result = self.runtime.call('7zip_extract', self.root, args, self.cancel)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['files'], ['native-示例.txt'])
        self.assertEqual((self.root / 'output' / result['files'][0]).read_text(encoding='utf-8'),
                         'Forge native reader verification')
        self.assertEqual(self.choices, [])

    def test_unsafe_archive_and_existing_destination_publish_nothing(self):
        with zipfile.ZipFile(self.root / 'fixture.zip', 'w') as archive:
            archive.writestr('file.txt', 'hello')
            archive.writestr('../outside.txt', 'reject')
        args = {'path': 'fixture.zip', 'destination': 'output'}
        result = self.runtime.call('7zip_extract', self.root, args, self.cancel)
        self.assertFalse(result['ok'])
        self.assertFalse((self.root / 'output').exists())
        self.assertFalse((self.root / 'outside.txt').exists())
        (self.root / 'output').mkdir()
        (self.root / 'output' / 'keep.txt').write_bytes(b'keep')
        result = self.runtime.call('7zip_extract', self.root, args, self.cancel)
        self.assertFalse(result['ok'])
        self.assertEqual((self.root / 'output' / 'keep.txt').read_bytes(), b'keep')

    def test_comfy_cancel_does_not_download_or_submit(self):
        args = self.workflow()
        with (patch.object(self.runtime, '_online', return_value=False),
              patch.object(self.runtime, '_install_comfy') as install,
              patch.object(self.runtime, '_queue_comfy') as submit):
            result = self.runtime.call('comfyui_run', self.root, args, self.cancel)
        self.assertEqual(self.choices, ['comfyui'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['meta']['execution_state'], 'not_started')
        self.assertFalse((self.root / 'output').exists())
        install.assert_not_called()
        submit.assert_not_called()

    def test_existing_comfy_service_runs_workflow_and_saves_artifact(self):
        calls = []
        media = b'fixture bytes from the local API'

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, payload):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

            def do_GET(self):
                url = urlsplit(self.path)
                calls.append((url.path, parse_qs(url.query)))
                if url.path == '/system_stats':
                    self.send({'system': {}, 'devices': []})
                elif url.path == '/history/test-prompt':
                    self.send({'test-prompt': {'status': {'completed': True}, 'outputs': {
                        '3': {'images': [{'filename': 'preview.png', 'type': 'output', 'subfolder': ''}]}}}})
                elif url.path == '/view':
                    self.send(media)
                else:
                    self.send_error(404)

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, data))
                self.send({'prompt_id': 'test-prompt'})

        args = {**self.workflow(), 'prompt_node': '2', 'prompt': 'replaced'}
        with ThreadingHTTPServer(('127.0.0.1', 0), Endpoint) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(tools, 'COMFY', f'http://127.0.0.1:{server.server_port}'):
                    result = self.runtime.call('comfyui_run', self.root, args, self.cancel)
            finally:
                server.shutdown()
                thread.join()
        self.assertTrue(result['ok'], result)
        self.assertEqual([path for path, _ in calls], ['/system_stats', '/prompt', '/history/test-prompt', '/view'])
        self.assertEqual(calls[1][1]['prompt']['2']['inputs']['text'], 'replaced')
        self.assertEqual((self.root / 'output' / '01-preview.png').read_bytes(), media)
        self.assertEqual(self.choices, [])


if __name__ == '__main__':
    unittest.main()
