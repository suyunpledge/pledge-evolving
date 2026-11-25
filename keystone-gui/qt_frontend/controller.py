"""Qt model/controller boundary; reuse Keystone's real transport and secret scope."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QPointF, Property, QRectF, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication, QDesktopServices, QTextBlockFormat, QTextCursor, QTransform

from keystone_client import ChatMessage, KeystoneGatewayClient, GenerationCancelled
from interaction_model import ATTACHMENT_HEADER, MAX_CONTEXT_CHARS, compose_prompt, read_attachment
from keystone.secrets import redact
from .conversation import preview_receipt
from .store import SessionStore
from .tool_settings import WorkToolSettings
from .planning_settings import PlanningSettings, make_plan
from phase_client import planning_selection
from keystone.compat import desktop_config


class RowsModel(QAbstractListModel):
    NAMES = ("sessionId", "title", "subtitle", "roleName", "textContent", "state", "stamp", "changeKey", "changeInfo",
             "historyIndex", "toolReceipts", "continuable", "steps", "todos", "contemplation")

    def __init__(self, rows=(), parent=None):
        super().__init__(parent)
        self.rows = list(rows)

    def roleNames(self):
        return {int(Qt.ItemDataRole.UserRole) + i: name.encode() for i, name in enumerate(self.NAMES)}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role):
        if index.isValid() and 0 <= index.row() < len(self.rows):
            i = int(role) - int(Qt.ItemDataRole.UserRole)
            if 0 <= i < len(self.NAMES):
                name = self.NAMES[i]
                if name in ("toolReceipts", "steps", "todos"):
                    return self.rows[index.row()].get(name, [])
                if name == "contemplation":
                    return self.rows[index.row()].get(name) or {}
                if name == "continuable":
                    return bool(self.rows[index.row()].get(name, False))
                return self.rows[index.row()].get(name, -1 if name == "historyIndex" else "")
        return None

    def reset(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def append(self, row):
        n = len(self.rows)
        self.beginInsertRows(QModelIndex(), n, n)
        self.rows.append(row)
        self.endInsertRows()

    def update(self, row, **values):
        self.rows[row].update(values)
        roles = [int(Qt.ItemDataRole.UserRole) + self.NAMES.index(key) for key in values]
        self.dataChanged.emit(self.index(row), self.index(row), roles)


class ChatController(QObject):
    changed = Signal()
    draftChanged = Signal()
    streamAdvanced = Signal()
    focusComposer = Signal()
    navigateRequested = Signal(str)
    browseRequested = Signal(str)
    modelRequested = Signal(str)
    chartPreviewRequested = Signal(QUrl, str)
    commandRequested = Signal(str, str)
    _workerEvent = Signal(str, object)

    def __init__(self, home, *, demo=False, client_factory=KeystoneGatewayClient, parent=None):
        super().__init__(parent)
        self.store = SessionStore(home)
        self.demo = demo
        self.factory = client_factory
        self.work_context = None
        self.work_environment = None
        self._code_highlighters = {}
        self._prefs = {} if demo else self.store.preferences()
        self._sessions = RowsModel(parent=self)
        self._messages = RowsModel(parent=self)
        self._history = []
        self._sid = "s" + uuid.uuid4().hex
        self._title = "新对话"
        self._draft = ""
        self._busy = False
        self._status = "预览 · 不发送请求" if demo else "正在检查网关…"
        self._connected = False
        self._error = ""
        self._planning = PlanningSettings(self.store.home, self)
        self._planning.failed.connect(self._planning_error)
        self._planning.refresh()
        self._attachments = []
        self._temperature = 0.7
        self._models = ["default"]
        self._managed_models = None
        self._pending_model = ''
        self._switching_model = False
        self._model = str(self._prefs.get("model", "default"))
        self._reasoning = str(self._prefs.get("reasoning", "medium"))
        self._effort_cache = {}
        desktop = {} if demo else self.store.read(desktop_config(Path(home)), {})
        port = desktop.get('gateway_port', 8799) if isinstance(desktop, dict) else 8799
        port = port if isinstance(port, int) and 1024 <= port <= 65535 else 8799
        self._gateway = str(self._prefs.get("gateway", os.environ.get("KEYSTONE_GATEWAY_URL", f"http://127.0.0.1:{port}")))
        self._material = float(self._prefs.get("material", 0.75))
        # KEYSTONE_REDUCE_MOTION (shared with the Tk shell) wins over the saved preference when set.
        forced = os.environ.get("KEYSTONE_REDUCE_MOTION", "").strip()
        self._reduce_motion = (forced != "0") if forced else bool(self._prefs.get("reduceMotion", False))
        self._solid = bool(self._prefs.get("solid", False))
        self._cancel = threading.Event()
        self._generation = 0
        self._connection_epoch = 0
        self._closed = False
        self.tool_authorizer = None
        # 返回 Work 回合上限的可调用对象（由 app 按 Keystone home 接入）；None 用默认值。
        self.turn_limit = None
        self.tool_filter = None
        self.mcp_tools = None
        self.chat_mcp_tools = None
        self._conversation_mode = 'work'
        self._tool_settings = WorkToolSettings(self)
        from .role_templates import RoleTemplates
        self._templates = RoleTemplates(self, home)
        from .changes import ReplyChanges
        self._changes = ReplyChanges(self)
        self._client = None
        self._workerEvent.connect(self._on_worker, Qt.ConnectionType.QueuedConnection)
        self._pending_chunks = []
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(32)
        self._stream_timer.timeout.connect(self._flush_stream)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(650)
        self._draft_timer.timeout.connect(self._save_draft)
        self._refresh_sessions()
        if demo:
            self._load_demo()

    sessions = Property(QObject, lambda self: self._sessions, constant=True)
    messages = Property(QObject, lambda self: self._messages, constant=True)
    sessionId = Property(str, lambda self: self._sid, notify=changed)
    title = Property(str, lambda self: self._title, notify=changed)
    busy = Property(bool, lambda self: self._busy or self._changes.busy, notify=changed)
    changeReview = Property(QObject, lambda self: self._changes, constant=True)
    toolSettings = Property(QObject, lambda self: self._tool_settings, constant=True)
    templates = Property(QObject, lambda self: self._templates, constant=True)
    conversationMode = Property(str, lambda self: self._conversation_mode, notify=changed)
    connected = Property(bool, lambda self: self._connected, notify=changed)
    canRetry = Property(bool, lambda self: self.connected and not self.busy and bool(self._history)
                        and self._history[-1].role == 'user', notify=changed)
    status = Property(str, lambda self: self._status, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)
    model = Property(str, lambda self: self._model, notify=changed)
    models = Property("QStringList", lambda self: self._models, notify=changed)
    modelSwitching = Property(bool, lambda self: self._switching_model, notify=changed)
    reasoning = Property(str, lambda self: self._reasoning, notify=changed)
    planning = Property(QObject, lambda self: self._planning, constant=True)

    def _planning_error(self, error):
        self._error = str(redact(error))
        self.changed.emit()

    # 思考强度随模型变化：只给当前模型可用的档位；所选档位不可用时按不超过它的最高档发送。
    reasoningLevels = Property("QStringList", lambda self: self._effort_info()['levels'], notify=changed)
    reasoningSupported = Property(bool, lambda self: self._effort_info()['supported'], notify=changed)
    reasoningHint = Property(str, lambda self: self._effort_info()['hint'], notify=changed)
    effectiveReasoning = Property(str, lambda self: self._effective_reasoning(), notify=changed)
    gateway = Property(str, lambda self: self._gateway, notify=changed)
    material = Property(float, lambda self: self._material, notify=changed)
    reduceMotion = Property(bool, lambda self: self._reduce_motion, notify=changed)
    solid = Property(bool, lambda self: self._solid, notify=changed)
    attachments = Property("QStringList", lambda self: [Path(x["path"]).name for x in self._attachments], notify=changed)
    isDemo = Property(bool, lambda self: self.demo, constant=True)

    @Property(str, notify=draftChanged)
    def draft(self):
        return self._draft

    @draft.setter
    def draft(self, value):
        if self._draft != value:
            self._draft = value
            self.draftChanged.emit()
            self._draft_timer.start()

    def _save_draft(self):
        if not self.demo:
            try:
                self.store.save_draft(self._sid, self._draft)
            except OSError:
                self._error = "草稿暂时无法保存，请保留窗口并重试。"
                self.changed.emit()

    def _save_preferences(self):
        if self.demo:
            return
        try:
            self.store.save_preferences({"model": self._model, "reasoning": self._reasoning,
                "gateway": self._gateway, "material": self._material,
                "reduceMotion": self._reduce_motion, "solid": self._solid})
        except OSError:
            self._error = "设置暂时无法保存。"
            self.changed.emit()

    def _refresh_sessions(self):
        if self.demo:
            return
        rows = self.store.sessions()
        self._sessions.reset([{"sessionId": str(s["id"]), "title": str(s.get("title", "对话")),
            "subtitle": time.strftime("%m月%d日", time.localtime(float(s.get("updated", 0))))} for s in rows])

    def _persist(self):
        if self.demo or not (self._history or self._changes.entries):
            return
        try:
            self._write_session(self._history, self._title, self._changes.entries)
            self._refresh_sessions()
        except OSError:
            self._error = "对话暂时无法保存，请保留窗口。"

    def _write_session(self, history, title, changes):
        self.store.save_session({"id": self._sid, "title": title, "updated": time.time(),
                                 "changes": self._changes.saved(changes),
                                 "messages": [m.to_session_dict() for m in history]})

    @Slot()
    def newSession(self):
        if self.busy:
            return
        self._save_draft()
        self._persist()
        self._sid = "s" + uuid.uuid4().hex
        self._title, self._draft, self._error = "新对话", "", ""
        self._history, self._attachments = [], []
        self._changes.entries = {}
        self._client = None
        self._messages.reset([])
        self.changed.emit()
        self.draftChanged.emit()
        self.focusComposer.emit()

    @Slot(str)
    def selectSession(self, sid):
        if self.busy or sid == self._sid:
            return
        if self.demo:
            row = next((r for r in self._sessions.rows if r["sessionId"] == sid), None)
            if row:
                self._sid, self._title = sid, row["title"]
                self.changed.emit()
            return
        row = next((r for r in self.store.sessions() if str(r["id"]) == sid), None)
        if row is None:
            return
        self._save_draft()
        self._persist()
        self._sid, self._title = sid, str(row.get("title", "对话"))
        self._changes.entries = row.get('changes', {}) if isinstance(row.get('changes'), dict) else {}
        self._history = [ChatMessage.from_session_dict(m)
            for m in row["messages"] if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
            and isinstance(m.get("content"), str)]
        self._messages.reset(self._transcript_rows())
        self._draft = self.store.draft(sid)
        self._attachments, self._error = [], ""
        self._client = None
        self.changed.emit()
        self.draftChanged.emit()
        self.focusComposer.emit()

    @Slot(str)
    def renameSession(self, title):
        title = title.strip()[:100]
        if title and not self._busy:
            self._title = title
            self._persist()
            self.changed.emit()

    @Slot(str)
    def setModel(self, model):
        if self.busy or self._switching_model or model not in self._models:
            return
        if model in (self._managed_models or ()):
            if model != self._model or not self._connected:
                self._pending_model = model
                self.gatewayChanging()
                self.modelRequested.emit(model)
            return
        self._model = model
        self._save_preferences()
        self.changed.emit()

    @Slot(object)
    def setManagedModels(self, models):
        self._managed_models = list(models)
        self._models = self._managed_models or ["default"]
        self._effort_cache.clear()
        self.changed.emit()

    @Slot()
    def refreshModelChoices(self):
        if self._managed_models is None:
            return
        from config_model import load_user_layer
        from .gateway_service import chat_model_routes, model_rows
        try:
            models = list(chat_model_routes(model_rows(self.store.home)))
        except (OSError, ValueError):
            return
        if models != self._managed_models:
            self.setManagedModels(models)

    def _effort_info(self):
        model = self._model
        if model not in self._effort_cache:
            from . import effort
            info = effort.support(None)
            if self._managed_models is not None and not self.demo:
                from .gateway_service import model_rows
                try:
                    info = effort.for_label(model_rows(self.store.home), model)
                except (OSError, ValueError):
                    pass
            self._effort_cache[model] = info
        return self._effort_cache[model]

    def _effective_reasoning(self):
        from . import effort
        return effort.effective(self._reasoning, self._effort_info()['levels'])

    def _run_command(self, text):
        """斜杠命令：本地处理并清空输入框；识别不了的按普通消息发送。"""
        from . import slash
        roles = [(t.id, t.name) for t in getattr(self._templates, '_templates', [])]
        command = slash.parse(text, models=self._models, roles=roles)
        if command is None:
            return False
        action, argument = command
        if action == 'error':
            self._error = argument
            self.changed.emit()
            return True
        self._draft = ''
        self._error = ''
        self.draftChanged.emit()
        if action == 'new':
            self.newSession()
        elif action == 'model':
            self.setModel(argument)
        elif action == 'effort':
            self.setReasoning(argument)
        elif action == 'role':
            self._templates.select(argument)
        else:
            self.commandRequested.emit(action, argument)
        self.changed.emit()
        return True

    @Slot(str)
    def setReasoning(self, value):
        from . import effort
        if self._busy or value not in effort.LEVELS:
            return
        self._reasoning = value
        self._save_preferences()
        self._sync_task_thinking()
        self.changed.emit()

    def _sync_task_thinking(self):
        """沉思档同时打开 Keystone 任务沉思（用户层 thinking.mode），与 Tk 版一致；只在变化时写。"""
        if self.demo:
            return
        from . import effort
        from config_model import load_user_layer, save_user_layer
        mode = effort.task_thinking(self._reasoning)
        try:
            latest = load_user_layer(self.store.home)
            row = next((r for r in latest if r.get('id') == 'thinking'), None)
            current = str(((row or {}).get('config') or {}).get('mode', 'off')).lower()
            if current == mode:
                return
            rows = copy.deepcopy(latest)
            row = next((r for r in rows if r.get('id') == 'thinking'), None)
            if row is None:
                rows.append({'id': 'thinking', 'name': 'thinking:mode', 'config': {'mode': mode}})
            else:
                row.setdefault('config', {})['mode'] = mode
            save_user_layer(self.store.home, rows, expected_rows=latest)
        except (OSError, ValueError):
            self._error = "沉思模式未能写入配置，本次对话仍按所选强度发送。"

    @Slot(float)
    def setMaterial(self, value):
        self._material = min(1.0, max(0.0, value))
        self._save_preferences()
        self.changed.emit()

    @Slot(bool)
    def setReduceMotion(self, value):
        self._reduce_motion = value
        self._save_preferences()
        self.changed.emit()

    @Slot(bool)
    def setSolid(self, value):
        self._solid = value
        self._save_preferences()
        self.changed.emit()

    @Slot(str)
    def setGateway(self, value):
        if self.busy or self._switching_model:
            return
        self.setManagedGateway(value, None)

    @Slot(str, object)
    def setManagedGateway(self, value, details):
        parsed = urlsplit(value.strip())
        if self._busy:
            return
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            self._error = "请输入不含账号密码的 HTTP / HTTPS 网关地址。"
            self.changed.emit()
            return
        model = details.get("model") if isinstance(details, dict) else ""
        if self._pending_model and model != self._pending_model:
            return
        if details is None:
            self._managed_models = None
        if model:
            self._model = model
        temperature = details.get("temperature") if isinstance(details, dict) else details
        self._pending_model = ''
        self._switching_model = False
        self._gateway = value.strip().rstrip("/")
        self._temperature = 0.7 if temperature is None else temperature
        self._save_preferences()
        self.refreshConnection()

    @Slot()
    def gatewayChanging(self):
        self._switching_model = True
        self._connection_epoch += 1
        self._connected = False
        self._client = None
        self._error = ""
        self._status = f"正在连接 {self._pending_model}…" if self._pending_model else "正在重新启动网关…"
        self.changed.emit()

    @Slot(str, str)
    def gatewayChangeFailed(self, message, active_url):
        if not self._switching_model:
            return
        self._pending_model = ''
        self._switching_model = False
        self._connected = bool(active_url and active_url == self._gateway)
        self._status, self._error = "切换模型失败", message
        self.changed.emit()

    @Slot()
    def refreshConnection(self):
        if self.demo or self._busy or self._switching_model:
            return
        endpoint = self._gateway
        self._connection_epoch += 1
        ticket = self._connection_epoch
        self._connected = False
        self._client = None
        self._status = "正在检查网关…"
        self.changed.emit()
        def worker():
            client = self.factory(endpoint)
            try:
                online, detail = client.health()
                models = []
                if online:
                    models = client.list_models(timeout=5)
                self._workerEvent.emit("connection", (ticket, endpoint, online, models, detail))
            except Exception as exc:
                self._workerEvent.emit("connection", (ticket, endpoint, False, [], str(redact(str(exc)))))
        threading.Thread(target=worker, daemon=True, name="Keystone-Qt-connection").start()

    @Slot(QUrl)
    def addAttachment(self, url):
        if self._busy or not url.isLocalFile():
            return
        try:
            if self._client is None:
                self._client = self.factory(self._gateway)
            item = read_attachment(url.toLocalFile(), secret_scope=self._client.secret_scope)
            self._attachments = [a for a in self._attachments if a["path"] != item["path"]] + [item]
            self._error = ""
        except (OSError, ValueError) as exc:
            self._error = str(redact(str(exc)))
        self.changed.emit()

    @Slot(int)
    def removeAttachment(self, index):
        if not self._busy and 0 <= index < len(self._attachments):
            self._attachments.pop(index)
            self.changed.emit()

    @Slot()
    def send(self):
        text = self._draft.strip()
        if self.busy or not text:
            return
        if self._run_command(text):
            return
        if self.demo:
            self._error = "当前是材质预览。请用不带 --demo 的启动入口连接真实网关。"
            self.changed.emit()
            return
        if not self._connected:
            self._error = "网关尚未连接。请在连接设置中重试，或从完整工作台启动网关。"
            self.changed.emit()
            return
        self._client = self._client or self.factory(self._gateway)
        try:
            prompt = compose_prompt(text, self._attachments, secret_scope=self._client.secret_scope)
            planning = self._planning.take_level(text)
        except (OSError, ValueError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return
        if not self._history:
            self._title = text.splitlines()[0][:36]
        self._history.append(ChatMessage("user", prompt, user_text=text, requested_planning=planning,
                            attachment_names=tuple(Path(a["path"]).name for a in self._attachments)))
        self._messages.append({"roleName": "user", "textContent": text, "state": "complete",
                               "historyIndex": len(self._history) - 1})
        self._draft, self._attachments = "", []
        self.draftChanged.emit()
        self._save_draft()
        self._start_generation()

    @Slot()
    def continueWork(self):
        """Resume a stopped turn at its checkpoint, or extend a completed turn budget."""
        if self.busy:
            return
        marked = [i for i, row in enumerate(self._messages.rows) if row.get('continuable')]
        latest = next((m for m in reversed(self._history) if m.role == 'user'), None)
        if marked and latest and latest.interrupted:
            if self.connected and marked[-1] == len(self._messages.rows) - 1:
                self._start_generation(resuming=True)
            return
        prior, attachments = self._draft, self._attachments
        self._draft, self._attachments = '继续完成剩余工作', []
        self.send()
        if self.busy:
            for index in marked:
                self._messages.update(index, continuable=False)
        # 输入框里原有的草稿与附件不被「继续」吞掉。
        self._draft, self._attachments = prior, attachments
        self.draftChanged.emit()
        self.changed.emit()

    @Slot()
    def retry(self):
        if not self.canRetry:
            return
        self._messages.reset(self._transcript_rows(retrying=True))
        self._start_generation()

    @Slot(str, int, result=bool)
    def withdrawMessage(self, sid, index):
        return self._revise_message(sid, index, None)

    @Slot(str, int, str, result=bool)
    def editMessage(self, sid, index, text):
        return self._revise_message(sid, index, text.strip())

    def _revise_message(self, sid, index, text):
        if (self.demo or self.busy or sid != self._sid or not 0 <= index < len(self._history)
                or self._history[index].role != 'user'):
            return False
        history = self._history[:index]
        if text is not None:
            if not text:
                return False
            if not self._connected:
                self._error = '网关尚未连接，无法重新发送。'
                self.changed.emit()
                return False
            self._client = self._client or self.factory(self._gateway)
            try:
                old = self._history[index]
                prompt = compose_prompt(text, [], secret_scope=self._client.secret_scope)
                if old.attachment_names:
                    # Keep the exact snapshot from this message, not the draft's
                    # attachments or a fresh read of files that may have changed.
                    prefix = old.user_text or ''
                    start = len(prefix) if old.content.startswith(prefix + ATTACHMENT_HEADER) else 0
                    split = old.content.find(ATTACHMENT_HEADER, start)
                    if split < 0:
                        raise ValueError('找不到原消息的附件快照，无法重新发送。')
                    prompt += old.content[split:]
                    if len(prompt) > MAX_CONTEXT_CHARS:
                        raise ValueError('修改后的消息和原附件合计过长。')
                history.append(ChatMessage('user', prompt, user_text=text, requested_planning=self._planning.take_level(text),
                                           attachment_names=old.attachment_names))
            except (OSError, ValueError) as exc:
                self._error = str(redact(str(exc)))
                self.changed.emit()
                return False
        # File changes already happened. Retain their receipts without sending
        # the withdrawn turns or their tool results in subsequent model requests.
        changes = {}
        for key, entry in self._changes.entries.items():
            anchor = entry.get('anchor')
            changes[key] = {**entry, 'anchor': None} if isinstance(anchor, int) and anchor >= index else entry
        title = self._title
        old_first = self._history[0].user_text or self._history[0].content
        if index == 0 and old_first.split('\n', 1)[0][:36] == title:
            title = text.splitlines()[0][:36] if text else '已撤回的对话'
        try:
            # Persist empty histories too, including overrides of imported chats.
            self._write_session(history, title, changes)
        except OSError:
            self._error = '对话暂时无法保存，请重试。'
            self.changed.emit()
            return False
        self._history, self._title, self._changes.entries = history, title, changes
        self._status, self._error = ('消息已撤回', '') if text is None else ('就绪', '')
        self._messages.reset(self._transcript_rows())
        self._refresh_sessions()
        self.changed.emit()
        self.focusComposer.emit()
        if text is not None:
            self._start_generation()
        return True

    def _transcript_rows(self, *, retrying=False):
        rows, receipts, names = [], [], {}
        pending_todos = []

        def flush_receipts():
            nonlocal pending_todos
            if receipts or pending_todos:
                rows.append({'roleName': 'assistant', 'textContent': '', 'state': 'complete',
                             'toolReceipts': receipts.copy(), 'todos': pending_todos})
                pending_todos = []
                receipts.clear()
                names.clear()

        for index, message in enumerate(self._history):
            if message.role == 'user':
                flush_receipts()
                pending_todos = message.todos or []
            if isinstance(message.tool_calls, list):
                for call in message.tool_calls:
                    if isinstance(call, dict) and isinstance(call.get('id'), str):
                        function = call.get('function')
                        if isinstance(function, dict) and isinstance(function.get('name'), str):
                            names[call['id']] = function['name']
            if message.role == 'tool':
                if names.get(message.tool_call_id) in ('update_todos', 'keystone_update_todos'):
                    continue
                try:
                    result = json.loads(message.content)
                except ValueError:
                    result = None
                receipts.append(preview_receipt(names.get(message.tool_call_id, ''), result))
            elif message.content:
                rows.append({'roleName': message.role, 'textContent': message.user_text or message.content,
                             'state': 'complete', 'historyIndex': index, 'steps': message.steps or []})
                if message.role == 'assistant' and not message.tool_calls and receipts:
                    rows[-1]['toolReceipts'] = receipts.copy()
                    receipts.clear()
                    names.clear()
                if message.role == 'assistant' and not message.tool_calls and pending_todos:
                    rows[-1]['todos'], pending_todos = pending_todos, []
            for key, entry in self._changes.entries.items():
                if entry.get('anchor') == index:
                    if message.role != 'assistant' or not message.content:
                        rows.append({'roleName': 'assistant', 'textContent': '', 'state': 'complete',
                                     'toolReceipts': receipts.copy()})
                        receipts.clear()
                    rows[-1].update(changeKey=key, changeInfo=self._changes.card(key))
        if retrying:
            pending_todos = []
        flush_receipts()
        for key, entry in self._changes.entries.items():
            if entry.get('anchor') is None:
                rows.append({'roleName': 'assistant', 'textContent': '已移除对话的文件修改', 'state': 'complete',
                             'changeKey': key, 'changeInfo': self._changes.card(key)})
        latest = next((m for m in reversed(self._history) if m.role == 'user'), None)
        if latest and latest.interrupted and not retrying:
            if not rows or rows[-1]['roleName'] == 'user' or rows[-1].get('changeInfo'):
                rows.append({'roleName': 'assistant', 'textContent': '', 'todos': latest.todos or []})
            rows[-1].update(state='stopped', continuable=True)
        return rows

    def _start_generation(self, *, resuming=False):
        self._changes.begin()
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        cancel = self._cancel
        self._busy, self._error, self._status = True, "", "正在生成…"
        self._turn_start = time.monotonic()
        messages, model, effort = copy.deepcopy(self._history), self._model, self._effective_reasoning()
        task_index = next(i for i in reversed(range(len(messages))) if messages[i].role == 'user')
        steps = []
        if resuming:
            previous = self._messages.rows[-1]
            steps = list(previous.get('steps') or [])
            elapsed = steps[-1]['at'] + max(0, steps[-1]['ms']) if steps else 0
            self._turn_start -= elapsed / 1000
        else:
            elapsed = 0
        steps.append({'phase': 'prepare', 'label': '继续未完成的任务' if resuming else '准备对话和工具',
                      'at': elapsed, 'ms': -1})
        row = {'roleName': 'assistant', 'textContent': '', 'state': 'streaming', 'continuable': False,
               'todos': messages[task_index].todos or [], 'steps': steps[-128:]}
        if resuming:
            self._messages.update(len(self._messages.rows) - 1, **row)
        else:
            for index, prior in enumerate(self._messages.rows):
                if prior.get('continuable'):
                    self._messages.update(index, continuable=False)
            self._messages.append(row)
        self._reply_index = len(self._messages.rows) - 1
        self._reply = ""
        self._persist()
        self.changed.emit()
        self.streamAdvanced.emit()

        def show_todos(items):
            messages[task_index].todos = items
            self._workerEvent.emit('todos', (generation, (task_index, items, messages[task_index].task_plan)))
        from . import effort as effort_levels
        effort = effort_levels.wire_effort(effort, self._effort_info()['levels'])
        client = self._client or self.factory(self._gateway)
        self._client = client
        authorize = self.tool_authorizer() if self.tool_authorizer is not None else None
        tools_enabled, tool_filter = self._tool_settings.snapshot()
        connection = self.mcp_tools() if tools_enabled and self.mcp_tools is not None else None
        chat_mode = self._conversation_mode == 'chat'
        chat_connection = None
        if chat_mode:
            chat_connection = self.chat_mcp_tools() if tools_enabled and self.chat_mcp_tools else ({}, None)
        change_notice = self._changes.notice()
        environment = self.work_environment() if self.work_environment else ''
        temperature = self._temperature
        # 沉思档：回答前先跑沉思轮（套件走插件席位），笔记只进本轮系统上下文，不写入历史。
        contemplating = self._reasoning == 'contemplate' and not self.demo
        task = next((m.content for m in reversed(messages)
                     if m.role == 'user' and isinstance(m.content, str)), '')
        home = self.store.home
        thought = ['']
        session_id = self._sid
        # 角色模板：角色指引随每轮系统上下文走（切换到 Work 后也保留）；工具与权限
        # 的收窄在 tool_settings.snapshot / permissions.authorize 里，回合上限在 turn_limit。
        role = self._templates.active()
        role_text = role.role_text() if role is not None else ''

        def update_status(value):
            self._workerEvent.emit('status', (generation, value))

        def progress(kind, label):
            step = (kind, str(redact(label))[:160], self.turnElapsedMs())
            self._workerEvent.emit('timeline', (generation, step))

        # 角色指引整轮会话不变：走系统上下文（稳定前缀、可缓存）；工作台背景才是逐轮的轮次上下文。
        def work_context(history):
            if self.work_context is None:
                return '\n'.join(part for part in (environment, thought[0], change_notice) if part)
            return '\n'.join(part for part in (environment, thought[0], change_notice,
                                                self.work_context(history, model, cancel, update_status)) if part)

        def enter_work(reason, history):
            arrived = threading.Event()
            self._workerEvent.emit('enter_work', (generation, (reason, arrived)))
            while not arrived.wait(.05):
                if cancel.is_set():
                    raise GenerationCancelled('已停止生成')
            if cancel.is_set():
                raise GenerationCancelled('已停止生成')
            return work_context(history)

        def worker():
            try:
                from .conversation import run
                from config_model import load_user_layer
                from keystone.todos import from_plan
                rows = load_user_layer(self.store.home)
                level, pair = planning_selection(rows)
                level = messages[task_index].requested_planning or level
                if level != 'none' and messages[task_index].task_plan is None:
                    progress('planning', '正在规划 · ' + (' / '.join(pair) if pair else model))
                    plan = make_plan(self.store.home, rows, pair, client, messages, level, model, cancel)
                    if cancel.is_set():
                        raise GenerationCancelled('已停止规划')
                    messages[task_index].task_plan = plan
                    show_todos(from_plan(plan.tasks) if plan.show_todos else [])
                # 长期记忆（分级检索插件，与 Tk 版同一桥）：未启用零网络调用；服务挂了对话照常。
                from .memory import bridge_for
                memory = bridge_for(home)
                if memory is not None:
                    update_status('检索长期记忆…')
                    block = memory.context_for(task, model=model)
                    notice = memory.consume_notice()
                    if notice:
                        update_status(notice)
                    if block:
                        thought[0] = block
                if contemplating:
                    from .contemplation import contemplate, thinking_suite
                    notes = contemplate(client, thinking_suite(home), task, model=model, effort=effort,
                                        temperature=temperature, cancel=cancel, status=update_status)
                    thought[0] = '\n'.join(part for part in (thought[0], notes.context()) if part)
                    if notes.rounds:
                        self._workerEvent.emit('contemplation', (generation, notes.summary()))
                    update_status('正在生成…')
                context = ('\n'.join(part for part in (thought[0], change_notice) if part)
                           if chat_mode else work_context(messages))
                from .conversation import MAX_TURNS
                max_turns = self.turn_limit() if self.turn_limit is not None else MAX_TURNS
                result = run(client, messages, model=model, effort=effort,
                    cancel=cancel, authorize=authorize, tool_filter=tool_filter, tools_enabled=tools_enabled,
                    turn_context=context, system_context=role_text, mcp=connection, temperature=temperature,
                    chat_mcp=chat_connection, enter_work=enter_work if chat_mode else None,
                    max_turns=max_turns, attachment_folder=self.store.folder, progress=progress,
                    on_todos=show_todos if authorize is not None else None,
                    resuming=resuming,
                    chunk=lambda chunk: self._workerEvent.emit('chunk', (generation, chunk)),
                    checkpoint=lambda history: self._workerEvent.emit('checkpoint', (generation, history)),
                    receipt=lambda name, response: self._workerEvent.emit('receipt', (generation, (name, response))),
                    status=update_status,
                    next_round=lambda: self._workerEvent.emit('next_round', (generation, '')))
                if getattr(result, 'budget_exhausted', False):
                    self._workerEvent.emit('budget_exhausted', (generation, ''))
                if memory is not None:
                    memory.ingest_turn_async(task, result.text, session=session_id)
                self._workerEvent.emit("done", (generation, (result.text, getattr(result, 'responses_replay', None))))
            except GenerationCancelled:
                self._workerEvent.emit("cancelled", (generation, ""))
            except Exception as exc:
                self._workerEvent.emit("failed", (generation, str(redact(str(exc)))))
        threading.Thread(target=worker, daemon=True, name="Keystone-Qt-stream").start()

    @Slot(str)
    def setConversationMode(self, mode):
        if self.busy or mode not in {'chat', 'work'} or mode == self._conversation_mode:
            return
        self._conversation_mode = mode
        self.changed.emit()

    @Slot()
    def stop(self):
        if self._busy:
            self._cancel.set()
            self._status = "正在停止…"
            self.changed.emit()

    @Slot(QObject)
    def formatCode(self, quick_document):
        from .code_highlighter import CodeHighlighter
        document = quick_document.textDocument()
        if document is None:
            return
        key = id(quick_document)
        if key not in self._code_highlighters:
            highlighter = CodeHighlighter(document)
            self._code_highlighters[key] = (highlighter, document)
            document.destroyed.connect(lambda _obj=None, k=key: self._code_highlighters.pop(k, None))
            highlighter.rehighlight()

    @Slot(QObject, int, int, str)
    def replaceCodeSelection(self, quick_document, start, end, replacement):
        cursor = QTextCursor(quick_document.textDocument())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)

    def _flush_stream(self):
        self._stream_timer.stop()
        if self._pending_chunks:
            self._reply += ''.join(self._pending_chunks)
            self._pending_chunks.clear()
            self._messages.update(self._reply_index, textContent=self._reply)
            self.streamAdvanced.emit()

    @Slot(str, object)
    def _on_worker(self, kind, payload):
        if self._closed:
            return
        if kind == "connection":
            ticket, endpoint, online, models, detail = payload
            if endpoint != self._gateway or ticket != self._connection_epoch:
                return
            self._client = None
            if self._managed_models is None:
                self._models = models or ["default"]
                if self._model not in self._models:
                    self._model = self._models[0]
            elif online and self._model not in models:
                self._connected = False
                self._status = "网关模型未就绪"
                self._error = "活动网关与所选模型不一致，请重新选择模型。"
                self.changed.emit()
                return
            self._connected = online
            self._status = "网关已连接" if online else "网关未连接"
            self._error = "" if online else "请启动 Keystone 网关，或在连接设置中更改地址。"
            self.changed.emit()
            return
        generation, value = payload
        if generation != self._generation or not self._busy:
            return
        if kind == 'enter_work':
            reason, arrived = value
            if not self._cancel.is_set():
                self._conversation_mode = 'work'
                self._status = '已切换至 Work · ' + reason
                self.navigateRequested.emit('chat')
                self.changed.emit()
            arrived.set()
            return
        if kind == 'checkpoint':
            self._history = value
            self._changes.anchor()
            self._persist()
            return
        if kind == 'todos':
            index, items, plan = value
            self._history[index].todos = items
            self._history[index].task_plan = plan
            self._messages.update(self._reply_index, todos=items)
            self._persist()
            self.streamAdvanced.emit()
            return
        if kind == 'receipt':
            self._changes.record(*value)
            receipts = self._messages.rows[self._reply_index].get('toolReceipts', [])
            self._messages.update(self._reply_index, toolReceipts=[*receipts, preview_receipt(*value)])
            return
        if kind == 'contemplation':
            self._messages.update(self._reply_index, contemplation=value)
            return
        if kind == 'budget_exhausted':
            self._messages.update(self._reply_index, continuable=True)
            return
        if kind == 'timeline':
            phase, label, elapsed = value
            steps = self._finish_step(elapsed)
            steps.append({'phase': phase, 'label': label, 'at': elapsed, 'ms': -1})
            self._messages.update(self._reply_index, steps=steps[-128:])
            self._status = label
            self.changed.emit()
            return
        if kind == 'status':
            self._status = value
            self.changed.emit()
            return
        if kind == 'next_round':
            self._stream_timer.stop()
            self._pending_chunks.clear()
            self._reply = ''
            self._messages.update(self._reply_index, textContent='')
            return
        if kind == "chunk":
            self._pending_chunks.append(value)
            if not self._stream_timer.isActive():
                self._stream_timer.start()
            return
        self._flush_stream()
        replay = None
        if kind == "done":
            if isinstance(value, tuple):
                value, replay = value
            self._reply = value or self._reply
        steps = self._finish_step(self.turnElapsedMs())
        if self._reply and kind != "failed":
            self._history.append(ChatMessage("assistant", self._reply, responses_replay=replay, steps=steps))
        state = "complete" if kind == "done" else "stopped" if kind == "cancelled" else "error"
        task = next(m for m in reversed(self._history) if m.role == 'user')
        task.interrupted = kind == 'cancelled' or (task.interrupted and kind == 'failed')
        if task.interrupted:
            self._messages.update(self._reply_index, continuable=True)
        self._messages.update(self._reply_index, textContent=self._reply, state=state, steps=steps)
        if self._changes.active in self._changes.entries:
            key = self._changes.active
            self._changes.anchor()
            self._messages.update(self._reply_index, changeKey=key, changeInfo=self._changes.card(key))
        self._busy = False
        self._status = "已停止" if kind == "cancelled" else "发送失败" if kind == "failed" else "就绪"
        self._error = value if kind == "failed" else ""
        self._persist()
        self._changes.finish()
        self.changed.emit()
        self.streamAdvanced.emit()

    @Slot(result=int)
    def turnElapsedMs(self):
        return int((time.monotonic() - self._turn_start) * 1000)

    def _finish_step(self, elapsed):
        steps = list(self._messages.rows[self._reply_index].get('steps', ()))
        if steps:
            steps[-1] = {**steps[-1], 'ms': max(0, elapsed - steps[-1]['at'])}
        return steps

    @Slot(str)
    def copyText(self, text):
        QGuiApplication.clipboard().setText(text)

    @Slot(QPointF, float, float)
    def syncInputMethod(self, origin, width, height):
        ime = QGuiApplication.inputMethod()
        ime.setInputItemTransform(QTransform.fromTranslate(origin.x(), origin.y()))
        ime.setInputItemRectangle(QRectF(0, 0, width, height))
        ime.update(Qt.InputMethodQuery.ImCursorRectangle | Qt.InputMethodQuery.ImAnchorRectangle
                   | Qt.InputMethodQuery.ImInputItemClipRectangle)

    @Slot(QObject)
    def formatMessage(self, quick_document):
        """Space native Markdown blocks without changing text, links or code spans."""
        document = quick_document.textDocument()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        block = document.begin()
        while block.isValid():
            before = block.blockFormat()
            style = QTextBlockFormat(before)
            style.setLineHeight(140, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            style.setTopMargin(0)
            style.setBottomMargin(4 if block.textList() else 12)
            if style != before:
                cursor.setPosition(block.position())
                cursor.setBlockFormat(style)
            block = block.next()
        cursor.endEditBlock()

    @Slot(str, str, result=str)
    def iconSource(self, name, color):
        import base64
        import re
        if not re.fullmatch(r"[a-z0-9-]+", name) or not re.fullmatch(r"#[0-9a-fA-F]{6,8}", color):
            return ""
        lucide_dir = Path(__file__).resolve().parents[1] / "assets" / "ui" / "lucide"
        path = lucide_dir / (name + ".svg")
        if not path.is_file():
            fallback = {
                "pencil": "square-pen", "panel-right": "panels-top-left", "arrow-down": "chevron-down",
                "code": "code-xml", "blocks": "puzzle", "grid-2x2": "layers", "list-checks": "clipboard-check",
                "users": "user-round", "sparkles": "dna", "calendar-clock": "clock-3", "file-code": "file-text",
                "refresh": "refresh-cw", "file": "file-text", "wrench": "sliders-horizontal",
            }.get(name, name)
            path = lucide_dir / (fallback + ".svg")
        if not path.is_file():
            path = path.with_name("info.svg")
        try:
            svg = path.read_text(encoding="utf-8").replace("currentColor", color)
            svg = svg.replace('stroke-width="2"', 'stroke-width="1.75"')
            return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode("ascii")
        except OSError:
            return ""

    @Slot(str)
    def openAttachment(self, reference):
        from .productivity_tools import attachment_path, office_fallback
        from .local_tool_runtime import ToolFailure
        try:
            path = attachment_path(self.store.folder, reference)
            url = QUrl.fromLocalFile(str(path))
            if path.suffix == '.png':
                self.chartPreviewRequested.emit(url, path.name)
                return
            application = office_fallback(path.suffix)
            if application:
                from PySide6.QtCore import QProcess
                started, _ = QProcess.startDetached(str(application), [str(path)])
                if started:
                    return
            if not QDesktopServices.openUrl(url):
                raise ValueError('系统未能打开附件。文件位于 ' + str(path))
        except (ValueError, OSError, ToolFailure) as exc:
            self._error = str(exc)
            self.changed.emit()

    @Slot(str)
    def openLink(self, url):
        target = QUrl(url)
        if target.scheme() in ("https", "http"):
            self.browseRequested.emit(url)

    @Slot(str)
    def openWorkbench(self, view="chat"):
        self.navigateRequested.emit('work' if view == 'chat' else view)

    def shutdown(self):
        self._flush_stream()
        self._save_draft()
        self._persist()
        self._closed = True
        self._cancel.set()

    def _load_demo(self):
        self._sid, self._title = "demo-glass", "让界面拥有真实的玻璃质感"
        self._models = ["Keystone · 默认模型", "轻量模型", "推理模型"]
        self._model = self._models[0]
        self._sessions.reset([
            {"sessionId": self._sid, "title": self._title, "subtitle": "刚刚"},
            {"sessionId": "demo-layout", "title": "梳理工作区与对话结构", "subtitle": "今天"},
            {"sessionId": "demo-font", "title": "字体与阅读体验", "subtitle": "昨天"}])
        self._messages.reset([
            {"roleName": "user", "textContent": "让输入框、选中会话和选项控件拥有统一的玻璃质感。", "state": "complete"},
            {"roleName": "assistant", "textContent": "## 清晰的内容，轻盈的界面\n\n玻璃承载操作，文字保持清晰。输入框浮在对话之上，背景在弧形边缘发生细微折射，光线随指针的位置变化。\n\n- 输入框与选中会话共享同一套材质\n- 选项浮层保持一致的透光与层次\n- 输入、选择与键盘导航由原生控件处理\n\n你可以移动指针、切换会话，或打开下方的选项查看效果。", "state": "complete"}])
        self.changed.emit()
