"""Cache request extensions for verified upstream endpoints."""
import hashlib
import json
from urllib.parse import urlsplit

from .context_plan import estimate_text


GPT61_MODELS = frozenset({"gpt-6.1-sol", "gpt-6.1-terra"})


def official_openai_base(address, wire):
    """Resolve official API roots and the full Responses URL entered in model settings."""
    endpoint = urlsplit(address)
    if (wire == "openai"
            and endpoint.scheme == "https" and endpoint.netloc.lower() in {"api.openai.com", "api.openai.com:443"}
            and endpoint.path.rstrip("/") in {"", "/v1", "/v1/responses"} and not (endpoint.query or endpoint.fragment)):
        return endpoint._replace(path="/v1").geturl()
    return None


def official_openai_endpoint(provider):
    return official_openai_base(provider.base_url, provider.wire) is not None


def apply_openai_breakpoint(payload, provider, model):
    """Keep the first long system block reusable even if later context changes."""
    mode = (provider.cache_control or "auto").strip().lower()
    if (model not in GPT61_MODELS or not official_openai_endpoint(provider)
            or mode not in {"auto", "explicit"} or not provider.adapt
            or "prompt_cache_options" in payload or "prompt_cache_retention" in payload):
        return None
    if mode == "auto" and provider.expected_calls < 2:
        return None

    messages = payload.get("messages")
    roles = {"system", "developer"}
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        return None
    first = messages[0]
    if first.get("role") not in roles:
        return None
    content = first.get("content")
    if isinstance(content, str):
        chunks = [{"type": "text", "text": content}]
    elif isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "text":
        chunks = content
    else:
        return None
    later_system = len(messages) > 1 and isinstance(messages[1], dict) and messages[1].get("role") in roles
    if mode == "auto" and len(chunks) == 1 and not later_system:
        return None
    text = chunks[0].get("text")
    if not isinstance(text, str) or estimate_text(text) < 1024:
        return None
    # The caller owns any explicit cache layout it already sent.
    for message in messages:
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, list) and any(isinstance(block, dict) and "prompt_cache_breakpoint" in block
                                            for block in blocks):
            return None

    marker = {**chunks[0], "prompt_cache_breakpoint": {"mode": "explicit"}}
    payload["messages"] = [{**first, "content": [marker, *chunks[1:]]}, *messages[1:]]
    wire_mode = "explicit" if mode == "explicit" else "implicit"
    payload["prompt_cache_options"] = {"mode": wire_mode, "ttl": "30m"}
    stable = {**payload, "messages": [{**first, "content": [marker]}]}
    return stable


def apply_affinity(payload, provider, model, scope):
    # This exact deployment accepted the field in the live schema probe.
    # Other aliases and reseller deployments keep their existing wire.
    endpoint=urlsplit(provider.base_url)
    supported=(provider.wire=='openai' and model=='doubao-seed-evolving'
        and endpoint.scheme=='https' and endpoint.hostname=='ark.cn-beijing.volces.com'
        and endpoint.path.rstrip('/')=='/api/coding/v3' and not endpoint.query)
    if not supported or not provider.adapt or provider.cache_affinity=='off' or scope is None:
        return None
    if 'prompt_cache_key' in payload:
        return {'status':'caller_supplied','guaranteed_hit':False}
    if not isinstance(scope,str) or not 1<=len(scope)<=512:
        raise ValueError('cache_scope must be a bounded nonempty host string')
    identity=[scope,provider._credential._account_fingerprint,provider.base_url.rstrip('/'),model,
        {k:payload[k] for k in ('thinking','reasoning_effort','thinking_budget') if k in payload},
        payload.get('tools')]
    encoded=json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    key='revon-'+hashlib.sha256(encoded).hexdigest()[:48]
    payload['prompt_cache_key']=key
    return {'status':'routing_hint','source':'host_scope','key_digest':hashlib.sha256(key.encode()).hexdigest(),
            'guaranteed_hit':False}
