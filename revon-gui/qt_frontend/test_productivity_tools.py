import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from revon_client import ChatMessage, CompletionResult
from .conversation import ENTER_WORK, preview_receipt, run
from .permissions import authorize
from .productivity_tools import NAMES, attachment_path, call, with_productivity


class ProductivityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cancel = threading.Event()

    def invoke(self, key, args):
        return call(key, args, self.root, self.cancel)

    @unittest.skipUnless(os.name == 'nt', 'uses Windows file associations')
    def test_office_fallback_respects_registered_handler_and_recovers_stale_choice(self):
        import winreg
        from unittest.mock import MagicMock, patch
        from .productivity_tools import office_fallback

        excel = self.root / 'EXCEL.EXE'
        excel.touch()
        with patch.object(winreg, 'OpenKey') as open_key, \
             patch.object(winreg, 'QueryValueEx', return_value=('ET.Xlsx.6', winreg.REG_SZ)), \
             patch.object(winreg, 'QueryValue', return_value=str(excel)):
            self.assertIsNone(office_fallback('.xlsx'))
            self.assertEqual(open_key.call_args.args, (winreg.HKEY_CLASSES_ROOT, 'ET.Xlsx.6'))
            open_key.side_effect = [MagicMock(), FileNotFoundError(), MagicMock()]
            self.assertEqual(office_fallback('.xlsx'), excel)
            self.assertEqual(open_key.call_args.args, (winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\EXCEL.EXE'))

    def test_function_and_statistics(self):
        result = self.invoke('evaluate_math', {'expression': 'sqrt(x^2) - 2**2', 'x_values': [-2, 3]})
        self.assertEqual(json.loads(result['content'])['y'], [-2, -1])
        self.assertEqual(json.loads(self.invoke('evaluate_math', {'expression': '-2^2'})['content'])['value'], -4)
        stats = json.loads(self.invoke('summarize_numbers', {'values': [1, 2, 3, 10]})['content'])
        self.assertEqual((stats['median'], stats['sum'], stats['mean']), (2.5, 16, 4))
        for expr in ('abs.__name__', 'x+1', '2**10000000', 'sqrt(-1)', 'True'):
            with self.subTest(expr=expr):
                self.assertFalse(self.invoke('evaluate_math', {'expression': expr})['ok'])
        self.assertFalse((self.root / 'exports').exists())

    def test_both_conversation_modes_dispatch_and_reopen_each_format(self):
        from docx import Document
        from openpyxl import load_workbook
        from PIL import Image
        from pptx import Presentation

        inputs = {'evaluate_math': {'expression': 'sin(pi/2)'}, 'summarize_numbers': {'values': [2, 6]},
                  'plot_chart': {'kind': 'function', 'expression': '1/x', 'title': '函数图'},
                  'create_spreadsheet': {'title': '统计', 'sheets': [
                      {'name': '销售', 'headers': ['月份', '销量'], 'rows': [['六月', 12], ['=literal', 18]]}]},
                  'create_slides': {'title': '月报', 'slides': [{'title': '结论', 'bullets': ['销量增长']}]},
                  'create_document': {'title': '分析', 'sections': [{'heading': '结论', 'paragraphs': ['增长'], 'bullets': ['跟进']}]}}
        for chat in (True, False):
            with self.subTest(chat=chat):
                calls = [{'id': 't'+str(i), 'type': 'function', 'function': {'name': 'keystone_'+k, 'arguments': json.dumps(v)}}
                         for i, (k, v) in enumerate(inputs.items())]
                receipts = {}
                case = self

                class Client:
                    def list_tools(self, **_):
                        case.assertFalse(chat)
                        return []

                    def stream_chat(self, messages, *, tools, **_):
                        names = {t['function']['name'] for t in tools}
                        case.assertTrue(NAMES.issubset(names))
                        case.assertEqual(ENTER_WORK in names, chat)
                        return CompletionResult('完成') if receipts else CompletionResult('', tool_calls=calls)

                connection = with_productivity(({}, None), self.root)
                def checked(name, args, cancel):
                    authorize('sandbox', self.root, name, args, lambda *_: case.fail('unexpected approval'), cancel)
                run(Client(), [ChatMessage('user', '生成分析资料')], model='test', effort=None, cancel=self.cancel,
                    authorize=checked, chunk=lambda _: None, checkpoint=lambda _: None, status=lambda _: None,
                    next_round=lambda: None, receipt=lambda k, r: receipts.update({k.removeprefix('keystone_'): r}),
                    mcp=connection, chat_mcp=connection if chat else None, enter_work=lambda *_: case.fail('unexpected handoff'))
                for name, result in receipts.items():
                    self.assertTrue(result['ok'], (name, result))
                def file(key):
                    row = preview_receipt('keystone_' + key, receipts[key])
                    return attachment_path(self.root, row['attachment'])
                with Image.open(file('plot_chart')) as image:
                    self.assertEqual(image.format, 'PNG')
                    image.verify()
                book = load_workbook(file('create_spreadsheet'))
                sheet = book['销售']
                self.assertEqual((sheet['B2'].value, sheet['A3'].data_type), (12, 's'))
                self.assertEqual(sheet.freeze_panes, 'A2')
                book.close()
                deck = Presentation(file('create_slides'))
                self.assertEqual([s.shapes.title.text for s in deck.slides], ['月报', '结论'])
                self.assertIn('销量增长', deck.slides[1].placeholders[1].text)
                self.assertEqual([p.text for p in Document(file('create_document')).paragraphs],
                                 ['分析', '结论', '增长', '跟进'])
        self.assertEqual(len(list((self.root / 'exports').iterdir())), 8)

    def test_failed_or_cancelled_export_leaves_no_attachment(self):
        for args in ({'kind': 'bar', 'title': '图', 'x': ['一'], 'y': [1, 2]},
                     {'kind': 'function', 'title': '图', 'expression': 'sqrt(-1)'},):
            self.assertFalse(self.invoke('plot_chart', args)['ok'])
        self.cancel.set()
        self.assertFalse(self.invoke('create_document', {'title': '稿', 'sections': [{'paragraphs': ['文']} ]})['ok'])
        self.assertEqual(list((self.root / 'exports').iterdir()), [])
        with self.assertRaises(ValueError):
            attachment_path(self.root, '../other.docx')
