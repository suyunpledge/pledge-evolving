"""One Qt shell, one private child host, and a retained browser side panel."""
import json
from pathlib import Path
import sys
import threading
import uuid
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Property, QProcess, QProcessEnvironment, QTimer, Signal, Slot
from PySide6.QtGui import QWindow
from forge.desktop_permissions import LABELS, PROFILES
from forge.secrets import redact

PAGES = {'chat': 'Work 对话', 'development': 'Coding', 'task': '任务', 'files': '文件与项目',
         'tools': '工具与插件', 'config': '模型与配置', 'agents': 'Agents', 'knowledge': '知识库',
         'evolution': '演化', 'schedules': '定时任务', 'connectors': '连接器',
         'preview': '代码预览', 'computer': '电脑操作', 'work': 'Work 完整功能'}


class WorkbenchController(QObject):
    changed = Signal()
    browserChanged = Signal()
    permissionRequested = Signal()
    gatewayChanged = Signal(str)
    _approvalRequested = Signal(str, str, str)
    _approvalFinished = Signal(str)

    def __init__(self, home, *, demo=False, parent=None):
        super().__init__(parent)
        self.home, self.demo = Path(home), demo
        self._page, self._profile = 'chat', 'sandbox'
        self._navigation = False
        self._browser_visible, self._browser_loaded = False, False
        self._browser_url, self._browser_width = 'about:blank', 420.0
        self._window = None
        self._error, self._status = '', ''
        self._busy = False
        self._unsaved = False
        self._page_serial = 0
        self._workspace = str(Path(__file__).resolve().parents[2])
        self._pending = []
        self._waiters = {}
        self._input_busy = lambda: False
        self._buffer = b''
        self._closing = False
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.readyReadStandardError.connect(self._read_error)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)
        self._approvalRequested.connect(self._queue_local_approval)
        self._approvalFinished.connect(self._clear_approval)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._host_timeout)

    page = Property(str, lambda s: s._page, notify=changed)
    pageTitle = Property(str, lambda s: PAGES[s._page], notify=changed)
    navigationOpen = Property(bool, lambda s: s._navigation, notify=changed)
    nativeWindow = Property(QObject, lambda s: s._window, notify=changed)
    busy = Property(bool, lambda s: s._busy, notify=changed)
    hasUnsaved = Property(bool, lambda s: s._unsaved, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)
    status = Property(str, lambda s: s._status, notify=changed)
    workspace = Property(str, lambda s: s._workspace, notify=changed)
    permission = Property(str, lambda s: s._profile, notify=changed)
    permissionLabel = Property(str, lambda s: LABELS[s._profile], notify=changed)
    approvals = Property('QVariantList', lambda s: s._pending, notify=changed)
    browserVisible = Property(bool, lambda s: s._browser_visible, notify=browserChanged)
    browserLoaded = Property(bool, lambda s: s._browser_loaded, notify=browserChanged)
    browserUrl = Property(str, lambda s: s._browser_url, notify=browserChanged)
    browserWidth = Property(float, lambda s: s._browser_width, notify=browserChanged)

    @Slot()
    def toggleNavigation(self):
        self._navigation = not self._navigation
        self.changed.emit()

    @Slot()
    def clearError(self):
        self._error = ''
        self.changed.emit()

    @Slot(str)
    def navigate(self, page):
        if page not in PAGES:
            return
        self._page = page
        if page == 'chat':
            self._navigation = False
            self._send({'command': 'navigate', 'page': page})
        else:
            self._navigation = True
            if self._process.state() == QProcess.ProcessState.NotRunning:
                self.start_host()
            self._send({'command': 'navigate', 'page': page})
        self.changed.emit()

    @Slot()
    def start_host(self):
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return
        self._error, self._status = '', '正在载入工作台…'
        gui = Path(__file__).resolve().parents[1]
        executable = Path(sys.executable).with_name('python.exe')
        self._process.setProgram(str(executable))
        args = [str(gui / 'qt_frontend' / 'embedded_host.py'), '--home', str(self.home)]
        if self.demo:
            args.append('--demo')
        self._process.setArguments(args)
        self._process.setWorkingDirectory(str(gui))
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONIOENCODING', 'utf-8')
        env.insert('FORGE_NO_AUTOSTART', '1' if self.demo else '0')
        self._process.setProcessEnvironment(env)
        self._process.start()
        self._timeout.start(30000)
        self.changed.emit()

    def _send(self, value):
        if self._process.state() == QProcess.ProcessState.Running:
            self._process.write((json.dumps(value, ensure_ascii=False) + '\n').encode())

    def _read(self):
        self._buffer += bytes(self._process.readAllStandardOutput())
        while b'\n' in self._buffer:
            raw, self._buffer = self._buffer.split(b'\n', 1)
            if not raw.startswith(b'FORGE_UI:'):
                continue
            try:
                row = json.loads(raw[9:])
                self._receive(row)
            except (ValueError, KeyError, TypeError) as exc:
                self._error = '工作台通信错误：' + str(exc)
                self.changed.emit()

    def _receive(self, row):
        kind = row.get('event')
        if kind == 'ready':
            self._timeout.stop()
            self._window = QWindow.fromWinId(int(row['handle']))
            if self._window is None:
                raise ValueError('无法挂载功能页面')
            self._status = ''
            self.changed.emit()
            self._send({'command': 'permission', 'profile': self._profile})
            self._send({'command': 'navigate', 'page': self._page})
            self._send({'command': 'mounted', 'visible': self._page != 'chat'})
        elif kind == 'mounted':
            self._window.setVisible(self._page != 'chat')
        elif kind == 'status':
            self._busy = bool(row.get('busy'))
            self._unsaved = bool(row.get('unsaved'))
            self._workspace = row.get('workspace', self._workspace)
            self._status = row.get('status', '')
            self.changed.emit()
        elif kind == 'navigate':
            if row.get('page') in PAGES:
                self._page = row['page']
                self._page_serial += 1
                self._navigation = self._page != 'chat'
                self.changed.emit()
        elif kind == 'browser':
            self.openBrowser(str(row.get('url', '')))
        elif kind == 'permissions':
            self.permissionRequested.emit()
        elif kind == 'approval':
            self._pending.append({'id': row['id'], 'title': row['title'], 'detail': row['detail'], 'source': 'host'})
            self.changed.emit()
        elif kind == 'approval_closed':
            self._pending = [r for r in self._pending if r['id'] != row['id']]
            self.changed.emit()
        elif kind == 'permission':
            if row.get('profile') in PROFILES:
                self._profile = row['profile']
            self.changed.emit()
        elif kind == 'gateway':
            self.gatewayChanged.emit(row['url'])
        elif kind == 'error':
            self._error = str(row.get('message', '工作台载入失败'))
            self.changed.emit()
        elif kind == 'closing' and not self._closing:
            self._error = '工作台退出：' + str(row.get('reason', ''))
            self.changed.emit()
        elif kind == 'host_stopped' and not row.get('closed') and not self._closing:
            self._error = '功能窗口意外结束。'
            self.changed.emit()

    def _read_error(self):
        text = bytes(self._process.readAllStandardError()).decode('utf-8', 'replace')
        if 'Traceback' in text or 'Error:' in text:
            self._error = str(redact(text[-2000:]))
            self.changed.emit()

    def _process_error(self, _):
        if not self._closing:
            self._error = '工作台启动失败：' + self._process.errorString()
            self.changed.emit()

    def _host_timeout(self):
        if self._window is None:
            self._error = '工作台仍未就绪。可返回对话，或关闭后重新打开。'
            self.changed.emit()

    def _finished(self, exit_code, exit_status):
        self._timeout.stop()
        if self._window is not None:
            self._window.setParent(None)
            self._window.deleteLater()
            self._window = None
        self._busy = False
        self._pending = [r for r in self._pending if r['source'] != 'host']
        if not self._closing:
            self._error = self._error or f'功能工作台已关闭（退出码 {exit_code}），可点击重新载入。'
        self.changed.emit()

    @Slot(str)
    def setPermission(self, profile):
        if profile not in PROFILES or self._busy or self._input_busy() or self._pending:
            return
        if self._process.state() == QProcess.ProcessState.Running:
            self._send({'command': 'permission', 'profile': profile})
        else:
            self._profile = profile
            self.changed.emit()

    def approve(self, title, args, cancel=None):
        identifier = uuid.uuid4().hex
        done = threading.Event()
        waiter = {'done': done, 'approved': False}
        self._waiters[identifier] = waiter
        self._approvalRequested.emit(identifier, title, json.dumps(redact(args), ensure_ascii=False, indent=2))
        while not done.wait(0.1):
            if self._closing or cancel is not None and cancel.is_set():
                break
        self._waiters.pop(identifier, None)
        self._approvalFinished.emit(identifier)
        return waiter['approved'] and not self._closing and not (cancel is not None and cancel.is_set())

    @Slot(str)
    def _clear_approval(self, identifier):
        self._pending = [row for row in self._pending if row['id'] != identifier]
        self.changed.emit()

    @Slot(str, str, str)
    def _queue_local_approval(self, identifier, title, detail):
        if identifier in self._waiters:
            self._pending.append({'id': identifier, 'title': title, 'detail': detail, 'source': 'local'})
            self.changed.emit()

    @Slot(str, bool)
    def resolveApproval(self, identifier, approved):
        row = next((r for r in self._pending if r['id'] == identifier), None)
        if row is None:
            return
        self._pending.remove(row)
        if row['source'] == 'host':
            self._send({'command': 'approval', 'id': identifier, 'approved': approved})
        else:
            waiter = self._waiters.get(identifier)
            if waiter:
                waiter['approved'] = approved
                waiter['done'].set()
        self.changed.emit()

    @Slot()
    def toggleBrowser(self):
        self._browser_visible = not self._browser_visible
        self._browser_loaded = self._browser_loaded or self._browser_visible
        self.browserChanged.emit()

    @Slot(float)
    def resizeBrowser(self, width):
        self._browser_width = max(300, min(720, width))
        self.browserChanged.emit()

    @Slot(str)
    def openBrowser(self, value):
        value = value.strip()
        if value and '://' not in value and not value.startswith(('about:', 'javascript:', 'file:', 'data:')):
            value = 'https://' + value
        try:
            parsed = urlsplit(value)
        except ValueError:
            self._error = '网址格式不正确。'
            self.changed.emit()
            return
        if value != 'about:blank' and (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password):
            self._error = '浏览器支持 HTTP / HTTPS 地址。'
            self.changed.emit()
            return
        self._browser_url = value or 'about:blank'
        self._browser_visible = self._browser_loaded = True
        self.browserChanged.emit()

    def shutdown(self):
        self._closing = True
        for waiter in self._waiters.values():
            waiter['done'].set()
        self._send({'command': 'close'})
        if not self._process.waitForFinished(10000):
            self._process.terminate()
            if not self._process.waitForFinished(1500):
                self._process.kill()
                self._process.waitForFinished(1000)
