"""Human-factors guarantees of the Qt front end (see docs/qt-human-factors-review.md).

Keyboard reachability, visible focus, proportional confirmation, turn feedback and
text contrast are behaviours, so they are pinned here rather than left to screenshots.
"""
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, Qt, QUrl
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
from PySide6.QtTest import QTest
from .controller import ChatController
from .workbench import WorkbenchController
from ._test_app import APP, FONT
ROOT = Path(__file__).parent


def _luminance(hex_color):
    value = hex_color.lstrip("#")[-6:]
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


class ThemeContrastTests(unittest.TestCase):
    # Lightest glass the UI renders text on: a selected surface over the aurora band.
    LIGHTEST_GLASS = "#24384a"

    def theme(self):
        text = (ROOT / "qml" / "Theme.qml").read_text(encoding="utf-8")
        return dict(re.findall(r'property color (\w+): "(#[0-9a-fA-F]{6,8})"', text))

    def test_text_tokens_meet_wcag_aa_on_lightest_glass(self):
        theme = self.theme()
        for name in ("textPrimary", "textSecondary", "textMuted"):
            self.assertGreaterEqual(contrast(theme[name], self.LIGHTEST_GLASS), 4.5, name)
        # Dim text is decorative/disabled only, but must still be distinguishable.
        self.assertGreaterEqual(contrast(theme["textDim"], self.LIGHTEST_GLASS), 3.0)
        self.assertGreaterEqual(contrast(theme["accentInk"], theme["primaryFill"]), 3.0)

    def test_focus_ring_stands_out_from_every_surface(self):
        theme = self.theme()
        # The ring is drawn 3px outside the control, so it sits on the surrounding surface.
        for surface in (theme["bgBase"], self.LIGHTEST_GLASS, theme["solidSurface"]):
            self.assertGreaterEqual(contrast(theme["focusRing"], surface), 3.0, surface)


class HumanFactorsSceneTests(unittest.TestCase):
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
        context.setContextProperty("brandUrl", QUrl.fromLocalFile(str(ROOT.parent / "assets" / "keystone-logo-104.png")))
        self.engine.load(QUrl.fromLocalFile(str(ROOT / "qml" / "Main.qml")))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.requestActivate()
        QTest.qWait(250)

    def tearDown(self):
        self.workbench._pending = []
        self.controller._busy = False
        self.workbench.shutdown()
        self.controller.shutdown()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, 0)
        APP.processEvents()
        self.temp.cleanup()
        self.assertFalse(self.warnings, self.warnings)

    def js(self, code):
        expression = QQmlExpression(self.engine.contextForObject(self.window), self.window, code)
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return value

    def find(self, name):
        found = self.window.findChild(QObject, name)
        self.assertIsNotNone(found, name)
        return found

    def test_tab_leaves_chat_composer_and_shows_focus_ring(self):
        editor = self.find("messageEditor")
        editor.forceActiveFocus()
        self.controller.draft = "hello"
        QTest.qWait(50)
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        QTest.qWait(80)
        self.assertEqual(self.controller.draft, "hello")
        self.assertFalse(editor.property("activeFocus"))
        focused = self.window.property("activeFocusItem")
        self.assertTrue(focused.property("visualFocus"))
        ring = focused.findChild(QObject, "focusRing")
        self.assertIsNotNone(ring)
        self.assertTrue(ring.property("visible"))
        QTest.keyClick(self.window, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        QTest.qWait(80)
        self.assertTrue(editor.property("activeFocus"))
        self.assertEqual(self.controller.draft, "hello")

    def test_code_editors_keep_literal_tabs(self):
        component = QQmlComponent(self.engine)
        component.setData(b'import QtQuick\nComposerText { width: 300; height: 80; submitOnEnter: false; editorName: "probeEditor" }',
                          QUrl.fromLocalFile(str(ROOT / "qml" / "Probe.qml")))
        field = component.create(self.engine.contextForObject(self.window))
        self.assertIsNotNone(field, component.errorString())
        field.setParentItem(self.window.property("contentItem"))
        self.assertFalse(field.property("tabNavigates"))
        editor = field.findChild(QObject, "probeEditor")
        editor.forceActiveFocus()
        QTest.keyClick(self.window, Qt.Key.Key_Tab)
        self.assertEqual(field.property("text"), "\t")
        field.deleteLater()

    def test_send_and_stop_are_one_solid_primary_target_with_turn_feedback(self):
        send = self.find("sendButton")
        self.assertTrue(send.property("primary"))
        self.assertGreaterEqual(send.property("width"), 40)
        self.assertFalse(self.find("busyRing").property("visible"))
        self.controller._busy = True
        self.controller._status = "正在调用工具"
        self.controller.changed.emit()
        QTest.qWait(2200)
        self.assertTrue(send.property("primary"))
        self.assertTrue(self.find("busyRing").property("visible"))
        status = self.find("turnStatusText").property("text")
        self.assertIn("正在调用工具", status)
        self.assertRegex(status, r"已用 [12] 秒")
        self.assertIn("Esc 停止", status)
        self.controller._busy = False
        self.controller._status = "就绪"
        self.controller.changed.emit()
        QTest.qWait(30)
        self.assertEqual(self.find("turnStatusText").property("text"), "就绪")
        self.assertEqual(self.window.property("turnSeconds"), 0)
        self.assertEqual(self.js("window.elapsedLabel(75)"), "1 分 15 秒")

    def _queue(self, *ids):
        waiters = {}
        for identifier in ids:
            waiters[identifier] = {"done": threading.Event(), "approved": None}
            self.workbench._waiters[identifier] = waiters[identifier]
        self.workbench._pending = [{"id": i, "title": "运行命令：rm -rf build", "detail": "cwd: C:/proj", "source": "local"} for i in ids]
        self.workbench.changed.emit()
        QTest.qWait(80)
        return waiters

    def test_approval_keys_cannot_approve_by_accident(self):
        waiters = self._queue("a1", "a2")
        card = self.find("approvalCard")
        self.assertTrue(card.property("visible"))
        self.assertTrue(card.property("activeFocus"))
        self.assertTrue(self.find("approveApproval").property("primary"))
        self.assertTrue(self.find("approvalQueue").property("visible"))
        self.assertIn("1", self.find("approvalQueue").property("text"))
        # A stray Enter (e.g. still typing in the composer) does nothing.
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assertEqual(len(self.workbench.approvals), 2)
        QTest.keyClick(self.window, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(waiters["a1"]["approved"])
        self.assertEqual([r["id"] for r in self.workbench.approvals], ["a2"])
        QTest.qWait(50)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertIs(waiters["a2"]["approved"], False)
        self.assertEqual(self.workbench.approvals, [])
        self.assertFalse(card.property("visible"))
        QTest.qWait(50)
        self.assertTrue(self.find("messageEditor").property("activeFocus"))

    def test_escape_during_approval_denies_instead_of_stopping_the_turn(self):
        self.controller._cancel.clear()
        self.controller._busy = True
        self.controller.changed.emit()
        waiters = self._queue("only")
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertIs(waiters["only"]["approved"], False)
        self.assertFalse(self.controller._cancel.is_set())
        # With no approval pending, Esc stops the turn as before.
        QTest.qWait(50)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.controller._cancel.is_set())

    def test_settings_are_grouped_and_exit_defaults_to_the_safe_choice(self):
        self.js("settings.open()")
        QTest.qWait(80)
        for name in ("settingsGroupConnection", "settingsGroupModels", "settingsGroupAppearance"):
            self.assertTrue(self.find(name).property("visible"), name)
        settings = self.find("connectionSettings")
        self.assertLessEqual(settings.property("height"), self.window.property("height"))
        self.js("settings.close()")
        self.js("exitPopup.open()")
        QTest.qWait(80)
        self.assertTrue(self.find("keepWorking").property("activeFocus"))
        self.assertTrue(self.find("confirmExit").property("danger"))
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        QTest.qWait(50)
        self.assertFalse(self.find("exitPopup").property("opened"))
        self.assertFalse(self.window.property("exitConfirmed"))

    def test_reply_actions_are_keyboard_buttons(self):
        # ListView delegates are not QObject children of the window; walk the item tree.
        found = self.js("""(function () {
            function walk(item) {
                if (item.objectName === "copyReply" && item.visible) return item
                for (let i = 0; i < item.children.length; ++i) {
                    const hit = walk(item.children[i]); if (hit) return hit
                }
                return null
            }
            const copy = walk(messages.contentItem)
            return copy ? [copy.focusPolicy, copy.height, copy.visible] : null
        })()""")
        self.assertIsNotNone(found)
        policy, height, visible = found.toVariant()
        self.assertEqual(policy, int(Qt.FocusPolicy.StrongFocus.value))
        self.assertGreaterEqual(height, 30)
        self.assertTrue(visible)

    def test_palette_highlights_the_enter_target_and_click_runs_it(self):
        # Regression: inside the row delegate `palette` resolved to Control.palette,
        # so the highlight never showed and clicks never reached runCurrent().
        self.js('window.openPalette("")')
        QTest.qWait(150)
        rows = self.js("""(function () {
            const out = []
            function walk(item) {
                if (item.index !== undefined && item.background && item.modelData) out.push(item)
                for (let i = 0; i < item.children.length; ++i) walk(item.children[i])
            }
            walk(commandPalette.contentItem)
            return out.map(r => [r.index, "" + r.background.color, r.modelData.label])
        })()""").toVariant()
        by_index = {row[0]: row for row in rows}
        self.assertNotEqual(by_index[0][1], "#00000000")
        self.assertEqual(by_index[1][1], "#00000000")
        label = by_index[0][2]
        self.assertEqual(label, "新建对话")
        before = self.controller.sessionId
        self.js("""(function () {
            function walk(item) {
                if (item.index === 0 && item.background && item.modelData) { item.clicked(); return true }
                for (let i = 0; i < item.children.length; ++i) if (walk(item.children[i])) return true
                return false
            }
            walk(commandPalette.contentItem)
        })()""")
        QTest.qWait(150)
        self.assertFalse(self.find("commandPalette").property("opened"))
        self.assertNotEqual(self.controller.sessionId, before)


if __name__ == "__main__":
    unittest.main()
