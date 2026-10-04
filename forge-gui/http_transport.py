"""Standard-library HTTP with per-request cancellation, including headers and TLS."""
from contextlib import contextmanager
import http.client
import socket
import threading
import urllib.error
import urllib.request


class RequestCancelled(RuntimeError):
    pass


class _Scope:
    def __init__(self, event):
        self.event = event
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.sock = None

    def check(self):
        if self.event.is_set():
            raise RequestCancelled("已停止请求")

    def register(self, sock):
        with self.lock:
            self.sock = sock
            if self.event.is_set():
                self._abort()
        self.check()

    def _abort(self):
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        # Windows buffered reads need the handle closed. Detaching avoids a
        # later response.close() closing a handle that the OS has already reused.
        try:
            handle = sock.detach()
            if handle != -1:
                socket.close(handle)
        except OSError:
            pass

    def watch(self):
        while not self.done.wait(.05):
            if self.event.is_set():
                with self.lock:
                    if not self.done.is_set():
                        self._abort()
                return

    def connect(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        self.check()
        host, port = address
        error = None
        # DNS resolution remains under the OS resolver's control. Each socket
        # is registered before connect(), so TCP/header/body waits are interruptible.
        for family, kind, proto, _name, target in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            self.check()
            sock = socket.socket(family, kind, proto)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                self.register(sock)
                sock.connect(target)
                self.check()
                return sock
            except OSError as exc:
                error = exc
                sock.close()
                self.check()
            except BaseException:
                sock.close()
                raise
        raise error or OSError("无法解析服务器地址")


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        hostname = self._tunnel_host or self.host
        with self._scope.lock:
            self._scope.check()
            # wrap_socket transfers ownership of the original descriptor.
            self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname,
                                                  do_handshake_on_connect=False)
            self._scope.sock = self.sock
        self._scope.check()
        self.sock.do_handshake()
        self._scope.check()


def _connection(scope, connection_type, host, **kwargs):
    scope.check()
    connection = connection_type(host, **kwargs)
    connection._create_connection = scope.connect
    connection._scope = scope
    return connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, scope):
        super().__init__()
        self.scope = scope

    def http_open(self, req):
        return self.do_open(lambda host, **kw: _connection(self.scope, http.client.HTTPConnection, host, **kw), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, scope):
        super().__init__()
        self.scope = scope

    def https_open(self, req):
        return self.do_open(lambda host, **kw: _connection(self.scope, _HTTPSConnection, host, **kw),
                            req, context=self._context)


@contextmanager
def open_response(request, *, timeout, cancel_event=None):
    if cancel_event is None:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            yield response
        return
    scope = _Scope(cancel_event)
    scope.check()
    watcher = threading.Thread(target=scope.watch, daemon=True, name="Forge-http-cancel")
    watcher.start()
    try:
        # Preserve urllib's normal proxy discovery, redirect and TLS verification.
        opener = urllib.request.build_opener(_HTTPHandler(scope), _HTTPSHandler(scope))
        with opener.open(request, timeout=timeout) as response:
            scope.check()
            yield response
            scope.check()
    except urllib.error.HTTPError as error:
        scope.check()
        # Read a bounded diagnostic while the cancellation watcher still owns
        # the socket. Callers can inspect it after this context has cleaned up.
        try:
            error._forge_detail = error.read(500).decode("utf-8", errors="replace")
            scope.check()
        except (OSError, http.client.HTTPException):
            scope.check()
            error._forge_detail = str(error.reason)
        finally:
            error.close()
        raise
    except (OSError, http.client.HTTPException):
        scope.check()
        raise
    finally:
        with scope.lock:
            scope.done.set()
            scope._abort()
        watcher.join(.2)
