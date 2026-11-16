"""Build a secret-free model capability catalogue from local receipts, offline.

Run from any directory: python scripts/model_fingerprints.py --output PATH
This never discovers credentials, sends requests or promotes observations to
runtime rules. New rules remain explicit, reviewed model-kind registry edits.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge.model_kinds import MODEL_KINDS, resolve_model_kind
from forge.vendors import PROFILES, normalize_usage, profile_for


def endpoint_scope(value):
    parts = urlsplit(str(value or ""))
    # Omit query, fragment, userinfo and any non-HTTP scope.
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname
    if ":" in host:
        host = "[" + host + "]"
    return urlunsplit((parts.scheme, host + (f":{parts.port}" if parts.port else ""), parts.path, "", ""))


def request_summary(request, chosen):
    profile = profile_for(model=chosen)
    parameters = request.get("parameters") or request
    controls = {key: parameters[key] for key in ("reasoning_effort", "max_tokens", "enable_thinking")
                if key in parameters and isinstance(parameters[key], (str, int, bool))}
    thinking = parameters.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in {"enabled", "disabled", "adaptive", "auto"}:
        controls["thinking"] = {"type": thinking["type"]}
    usage = request.get("usage") or {}
    result = {"controls": controls, "usage": normalize_usage(profile, usage),
              "response_received": any(key in request for key in ("usage", "finish_reason", "status"))}
    # These fields come from sanitised receipts; never copy message bodies,
    # reasoning content, arbitrary headers, authentication or request IDs.
    for key in ("status", "finish_reason", "tool_names", "content_type"):
        if key in request:
            result[key] = request[key]
    if isinstance(usage.get("credit"), (int, float)):
        result["observed_channel_credit"] = usage["credit"]
    return result


def collect_receipts(directory):
    observations = {key: [] for key in MODEL_KINDS}

    def add(chosen, receipt):
        kind, source = resolve_model_kind(chosen)
        # A dated family match is not a direct observation of the canonical ID.
        if kind and source == "model_id":
            observations[kind.id].append({"requested_model": chosen, **receipt})

    for path in sorted([*directory.glob("forge-wire-*.json"), *directory.glob("forge-effort-*.json")]):
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        for index, record in enumerate(raw.get("records", [])):
            for offset, request in enumerate(record.get("requests", [])):
                chosen = str(record.get("model") or "")
                receipt = {"source": path.name, "record": index, "request": offset,
                           "scope": endpoint_scope(record.get("endpoint")),
                           "scenario_passed": record.get("ok"), **request_summary(request, chosen)}
                if record.get("error_type"):
                    receipt["error_type"] = record["error_type"]
                    receipt["error_category"] = ("model_rejected_on_this_endpoint"
                        if "Not supported model" in str(record.get("error_detail")) else "request_failed")
                add(chosen, receipt)

    for path in sorted(directory.glob("workbuddy-hy4-tools*.json")):
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        for index, request in enumerate(raw.get("requests", [])):
            chosen = str(request.get("requested_model") or "")
            add(chosen, {"source": path.name, "request": index, "scope": "workbuddy-broker",
                         "scenario_passed": raw.get("ok"), **request_summary(request, chosen)})

    # Previous investigator's receipts are useful provenance; only normalised
    # metrics are copied, never their captured message/reasoning previews.
    for name in ("vendor-probe-results.json", "vendor-probe-mimo.json"):
        path = directory / name
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        sections = [raw] if name == "vendor-probe-mimo.json" else list(raw.values())
        for section in sections:
            scope = endpoint_scope(section.get("_base") or (section.get("_endpoint") or {}).get("base"))
            for scenario, request in section.items():
                if not isinstance(request, dict) or not request.get("model_echo"):
                    continue
                chosen = str(request["model_echo"])
                add(chosen, {"source": name, "scenario": scenario, "scope": scope,
                             "provenance": "archived_probe", "scenario_passed": request.get("ok"),
                             **request_summary(request, chosen)})
    return observations


def build_catalogue(directory):
    observations = collect_receipts(directory)
    rows = []
    for key, kind in sorted(MODEL_KINDS.items()):
        profile = profile_for(model_kind_id=key)
        rows.append({"model_kind_id": key, "manufacturer_id": kind.manufacturer_id,
                     "family_id": kind.family_id, "canonical_model": kind.canonical_model,
                     "aliases": list(kind.aliases), "family_prefixes": list(kind.prefixes),
                     "evidence_grade": kind.evidence,
                     "behavior_fingerprint": profile.behavior_fingerprint,
                     "traits": list(kind.traits), "effective_profile": profile.to_raw(),
                     "observations": observations[key]})
    manufacturers = sorted({kind.manufacturer_id for kind in MODEL_KINDS.values()})
    counts = Counter(kind.manufacturer_id for kind in MODEL_KINDS.values())
    return {"schema_version": 1, "mode": "offline_receipt_catalogue",
            "limits": ["Capability identity is not proof of remote model weights or authorship.",
                       "Behaviour fingerprints do not merge model/endpoint/account cache namespaces.",
                       "Registry/configured traits are not live verification; family matches inherit defaults.",
                       "Channel restrictions and observed credit apply only to the named scope.",
                       "Short probes do not establish steady-state cache hit rates or quality rankings.",
                       "Scenario failures are retained; no runtime rules are learned from one answer."],
            "manufacturer_defaults": [{"manufacturer_id": key, "registered_kinds": counts[key],
                                       "scope": "fallback_defaults_not_universal_guarantees",
                                       "profile": PROFILES[key].to_raw()} for key in manufacturers],
            "model_kinds": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "docs/vendor-adapt-2026-11-04")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalogue = build_catalogue(args.evidence_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model_kinds": len(catalogue["model_kinds"]),
                      "manufacturers": len(catalogue["manufacturer_defaults"]),
                      "observations": sum(len(row["observations"]) for row in catalogue["model_kinds"]),
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
