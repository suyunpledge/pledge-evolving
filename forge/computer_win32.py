"""Bounded Windows standard-control driver (Button/Edit), no global input.

The trusted host selects an HWND AND owner PID. This is deliberately not a
general browser/UIA driver: unsupported controls advertise no actions.
"""
import ctypes as c
from ctypes import wintypes as w
import os
import time


class WindowsControlDriver:
    def __init__(self, window, process_id):
        if os.name != 'nt':
            raise RuntimeError('Windows controls require Windows')
        self.window, self.process_id = int(window), int(process_id)
        self.target_id = f'win32:{self.process_id}:{self.window}'
        self.u = c.WinDLL('user32', use_last_error=True)
        self.u.IsWindow.argtypes = [w.HWND]
        self.u.IsChild.argtypes = [w.HWND, w.HWND]
        self.u.IsWindowEnabled.argtypes = [w.HWND]
        self.u.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
        self.u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
        self.u.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
        self.u.GetWindowLongW.argtypes = [w.HWND, c.c_int]
        self.callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.u.EnumChildWindows.argtypes = [w.HWND, self.callback_type, w.LPARAM]
        self.u.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM,
                                             w.UINT, w.UINT, c.POINTER(c.c_size_t)]
        self.u.SendMessageTimeoutW.restype = w.LPARAM
        self._validate()

    def _validate(self):
        owner = w.DWORD()
        if not self.u.IsWindow(self.window):
            raise ValueError('Selected window no longer exists')
        self.u.GetWindowThreadProcessId(self.window, c.byref(owner))
        if owner.value != self.process_id:
            raise ValueError('Selected window belongs to another process')

    def _message(self, window, message, wp=0, lp=0):
        result = c.c_size_t()
        if not self.u.SendMessageTimeoutW(window, message, wp, lp, 0x2 | 0x20, 500, c.byref(result)):
            raise TimeoutError('Window did not confirm the message within 500 ms')
        return result.value

    def _element(self, window):
        cls = c.create_unicode_buffer(128)
        self.u.GetClassNameW(window, cls, len(cls))
        rect = w.RECT()
        if not self.u.GetWindowRect(window, c.byref(rect)):
            raise ValueError('Cannot read target geometry')
        kind = cls.value.lower()
        password = kind == 'edit' and self.u.GetWindowLongW(window, -16) & 0x20
        text = c.create_unicode_buffer(4096)
        if not password:
            if self._message(window, 0xE) >= len(text):
                raise ValueError('Control text exceeds complete observation budget')
            self._message(window, 0xD, len(text), c.addressof(text))  # WM_GETTEXT
        enabled = bool(self.u.IsWindowEnabled(window))
        style = self.u.GetWindowLongW(window, -16)
        actions = ['click'] if kind == 'button' and enabled and style & 0xF in {0,1,2,3,4,5,6,9} else ['type_text'] if kind == 'edit' and enabled and not password and not style & 0x800 else []
        return {'ref': str(int(window)), 'class': cls.value, 'text': text.value,
                'rect': [rect.left, rect.top, rect.right, rect.bottom], 'enabled': enabled,
                'actions': actions, 'checked': self._message(window, 0xF0) if kind == 'button' else None}

    def observe(self):
        self._validate()
        elements, errors = [], []
        deadline = time.monotonic() + 3
        def visit(window, _):
            try:
                if len(elements) >= 64 or time.monotonic() > deadline:
                    raise ValueError('Window exceeds bounded control inspection budget')
                owner = w.DWORD(); self.u.GetWindowThreadProcessId(window, c.byref(owner))
                if owner.value != self.process_id:
                    raise ValueError('Child belongs to another process')
                elements.append(self._element(window))
                return True
            except Exception as exc:
                errors.append(exc); return False
        self.u.EnumChildWindows(self.window, self.callback_type(visit), 0)
        if errors: raise errors[0]
        self._validate()
        return {'window': self._element(self.window), 'elements': elements}

    def act(self, ref, action, text=''):
        self._validate()
        window = int(ref)
        if not self.u.IsChild(self.window, window):
            raise ValueError('Element no longer belongs to selected window')
        element = self._element(window)
        if action not in element['actions']:
            raise ValueError('Unsupported control action')
        if action == 'click':
            self._message(window, 0xF5)  # BM_CLICK; no focus-dependent global input.
        else:
            value = c.create_unicode_buffer(text)
            if not self._message(window, 0xC, 0, c.addressof(value)):  # WM_SETTEXT
                raise RuntimeError('Control rejected text input')
