"""A gateway-owned HTTPS connection for consecutive Ark Seed tool rounds."""
from contextlib import contextmanager, ExitStack, nullcontext
from http.client import HTTPException, HTTPSConnection
from io import BytesIO
import select
import ssl
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.request import getproxies, proxy_bypass


HOST = 'ark.cn-beijing.volces.com'
CHAT_PATH = '/api/coding/v3/chat/completions'
CHAT_URL = 'https://' + HOST + CHAT_PATH
CONNECT_TIMEOUT = 8.0
MAX_IDLE = 30.0
# Direct Coding requests return JSON. Only probe a reused socket when the
# agent can retry this same model step on a fresh connection.
REUSED_HEADER_TIMEOUT = 30.0


class SeedConnectFailed(ConnectionError):
    """TCP/TLS failed before any HTTP request bytes were sent."""


class SeedHeaderInterrupted(ConnectionError):
    """A reused connection returned no complete response headers."""


class SeedTransport:
    def __init__(self):
        self.lock = Lock()
        self.idle = None
        self.saved_at = 0.0
        self.closed = False
        self.connect_failures = 0

    def close(self):
        with self.lock:
            self.closed = True
            if self.idle:
                self.idle.close()
                self.idle = None

    def _take(self, timeout, scope=None):
        with self.lock:
            connection, self.idle = self.idle, None
            expired = monotonic() - self.saved_at >= MAX_IDLE
        if connection is not None:
            try:
                # A readable idle TLS socket may contain EOF or unsolicited bytes.
                if not expired and connection.sock and not select.select([connection.sock], [], [], 0)[0]:
                    return connection
            except (OSError, ValueError):
                pass
            connection.close()
        deadline = min(timeout, CONNECT_TIMEOUT)
        if scope is not None:
            from .http_transport import scoped_https_connection
            return scoped_https_connection(scope, HOST, deadline)
        return HTTPSConnection(HOST, timeout=deadline)

    def _put(self, connection):
        with self.lock:
            if not self.closed and self.idle is None:
                self.idle, self.saved_at = connection, monotonic()
                return
        connection.close()

    @contextmanager
    def open_coding(self, request, *, timeout, cancel_event=None, timings, probe_reuse=False):
        # The outer step owns the retry. If two direct handshakes fail, retain
        # the system proxy route used by older desktop installations. Never
        # switch paths after a POST may have reached Ark.
        from .http_transport import open_response
        with ExitStack() as stack:
            try:
                response = stack.enter_context(self.open(request, timeout=timeout, timings=timings,
                                                         cancel_event=cancel_event, probe_reuse=probe_reuse))
            except SeedConnectFailed:
                self.connect_failures += 1
                if self.connect_failures < 2 or not getproxies().get('https') or proxy_bypass(HOST):
                    raise
                timings['seed_connection'] = 'system_proxy_after_connect'
                response = stack.enter_context(open_response(request, timeout=timeout, cancel_event=cancel_event))
            else:
                self.connect_failures = 0
            yield response

    @contextmanager
    def open(self, request, *, timeout, timings=None, cancel_event=None, probe_reuse=False):
        if request.full_url != CHAT_URL or request.get_method() != 'POST':
            raise ValueError('Unexpected Seed request destination')
        from .http_transport import cancellable_https
        lease = cancellable_https(cancel_event) if cancel_event is not None else nullcontext()
        conn = None
        reusable = False
        try:
            with lease as scope:
                conn = self._take(timeout, scope)
                if scope is not None and conn.sock is not None:
                    scope.register(conn.sock)
                with self._exchange(conn, request, timeout, timings, probe_reuse) as response:
                    yield response
                reusable = not response.will_close and conn.sock is not None
        except BaseException:
            reusable = False
            raise
        finally:
            if reusable and conn.sock is not None:
                self._put(conn)
            elif conn is not None:
                conn.close()

    @contextmanager
    def _exchange(self, conn, request, timeout, timings, probe_reuse=False):
        conn.set_debuglevel(0)  # Never inherit process-wide debugging for credentialed requests.
        fresh = conn.sock is None
        start = monotonic()
        if fresh:
            if timings is not None:
                timings['seed_connection'] = 'seed_connect'
            try:
                conn.connect()  # Only TCP/TLS uses the short deadline.
            except ssl.SSLCertVerificationError:
                raise
            except OSError as exc:
                if timings is not None:
                    duration = round((monotonic() - start) * 1000)
                    timings['seed_connection'] = f'seed_connect_failed;dur={duration}'
                raise SeedConnectFailed(str(exc)) from exc
        if timings is not None:
            duration = round((monotonic() - start) * 1000)
            timings['seed_connection'] = f'seed_connect;dur={duration}' if fresh else 'seed_reuse;dur=0'
        sock = conn.sock
        sock.settimeout(timeout)
        sent_at = monotonic()
        conn.request('POST', CHAT_PATH, body=request.data, headers=dict(request.header_items()))
        waiting_at = monotonic()
        probe = probe_reuse and not fresh
        header_timeout = min(timeout, REUSED_HEADER_TIMEOUT) if probe else timeout
        if timings is not None:
            timings['send_ms'] = round((waiting_at - sent_at) * 1000)
            timings['header_timeout_s'] = header_timeout
        if probe:
            sock.settimeout(header_timeout)
        try:
            response = conn.getresponse()
        except (TimeoutError, ConnectionError, ssl.SSLEOFError) as exc:
            if timings is not None:
                timings['header_failure'] = type(exc).__name__
            if probe:
                raise SeedHeaderInterrupted(type(exc).__name__) from exc
            raise
        finally:
            if timings is not None:
                timings['header_wait_ms'] = round((monotonic() - waiting_at) * 1000)
        with response:
            if probe:
                sock.settimeout(timeout)  # Keep the provider's body inactivity timeout.
            if 300 <= response.status < 400:
                raise PermissionError('Authenticated HTTP redirects are disabled')
            if not 200 <= response.status < 300:
                raise HTTPError(CHAT_URL, response.status, response.reason, response.headers,
                                BytesIO(response.read(1024)))
            yield response
            if not response.will_close and conn.sock is not None and not response.isclosed():
                # SSE ends at [DONE]; consume only the remaining blank line and HTTP trailer.
                # An interrupted stream or a slow/incomplete trailer retires the connection.
                try:
                    conn.sock.settimeout(min(.2, timeout))
                    if response.read(8).strip() or not response.isclosed():
                        conn.close()
                except (OSError, HTTPException):
                    conn.close()
