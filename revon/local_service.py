"""Local inference service profiles — adaptations gated on the engine.

Why a gate exists
-----------------
A few behaviours that make a *self-hosted* inference engine usable are simply
wrong for a *cloud* provider. Measured on 2026-09-21 against Ollama 0.34.0
serving qwen3:8b:

1. **The native chat surface rejects a replayed tool history.** Feeding the
   model's own ``tool_calls`` back into ``/api/chat`` returns HTTP 400
   (``Value looks like object, but can't find closing '}' symbol``); the same
   body to the OpenAI-compatible surface (``/v1/chat/completions``) works. A
   multi-turn tool loop therefore has to use the OpenAI-compatible path.
2. **Contemplation can fail to converge.** The same model that reasons its way
   out of a normal task can loop indefinitely on a question whose premise is
   unusual (measured: 420 s, no answer, 3000-token cap hit). A bounded output
   budget is the cheap mitigation.

Neither rule may leak into a cloud provider. ``https://api.deepseek.com``
already expects ``/chat/completions`` and carries its own base path; rewriting
it to ``/v1/chat/completions`` would break a working provider. ``llamacpp`` and
``mnn`` sit on the same side of the line as ``ollama`` — self-hosted, served
from ``localhost``, OpenAI-compatible surface available — so all three are
treated as *local services* and get the adaptations; everything else is left
exactly as configured.

Provider config::

    {"id": "local", "name": "provider:local",
     "config": {"service": "ollama",          # ollama | llamacpp | mnn
                "wire": "openai",
                "baseURL": "http://127.0.0.1:11434",
                "model": "qwen3:8b"}}

``service`` is optional. Absent or unrecognised means "not a local service",
which keeps every pre-existing provider row byte-identical in behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .thinking import normalise_thinking_mode

# Canonical service ids. Anything else is "not local" by definition.
LOCAL_SERVICES: tuple[str, ...] = ("ollama", "llamacpp", "mnn")

# Accepted spellings → canonical id. Keys are lowercased, stripped forms.
#
# ``"llama"`` is kept deliberately, though a bare "llama" is ambiguous (it is
# also a model family name). The asymmetry decides it: failing to gate a real
# local engine hands it ``/chat/completions``, which Ollama resets — a hard
# failure — whereas over-gating needs someone to put an odd string like
# ``"proxy/llama"`` in a field whose value is meant to be an engine id anyway.
SERVICE_ALIASES: dict[str, str] = {
    "ollama": "ollama",
    "llama.cpp": "llamacpp",
    "llama_cpp": "llamacpp",
    "llamacpp": "llamacpp",
    "llama-server": "llamacpp",
    "llamaserver": "llamacpp",
    "llama": "llamacpp",
    "mnn": "mnn",
    "mnn-llm": "mnn",
    "mnn_llm": "mnn",
    "mnnllm": "mnn",
}

# Paths. Cloud providers keep the historical shape; local engines get the
# OpenAI-compatible surface, which is the only one that accepts a replayed
# tool-call history.
CLOUD_CHAT_PATH = "/chat/completions"
OPENAI_COMPAT_CHAT_PATH = "/v1/chat/completions"

# Output budget applied to a local engine's contemplation rounds. A ceiling,
# not a target: a smaller explicitly configured value wins.
LOCAL_THINKING_CAP = 2048


# Separators that may appear around a service id but never *inside* one.
# Deliberately excludes "-" and ".": aliases use them (llama.cpp, mnn-llm), so
# splitting on them would let a typo ("llama-cpp-typo") match an alias.
_TOKEN_SPLIT = re.compile(r"[\s/:()\[\],;=]+")


def normalise_service(value: Any) -> str:
    """Resolve a configured service string to a canonical id, or ``""``.

    Unknown values normalise to ``""`` (not-local) rather than raising — a
    typo must never silently switch a cloud provider onto local-only paths.

    Matching surface: the *whole* value is tried first, then each token after
    splitting on ``/ : ( ) [ ] , ; =`` and whitespace. So ``"ollama:11434"`` and
    ``"http://127.0.0.1/mnn"`` resolve (the ``/`` split is what makes a
    host-embedded name findable), while ``"llama-cpp-typo"`` does not — ``-``
    and ``.`` are deliberately NOT separators, so a mangled alias stays whole
    and unmatched instead of being chopped into a valid one.
    """
    key = str(value or "").strip().lower()
    if not key:
        return ""
    if key in SERVICE_ALIASES:
        return SERVICE_ALIASES[key]
    for token in _TOKEN_SPLIT.split(key):
        token = token.strip()
        if token in SERVICE_ALIASES:
            return SERVICE_ALIASES[token]
    return ""


def is_local_service(value: Any) -> bool:
    """True only for the three self-hosted engines this module covers."""
    return normalise_service(value) in LOCAL_SERVICES


@dataclass(frozen=True)
class ServiceProfile:
    """The resolved behaviour set for one service."""

    service: str                 # canonical id; "" when unknown / cloud
    is_local: bool
    chat_path: str               # path for chat + tool-loop requests
    thinking_default: str        # "" = leave the configured mode alone
    thinking_cap: int | None     # forced output budget while thinking
    native_thinking_param: str | None
    notes: str


def profile(service: Any) -> ServiceProfile:
    """Resolve a service string into its gated behaviour set."""
    sid = normalise_service(service)
    if sid in LOCAL_SERVICES:
        return ServiceProfile(
            service=sid,
            is_local=True,
            chat_path=OPENAI_COMPAT_CHAT_PATH,
            thinking_default="off",
            thinking_cap=LOCAL_THINKING_CAP,
            native_thinking_param="think" if sid == "ollama" else None,
            notes=("self-hosted engine: OpenAI-compatible surface required for "
                   "tool-call replay; contemplation is opt-in and budget-bounded"),
        )
    return ServiceProfile(
        service=sid,
        is_local=False,
        chat_path=CLOUD_CHAT_PATH,
        thinking_default="",      # cloud: configured value stands
        thinking_cap=None,
        native_thinking_param=None,
        notes="not a local service: no gated adaptation applies",
    )


def _has_version_suffix(base_url: str) -> bool:
    """True when the base URL already carries a version segment.

    ``/api`` counts as a version segment so a base like ``.../11434/api``
    does not become ``/api/v1/chat/completions``.
    """
    tail = str(base_url or "").rstrip("/").lower()
    return tail.endswith("/v1") or tail.endswith("/api")


def chat_request_path(service: Any, wire: str, base_url: str = "") -> str:
    """Request path for chat completions.

    * Anthropic wire is untouched (``/v1/messages``).
    * Local services get ``/v1/chat/completions`` unless the base URL already
      carries a version segment, in which case ``/chat/completions`` avoids
      producing ``.../v1/v1/chat/completions``.
    * Everything else keeps ``/chat/completions`` — the pre-existing behaviour,
      unchanged.
    """
    if wire == "anthropic":
        return "/v1/messages"
    if not is_local_service(service):
        return CLOUD_CHAT_PATH
    if _has_version_suffix(base_url):
        return CLOUD_CHAT_PATH
    return OPENAI_COMPAT_CHAT_PATH


def resolve_thinking_mode(configured: Any, service: Any) -> str:
    """Thinking mode after the gate.

    Cloud/unknown service: the configured mode is returned untouched.
    Local engine: only an explicit ``on`` engages contemplation. ``smart``
    (heuristic auto-trigger) is downgraded to ``off`` — on a small local model a
    non-converging contemplation loop is expensive to detect and trivial to
    avoid by not starting it.
    """
    mode = normalise_thinking_mode(configured)
    if not is_local_service(service):
        return mode
    return "on" if mode == "on" else "off"


def thinking_token_cap(service: Any, *, engaged: bool,
                       configured: int | None = None) -> int | None:
    """Output-token budget to attach to a contemplation round.

    ``None`` means "do not touch the request" — which is what every
    non-local service gets. A local engine with contemplation engaged always
    gets a budget: the smaller of the configured value and the ceiling.
    """
    if not engaged or not is_local_service(service):
        return None
    if configured is not None:
        try:
            want = int(configured)
        except (TypeError, ValueError):
            want = 0
        if want > 0:
            return min(want, LOCAL_THINKING_CAP)
    return LOCAL_THINKING_CAP


def _default_model(conf: dict[str, Any]) -> str:
    """Mirror ModelRouter's notion of a provider's default model."""
    return str((conf or {}).get("model", (conf or {}).get("defaultModel", "")) or "")


def service_from_config(config: Any, *, provider_id: str = "") -> str:
    """Resolve the service of the provider the run will actually use.

    The gate must key off the *same* provider ``ModelRouter._order`` will try
    first, or a cloud run can come out of the gate with local-only behaviour
    switched on. Three cases, all mirroring ``_order``:

    1. ``model.primary`` names a provider row -> that row's service.
    2. no ``model.primary`` -> the first provider row carrying a default model
       (``_order``'s own fallback).
    3. primary names an unknown row, or nothing resolves -> ``""``.

    Case 3 is deliberately the empty string rather than a scan for "any row
    that happens to declare a service": an unresolvable primary is an
    *unknown*, and unknowns must not switch the gate on. A previous version
    scanned, which made a cloud run (cloud primary absent, a local row present)
    resolve to the local engine.
    """
    if config is None:
        return ""
    try:
        rows = list(config.active())
    except Exception:
        return ""

    provider_rows = [(rid, conf) for rid, name, conf in rows
                     if str(name).startswith("provider:")]

    target = provider_id
    if not target:
        try:
            primary = config.get("model", "primary", None)
        except Exception:
            primary = None
        if isinstance(primary, (list, tuple)) and primary:
            target = str(primary[0])
        elif isinstance(primary, str):
            # A bare string primary is malformed (ModelRouter would explode it
            # into a character tuple). Do not pretend to know which provider it
            # meant; treat it as unresolvable.
            target = ""

    if target:
        for rid, conf in provider_rows:
            if rid == target:
                return normalise_service((conf or {}).get("service"))
        return ""        # primary names a row we do not have: unknown, no gate

    for _rid, conf in provider_rows:
        if _default_model(conf):
            return normalise_service((conf or {}).get("service"))
    return ""


__all__ = [
    "CLOUD_CHAT_PATH",
    "LOCAL_SERVICES",
    "LOCAL_THINKING_CAP",
    "OPENAI_COMPAT_CHAT_PATH",
    "SERVICE_ALIASES",
    "ServiceProfile",
    "chat_request_path",
    "is_local_service",
    "normalise_service",
    "profile",
    "resolve_thinking_mode",
    "service_from_config",
    "thinking_token_cap",
]
