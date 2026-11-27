"""A bounded, session-local summary of completed model requests for the desktop footer."""
from revon.cache_metrics import cache_coverage
from revon.model import Provider
from revon.vendors import normalize_usage, profile_for


def context_limit(config):
    model = config.get('model', '')
    try:
        provider = Provider(name='', base_url=config.get('baseURL', ''), api_key='',
                            route_capabilities=config.get('routeCapabilities', {}),
                            model_capabilities=config.get('modelCapabilities', {}),
                            adapt=config.get('adapt', True))
        return provider.capabilities_for_model(model).get('contextWindow')
    except (TypeError, ValueError, AttributeError):
        return None


COUNTS = ('requests', 'input', 'output', 'inputReports', 'outputReports',
          'cached', 'cacheInput', 'cacheReports', 'context', 'limit')


def saved(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in COUNTS if type(value.get(key)) is int and value[key] >= 0}


def add_request(previous, raw, *, model='', window=None, executor=True):
    out = saved(previous)
    raw = raw if isinstance(raw, dict) else {}
    counts = normalize_usage(profile_for(model=model), raw)
    out['requests'] = out.get('requests', 0) + 1
    for field in ('input', 'output'):
        if raw.get(field + '_observed') is False:
            counts.pop(field + '_tokens', None)
        n = counts.get(field + '_tokens')
        if n is not None:
            out[field] = out.get(field, 0) + n
            reports = field + 'Reports'
            out[reports] = out.get(reports, 0) + 1
    prompt = counts.get('input_tokens')
    if executor:
        for key, n in (('context', prompt), ('limit', window)):
            if type(n) is int and n >= 0:
                out[key] = n
            else:
                out.pop(key, None)
    sample = cache_coverage([{'usage': {'prompt_tokens': prompt, 'cached_tokens': counts.get('cached_tokens'),
                                        'cache_observed': raw.get('cache_observed')}}])['all_observed']
    if sample['requests']:
        for key, value in (('cached', sample['cached_tokens']), ('cacheInput', sample['input_tokens']),
                           ('cacheReports', 1)):
            out[key] = out.get(key, 0) + value
    return out


def add_report(previous, report, *, executor=True):
    """Core reports retain each constituent request, including its own model and budget."""
    out = previous
    for row in (report or {}).get('requests', ()):
        if isinstance(row, dict):
            budget = (row.get('plan') or {}).get('request_budget') or {}
            out = add_request(out, row.get('usage'), model=row.get('model') or '',
                              window=budget.get('context_window'),
                              executor=executor and row.get('phase') not in ('thinking', 'planning', 'reflection')
                              and row.get('stage') not in ('plan', 'planning', 'integrate'))
    return out
