"""Open files and folders passed on the command line (Explorer "Open with").

A second launch with paths hands them to the running window for the same
home over a local socket and exits, so files open where the user already is.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def server_name(home):
    key = str(Path(home).expanduser().resolve()).casefold().encode('utf-8')
    return 'revon-desktop-' + hashlib.sha1(key).hexdigest()[:12]


def normalize(paths, cwd=None):
    """Absolute, existing paths only; keeps order, drops duplicates."""
    base = Path(cwd) if cwd else Path.cwd()
    seen, result = set(), []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw).expanduser()
        path = (path if path.is_absolute() else base / path).resolve()
        if path.exists() and str(path) not in seen:
            seen.add(str(path))
            result.append(str(path))
    return result


def project_for(path):
    """The project a path belongs to: a folder itself, else the file's Git root or its folder."""
    target = Path(path)
    if target.is_dir():
        return str(target)
    folder = target.parent
    try:
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=folder, capture_output=True,
                                text=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode == 0 and result.stdout.strip():
            return str(Path(result.stdout.strip()).resolve())
    except (OSError, subprocess.SubprocessError):
        pass
    return str(folder)


def forward(paths, home, timeout_ms=1500):
    """Send paths to a running instance; True when it accepted them."""
    socket = QLocalSocket()
    socket.connectToServer(server_name(home))
    if not socket.waitForConnected(timeout_ms):
        return False
    if sys.platform == 'win32':
        # Let the running window take the foreground when it receives the request.
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    socket.write((json.dumps({'open': list(paths)}, ensure_ascii=False) + '\n').encode('utf-8'))
    socket.waitForBytesWritten(timeout_ms)
    accepted = socket.waitForReadyRead(timeout_ms) and bytes(socket.readAll()).strip() == b'ok'
    socket.disconnectFromServer()
    return accepted


class OpenRequests(QObject):
    """Local server that receives paths from later launches."""
    received = Signal(list)

    def __init__(self, home, parent=None):
        super().__init__(parent)
        self._buffers = {}
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        name = server_name(home)
        if not self.server.listen(name):
            # A stale name after a crash is removable; a live owner keeps it.
            probe = QLocalSocket()
            probe.connectToServer(name)
            if not probe.waitForConnected(300):
                QLocalServer.removeServer(name)
                self.server.listen(name)

    @property
    def listening(self):
        return self.server.isListening()

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._buffers[socket] = b''
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: (self._buffers.pop(s, None), s.deleteLater()))

    def _read(self, socket):
        self._buffers[socket] = self._buffers.get(socket, b'') + bytes(socket.readAll())
        if b'\n' not in self._buffers[socket]:
            return
        line = self._buffers[socket].split(b'\n', 1)[0]
        try:
            paths = json.loads(line.decode('utf-8')).get('open', [])
            paths = [p for p in paths if isinstance(p, str)]
        except (ValueError, AttributeError):
            socket.write(b'error\n')
            return
        socket.write(b'ok\n')
        socket.flush()
        self.received.emit(normalize(paths))
