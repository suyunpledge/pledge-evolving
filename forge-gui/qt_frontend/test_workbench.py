"""Exercise the real child host, retained WebEngine view and approval handshake."""
import json
import os
import shutil
import subprocess
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
            self.wfile.write(b'<title>Forge retained browser test</title><input value="draft123">')
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
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets/forge-logo-104.png'))}.items():
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
        self.assertFalse(tools.isVisible())
        work.clicked.emit()
        self.assertEqual(self.controller.conversationMode, 'work')
        self.assertFalse(mcp.isVisible())
        self.assertTrue(tools.isVisible())
        self.assertEqual(self.controller.draft, '保留这个草稿')

    def test_work_tool_catalog_filters_and_toggles_advertised_tools(self):
        from functools import partial
        from .permissions import tool_available

        self.controller.tool_filter = lambda: partial(tool_available, 'sandbox')
        self.controller.mcp_tools = lambda: ({f'forge_{name}': (
            {'type': 'function', 'function': {'name': f'forge_{name}'}}, ('local', name))
            for name in ('unit_convert', 'text_transform', 'comfyui_run')}, None)
        self.window.findChild(QObject, 'workToolCatalogButton').clicked.emit()
        popup = self.window.findChild(QObject, 'workToolCatalog')
        until(lambda: popup.property('opened'))
        search = popup.findChild(QObject, 'workToolSearch')
        search.setProperty('text', '换算')
        until(lambda: len(popup.property('filtered').toVariant()) == 1)
        self.controller.toolSettings.setTool('forge_unit_convert', False)
        self.assertFalse(self.controller.toolSettings.snapshot()[1]('forge_unit_convert'))
        search.setProperty('text', 'comfyui')
        until(lambda: len(popup.property('filtered').toVariant()) == 1)
        self.assertFalse(popup.property('filtered').toVariant()[0]['available'])

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
            until(lambda: web.property('title') == 'Forge retained browser test')
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
            self.assertEqual(web.property('title'), 'Forge retained browser test')
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
