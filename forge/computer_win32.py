"""Bounded Windows controls and explicitly approved physical coordinate input.

The trusted host selects an HWND AND owner PID. This is deliberately not a
general browser/UIA driver: unsupported controls advertise no native actions.
Visual clicks have a separate screenshot, approval and final hit-test lifecycle.
"""
import ctypes as c
from ctypes import wintypes as w
import os
import time
from contextlib import contextmanager


class CoordinateNotStarted(RuntimeError):
    """Driver rejected a coordinate before any input message was dispatched."""


@contextmanager
def dpi_context(u, context=-4):
    u.SetThreadDpiAwarenessContext.argtypes = [w.HANDLE]
    u.SetThreadDpiAwarenessContext.restype = w.HANDLE
    old = u.SetThreadDpiAwarenessContext(w.HANDLE(context))
    if not old: raise CoordinateNotStarted('Cannot establish DPI coordinate space')
    try: yield
    finally: u.SetThreadDpiAwarenessContext(old)


def list_windows(*, exclude_pid=None):
    """User-requested picker inventory; no pixels, focus changes or input."""
    if os.name != 'nt':
        raise RuntimeError('Window selection requires Windows')
    u = c.WinDLL('user32', use_last_error=True)
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    u.EnumWindows.argtypes = [callback_type, w.LPARAM]
    u.IsWindowVisible.argtypes = [w.HWND]
    u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    u.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    rows = []
    def visit(window, _):
        owner = w.DWORD()
        u.GetWindowThreadProcessId(window, c.byref(owner))
        title = c.create_unicode_buffer(512)
        if owner.value != exclude_pid and u.IsWindowVisible(window):
            u.GetWindowTextW(window, title, len(title))
            if title.value:
                rows.append({'window': int(window), 'process_id': owner.value, 'title': title.value})
        return len(rows) < 128
    u.EnumWindows(callback_type(visit), 0)
    return rows


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

    def screenshot(self, *, cancelled=None):
        from .computer_capture import capture_window
        self._validate()
        return capture_window(self.window, self.process_id, cancelled=cancelled)

    def geometry(self):
        self._validate()
        self.u.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
        self.u.ClientToScreen.argtypes = [w.HWND, c.POINTER(w.POINT)]
        self.u.GetDpiForWindow.argtypes = [w.HWND]
        self.u.GetDpiForWindow.restype = w.UINT
        with dpi_context(self.u):
            rect, client, origin = w.RECT(), w.RECT(), w.POINT()
            if not (self.u.GetWindowRect(self.window, c.byref(rect)) and
                    self.u.GetClientRect(self.window, c.byref(client)) and
                    self.u.ClientToScreen(self.window, c.byref(origin))):
                raise CoordinateNotStarted('Window geometry unavailable')
            return {'rect': [rect.left,rect.top,rect.right,rect.bottom],
                    'client_rect': [origin.x,origin.y,origin.x+client.right,origin.y+client.bottom],
                    'dpi': int(self.u.GetDpiForWindow(self.window))}

    def _client_point(self, window, sx, sy):
        self.u.GetWindowDpiAwarenessContext.argtypes = [w.HWND]
        self.u.GetWindowDpiAwarenessContext.restype = w.HANDLE
        self.u.PhysicalToLogicalPointForPerMonitorDPI.argtypes = [w.HWND,c.POINTER(w.POINT)]
        self.u.ScreenToClient.argtypes = [w.HWND,c.POINTER(w.POINT)]
        point = w.POINT(sx,sy)
        with dpi_context(self.u, self.u.GetWindowDpiAwarenessContext(window)):
            if not (self.u.PhysicalToLogicalPointForPerMonitorDPI(window,c.byref(point)) and
                    self.u.ScreenToClient(window,c.byref(point))):
                raise CoordinateNotStarted('Cannot map physical screenshot to target client coordinates')
        return point

    def prepare_coordinate(self, frame, x, y):
        """Resolve only descendants of the bound root; no desktop hit-test/focus."""
        if self.geometry() != {k:frame[k] for k in ('rect','client_rect','dpi')}:
            raise CoordinateNotStarted('Window geometry changed; capture again')
        sx,sy = frame['rect'][0]+x, frame['rect'][1]+y
        left,top,right,bottom = frame['client_rect']
        if not (left <= sx < right and top <= sy < bottom):
            raise CoordinateNotStarted('Only the selected window client area is clickable')
        self.u.ChildWindowFromPointEx.argtypes = [w.HWND,w.POINT,w.UINT]
        self.u.ChildWindowFromPointEx.restype = w.HWND
        self.u.IsWindowVisible.argtypes = [w.HWND]
        window = self.window
        for _ in range(32):
            owner = w.DWORD(); self.u.GetWindowThreadProcessId(window,c.byref(owner))
            if (owner.value != self.process_id or not self.u.IsWindowVisible(window)
                    or not self.u.IsWindowEnabled(window)):
                raise CoordinateNotStarted('Click target is unavailable or disabled')
            point = self._client_point(window,sx,sy)
            with dpi_context(self.u, self.u.GetWindowDpiAwarenessContext(window)):
                child = self.u.ChildWindowFromPointEx(window,point,0)
            if not child: raise CoordinateNotStarted('No client target at this point')
            if child == window:
                if not (0 <= point.x <= 32767 and 0 <= point.y <= 32767):
                    raise CoordinateNotStarted('Target coordinates exceed message range')
                return {'window':int(window), 'x':point.x, 'y':point.y,
                        'pixel_x':x, 'pixel_y':y}
            if not self.u.IsChild(self.window,child):
                raise CoordinateNotStarted('Point resolved outside the selected window')
            window = child
        raise CoordinateNotStarted('Target nesting exceeds inspection budget')

    def activate_for_click(self, *, cancelled=None):
        self._validate()
        if cancelled is not None and cancelled.is_set(): raise CoordinateNotStarted('Click cancelled during activation')
        self.u.SetForegroundWindow.argtypes=[w.HWND]
        self.u.GetForegroundWindow.restype=w.HWND
        # Windows may deny background focus requests. A visible background window
        # can still receive a normal physical click; final hit-testing is mandatory.
        self.u.SetForegroundWindow(self.window)

    def coordinate_click(self, prepared, frame, *, cancelled=None):
        if self.prepare_coordinate(frame,prepared['pixel_x'],prepared['pixel_y']) != prepared:
            raise CoordinateNotStarted('Click target changed after approval')
        if cancelled is not None and cancelled.is_set():
            raise CoordinateNotStarted('Click cancelled before dispatch')
        sx,sy=frame['rect'][0]+prepared['pixel_x'],frame['rect'][1]+prepared['pixel_y']
        self.u.WindowFromPoint.argtypes=[w.POINT];self.u.WindowFromPoint.restype=w.HWND
        self.u.GetAncestor.argtypes=[w.HWND,w.UINT];self.u.GetAncestor.restype=w.HWND
        self.u.GetForegroundWindow.restype=w.HWND
        self.u.GetSystemMetrics.argtypes=[c.c_int]
        self.u.GetAsyncKeyState.argtypes=[c.c_int];self.u.GetAsyncKeyState.restype=c.c_short
        class ThreadInfo(c.Structure):
            _fields_=[('size',w.DWORD),('flags',w.DWORD),('active',w.HWND),('focus',w.HWND),
                      ('capture',w.HWND),('menu',w.HWND),('move',w.HWND),('caret',w.HWND),('rect',w.RECT)]
        self.u.GetGUIThreadInfo.argtypes=[w.DWORD,c.POINTER(ThreadInfo)]
        class Mouse(c.Structure):
            _fields_=[('dx',w.LONG),('dy',w.LONG),('data',w.DWORD),('flags',w.DWORD),
                      ('time',w.DWORD),('extra',c.c_size_t)]
        class Union(c.Union): _fields_=[('mouse',Mouse)]
        class Input(c.Structure): _fields_=[('type',w.DWORD),('value',Union)]
        self.u.SendInput.argtypes=[w.UINT,c.POINTER(Input),c.c_int];self.u.SendInput.restype=w.UINT
        with dpi_context(self.u):
            hit=self.u.WindowFromPoint(w.POINT(sx,sy))
            owner=w.DWORD();self.u.GetWindowThreadProcessId(hit,c.byref(owner))
            if (not hit or int(hit)!=prepared['window'] or owner.value!=self.process_id or self.u.GetAncestor(hit,2)!=self.window
                    or any(self.u.GetAsyncKeyState(key)&0x8000 for key in (1,2,4,16,17,18,91,92))):
                raise CoordinateNotStarted('Click point is covered or user input is active; bring target forward and capture again')
            info=ThreadInfo();info.size=c.sizeof(info)
            if not self.u.GetGUIThreadInfo(0,c.byref(info)) or info.capture or info.menu or info.move:
                raise CoordinateNotStarted('Desktop has mouse capture, a menu or a window move in progress')
            left,top=self.u.GetSystemMetrics(76),self.u.GetSystemMetrics(77)
            width,height=self.u.GetSystemMetrics(78),self.u.GetSystemMetrics(79)
            if not (width>1 and height>1 and left<=sx<left+width and top<=sy<top+height):
                raise CoordinateNotStarted('Click is outside the virtual desktop')
            x,y=round((sx-left)*65535/(width-1)),round((sy-top)*65535/(height-1))
            inputs=(Input*3)(Input(0,Union(Mouse(x,y,0,0xC001,0,0))),
                              Input(0,Union(Mouse(0,0,0,2,0,0))),Input(0,Union(Mouse(0,0,0,4,0,0))))
            if cancelled is not None and cancelled.is_set(): raise CoordinateNotStarted('Click cancelled before input')
            sent=self.u.SendInput(3,inputs,c.sizeof(Input))
            if sent!=3:
                if sent==2: self.u.SendInput(1,c.byref(inputs[2]),c.sizeof(Input))  # release only
                raise RuntimeError(f'Native click outcome unknown: {sent}/3 input events inserted; do not replay')

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
