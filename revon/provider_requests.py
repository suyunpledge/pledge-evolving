"""Local aggregate of upstream model request attempts, recorded at transport open."""
from contextlib import closing, contextmanager, ExitStack
import http.client
import ipaddress
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
from urllib.parse import urlsplit

from .secrets import redact


_FILE = 'provider-requests.sqlite3'
# Unknown path segments can be API keys, account identifiers or deployment names.
_API_SEGMENTS = frozenset('api bypass openai compatible-mode openai-compatible anthropic beta chat completions '
                          'responses messages models llm paas coding plan step_plan aitokenapi hunyuan ark '
                          'inference openai-compatible-mode rest deployments endpoints model gateway'.split())
_VERSION = re.compile(r'v[0-9]{1,2}(?:beta[0-9]?|alpha[0-9]?)?\Z')


def safe_url(value):
    """Display origin and public API path structure. Drop credentials, queries and fragments."""
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value):
        return ''
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme not in ('http', 'https') or not host or redact(host) != host:
            return ''
        if ':' in host:
            if '%' in host:
                return ''
            host = '[' + str(ipaddress.IPv6Address(host)) + ']'
        elif not re.fullmatch(r'[a-z0-9.-]+', host):
            return ''
        origin = f'{parts.scheme}://{host}'
        if parts.port is not None:
            origin += f':{parts.port}'
    except (TypeError, ValueError):
        return ''
    public_segments = _API_SEGMENTS
    if origin in ('https://opencode.ai', 'https://opencode.ai:443'):
        public_segments = public_segments.union(('zen', 'go', 'google', 'systemone'))
    segments = []
    for segment in filter(None, parts.path.split('/')):
        if (segment in public_segments or _VERSION.fullmatch(segment)) and redact(segment) == segment:
            segments.append(segment)
        elif segments[-1:] == ['models'] and segment.rpartition(':')[2] in ('generateContent', 'streamGenerateContent'):
            segments.append('…:' + segment.rpartition(':')[2])
        else:
            segments.append('…')
    return '/'.join([origin, *segments])


def _record(home, provider_id, base_url, request_url, source, status):
    if provider_id is None and source == 'gateway':
        provider_id = ''  # An independently started gateway can prove its address, but not a settings row.
    if (home is None or source not in ('gateway', 'direct') or not isinstance(provider_id, str)
            or not (re.fullmatch(r'[\w.-]{1,128}', provider_id) or not provider_id and source == 'gateway')
            or redact(provider_id) != provider_id):
        return
    base, target = safe_url(base_url), safe_url(request_url)
    if not base or not target:
        return
    try:
        folder = Path(home)
        folder.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(folder / _FILE, timeout=1)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                provider_id TEXT, base_url TEXT, request_url TEXT, source TEXT,
                http_status INTEGER, last_seen REAL, attempts INTEGER,
                PRIMARY KEY (provider_id, base_url, request_url, source))""")
            db.execute("""INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(provider_id, base_url, request_url, source) DO UPDATE SET
                attempts = attempts + 1,
                http_status = CASE WHEN excluded.last_seen >= last_seen THEN excluded.http_status ELSE http_status END,
                last_seen = MAX(last_seen, excluded.last_seen)""",
                       (provider_id, base, target, source, status, time.time()))
            db.execute("""DELETE FROM requests WHERE rowid IN
                (SELECT rowid FROM requests ORDER BY last_seen DESC, rowid DESC LIMIT -1 OFFSET 512)""")
    except (OSError, sqlite3.Error):
        pass  # Observation must never fail or replay the model request.


def recent_requests(home, *, limit=100):
    """List recent distinct routes without creating a database or contacting a provider."""
    path = Path(home) / _FILE
    try:
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.15)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                """SELECT provider_id, base_url, request_url, source, http_status, last_seen, attempts
                FROM requests ORDER BY last_seen DESC, rowid DESC LIMIT ?""", (max(0, min(int(limit), 512)),))]
    except (OSError, sqlite3.Error):
        return []


@contextmanager
def open_recorded(opener, request, *, home, provider_id, base_url, source, **options):
    """Count the initial request target and its observed HTTP status, before response body decoding."""
    with ExitStack() as stack:
        try:
            response = stack.enter_context(opener(request, **options))
        except urllib.error.HTTPError as exc:
            _record(home, provider_id, base_url, request.full_url, source, exc.code)
            raise
        except (OSError, http.client.HTTPException):
            _record(home, provider_id, base_url, request.full_url, source, None)
            raise
        _record(home, provider_id, base_url, request.full_url, source, getattr(response, 'status', None))
        yield response
