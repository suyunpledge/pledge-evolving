"""Local wallpaper import, validation and persistence for the Qt workbench."""

import math
from pathlib import Path
import re
import threading
import uuid

from PySide6.QtCore import QLockFile, QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImageReader
from PySide6.QtMultimedia import QMediaPlayer

from .store import SessionStore

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}
VIDEO_SUFFIXES = {'.mp4', '.webm', '.mov', '.m4v'}
DEFAULT_DIM = 0.52


class Wallpaper(QObject):
    changed = Signal()
    _copied = Signal(object, str)

    def __init__(self, home, parent=None):
        super().__init__(parent)
        self.folder = Path(home) / 'gui' / 'qt' / 'wallpapers'
        self.path = self.folder.parent / 'wallpaper.json'
        self._record = {}
        self._dim = DEFAULT_DIM
        self._busy = False
        self._error = ''
        self._pending = None
        self._probe = None
        self._discard = set()
        self._stop = threading.Event()
        self._copied.connect(self._finish_copy, Qt.ConnectionType.QueuedConnection)
        self._deadline = QTimer(self, singleShot=True, interval=12000)
        self._deadline.timeout.connect(self._video_error)
        record = SessionStore.read(self.path, {})
        if isinstance(record, dict):
            dim = record.get('dim')
            if type(dim) in (int, float) and 0 <= dim <= 0.9:
                self._dim = float(dim)
            name, kind = record.get('file'), record.get('kind')
            if (isinstance(name, str) and re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]+', name)
                    and kind in ('image', 'gif', 'video')):
                if (self.folder / name).is_file():
                    label = record.get('name')
                    self._record = {'file': name, 'kind': kind,
                                    'name': label[:120] if isinstance(label, str) else name}
                else:
                    self._error = '找不到已导入的壁纸。'

    source = Property(QUrl, lambda s: QUrl.fromLocalFile(str(s.folder / s._record['file'])) if s._record else QUrl(), notify=changed)
    kind = Property(str, lambda s: s._record.get('kind', ''), notify=changed)
    name = Property(str, lambda s: s._record.get('name', ''), notify=changed)
    dim = Property(float, lambda s: s._dim, notify=changed)
    busy = Property(bool, lambda s: s._busy, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)

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
        threading.Thread(target=self._copy, args=(source, record), name='Keystone-wallpaper-import', daemon=True).start()

    def _copy(self, source, record):
        target = self.folder / record['file']
        partial = target.with_suffix(target.suffix + '.part')
        message = ''
        try:
            if not source.is_file():
                raise ValueError('无法读取选中的壁纸文件。')
            self.folder.mkdir(parents=True, exist_ok=True)
            limit = (1024 if record['kind'] == 'video' else 80) * 1024 * 1024
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
                reader = QImageReader(str(partial))
                try:
                    detected = bytes(reader.format())
                    allowed = {b'gif'} if record['kind'] == 'gif' else {b'png', b'jpeg', b'webp', b'bmp'}
                    if not reader.canRead() or detected not in allowed:
                        raise ValueError('无法读取该图片，或文件内容与所选类型不符。')
                    dimensions = reader.size()
                    if not dimensions.isValid() or dimensions.width() * dimensions.height() > 64_000_000:
                        raise ValueError('壁纸图片太大，请缩小分辨率后重试。')
                    if reader.read().isNull():
                        raise ValueError('无法解码选中的壁纸图片。')
                finally:
                    del reader
            partial.rename(target)
        except (OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else '无法导入文件。请检查文件和可用磁盘空间。'
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        if self._stop.is_set():
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            self._copied.emit(record, message)

    def _finish_copy(self, record, error):
        if self._stop.is_set():
            return
        if error or record['kind'] != 'video':
            self._finish_import(error)
            return
        self._probe = QMediaPlayer(self)
        self._probe.mediaStatusChanged.connect(self._video_status)
        self._probe.errorOccurred.connect(self._video_error)
        self._deadline.start()
        self._probe.setSource(QUrl.fromLocalFile(str(self.folder / record['file'])))

    def _video_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._finish_import('' if self._probe.hasVideo() else '文件中没有可播放的视频轨道。')
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._video_error()

    def _video_error(self, *_):
        if self._probe is not None:
            self._finish_import('无法读取该视频。可尝试使用 H.264 编码的 MP4。')

    def _finish_import(self, error):
        self._deadline.stop()
        if self._probe is not None:
            probe, self._probe = self._probe, None
            probe.mediaStatusChanged.disconnect(self._video_status)
            probe.errorOccurred.disconnect(self._video_error)
            probe.setSource(QUrl())
            probe.deleteLater()
        record, self._pending = self._pending, None
        if not error:
            error = self._apply(record, self._dim)
        if error:
            self._queue_remove(record)
        self._error, self._busy = error, False
        self.changed.emit()

    def _apply(self, record, dim):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(self.path) + '.lock')
            if not lock.tryLock(0):
                return '另一个 Keystone 窗口正在保存壁纸。'
            try:
                previous = SessionStore.read(self.path, {})
                if record == self._record and record:
                    if not isinstance(previous, dict) or previous.get('file') != record['file']:
                        return '壁纸已在另一个窗口更换，请重新导入。'
                SessionStore.atomic(self.path, {**record, 'dim': dim})
            finally:
                lock.unlock()
        except OSError:
            return '无法保存壁纸设置。'
        old = previous.get('file') if isinstance(previous, dict) else None
        if isinstance(old, str) and old != record.get('file') and re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]+', old):
            self._queue_remove({'file': old})
        self._record, self._dim = record, dim
        return ''

    def _queue_remove(self, record):
        if record:
            self._discard.add(self.folder / record['file'])
            QTimer.singleShot(500, self, self._remove_discarded)

    def _remove_discarded(self):
        for path in tuple(self._discard):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._discard.discard(path)

    @Slot(float)
    def setDim(self, value):
        if self._record and math.isfinite(value):
            self._error = self._apply(self._record, min(0.9, max(0.0, value)))
            self.changed.emit()

    @Slot()
    def reset(self):
        if not self._busy:
            self._error = self._apply({}, DEFAULT_DIM)
            self.changed.emit()

    def shutdown(self):
        self._stop.set()
        if self._probe is not None:
            self._video_error()
        elif self._pending is not None:
            self._queue_remove(self._pending)
        self._remove_discarded()
