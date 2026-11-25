"""Record Windows foreground/focus attribution during a local background-action test.

Example: python scripts/verify_background_focus.py --pid 1234 --pid 5678 --seconds 60 --verify-while-typing --output focus.jsonl
Records window handles, process/thread IDs and activity timing, without window text, keys or coordinates.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import time


class GuiThreadInfo(c.Structure):
    _fields_ = [('size', w.DWORD), ('flags', w.DWORD)] + [
        (name, w.HWND) for name in ('active', 'focus', 'capture', 'menu', 'move', 'caret')
    ] + [('rect', w.RECT)]


class LastInput(c.Structure):
    _fields_ = [('size', w.UINT), ('tick', w.DWORD)]


class DesktopFocus:
    def __init__(self):
        self.user = c.WinDLL('user32', use_last_error=True)
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
        self.user.GetGUIThreadInfo.argtypes = [w.DWORD, c.POINTER(GuiThreadInfo)]
        self.user.GetLastInputInfo.argtypes = [c.POINTER(LastInput)]
        self.user.GetCursorPos.argtypes = [c.POINTER(w.POINT)]

    def is_input_desktop(self):
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentThreadId.restype = w.DWORD
        self.user.GetThreadDesktop.argtypes = [w.DWORD]
        self.user.GetThreadDesktop.restype = w.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
        handle = self.user.GetThreadDesktop(kernel.GetCurrentThreadId())
        active = w.BOOL()
        # UOI_IO answers whether this specific desktop receives physical input.
        if not handle or not self.user.GetUserObjectInformationW(handle, 6, c.byref(active), c.sizeof(active), None):
            raise c.WinError(c.get_last_error())
        return bool(active.value)

    def pointer(self):
        point = w.POINT()
        last = LastInput(c.sizeof(LastInput), 0)
        if self.user.GetCursorPos(c.byref(point)) and self.user.GetLastInputInfo(c.byref(last)):
            return (point.x, point.y), last.tick

    def window(self, hwnd):
        pid = w.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, c.byref(pid)) if hwnd else 0
        return {'hwnd': hwnd or 0, 'pid': pid.value, 'thread': thread}

    def sample(self):
        foreground = self.window(self.user.GetForegroundWindow())
        if not foreground['thread']:
            return None
        info = GuiThreadInfo()
        info.size = c.sizeof(info)
        if not self.user.GetGUIThreadInfo(foreground['thread'], c.byref(info)):
            return None
        focus = self.window(info.focus)
        if foreground['hwnd'] != self.user.GetForegroundWindow():
            return None
        return {'foreground': foreground, 'focus': focus}


def classify(previous, current, targets):
    if current['foreground']['pid'] in targets:
        return 'target_foreground'
    if current['focus']['pid'] in targets:
        return 'target_keyboard_focus'
    if previous is None:
        return 'initial'
    if current['foreground']['hwnd'] != previous['foreground']['hwnd']:
        return 'other_foreground_change'
    if not current['focus']['hwnd'] or not previous['focus']['hwnd']:
        return 'other_focus_transition_through_null'
    return 'other_control_focus_change'


class DeviceActivity:
    """Count keyboard and mouse notifications on separate message-only windows. No payload is read."""
    class Device(c.Structure):
        _fields_ = [('page', w.USHORT), ('usage', w.USHORT), ('flags', w.DWORD), ('target', w.HWND)]

    def __init__(self, user):
        self.user = user
        user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int,
                                         c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        user.CreateWindowExW.restype = w.HWND
        user.DestroyWindow.argtypes = [w.HWND]
        user.RegisterRawInputDevices.argtypes = [c.POINTER(self.Device), w.UINT, w.UINT]
        # HWND_MESSAGE has no visual surface and cannot receive desktop focus.
        self.windows = {}
        try:
            for usage in (2, 6):  # Generic Desktop mouse and keyboard
                hwnd = user.CreateWindowExW(0, 'STATIC', '', 0, 0, 0, 0, 0, -3, None, None, None)
                if not hwnd:
                    raise c.WinError(c.get_last_error())
                self.windows[usage] = hwnd
                device = self.Device(1, usage, 0x100, hwnd)  # RIDEV_INPUTSINK
                if not user.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device)):
                    raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        for usage, hwnd in self.windows.items():
            remove = self.Device(1, usage, 1, None)  # RIDEV_REMOVE
            self.user.RegisterRawInputDevices(c.byref(remove), 1, c.sizeof(remove))
            self.user.DestroyWindow(hwnd)
        self.windows.clear()


def record(desktop, targets, seconds, out, *, verify_typing=False, stop=None):
    user = desktop.user
    callback_type = c.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
    user.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HANDLE, callback_type, w.DWORD, w.DWORD, w.DWORD]
    user.SetWinEventHook.restype = w.HANDLE
    user.UnhookWinEvent.argtypes = [w.HANDLE]
    user.PeekMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
    user.DispatchMessageW.restype = c.c_ssize_t
    last = None
    samples = 0
    unstable_samples = 0
    last_pointer = None
    input_intervals = pointer_moves_without_input = target_owner_samples = target_foreground_events = 0

    def emit(kind, **fields):
        out.write(json.dumps(dict(time=time.time(), event=kind, **fields)) + '\n')
        out.flush()

    @callback_type
    def notify(_hook, event, hwnd, obj, child, thread, tick):
        # The event source can differ from the actual foreground when an IME
        # or another app opens a popup, or when focus changes again in transit.
        nonlocal target_foreground_events
        source = desktop.window(hwnd)
        if event == 3 and source['pid'] in targets:
            target_foreground_events += 1
        emit('foreground_notification' if event == 3 else 'focus_notification',
             source=source, object_id=obj, child_id=child,
             event_thread=thread, event_tick=tick, observed=desktop.sample())

    hooks = []
    devices = DeviceActivity(user) if verify_typing else None
    previous_mouse = pending_move = False
    pointer_samples = mouse_messages = matched_moves = 0
    keyboard_messages = batch = 0
    next_report = time.monotonic() + 1
    try:
        for event in (3, 0x8005):  # EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS
            hook = user.SetWinEventHook(event, event, None, notify, 0, 0, 0)
            if not hook:
                raise c.WinError(c.get_last_error())
            hooks.append(hook)
        emit('start', target_pids=sorted(targets))
        message = w.MSG()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not (stop and stop.is_set()):
            mouse = False
            while user.PeekMessageW(c.byref(message), None, 0, 0, 1):
                if devices and message.message == 0x00FF:  # WM_INPUT
                    if message.hWnd == devices.windows[6]:
                        batch += 1
                        keyboard_messages += 1
                    elif message.hWnd == devices.windows[2]:
                        mouse = True
                        mouse_messages += 1
                user.TranslateMessage(c.byref(message))
                user.DispatchMessageW(c.byref(message))
            if devices and time.monotonic() >= next_report:
                if batch:
                    emit('keyboard_activity', messages=batch)
                    batch = 0
                next_report = time.monotonic() + 1
            current = desktop.sample()
            pointer = desktop.pointer()
            if pending_move:
                # A position sample may land just before its device notification.
                if mouse:
                    matched_moves += 1
                else:
                    pointer_moves_without_input += 1
                    emit('pointer_change_without_mouse_activity')
                pending_move = False
            if pointer:
                pointer_samples += 1
            if pointer and last_pointer:
                if pointer[1] != last_pointer[1]:
                    input_intervals += 1
                    emit('input_activity')
                if pointer[0] != last_pointer[0]:
                    if pointer[1] == last_pointer[1]:
                        emit('pointer_change_same_input_tick')
                    if devices:
                        if mouse or previous_mouse:
                            matched_moves += 1
                        else:
                            pending_move = True
            previous_mouse = mouse
            last_pointer = pointer
            if current is None:
                unstable_samples += 1
            else:
                samples += 1
                if any(current[key]['pid'] in targets for key in ('focus', 'foreground')):
                    target_owner_samples += 1
                if current != last:
                    emit('owner', reason=classify(last, current, targets), previous=last, current=current)
                    last = current
            time.sleep(.01)
    finally:
        if devices:
            devices.close()
            if pending_move:
                pointer_moves_without_input += 1
                emit('pointer_change_without_mouse_activity')
            if batch:
                emit('keyboard_activity', messages=batch)
        for hook in hooks:
            user.UnhookWinEvent(hook)
        summary = dict(keyboard_messages=keyboard_messages, mouse_messages=mouse_messages, pointer_samples=pointer_samples,
                       pointer_moves_with_mouse_activity=matched_moves, samples=samples,
                       unstable_samples=unstable_samples, input_intervals=input_intervals,
                       pointer_moves_without_input=pointer_moves_without_input, target_owner_samples=target_owner_samples,
                       target_foreground_events=target_foreground_events)
        emit('end', **summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, action='append', required=True, help='selected app or revon PID')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-while-typing', action='store_true',
                        help='require observed input activity and zero target ownership or idle pointer movement')
    args = parser.parse_args()
    if sys.platform != 'win32' or not 0 < args.seconds <= 86400 or min(args.pid) <= 0:
        parser.error('Requires Windows, positive PIDs and a duration up to 86400 seconds.')
    with args.output.open('x', encoding='utf8') as stream:
        result = record(DesktopFocus(), set(args.pid), args.seconds, stream, verify_typing=args.verify_while_typing)
    if args.verify_while_typing:
        if not result['samples'] or not result['pointer_samples'] or not result['keyboard_messages']:
            sys.exit('Inconclusive: no foreground samples or no concurrent keyboard activity was observed')
        if any(result[key] for key in ('pointer_moves_without_input', 'target_owner_samples', 'target_foreground_events')):
            sys.exit('Foreground or pointer activity requires review; see the recorded events')
