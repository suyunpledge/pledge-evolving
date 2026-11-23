"""Desktop pets: pack validation and fallback, mood/animation mapping, persistence and the pet window."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'Basic')
from PySide6.QtCore import QObject, QPoint, Property, Qt, QUrl, Signal
from PySide6.QtGui import QFont
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest

from ._test_app import APP
from . import pets as pets_module
from .controller import ChatController
from .pets import (BUILTIN_ROOT, HAPPY_MS, STATES, WORRIED_MS, MoodTracker, PackError, PetController,
                   animation_state, art_for, load_pack, scan_packs)
from .test_wallpaper import save_png
from .test_workbench import until
from .workbench import WorkbenchController

ROOT = Path(__file__).parent


def write_pack(folder, manifest, images=('idle.png',)):
    folder.mkdir(parents=True, exist_ok=True)
    for name in images:
        save_png(folder / name)
    (folder / 'pet.json').write_text(manifest if isinstance(manifest, str) else json.dumps(manifest),
                                     encoding='utf-8')
    return folder


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeAgent(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self._busy, self._error, self._status, self._approvals = False, '', '就绪', []

    busy = Property(bool, lambda s: s._busy, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)
    status = Property(str, lambda s: s._status, notify=changed)
    approvals = Property('QVariantList', lambda s: s._approvals, notify=changed)

    def set(self, **values):
        for key, value in values.items():
            setattr(self, '_' + key, value)
        self.changed.emit()


class PackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.user = self.home / 'gui' / 'qt' / 'pets'

    def tearDown(self):
        self.temp.cleanup()

    def test_builtin_packs_are_original_svgs_with_every_state(self):
        packs, errors = scan_packs(self.home)
        self.assertEqual(errors, [])
        self.assertEqual(set(packs), {'crab', 'robot', 'whale'})
        for pack in packs.values():
            self.assertTrue(pack['builtin'])
            self.assertEqual(set(pack['states']), set(STATES))
            self.assertEqual(pack['states']['walk']['kind'], 'sheet')
            for art in pack['states'].values():
                self.assertTrue(QUrl(art['source']).toLocalFile().endswith('.svg'))

    def test_manifest_validation_rejects_bad_packs(self):
        good = {'name': '猫', 'states': {'idle': 'idle.png'}}
        cases = {
            'not-json': ('{oops', '不是有效的 JSON'),
            'no-idle': ({'states': {'walk': 'idle.png'}}, '至少包含 idle'),
            'bad-size': ({**good, 'size': 1000}, 'size'),
            'bad-size-type': ({**good, 'size': '96'}, 'size'),
            'escape': ({'states': {'idle': '../outside.png'}}, '宠物文件夹内'),
            'absolute': ({'states': {'idle': str(self.home / 'x.png')}}, '宠物文件夹内'),
            'suffix': ({'states': {'idle': 'idle.exe'}}, '格式不受支持'),
            'missing': ({'states': {'idle': 'nope.png'}}, '找不到'),
            'unknown-state': ({'states': {'idle': 'idle.png', 'dance': 'idle.png'}}, '未知的状态'),
            'frames': ({'states': {'idle': {'sheet': 'idle.png', 'frames': 0}}}, 'frames'),
            'fps': ({'states': {'idle': {'sheet': 'idle.png', 'frames': 2, 'fps': 120}}}, 'fps'),
            'columns': ({'states': {'idle': {'sheet': 'idle.png', 'frames': 2, 'columns': 3}}}, 'columns'),
            'gif-sheet': ({'states': {'idle': {'sheet': 'a.gif', 'frames': 2}}}, 'GIF'),
            'facing': ({**good, 'facing': 'up'}, 'facing'),
            'format': ({**good, 'format': 2}, '格式版本'),
            'empty-name': ({**good, 'name': '  '}, 'name'),
        }
        for folder, (manifest, message) in cases.items():
            with self.subTest(folder):
                pack = write_pack(self.home / folder, manifest)
                (pack / 'outside.png').unlink(missing_ok=True)
                if folder == 'gif-sheet':
                    (pack / 'a.gif').write_bytes(b'GIF89a')
                with self.assertRaises(PackError) as caught:
                    load_pack(pack)
                self.assertIn(message, str(caught.exception))
        with self.assertRaisesRegex(PackError, '文件夹名'):
            load_pack(write_pack(self.home / 'Bad Name', good))
        with self.assertRaisesRegex(PackError, 'pet.json'):
            load_pack(self.home / 'no-manifest')
        corrupt = write_pack(self.home / 'corrupt', good)
        (corrupt / 'idle.png').write_bytes(b'not really a png')
        with self.assertRaisesRegex(PackError, '无法解码'):
            load_pack(corrupt)

    def test_valid_user_pack_with_sheet_and_gif(self):
        folder = write_pack(self.user / 'my-cat', {
            'name': '我的猫', 'size': [80, 64], 'smooth': False, 'facing': 'left',
            'states': {'idle': 'idle.png', 'walk': {'sheet': 'walk.png', 'frames': 4, 'fps': 8, 'columns': 2},
                       'sleep': 'sleep.gif'}}, images=('idle.png', 'walk.png'))
        shutil.copy(ROOT / 'pets' / 'crab' / 'idle.svg', folder / 'unused.svg')
        from .test_wallpaper import lzw_frames
        (folder / 'sleep.gif').write_bytes(lzw_frames(4, 4, [0, 1]))
        pack = load_pack(folder)
        self.assertEqual((pack['name'], pack['width'], pack['height'], pack['smooth'], pack['facing']),
                         ('我的猫', 80, 64, False, 'left'))
        self.assertEqual(pack['states']['walk'], {'source': QUrl.fromLocalFile(str((folder / 'walk.png').resolve())).toString(),
                                                  'kind': 'sheet', 'frames': 4, 'fps': 8, 'columns': 2})
        self.assertEqual(pack['states']['sleep']['kind'], 'animated')
        self.assertEqual(art_for(pack, 'think'), pack['states']['idle'])
        self.assertEqual(art_for(pack, 'walk')['kind'], 'sheet')
        packs, errors = scan_packs(self.home)
        self.assertEqual(errors, [])
        self.assertFalse(packs['my-cat']['builtin'])

    def test_user_override_replaces_builtin_art_and_broken_override_falls_back(self):
        write_pack(self.user / 'crab', {'name': '我的蟹', 'states': {'idle': 'idle.png'}})
        packs, errors = scan_packs(self.home)
        self.assertEqual(errors, [])
        self.assertTrue(packs['crab']['custom'])
        self.assertIn('/gui/qt/pets/crab/', packs['crab']['states']['idle']['source'])
        (self.user / 'crab' / 'pet.json').write_text('{"states": {}}', encoding='utf-8')
        write_pack(self.user / 'broken', '[]')
        packs, errors = scan_packs(self.home)
        self.assertNotIn('custom', packs['crab'])
        self.assertTrue(packs['crab']['states']['idle']['source'].endswith('/qt_frontend/pets/crab/idle.svg'))
        self.assertNotIn('broken', packs)
        self.assertEqual(len(errors), 2)
        self.assertTrue(any('crab' in e and '已改用内置形象' in e for e in errors))
        controller = PetController(self.home)
        self.assertIn('broken', controller.error)
        self.assertEqual(controller.currentId, 'crab')

    def test_missing_builtins_still_fall_back_to_a_user_pack(self):
        write_pack(self.user / 'solo', {'states': {'idle': 'idle.png'}})
        empty = self.home / 'no-builtins'
        empty.mkdir()
        original = pets_module.BUILTIN_ROOT
        pets_module.BUILTIN_ROOT = empty
        try:
            controller = PetController(self.home)
        finally:
            pets_module.BUILTIN_ROOT = original
        self.assertEqual(controller.currentId, 'solo')


class MoodTests(unittest.TestCase):
    def test_animation_state_mapping(self):
        table = {
            ('', 'idle'): 'idle', ('', 'walk'): 'walk', ('', 'sleep'): 'sleep', ('', 'bogus'): 'idle',
            ('think', 'idle'): 'think', ('think', 'sleep'): 'think', ('think', 'walk'): 'think',
            ('happy', 'walk'): 'happy', ('worried', 'sleep'): 'worried', ('angry', 'walk'): 'walk',
        }
        for (mood, ambient), expected in table.items():
            with self.subTest(mood=mood, ambient=ambient):
                self.assertEqual(animation_state(mood, ambient), expected)
        controller = PetController(tempfile.mkdtemp())
        self.assertEqual(controller.animationState('think', 'sleep'), 'think')

    def test_tracker_thinks_then_celebrates_or_worries(self):
        tracker, now = MoodTracker(), 10.0
        self.assertEqual(tracker.update(busy=False, error='', approvals=0, stopped=False, now=now), '')
        self.assertEqual(tracker.update(busy=True, error='', approvals=0, stopped=False, now=now), 'think')
        self.assertEqual(tracker.update(busy=True, error='', approvals=1, stopped=False, now=now), 'worried')
        self.assertEqual(tracker.update(busy=False, error='', approvals=0, stopped=False, now=now), 'happy')
        self.assertEqual(tracker.remaining_ms(now), HAPPY_MS)
        self.assertEqual(tracker.mood(approvals=0, now=now + HAPPY_MS / 1000 + 0.01), '')
        tracker.update(busy=True, error='', approvals=0, stopped=False, now=now)
        self.assertEqual(tracker.update(busy=False, error='网关断开', approvals=0, stopped=False, now=now), 'worried')
        self.assertEqual(tracker.mood(approvals=0, now=now + WORRIED_MS / 1000 - 0.5), 'worried')
        self.assertEqual(tracker.mood(approvals=0, now=now + WORRIED_MS / 1000 + 0.5), '')
        # The same lingering error does not flash again; a stopped turn is not celebrated.
        self.assertEqual(tracker.update(busy=False, error='网关断开', approvals=0, stopped=False, now=now + 20), '')
        tracker.update(busy=True, error='', approvals=0, stopped=False, now=now + 30)
        self.assertEqual(tracker.update(busy=False, error='', approvals=0, stopped=True, now=now + 31), '')

    def test_controller_follows_agent_signals_and_expires_flash(self):
        clock = Clock()
        pets = PetController(tempfile.mkdtemp(), clock=clock)
        agent, bench = FakeAgent(), FakeAgent()
        pets.bind(agent, bench)
        moods = []
        pets.moodChanged.connect(lambda: moods.append(pets.mood))
        agent.set(busy=True, status='正在生成…')
        bench.set(approvals=[{'id': 'a'}])
        bench.set(approvals=[])
        agent.set(busy=False, status='就绪')
        self.assertEqual(moods, ['think', 'worried', 'think', 'happy'])
        clock.now += HAPPY_MS / 1000 + 1
        until(lambda: pets.mood == '', timeout=HAPPY_MS / 1000 + 3)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return json.loads((self.home / 'keystone-desktop.json').read_text(encoding='utf-8'))

    def test_choices_persist_in_desktop_config_preserving_other_keys(self):
        (self.home / 'keystone-desktop.json').write_text(json.dumps({'gateway_port': 9001, 'wallpaper': {'file': ''}}),
                                                         encoding='utf-8')
        pets = PetController(self.home)
        self.assertFalse(pets.enabled)
        self.assertEqual(pets.currentId, 'crab')
        self.assertFalse(pets.hasPosition)
        pets.setEnabled(True)
        pets.select('whale')
        pets.select('nope')
        pets.rename('whale', '  大胖鱼  ')
        pets.rename('nope', 'x')
        pets.savePosition(120, -40)
        data = self.config()
        self.assertEqual(data['gateway_port'], 9001)
        self.assertEqual(data['wallpaper'], {'file': ''})
        self.assertEqual(data['pets'], {'enabled': True, 'current': 'whale', 'names': {'whale': '大胖鱼'},
                                        'position': [120, -40]})
        again = PetController(self.home)
        self.assertTrue(again.enabled)
        self.assertEqual((again.currentId, again.current['name'], again.posX, again.posY), ('whale', '大胖鱼', 120, -40))
        self.assertEqual(next(r for r in again.packs if r['id'] == 'whale')['defaultName'], '圆滚滚鲸鱼')
        again.rename('whale', '')
        self.assertEqual(again.current['name'], '圆滚滚鲸鱼')
        self.assertEqual(self.config()['pets']['names'], {})

    def test_legacy_forge_desktop_file_is_used(self):
        (self.home / 'forge-desktop.json').write_text(json.dumps({'pets': {'enabled': True, 'current': 'robot'}}),
                                                      encoding='utf-8')
        pets = PetController(self.home)
        self.assertEqual((pets.enabled, pets.currentId), (True, 'robot'))
        pets.select('crab')
        self.assertEqual(json.loads((self.home / 'forge-desktop.json').read_text(encoding='utf-8'))['pets']['current'], 'crab')

    def test_invalid_saved_values_are_ignored(self):
        (self.home / 'keystone-desktop.json').write_text(json.dumps({'pets': {
            'enabled': 'yes', 'current': '../x', 'names': {'crab': 5, 'BAD': 'x', 'robot': '小R'},
            'position': [1, 'a']}}), encoding='utf-8')
        pets = PetController(self.home)
        self.assertFalse(pets.enabled)
        self.assertEqual(pets.currentId, 'crab')
        self.assertFalse(pets.hasPosition)
        self.assertEqual(next(r for r in pets.packs if r['id'] == 'robot')['name'], '小R')
        (self.home / 'keystone-desktop.json').write_text(json.dumps({'pets': {'current': 'gone'}}), encoding='utf-8')
        pets = PetController(self.home)
        self.assertEqual(pets.currentId, 'crab')
        self.assertIn('gone', pets.error)


class PetWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.controller = ChatController(self.temp.name, demo=True)
        self.workbench = WorkbenchController(self.temp.name, demo=True)
        self.pets = self.workbench.pets
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
        self.loader = self.window.findChild(QObject, 'petLoader')

    def tearDown(self):
        import shiboken6
        self.pets.setEnabled(False)
        APP.processEvents()
        self.workbench.shutdown()
        self.controller.shutdown()
        if shiboken6.isValid(self.window):
            self.window.setProperty('exitConfirmed', True)
            self.window.close()
        self.engine.deleteLater()
        APP.processEvents()
        self.temp.cleanup()
        self.assertFalse(self.warnings, self.warnings)

    def show_pet(self):
        self.pets.setEnabled(True)
        until(lambda: self.loader.property('item') is not None)
        pet = self.loader.property('item')
        pet.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        until(pet.isExposed)
        return pet

    def test_pet_window_is_a_transparent_independent_top_level(self):
        self.assertIsNone(self.loader.property('item'))
        pet = self.show_pet()
        flags = pet.flags()
        for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.Tool):
            self.assertTrue(flags & flag)
        self.assertIsNone(pet.transientParent())
        self.assertEqual(pet.color().alpha(), 0)
        image = pet.grabWindow()
        self.assertEqual(image.pixelColor(1, 1).alpha(), 0)
        self.pets.select('robot')
        self.assertIn('/pets/robot/', pet.property('art')['source'])
        self.pets.setEnabled(False)
        until(lambda: self.loader.property('item') is None)

    def test_agent_mood_drives_animation_state_and_bubble(self):
        pet = self.show_pet()
        self.pets.bind(self.controller, self.workbench)
        self.assertEqual(pet.property('animState'), 'idle')
        self.pets.observe(busy=True, error='', approvals=0)
        self.assertEqual(pet.property('animState'), 'think')
        self.assertIn('/pets/crab/think.svg', pet.property('art')['source'])
        bubble = pet.findChild(QObject, 'petBubble')
        self.assertTrue(bubble.property('visible'))
        pet.setProperty('ambient', 'sleep')
        self.assertEqual(pet.property('animState'), 'think')
        until(lambda: pet.property('ambient') == 'idle')  # a mood wakes a sleeping pet
        self.pets.observe(busy=False, error='', approvals=0)
        self.assertEqual(pet.property('animState'), 'happy')
        self.pets.observe(busy=False, error='', approvals=1)
        self.assertEqual(pet.property('animState'), 'worried')
        self.assertEqual(bubble.property('mark'), '!')

    def test_drag_moves_and_remembers_position_click_does_not(self):
        pet = self.show_pet()
        start = QPoint(pet.x(), pet.y())
        centre = QPoint(pet.width() // 2, pet.height() - 30)
        QTest.mousePress(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        for step in range(1, 6):
            QTest.mouseMove(pet, centre + QPoint(step * 8, step * 4) - QPoint(pet.x() - start.x(), pet.y() - start.y()))
        QTest.mouseRelease(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           centre + QPoint(40, 20) - QPoint(pet.x() - start.x(), pet.y() - start.y()))
        until(lambda: self.pets.hasPosition)
        self.assertEqual((self.pets.posX, self.pets.posY), (start.x() + 40, start.y() + 20))
        self.assertEqual((pet.x(), pet.y()), (start.x() + 40, start.y() + 20))
        saved = json.loads((Path(self.temp.name) / 'keystone-desktop.json').read_text(encoding='utf-8'))['pets']['position']
        self.assertEqual(saved, [start.x() + 40, start.y() + 20])
        QTest.mouseClick(pet, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        self.assertEqual((pet.x(), pet.y()), (start.x() + 40, start.y() + 20))
        # A new window starts where the pet was left.
        self.pets.setEnabled(False)
        until(lambda: self.loader.property('item') is None)
        pet = self.show_pet()
        self.assertEqual((pet.x(), pet.y()), (start.x() + 40, start.y() + 20))

    def test_reduced_motion_and_hidden_window_stop_all_timers(self):
        pet = self.show_pet()
        self.pets.select('crab')
        pet.setProperty('ambient', 'walk')
        sheet_timer = lambda: pet.property('moving') and pet.property('art')['kind'] == 'sheet'
        self.assertTrue(sheet_timer())
        self.controller.setReduceMotion(True)
        until(lambda: pet.property('ambient') == 'idle')
        self.assertFalse(pet.property('moving'))
        pet.setProperty('ambient', 'walk')
        self.assertFalse(sheet_timer())
        self.controller.setReduceMotion(False)
        pet.setProperty('ambient', 'idle')
        self.assertTrue(pet.property('moving'))
        timers = [t for t in pet.findChildren(QObject) if t.metaObject().className() == 'QQmlTimer']
        self.assertGreaterEqual(len(timers), 6)
        self.assertTrue(any(t.property('running') for t in timers))
        pet.hide()
        until(lambda: not pet.property('live'))
        self.assertFalse(pet.property('moving'))
        self.assertEqual([t for t in timers if t.property('running')], [])


if __name__ == '__main__':
    unittest.main()
