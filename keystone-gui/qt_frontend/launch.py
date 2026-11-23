"""Startup (launch) animation slot.

A small frameless window shows a brand-mark fade while the main window and its
services load. It never holds the app back: it is shown before the heavy main
QML loads, and it fades out on the main window's first frame even if the
animation has not finished. A click or Esc skips it. It is skipped entirely
under reduced motion, in smoke runs, with ``KEYSTONE_NO_SPLASH=1``, or when
turned off in the settings.

Replacing the placeholder (first match wins):

1. ``"launch": {"source": "<path>"}`` in the desktop settings file
   (``keystone.compat.desktop_config(home)``; relative paths are resolved
   against ``<home>/gui/qt/launch``);
2. a file named ``launch.<ext>`` in ``<home>/gui/qt/launch/``.

Supported: ``.qml`` (a root Item, which can read ``launch.ready`` and call
``launch.skip()``), video (``.mp4 .webm .mov .m4v``, played muted), animated
images (``.gif .webp``) and still images (``.png .jpg .jpeg .svg``). A file that
is missing, too large or of another type falls back to the built-in fade.
``"launch": {"enabled": false}`` turns the slot off.
"""

import os
from pathlib import Path

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication

from keystone.compat import desktop_config

from .store import SessionStore

KINDS = {'.qml': 'qml', '.mp4': 'video', '.webm': 'video', '.mov': 'video', '.m4v': 'video',
         '.gif': 'animated', '.webp': 'animated', '.png': 'image', '.jpg': 'image', '.jpeg': 'image',
         '.svg': 'image'}
MAX_BYTES = {'qml': 256 * 1024, 'video': 200 * 1024 * 1024, 'animated': 40 * 1024 * 1024,
             'image': 20 * 1024 * 1024}
ANIMATION_MS = 1400   # the built-in brand-mark fade
MAX_WAIT_MS = 12000   # safety cap if the main window never reports its first frame
FADE_MS = 180
SIZE = (420, 260)


def launch_folder(home):
    return Path(home) / 'gui' / 'qt' / 'launch'


def resolve_source(home):
    """Return (kind, path) for a replacement animation, or ('builtin', None)."""
    settings = SessionStore.read(desktop_config(Path(home)), {})
    config = settings.get('launch') if isinstance(settings, dict) else None
    config = config if isinstance(config, dict) else {}
    folder = launch_folder(home)
    candidates = []
    source = config.get('source')
    if isinstance(source, str) and source.strip():
        path = Path(os.path.expandvars(os.path.expanduser(source.strip())))
        candidates.append(path if path.is_absolute() else folder / path)
    candidates += [folder / f'launch{suffix}' for suffix in KINDS]
    for path in candidates:
        kind = KINDS.get(path.suffix.lower())
        try:
            if kind and path.is_file() and 0 < path.stat().st_size <= MAX_BYTES[kind]:
                return kind, path
        except OSError:
            continue
    return 'builtin', None


def should_play(home, *, reduce_motion=None, smoke=False):
    """Whether to show the launch animation at all."""
    if smoke or os.environ.get('KEYSTONE_NO_SPLASH', '').strip() not in ('', '0'):
        return False
    if reduce_motion is None:
        forced = os.environ.get('KEYSTONE_REDUCE_MOTION', '').strip()
        if forced:
            reduce_motion = forced != '0'
        else:
            prefs = SessionStore.read(Path(home) / 'gui' / 'qt' / 'preferences.json', {})
            reduce_motion = isinstance(prefs, dict) and prefs.get('reduceMotion') is True
    if reduce_motion:
        return False
    settings = SessionStore.read(desktop_config(Path(home)), {})
    config = settings.get('launch') if isinstance(settings, dict) else None
    return not (isinstance(config, dict) and config.get('enabled') is False)


class LaunchSlot(QObject):
    """The launch window. ``show()`` returns at once; ``markReady()`` ends it."""
    changed = Signal()
    finished = Signal()

    def __init__(self, home, brand_url=QUrl(), parent=None, *, max_wait_ms=MAX_WAIT_MS):
        super().__init__(parent)
        self._kind, path = resolve_source(home)
        self._source = QUrl.fromLocalFile(str(path)) if path else QUrl()
        self._brand = brand_url
        self._ready = False
        self._ended = False
        self._view = None
        self._cap = QTimer(self, singleShot=True, interval=max_wait_ms)
        self._cap.timeout.connect(self.markReady)
        self._closer = QTimer(self, singleShot=True, interval=FADE_MS + 40)
        self._closer.timeout.connect(self._close)

    kind = Property(str, lambda s: s._kind, constant=True)
    source = Property(QUrl, lambda s: s._source, constant=True)
    brandUrl = Property(QUrl, lambda s: s._brand, constant=True)
    duration = Property(int, lambda s: ANIMATION_MS, constant=True)
    fadeMs = Property(int, lambda s: FADE_MS, constant=True)
    ready = Property(bool, lambda s: s._ready, notify=changed)
    ended = Property(bool, lambda s: s._ended, notify=changed)
    view = property(lambda s: s._view)

    def show(self):
        from PySide6.QtQuick import QQuickView
        view = QQuickView()
        view.setObjectName('keystoneLaunch')
        view.setTitle('Keystone')
        view.setFlags(Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
                      | Qt.WindowType.WindowStaysOnTopHint)
        view.setColor(QColor(0, 0, 0, 0))
        view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
        view.rootContext().setContextProperty('launch', self)
        view.setSource(QUrl.fromLocalFile(str(Path(__file__).resolve().parent / 'qml' / 'Launch.qml')))
        if view.status() != QQuickView.Status.Ready:
            view.deleteLater()
            self._end()
            return self
        view.resize(*SIZE)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            view.setPosition(area.x() + (area.width() - SIZE[0]) // 2, area.y() + (area.height() - SIZE[1]) // 2)
        self._view = view
        view.show()
        view.requestActivate()
        self._cap.start()
        # Paint the first frame before the caller goes on to the blocking main-window load.
        QGuiApplication.processEvents()
        return self

    @Slot()
    def markReady(self):
        """The app is usable: fade out now, whether or not the animation finished."""
        if self._ready or self._ended:
            return
        self._ready = True
        self.changed.emit()
        if self._view is None:
            self._end()
        else:
            self._closer.start()

    @Slot()
    def skip(self):
        self._ready = True
        self._close()

    def _close(self):
        if self._view is not None:
            self._view.close()
            self._view.deleteLater()
            self._view = None
        self._end()

    def _end(self):
        if not self._ended:
            self._ended = True
            self._cap.stop()
            self.changed.emit()
            self.finished.emit()
