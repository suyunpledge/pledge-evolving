"""Human installation decisions for the Work conversation tools."""
import threading

from PySide6.QtCore import QObject, Property, Signal, Slot

from .local_tool_runtime import LocalToolRuntime, WINDOWS_PORTABLE, schemas


class LocalTools(QObject):
    changed = Signal()
    _requested = Signal(object)
    _cleared = Signal(object)
    _progress = Signal(str, int)

    def __init__(self, home, parent=None, *, demo=False):
        super().__init__(parent)
        self.demo = demo
        self._pending = None
        self._step = ''
        self._progress_value = -1
        self._shutting_down = threading.Event()
        self.runtime = LocalToolRuntime(home, self._choose, self._progress.emit)
        self._requested.connect(self._show_choice)
        self._cleared.connect(self._close_choice)
        self._progress.connect(self._update_progress)

    requestKind = Property(str, lambda self: self._pending['kind'] if self._pending else '', notify=changed)
    canDownload = Property(bool, lambda self: WINDOWS_PORTABLE and not self.demo, constant=True)
    message = Property(str, lambda self: self._step, notify=changed)
    percent = Property(int, lambda self: self._progress_value, notify=changed)

    def _choose(self, kind, cancel):
        if self.demo:
            return ''
        request = {'kind': kind, 'done': threading.Event(), 'choice': ''}
        self._requested.emit(request)
        while not request['done'].wait(0.1):
            if cancel.is_set() or self._shutting_down.is_set():
                self._cleared.emit(request)
                return ''
        return request['choice'] if not cancel.is_set() and not self._shutting_down.is_set() else ''

    @Slot(object)
    def _show_choice(self, request):
        if self._shutting_down.is_set() or self._pending is not None:
            request['done'].set()
            return
        self._pending = request
        self.changed.emit()

    @Slot(object)
    def _close_choice(self, request):
        if self._pending is request:
            self._pending = None
            self.changed.emit()

    @Slot(str)
    def respond(self, choice):
        if self._pending is None:
            return
        request = self._pending
        request['choice'] = choice if choice in {'existing', 'nvidia', 'cpu', 'amd'} else ''
        self._close_choice(request)
        request['done'].set()

    @Slot(str, int)
    def _update_progress(self, message, value):
        self._step, self._progress_value = message, value
        self.changed.emit()

    def tools(self):
        return schemas()

    def call(self, name, workspace, arguments, cancel):
        if self.demo:
            return {'ok': False, 'error': '预览不会执行本机工具。',
                    'meta': {'execution_state': 'not_started'}}
        return self.runtime.call(name, workspace, arguments, cancel)

    def shutdown(self):
        self._shutting_down.set()
        self.respond('')
        self.runtime.shutdown()
