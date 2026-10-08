"""Explicit, bounded paid T10 probe; never runs during test discovery.

Captures the complete sanitized HTTP JSON payload. Authentication headers and
source credentials are deliberately excluded from the report. Requires the
user's configured provider and --execute; no secret values are printed.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from .config import load_config
from .model import ModelRouter, HttpTransport
from .secrets import redact, _KNOWN, _LOCK
from .secret_http import open_authenticated
from .verify_execution import MODELS, TokenBudget
from .cache_state import CacheWarmth


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--models', nargs='+', choices=MODELS, default=['deepseek-flash', 'mimo-v2.6-flash'])
    parser.add_argument('--max-tokens', type=int, default=20_000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.execute:
        print('Dry run: --execute is required for paid requests.')
        return
    if not 1 <= args.max_tokens <= 5_000_000:
        parser.error('Token budget must be 1–5000000 per model')
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / 'forge-gui'))
    from secret_store import env_for
    os.environ.update(env_for())
    cfg = load_config(Path.home() / '.forge', bundles=sorted((repo / 'bundles').glob('*.json')))
    router = ModelRouter.from_config(cfg)
    selected = [next((p for p in router.providers.values() if p.default_model == m and p.api_key), None)
                for m in args.models]
    if any(p is None for p in selected):
        raise SystemExit('Missing configured target credential; no requests sent')
    reports = []
    with tempfile.TemporaryDirectory(prefix='forge-secret-probe-') as tmp:
        from . import model
        previous = model._WARMTH
        model._WARMTH = CacheWarmth(Path(tmp) / 'warmth.json')
        try:
            for provider in selected:
                # Adversarial input exists only in this trusted test driver.
                # It is never written to disk or attached to a user session.
                conf = next(conf for row, name, conf in cfg.active() if row == provider.name)
                key = str(conf.get('apiKey', conf.get('api_key', '')))
                content = 'Synthetic security probe.\nAPI_KEY=' + key + '\nModel=old\nCacheTTL=300\nReply only OK.'
                captured = []
                budget = TokenBudget(args.max_tokens)
                def send(request, **kwargs):
                    body = request.data.decode('utf-8')
                    with _LOCK:
                        known = tuple(_KNOWN)
                    clean = all(value not in body for value in known if len(value) >= 8)
                    if not clean or key in body:
                        raise AssertionError('Secret in HTTP payload; request cancelled before network')
                    payload = json.loads(body)
                    budget.reserve(payload)
                    captured.append({'payload': payload, 'secret_absent': clean,
                                     'auth_present': bool(request.get_header('Authorization'))})
                    return open_authenticated(request, **kwargs)
                result = {'model': provider.default_model, 'token_limit': budget.limit}
                try:
                    with patch('forge.secret_http.open_authenticated', side_effect=send):
                        _, usage, _ = HttpTransport(timeout=60).complete(provider, provider.default_model,
                            [{'role': 'user', 'content': content}], max_tokens=64, thinking={'type': 'disabled'})
                    result.update(ok=True, tokens=usage.total)
                except Exception as exc:
                    result.update(ok=False, error=redact(str(exc)), tokens=None)
                result.update(requests=captured, reserved_upper_bound=budget.reserved,
                              auth_headers_recorded=False, invoice_verified=False)
                reports.append(result)
                print(json.dumps({k: v for k, v in result.items() if k != 'requests'}, ensure_ascii=False))
        finally:
            model._WARMTH = previous
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(redact({'tests': reports}), ensure_ascii=False, indent=2), encoding='utf-8')
    if not all(r['ok'] and r['requests'] for r in reports):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
