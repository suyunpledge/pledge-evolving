"""A gateway-owned HTTPS connection for consecutive Ark Seed tool rounds."""
from contextlib import contextmanager
from http.client import HTTPException, HTTPSConnection
from io import BytesIO
import select
from threading import Lock
from time import monotonic
from urllib.error import HTTPError


HOST = 'ark.cn-beijing.volces.com'
CHAT_PATH = '/api/coding/v3/chat/completions'
CHAT_URL = 'https://' + HOST + CHAT_PATH
CONNECT_TIMEOUT = 8.0
MAX_IDLE = 30.0


class SeedTransport:
    def __init__(self):
        self.lock = Lock()
        self.idle = None
        self.saved_at = 0.0
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
            if self.idle:
                self.idle.close()
                self.idle = None

    def _take(self, timeout):
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
        return HTTPSConnection(HOST, timeout=min(timeout, CONNECT_TIMEOUT))

    def _put(self, connection):
        with self.lock:
            if not self.closed and self.idle is None:
                self.idle, self.saved_at = connection, monotonic()
                return
        connection.close()

    @contextmanager
    def open(self, request, *, timeout, timings=None):
        if request.full_url != CHAT_URL or request.get_method() != 'POST':
            raise ValueError('Unexpected Seed request destination')
        conn = self._take(timeout)
        reusable = False
        try:
            conn.set_debuglevel(0)  # Never inherit process-wide debugging for credentialed requests.
            fresh = conn.sock is None
            start = monotonic()
            if fresh:
                conn.connect()  # Only TCP/TLS uses the short deadline.
            if timings is not None:
                duration = round((monotonic() - start) * 1000)
                timings['seed_connection'] = f'seed_connect;dur={duration}' if fresh else 'seed_reuse;dur=0'
            conn.sock.settimeout(timeout)
            conn.request('POST', CHAT_PATH, body=request.data, headers=dict(request.header_items()))
            with conn.getresponse() as response:
                if 300 <= response.status < 400:
                    raise PermissionError('Authenticated HTTP redirects are disabled')
                if not 200 <= response.status < 300:
                    raise HTTPError(CHAT_URL, response.status, response.reason, response.headers,
                                    BytesIO(response.read()))
                yield response
                if not response.will_close and conn.sock is not None:
                    # SSE ends at [DONE]; consume only the remaining blank line and HTTP trailer.
                    # An interrupted stream or a slow/incomplete trailer retires the connection.
                    try:
                        conn.sock.settimeout(min(.2, timeout))
                        reusable = response.isclosed() or (not response.read(8).strip() and response.isclosed())
                    except (OSError, HTTPException):
                        pass
        finally:
            if reusable:
                self._put(conn)
            else:
                conn.close()
