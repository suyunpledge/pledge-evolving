"""Explicit paid smoke probe; never runs under unittest discovery.

Usage: python -m forge.verify_execution --execute --output report.json
       --ttl-wait 315
Only the three user-authorized Flash models are allowed. Input is generated
probe data, never project/user conversations. Keys stay in the configured
provider registry; reports contain usage, hashes and status, not credentials.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from .config import load_config
from . import model
from .cache_state import CacheWarmth
from .gateway import GatewayConfig, serve
from .request_execution import UsageObserver
from .vendors import normalize_usage

MODELS = ("deepseek-flash", "qwen3.8-flash", "mimo-v2.6-flash")


class TokenBudget:
    def __init__(self, limit):
        self.limit = min(5_000_000, limit)
        self.reserved = 0
        self.observed = 0

    def reserve(self, payload):
        # UTF-8 byte count is a deliberately generous upper bound, with 4096
        # framing tokens; max_tokens includes reasoning for these probes.
        bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 4096 + int(payload["max_tokens"])
        if self.reserved + bound > self.limit:
            raise ValueError("Token budget would be exceeded")
        self.reserved += bound  # Keep reservations even if telemetry is absent.

    def observe(self, usage):
        self.observed += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


def probe(provider, output, *, limit, ttl_wait, shared_path):
    name = provider.default_model
    budget = TokenBudget(limit)
    rows = []
    nonce = uuid.uuid4().hex
    prefix = "Synthetic cache probe. These are test records, not conversation history.\n" + "\n".join(
        f"record {i:04d}: alpha beta gamma delta epsilon {nonce}" for i in range(100))
    payload = {"model": name, "messages": [{"role": "system", "content": prefix},
               {"role": "user", "content": "Reply only OK."}], "max_tokens": 128}
    if name.startswith("qwen"):
        payload["enable_thinking"] = False
    else:
        payload["thinking"] = {"type": "disabled"}
    provider.cache_control = "explicit" if name.startswith("qwen") else "auto"
    provider.expected_calls = 4

    def save():
        report = {"model": name, "endpoint": provider.base_url, "token_limit": budget.limit,
                  "reserved_upper_bound": budget.reserved, "observed_tokens": budget.observed,
                  "prefix_sha256": hashlib.sha256(prefix.encode()).hexdigest(), "requests": rows,
                  "invoice_verified": False, "ttl_wait_seconds": ttl_wait if name.startswith("qwen") else 0}
        file = output / (name + ".json")
        temp = file.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(file)

    def request(kind, via_gateway=False):
        started = time.monotonic()
        row = {"kind": kind, "started_at": time.time()}
        cfg = None; server = None
        try:
            budget.reserve(payload)
            if not via_gateway:
                options = {k: v for k, v in payload.items() if k not in ("model", "messages")}
                text, usage, extra = model.HttpTransport(timeout=90).complete(provider, name, payload["messages"], **options)
                canonical = {"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
                             "cached_tokens": usage.cached_tokens, "cache_write_tokens": usage.cache_write_tokens,
                             "reasoning_tokens": usage.reasoning_tokens}
                row.update(plan=extra["plan"], text_present=bool(text), tool_calls=len(extra.get("tool_calls") or []))
            else:
                cfg = GatewayConfig(upstream=provider.base_url, _credential=provider._credential, port=0,
                    upstream_wire=provider.wire, wire="openai", warmth_path=shared_path,
                    provider_options={"cacheControl": provider.cache_control, "expectedCalls": 4})
                server = serve(cfg)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                streamed = {**payload, "stream": True}
                req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                                             data=json.dumps(streamed).encode(), headers={"content-type": "application/json"})
                observer = UsageObserver(); count = 0; text_present = False
                with urllib.request.urlopen(req, timeout=90) as response:
                    for line in response:
                        observer.feed(line)
                        count += 1
                        if b'"content"' in line:
                            text_present = True
                canonical = normalize_usage(provider.profile_for_model(name), observer.usage)
                row.update(sse_lines=count, text_present=text_present, gateway_planner_observed=any("ADAPT " in s for s in cfg.log.lines))
            row["usage"] = canonical
            row["cache_hit_observed"] = canonical.get("cached_tokens", 0) > 0
            row["status"] = "ok"
            budget.observe(canonical)
        except urllib.error.HTTPError as exc:
            row.update(status="http_error", http_status=exc.code)
        except Exception as exc:
            # Error messages could include upstream response/request data.
            row.update(status="error", error_type=type(exc).__name__)
            import re
            code = re.search(r'"code"\s*:\s*"([A-Za-z_]{1,64})"', str(exc))
            if code:
                row["vendor_error_code"] = code.group(1)
        finally:
            if server:
                server.shutdown(); server.server_close()
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            rows.append(row); save()
            print(json.dumps({"model": name, "kind": kind, "status": row["status"],
                              "usage": row.get("usage"), "observed_tokens": budget.observed}), flush=True)
        return row

    first = request("cold")
    if first["status"] != "ok":
        return
    time.sleep(2)
    request("same_prefix")
    request("gateway_stream", via_gateway=True)
    if name.startswith("qwen") and ttl_wait:
        # The stream may refresh the TTL, so measure from its completion.
        print(json.dumps({"model": name, "status": "waiting_for_ttl", "seconds": ttl_wait}), flush=True)
        time.sleep(ttl_wait)
        request("after_explicit_ttl")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=100_000)
    parser.add_argument("--ttl-wait", type=int, default=0)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    args = parser.parse_args()
    if not args.execute:
        print("Dry run. Add --execute to use the three configured paid model endpoints.")
        return
    if not 1 <= args.max_tokens <= 5_000_000 or args.ttl_wait not in (0, *range(305, 601)):
        parser.error("Budget must be 1–5000000; TTL wait must be 0 or 305–600 seconds")
    # Load GUI's existing secret environment without printing any value.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "forge-gui"))
    from secret_store import env_for
    os.environ.update(env_for())
    repo = Path(__file__).resolve().parent.parent
    cfg = load_config(Path.home() / ".forge", bundles=sorted((repo / "bundles").glob("*.json")))
    providers = model.ModelRouter.from_config(cfg).providers.values()
    selected = {m: next((p for p in providers if p.default_model == m and p.api_key), None) for m in args.models}
    if any(p is None for p in selected.values()):
        raise SystemExit("Missing configured target provider/key; no requests sent")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        shared_path = Path(tmp) / "cache.json"
        model._WARMTH = CacheWarmth(shared_path)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(probe, p, args.output, limit=args.max_tokens, ttl_wait=args.ttl_wait,
                                   shared_path=shared_path) for p in selected.values()]
            for future in futures:
                future.result()


if __name__ == "__main__":
    main()
