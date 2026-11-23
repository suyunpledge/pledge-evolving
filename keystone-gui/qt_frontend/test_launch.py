"""Launch animation slot: when it plays, replacement sources, and that it never holds up readiness."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'Basic')
from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest

from ._test_app import APP
from .controller import ChatController
from .launch import ANIMATION_MS, FADE_MS, LaunchSlot, launch_folder, resolve_source, should_play
from .test_wallpaper import lzw_frames, save_png
from .test_workbench import until
from .workbench import WorkbenchController

ROOT = Path(__file__).parent
BRAND = QUrl.fromLocalFile(str(ROOT.parent / 'assets/keystone-logo-104.png'))
CLEAN_ENV = {'KEYSTONE_REDUCE_MOTION': '', 'KEYSTONE_NO_SPLASH': ''}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        env = mock.patch.dict(os.environ, CLEAN_ENV)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, launch):
        (self.home / 'keystone-desktop.json').write_text(json.dumps({'gateway_port': 9001, 'launch': launch}),
                                                         encoding='utf-8')

    def test_plays_by_default_and_is_skipped_under_reduced_motion_smoke_or_opt_out(self):
        self.assertTrue(should_play(self.home))
        self.assertFalse(should_play(self.home, smoke=True))
        self.assertFalse(should_play(self.home, reduce_motion=True))
        prefs = self.home / 'gui' / 'qt' / 'preferences.json'
        prefs.parent.mkdir(parents=True)
        prefs.write_text(json.dumps({'reduceMotion': True}), encoding='utf-8')
        self.assertFalse(should_play(self.home))
        with mock.patch.dict(os.environ, {'KEYSTONE_REDUCE_MOTION': '0'}):
            self.assertTrue(should_play(self.home))
        prefs.write_text(json.dumps({'reduceMotion': False}), encoding='utf-8')
        with mock.patch.dict(os.environ, {'KEYSTONE_REDUCE_MOTION': '1'}):
            self.assertFalse(should_play(self.home))
        with mock.patch.dict(os.environ, {'KEYSTONE_NO_SPLASH': '1'}):
            self.assertFalse(should_play(self.home))
        self.write_config({'enabled': False})
        self.assertFalse(should_play(self.home))
        self.write_config({'enabled': True})
        self.assertTrue(should_play(self.home))

    def test_replacement_source_from_documented_folder_or_config_key(self):
        self.assertEqual(resolve_source(self.home), ('builtin', None))
        folder = launch_folder(self.home)
        folder.mkdir(parents=True)
        (folder / 'launch.gif').write_bytes(lzw_frames(4, 4, [0, 1]))
        self.assertEqual(resolve_source(self.home), ('animated', folder / 'launch.gif'))
        (folder / 'launch.qml').write_text('import QtQuick\nItem {}\n', encoding='utf-8')
        self.assertEqual(resolve_source(self.home)[0], 'qml')  # .qml is checked first
        save_png(folder / 'brand.png')
        self.write_config({'source': 'brand.png'})
        self.assertEqual(resolve_source(self.home), ('image', folder / 'brand.png'))
        outside = self.home / 'intro.mp4'
        outside.write_bytes(b'\0' * 64)
        self.write_config({'source': str(outside)})
        self.assertEqual(resolve_source(self.home), ('video', outside))

    def test_missing_unsupported_or_empty_sources_fall_back_to_builtin(self):
        folder = launch_folder(self.home)
        folder.mkdir(parents=True)
        (folder / 'launch.exe').write_bytes(b'MZ')
        (folder / 'launch.png').write_bytes(b'')
        self.write_config({'source': 'gone.mp4'})
        self.assertEqual(resolve_source(self.home), ('builtin', None))
        self.write_config({'source': 42})
        self.assertEqual(resolve_source(self.home), ('builtin', None))


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)

    def tearDown(self):
        APP.processEvents()
        self.temp.cleanup()

    def test_show_returns_at_once_and_ready_ends_it_before_the_animation_would(self):
        slot = LaunchSlot(self.home, BRAND)
        finished = []
        slot.finished.connect(lambda: finished.append(time.monotonic()))
        started = time.monotonic()
        slot.show()
        self.assertLess(time.monotonic() - started, 1.0)
        view = slot.view
        self.assertTrue(view.isVisible())
        self.assertEqual(view.rootObject().findChild(QObject, 'launchBuiltin').property('visible'), True)
        self.assertTrue(view.flags() & Qt.WindowType.FramelessWindowHint)
        ready_at = time.monotonic()
        slot.markReady()
        until(lambda: slot.ended, timeout=3)
        self.assertEqual(len(finished), 1)
        self.assertLess(finished[0] - ready_at, (FADE_MS + 300) / 1000)
        self.assertLess(finished[0] - started, ANIMATION_MS / 1000)
        slot.markReady()
        self.assertEqual(len(finished), 1)

    def test_click_or_escape_skip_immediately(self):
        slot = LaunchSlot(self.home, BRAND).show()
        QTest.mouseClick(slot.view, Qt.MouseButton.LeftButton)
        self.assertTrue(slot.ended)
        slot = LaunchSlot(self.home, BRAND).show()
        until(slot.view.isExposed)
        QTest.keyClick(slot.view, Qt.Key.Key_Escape)
        until(lambda: slot.ended, timeout=2)

    def test_safety_cap_ends_it_if_ready_never_comes(self):
        slot = LaunchSlot(self.home, BRAND, max_wait_ms=150).show()
        until(lambda: slot.ended, timeout=2)

    def test_replacement_qml_and_image_load(self):
        folder = launch_folder(self.home)
        folder.mkdir(parents=True)
        (folder / 'launch.qml').write_text(
            'import QtQuick\nRectangle { objectName: "custom"; color: launch.ready ? "red" : "teal" }\n', encoding='utf-8')
        slot = LaunchSlot(self.home, BRAND).show()
        self.assertEqual(slot.kind, 'qml')
        until(lambda: slot.view.rootObject().findChild(QObject, 'custom') is not None, timeout=3)
        self.assertFalse(slot.view.rootObject().findChild(QObject, 'launchBuiltin').property('visible'))
        slot.skip()
        (folder / 'launch.qml').unlink()
        save_png(folder / 'launch.png')
        slot = LaunchSlot(self.home, BRAND).show()
        self.assertEqual(slot.kind, 'image')
        self.assertEqual(slot.view.errors(), [])
        slot.skip()


class ReadinessTests(unittest.TestCase):
    """Wired as app.py does: the main window's first frame ends the launch animation."""

    def test_main_window_is_never_held_back_by_the_launch_animation(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        launch = LaunchSlot(temp.name, BRAND).show()
        controller = ChatController(temp.name, demo=True)
        workbench = WorkbenchController(temp.name, demo=True)
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda rows: warnings.extend(map(str, rows)))
        for name, value in {'controller': controller, 'workbench': workbench, 'smokeMode': True,
                            'gpuEnabled': True, 'uiFont': QFont('Segoe UI', 10), 'brandUrl': BRAND}.items():
            engine.rootContext().setContextProperty(name, value)
        engine.load(QUrl.fromLocalFile(str(ROOT / 'qml/Main.qml')))
        window = engine.rootObjects()[0]
        frames = []
        window.frameSwapped.connect(lambda: frames.append(time.monotonic()), Qt.ConnectionType.SingleShotConnection)
        window.frameSwapped.connect(launch.markReady, Qt.ConnectionType.SingleShotConnection)
        ended = []
        launch.finished.connect(lambda: ended.append(time.monotonic()))
        window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        window.setPosition(40, 40)  # on screen: an off-screen window never draws a frame
        window.show()
        try:
            until(lambda: frames and ended, timeout=10)
            self.assertTrue(launch.ready)
            # The main window drew its first frame without waiting for the animation...
            self.assertLessEqual(frames[0], ended[0])
            # ...and the animation got out of the way right after.
            self.assertLess(ended[0] - frames[0], (FADE_MS + 400) / 1000)
            self.assertEqual(warnings, [])
        finally:
            workbench.shutdown()
            controller.shutdown()
            window.setProperty('exitConfirmed', True)
            window.close()
            engine.deleteLater()
            APP.processEvents()
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
