"""Wallpaper library: import validation, persistence, fallback and animated pause."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'Basic')
from PySide6.QtCore import QMetaObject, QObject, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QImage
from PySide6.QtQml import QQmlApplicationEngine

from ._test_app import APP
from . import wallpaper as wallpaper_module
from .controller import ChatController
from .test_workbench import until
from .wallpaper import Wallpaper
from .workbench import WorkbenchController

ROOT = Path(__file__).parent


def lzw_frames(width, height, frames):
    """A tiny animated GIF built by hand, so the pause tests need no ffmpeg.

    Pixels are written as literal 3-bit codes with a clear code every two pixels,
    which keeps the LZW code width fixed and needs no real compressor.
    """
    out = bytearray(b'GIF89a' + width.to_bytes(2, 'little') + height.to_bytes(2, 'little') + bytes([0xF1, 0, 0]))
    out += bytes([0xF0, 0x55, 0x33, 0x20, 0x60, 0xF0, 0x33, 0xCC, 0x66, 0xFF, 0xFF, 0xFF])  # 4-colour table
    out += b'\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00'  # loop forever
    for colour in frames:
        out += b'\x21\xF9\x04\x00\x0A\x00\x00\x00'  # 100 ms per frame
        out += b'\x2C\x00\x00\x00\x00' + width.to_bytes(2, 'little') + height.to_bytes(2, 'little') + b'\x00'
        codes = []
        for i in range(width * height):
            if i % 2 == 0:
                codes.append(4)  # clear
            codes.append(colour)
        codes.append(5)  # end of information
        bits, value, data = 0, 0, bytearray()
        for code in codes:
            value |= code << bits
            bits += 3
            while bits >= 8:
                data.append(value & 0xFF)
                value >>= 8
                bits -= 8
        if bits:
            data.append(value & 0xFF)
        out += b'\x02'
        for start in range(0, len(data), 255):
            block = data[start:start + 255]
            out += bytes([len(block)]) + block
        out += b'\x00'
    return bytes(out + b'\x3B')


def save_png(path, colour='#f05533', size=(64, 48)):
    picture = QImage(*size, QImage.Format.Format_RGB32)
    picture.fill(QColor(colour))
    assert picture.save(str(path))
    return path


def import_file(wallpaper, path, dynamic=False, timeout=15):
    wallpaper.importFile(QUrl.fromLocalFile(str(path)), dynamic)
    until(lambda: not wallpaper.busy, timeout=timeout)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name) / '用户 home'
        self.home.mkdir()
        self.source = Path(self.temp.name) / '来源'
        self.source.mkdir()
        self.wallpaper = Wallpaper(self.home)

    def tearDown(self):
        self.wallpaper.shutdown()
        APP.processEvents()
        self.temp.cleanup()

    def config(self):
        return json.loads((self.home / 'keystone-desktop.json').read_text(encoding='utf-8'))

    def test_import_validation_rejects_wrong_type_size_and_content(self):
        png = save_png(self.source / '山.png')
        import_file(self.wallpaper, png, dynamic=True)  # a still picked through the animated importer
        self.assertTrue(self.wallpaper.error)
        text = self.source / 'notes.txt'
        text.write_text('hi', encoding='utf-8')
        import_file(self.wallpaper, text)
        self.assertTrue(self.wallpaper.error)
        fake = self.source / 'fake.jpg'
        fake.write_bytes(b'not really a jpeg')
        import_file(self.wallpaper, fake)
        self.assertIn('无法读取', self.wallpaper.error)
        mislabelled = self.source / 'mislabelled.gif'
        shutil.copyfile(png, mislabelled)
        import_file(self.wallpaper, mislabelled, dynamic=True)
        self.assertTrue(self.wallpaper.error)
        with mock.patch.object(wallpaper_module, 'MAX_IMAGE_BYTES', 16):
            import_file(self.wallpaper, png)
        self.assertIn('80 MB', self.wallpaper.error)
        import_file(self.wallpaper, self.source / 'missing.png')
        self.assertTrue(self.wallpaper.error)
        self.assertEqual(self.wallpaper.items, [])
        self.assertFalse(self.wallpaper.kind)
        until(lambda: not self.wallpaper._discard)
        folder = self.home / 'gui' / 'qt' / 'wallpapers'
        leftovers = [p.name for p in folder.iterdir()] if folder.exists() else []
        self.assertEqual([n for n in leftovers if n not in ('.lock',)], [])

    def test_import_copies_into_library_with_thumbnail_and_keeps_previous(self):
        first = save_png(self.source / '第一张.png', '#f05533', (640, 360))
        second = save_png(self.source / 'second.jpg', '#3366cc', (200, 300))
        import_file(self.wallpaper, first)
        self.assertEqual(self.wallpaper.kind, 'image', self.wallpaper.error)
        first_id = self.wallpaper.currentId
        import_file(self.wallpaper, second)
        self.assertEqual(self.wallpaper.error, '')
        rows = self.wallpaper.items
        self.assertEqual([r['name'] for r in rows], ['second.jpg', '第一张.png'])
        self.assertEqual(self.wallpaper.currentId, rows[0]['id'])
        folder = self.home / 'gui' / 'qt' / 'wallpapers'
        for row in rows:
            copy = Path(QUrl(row['source']).toLocalFile())
            self.assertEqual(copy.parent, folder)
            self.assertTrue(copy.is_file())
            thumb = QImage(QUrl(row['thumb']).toLocalFile())
            self.assertFalse(thumb.isNull())
            self.assertLessEqual(thumb.width(), 320)
            self.assertLessEqual(thumb.height(), 200)
        first.unlink()  # the library owns its copy
        self.wallpaper.select(first_id)
        self.assertEqual(self.wallpaper.name, '第一张.png')

    def test_selection_and_dim_persist_in_desktop_config_preserving_other_keys(self):
        (self.home / 'keystone-desktop.json').write_text(json.dumps({'gateway_port': 9100}), encoding='utf-8')
        import_file(self.wallpaper, save_png(self.source / 'a.png'))
        chosen = self.wallpaper.currentId
        import_file(self.wallpaper, save_png(self.source / 'b.png', '#00aa66'))
        self.wallpaper.select(chosen)
        self.wallpaper.setDim(0.3)
        config = self.config()
        self.assertEqual(config['gateway_port'], 9100)
        self.assertEqual(config['wallpaper'], {'file': chosen, 'dim': 0.3})
        reopened = Wallpaper(self.home)
        self.assertEqual(reopened.currentId, chosen)
        self.assertEqual(reopened.source, self.wallpaper.source)
        self.assertEqual(reopened.dim, 0.3)
        self.assertEqual(len(reopened.items), 2)
        reopened.reset()  # back to the built-in background; the library is kept
        self.assertFalse(reopened.kind)
        again = Wallpaper(self.home)
        self.assertEqual(again.currentId, '')
        self.assertEqual(len(again.items), 2)

    def test_legacy_forge_desktop_file_and_single_slot_record_are_honoured(self):
        legacy = self.home / 'forge-desktop.json'
        legacy.write_text(json.dumps({'gateway_port': 9200}), encoding='utf-8')
        folder = self.home / 'gui' / 'qt' / 'wallpapers'
        folder.mkdir(parents=True)
        old_file = 'a' * 32 + '.png'
        save_png(folder / old_file)
        (folder.parent / 'wallpaper.json').write_text(
            json.dumps({'file': old_file, 'kind': 'image', 'name': '旧壁纸.png', 'dim': 0.4}), encoding='utf-8')
        adopted = Wallpaper(self.home)
        self.assertEqual(adopted.currentId, old_file)
        self.assertEqual(adopted.name, '旧壁纸.png')
        self.assertEqual(adopted.dim, 0.4)
        adopted.setDim(0.2)
        self.assertFalse((self.home / 'keystone-desktop.json').exists())
        saved = json.loads(legacy.read_text(encoding='utf-8'))
        self.assertEqual(saved['gateway_port'], 9200)
        self.assertEqual(saved['wallpaper']['file'], old_file)

    def test_remove_deletes_copy_and_falls_back_when_current(self):
        import_file(self.wallpaper, save_png(self.source / 'keep.png'))
        keep = self.wallpaper.currentId
        import_file(self.wallpaper, save_png(self.source / 'drop.png', '#123456'))
        drop = self.wallpaper.currentId
        folder = self.home / 'gui' / 'qt' / 'wallpapers'
        self.assertTrue((folder / drop).is_file())
        self.wallpaper.remove(drop)
        self.assertEqual(self.wallpaper.currentId, '')
        self.assertFalse(self.wallpaper.kind)
        self.assertEqual([r['id'] for r in self.wallpaper.items], [keep])
        until(lambda: not (folder / drop).exists() and not (folder / (drop.split('.')[0] + '.thumb.jpg')).exists())
        self.assertTrue((folder / keep).is_file())
        self.assertEqual(Wallpaper(self.home).currentId, '')
        self.wallpaper.select(keep)
        self.wallpaper.remove('0' * 32 + '.png')  # unknown ids are ignored
        self.assertEqual(self.wallpaper.currentId, keep)

    def test_missing_or_unparseable_state_falls_back_to_default(self):
        import_file(self.wallpaper, save_png(self.source / 'gone.png'))
        chosen = self.wallpaper.currentId
        (self.home / 'gui' / 'qt' / 'wallpapers' / chosen).unlink()
        reopened = Wallpaper(self.home)
        self.assertFalse(reopened.kind)
        self.assertEqual(reopened.source, QUrl())
        self.assertEqual(reopened.items, [])
        self.assertIn('默认背景', reopened.error)
        (self.home / 'keystone-desktop.json').write_text('{broken', encoding='utf-8')
        (self.home / 'gui' / 'qt' / 'wallpapers' / 'index.json').write_text('[1, 2', encoding='utf-8')
        garbage = Wallpaper(self.home)
        self.assertFalse(garbage.kind)
        self.assertEqual(garbage.items, [])

    def test_report_failure_shows_default_for_this_session_only(self):
        import_file(self.wallpaper, save_png(self.source / 'x.png'))
        chosen = self.wallpaper.currentId
        self.wallpaper.reportFailure('someone-else')
        self.assertEqual(self.wallpaper.kind, 'image')
        self.wallpaper.reportFailure(chosen)
        self.assertFalse(self.wallpaper.kind)
        self.assertEqual(self.wallpaper.currentId, '')
        self.assertTrue(self.wallpaper.error)
        self.assertEqual(Wallpaper(self.home).currentId, chosen)  # the file may be fine next time
        self.wallpaper.select(chosen)  # choosing it again retries
        self.assertEqual(self.wallpaper.kind, 'image')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'requires ffmpeg to generate a video fixture')
    def test_video_import_probes_track_and_captures_poster(self):
        clip = self.source / '片段.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=10', '-t', '1', '-pix_fmt', 'yuv420p', str(clip)],
                       capture_output=True, check=True, timeout=20)
        import_file(self.wallpaper, clip, dynamic=True, timeout=20)
        self.assertEqual(self.wallpaper.kind, 'video', self.wallpaper.error)
        self.assertFalse(QImage(self.wallpaper.thumb.toLocalFile()).isNull())
        audio = self.source / 'tone.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', str(audio)], capture_output=True, check=True, timeout=20)
        import_file(self.wallpaper, audio, dynamic=True, timeout=20)
        self.assertTrue(self.wallpaper.error)
        self.assertEqual(self.wallpaper.kind, 'video')  # the previous choice stays


class ViewTests(unittest.TestCase):
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
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets/keystone-logo-104.png'))}.items():
            context.setContextProperty(name, value)
        self.engine.load(QUrl.fromLocalFile(str(ROOT / 'qml/Main.qml')))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
        self.window = self.engine.rootObjects()[0]
        self.window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        self.window.setPosition(-4000, -3000)
        self.window.show()
        self.wallpaper = self.workbench.wallpaper

    def tearDown(self):
        import shiboken6
        self.workbench.shutdown()
        self.controller.shutdown()
        if shiboken6.isValid(self.window):
            self.window.setProperty('exitConfirmed', True)
            self.window.close()
        self.engine.deleteLater()
        APP.processEvents()
        self.temp.cleanup()
        self.assertFalse(self.warnings, self.warnings)

    def test_animated_wallpaper_pauses_when_minimised_or_reduced_motion(self):
        gif = Path(self.temp.name) / '动.gif'
        gif.write_bytes(lzw_frames(8, 8, [0, 1, 2, 3]))
        import_file(self.wallpaper, gif, dynamic=True)
        self.assertEqual(self.wallpaper.kind, 'gif', self.wallpaper.error)
        element = self.window.findChild(QObject, 'gifWallpaper')
        self.assertIsNotNone(element)
        layer = element.parent()
        until(lambda: element.property('frameCount') == 4)
        until(lambda: layer.property('running'))
        self.assertFalse(element.property('paused'))
        frame = element.property('currentFrame')
        until(lambda: element.property('currentFrame') != frame)

        self.window.showMinimized()
        until(lambda: not layer.property('running'))
        self.assertTrue(element.property('paused'))
        frozen = element.property('currentFrame')
        until(lambda: True, timeout=0.3)
        self.assertEqual(element.property('currentFrame'), frozen)
        self.window.showNormal()
        until(lambda: layer.property('running'))

        self.controller.setReduceMotion(True)
        self.assertFalse(layer.property('running'))
        self.assertTrue(element.property('paused'))
        self.controller.setReduceMotion(False)
        self.assertTrue(layer.property('running'))
        self.window.hide()
        until(lambda: not layer.property('running'))

    def test_corrupt_wallpaper_in_view_falls_back_to_default(self):
        good = save_png(Path(self.temp.name) / 'good.png')
        import_file(self.wallpaper, good)
        first = self.wallpaper.currentId
        import_file(self.wallpaper, save_png(Path(self.temp.name) / 'other.png', '#0044aa'))
        still = self.window.findChild(QObject, 'stillWallpaper')
        until(lambda: still.property('loaded'))
        (self.workbench.home / 'gui' / 'qt' / 'wallpapers' / first).write_bytes(b'corrupted')
        self.wallpaper.select(first)
        until(lambda: not self.wallpaper.kind)
        self.assertIn('默认背景', self.wallpaper.error)
        self.assertEqual(still.property('source'), QUrl())
        # Qt logs the decode failure itself; that warning is the point of this test.
        self.warnings = [w for w in self.warnings if 'Error decoding' not in w]

    def test_picker_lists_selects_and_deletes_with_confirmation(self):
        import_file(self.wallpaper, save_png(Path(self.temp.name) / '图.png'))
        imported = self.wallpaper.currentId
        panel = self.window.findChild(QObject, 'wallpaperSettings')
        QMetaObject.invokeMethod(panel, 'open')
        until(lambda: panel.property('opened'))
        grid = self.window.findChild(QObject, 'wallpaperGrid')

        def tiles():
            items = [c for c in grid.property('contentItem').childItems() if c.property('label')]
            return sorted(items, key=lambda c: (c.property('y'), c.property('x')))

        until(lambda: len(tiles()) == 2)
        default, picture = tiles()
        self.assertEqual(tiles(), [default, picture])
        self.assertEqual([default.property('label'), picture.property('label')], ['默认背景', '图.png'])
        self.assertTrue(picture.property('current'))
        self.assertTrue(picture.property('preview').toString().endswith('.thumb.jpg'))
        QMetaObject.invokeMethod(default, 'click')
        until(lambda: default.property('current'))
        self.assertEqual(self.wallpaper.currentId, '')
        QMetaObject.invokeMethod(picture, 'click')
        until(lambda: self.wallpaper.currentId == imported)
        self.assertEqual(tiles(), [default, picture])  # selecting does not rebuild the tiles
        remove = picture.findChild(QObject, 'removeWallpaper')
        QMetaObject.invokeMethod(remove, 'click')  # first click only asks for confirmation
        self.assertTrue(picture.property('confirming'))
        self.assertEqual(len(self.wallpaper.items), 1)
        QMetaObject.invokeMethod(remove, 'click')
        until(lambda: len(tiles()) == 1)
        self.assertEqual(self.wallpaper.items, [])
        self.assertFalse(self.wallpaper.kind)
        until(lambda: not (self.workbench.home / 'gui' / 'qt' / 'wallpapers' / imported).exists())


class ReduceMotionEnvTests(unittest.TestCase):
    def test_env_setting_overrides_saved_preference(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
            with mock.patch.dict(os.environ, {'KEYSTONE_REDUCE_MOTION': '1'}):
                controller = ChatController(home, demo=True)
                self.assertTrue(controller.reduceMotion)
                controller.shutdown()
            with mock.patch.dict(os.environ, {'KEYSTONE_REDUCE_MOTION': '0'}):
                controller = ChatController(home, demo=True)
                self.assertFalse(controller.reduceMotion)
                controller.shutdown()


if __name__ == '__main__':
    unittest.main()
