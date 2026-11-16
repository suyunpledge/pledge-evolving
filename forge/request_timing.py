"""Shared model wait policy. Timeouts are socket inactivity limits, not run TTLs."""
from __future__ import annotations

import errno
import math
import urllib.error

MODEL_RESPONSE_TIMEOUT = 600.0
GATEWAY_RESPONSE_GRACE = 60.0
METADATA_RESPONSE_TIMEOUT = 10.0
TOOL_RESPONSE_TIMEOUT = 90.0
STREAM_FINISH_GRACE = 5.0


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
