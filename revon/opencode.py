"""OpenCode Console's plan-specific inference and model discovery endpoints."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from urllib.parse import urlsplit
from uuid import uuid4

from . import __version__


ORIGIN = 'https://opencode.ai'
GO = ORIGIN + '/inference/go'
ZEN = ORIGIN + '/inference'
USER_AGENT = 'revon/' + __version__
_conversation = ContextVar('opencode_conversation', default='')


def plan_root(address):
    """Recognize current and legacy public API paths on the exact HTTPS origin."""
    try:
        url = urlsplit(address)
        if url.scheme != 'https' or url.netloc.lower() not in ('opencode.ai', 'opencode.ai:443'):
            return ''
    except (TypeError, ValueError):
        return ''
    parts = url.path.strip('/').split('/')
    if parts[0] not in ('inference', 'zen') or any(p in ('', '.', '..') for p in parts):
        return ''
    go = parts[1:2] == ['go']
    rest = parts[2:] if go else parts[1:]
    if rest and rest[0] not in ('v1', 'openai', 'anthropic', 'google', 'systemone'):
        return ''
    return GO if go else ZEN


def model_list_url(address):
    root = plan_root(address)
    if root:
        # Only remap standard API bases. Keep custom proxy paths as entered.
        path = urlsplit(address).path.rstrip('/')
        base = '/inference/go' if root == GO else '/inference'
        for prefix in (base, base.replace('/inference', '/zen', 1)):
            if path in {prefix + suffix for suffix in ('', '/v1', '/openai/v1', '/anthropic', '/anthropic/v1')}:
                return root + '/v1/models'
    return ''


def anthropic_base(address, wire):
    if wire == 'anthropic' and plan_root(address):
        base = address.rstrip('/')
        return base if base.endswith('/v1') else base + '/v1'
    return ''


@contextmanager
def conversation(identifier):
    token = _conversation.set(identifier)
    try:
        yield
    finally:
        _conversation.reset(token)


def request_headers(address, identifier=None):
    if not plan_root(address):
        return {}
    scope = identifier or _conversation.get() or uuid4().hex
    public_id = hashlib.sha256(('revon:opencode:' + scope).encode()).hexdigest()[:32]
    return {'User-Agent': USER_AGENT, 'x-opencode-session': 'ses_' + public_id}
