"""Explicit, configured phase models; never silently change a chosen vendor."""
from .model import ModelRouter


def model_pair(value):
    if value is None or value == '':
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in value)):
        raise ValueError('Phase model must be [configured provider, model] or empty')
    return tuple(value)


def phase_router(catalog, selection, fallback):
    pair = model_pair(selection)
    if pair is None:
        return fallback
    provider = catalog.providers.get(pair[0])
    if provider is None or pair[1] not in {provider.default_model, provider.small_model, *provider.models}:
        raise ValueError('Phase model is no longer configured; select a configured provider/model')
    # Retain the existing HTTP adapter, Secret gate, rate limiter and receipts.
    # One selected provider; no premium/MoA/fallback to an unselected vendor.
    return ModelRouter([provider], transport=catalog.transport, primary=pair,
                       retries_per_provider=catalog.retries_per_provider)
