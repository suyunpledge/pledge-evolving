"""Record Windows foreground/focus attribution during a local background-action test.

Example: python scripts/verify_background_focus.py --pid 1234 --pid 5678 --seconds 60 --output focus.jsonl
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


def record(desktop, targets, seconds, out):
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

    def emit(kind, **fields):
        out.write(json.dumps(dict(time=time.time(), event=kind, **fields)) + '\n')
        out.flush()

    @callback_type
    def notify(_hook, event, hwnd, obj, child, thread, tick):
        # The event source can differ from the actual foreground when an IME
        # or another app opens a popup, or when focus changes again in transit.
        emit('foreground_notification' if event == 3 else 'focus_notification',
             source=desktop.window(hwnd), object_id=obj, child_id=child,
             event_thread=thread, event_tick=tick, observed=desktop.sample())

    hooks = []
    try:
        for event in (3, 0x8005):  # EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_FOCUS
            hook = user.SetWinEventHook(event, event, None, notify, 0, 0, 0)
            if not hook:
                raise c.WinError(c.get_last_error())
            hooks.append(hook)
        emit('start', target_pids=sorted(targets))
        message = w.MSG()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while user.PeekMessageW(c.byref(message), None, 0, 0, 1):
                user.TranslateMessage(c.byref(message))
                user.DispatchMessageW(c.byref(message))
            current = desktop.sample()
            pointer = desktop.pointer()
            if pointer and last_pointer:
                if pointer[1] != last_pointer[1]:
                    emit('input_activity')
                elif pointer[0] != last_pointer[0]:
                    emit('pointer_change_same_input_tick')
            last_pointer = pointer
            if current is None:
                unstable_samples += 1
            else:
                samples += 1
                if current != last:
                    emit('owner', reason=classify(last, current, targets), previous=last, current=current)
                    last = current
            time.sleep(.01)
    finally:
        for hook in hooks:
            user.UnhookWinEvent(hook)
        emit('end', samples=samples, unstable_samples=unstable_samples)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, action='append', required=True, help='selected app or Keystone PID')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or not 0 < args.seconds <= 86400 or min(args.pid) <= 0:
        parser.error('Requires Windows, positive PIDs and a duration up to 86400 seconds.')
    with args.output.open('x', encoding='utf8') as stream:
        record(DesktopFocus(), set(args.pid), args.seconds, stream)
