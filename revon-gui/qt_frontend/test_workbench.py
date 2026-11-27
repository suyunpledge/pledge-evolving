"""Exercise the real child host, retained WebEngine view and approval handshake."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'Basic')
from PySide6.QtCore import QObject, QMetaObject, QProcess, Q_ARG, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QInputMethodEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtTest import QTest
from .controller import ChatController
from .workbench import WorkbenchController

from ._test_app import APP
ROOT = Path(__file__).parent


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    raise AssertionError('condition timed out')


def visual_item(root, name):
    if root.objectName() == name:
        return root
    for child in root.childItems() if hasattr(root, 'childItems') else []:
        item = visual_item(child, name)
        if item is not None:
            return item
    return None


class Page(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        try:
            self.wfile.write(b'<title>revon retained browser test</title><input value="draft123">')
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # Closing a retained browser also cancels its favicon request.


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.controller = ChatController(self.temp.name, demo=True)
        self.workbench = WorkbenchController(self.temp.name, demo=True)
        self.engine = QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda rows: self.warnings.extend(map(str, rows)))
        context = self.engine.rootContext()
        for name, value in {'controller': self.controller, 'workbench': self.workbench,
                            'smokeMode': True, 'gpuEnabled': True, 'uiFont': QFont('Segoe UI', 10),
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets/revon-logo-104.png'))}.items():
            context.setContextProperty(name, value)
        self.engine.load(QUrl.fromLocalFile(str(ROOT / 'qml/Main.qml')))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.window.setPosition(-4000, -3000)
        self.window.show()

    def tearDown(self):
        import shiboken6
        self.workbench.shutdown()
        self.assertEqual(self.workbench._process.state(), QProcess.ProcessState.NotRunning)
        self.controller.shutdown()
        if shiboken6.isValid(self.window):
            self.window.setProperty('exitConfirmed', True)
            self.window.close()
        self.engine.deleteLater()
        APP.processEvents()
        self.temp.cleanup()
        self.assertFalse(self.warnings, self.warnings)

    def test_office_and_browser_share_the_side_panel(self):
        office = lambda: self.window.findChild(QObject, 'officeWebView')
        self.workbench.showSideTool('office')
        until(lambda: office() is not None and office().isVisible())
        panel = visual_item(self.window.contentItem(), 'browserPanel')
        view = office()
        left = view.mapToItem(self.window.contentItem(), 0, 0).x()
        self.assertGreaterEqual(left, panel.x())
        self.assertLessEqual(view.width(), panel.width())
        self.assertTrue(visual_item(self.window.contentItem(), 'officeSideHeader').isVisible())
        # Switching the tab shows the browser in the same slot and hides Office.
        self.workbench.showSideTool('browser')
        until(lambda: not office().isVisible())
        self.assertTrue(self.workbench.browserVisible)
        self.assertEqual(self.workbench.sideTool, 'browser')
        # Toggling the active tool closes the panel; the other tool reopens it.
        self.workbench.toggleSideTool('browser')
        self.assertFalse(self.workbench.browserVisible)
        self.workbench.toggleSideTool('office')
        until(lambda: office().isVisible())
        if os.environ.get('REVON_SHOT'):
            self.window.grabWindow().save(os.environ['REVON_SHOT'])

    def test_office_file_from_work_opens_in_side_panel(self):
        self.workbench._workspace = self.temp.name     # never create documents in the repository
        created = json.loads(self.workbench.office.request(json.dumps({'action': 'create', 'kind': 'document', 'title': '季度总结'})))
        self.assertTrue(created['ok'], created)
        doc_id = created['data']['id']
        requested = []
        self.workbench.officeDocumentRequested.connect(requested.append)
        from .controller import _office_document
        response = {'ok': True, 'content': json.dumps({'id': doc_id, 'kind': 'document'})}
        self.assertEqual(_office_document('keystone_office_create', response), doc_id)
        self.assertEqual(_office_document('keystone_office_read', response), '')
        self.assertEqual(_office_document('keystone_office_apply', {'ok': False}), '')
        self.controller.officeDocumentProduced.emit(doc_id)
        self.assertEqual(requested, [doc_id])
        self.assertTrue(self.workbench.browserVisible)
        self.assertEqual(self.workbench.sideTool, 'office')
        until(lambda: self.window.property('officeReady'))
        page = self.window.findChild(QObject, 'officeWebView').parent()
        until(lambda: page.property('pendingDocument') == '')

    def test_reply_checklist_renders_counts_status_and_collapses(self):
        rows = [{'text': '检查数据', 'status': 'completed'}, {'text': '核对结果', 'status': 'in_progress'}]
        self.controller.messages.reset([dict(roleName='assistant', textContent='', state='streaming', todos=rows)])
        listing = self.window.findChild(QObject, 'conversationList')
        until(lambda: visual_item(listing, 'toggleTodos') is not None)
        button = visual_item(listing, 'toggleTodos')
        self.assertIn('1 / 2', button.property('text'))

        def rendered(item):
            if not item.isVisible():
                return []
            return [item.property('text')] + [s for child in item.childItems() for s in rendered(child)]

        card = button.parentItem()
        until(lambda: '进行中' in rendered(card))
        self.assertIn('检查数据', rendered(card))
        self.controller.messages.update(0, state='stopped')
        until(lambda: '未完成' in rendered(card))
        button.clicked.emit()
        until(lambda: '核对结果' not in rendered(card))
        self.assertIn('1 / 2', button.property('text'))

    def test_planner_password_popup_saves_only_to_selected_test_home(self):
        from config_model import save_user_layer
        from revon.compat import user_layer
        import secret_store
        home = Path(self.temp.name)
        save_user_layer(home, [{'id': 'local-planner', 'config': {'baseURL': 'http://127.0.0.1:1/v1', 'model': 'm'}},
                               {'id': 'planning', 'config': {'model': ['local-planner', 'm'], 'level': 'low'}}])
        live = ChatController(home)
        context = self.engine.rootContext()
        context.setContextProperty('controller', live)
        try:
            find = lambda name: self.window.findChild(QObject, name)
            self.window.resize(820, 620)
            options = find('glassOptions')
            QMetaObject.invokeMethod(options, 'open')
            until(lambda: options.property('opened'))
            self.assertLessEqual(options.property('y') + options.property('height'), self.window.height())
            find('managePlanningKey').clicked.emit()
            popup = find('plannerKeyPopup')
            until(lambda: popup.property('opened'))
            self.assertEqual(popup.property('address'), 'http://127.0.0.1:1/v1')
            field = find('plannerPassword')
            field.setProperty('text', 'local-only-ui-fixture')
            find('savePlannerKey').clicked.emit()
            until(lambda: not popup.property('visible'))
            self.assertEqual(field.property('text'), '')
            self.assertTrue(live.planning.hasKey)
            self.assertEqual(list(secret_store.load(path=home / 'secrets.json').values()), ['local-only-ui-fixture'])
            self.assertNotIn('local-only-ui-fixture', user_layer(home).read_text(encoding='utf-8'))
        finally:
            context.setContextProperty('controller', self.controller)
            live.shutdown()

    def test_onboarding_selection_is_visible_and_persists_when_going_back(self):
        def item(name):
            widget = self.window.findChild(QObject, name)
            self.assertIsNotNone(widget, name)
            return widget

        def assert_selected(selected, other, label):
            self.assertTrue(selected.property('checked'))
            self.assertTrue(selected.findChild(QObject, 'guideSelectionOutline').property('visible'))
            self.assertEqual(selected.property('status'), label)
            self.assertEqual(selected.property('actionGlyph'), 'circle-check')
            self.assertFalse(other.property('checked'))
            self.assertEqual(other.property('status'), '')

        popup = item('onboardingPopup')
        self.assertTrue(QMetaObject.invokeMethod(popup, 'showGuide'))
        until(lambda: popup.property('opened'))
        chinese, english = item('guideChinese'), item('guideEnglish')
        assert_selected(chinese, english, '已选中')
        english.clicked.emit()
        self.assertEqual(popup.property('step'), 0)
        assert_selected(english, chinese, 'Selected')
        item('setupContinue').clicked.emit()
        simple, developer = item('guideSimple'), item('guideDeveloper')
        simple.clicked.emit()
        assert_selected(simple, developer, 'Selected')
        developer.clicked.emit()
        self.assertEqual(popup.property('step'), 1)
        assert_selected(developer, simple, 'Selected')
        item('guideBack').clicked.emit()
        assert_selected(english, chinese, 'Selected')
        item('setupContinue').clicked.emit()
        assert_selected(developer, simple, 'Selected')

    def test_chat_and_work_switch_the_composer_without_destroying_the_draft(self):
        chat = self.window.findChild(QObject, 'selectChatMode')
        work = self.window.findChild(QObject, 'selectWorkMode')
        mcp = self.window.findChild(QObject, 'chatMcpButton')
        tools = self.window.findChild(QObject, 'workToolCatalogButton')
        self.controller.draft = '保留这个草稿'
        chat.clicked.emit()
        self.assertEqual(self.controller.conversationMode, 'chat')
        self.assertTrue(mcp.isVisible())
        self.assertTrue(tools.isVisible())
        work.clicked.emit()
        self.assertEqual(self.controller.conversationMode, 'work')
        self.assertFalse(mcp.isVisible())
        self.assertTrue(tools.isVisible())
        self.assertEqual(self.controller.draft, '保留这个草稿')

    @unittest.skipUnless(os.name == 'nt', 'Windows window capture')
    def test_capture_worker_leaves_qt_service_command_pipe_available(self):
        from tests.test_computer_coordinates import OwnedWindow
        target = OwnedWindow('aware')
        self.addCleanup(target.close)
        target_id = str(target.target['window'])
        self.workbench.navigate('computer')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.setPermission('full')
        until(lambda: self.workbench.permission == 'full')
        pid = self.workbench._process.processId()
        view = lambda: self.workbench.pageData['computer']
        self.workbench.pageAction('computer', 'windows', '{}')
        until(lambda: not self.workbench.busy and any(r['id'] == target_id for r in view()['rows']))
        self.workbench.pageAction('computer', 'bind', json.dumps({'id': target_id}))
        until(lambda: not self.workbench.busy and 'computer_observe' in self.workbench.service_tools())
        previous = ''
        for _ in range(2):
            self.workbench.pageAction('computer', 'capture', '{}')
            until(lambda: not self.workbench.busy and view().get('observationId') and view()['observationId'] != previous)
            previous = view()['observationId']
            self.assertTrue(view()['image'].startswith('data:image/png;base64,'))
            self.assertEqual(self.workbench._process.processId(), pid)

    @unittest.skipUnless(os.name == 'nt', 'Windows UI Automation')
    def test_workbench_grants_background_input_to_a_live_window(self):
        folder = Path(self.temp.name)
        fixture = ROOT.parents[1] / 'tests/test_visfunc_screen.py'
        proc = subprocess.Popen([sys.executable, '-B', str(fixture), '--background-fixture', str(folder)],
                                env={**os.environ, 'QT_QPA_PLATFORM': 'windows'})
        try:
            until(lambda: (folder / 'hwnd').exists())
            hwnd, _ = json.loads((folder / 'hwnd').read_text())
            self.workbench.navigate('computer')
            until(lambda: self.workbench.nativePageReady)
            self.workbench.setPermission('full')
            until(lambda: self.workbench.permission == 'full')
            def action(name, **values):
                self.workbench.pageAction('computer', name, json.dumps(values))
            def rows():
                return self.workbench.pageData['computer']['rows']
            action('windows')
            until(lambda: not self.workbench.busy and any(r['id'] == str(hwnd) for r in rows()))
            action('bind', id=str(hwnd))
            until(lambda: not self.workbench.busy and 'computer_observe' in self.workbench.service_tools())
            self.assertNotIn('computer_act', self.workbench.service_tools())
            action('authorize_background')
            until(lambda: bool(self.workbench.approvals))
            self.workbench.resolveApproval(self.workbench.approvals[0]['id'], True)
            until(lambda: not self.workbench.busy and 'computer_act' in self.workbench.service_tools())
            self.assertNotIn('computer_click', self.workbench.service_tools())
            action('observe', clicks=True)
            until(lambda: not self.workbench.busy and any(r['title'] == 'Profile name' for r in rows()))
            def control(label):
                return next(r['id'] for r in rows() if r['title'] == label)
            ref = control('Profile name')
            action('control_type_text', id=ref, text='Workbench 后台')
            until(lambda: not self.workbench.busy and control('Profile name') != ref)
            action('control_click', id=control('Apply profile'))
            until(lambda: not self.workbench.busy and (folder / 'invocations').exists())
            self.assertEqual((folder / 'applied').read_text(encoding='utf8'), 'Workbench 后台')
            self.assertEqual((folder / 'invocations').read_text(), '1')
            action('revoke')
            until(lambda: 'computer_act' not in self.workbench.service_tools())
        finally:
            (folder / 'close').touch()
            proc.wait(timeout=8)

    def test_config_shows_warning_from_saved_policy(self):
        from config_model import save_user_layer
        save_user_layer(Path(self.temp.name), [{"id": "policy", "name": "policy:core",
            "config": {"sandbox": "read-only", "allow": ["write_file"]}}])
        self.workbench.navigate('config')
        until(lambda: self.workbench.nativePageReady and visual_item(self.window.contentItem(), 'page_config'))
        page = visual_item(self.window.contentItem(), 'page_config')
        label = page.findChild(QObject, 'configPolicyWarnings')
        until(lambda: label.isVisible() and 'write_file' in label.property('text'))
        self.assertIn('只读边界仍会拒绝', label.property('text'))

    def test_computer_preview_maps_only_image_pixels_and_disarms_on_refresh(self):
        from unittest.mock import patch
        from PySide6.QtCore import QPointF
        self.workbench.navigate('computer')
        until(lambda: self.workbench.nativePageReady and visual_item(self.window.contentItem(), 'page_computer'))
        page = visual_item(self.window.contentItem(), 'page_computer')
        sample = Path(self.temp.name) / 'window.png'
        image = QImage(200, 400, QImage.Format.Format_RGB32)
        image.fill(QColor('white'))
        self.assertTrue(image.save(str(sample)))
        data = dict(self.workbench.pageData['computer'], image=QUrl.fromLocalFile(str(sample)).toString(),
                    canInput=True, observationId='first', point=None)
        self.workbench._receive({'event': 'page', 'page': 'computer', 'data': data})
        picture = page.findChild(QObject, 'computerPreviewImage')
        button = page.findChild(QObject, 'confirmPreviewPoint')
        self.window.grabWindow()  # Lay out the off-screen test window before injecting pointer events.
        until(lambda: picture.isVisible() and picture.property('paintedWidth') > 0)
        def tap(x, y):
            QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             picture.mapToScene(QPointF(x, y)).toPoint())
        self.assertFalse(button.isEnabled())
        self.assertGreater(picture.property('imageLeft'), 4)
        tap(2, picture.height() / 2)
        self.assertFalse(button.isEnabled())
        with patch.object(self.workbench, '_send') as send:
            tap(picture.width() / 2, picture.height() / 2)
            self.assertTrue(button.isEnabled())
            send.assert_not_called()
            button.clicked.emit()
            sent = send.call_args.args[0]
            self.assertEqual((sent['action'], sent['values']['observation_id']), ('click', 'first'))
            self.assertAlmostEqual(sent['values']['x'], 100, delta=2)
            self.assertAlmostEqual(sent['values']['y'], 200, delta=2)
        self.workbench._receive({'event': 'page', 'page': 'computer', 'data': dict(data, observationId='next')})
        self.assertFalse(button.isEnabled())

    def test_work_tool_catalog_filters_and_toggles_advertised_tools(self):
        from functools import partial
        from .permissions import tool_available

        self.controller.tool_filter = lambda: partial(tool_available, 'sandbox')
        self.controller.mcp_tools = lambda: ({f'keystone_{name}': (
            {'type': 'function', 'function': {'name': f'keystone_{name}'}}, ('local', name))
            for name in ('unit_convert', 'text_transform', 'comfyui_run')}, None)
        self.window.findChild(QObject, 'workToolCatalogButton').clicked.emit()
        popup = self.window.findChild(QObject, 'workToolCatalog')
        until(lambda: popup.property('opened'))
        search = popup.findChild(QObject, 'workToolSearch')
        search.setProperty('text', '换算')
        until(lambda: len(popup.property('filtered').toVariant()) == 1)
        self.controller.toolSettings.setTool('keystone_unit_convert', False)
        self.assertFalse(self.controller.toolSettings.snapshot()[1]('keystone_unit_convert'))
        search.setProperty('text', 'comfyui')
        until(lambda: len(popup.property('filtered').toVariant()) == 1)
        self.assertFalse(popup.property('filtered').toVariant()[0]['available'])

    def test_chat_catalog_and_persisted_attachment_button(self):
        from unittest.mock import patch
        from revon_client import ChatMessage
        from .productivity_tools import call, with_productivity

        owner = self.controller
        owner.chat_mcp_tools = lambda: with_productivity(({}, None), owner.store.folder)
        owner.setConversationMode('chat')
        self.window.findChild(QObject, 'workToolCatalogButton').clicked.emit()
        popup = self.window.findChild(QObject, 'workToolCatalog')
        until(lambda: popup.property('opened'))
        self.assertTrue(popup.property('chatMode'))
        rows = owner.toolSettings.rows
        self.assertEqual(len(rows), 6)
        self.assertIn('图表与办公', {r['category'] for r in rows})
        owner.toolSettings.setTool('keystone_plot_chart', False)
        self.assertFalse(owner.toolSettings.snapshot()[1]('keystone_plot_chart'))
        QMetaObject.invokeMethod(popup, 'close')

        result = call('create_document', {'title': '周报', 'sections': [{'paragraphs': ['正文']}]},
                      owner.store.folder, threading.Event())
        owner._history = [ChatMessage('assistant', '', tool_calls=[
            {'id': 'doc', 'function': {'name': 'keystone_create_document'}}]),
            ChatMessage('tool', json.dumps(result), tool_call_id='doc'), ChatMessage('assistant', '周报已生成')]
        owner._messages.reset(owner._transcript_rows())
        until(lambda: visual_item(self.window.contentItem(), 'openToolAttachment') is not None)
        button = visual_item(self.window.contentItem(), 'openToolAttachment')
        self.assertTrue(button.isVisible())
        self.assertEqual(button.property('filename'), '周报.docx')
        with patch('qt_frontend.productivity_tools.office_fallback', return_value=None), \
             patch('qt_frontend.controller.QDesktopServices.openUrl', return_value=True) as opened:
            button.clicked.emit()
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), Path(result['path']))
        with patch('qt_frontend.productivity_tools.office_fallback', return_value=Path('WINWORD.EXE')), \
             patch.object(QProcess, 'startDetached', return_value=(True, 123)) as started, \
             patch('qt_frontend.controller.QDesktopServices.openUrl') as shell:
            button.clicked.emit()
        started.assert_called_once_with('WINWORD.EXE', [result['path']])
        shell.assert_not_called()

    def test_chart_receipt_reopens_in_app_from_history(self):
        from unittest.mock import patch
        from revon_client import ChatMessage
        from .productivity_tools import call

        owner = self.controller
        result = call('plot_chart', {'kind': 'line', 'title': '趋势', 'y': [1, 3]},
                      owner.store.folder, threading.Event())
        owner._history = [ChatMessage('assistant', '', tool_calls=[
            {'id': 'plot', 'function': {'name': 'keystone_plot_chart'}}]),
            ChatMessage('tool', json.dumps(result), tool_call_id='plot'), ChatMessage('assistant', '图表已生成')]
        owner._messages.reset(owner._transcript_rows())
        until(lambda: visual_item(self.window.contentItem(), 'openToolAttachment') is not None)
        preview = self.window.findChild(QObject, 'chartAttachmentPreview')
        image = preview.findChild(QObject, 'attachmentChartImage')
        with patch('qt_frontend.controller.QDesktopServices.openUrl') as shell:
            visual_item(self.window.contentItem(), 'openToolAttachment').clicked.emit()
            until(lambda: preview.property('opened') and image.property('progress') == 1
                  and image.property('implicitWidth') > 0)
            self.assertEqual(Path(image.property('source').toLocalFile()), Path(result['path']))
            preview.findChild(QObject, 'closeChartPreview').clicked.emit()
            until(lambda: image.property('source').isEmpty())
        shell.assert_not_called()

    def test_public_search_panel_and_browser_use_selected_engine(self):
        from urllib.parse import parse_qs, urlsplit

        panel = self.window.findChild(QObject, 'publicSearchPanel')
        self.assertIsNotNone(panel)
        button = self.window.findChild(QObject, 'publicSearchButton')
        button.clicked.emit()
        until(lambda: panel.property('opened'))
        self.assertEqual(len(self.workbench.search.sources), 8)
        self.workbench.search.setEngine('github')
        self.workbench.openBrowserAddress('PySide6 language:python')
        parsed = urlsplit(self.workbench.browserUrl)
        self.assertEqual(parsed.hostname, 'github.com')
        self.assertEqual(parse_qs(parsed.query), {'q': ['PySide6 language:python'], 'type': ['repositories']})
        self.workbench.openBrowserAddress('example.com/docs')
        self.assertEqual(self.workbench.browserUrl, 'https://example.com/docs')
        QMetaObject.invokeMethod(panel, 'close')
        until(lambda: not panel.property('visible'))

    def test_search_fallback_is_visible_in_panel_and_work_receipt(self):
        from unittest.mock import patch
        service = self.workbench.search
        service.demo = False
        panel = self.window.findChild(QObject, 'publicSearchPanel')
        self.window.findChild(QObject, 'publicSearchButton').clicked.emit()
        until(lambda: panel.property('opened'))
        status = panel.findChild(QObject, 'publicSearchStatus')
        for actual in ('duckduckgo', 'google'):
            rows = [{'title': 'Example', 'url': 'https://example.org/', 'engine': actual}]
            with patch('revon.web_backends.search', return_value=rows):
                service.run('python', 'google', 'repositories')
                until(lambda: not service.busy)
                receipt = service.call_work_tool('google', {'query': 'python'}, threading.Event())
            self.assertEqual(receipt['requested_engine'], 'google')
            self.assertEqual(receipt['engine'], actual)
            self.assertIn(f'<web-data source="{actual} public search">', receipt['content'])
            for content in (status.property('text'), receipt['content']):
                self.assertEqual('已改用 DuckDuckGo' in content, actual != 'google')
        service.reset()
        self.assertEqual(service.sourceNotice, '')

    def test_china_catalog_filters_and_opens_provider_setup(self):
        self.workbench.navigate('connectors')
        until(lambda: self.workbench.mcp.loaded and self.window.findChild(QObject, 'connectorsPage') is not None)
        page = self.window.findChild(QObject, 'connectorsPage')
        self.assertIsNotNone(page)
        page.setProperty('region', 'china')
        page.findChild(QObject, 'mcpSearch').setProperty('text', '高德')
        def rows(item):
            matches = [item.objectName()] if item.objectName().startswith('mcpRow_') else []
            for child in item.childItems():
                matches.extend(rows(child))
            return matches
        until(lambda: rows(page) == ['mcpRow_preset.amap'], timeout=2)
        page.setProperty('selectedId', 'setup.baidu-search')
        self.assertEqual(page.property('selectedSetup'), 'endpoint')

    def test_chinese_connectors_open_the_matching_login_form(self):
        self.workbench.navigate('connectors')
        until(lambda: self.workbench.mcp.loaded and self.window.findChild(QObject, 'connectorsPage') is not None)
        page = self.window.findChild(QObject, 'connectorsPage')
        form = page.findChild(QObject, 'mcpBrowserForm')
        self.assertIsNotNone(form)
        for key, mode in (('feishu', 'app-oauth'), ('baidu-netdisk', 'manual-token'),
                          ('zhihu', 'manual-token'), ('tencent-meeting', 'manual-token'),
                          ('google-cloud-run', 'google-cloud')):
            page.setProperty('selectedId', 'preset.' + key)
            self.assertTrue(QMetaObject.invokeMethod(page, 'connectSelected'))
            until(lambda: form.property('opened'))
            self.assertEqual(form.property('mode'), mode)
            QMetaObject.invokeMethod(form, 'close')
            until(lambda: not form.property('visible'))
        wecom = next(row for row in self.workbench.mcp.presets if row['id'] == 'preset.wecom')
        self.assertEqual(wecom['loginMode'], 'scan')
        self.assertTrue(wecom['local'])

    def test_wallpaper_import_persistence_and_bad_image_preserves_current_background(self):
        from .wallpaper import Wallpaper

        asset = Path(self.temp.name) / '背景.png'
        picture = QImage(64, 48, QImage.Format.Format_RGB32)
        picture.fill(QColor('#f05533'))
        self.assertTrue(picture.save(str(asset)))
        wallpaper = self.workbench.wallpaper
        wallpaper.importFile(QUrl.fromLocalFile(str(asset)), False)
        until(lambda: not wallpaper.busy)
        self.assertEqual(wallpaper.kind, 'image', wallpaper.error)
        self.assertNotEqual(wallpaper.source.toLocalFile(), str(asset))
        asset.unlink()
        background = self.window.findChild(QObject, 'stillWallpaper')
        until(lambda: background.property('loaded'))
        wallpaper.setDim(0.35)
        reopened = Wallpaper(self.temp.name)
        self.assertEqual(reopened.source, wallpaper.source)
        self.assertEqual(reopened.dim, 0.35)
        asset.write_bytes(b'invalid')
        wallpaper.importFile(QUrl.fromLocalFile(str(asset)), False)
        until(lambda: not wallpaper.busy)
        self.assertTrue(wallpaper.error)
        self.assertEqual(wallpaper.source, reopened.source)
        wallpaper.reset()
        self.assertFalse(wallpaper.kind)
        self.assertFalse(Wallpaper(self.temp.name).kind)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'requires ffmpeg to generate dynamic wallpaper fixtures')
    def test_wallpaper_animated_gif_and_video_accept_real_media(self):
        wallpaper = self.workbench.wallpaper
        for suffix, kind, object_name in (('gif', 'gif', 'gifWallpaper'), ('mp4', 'video', 'wallpaperPlayer')):
            with self.subTest(kind=kind):
                asset = Path(self.temp.name) / ('motion.' + suffix)
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                                'testsrc2=size=96x64:rate=8', '-t', '1', str(asset)],
                               capture_output=True, check=True, timeout=15)
                wallpaper.importFile(QUrl.fromLocalFile(str(asset)), True)
                until(lambda: not wallpaper.busy, timeout=18)
                self.assertEqual(wallpaper.kind, kind, wallpaper.error)
                element = self.window.findChild(QObject, object_name)
                self.assertIsNotNone(element)
                if kind == 'gif':
                    until(lambda: element.property('frameCount') > 1)
                    element.parent().setProperty('running', False)
                    self.assertTrue(element.property('paused'))
                    first_frame = element.property('currentFrame')
                    element.parent().setProperty('running', True)
                    until(lambda: element.property('currentFrame') != first_frame)
                    element.parent().setProperty('running', False)
                    self.assertTrue(element.property('paused'))
                else:
                    until(lambda: element.property('hasVideo'))
                    element.parent().setProperty('running', True)
                    until(lambda: element.property('position') > 0)
                    element.parent().setProperty('running', False)
                    QTest.qWait(100)
                    position = element.property('position')
                    QTest.qWait(170)
                    self.assertEqual(element.property('position'), position)
        wallpaper.reset()

    def test_all_features_use_retained_qml_pages_and_headless_service(self):
        self.workbench.start_host()
        until(lambda: self.workbench._service_ready)
        pid = self.workbench._process.processId()
        pages = {}
        for key in ('preview', 'config', 'tools', 'agents', 'knowledge', 'evolution', 'task',
                    'schedules', 'computer', 'files', 'development', 'connector_accounts'):
            self.workbench.navigate(key)
            until(lambda: self.workbench.nativePageReady and visual_item(self.window.contentItem(), 'page_' + key) is not None)
            page = visual_item(self.window.contentItem(), 'page_' + key)
            until(page.isVisible)
            pages[key] = page
            self.assertEqual(self.workbench._process.processId(), pid, key)
            self.assertFalse(self.workbench.error, (key, self.workbench.error))
            self.assertIsNone(self.workbench.nativeWindow)
            self.assertIsNone(self.window.findChild(QObject, 'nativeContainer'))
        self.workbench.navigate('chat')
        for page in pages.values():
            self.assertFalse(page.isVisible())
        self.workbench.navigate('config')
        until(pages['config'].isVisible)
        self.assertIs(visual_item(self.window.contentItem(), 'page_config'), pages['config'])
        for profile in ('manual', 'full', 'sandbox'):
            self.workbench.setPermission(profile)
            until(lambda: self.workbench.permission == profile)

    def test_market_filters_plugins_and_seats_without_reloading_service(self):
        self.workbench.navigate('tools')
        root = self.window.contentItem()
        until(lambda: self.workbench.nativePageReady and visual_item(root, 'marketEntry') is not None)
        listing = visual_item(root, 'marketList')
        search = visual_item(root, 'marketSearch')
        page = visual_item(root, 'page_tools')
        total = listing.property('count')
        self.assertGreater(total, 0)
        search.setProperty('text', 'no-such-plugin-937')
        until(lambda: listing.property('count') == 0)
        search.setProperty('text', '')
        until(lambda: listing.property('count') == total)
        page.setProperty('section', 'seats')
        until(lambda: listing.property('count') == 8)

    def test_coding_panel_keeps_draft_and_routes_options_to_host(self):
        self.workbench.navigate('development')
        until(lambda: self.workbench.coding.get('ready'))
        until(lambda: self.window.findChild(QObject, 'codingMessageEditor') is not None)
        editor = self.window.findChild(QObject, 'codingMessageEditor')
        self.assertTrue(editor.isVisible())
        self.workbench.setCodingDraft('中文 draft 123')
        self.workbench.codingAction('planning', 'medium')
        until(lambda: self.workbench.coding.get('planning') == 'medium')
        self.workbench.navigate('chat')
        until(lambda: not editor.isVisible())
        self.workbench.navigate('development')
        until(lambda: editor.isVisible() and self.workbench.nativePageReady)
        self.assertEqual(editor.property('text'), '中文 draft 123')
        self.assertFalse(self.workbench.error, self.workbench.error)

    def test_native_multiline_forms_are_usable_and_preserve_drafts(self):
        self.workbench.navigate('knowledge')
        until(lambda: self.workbench.nativePageReady and visual_item(self.window.contentItem(), 'page_knowledge') is not None)
        page = visual_item(self.window.contentItem(), 'page_knowledge')
        until(lambda: visual_item(page, 'field_text') is not None)
        editor = visual_item(page, 'field_text')
        until(lambda: editor.height() >= 70)
        QMetaObject.invokeMethod(editor, 'forceActiveFocus')
        event = QInputMethodEvent()
        event.setCommitString('保留这段多行草稿\n第二行')
        APP.sendEvent(editor, event)
        self.workbench.navigate('config')
        until(lambda: self.workbench.nativePageReady and visual_item(self.window.contentItem(), 'page_config') is not None)
        config_page = visual_item(self.window.contentItem(), 'page_config')
        until(lambda: visual_item(config_page, 'workbenchField_json') is not None)
        section = visual_item(config_page, 'workbenchField_json')
        self.assertFalse(section.property('expanded'))
        section.setProperty('expanded', True)
        until(lambda: visual_item(config_page, 'field_json') is not None)
        until(lambda: visual_item(config_page, 'field_json').height() >= 190)
        self.workbench.navigate('knowledge')
        until(lambda: page.isVisible() and self.workbench.nativePageReady)
        self.assertEqual(editor.property('text'), '保留这段多行草稿\n第二行')

    def test_connector_view_is_retained_and_old_native_ack_cannot_cover_it(self):
        self.workbench.navigate('config')
        old_ticket = self.workbench._navigation_ticket
        self.workbench.navigate('connectors')
        until(lambda: self.workbench.mcp.loaded and self.window.findChild(QObject, 'connectorsPage') is not None)
        page = self.window.findChild(QObject, 'connectorsPage')
        self.workbench._receive({'event': 'navigate', 'page': 'config', 'ticket': old_ticket})
        self.assertEqual(self.workbench.page, 'connectors')
        self.assertFalse(self.workbench.nativePageReady)
        self.workbench.navigate('chat')
        self.assertFalse(page.isVisible())
        self.workbench.navigate('connectors')
        until(page.isVisible)
        self.assertIs(self.window.findChild(QObject, 'connectorsPage'), page)

    def test_native_editor_accepts_keys_ime_and_keeps_draft_across_pages(self):
        project = Path(self.temp.name).resolve() / 'project'
        project.mkdir()
        path = project / 'sample.py'
        path.write_text('value = 1\n', encoding='utf-8')
        self.workbench.navigate('files')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.pageAction('files', 'project', json.dumps({'path': str(project)}))
        until(lambda: self.workbench.workspace == str(project) and not self.workbench.busy)
        self.workbench.pageAction('files', 'open', json.dumps({'path': str(path)}))
        until(lambda: self.workbench.pageData.get('files', {}).get('document', {}).get('path') == str(path))
        page = visual_item(self.window.contentItem(), 'page_files')
        editor = visual_item(page, 'codeEditorInput')
        until(lambda: editor.property('text') == 'value = 1\n')
        QMetaObject.invokeMethod(editor, 'forceActiveFocus')
        QTest.keyClick(self.window, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
        for char in '# abc123 ':
            QTest.keyClick(self.window, Qt.Key(ord(char.upper())))
        event = QInputMethodEvent()
        event.setCommitString('中文')
        APP.sendEvent(editor, event)
        self.assertIn('# abc123 中文', editor.property('text'))
        self.workbench.navigate('task')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.navigate('files')
        until(lambda: page.isVisible() and self.workbench.nativePageReady)
        self.assertIn('# abc123 中文', editor.property('text'))
        self.assertTrue(self.workbench.hasUnsaved)
        self.workbench.pageAction('files', 'save', json.dumps({'path': str(path)}))
        until(lambda: not self.workbench.hasUnsaved and not self.workbench.busy)
        self.assertIn('# abc123 中文', path.read_text(encoding='utf-8'))
        self.assertFalse(self.workbench.error, self.workbench.error)

    def test_coding_editor_jumps_searches_and_undoes_a_replacement(self):
        project = Path(self.temp.name).resolve() / 'project'
        project.mkdir()
        path = project / 'sample.py'
        contents = 'alpha x alpha\n' + 'pass\n' * 118 + 'last alpha'
        path.write_text(contents, encoding='utf-8')
        self.workbench.navigate('development')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.pageAction('development', 'project', json.dumps({'path': str(project)}))
        until(lambda: self.workbench.workspace == str(project) and not self.workbench.busy)
        self.workbench.pageAction('development', 'open', json.dumps({'path': str(path)}))
        until(lambda: visual_item(self.window.contentItem(), 'page_development') is not None)
        page = visual_item(self.window.contentItem(), 'page_development')
        until(lambda: page.property('editorPath') == str(path))
        editor = visual_item(page, 'codeEditorInput')
        gutter = visual_item(page, 'editorLineNumbers')
        self.assertTrue(gutter.isVisible())
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.window.show()
        self.window.requestActivate()
        until(self.window.isActive)
        QMetaObject.invokeMethod(editor, 'forceActiveFocus')
        QTest.keyClick(self.window, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
        jump = visual_item(page, 'editorJumpInput')
        self.assertTrue(jump.isVisible())
        jump.setProperty('text', '121')
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assertTrue(jump.isVisible())
        jump.setProperty('text', '120:6')
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assertEqual(editor.property('cursorPosition'), contents.rindex('alpha'))
        self.assertEqual(visual_item(page, 'editorPosition').property('text'), '行 120，列 6')
        self.assertGreater(gutter.property('firstLine'), 0)

        QTest.keyClick(self.window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        search = visual_item(page, 'editorFindInput')
        count = visual_item(page, 'editorMatchCount')
        self.assertTrue(search.isVisible())
        search.setProperty('text', 'alpha')
        self.assertEqual(count.property('text'), '3 处')
        QTest.keyClick(self.window, Qt.Key.Key_F3)
        self.assertEqual(count.property('text'), '3 / 3')
        QTest.keyClick(self.window, Qt.Key.Key_F3)
        self.assertEqual(count.property('text'), '1 / 3')
        QTest.keyClick(self.window, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(count.property('text'), '3 / 3')
        visual_item(page, 'editorReplaceInput').setProperty('text', 'omega')
        visual_item(page, 'replaceEditorMatch').clicked.emit()
        until(lambda: self.workbench.hasUnsaved)
        self.assertTrue(editor.property('text').endswith('last omega'))
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(search.isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(editor.property('text'), contents)
        until(lambda: not self.workbench.hasUnsaved)

    def test_terminal_tabs_restore_drafts_and_isolate_background_output(self):
        wb = self.workbench
        wb.navigate('development')
        until(lambda: wb.nativePageReady)
        page = visual_item(self.window.contentItem(), 'page_development')
        visual_item(page, 'codeRail_terminal').clicked.emit()
        entry = visual_item(page, 'codeTerminalInput')
        first = wb.activeTerminal
        entry.setProperty('text', 'unfinished')
        visual_item(page, 'terminalNew').clicked.emit()
        second = wb.activeTerminal
        self.assertNotEqual(first, second)
        self.assertEqual(entry.property('text'), '')
        for identifier, marker in ((first, 'first output'), (second, 'second output')):
            wb._receive({'event': 'terminal', 'root': wb.workspace, 'session': identifier,
                         'text': '', 'screen': marker, 'running': True})
        self.assertEqual(wb.terminalText, 'second output')
        visual_item(page, f'terminalSession_{first}').clicked.emit()
        self.assertEqual(entry.property('text'), 'unfinished')
        self.assertEqual(wb.terminalText, 'first output')
        visual_item(page, 'terminalClose').clicked.emit()
        self.assertEqual(wb.activeTerminal, second)
        wb._receive({'event': 'terminal', 'root': wb.workspace, 'session': first,
                     'text': 'late output', 'running': False})
        self.assertEqual(wb.terminalText, 'second output')
        self.assertTrue(wb.terminalRunning)
        wb._receive({'event': 'terminal', 'root': wb.workspace, 'text': 'test output', 'reveal': True})
        self.assertEqual(page.property('dockTab'), 'terminal')
        visual_item(page, 'codeDockTab_output').clicked.emit()
        self.assertEqual(wb.terminalOutput, 'test output')

    def test_code_terminal_dock_preserves_output_and_editor_across_toggles(self):
        project = Path(self.temp.name).resolve() / 'project'
        subprocess.run(['git', 'init', '-q', str(project)], check=True, capture_output=True)
        self.workbench.navigate('development')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.pageAction('development', 'project', json.dumps({'path': str(project)}))
        until(lambda: self.workbench.workspace == str(project) and not self.workbench.busy)
        until(lambda: visual_item(self.window.contentItem(), 'page_development') is not None)
        page = visual_item(self.window.contentItem(), 'page_development')
        rail = visual_item(page, 'codeRail_terminal')
        rail.clicked.emit()
        transcript = visual_item(page, 'codeTerminalTranscript')
        self.assertTrue(transcript.isVisible())
        self.assertTrue(visual_item(page, 'codeProjectTree').isVisible())
        visual_item(page, 'terminalGitStatus').clicked.emit()
        until(lambda: '退出码 0' in self.workbench.terminalText)
        self.assertIn('## ', transcript.property('text'))
        rail.clicked.emit()
        self.assertFalse(transcript.isVisible())
        self.workbench.navigate('chat')
        self.workbench.navigate('development')
        until(lambda: self.workbench.nativePageReady and page.isVisible())
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.window.show()
        self.window.requestActivate()
        until(self.window.isActive)
        QTest.keyClick(self.window, Qt.Key.Key_QuoteLeft, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(transcript.isVisible())
        self.assertIn('退出码 0', transcript.property('text'))
        self.assertTrue(visual_item(page, 'codeTerminalInput').property('readOnly'))
        visual_item(page, 'codeDockTab_output').clicked.emit()
        self.assertEqual(page.property('dockTab'), 'output')
        QTest.keyClick(self.window, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(transcript.isVisible())

    def open_code_project(self, files):
        project = Path(self.temp.name).resolve() / '项目 空格'
        for name, text in files.items():
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            (project / name).write_text(text, encoding='utf-8')
        self.workbench.navigate('development')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.pageAction('development', 'project', json.dumps({'path': str(project)}))
        until(lambda: self.workbench.workspace == str(project) and not self.workbench.busy)
        page = visual_item(self.window.contentItem(), 'page_development')
        return project, page

    def test_code_tree_labels_stay_inside_their_rows(self):
        project, page = self.open_code_project({'src/主 程序.py': 'x = 1\n', 'b.js': ''})
        tree = visual_item(page, 'codeProjectTree')
        until(lambda: tree.property('count') == 2)
        QTest.qWait(100)
        rows = [item for item in tree.property('contentItem').childItems() if item.property('modelData')]
        self.assertTrue(rows)
        for row in rows:
            content = row.property('contentItem')
            label = next(c for c in content.childItems() if c.property('text') == row.property('modelData')['title'])
            top = label.mapToItem(row, label.boundingRect().topLeft()).y()
            # The label box must sit inside the row (selection rectangle), centred within a pixel.
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(top + label.height(), row.height())
            self.assertAlmostEqual(top + label.height() / 2, row.height() / 2, delta=1.0)

    def test_code_run_state_problems_jump_to_file_line(self):
        project, page = self.open_code_project({'src/主 程序.cpp': 'int main() {\n  return y;\n}\n', 'app.py': 'print(1)\n'})
        self.workbench.pageAction('development', 'open', json.dumps({'path': str(project / 'app.py')}))
        until(lambda: self.workbench.runState.get('file') == str(project / 'app.py') and not self.workbench.busy)
        self.assertIn('file.run', [t['id'] for t in self.workbench.runState['tasks']])
        self.assertEqual(self.workbench.runState['toolchains'][0]['language'], 'python')
        self.assertFalse(self.workbench.runState['canRun'], 'preview/sandbox never executes project code')
        run = visual_item(page, 'codeRunFile')
        self.assertTrue(run.isVisible())
        target = str(project / 'src' / '主 程序.cpp')
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.window.show()
        self.window.requestActivate()
        until(self.window.isActive)
        self.workbench._run = {**self.workbench._run, 'problems': [
            {'path': target, 'line': 2, 'column': 10, 'severity': 'error', 'message': "'y' was not declared",
             'code': '', 'source': 'gcc', 'inWorkspace': True, 'relative': 'src/主 程序.cpp'}],
            'last': {'label': '编译并运行', 'status': 'failed', 'stamp': 1.0}}
        self.workbench.runChanged.emit()
        until(lambda: page.property('dockTab') == 'problems')
        problems = visual_item(page, 'codeProblemList')
        until(lambda: problems.isVisible() and problems.property('count') == 1)
        problems.setProperty('currentIndex', 0)
        until(lambda: problems.property('currentItem') is not None and problems.property('height') > 0)
        QTest.qWait(300)
        item = problems.property('currentItem')
        from PySide6.QtCore import QPointF
        point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        until(lambda: page.property('editorPath') == target and page.property('cursorLine') == 1)

    def test_code_tree_quick_open_and_workspace_matches(self):
        project = Path(self.temp.name).resolve() / 'project'
        folder = project / 'src' / 'nested'
        folder.mkdir(parents=True)
        file = folder / 'example.py'
        other = project / 'readme.md'
        file.write_bytes(('start\r\n😀 中文 Needle needle\r\n').encode('utf-16'))
        other.write_text('Needle in the readme', encoding='utf-8')
        self.workbench.navigate('development')
        until(lambda: self.workbench.nativePageReady)
        self.workbench.pageAction('development', 'project', json.dumps({'path': str(project)}))
        until(lambda: self.workbench.workspace == str(project) and not self.workbench.busy)
        page = visual_item(self.window.contentItem(), 'page_development')
        tree = visual_item(page, 'codeProjectTree')
        until(lambda: tree.property('count') == 2)
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.window.show()
        self.window.requestActivate()
        until(self.window.isActive)
        ctrl, shift = Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier
        QTest.keyClick(self.window, Qt.Key.Key_E, ctrl | shift)
        until(tree.hasActiveFocus)
        tree.setProperty('currentIndex', 0)
        QTest.keyClick(self.window, Qt.Key.Key_Right)
        until(lambda: tree.property('count') == 3 and not self.workbench.busy)
        QTest.keyClick(self.window, Qt.Key.Key_Left)
        until(lambda: tree.property('count') == 2 and not self.workbench.busy)

        QTest.keyClick(self.window, Qt.Key.Key_P, ctrl)
        until(lambda: visual_item(self.window.contentItem(), 'quickOpenInput') is not None)
        query = visual_item(self.window.contentItem(), 'quickOpenInput')
        until(query.hasActiveFocus)
        query.setProperty('text', 'sxmpy')
        choices = visual_item(self.window.contentItem(), 'quickOpenResults')
        until(lambda: choices.property('count') == 1)
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        until(lambda: page.property('editorPath') == str(file) and not self.workbench.busy)
        until(lambda: tree.property('count') == 4)
        editor = visual_item(page, 'codeEditorInput')
        until(editor.hasActiveFocus)
        self.assertEqual(editor.property('cursorPosition'), 0)
        QTest.keyClick(self.window, Qt.Key.Key_F, ctrl | shift)
        search = visual_item(page, 'workspaceSearchInput')
        until(search.hasActiveFocus)
        search.setProperty('text', 'wrong')
        search.setProperty('text', 'needle')
        hits = visual_item(page, 'workspaceSearchResults')
        summary = visual_item(page, 'workspaceSearchSummary')
        until(lambda: summary.property('text') == '3 处匹配，2 个文件')
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        until(editor.hasActiveFocus)
        self.assertEqual(editor.property('selectedText'), 'Needle')
        self.assertEqual(visual_item(page, 'editorPosition').property('text'), '行 2，列 13')
        hits.setProperty('currentIndex', 2)
        QMetaObject.invokeMethod(hits, 'forceActiveFocus')
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        until(lambda: page.property('editorPath') == str(other) and editor.hasActiveFocus())
        self.assertEqual(editor.property('selectedText'), 'Needle')
        self.assertTrue(visual_item(page, 'workspaceSearchInput').isVisible())

    def test_workspace_pairing_can_select_every_service_or_a_subset(self):
        self.workbench.navigate('connectors')
        until(lambda: self.workbench.mcp.loaded and self.window.findChild(QObject, 'connectorPairing_google') is not None)
        popup = self.window.findChild(QObject, 'connectorPairing_google')
        microsoft = self.window.findChild(QObject, 'connectorPairing_microsoft')
        QMetaObject.invokeMethod(popup, 'show', Q_ARG('QVariant', ''))
        until(lambda: popup.property('opened'))
        select_all = popup.findChild(QObject, 'pairSelectAll')
        submit = popup.findChild(QObject, 'pairSubmit')
        self.assertFalse(submit.property('enabled'))
        select_all.clicked.emit()
        self.assertEqual(submit.property('text'), '配对全部')
        self.assertEqual(popup.findChild(QObject, 'pairOptions').property('count'), 10)
        self.assertTrue(any(row['id'] == 'google.universal-search' for row in self.workbench.mcp.families))
        self.assertTrue(submit.property('enabled'))
        QMetaObject.invokeMethod(popup, 'toggle', Q_ARG('QVariant', 'google.gmail'))
        self.assertEqual(submit.property('text'), '配对所选')
        submit.clicked.emit()
        self.assertIn('google', self.workbench.mcp.pairErrors)
        self.assertNotIn('microsoft', self.workbench.mcp.pairErrors)
        QMetaObject.invokeMethod(popup, 'close')
        QMetaObject.invokeMethod(microsoft, 'show', Q_ARG('QVariant', ''))
        until(lambda: microsoft.property('opened'))
        self.assertEqual(microsoft.findChild(QObject, 'pairOptions').property('count'), 6)
        microsoft.findChild(QObject, 'pairSelectAll').clicked.emit()
        microsoft.findChild(QObject, 'pairSubmit').clicked.emit()
        self.assertEqual(set(self.workbench.mcp.pairErrors), {'microsoft'})

    def test_browser_retains_webengine_instance_and_rejects_local_navigation(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            self.workbench.openBrowser(url)
            until(lambda: self.window.findChild(QObject, 'workBrowser') is not None)
            web = self.window.findChild(QObject, 'workBrowser')
            until(lambda: web.property('title') == 'revon retained browser test')
            self.workbench.toggleBrowserWidth()
            self.assertTrue(self.workbench.browserExpanded)
            self.assertIs(self.window.findChild(QObject, 'workBrowser'), web)
            self.workbench.toggleBrowserWidth()
            self.assertFalse(self.workbench.browserExpanded)
            self.workbench.toggleBrowser()
            QTest.qWait(50)
            self.assertFalse(self.workbench.browserVisible)
            self.workbench.toggleBrowser()
            self.assertIs(self.window.findChild(QObject, 'workBrowser'), web)
            self.assertEqual(web.property('title'), 'revon retained browser test')
            self.workbench.openBrowser('file:///C:/private.txt')
            self.assertEqual(self.workbench.browserUrl, url)
            self.workbench.openBrowser('https://name:password@example.com')
            self.assertEqual(self.workbench.browserUrl, url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_files_can_collapse_to_the_previous_page_and_reuse_host(self):
        self.workbench.navigate('connectors')
        self.workbench.toggleFiles()
        until(lambda: self.workbench.nativePageReady)
        self.assertEqual(self.workbench.page, 'files')
        until(lambda: visual_item(self.window.contentItem(), 'page_files') is not None)
        host = visual_item(self.window.contentItem(), 'page_files')
        self.workbench.toggleFiles()
        until(lambda: self.workbench.page == 'connectors')
        self.assertFalse(host.isVisible())
        self.workbench.toggleFiles()
        until(lambda: self.workbench.nativePageReady)
        self.assertIs(visual_item(self.window.contentItem(), 'page_files'), host)

    def test_approval_is_single_request_and_cancellation_cleans_panel(self):
        cancel, result = threading.Event(), []
        thread = threading.Thread(target=lambda: result.append(self.workbench.approve('write_file', {'path': 'a'}, cancel)))
        thread.start()
        until(lambda: bool(self.workbench.approvals))
        self.workbench.setPermission('full')
        self.assertEqual(self.workbench.permission, 'sandbox')
        self.workbench.resolveApproval(self.workbench.approvals[0]['id'], True)
        until(lambda: not thread.is_alive())
        self.assertEqual(result, [True])
        self.assertFalse(self.workbench.approvals)
        result.clear()
        thread = threading.Thread(target=lambda: result.append(self.workbench.approve('write_file', {'path': 'a'}, cancel)))
        thread.start()
        until(lambda: bool(self.workbench.approvals))
        cancel.set()
        until(lambda: not thread.is_alive() and not self.workbench.approvals)
        self.assertEqual(result, [False])

    def test_unsaved_content_prevents_accidental_window_close(self):
        self.workbench._unsaved = True
        self.workbench.changed.emit()
        self.assertFalse(self.window.close())
        until(lambda: self.window.findChild(QObject, 'exitPopup').property('opened'))
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.property('exitConfirmed'))


if __name__ == '__main__':
    unittest.main()
