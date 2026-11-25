from pathlib import Path
import tempfile
from threading import Event
import unittest

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from revon_client import CompletionResult
from revon.test_visual_input import png
from .office_workspace import OfficeWorkspace
from .productivity_tools import attachment_path
from .test_turn_budget import Client, drive
from . import visual_tools


class VisualToolsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / 'chat'
        self.stop = Event()
        self.vision = {'model': 'custom', 'routeCapabilities': {'vision': True}}

    def call(self, name, **args):
        return visual_tools.call(('visual', name), args, self.root, self.folder, self.stop)

    def test_real_webengine_outputs_page_content_and_honors_page_numbers(self):
        store = OfficeWorkspace(self.root)
        doc = store.create('document', 'Local PDF', {'P1': 'Document heading', 'P1.style': 'heading', 'P2': 'Page content'})
        slides = store.create('presentation', 'Two slides', {'S1': 'First page', 'S2': 'Second page', 'S2.body': 'Visible body'})
        pixels = []
        for source, page, size in ((doc, 1, QSize(794, 1123)), (slides, 1, QSize(960, 540)), (slides, 2, QSize(960, 540))):
            output = self.call('office_render', id=source['id'], page=page)
            self.assertTrue(output['ok'], output.get('error'))
            receipt = output['meta']
            self.assertEqual(receipt['pages'], 2 if source is slides else 1)
            path = attachment_path(self.folder, receipt['attachment'])
            image = QImage(str(path))
            self.assertEqual(image.size(), size)
            colors = {image.pixel(x,y) for x in range(0,size.width(),4) for y in range(0,size.height(),4)}
            self.assertGreater(len(colors), 3)
            pixels.append(output['_visual_observation'])
            self.assertEqual(self.call('view_image', path=receipt['attachment'])['_visual_observation'], pixels[-1])
        self.assertNotEqual(pixels[1], pixels[2])
        self.assertFalse(self.call('office_render', id=doc['id'], page=99)['ok'])

    def test_chat_and_work_send_transient_image_only_to_capable_model(self):
        (self.root / 'red.png').write_bytes(png())
        encoded = self.call('view_image', path='red.png')['_visual_observation'][1]
        self.assertNotIn('keystone_view_image', visual_tools.tools({}))
        self.assertNotIn('keystone_office_render', visual_tools.tools({}))
        self.assertNotIn('keystone_office_render', visual_tools.tools(self.vision, chat=True))
        self.assertIn('keystone_read_pdf', visual_tools.tools({}))
        class VisualClient(Client):
            def stream_chat(inner, messages, on_chunk, **kwargs):
                inner.requests.append([m.to_dict() for m in messages])
                if len(inner.requests) == 1:
                    return CompletionResult('', tool_calls=[{'id': 'v1', 'type': 'function', 'function': {
                        'name': 'keystone_view_image', 'arguments': '{"path":"red.png"}'}}])
                return CompletionResult('done')
        for chat in (False, True):
            client = VisualClient()
            def dispatch(target, args, cancel):
                return visual_tools.call(target, args, self.root, self.folder, cancel, chat=chat)
            connection = visual_tools.tools(self.vision, chat=chat), dispatch
            _, _, history = drive(client, **{'chat_mcp' if chat else 'mcp': connection})
            self.assertEqual(encoded in str(client.requests[-1]), not chat)
            self.assertNotIn(encoded, str(history))

    def test_client_preserves_single_line_source_through_model_projection(self):
        import io
        import json
        from unittest.mock import patch
        from revon_client import ChatMessage, RevonGatewayClient
        from .conversation import model_tool_result
        row = {'ok': True, 'content': '7\ttoken = value', 'meta': {'start': 7, 'end': 7}}
        client = RevonGatewayClient('http://127.0.0.1:1')
        self.addCleanup(client.secret_scope.close)
        with patch('revon_client.open_response', return_value=io.BytesIO(json.dumps(row).encode())):
            read = client.call_tool('keystone_read_range', {'path': 'logic.py', 'start': 7, 'end': 7})
        display = model_tool_result('keystone_read_range', read)
        msg = ChatMessage('tool', json.dumps(display), tool_call_id='source')
        self.assertEqual(json.loads(msg.to_dict(client.secret_scope)['content'])['content'], 'token = value')

    def test_pdf_text_and_empty_pages(self):
        from PySide6.QtGui import QGuiApplication, QPdfWriter, QPainter
        app = QGuiApplication.instance() or QGuiApplication([])
        path = self.root / 'native.pdf'
        writer = QPdfWriter(str(path))
        paint = QPainter(writer)
        paint.drawText(150, 250, 'Native PDF text')
        writer.newPage()
        paint.end()
        del writer
        output = self.call('read_pdf', path='native.pdf', page=1)
        self.assertTrue(output['ok'], output)
        self.assertEqual(output['meta']['pages'], 2)
        self.assertIn('Native PDF text', output['content'])
        self.assertIn('没有可提取', self.call('read_pdf', path='native.pdf', page=2)['content'])
        self.assertFalse(self.call('read_pdf', path='native.pdf', page=3)['ok'])
