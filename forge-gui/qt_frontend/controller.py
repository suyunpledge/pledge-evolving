"""Qt model/controller boundary; reuse Forge's real transport and secret scope."""
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

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication, QDesktopServices, QTextBlockFormat, QTextCursor

from forge_client import ChatMessage, ForgeGatewayClient, GenerationCancelled
from http_transport import open_response
from interaction_model import compose_prompt, read_attachment
from forge.secrets import redact
from .store import SessionStore


class RowsModel(QAbstractListModel):
    NAMES = ("sessionId", "title", "subtitle", "roleName", "textContent", "state", "stamp")

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
                return self.rows[index.row()].get(self.NAMES[i], "")
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
    _workerEvent = Signal(str, object)

    def __init__(self, home, *, demo=False, client_factory=ForgeGatewayClient, parent=None):
        super().__init__(parent)
        self.store = SessionStore(home)
        self.demo = demo
        self.factory = client_factory
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
        self._attachments = []
        self._models = ["default"]
        self._model = str(self._prefs.get("model", "default"))
        self._reasoning = str(self._prefs.get("reasoning", "medium"))
        desktop = {} if demo else self.store.read(Path(home) / 'forge-desktop.json', {})
        port = desktop.get('gateway_port', 8799) if isinstance(desktop, dict) else 8799
        port = port if isinstance(port, int) and 1024 <= port <= 65535 else 8799
        self._gateway = str(self._prefs.get("gateway", os.environ.get("FORGE_GATEWAY_URL", f"http://127.0.0.1:{port}")))
        self._material = float(self._prefs.get("material", 0.75))
        self._reduce_motion = bool(self._prefs.get("reduceMotion", False))
        self._solid = bool(self._prefs.get("solid", False))
        self._cancel = threading.Event()
        self._generation = 0
        self._connection_epoch = 0
        self._closed = False
        self.tool_authorizer = None
        self._client = None
        self._workerEvent.connect(self._on_worker, Qt.ConnectionType.QueuedConnection)
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
    busy = Property(bool, lambda self: self._busy, notify=changed)
    connected = Property(bool, lambda self: self._connected, notify=changed)
    status = Property(str, lambda self: self._status, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)
    model = Property(str, lambda self: self._model, notify=changed)
    models = Property("QStringList", lambda self: self._models, notify=changed)
    reasoning = Property(str, lambda self: self._reasoning, notify=changed)
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
        if self.demo or not self._history:
            return
        try:
            self.store.save_session({"id": self._sid, "title": self._title, "updated": time.time(),
                                     "messages": [m.to_session_dict() for m in self._history]})
            self._refresh_sessions()
        except OSError:
            self._error = "对话暂时无法保存，请保留窗口。"

    @Slot()
    def newSession(self):
        if self._busy:
            return
        self._save_draft()
        self._persist()
        self._sid = "s" + uuid.uuid4().hex
        self._title, self._draft, self._error = "新对话", "", ""
        self._history, self._attachments = [], []
        self._client = None
        self._messages.reset([])
        self.changed.emit()
        self.draftChanged.emit()
        self.focusComposer.emit()

    @Slot(str)
    def selectSession(self, sid):
        if self._busy or sid == self._sid:
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
        self._history = [ChatMessage(m['role'], m['content'], tool_calls=m.get('tool_calls'), tool_call_id=m.get('tool_call_id', ''))
            if m.get('role') == 'tool' or m.get('tool_calls') else ChatMessage.from_session_dict(m)
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
        if not self._busy and model in self._models:
            self._model = model
            self._save_preferences()
            self.changed.emit()

    @Slot(str)
    def setReasoning(self, value):
        if not self._busy and value in ("off", "low", "medium", "high"):
            self._reasoning = value
            self._save_preferences()
            self.changed.emit()

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
        parsed = urlsplit(value.strip())
        if self._busy:
            return
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            self._error = "请输入不含账号密码的 HTTP / HTTPS 网关地址。"
            self.changed.emit()
            return
        self._gateway = value.strip().rstrip("/")
        self._save_preferences()
        self.refreshConnection()

    @Slot()
    def refreshConnection(self):
        if self.demo or self._busy:
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
                    with open_response(client._request("GET", "/v1/models"), timeout=5) as response:
                        data = json.loads(response.read())
                        models = [str(x["id"]) for x in data.get("data", []) if isinstance(x, dict) and x.get("id")]
                self._workerEvent.emit("connection", (ticket, endpoint, online, models, detail))
            except Exception as exc:
                self._workerEvent.emit("connection", (ticket, endpoint, False, [], str(redact(str(exc)))))
        threading.Thread(target=worker, daemon=True, name="Forge-Qt-connection").start()

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
        if self._busy or not text:
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
        except ValueError as exc:
            self._error = str(exc)
            self.changed.emit()
            return
        if not self._history:
            self._title = text.splitlines()[0][:36]
        self._history.append(ChatMessage("user", prompt, user_text=text,
                            attachment_names=tuple(Path(a["path"]).name for a in self._attachments)))
        self._messages.append({"roleName": "user", "textContent": text, "state": "complete"})
        self._draft, self._attachments = "", []
        self.draftChanged.emit()
        self._save_draft()
        self._start_generation()

    @Slot()
    def retry(self):
        if self._busy or not self._connected or not self._history or self._history[-1].role != "user":
            return
        self._messages.reset(self._transcript_rows())
        self._start_generation()

    def _transcript_rows(self):
        rows = []
        for message in self._history:
            if message.role == 'tool':
                try:
                    result = json.loads(message.content)
                    text = '工具执行完成' if result.get('ok') else '工具未完成：' + str(result.get('error', ''))
                except (ValueError, AttributeError):
                    text = '工具记录已保存'
                rows.append({'roleName': 'tool', 'textContent': text, 'state': 'complete'})
            elif message.content:
                rows.append({'roleName': message.role, 'textContent': message.user_text or message.content, 'state': 'complete'})
        return rows

    def _start_generation(self):
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        cancel = self._cancel
        self._busy, self._error, self._status = True, "", "正在生成…"
        self._messages.append({"roleName": "assistant", "textContent": "", "state": "streaming"})
        self._reply_index = len(self._messages.rows) - 1
        self._reply = ""
        self._persist()
        self.changed.emit()
        self.streamAdvanced.emit()
        messages, model, effort = copy.deepcopy(self._history), self._model, self._reasoning
        client = self._client or self.factory(self._gateway)
        self._client = client
        authorize = self.tool_authorizer() if self.tool_authorizer is not None else None
        def worker():
            try:
                from .conversation import run
                result = run(client, messages, model=model, effort=None if effort == 'off' else effort,
                    cancel=cancel, authorize=authorize,
                    chunk=lambda chunk: self._workerEvent.emit('chunk', (generation, chunk)),
                    checkpoint=lambda history: self._workerEvent.emit('checkpoint', (generation, history)),
                    status=lambda status: self._workerEvent.emit('status', (generation, status)),
                    next_round=lambda: self._workerEvent.emit('next_round', (generation, '')))
                self._workerEvent.emit("done", (generation, result.text))
            except GenerationCancelled:
                self._workerEvent.emit("cancelled", (generation, ""))
            except Exception as exc:
                self._workerEvent.emit("failed", (generation, str(redact(str(exc)))))
        threading.Thread(target=worker, daemon=True, name="Forge-Qt-stream").start()

    @Slot()
    def stop(self):
        if self._busy:
            self._cancel.set()
            self._status = "正在停止…"
            self.changed.emit()

    @Slot(str, object)
    def _on_worker(self, kind, payload):
        if self._closed:
            return
        if kind == "connection":
            ticket, endpoint, online, models, detail = payload
            if endpoint != self._gateway or ticket != self._connection_epoch:
                return
            self._client = None
            self._connected = online
            self._models = models or ["default"]
            if self._model not in self._models:
                self._model = self._models[0]
            self._status = "网关已连接" if online else "网关未连接"
            self._error = "" if online else "请启动 Forge 网关，或在连接设置中更改地址。"
            self.changed.emit()
            return
        generation, value = payload
        if generation != self._generation or not self._busy:
            return
        if kind == 'checkpoint':
            self._history = value
            self._persist()
            return
        if kind == 'status':
            self._status = value
            self.changed.emit()
            return
        if kind == 'next_round':
            self._reply = ''
            self._messages.update(self._reply_index, textContent='')
            return
        if kind == "chunk":
            self._reply += value
            self._messages.update(self._reply_index, textContent=self._reply)
            self.streamAdvanced.emit()
            return
        if kind == "done":
            self._reply = value or self._reply
        if self._reply and kind != "failed":
            self._history.append(ChatMessage("assistant", self._reply))
        state = "complete" if kind == "done" else "stopped" if kind == "cancelled" else "error"
        self._messages.update(self._reply_index, textContent=self._reply, state=state)
        self._busy = False
        self._status = "已停止" if kind == "cancelled" else "发送失败" if kind == "failed" else "就绪"
        self._error = value if kind == "failed" else ""
        self._persist()
        self.changed.emit()
        self.streamAdvanced.emit()

    @Slot(str)
    def copyText(self, text):
        QGuiApplication.clipboard().setText(text)

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
        name = {"pencil": "square-pen", "panel-right": "panels-top-left", "arrow-down": "chevron-down",
                "code": "code-xml", "blocks": "puzzle", "grid-2x2": "layers", "list-checks": "clipboard-check",
                "users": "user-round", "sparkles": "dna", "calendar-clock": "clock-3", "file-code": "file-text"}.get(name, name)
        path = Path(__file__).resolve().parents[1] / "assets" / "ui" / "lucide" / (name + ".svg")
        if not path.is_file():
            path = path.with_name("info.svg")
        try:
            svg = path.read_text(encoding="utf-8").replace("currentColor", color)
            return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode("ascii")
        except OSError:
            return ""

    @Slot(str)
    def openLink(self, url):
        target = QUrl(url)
        if target.scheme() in ("https", "http"):
            self.browseRequested.emit(url)

    @Slot(str)
    def openWorkbench(self, view="chat"):
        self.navigateRequested.emit('work' if view == 'chat' else view)

    def shutdown(self):
        self._save_draft()
        self._persist()
        self._closed = True
        self._cancel.set()

    def _load_demo(self):
        self._sid, self._title = "demo-glass", "让界面拥有真实的玻璃质感"
        self._models = ["Forge · 默认模型", "轻量模型", "推理模型"]
        self._model = self._models[0]
        self._sessions.reset([
            {"sessionId": self._sid, "title": self._title, "subtitle": "刚刚"},
            {"sessionId": "demo-layout", "title": "梳理工作区与对话结构", "subtitle": "今天"},
            {"sessionId": "demo-font", "title": "字体与阅读体验", "subtitle": "昨天"}])
        self._messages.reset([
            {"roleName": "user", "textContent": "让输入框、选中会话和选项控件拥有统一的玻璃质感。", "state": "complete"},
            {"roleName": "assistant", "textContent": "## 清晰的内容，轻盈的界面\n\n玻璃承载操作，文字保持清晰。输入框浮在对话之上，背景在弧形边缘发生细微折射，光线随指针的位置变化。\n\n- 输入框与选中会话共享同一套材质\n- 选项浮层保持一致的透光与层次\n- 输入、选择与键盘导航由原生控件处理\n\n你可以移动指针、切换会话，或打开下方的选项查看效果。", "state": "complete"}])
        self.changed.emit()
