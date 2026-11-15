"""Real Qt scene graph tests: input/IME, geometry, and optical texture sampling."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
from PySide6.QtCore import QObject, QCoreApplication, QMetaObject, QPointF, Qt, QUrl
from PySide6.QtGui import QFont, QGuiApplication, QInputMethodEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtTest import QTest
from .controller import ChatController
from .workbench import WorkbenchController
from ._test_app import APP, FONT
ROOT = Path(__file__).parent


def type_keys(window, text):
    for character in text:
        modifier = Qt.KeyboardModifier.ShiftModifier if character.isupper() else Qt.KeyboardModifier.NoModifier
        QTest.keyClick(window, Qt.Key(ord(character.upper())), modifier)


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = ChatController(self.temp.name, demo=True)
        self.workbench = WorkbenchController(self.temp.name, demo=True)
        self.engine = QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda rows: self.warnings.extend(str(x) for x in rows))
        context = self.engine.rootContext()
        context.setContextProperty("controller", self.controller)
        context.setContextProperty("workbench", self.workbench)
        context.setContextProperty("gpuEnabled", True)
        context.setContextProperty("uiFont", FONT)
        context.setContextProperty("brandUrl", QUrl.fromLocalFile(str(ROOT.parent / "assets" / "forge-logo-104.png")))
        self.engine.load(QUrl.fromLocalFile(str(ROOT / "qml" / "Main.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()
        QTest.qWait(250)

    def tearDown(self):
        self.workbench.shutdown()
        self.controller.shutdown()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, 0)
        APP.processEvents()
        self.temp.cleanup()
        self.assertFalse(self.warnings, self.warnings)

    def test_native_input_ime_commit_and_resize_keep_cursor_inside_editor(self):
        editor = self.window.findChild(QObject, "messageEditor")
        editor.forceActiveFocus()
        type_keys(self.window, "abc 123")
        self.assertEqual(self.controller.draft, "abc 123")
        preedit = QInputMethodEvent("zhong", [])
        QCoreApplication.sendEvent(editor, preedit)
        self.assertTrue(editor.property("inputMethodComposing"))
        commit = QInputMethodEvent()
        commit.setCommitString("中文 ABC")
        QCoreApplication.sendEvent(editor, commit)
        self.assertIn("中文 ABC", self.controller.draft)
        for width, height in ((820, 620), (1280, 860), (1600, 1000)):
            self.window.resize(width, height)
            QTest.qWait(100)
            cursor = editor.property("cursorRectangle")
            point = editor.mapToScene(QPointF(cursor.x(), cursor.y()))
            self.assertGreaterEqual(point.y(), 0)
            self.assertLess(point.y() + cursor.height(), height)
            self.assertLess(point.x(), width)
            ime_rect = APP.inputMethod().cursorRectangle()
            self.assertAlmostEqual(ime_rect.y(), point.y(), delta=2)
        self.window.showMaximized()
        QTest.qWait(100)
        maximized_cursor = APP.inputMethod().cursorRectangle()
        self.window.showNormal()
        self.window.resize(820, 620)
        QTest.qWait(100)
        self.assertGreater(maximized_cursor.y(), APP.inputMethod().cursorRectangle().y())

    def test_popover_returns_focus_and_preserves_draft(self):
        editor = self.window.findChild(QObject, "messageEditor")
        editor.forceActiveFocus()
        type_keys(self.window, "draft")
        popup = self.window.findChild(QObject, "glassOptions")
        QMetaObject.invokeMethod(popup, "open")
        QTest.qWait(50)
        glass = self.window.findChild(QObject, "optionsGlass")
        actual = glass.mapToScene(QPointF(0, 0))
        sampled = glass.property("scenePoint")
        self.assertAlmostEqual(sampled.x(), actual.x(), delta=1)
        self.assertAlmostEqual(sampled.y(), actual.y(), delta=1)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        QTest.qWait(50)
        self.assertFalse(popup.property("opened"))
        self.assertTrue(editor.hasActiveFocus())
        type_keys(self.window, "123")
        self.assertEqual(self.controller.draft, "draft123")

    def test_quick_setup_restores_provider_and_selected_region(self):
        setup = self.workbench.onboarding
        setup._preferred_vendor = 'openrouter'
        panel = self.window.findChild(QObject, "onboardingPopup")
        QMetaObject.invokeMethod(panel, "open")
        QTest.qWait(100)
        vendor = self.window.findChild(QObject, "setupVendor")
        endpoint = self.window.findChild(QObject, "setupEndpoint")
        self.assertEqual(vendor.property("currentValue"), "openrouter")
        self.assertEqual(endpoint.property("text"), "https://openrouter.ai/api/v1")
        QMetaObject.invokeMethod(panel, "close")
        QTest.qWait(100)
        catalog = self.window.findChild(QObject, "providerCatalog")
        self.workbench.showProviderCatalog("阿里云")
        QTest.qWait(100)
        preset = next(row for row in self.workbench.providers if row['key'] == 'qwen')
        def row_with_text(item, text):
            if item.property('text') == text and item.property('detail') is not None:
                return item
            for child in item.childItems():
                found = row_with_text(child, text)
                if found is not None:
                    return found
        def click_row(text):
            item = row_with_text(self.window.contentItem(), text)
            self.assertIsNotNone(item, text)
            point = item.mapToScene(item.boundingRect().center()).toPoint()
            QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, pos=point)
            QTest.qWait(100)
        click_row(preset['name'])
        click_row("国际 · 新加坡")
        QTest.qWait(100)
        self.assertFalse(catalog.property("opened"))
        self.assertTrue(panel.property("opened"))
        plan = self.window.findChild(QObject, "setupPlan")
        self.assertEqual(vendor.property("currentValue"), "qwen")
        self.assertEqual(plan.property("currentIndex"), 1)
        self.assertEqual(plan.property("currentValue"), endpoint.property("text"))
        QMetaObject.invokeMethod(panel, "close")

    def test_media_settings_keep_actions_visible_in_small_window(self):
        self.window.resize(820, 620)
        service = self.workbench.mediaModels
        service._save('', 'openai-images', 'http://127.0.0.1:1/v1', 'fixture', '', True)
        service._config = service._read()
        service.changed.emit()
        panel = self.window.findChild(QObject, "mediaModelsPopup")
        QMetaObject.invokeMethod(panel, "showSettings")
        QTest.qWait(100)
        service._notice = "请检查接口配置，保存后可以继续生成。" * 10
        service.changed.emit()
        QTest.qWait(100)
        self.assertGreaterEqual(panel.property("y"), 0)
        self.assertLessEqual(panel.property("y") + panel.property("height"), self.window.height())
        for name in ("mediaSave", "mediaSkip"):
            button = self.window.findChild(QObject, name)
            bottom = button.mapToScene(QPointF(0, button.height())).y()
            self.assertLess(bottom, self.window.height())
            self.assertTrue(button.isVisible())
        self.window.grabWindow().save(str(Path(tempfile.gettempdir()) / "forge-audit-media-small.png"))
        QMetaObject.invokeMethod(panel, "close")

    def test_long_draft_does_not_hide_send_at_minimum_window(self):
        self.window.resize(820, 620)
        self.controller.draft = "long draft 中文\n" * 60
        QTest.qWait(150)
        editor = self.window.findChild(QObject, "messageEditor")
        send = self.window.findChild(QObject, "sendButton")
        composer = self.window.findChild(QObject, "composer")
        point = send.mapToScene(QPointF(0, 0))
        self.assertLess(point.y() + send.height(), 620)
        self.assertLess(point.x() + send.width(), 820)
        self.assertGreater(composer.y(), 150)
        self.assertEqual(editor.property("text"), self.controller.draft)

    def test_gpu_shader_refracts_live_background_pixels(self):
        probe_engine = QQmlApplicationEngine()
        probe_engine.load(QUrl.fromLocalFile(str(ROOT / "qml" / "ShaderProbe.qml")))
        probe = probe_engine.rootObjects()[0]
        try:
            QTest.qWait(180)
            initial = probe.grabWindow()
            self.assertFalse(initial.isNull())
            self.assertNotEqual(probe.rendererInterface().graphicsApi(), QSGRendererInterface.GraphicsApi.Software)
            probe.setProperty("strength", 1.0)
            QTest.qWait(100)
            refracted = probe.grabWindow()
            scale = initial.width() / probe.width()
            changes = []
            for x in range(75, 91):
                for y in range(110, 240, 3):
                    a = initial.pixelColor(round(x * scale), round(y * scale))
                    b = refracted.pixelColor(round(x * scale), round(y * scale))
                    changes.append(abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue()))
            self.assertGreater(sum(changes) / len(changes), 12, "optics must change pixels beyond the 1px outline")
            probe.setProperty("phase", 9)
            QTest.qWait(100)
            moved = probe.grabWindow()
            live_changes = []
            for x in range(160, 360, 3):
                a = refracted.pixelColor(round(x * scale), round(170 * scale))
                b = moved.pixelColor(round(x * scale), round(170 * scale))
                live_changes.append(abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue()))
            self.assertGreater(sum(live_changes) / len(live_changes), 30,
                               "glass must sample the live scene, not a fixed texture")
        finally:
            probe.close()
            probe_engine.deleteLater()
            APP.processEvents()

    def test_change_card_diff_popup_and_confirmed_revert(self):
        from forge.change_review import ChangeReviewStore
        project = Path(self.temp.name) / 'project'
        project.mkdir()
        store = ChangeReviewStore(Path(self.temp.name) / 'host', project)
        file = project / 'composer.qml'
        original = b'Item {\n    width: 640\n    height: 100\n}\n'
        file.write_bytes(original)
        before = store.capture(file)
        file.write_bytes(b'Item {\n    width: Math.min(800, parent.width - 48)\n    height: content.height + 32\n    clip: true\n}\n')
        ids = [store.record(file, before)]
        added = project / 'typography.py'
        before = store.capture(added)
        added.write_bytes(b'BODY_SIZE = 15\nLINE_HEIGHT = 1.4\n')
        ids.append(store.record(added, before))
        class LocalClient:
            def review_changes(self, identifiers, revert=False):
                return store.revert(identifiers, lambda *_: None) if revert else store.review(identifiers)
        self.controller.factory = lambda *_: LocalClient()
        self.controller.tool_authorizer = lambda: lambda *_: None
        service = self.controller.changeReview
        service.entries['fixture'] = {'ids': ids, 'endpoint': 'isolated-fixture', 'anchor': 0, **store.review(ids)}
        self.controller.messages.reset([{'roleName': 'assistant', 'textContent': '已调整输入区域的宽度与留白，并统一正文排版。\n\n' * 24,
            'state': 'complete', 'changeKey': 'fixture', 'changeInfo': service.card('fixture')}])
        QTest.qWait(200)
        def find_card(item):
            if item.objectName() == 'replyChangeCard':
                return item
            for child in item.childItems():
                found = find_card(child)
                if found is not None:
                    return found
        card = find_card(self.window.contentItem())
        self.assertIsNotNone(card)
        composer = self.window.findChild(QObject, 'composer')
        self.assertLessEqual(card.mapToScene(QPointF(0, card.height())).y(), composer.y())
        capture = os.environ.get('FORGE_TEST_CAPTURE_DIR')
        if capture:
            target = Path(capture); target.mkdir(parents=True, exist_ok=True)
            self.window.grabWindow().save(str(target / 'reply-change-card.png'))
        service.open('fixture')
        for _ in range(200):
            QTest.qWait(10)
            if not service.busy:
                break
        popup = self.window.findChild(QObject, 'changeReviewPopup')
        self.assertTrue(popup.property('opened'))
        self.assertTrue(service.data['canRevert'])
        selector = self.window.findChild(QObject, 'changeFileSelector')
        point = selector.mapToScene(QPointF(selector.width() / 2, selector.height() / 2)).toPoint()
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, pos=point)
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        QTest.qWait(50)
        self.assertEqual(popup.property('fileIndex'), 1)
        self.assertEqual(selector.property('currentText'), 'typography.py')
        button = self.window.findChild(QObject, 'confirmRevertButton')
        self.assertTrue(button.property('enabled'))
        if capture:
            self.window.grabWindow().save(str(target / 'reply-change-diff.png'))
        self.window.resize(820, 620)
        QTest.qWait(150)
        self.assertLessEqual(popup.property('width'), 820 - 60)
        self.assertLessEqual(popup.property('height'), 620 - 60)
        self.assertGreater(self.window.findChild(QObject, 'changeDiffLines').height(), 100)
        if capture:
            self.window.grabWindow().save(str(target / 'reply-change-compact.png'))
        QMetaObject.invokeMethod(button, 'clicked')
        self.assertTrue(popup.property('confirmUndo'))
        self.assertNotEqual(file.read_bytes(), original)
        QMetaObject.invokeMethod(button, 'clicked')
        for _ in range(200):
            QTest.qWait(10)
            if not service.busy:
                break
        self.assertTrue(service.data['restored'], service.error)
        self.assertEqual(file.read_bytes(), original)
        self.assertFalse(added.exists())
        # Refreshing the model after revert must not leave the selector and diff on different files.
        self.assertEqual(popup.property('fileIndex'), selector.property('currentIndex'))
        current_file = popup.property('currentFile')
        self.assertEqual(current_file['name'], selector.property('currentText'))
        QMetaObject.invokeMethod(popup, 'close')


if __name__ == "__main__":
    unittest.main()
