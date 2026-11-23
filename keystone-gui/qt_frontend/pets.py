"""Desktop pets: pet packs, selection, persistence and the agent-state mood.

A pet pack is a folder holding ``pet.json`` plus its art::

    {
      "format": 1,
      "name": "像素小蟹",
      "size": 96,                      # display size in px (32-256), or [width, height]
      "smooth": true,                  # false keeps pixel art crisp when scaled
      "facing": "right",               # which way the art faces; mirrored when walking the other way
      "states": {
        "idle": "idle.svg",            # required
        "walk": {"sheet": "walk.png", "frames": 4, "fps": 8, "columns": 4},
        "sleep": "sleep.gif",          # GIFs play as they are
        "think": "think.png", "happy": "happy.png", "worried": "worried.png"
      }
    }

Only ``idle`` is required; a missing state shows the idle art. Built-in packs ship
in ``qt_frontend/pets/<id>``. User packs live in ``<home>/gui/qt/pets/<id>``; a user
pack with a built-in id replaces that pet's art, and a broken one falls back to
the built-in art. The selection, user-chosen names and window position live under
the ``"pets"`` key of the desktop settings file (``keystone.compat.desktop_config``).
"""

from pathlib import Path
import re
import time

from PySide6.QtCore import QLockFile, QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImageReader

from keystone.compat import desktop_config

from .store import SessionStore

BUILTIN_ROOT = Path(__file__).resolve().parent / 'pets'
DEFAULT_PET = 'crab'
STATES = ('idle', 'walk', 'sleep', 'think', 'happy', 'worried')
AMBIENT = ('idle', 'walk', 'sleep')
MOODS = ('think', 'happy', 'worried')
PACK_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
ART_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.svg', '.gif'}
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ART_BYTES = 8 * 1024 * 1024
MAX_ART_SIDE = 4096
MAX_NAME = 24
SIZE_RANGE = (32, 256)
HAPPY_MS = 4000
WORRIED_MS = 8000
COORD_LIMIT = 100_000


class PackError(ValueError):
    """A pet pack that cannot be shown; the message is user-facing (Chinese)."""


def _size(value):
    if type(value) is int:
        value = [value, value]
    if (not isinstance(value, list) or len(value) != 2
            or not all(type(v) is int and SIZE_RANGE[0] <= v <= SIZE_RANGE[1] for v in value)):
        raise PackError(f'size 需为 {SIZE_RANGE[0]}–{SIZE_RANGE[1]} 之间的整数或 [宽, 高]')
    return value


def _art(folder, name, state):
    if not isinstance(name, str) or not name or len(name) > 128:
        raise PackError(f'状态 {state} 的图片文件名无效')
    path = Path(name)
    if path.is_absolute() or path.drive or '..' in path.parts:
        raise PackError(f'状态 {state} 的图片必须放在宠物文件夹内')
    path = (folder / path).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise PackError(f'状态 {state} 的图片必须放在宠物文件夹内')
    if path.suffix.lower() not in ART_SUFFIXES:
        raise PackError(f'状态 {state} 的图片格式不受支持（{path.suffix or "无扩展名"}）')
    try:
        if not path.is_file():
            raise PackError(f'找不到状态 {state} 的图片 {name}')
        if path.stat().st_size > MAX_ART_BYTES:
            raise PackError(f'状态 {state} 的图片超过 8 MB')
    except OSError as exc:
        raise PackError(f'无法读取状态 {state} 的图片 {name}') from exc
    reader = QImageReader(str(path))
    size = reader.size()
    if not reader.canRead() or (size.isValid() and max(size.width(), size.height()) > MAX_ART_SIDE):
        raise PackError(f'状态 {state} 的图片无法解码或尺寸过大')
    return path


def _state(folder, state, value):
    if isinstance(value, str):
        path = _art(folder, value, state)
        kind = 'animated' if path.suffix.lower() == '.gif' else 'image'
        return {'source': QUrl.fromLocalFile(str(path)).toString(), 'kind': kind,
                'frames': 1, 'fps': 1, 'columns': 1}
    if not isinstance(value, dict):
        raise PackError(f'状态 {state} 需为图片文件名或精灵图对象')
    path = _art(folder, value.get('sheet'), state)
    if path.suffix.lower() == '.gif':
        raise PackError(f'状态 {state} 的精灵图不能是 GIF')
    frames, fps = value.get('frames'), value.get('fps', 8)
    columns = value.get('columns', frames)
    if type(frames) is not int or not 1 <= frames <= 64:
        raise PackError(f'状态 {state} 的 frames 需为 1–64')
    if type(fps) is not int or not 1 <= fps <= 30:
        raise PackError(f'状态 {state} 的 fps 需为 1–30')
    if type(columns) is not int or not 1 <= columns <= frames:
        raise PackError(f'状态 {state} 的 columns 需为 1–frames')
    return {'source': QUrl.fromLocalFile(str(path)).toString(), 'kind': 'sheet',
            'frames': frames, 'fps': fps, 'columns': columns}


def load_pack(folder, *, builtin=False):
    """Validate ``folder/pet.json`` and return the pack; raises PackError."""
    folder = Path(folder)
    pack_id = folder.name
    if not PACK_ID.fullmatch(pack_id):
        raise PackError('文件夹名只能用小写字母、数字、- 和 _')
    manifest = folder / 'pet.json'
    try:
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise PackError('pet.json 过大')
    except OSError as exc:
        raise PackError('缺少 pet.json') from exc
    data = SessionStore.read(manifest, None)
    if not isinstance(data, dict):
        raise PackError('pet.json 不是有效的 JSON 对象')
    if data.get('format', 1) != 1:
        raise PackError('不支持的 pet.json 格式版本')
    name = data.get('name', pack_id)
    if not isinstance(name, str) or not name.strip():
        raise PackError('name 需为非空文字')
    width, height = _size(data.get('size', 96))
    states = data.get('states')
    if not isinstance(states, dict) or 'idle' not in states:
        raise PackError('states 必须至少包含 idle')
    smooth, facing = data.get('smooth', True), data.get('facing', 'right')
    if type(smooth) is not bool:
        raise PackError('smooth 需为 true 或 false')
    if facing not in ('left', 'right'):
        raise PackError('facing 需为 "left" 或 "right"')
    unknown = sorted(set(states) - set(STATES))
    if unknown:
        raise PackError('未知的状态：' + '、'.join(map(str, unknown)))
    return {'id': pack_id, 'name': name.strip()[:MAX_NAME], 'builtin': builtin,
            'folder': str(folder), 'width': width, 'height': height, 'smooth': smooth, 'facing': facing,
            'states': {state: _state(folder, state, value) for state, value in states.items()}}


def art_for(pack, state):
    """The art to show for an animation state; undeclared states use the idle art."""
    return pack['states'].get(state) or pack['states']['idle']


def animation_state(mood, ambient):
    """Map the agent mood and the pet's own ambient activity to an animation state.

    An agent mood (thinking, happy, worried) always wins over what the pet was
    doing on its own (idle, walking, sleeping).
    """
    if mood in MOODS:
        return mood
    return ambient if ambient in AMBIENT else 'idle'


def scan_packs(home):
    """Built-in packs overlaid by valid user packs; returns (packs by id, error messages)."""
    packs, errors = {}, []
    for folder in sorted(BUILTIN_ROOT.iterdir()) if BUILTIN_ROOT.is_dir() else []:
        if folder.is_dir():
            try:
                packs[folder.name] = load_pack(folder, builtin=True)
            except PackError as exc:
                errors.append(f'内置宠物 {folder.name} 无效：{exc}')
    user = Path(home) / 'gui' / 'qt' / 'pets'
    try:
        folders = sorted(p for p in user.iterdir() if p.is_dir() and not p.name.startswith('.'))
    except OSError:
        folders = []
    for folder in folders[:64]:
        try:
            pack = load_pack(folder)
        except PackError as exc:
            fallback = '，已改用内置形象' if folder.name in packs else ''
            errors.append(f'宠物包「{folder.name}」无效：{exc}{fallback}')
            continue
        if folder.name in packs:
            pack['builtin'] = True  # replaced art for a built-in pet
            pack['custom'] = True
        packs[pack['id']] = pack
    return packs, errors


class MoodTracker:
    """Derive the pet's mood from the agent: thinking, a short happy/worried flash, or nothing."""

    def __init__(self):
        self._busy = False
        self._error = ''
        self._flash = ''
        self._until = 0.0

    def update(self, *, busy, error, approvals, stopped, now):
        error = error or ''
        if self._busy and not busy and not error and not stopped:
            self._flash, self._until = 'happy', now + HAPPY_MS / 1000
        if error and error != self._error:
            self._flash, self._until = 'worried', now + WORRIED_MS / 1000
        self._busy, self._error = bool(busy), error
        return self.mood(approvals=approvals, now=now)

    def mood(self, *, approvals, now):
        flash = self._flash if now < self._until else ''
        if approvals or flash == 'worried':
            return 'worried'
        if self._busy:
            return 'think'
        return flash

    def remaining_ms(self, now):
        return max(0, int((self._until - now) * 1000)) if self._flash and now < self._until else 0


class PetController(QObject):
    changed = Signal()
    moodChanged = Signal()

    def __init__(self, home, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.home = Path(home)
        self.folder = self.home / 'gui' / 'qt' / 'pets'
        self._clock = clock
        self._packs, self._errors = {}, []
        self._enabled = False
        self._current = DEFAULT_PET
        self._names = {}
        self._position = None
        self._error = ''
        self._tracker = MoodTracker()
        self._mood = ''
        self._agent = None
        self._expiry = QTimer(self, singleShot=True)
        self._expiry.timeout.connect(self._observe)
        self._load_settings()
        self._scan()

    # ------------------------------------------------------------- settings

    def _load_settings(self):
        settings = SessionStore.read(desktop_config(self.home), {})
        data = settings.get('pets') if isinstance(settings, dict) else None
        data = data if isinstance(data, dict) else {}
        self._enabled = data.get('enabled') is True
        current = data.get('current')
        self._current = current if isinstance(current, str) and PACK_ID.fullmatch(current) else DEFAULT_PET
        names = data.get('names')
        self._names = {k: v.strip()[:MAX_NAME] for k, v in (names.items() if isinstance(names, dict) else ())
                       if isinstance(k, str) and PACK_ID.fullmatch(k) and isinstance(v, str) and v.strip()}
        position = data.get('position')
        if (isinstance(position, list) and len(position) == 2
                and all(type(v) is int and abs(v) <= COORD_LIMIT for v in position)):
            self._position = position

    def _save(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(self.folder / '.lock'))
            if not lock.tryLock(2000):
                return '另一个 Keystone 窗口正在保存宠物设置，请稍后重试。'
            try:
                path = desktop_config(self.home)
                settings = SessionStore.read(path, {})
                settings = settings if isinstance(settings, dict) else {}
                settings['pets'] = {'enabled': self._enabled, 'current': self._current,
                                    'names': self._names, 'position': self._position}
                SessionStore.atomic(path, settings)
            finally:
                lock.unlock()
        except OSError:
            return '无法保存宠物设置。'
        return ''

    def _persist(self):
        error = self._save()
        if error:
            self._error = error
        self.changed.emit()

    def _scan(self):
        self._packs, self._errors = scan_packs(self.home)
        if self._current not in self._packs:
            if self._packs:
                missing = self._current
                self._current = DEFAULT_PET if DEFAULT_PET in self._packs else next(iter(self._packs))
                if missing != DEFAULT_PET:
                    self._errors.append(f'找不到宠物「{missing}」，已改用{self._display(self._current)}。')
        self._error = '\n'.join(self._errors)

    def _display(self, pack_id):
        pack = self._packs.get(pack_id)
        return self._names.get(pack_id) or (pack['name'] if pack else pack_id)

    # ------------------------------------------------------------ properties

    def _current_map(self):
        pack = self._packs.get(self._current)
        if pack is None:
            return {}
        return {'id': pack['id'], 'name': self._display(pack['id']), 'width': pack['width'],
                'height': pack['height'], 'smooth': pack['smooth'], 'facing': pack['facing'], 'states': {s: art_for(pack, s) for s in STATES}}

    def _rows(self):
        return [{'id': pack['id'], 'name': self._display(pack['id']), 'defaultName': pack['name'],
                 'builtin': pack['builtin'], 'custom': bool(pack.get('custom')),
                 'preview': pack['states']['idle']['source'], 'kind': pack['states']['idle']['kind'],
                 'frames': pack['states']['idle']['frames'], 'columns': pack['states']['idle']['columns']}
                for pack in sorted(self._packs.values(), key=lambda p: (not p['builtin'], p['id']))]

    enabled = Property(bool, lambda s: s._enabled and bool(s._packs), notify=changed)
    currentId = Property(str, lambda s: s._current if s._current in s._packs else '', notify=changed)
    current = Property('QVariantMap', _current_map, notify=changed)
    packs = Property('QVariantList', _rows, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)
    hasPosition = Property(bool, lambda s: s._position is not None, notify=changed)
    posX = Property(int, lambda s: s._position[0] if s._position else 0, notify=changed)
    posY = Property(int, lambda s: s._position[1] if s._position else 0, notify=changed)
    folderPath = Property(str, lambda s: str(s.folder), constant=True)
    mood = Property(str, lambda s: s._mood, notify=moodChanged)

    # ----------------------------------------------------------------- slots

    @Slot(bool)
    def setEnabled(self, value):
        if self._enabled != bool(value):
            self._enabled = bool(value)
            self._persist()

    @Slot()
    def toggle(self):
        self.setEnabled(not self._enabled)

    @Slot(str)
    def select(self, pack_id):
        if pack_id in self._packs and pack_id != self._current:
            self._current = pack_id
            self._persist()

    @Slot(str, str)
    def rename(self, pack_id, name):
        if pack_id not in self._packs:
            return
        name = (name or '').strip()[:MAX_NAME]
        if name and name != self._packs[pack_id]['name']:
            self._names[pack_id] = name
        else:
            self._names.pop(pack_id, None)
        self._persist()

    @Slot(int, int)
    def savePosition(self, x, y):
        position = [max(-COORD_LIMIT, min(COORD_LIMIT, int(x))), max(-COORD_LIMIT, min(COORD_LIMIT, int(y)))]
        if position != self._position:
            self._position = position
            error = self._save()
            if error:
                self._error = error
                self.changed.emit()

    @Slot(result=QUrl)
    def packFolder(self):
        """The user pack folder, created on demand so it can be opened in the file manager."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return QUrl.fromLocalFile(str(self.folder))

    @Slot()
    def reload(self):
        self._scan()
        self.changed.emit()

    @Slot(str, str, result=str)
    def animationState(self, mood, ambient):
        return animation_state(mood, ambient)

    # ------------------------------------------------------------ agent mood

    def bind(self, controller, workbench=None):
        """Follow the chat controller (busy / error / status) and pending approvals."""
        self._agent = (controller, workbench)
        controller.changed.connect(self._observe)
        if workbench is not None:
            workbench.changed.connect(self._observe)
        self._observe()

    def observe(self, *, busy, error, approvals, stopped=False):
        now = self._clock()
        self._set_mood(self._tracker.update(busy=busy, error=error, approvals=approvals,
                                            stopped=stopped, now=now))
        remaining = self._tracker.remaining_ms(now)
        if remaining:
            self._expiry.start(remaining + 20)

    @Slot()
    def _observe(self):
        if self._agent is None:
            self._set_mood(self._tracker.mood(approvals=0, now=self._clock()))
            return
        controller, workbench = self._agent
        approvals = len(workbench.property('approvals') or []) if workbench is not None else 0
        self.observe(busy=bool(controller.property('busy')), error=controller.property('error') or '',
                     approvals=approvals, stopped=controller.property('status') == '已停止')

    def _set_mood(self, mood):
        if mood != self._mood:
            self._mood = mood
            self.moodChanged.emit()
