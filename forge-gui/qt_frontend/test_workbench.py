"""Exercise the real child host, retained WebEngine view and approval handshake."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'Basic')
from PySide6.QtCore import QObject, QProcess, Qt, QUrl
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtTest import QTest
from PySide6.QtWebEngineQuick import QtWebEngineQuick
from .controller import ChatController
from .workbench import WorkbenchController

QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Direct3D11)
QtWebEngineQuick.initialize()
APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)
ROOT = Path(__file__).parent


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(20)
    raise AssertionError('condition timed out')


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

    def test_all_features_share_one_host_and_chat_releases_surface(self):
        self.workbench.start_host()
        until(lambda: self.workbench.nativeWindow is not None)
        pid = self.workbench._process.processId()
        until(lambda: self.workbench._page_serial > 0)
        self.assertEqual(self.workbench.page, 'chat')
        self.assertFalse(self.workbench.nativeWindow.isVisible())
        for page in ('preview', 'config', 'tools', 'agents', 'knowledge', 'evolution', 'task',
                     'schedules', 'connectors', 'computer', 'files', 'development', 'work', 'chat'):
            serial = self.workbench._page_serial
            self.workbench.navigate(page)
            until(lambda: self.workbench._page_serial > serial and self.workbench.page == page)
            QTest.qWait(180)
            self.assertEqual(self.workbench._process.processId(), pid, page)
            self.assertFalse(self.workbench.error, (page, self.workbench.error))
            if page != 'chat':
                self.assertIsNotNone(self.workbench.nativeWindow.parent(), page)
                self.assertGreater(self.workbench.nativeWindow.width(), 250)
        self.assertFalse(self.workbench.nativeWindow.isVisible())
        for profile in ('manual', 'full', 'sandbox'):
            self.workbench.setPermission(profile)
            until(lambda: self.workbench.permission == profile)

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
