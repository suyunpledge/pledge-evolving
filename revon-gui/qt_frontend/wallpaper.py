"""Local wallpaper library: import, validation, thumbnails, selection and persistence.

Imported files are copied into ``<home>/gui/qt/wallpapers`` and listed in its
``index.json``. The chosen wallpaper and its dimming live under the
``"wallpaper"`` key of the desktop settings file (``revon.compat.desktop_config``),
next to the keys other desktop front ends own, which are preserved on write.
"""

import math
from pathlib import Path
import re
import threading
import time
import uuid

from PySide6.QtCore import QLockFile, QObject, Property, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

from revon.compat import desktop_config

from .store import SessionStore

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
VIDEO_SUFFIXES = {'.mp4', '.webm', '.mov', '.m4v'}
KINDS = ('image', 'gif', 'video')
DEFAULT_DIM = 0.52
MAX_IMAGE_BYTES = 80 * 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
# GIF frames are decoded on the GUI thread while playing; keep them small enough not to stall input.
MAX_GIF_PIXELS = 16_000_000
THUMB_SIZE = QSize(320, 200)
FILE_NAME = re.compile(r'[a-f0-9]{32}\.[a-z0-9]+')


def _thumb_name(file):
    return file.split('.', 1)[0] + '.thumb.jpg'


def _thumbnail(image):
    scaled = image.scaled(THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    x, y = max(0, (scaled.width() - THUMB_SIZE.width()) // 2), max(0, (scaled.height() - THUMB_SIZE.height()) // 2)
    return scaled.copy(x, y, min(scaled.width(), THUMB_SIZE.width()), min(scaled.height(), THUMB_SIZE.height()))


def _is_nearly_black(image):
    """Detect fade-from-black opening video frames so thumbnails show actual content."""
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return True
    sample = image.scaled(QSize(24, 16), Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.FastTransformation)
    total = 0
    count = sample.width() * sample.height()
    for y in range(sample.height()):
        for x in range(sample.width()):
            c = sample.pixelColor(x, y)
            total += (c.red() * 299 + c.green() * 587 + c.blue() * 114) // 1000
    return (total / max(1, count)) < 10.0


class Wallpaper(QObject):
    changed = Signal()
    itemsChanged = Signal()  # only when the library itself changes, so picker tiles persist
    _copied = Signal(object, str)

    def __init__(self, home, parent=None):
        super().__init__(parent)
        self.home = Path(home)
        self.folder = self.home / 'gui' / 'qt' / 'wallpapers'
        self.index_path = self.folder / 'index.json'
        self.legacy_path = self.folder.parent / 'wallpaper.json'
        self._items = []          # [{'file', 'kind', 'name'}], newest first
        self._current = ''        # file name of the selected wallpaper; '' = built-in background
        self._broken = set()      # files that failed to render this session
        self._dim = DEFAULT_DIM
        self._busy = False
        self._error = ''
        self._pending = None
        self._probe = None
        self._sink = None
        self._probe_loaded = False
        self._probe_seeked = False
        self._probe_fallback = None
        self._discard = {}
        self._stop = threading.Event()
        self._copied.connect(self._finish_copy, Qt.ConnectionType.QueuedConnection)
        self._deadline = QTimer(self, singleShot=True, interval=12000)
        self._deadline.timeout.connect(self._probe_timeout)
        self._sweeper = QTimer(self, singleShot=True, interval=500)
        self._sweeper.timeout.connect(self._remove_discarded)
        self._load()

    # ------------------------------------------------------------------ state

    def _read_index(self):
        data = SessionStore.read(self.index_path, {})
        rows = data.get('items') if isinstance(data, dict) else None
        items, seen = [], set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            file, kind, name = row.get('file'), row.get('kind'), row.get('name')
            if (isinstance(file, str) and FILE_NAME.fullmatch(file) and kind in KINDS
                    and file not in seen and (self.folder / file).is_file()):
                seen.add(file)
                items.append({'file': file, 'kind': kind,
                              'name': name[:120] if isinstance(name, str) and name else file})
        return items

    def _load(self):
        self._items = self._read_index()
        settings = SessionStore.read(desktop_config(self.home), {})
        choice = settings.get('wallpaper') if isinstance(settings, dict) else None
        if not isinstance(choice, dict):
            choice = self._adopt_legacy()
        dim = choice.get('dim')
        if type(dim) in (int, float) and 0 <= dim <= 0.9:
            self._dim = float(dim)
        file = choice.get('file')
        if isinstance(file, str) and file:
            if any(item['file'] == file for item in self._items):
                self._current = file
            else:
                self._error = '找不到已导入的壁纸，已改用默认背景。'
        self._sweep_orphans()

    def _adopt_legacy(self):
        """Bring a wallpaper chosen with the earlier single-slot settings into the library."""
        record = SessionStore.read(self.legacy_path, {})
        if not isinstance(record, dict):
            return {}
        file, kind, name = record.get('file'), record.get('kind'), record.get('name')
        if (isinstance(file, str) and FILE_NAME.fullmatch(file) and kind in KINDS
                and (self.folder / file).is_file()
                and not any(item['file'] == file for item in self._items)):
            self._items.insert(0, {'file': file, 'kind': kind,
                                   'name': name[:120] if isinstance(name, str) and name else file})
            self._write(index=True)
        return {'file': file if isinstance(file, str) else '', 'dim': record.get('dim')}

    def _sweep_orphans(self):
        """Remove copies no longer listed (e.g. a deletion interrupted by a locked file)."""
        if not self.folder.is_dir():
            return
        keep = {item['file'] for item in self._items} | {_thumb_name(item['file']) for item in self._items}
        cutoff = time.time() - 600  # another window may still be importing a recent file
        try:
            for path in self.folder.iterdir():
                if (path.name not in keep and (FILE_NAME.fullmatch(path.name) or path.name.endswith('.thumb.jpg'))
                        and path.stat().st_mtime < cutoff):
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _write(self, *, index=False, choice=False):
        """Persist the index and/or the selection; returns an error message or ''."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(self.folder / '.lock'))
            if not lock.tryLock(2000):
                return '另一个 revon 窗口正在保存壁纸，请稍后重试。'
            try:
                if index:
                    SessionStore.atomic(self.index_path, {'items': self._items})
                if choice:
                    path = desktop_config(self.home)
                    settings = SessionStore.read(path, {})
                    settings = settings if isinstance(settings, dict) else {}
                    settings['wallpaper'] = {'file': self._current, 'dim': self._dim}
                    SessionStore.atomic(path, settings)
            finally:
                lock.unlock()
        except OSError:
            return '无法保存壁纸设置。'
        return ''

    def _item(self, file):
        return next((item for item in self._items if item['file'] == file), None)

    def _active(self):
        item = self._item(self._current)
        return item if item is not None and self._current not in self._broken else None

    def _url(self, name):
        return QUrl.fromLocalFile(str(self.folder / name))

    def _thumb_url(self, file):
        thumb = self.folder / _thumb_name(file)
        return QUrl.fromLocalFile(str(thumb)) if thumb.is_file() else QUrl()

    def _rows(self):
        return [{'id': item['file'], 'name': item['name'], 'kind': item['kind'],
                 'source': self._url(item['file']).toString(),
                 'thumb': self._thumb_url(item['file']).toString()} for item in self._items]

    source = Property(QUrl, lambda s: s._url(s._active()['file']) if s._active() else QUrl(), notify=changed)
    thumb = Property(QUrl, lambda s: s._thumb_url(s._active()['file']) if s._active() else QUrl(), notify=changed)
    kind = Property(str, lambda s: (s._active() or {}).get('kind', ''), notify=changed)
    name = Property(str, lambda s: (s._active() or {}).get('name', ''), notify=changed)
    currentId = Property(str, lambda s: s._current if s._active() else '', notify=changed)
    items = Property('QVariantList', _rows, notify=itemsChanged)
    dim = Property(float, lambda s: s._dim, notify=changed)
    busy = Property(bool, lambda s: s._busy, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)

    # ----------------------------------------------------------------- import

    @Slot(QUrl, bool)
    def importFile(self, url, dynamic):
        if self._busy or self._stop.is_set():
            return
        source = Path(url.toLocalFile()) if url.isLocalFile() else None
        suffix = source.suffix.lower() if source is not None else ''
        if not dynamic and suffix in IMAGE_SUFFIXES:
            kind = 'image'
        elif dynamic and suffix == '.gif':
            kind = 'gif'
        elif dynamic and suffix in VIDEO_SUFFIXES:
            kind = 'video'
        else:
            self._error = '请选择支持的本地图片、GIF 或视频。'
            self.changed.emit()
            return
        record = {'kind': kind, 'file': uuid.uuid4().hex + suffix, 'name': source.name[:120]}
        self._pending = record
        self._busy, self._error = True, ''
        self.changed.emit()
        threading.Thread(target=self._copy, args=(source, record), name='revon-wallpaper-import', daemon=True).start()

    def _copy(self, source, record):
        target = self.folder / record['file']
        partial = target.with_suffix(target.suffix + '.part')
        message = ''
        try:
            if not source.is_file():
                raise ValueError('无法读取选中的壁纸文件。')
            self.folder.mkdir(parents=True, exist_ok=True)
            limit = MAX_VIDEO_BYTES if record['kind'] == 'video' else MAX_IMAGE_BYTES
            if source.stat().st_size > limit:
                raise ValueError('图片和 GIF 最大支持 80 MB，视频最大支持 1 GB。')
            with source.open('rb') as incoming, partial.open('xb') as outgoing:
                size = 0
                while not self._stop.is_set():
                    chunk = incoming.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('壁纸文件超出大小限制。')
                    outgoing.write(chunk)
            if self._stop.is_set():
                return
            if record['kind'] != 'video':
                self._validate_image(partial, record)
            partial.rename(target)
        except (OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else '无法导入文件。请检查文件和可用磁盘空间。'
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        if self._stop.is_set() or message:
            for path in (target, self.folder / _thumb_name(record['file'])):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if not self._stop.is_set():
            self._copied.emit(record, message)

    def _validate_image(self, path, record):
        reader = QImageReader(str(path))
        try:
            reader.setAutoTransform(True)
            detected = bytes(reader.format())
            allowed = {b'gif'} if record['kind'] == 'gif' else {b'png', b'jpeg', b'webp', b'bmp'}
            if not reader.canRead() or detected not in allowed:
                raise ValueError('无法读取该图片，或文件内容与所选类型不符。')
            dimensions = reader.size()
            limit = MAX_GIF_PIXELS if record['kind'] == 'gif' else MAX_IMAGE_PIXELS
            if not dimensions.isValid() or dimensions.width() * dimensions.height() > limit:
                raise ValueError('壁纸图片太大，请缩小分辨率后重试。')
            image = reader.read()
            if image.isNull():
                raise ValueError('无法解码选中的壁纸图片。')
        finally:
            del reader
        # A missing thumbnail only costs the picker a live preview; never fail the import for it.
        _thumbnail(image).save(str(self.folder / _thumb_name(record['file'])), 'JPG', 86)

    def _finish_copy(self, record, error):
        if self._stop.is_set():
            return
        if error or record['kind'] != 'video':
            self._finish_import(error)
            return
        # Probe the copy: it must hold a playable video track. The first decoded frame
        # becomes the thumbnail. No audio output is attached, so nothing is heard.
        self._probe_loaded = False
        self._probe_seeked = False
        self._probe_fallback = None
        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._video_frame)
        self._probe = QMediaPlayer(self)
        self._probe.setVideoSink(self._sink)
        self._probe.mediaStatusChanged.connect(self._video_status)
        self._probe.errorOccurred.connect(self._video_error)
        self._deadline.start()
        self._probe.setSource(self._url(record['file']))

    def _video_status(self, status):
        if self._probe is None:
            return
        if status == QMediaPlayer.MediaStatus.LoadedMedia and not self._probe_loaded:
            if not self._probe.hasVideo():
                self._finish_import('文件中没有可播放的视频轨道。')
                return
            self._probe_loaded = True
            self._probe.play()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._video_error()

    def _video_frame(self, frame):
        if self._probe is None or self._pending is None or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        if (not self._probe_seeked and _is_nearly_black(image)
                and self._probe.isSeekable() and self._probe.duration() > 600):
            self._probe_seeked = True
            self._probe_fallback = image
            self._probe.setPosition(min(1200, self._probe.duration() // 3))
            return
        _thumbnail(image).save(str(self.folder / _thumb_name(self._pending['file'])), 'JPG', 88)
        self._probe_fallback = None
        self._finish_import('')

    def _probe_timeout(self):
        # Loaded with a video track but no frame in time: accept it (saving fallback frame if any).
        if self._probe_loaded and self._pending is not None and self._probe_fallback is not None:
            _thumbnail(self._probe_fallback).save(str(self.folder / _thumb_name(self._pending['file'])), 'JPG', 88)
        self._probe_fallback = None
        self._finish_import('' if self._probe_loaded else '无法读取该视频。可尝试使用 H.264 编码的 MP4。')

    def _video_error(self, *_):
        if self._probe is not None:
            self._finish_import('无法读取该视频。可尝试使用 H.264 编码的 MP4。')

    def _finish_import(self, error):
        self._deadline.stop()
        if self._probe is not None:
            probe, self._probe = self._probe, None
            probe.mediaStatusChanged.disconnect(self._video_status)
            probe.errorOccurred.disconnect(self._video_error)
            self._sink.videoFrameChanged.disconnect(self._video_frame)
            probe.stop()
            probe.setSource(QUrl())
            probe.deleteLater()
            self._sink.deleteLater()
            self._sink = None
        record, self._pending = self._pending, None
        if record is None:
            return
        if not error:
            # Merge with what another window may have added meanwhile.
            self._items = [record] + [i for i in self._read_index() if i['file'] != record['file']]
            previous = self._current
            self._current = record['file']
            error = self._write(index=True, choice=True)
            if error:
                self._items = [i for i in self._items if i['file'] != record['file']]
                self._current = previous
        if error:
            self._queue_remove(record['file'])
        else:
            self.itemsChanged.emit()
        self._error, self._busy = error, False
        self.changed.emit()

    # -------------------------------------------------------------- selection

    @Slot(str)
    def select(self, file):
        if self._busy:
            return
        file = file or ''
        if file and self._item(file) is None:
            self._error = '找不到该壁纸。'
            self.changed.emit()
            return
        self._broken.discard(file)
        previous, self._current = self._current, file
        self._error = self._write(choice=True)
        if self._error:
            self._current = previous
        self.changed.emit()

    @Slot()
    def reset(self):
        """Return to the built-in background; imported wallpapers stay in the library."""
        self.select('')

    @Slot(str)
    def remove(self, file):
        if self._busy or self._item(file) is None:
            return
        self._items = [i for i in self._read_index() if i['file'] != file]
        clear_choice = self._current == file
        if clear_choice:
            self._current = ''
        self._error = self._write(index=True, choice=clear_choice)
        self._broken.discard(file)
        # The displayed copy may still be open in a player; delete after it has been released.
        self._queue_remove(file)
        self.itemsChanged.emit()
        self.changed.emit()

    @Slot(str)
    def reportFailure(self, file):
        """The view could not render ``file``: show the built-in background this session."""
        if file and file == self._current and file not in self._broken:
            self._broken.add(file)
            self._error = '无法显示所选壁纸（文件可能已损坏），已改用默认背景。'
            self.changed.emit()

    @Slot(float)
    def previewDim(self, value):
        """Update dimming in memory for smooth slider dragging without locking disk on each frame."""
        if math.isfinite(value):
            clamped = min(0.9, max(0.0, value))
            if abs(clamped - self._dim) > 1e-4:
                self._dim = clamped
                self.changed.emit()

    @Slot(float)
    def setDim(self, value):
        if math.isfinite(value):
            previous, self._dim = self._dim, min(0.9, max(0.0, value))
            self._error = self._write(choice=True)
            if self._error:
                self._dim = previous
            self.changed.emit()

    # ---------------------------------------------------------------- cleanup

    def _queue_remove(self, file):
        for path in (self.folder / file, self.folder / _thumb_name(file)):
            self._discard[path] = 20
        self._sweeper.start()

    def _remove_discarded(self):
        for path, tries in tuple(self._discard.items()):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                if tries > 1:
                    self._discard[path] = tries - 1
                    continue
            del self._discard[path]
        if self._discard and not self._stop.is_set():
            self._sweeper.start()

    def shutdown(self):
        self._stop.set()
        if self._probe is not None:
            self._video_error()
        elif self._pending is not None:
            self._queue_remove(self._pending['file'])
        self._remove_discarded()
