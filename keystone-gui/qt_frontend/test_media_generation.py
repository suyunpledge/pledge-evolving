"""Verify media protocols over loopback and keep generation credentials out of profiles."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from .media_generation import MediaGeneration, PRESETS, ToolFailure
from .media_models import MediaModels, _credential
import secret_store


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO/a9XkAAAAASUVORK5CYII='
)
MP4 = b'\0\0\0\x18ftypmp42\0\0\0\0mp42isom'
KEY = 'keystone-fixture-media-key'


class MediaTests(unittest.TestCase):
    def test_independent_defaults_custom_endpoint_and_secret_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MediaModels(directory)
            image = service._save('', 'openai-images', 'https://images.example/v1/images/generations',
                                  'gpt-image-1.5', KEY, False)
            video = service._save('', 'seedance', 'https://video.example/api/v3',
                                  'doubao-seedance-fixture', KEY, False)
            extra = service._save('', 'seedream', 'https://images.example/api/v3',
                                  'seedream-fixture', KEY, False)
            self.assertEqual(service._profile('image'), (image, KEY))
            self.assertEqual(service._profile('video'), (video, KEY))
            self.assertEqual(image['url'], 'https://images.example/v1')
            self.assertNotIn(KEY, service.path.read_text(encoding='utf-8'))
            self.assertNotIn(_credential(image['id']), secret_store.env_for(
                secret_store.load(path=service.credentials)))
            with self.assertRaisesRegex(ValueError, 'API 密钥'):
                service._save(image['id'], 'openai-images', 'https://other.example/v1',
                              'gpt-image-1.5', '', False)
            service._save(extra['id'], 'seedream', extra['url'], extra['model'], '', True)
            self.assertEqual(service._profile('image'), (extra, KEY))

    def test_media_provider_contracts_keep_credentials_on_the_configured_api(self):
        calls = []
        protocol_routes = {
            'openai-images': ('/images/generations', None, 'image-1.png'),
            'seedream': ('/images/generations', None, 'image-1.png'),
            'wan-image': ('/services/aigc/multimodal-generation/generation', None, 'image-1.png'),
            'flux': ('/flux-fixture', '/get_result', 'image-1.png'),
            'seedance': ('/contents/generations/tasks', '/contents/generations/tasks/job', 'video-1.mp4'),
            'wan-video': ('/services/aigc/video-generation/video-synthesis', '/tasks/job', 'video-1.mp4'),
            'openrouter-image': ('/images', None, 'image-1.png'),
            'openrouter-video': ('/videos', '/videos/job', 'video-1.mp4'),
            'qiniu-image': ('/images/generations', '/images/tasks/job', 'image-1.png'),
            'qiniu-video': ('/videos', '/videos/job', 'video-1.mp4'),
        }
        active = None

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, payload):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

            def capture(self, body=None):
                route = urlsplit(self.path).path.removeprefix('/custom')
                calls.append((route, self.headers.get('Authorization'), self.headers.get('x-key'),
                              self.headers.get('X-DashScope-Async'), body))
                return route

            def do_POST(self):
                self.capture(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if active in {'openai-images', 'openrouter-image'}:
                    self.send({'data': [{'b64_json': base64.b64encode(PNG).decode()}]})
                elif active == 'seedream':
                    self.send({'data': [{'url': origin + '/file.png'}]})
                elif active == 'wan-image':
                    self.send({'output': {'choices': [
                        {'message': {'content': [{'image': origin + '/file.png'}]}},
                    ]}})
                elif active == 'wan-video':
                    self.send({'output': {'task_id': 'job'}})
                elif active == 'flux':
                    self.send({'id': 'job', 'polling_url': origin + '/custom/get_result?id=job'})
                elif active == 'qiniu-image':
                    self.send({'task_id': 'job'})
                else:
                    self.send({'id': 'job'})

            def do_GET(self):
                route = self.capture()
                if route == '/file.png':
                    self.send(PNG)
                elif route == '/file.mp4':
                    self.send(MP4)
                elif active == 'flux':
                    self.send({'status': 'Ready', 'result': {'sample': origin + '/file.png'}})
                elif active == 'wan-video':
                    self.send({'output': {'task_status': 'SUCCEEDED', 'video_url': origin + '/file.mp4'}})
                elif active == 'openrouter-video':
                    if route == '/videos/job/content':
                        self.send(MP4)
                    else:
                        # The provider response can't choose where Keystone sends the key.
                        self.send({'status': 'completed', 'unsigned_urls': [origin + '/file.mp4']})
                elif active == 'qiniu-image':
                    self.send({'status': 'succeed', 'data': [{'url': origin + '/file.png'}]})
                elif active == 'qiniu-video':
                    self.send({'status': 'completed', 'task_result': {'videos': [{'url': origin + '/file.mp4'}]}})
                else:
                    self.send({'status': 'succeeded', 'content': {'video_url': origin + '/file.mp4'}})

        with (tempfile.TemporaryDirectory() as directory,
              ThreadingHTTPServer(('127.0.0.1', 0), Endpoint) as server):
            origin = f'http://127.0.0.1:{server.server_port}'
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for protocol, (post, polling, filename) in protocol_routes.items():
                    with self.subTest(protocol=protocol):
                        active = protocol
                        calls.clear()
                        model = 'flux-fixture' if protocol == 'flux' else PRESETS[protocol][3]
                        runtime = MediaGeneration({'url': origin + '/custom', 'protocol': protocol, 'model': model},
                                                  KEY, threading.Event(), lambda _: None)
                        result = runtime.run(Path(directory), 'A paper boat on a lake', protocol)
                        self.assertTrue(result['ok'])
                        route, bearer, flux_key, async_header, body = calls[0]
                        self.assertEqual(route, post)
                        self.assertEqual(bearer, None if protocol == 'flux' else 'Bearer ' + KEY)
                        self.assertEqual(flux_key, KEY if protocol == 'flux' else None)
                        self.assertEqual(async_header, 'enable' if protocol == 'wan-video' else None)
                        if polling:
                            self.assertEqual(calls[1][:3], (polling, bearer, flux_key))
                        if protocol == 'openrouter-video':
                            self.assertEqual(calls[-1][:3], ('/videos/job/content', bearer, None))
                        elif protocol not in {'openai-images', 'openrouter-image'}:
                            self.assertEqual(calls[-1][1:3], (None, None))
                        if protocol.startswith(('qiniu-', 'openrouter-')):
                            self.assertEqual(body['model'], model)
                            self.assertEqual(body['prompt'], 'A paper boat on a lake')
                        if protocol == 'qiniu-video':
                            self.assertEqual(body['mode'], 'pro')
                        if protocol == 'wan-video':
                            self.assertIn('prompt', body['input'])
                        if protocol == 'seedance':
                            self.assertEqual(body['content'][0]['type'], 'text')
                        path = Path(result['files'][0])
                        self.assertEqual(path.name, filename)
                        self.assertEqual(path.read_bytes(), PNG if filename.endswith('png') else MP4)
            finally:
                server.shutdown()
                thread.join()

    def test_result_failure_preserves_unknown_submission_and_job_id(self):
        import unittest.mock
        with tempfile.TemporaryDirectory() as directory:
            value = MediaGeneration({'url': 'http://127.0.0.1:1/v3', 'protocol': 'seedance', 'model': 'seedance-fixture'},
                                    KEY, threading.Event(), lambda _: None)
            with unittest.mock.patch.object(value, '_json', side_effect=[
                {'id': 'queued-job'}, {'status': 'succeeded', 'content': {'video_url': 'file:///bad.mp4'}},
            ]) as req:
                with self.assertRaises(ToolFailure) as caught:
                    value.run(Path(directory), 'test prompt', 'output')
            self.assertEqual(req.call_count, 2)
            self.assertEqual(caught.exception.state, 'unknown')
            self.assertEqual(caught.exception.details, {'job_id': 'queued-job'})
            self.assertFalse((Path(directory) / 'output').exists())

    def test_interrupted_media_download_keeps_job_id_for_manual_recovery(self):
        from http.client import IncompleteRead
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            runtime = MediaGeneration({'url': 'http://127.0.0.1:1/v3', 'protocol': 'seedance', 'model': 'fixture'},
                                      KEY, threading.Event(), lambda _: None)
            with (patch.object(runtime, '_json', side_effect=[
                    {'id': 'saved-job'}, {'status': 'succeeded', 'content': {'video_url': 'https://example.com/video.mp4'}},
                  ]) as request, patch.object(runtime, '_save', side_effect=IncompleteRead(b'partial', 100))):
                with self.assertRaises(ToolFailure) as caught:
                    runtime.run(Path(directory), 'test prompt', 'output')
            self.assertEqual(caught.exception.state, 'unknown')
            self.assertEqual(caught.exception.details, {'job_id': 'saved-job'})
            self.assertEqual(request.call_count, 2)
            self.assertFalse((Path(directory) / 'output').exists())

    def test_truncated_download_is_not_published(self):
        from io import BytesIO
        from unittest.mock import patch
        stream = BytesIO(MP4)
        stream.headers = {'Content-Length': str(len(MP4) + 100)}
        runtime = MediaGeneration({'url': 'http://127.0.0.1:1/v3', 'protocol': 'seedance', 'model': 'fixture'},
                                  KEY, threading.Event(), lambda _: None)
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(runtime, '_json', side_effect=[
                  {'id': 'saved-job'}, {'status': 'succeeded', 'content': {'video_url': 'http://127.0.0.1:1/video.mp4'}},
              ]), patch('qt_frontend.media_generation.open_response', return_value=stream)):
            with self.assertRaises(ToolFailure) as caught:
                runtime.run(Path(directory), 'test prompt', 'output')
            self.assertEqual(caught.exception.details, {'job_id': 'saved-job'})
            self.assertFalse((Path(directory) / 'output').exists())


if __name__ == '__main__':
    unittest.main()
