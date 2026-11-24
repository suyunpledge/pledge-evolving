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
    @unittest.skipUnless(sys.platform == 'win32', 'Windows Edge launcher')
    def test_background_edge_uses_its_own_profile_and_does_not_request_activation(self):
        from qt_frontend.computer_service import launch_background_edge
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            binary = home / 'Microsoft/Edge/Application/msedge.exe'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.dict(os.environ, {'ProgramFiles(x86)': folder}), patch('subprocess.Popen') as spawn:
                with self.assertRaises(ValueError):
                    launch_background_edge(home, 'file:///private')
                spawn.assert_not_called()
                launch_background_edge(home, 'https://example.org/form')
                args, kwargs = spawn.call_args
                self.assertEqual(args[0][0], str(binary))
                self.assertIn('--user-data-dir=' + str(home / 'gui/background-edge'), args[0])
                self.assertIn('--disable-backgrounding-occluded-windows', args[0])
                self.assertEqual(args[0][-1], '--app=https://example.org/form')
                self.assertEqual(kwargs['startupinfo'].wShowWindow, 4)

    def test_focus_attribution_distinguishes_other_controls_from_target_activation(self):
        from scripts.verify_background_focus import classify
        def sample(top, focus, owner=12):
            return {'foreground': {'pid': owner, 'hwnd': top},
                    'focus': {'pid': owner if focus else 0, 'hwnd': focus}}
        old = sample(1, 2)
        self.assertEqual(classify(old, sample(1, 4), {9}), 'other_control_focus_change')
        self.assertEqual(classify(old, sample(1, 0), {9}), 'other_focus_transition_through_null')
        self.assertEqual(classify(old, sample(3, 4), {9}), 'other_foreground_change')
        self.assertEqual(classify(old, sample(3, 4, 9), {9}), 'target_foreground')

    def test_menu_headers_offer_state_specific_actions_and_leaves_remain_invokable(self):
        from keystone.computer_background import _menu_action
        self.assertEqual(_menu_action('menu_item', True, 0), 'expand')
        self.assertEqual(_menu_action('menu_item', True, 1), 'collapse')
        self.assertIsNone(_menu_action('menu_item', True, 3))
        self.assertIsNone(_menu_action('button', True, 0))

    def test_background_text_readback_waits_without_repeating_the_write(self):
        from unittest.mock import Mock, PropertyMock
        from keystone.computer_background import _set_value
        for updates, verified in ((['old', 'old', 'new'], True), (['old'] * 40, False)):
            handle = Mock(CurrentIsReadOnly=False)
            type(handle).CurrentValue = PropertyMock(side_effect=updates)
            now = [0.]
            def sleep(seconds):
                now[0] += seconds
            before_input = Mock()
            if verified:
                _set_value(handle, 'new', before_input, clock=lambda: now[0], sleep=sleep)
            else:
                with self.assertRaisesRegex(RuntimeError, '尚未读回'):
                    _set_value(handle, 'new', before_input, clock=lambda: now[0], sleep=sleep)
            handle.SetValue.assert_called_once_with('new')
            before_input.assert_called_once_with()

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
        from scripts.verify_background_focus import DesktopFocus
        monitor = DesktopFocus()
        desktop = monitor.user
        class LastInput(ctypes.Structure):
            _fields_ = [('size', wintypes.UINT), ('tick', wintypes.DWORD)]

        def desktop_state():
            state = monitor.sample()
            for _ in range(5):
                if state is not None:
                    break
                time.sleep(.01)
                state = monitor.sample()
            self.assertIsNotNone(state, 'Requires an interactive foreground desktop')
            self.assertNotIn(pid, (state['foreground']['pid'], state['focus']['pid']), state)
            pos = wintypes.POINT()
            self.assertTrue(desktop.GetCursorPos(ctypes.byref(pos)))
            last = LastInput()
            last.size = ctypes.sizeof(last)
            self.assertTrue(desktop.GetLastInputInfo(ctypes.byref(last)))
            idle_ms = (ctypes.windll.kernel32.GetTickCount() - last.tick) & 0xffffffff
            return (pos.x, pos.y), last.tick, idle_ms

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
                desktop_state()
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
                    # In an interactive session the human may move the pointer or
                    # change apps. Compare positions/focus over idle intervals and
                    # independently check target events even while the human works.
                    if after[1] == before[1] and min(before[2], after[2]) > 150:
                        self.assertEqual(after[0], before[0])
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
                    desktop_state()
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
