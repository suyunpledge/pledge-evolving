"""Small opt-in live probe through Forge's own transport, with no fallback chain.

Credentials are read locally; only a file path/key name is accepted on argv.
Each model gets at most three requests: tool call, replay, and an on/effort probe.
The endpoint list intentionally excludes the GLM and Qnaigc paid paths.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forge import model
from forge.cache_state import CacheWarmth
from forge.secret_http import open_authenticated
from forge.secrets import redact


TARGETS = {
    "deepseek": ("https://api.deepseek.com", "FORGE_DEEPSEEK_KEY",
                 ("deepseek-flash", "deepseek-v4-pro")),
    "minimax": ("https://api.minimaxi.com/v1", "FORGE_MINIMAX_KEY",
                ("minimax-m3", "minimax-m2.7-highspeed")),
    "mimo": ("https://token-plan-cn.xiaomimimo.com/v1", "",
             ("mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed")),
    "volcengine": ("https://ark.cn-beijing.volces.com/api/coding/v3", "",
                   ("doubao-seed-evolving", "doubao-seed-2.1-turbo")),
}
TOOL = {"type": "function", "function": {"name": "add", "description": "Add two integers.",
        "parameters": {"type": "object", "properties": {"a": {"type": "integer"},
        "b": {"type": "integer"}}, "required": ["a", "b"]}}}


def run_probe(vendor, *, credential_file, credential_name="", effort_only=False, chosen_model=""):
    base, default_key, models = TARGETS[vendor]
    if chosen_model:
        if chosen_model not in models:
            raise ValueError("Model is not in this vendor's verified endpoint list")
        models = (chosen_model,)
    credentials = json.loads(credential_file.read_text(encoding="utf-8-sig"))
    value = credentials.get(credential_name or default_key)
    if not isinstance(value, str) or not value:
        raise ValueError("Credential is missing; provide its file and key name")
    provider = model.Provider(vendor, base, value, default_model=models[0])
    records = []

    for chosen in models:
        row = {"vendor": vendor, "model": chosen, "endpoint": base, "requests": []}
        records.append(row)

        def opener(request, **kwargs):
            body = json.loads(request.data)
            row["requests"].append({"url": request.full_url, "parameters": {
                k: body[k] for k in ("model", "thinking", "enable_thinking", "reasoning_effort",
                                     "max_tokens", "temperature", "tool_choice") if k in body},
                "message_roles": [m.get("role") for m in body.get("messages", [])]})
            return open_authenticated(request, **kwargs)

        router = model.ModelRouter([provider], transport=model.HttpTransport(timeout=60, response_opener=opener),
                                   chain=[(vendor, chosen)], retries_per_provider=0)

        def call(messages, **kwargs):
            started = time.monotonic()
            completion = router.complete(messages, max_tokens=256, **kwargs)
            receipt = row["requests"][-1]
            receipt.update(seconds=round(time.monotonic() - started, 2),
                           usage=vars(completion.usage), finish_reason=completion.finish_reason,
                           tool_names=[c["name"] for c in completion.tool_calls],
                           text=completion.text[:120], adaptation=completion.plan.get("actions", []))
            return completion

        try:
            if effort_only:
                levels = ("low", "medium", "high") if vendor == "minimax" else ("high",)
                results = []
                for effort in levels:
                    completion = call([{"role": "user", "content":
                        "Compare 9.11 and 9.9 as decimals. Reply with only the larger number."}],
                        reasoning_effort=effort)
                    results.append(completion.finish_reason not in {"length", "max_tokens"}
                                   and "9.9" in completion.text)
                row["wire_ok"] = True
                row["answer_checks"] = results
                row["ok"] = all(results)
                continue
            messages = [{"role": "system", "content": "Use add once when asked. Be concise."},
                        {"role": "user", "content": "Call add with a=17,b=25. Do not calculate without the tool."}]
            first = call(messages, enable_thinking=False, tools=[TOOL],
                         tool_choice=('auto' if vendor == 'mimo' else
                                      {"type": "function", "function": {"name": "add"}}))
            calls = first.tool_calls
            if (first.finish_reason in {"length", "max_tokens"} or len(calls) != 1
                    or calls[0]["name"] != "add" or calls[0]["args"] != {"a": 17, "b": 25}):
                row["ok"] = False
                row["error"] = "Expected one complete add(17,25) call"
                continue
            messages += [first.assistant_message, {"role": "tool", "tool_call_id": calls[0]["id"],
                                                   "content": "42"}]
            second = call(messages, enable_thinking=False, tools=[TOOL])
            row["replay_ok"] = "42" in second.text and second.finish_reason not in {"length", "max_tokens"}
            third = call([{"role": "user", "content": "What is 2+2? Reply with the number only."}],
                         **({"thinking": {"type": "enabled"}} if vendor == "minimax"
                            else {"enable_thinking": True}))
            row["on_probe_complete"] = third.finish_reason not in {"length", "max_tokens"}
            row["ok"] = row["replay_ok"] and row["on_probe_complete"] and "4" in third.text
        except Exception as exc:
            # Deliberately do not print raw errors/headers or discover another key.
            row.update(ok=False, error_type=type(exc).__name__, error_detail=redact(str(exc))[:600])
        finally:
            print(json.dumps({k: v for k, v in row.items() if k != "requests"}, ensure_ascii=False), flush=True)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--vendor", choices=TARGETS, required=True)
    parser.add_argument("--credential-file", type=Path, default=Path.home() / ".forge" / "keys.json")
    parser.add_argument("--credential-name", default="")
    parser.add_argument("--effort-only", action="store_true")
    parser.add_argument("--model", default="", help="probe only one model from the selected vendor list")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("Live calls require --live; use forge.test_vendor_wire for offline tests")
    if args.effort_only and args.vendor not in {"deepseek", "minimax"}:
        parser.error("Effort probes are verified only for DeepSeek and MiniMax")
    # No shared home/store writes. Model calls themselves are the only external effect.
    with tempfile.TemporaryDirectory() as tmp:
        model._WARMTH = CacheWarmth(Path(tmp) / "warmth.json")
        records = run_probe(args.vendor, credential_file=args.credential_file,
                            credential_name=args.credential_name, effort_only=args.effort_only,
                            chosen_model=args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(redact({"mode": "live", "records": records}),
                                     ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row["ok"] for row in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
