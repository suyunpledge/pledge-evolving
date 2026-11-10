"""Cache observations with explicit denominators; never changes billing totals."""
from __future__ import annotations

import copy
import math


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value >= 0 else None


def annotate_requests(requests, revision, transaction_id, seen):
    rows = copy.deepcopy(requests)
    for row in rows:
        if row.get("stage") == "integrate":
            continue  # The judge receives a different prompt, not rewritten history.
        key = (row.get("provider"), row.get("model"))
        row["context_revision"] = revision
        if revision and seen.get(key, 0) < revision:
            row["cache_rebuild"] = {"reason": "compaction", "transaction_id": transaction_id,
                                    "revision": revision}
        seen[key] = revision
    return rows


def cache_coverage(requests):
    totals = {name: {"requests": 0, "input_tokens": 0, "cached_tokens": 0}
              for name in ("all_observed", "rebuild", "non_rebuild")}
    prefix = {"requests": 0, "input_tokens": 0, "cached_tokens": 0, "excluded_tail_tokens": 0}
    unknown = invalid = duplicate = 0
    seen = set()
    for request in requests:
        if not isinstance(request, dict):
            invalid += 1
            continue
        # Only explicit request + attempt identities permit deduplication.
        identity = (request.get("request_id"), request.get("attempt_id"))
        if all(isinstance(v, str) and v for v in identity):
            identity = (str(request.get("provider") or ""), str(request.get("model") or ""), *identity)
            if identity in seen:
                duplicate += 1
                continue
            seen.add(identity)
        usage = request.get("usage") or {}
        if not isinstance(usage, dict):
            invalid += 1
            continue
        prompt, cached = _count(usage.get("prompt_tokens")), _count(usage.get("cached_tokens"))
        if usage.get("cache_observed") is False or prompt is None or cached is None:
            unknown += 1
            continue
        if cached > prompt:
            invalid += 1
            continue
        bucket = "rebuild" if request.get("cache_rebuild") else "non_rebuild"
        for name in ("all_observed", bucket):
            totals[name]["requests"] += 1
            totals[name]["input_tokens"] += prompt
            totals[name]["cached_tokens"] += cached
        plan = request.get("plan")
        caps = plan.get("route_capabilities") if isinstance(plan, dict) else None
        caps = caps if isinstance(caps, dict) else {}
        # Opt-in route evidence only; never guessed from vendor/model names.
        if caps.get("cacheTailRule") == "deepseek-128":
            tail = min(prompt, 128 + prompt % 128, prompt - cached)
            prefix["requests"] += 1
            prefix["input_tokens"] += prompt - tail
            prefix["cached_tokens"] += cached
            prefix["excluded_tail_tokens"] += tail
    for bucket in [*totals.values(), prefix]:
        bucket["hit_rate"] = (round(bucket["cached_tokens"] / bucket["input_tokens"], 4)
                              if bucket["input_tokens"] else None)
    return {**totals, "eligible_prefix": prefix, "unknown_requests": unknown,
            "invalid_requests": invalid, "duplicate_observations": duplicate,
            "denominator": "observed input tokens; eligible_prefix excludes declared structural tail"}


def foreground_hit_rate(completion):
    """压缩决策只用单个前台请求的真实观测，不混入 MoA/评审的另一套上下文。"""
    requests = getattr(completion, 'requests', ())
    if (getattr(completion, 'aggregated', False)
            or not isinstance(requests, (list, tuple)) or len(requests) != 1
            or not isinstance(requests[0], dict)
            or requests[0].get('stage') in {'integrate', 'candidate', 'review'}):
        return None
    return cache_coverage(requests)['all_observed']['hit_rate']
