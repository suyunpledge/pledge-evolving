"""Shared model wait policy. Timeouts are socket inactivity limits, not run TTLs."""
from __future__ import annotations

import errno
import http.client
import math
import socket
import ssl
import urllib.error
from contextlib import contextmanager
from contextvars import ContextVar

MODEL_RESPONSE_TIMEOUT = 600.0
GATEWAY_RESPONSE_GRACE = 60.0
METADATA_RESPONSE_TIMEOUT = 10.0
TOOL_RESPONSE_TIMEOUT = 90.0
STREAM_FINISH_GRACE = 5.0
# First, retryable Seed Office continuations get this long between productive SSE
# deltas. The next attempt keeps the provider's ordinary inactivity limit.
SEED_OFFICE_PROGRESS_TIMEOUT = 30.0

# Scoped to one desktop Coding worker. Routers shared with planners and child
# sessions never inherit another task's socket or timing observer.
coding_request = ContextVar('coding_request', default=None)
coding_resume_task = ContextVar('coding_resume_task', default=None)


@contextmanager
def coding_requests(observer, *, resume_task=None):
    from .seed_transport import SeedTransport
    pool = SeedTransport()
    token = coding_request.set((pool, observer))
    resume_token = coding_resume_task.set(resume_task)
    try:
        yield
    finally:
        coding_resume_task.reset(resume_token)
        coding_request.reset(token)
        pool.close()


def request_timeout(value=0, *, default=MODEL_RESPONSE_TIMEOUT):
    """Zero/None inherits the host default; explicit positive limits are honored."""
    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise ValueError('requestTimeout must be a finite nonnegative number of seconds')
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('requestTimeout must be a finite nonnegative number of seconds') from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('requestTimeout must be a finite nonnegative number of seconds')
    return seconds or float(default)


def gateway_client_timeout(upstream=0):
    return request_timeout(upstream) + GATEWAY_RESPONSE_GRACE


def is_timeout(error):
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    return isinstance(error, TimeoutError) or (isinstance(error, OSError)
        and (error.errno == errno.ETIMEDOUT or getattr(error, 'winerror', None) == 10060))


def transient_disconnect(error):
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    return (is_timeout(error)
            or isinstance(error, (ConnectionError, ssl.SSLEOFError, http.client.IncompleteRead))
            or isinstance(error, socket.gaierror) and error.errno == socket.EAI_AGAIN)
