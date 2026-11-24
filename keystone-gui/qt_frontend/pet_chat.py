"""Lightweight pet chat: a bounded Chat-mode conversation behind the desktop pet bubble.

The pet talks through the same :func:`keystone-gui conversation.run` pipeline as the
main window's Chat mode, but with tools disabled (``chat_mcp=({}, None)`` +
``tools_enabled=False``): no approvals, no Work handoff, just conversation. History
is kept small and private to the pet; the persona and greeting lines come from the
current pack's ``lines.json`` when present (see ``pets/mingyue``).
"""
import json
import random
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Property, Signal, Slot

from keystone_client import ChatMessage, GenerationCancelled
from keystone.secrets import redact

from .pets import BUILTIN_ROOT

HISTORY_LIMIT = 16
DEFAULT_PERSONA = (
    '你是 Keystone 的桌面宠物助手。说话简短友好：1～3 句口语中文，不用 Markdown。'
    '需要操作电脑、读写文件、运行代码的正经任务，提醒主人打开 Keystone 主窗口处理，自己不假装执行。'
    '不知道就说不知道，不编造。'
)


def greet_bucket(hour):
    if 5 <= hour < 8:
        return 'morning'
    if 8 <= hour < 11:
        return 'forenoon'
    if 11 <= hour < 13:
        return 'noon'
    if 13 <= hour < 17:
        return 'afternoon'
    if 17 <= hour < 20:
        return 'evening'
    return 'night'


class PetChat(QObject):
    """One small conversation owned by the pet window; thread events follow the
    same queued-signal pattern as ChatController so QML only reads GUI-thread state."""

    changed = Signal()
    _chunked = Signal(int, str)
    _restart = Signal(int)
    _done = Signal(int, str, str)

    def __init__(self, controller, pets, parent=None, *, clock=time.localtime):
        super().__init__(parent)
        self._controller = controller
        self._pets = pets
        self._clock = clock
        self._history = []
        self._busy = False
        self._reply = ''
        self._error = ''
        self._greeting = ''
        self._cancel = threading.Event()
        self._generation = 0
        self._chunked.connect(self._on_chunk)
        self._restart.connect(self._on_restart)
        self._done.connect(self._on_done)
        self.refreshGreeting()

    # ------------------------------------------------------------- properties

    busy = Property(bool, lambda self: self._busy, notify=changed)
    reply = Property(str, lambda self: self._reply, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)
    greeting = Property(str, lambda self: self._greeting, notify=changed)
    connected = Property(bool, lambda self: bool(self._controller.connected) and not self._controller.demo,
                         notify=changed)

    # ---------------------------------------------------------------- persona

    def _pack_folder(self):
        pack_id = self._pets.currentId
        if not pack_id:
            return None
        user = Path(self._pets.folder) / pack_id
        if (user / 'pet.json').is_file():
            return user
        builtin = BUILTIN_ROOT / pack_id
        return builtin if builtin.is_dir() else None

    def _lines(self):
        folder = self._pack_folder()
        if folder is None:
            return {}
        try:
            data = json.loads((folder / 'lines.json').read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @Slot()
    def refreshGreeting(self):
        lines = self._lines()
        greetings = lines.get('greetings')
        pool = []
        if isinstance(greetings, dict):
            pool = [s for s in greetings.get(greet_bucket(self._clock().tm_hour), []) if isinstance(s, str)]
        if not pool:
            pool = [s for s in lines.get('idle', []) if isinstance(s, str)]
        self._greeting = random.choice(pool) if pool else '你好呀，想聊点什么？'
        self.changed.emit()

    # ------------------------------------------------------------------- chat

    @Slot(str)
    def send(self, text):
        text = (text or '').strip()
        if not text or self._busy:
            return
        if self._controller.demo:
            self._error = '预览模式不聊天。请用不带 --demo 的启动入口连接真实网关。'
            self.changed.emit()
            return
        if not self._controller.connected:
            self._error = '网关尚未连接。请先在主窗口连接模型网关，再来找溟月聊天。'
            self.changed.emit()
            return
        self._error = ''
        self._reply = ''
        self._busy = True
        self._cancel = threading.Event()
        self._generation += 1
        generation = self._generation
        if len(self._history) >= HISTORY_LIMIT:
            self._history = self._history[-(HISTORY_LIMIT - 2):]
            while self._history and self._history[0].role != 'user':
                self._history.pop(0)
        self._history.append(ChatMessage('user', text, user_text=text))
        self.changed.emit()
        client = self._controller._client or self._controller.factory(self._controller.gateway)
        model = self._controller.model
        persona = self._lines().get('persona') or DEFAULT_PERSONA
        if not isinstance(persona, str) or not persona.strip():
            persona = DEFAULT_PERSONA
        cancel = self._cancel
        history = self._history

        def worker():
            try:
                from .conversation import run
                result = run(client, history, model=model, effort=None, cancel=cancel, authorize=None,
                             chunk=lambda piece: self._chunked.emit(generation, piece),
                             checkpoint=lambda _h: None, status=lambda _s: None,
                             next_round=lambda: self._restart.emit(generation),
                             system_context=persona, chat_mcp=({}, None), tools_enabled=False,
                             enter_work=None, max_turns=2, temperature=0.8)
                self._done.emit(generation, result.text or '', '')
            except GenerationCancelled:
                self._done.emit(generation, '', '')
            except Exception as exc:
                self._done.emit(generation, '', str(redact(str(exc))))

        threading.Thread(target=worker, daemon=True, name='Keystone-PetChat').start()

    @Slot()
    def stop(self):
        self._cancel.set()

    @Slot()
    def clear(self):
        if self._busy:
            return
        self._history = []
        self._reply = ''
        self._error = ''
        self.refreshGreeting()

    @Slot()
    def shutdown(self):
        self._cancel.set()

    # --------------------------------------------------------- worker events

    def _on_chunk(self, generation, piece):
        if generation == self._generation and self._busy:
            self._reply += piece
            self.changed.emit()

    def _on_restart(self, generation):
        if generation == self._generation and self._busy:
            self._reply = ''
            self.changed.emit()

    def _on_done(self, generation, text, error):
        if generation != self._generation:
            return
        self._busy = False
        self._error = error
        final = text or self._reply
        self._reply = final
        if final:
            self._history.append(ChatMessage('assistant', final))
        self.changed.emit()
