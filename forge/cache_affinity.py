"""Verified Ark Coding Plan cache routing hints; no context or permission changes."""
import hashlib
import json
from urllib.parse import urlsplit


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
    key='forge-'+hashlib.sha256(encoded).hexdigest()[:48]
    payload['prompt_cache_key']=key
    return {'status':'routing_hint','source':'host_scope','key_digest':hashlib.sha256(key.encode()).hexdigest(),
            'guaranteed_hit':False}
