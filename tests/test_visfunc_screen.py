"""VisFunc targets must belong to the selected client and remain ordinary approved input."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'keystone-gui')]
from computer_runtime import ComputerLane
from keystone import computer_visfunc
from keystone.computer_use import ComputerSession
from keystone.computer_win32 import WindowsControlDriver
from keystone.policy import Policy
from keystone.secrets import SecretScope


class RecognitionTests(unittest.TestCase):
    def test_dense_form_keeps_focus_and_selection_without_field_values(self):
        from types import SimpleNamespace
        driver = SimpleNamespace(window=12, process_id=34)
        frame = {'rect': [0, 0, 600, 400], 'client_rect': [0, 0, 600, 400]}
        label = {'control_type': 'text', 'center_px': [25, 30], 'enabled': True, 'name': 'label'}
        field = dict(label, name='显示名称', control_type='edit', focused=True,
                     automation_id='parent.' * 30 + 'editLabel', value='QA-private-value')
        row = dict(label, name='Model A', control_type='list_item', selected=True)
        tree = {'window': {'process_id': 34}, 'elements': [label] * 65 + [field, row], 'truncated': False}
        with patch.object(computer_visfunc.ui_elements, 'list_elements', return_value=tree):
            result = computer_visfunc.inspect_window(driver, frame, {})
            focus, selected = result['controls'][:2]
            self.assertTrue(result['truncated'])
            self.assertEqual(len(result['controls']), 64)
            self.assertTrue(focus['focused'] and focus['id'].endswith('editLabel'))
            self.assertTrue(selected['selected'])
            self.assertNotIn(field['value'], str(result))
            found = computer_visfunc.inspect_window(driver, frame, {'target': focus['id'], 'role': 'edit'})
            self.assertEqual(found['control'], focus)

    def test_ambiguous_disabled_and_other_window_do_not_produce_a_point(self):
        from types import SimpleNamespace
        driver = SimpleNamespace(window=12, process_id=34)
        frame = {'rect': [-400, 20, 0, 300], 'client_rect': [-394, 51, -6, 294]}
        item = {'name': '提交', 'control_type': 'button', 'center_px': [-200, 110], 'enabled': True}
        tree = {'window': {'process_id': 34}, 'elements': [item, dict(item)], 'truncated': False}
        with patch.object(computer_visfunc.ui_elements, 'list_elements', return_value=tree) as read:
            inspect = lambda: computer_visfunc.inspect_window(driver, frame, {'target': '提交', 'role': 'button'})
            self.assertEqual(inspect()['status'], 'ambiguous')
            tree['elements'].pop()
            self.assertEqual(inspect()['point']['x'], 200)
            item['enabled'] = False
            self.assertEqual(inspect()['status'], 'disabled')
            tree['window']['process_id'] = 56
            with self.assertRaises(ValueError):
                inspect()
            self.assertEqual(read.call_args.kwargs['hwnd'], 12)
            self.assertEqual(read.call_args.kwargs['process_id'], 34)


def fixture(folder, background=False):
    from PySide6.QtCore import QEvent, QObject, Qt, QTimer
    from PySide6.QtGui import QFont, QPainter
    from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPushButton, QVBoxLayout, QWidget
    app = QApplication([])
    app.setCursorFlashTime(0)

    class Canvas(QWidget):
        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), Qt.GlobalColor.white)
            painter.setPen(Qt.GlobalColor.black)
            painter.setFont(QFont('Arial', 22))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, 'ALPINE TARGET')
            painter.end()

    window = QWidget()
    window.setWindowTitle('Keystone VisFunc test')
    layout = QVBoxLayout(window)
    edit = QLineEdit()
    edit.setAccessibleName('Profile name')
    save = QPushButton('✓')
    save.setAccessibleName('Apply profile')
    def apply_profile():
        (folder / 'applied').write_text(edit.text(), encoding='utf8')
        if background:
            with (folder / 'invocations').open('a', encoding='ascii') as log:
                log.write('1')
    save.clicked.connect(apply_profile)
    canvas = Canvas()
    canvas.setMinimumHeight(75)
    for control in (edit, QCheckBox('Enabled option'), save, canvas):
        layout.addWidget(control)
    if background:
        secret = QLineEdit()
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        secret.setAccessibleName('Private password fixture')
        layout.addWidget(secret)
    window.setGeometry(130, 170, 430, 235)
    if background:
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        window.setWindowFlag(Qt.WindowType.WindowStaysOnBottomHint)
    window.show()
    if not background:
        window.raise_()
        window.activateWindow()
    def ready():
        if background:
            class InputMonitor(QObject):
                def eventFilter(self, obj, event):
                    if event.spontaneous() and event.type() in (QEvent.Type.WindowActivate, QEvent.Type.KeyPress,
                            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                        with (folder / 'desktop-input').open('a', encoding='ascii') as log:
                            log.write(event.type().name + '\n')
                    return False
            window.monitor = InputMonitor(window)
            app.installEventFilter(window.monitor)
        temporary = folder / 'ready.tmp'
        temporary.write_text(json.dumps([int(window.winId()), os.getpid()]))
        temporary.replace(folder / 'hwnd')
    timer = QTimer(window)
    timer.timeout.connect(lambda: app.quit() if (folder / 'close').exists() else None)
    timer.start(100)
    QTimer.singleShot(350, ready)
    QTimer.singleShot(90000, app.quit)
    return app.exec()


@unittest.skipUnless(sys.platform == 'win32' and all(importlib.util.find_spec(lib)
    for lib in ('PySide6','comtypes','rapidocr_onnxruntime')), 'Windows Qt/VisFunc desktop dependencies')
class NativeTests(unittest.TestCase):
    def test_owned_qt_window_and_painted_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--fixture', folder],
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'windows'})
            scope = SecretScope()
            try:
                for _ in range(200):
                    if (root / 'hwnd').is_file() or proc.poll() is not None:
                        break
                    time.sleep(.05)
                hwnd, pid = json.loads((root / 'hwnd').read_text())
                decisions = []
                def approve(target, args, frame, token):
                    self.assertEqual(target, f'win32:{pid}:{hwnd}')
                    decisions.append(args['reason'])
                    return args['reason'] != 'deny'
                lane = ComputerLane(ComputerSession(WindowsControlDriver(hwnd, pid)), rows=[],
                    selection=None, environment={}, consent=False, scope=scope, cancel=threading.Event(),
                    workspace=root, home=root / 'runtime', policy=Policy(workspace=root), clicks=True, approve=approve)
                def call(name, args):
                    result = lane.call(name, args)
                    self.assertTrue(result['ok'], result.get('error'))
                    return json.loads(result['content'])
                def locate(label):
                    shot = call('computer_screenshot', {'target': label})
                    self.assertEqual(shot['local']['status'], 'found', shot['local'])
                    return shot
                def click_args(shot, reason):
                    return {'observation_id': shot['observation_id'], 'x': shot['point']['x'],
                            'y': shot['point']['y'], 'reason': reason}
                initial = call('computer_screenshot', {})
                self.assertIn('Apply profile', [c['label'] for c in initial['local']['controls']])
                shot = locate('ALPINE TARGET')
                self.assertEqual(shot['local']['source'], 'visfunc_ocr')
                field = locate('Profile name')
                self.assertEqual(field['local']['source'], 'visfunc_uia')
                focused = call('computer_click', click_args(field, 'focus input'))
                call('computer_type', {'observation_id': focused['observation_id'], 'text': '测试42', 'reason': 'type'})
                checked = call('computer_click', click_args(locate('Enabled option'), 'check option'))
                self.assertTrue(any(c['label'] == 'Enabled option' and c.get('checked') == 'on'
                                    for c in checked['local']['controls']))
                submit = locate('Apply profile')
                denied = lane.call('computer_click', click_args(submit, 'deny'))
                self.assertFalse(denied['ok'])
                self.assertFalse((root / 'applied').exists())
                call('computer_click', click_args(locate('Apply profile'), 'save'))
                self.assertEqual((root / 'applied').read_text(encoding='utf8'), '测试42')
                self.assertEqual(decisions, ['focus input','type','check option','deny','save'])
            finally:
                (root / 'close').touch()
                proc.wait(timeout=5)
                scope.close()


@unittest.skipUnless(sys.platform == 'win32' and all(importlib.util.find_spec(lib)
    for lib in ('PySide6', 'comtypes')), 'Windows Qt accessibility fixture')
class BackgroundTests(unittest.TestCase):
    def test_work_lane_sets_toggles_and_invokes_without_desktop_input(self):
        import ctypes
        from ctypes import wintypes
        desktop = ctypes.WinDLL('user32', use_last_error=True)
        desktop.GetForegroundWindow.restype = wintypes.HWND
        class GuiThreadInfo(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD)] + [
                (name, wintypes.HWND) for name in ('active', 'focus', 'capture', 'menu', 'move', 'caret')
            ] + [('caret_rect', wintypes.RECT)]
        class LastInput(ctypes.Structure):
            _fields_ = [('size', wintypes.UINT), ('tick', wintypes.DWORD)]

        def desktop_state():
            pos = wintypes.POINT()
            self.assertTrue(desktop.GetCursorPos(ctypes.byref(pos)))
            info = GuiThreadInfo()
            info.size = ctypes.sizeof(info)
            self.assertTrue(desktop.GetGUIThreadInfo(0, ctypes.byref(info)))
            last = LastInput()
            last.size = ctypes.sizeof(last)
            self.assertTrue(desktop.GetLastInputInfo(ctypes.byref(last)))
            return desktop.GetForegroundWindow(), pos.x, pos.y, info.focus, last.tick

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--background-fixture', folder],
                                    env={**os.environ, 'QT_QPA_PLATFORM': 'windows'})
            scope = SecretScope()
            try:
                for _ in range(200):
                    if (root / 'hwnd').is_file() or proc.poll() is not None:
                        break
                    time.sleep(.05)
                hwnd, pid = json.loads((root / 'hwnd').read_text())
                previous = desktop_state()
                self.assertIsNotNone(previous[0], 'This test requires an interactive desktop with a foreground window')
                self.assertNotEqual(previous[0], hwnd)
                decisions = []
                def approve(target, args, frame, token):
                    decisions.append(args['control'])
                    return True
                lane = ComputerLane(ComputerSession(WindowsControlDriver(hwnd, pid)), rows=[],
                    selection=None, environment={}, consent=False, scope=scope, cancel=threading.Event(),
                    workspace=root, home=root / 'runtime', policy=Policy(workspace=root), clicks=True,
                    input_mode='background', approve=approve)
                names = {schema['function']['name'] for schema in lane.schemas()}
                self.assertIn('computer_act', names)
                self.assertFalse(names & {'computer_key', 'computer_type', 'computer_click'})
                def call(name, args):
                    before = desktop_state()
                    result = lane.call(name, args)
                    self.assertTrue(result['ok'], result.get('error'))
                    after = desktop_state()
                    self.assertNotEqual(after[0], hwnd)
                    # In an interactive session the human may move the pointer or
                    # change apps. Compare positions/focus over idle intervals and
                    # independently check target events even while the human works.
                    if after[-1] == before[-1]:
                        self.assertEqual(after, before)
                    return json.loads(result['content'])
                state = call('computer_observe', {})
                self.assertNotIn('Private password fixture', str(state))
                for label, action in [('Profile name', 'type_text'), ('Enabled option', 'click'), ('Apply profile', 'click')]:
                    control = next(row for row in state['state']['elements'] if row['name'] == label)
                    args = {'observation_id': state['observation_id'], 'ref': control['ref'], 'action': action}
                    if action == 'type_text':
                        args['text'] = '后台中文 42'
                    state = call('computer_act', args)
                    self.assertNotIn('后台中文', str(state))
                    self.assertFalse(lane.call('computer_act', args)['ok'])
                    if label == 'Enabled option':
                        self.assertEqual(next(row for row in state['state']['elements'] if row['name'] == label)['checked'], 1)
                # UIA Invoke may return before Qt delivers its clicked signal.
                deadline = time.monotonic() + 3
                while not (root / 'invocations').exists() and time.monotonic() < deadline:
                    self.assertNotEqual(desktop_state()[0], hwnd)
                    time.sleep(.02)
                self.assertEqual((root / 'applied').read_text(encoding='utf8'), '后台中文 42')
                self.assertEqual((root / 'invocations').read_text(encoding='ascii'), '1')
                self.assertFalse((root / 'desktop-input').exists(), 'target received foreground activation or desktop input')
                self.assertEqual(decisions, ['Profile name', 'Enabled option', 'Apply profile'])
            finally:
                (root / 'close').touch()
                proc.wait(timeout=8)
                scope.close()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--background-fixture']:
        raise SystemExit(fixture(Path(sys.argv[2]), background=True))
    if sys.argv[1:2] == ['--fixture']:
        raise SystemExit(fixture(Path(sys.argv[2])))
    unittest.main()
