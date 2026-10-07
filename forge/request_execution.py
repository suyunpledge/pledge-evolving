"""Small, explicit execution boundary for adapter recommendations.

Scheduling is opt-in and bounded. Batch has a separate job API; synchronous
chat is never secretly submitted to a 24-hour queue. Discounts in a plan are
estimates, not evidence of the service tier used by an upstream.
"""
from __future__ import annotations

import copy
import math
import threading
import time

from .vendors import is_peak


def off_peak_delay(profile, when=None):
    now = time.time() if when is None else when
    if not is_peak(profile, now):
        return 0.0
    # Search the next boundary, then align to its start (UTC whole hour).
    for hour in range(1, 25):
        boundary = (int(now) // 3600 + hour) * 3600
        if not is_peak(profile, boundary):
            return boundary - now
    raise ValueError("No off-peak boundary found")


def wait_for_schedule(plan, cancel_event=None):
    delay = float(plan.get("execution", {}).get("defer", {}).get("delay_seconds", 0))
    event = cancel_event or threading.Event()
    if event.wait(max(0, delay)):
        raise ValueError("Request cancelled before execution")


def prepare_execution(payload, profile, model, *, interactive=True,
                      think_budget=None, flex=False, batch=False, defer=False,
                      max_defer_seconds=0, when=None, batch_submission=False):
    shaped = copy.deepcopy(payload)
    execution = {}
    if batch:
        if not batch_submission:
            raise ValueError("Batch is asynchronous; use BatchExecutor.submit instead of synchronous chat")
        if flex or defer or interactive or shaped.get("stream"):
            raise ValueError("Batch cannot combine with flex, defer, interactive or streaming")
        if profile.id not in ("bailian", "openai") or profile.wire != "openai":
            raise ValueError("Batch execution is not supported by this endpoint")
        execution["batch"] = {"status": "submission", "discount_confirmed": False}
    if flex:
        if (profile.id != "openai" or not profile.supports_flex):
            raise ValueError("Flex execution is not supported by this endpoint")
        if shaped.get("service_tier") not in (None, "flex"):
            raise ValueError("Flex conflicts with the explicitly selected service tier")
        shaped["service_tier"] = "flex"
        execution["flex"] = {"status": "requested", "discount_confirmed": False}
    if defer:
        if interactive:
            raise ValueError("Interactive requests cannot be deferred")
        limit = float(max_defer_seconds)
        if not math.isfinite(limit) or not 0 <= limit <= 86400:
            raise ValueError("max_defer_seconds must be between 0 and 86400")
        delay = off_peak_delay(profile, when)
        if delay > limit:
            raise ValueError(f"Off-peak wait {delay:.0f}s exceeds allowed {limit:.0f}s")
        execution["defer"] = {"status": "scheduled" if delay else "ready",
                              "delay_seconds": delay}
    if think_budget is not None:
        if isinstance(think_budget, bool) or not isinstance(think_budget, int) or not 1 <= think_budget <= 262144:
            raise ValueError("think_budget must be an integer between 1 and 262144")
        if "reasoning_effort" in shaped or "thinking" in shaped:
            execution["reasoning"] = {"status": "explicit_override", "requested": think_budget}
        elif profile.id == "bailian" and model.lower().startswith("qwen3"):
            shaped["thinking_budget"] = think_budget
            execution["reasoning"] = {"status": "applied", "scope": "thinking", "tokens": think_budget}
        elif profile.id == "deepseek":
            # DeepSeek ignores thinking.budget_tokens. max_tokens bounds all
            # generated tokens; this is deliberately NOT a separate CoT cap.
            shaped["max_tokens"] = min(int(shaped.get("max_tokens") or think_budget), think_budget)
            execution["reasoning"] = {"status": "applied", "scope": "total_output",
                                      "tokens": shaped["max_tokens"]}
        else:
            execution["reasoning"] = {"status": "unsupported", "requested": think_budget}
    return shaped, execution


class UsageObserver:
    """Observe bounded SSE data without buffering message content.

    A dropped/oversized/malformed event removes evidence, never invents usage.
    Handles both OpenAI final usage and Anthropic message_start/message_delta.
    """
    MAX_EVENT_BYTES = 256 * 1024

    def __init__(self):
        self.pending = b""
        self.discard_line = False
        self.usage = {}
        self.service_tier = None

    def observe(self, raw):
        if not isinstance(raw, dict):
            return
        usage = raw.get("usage")
        if raw.get("type") == "message_start":
            usage = (raw.get("message") or {}).get("usage")
        if isinstance(usage, dict):
            self.usage.update(usage)
        if raw.get("service_tier"):
            self.service_tier = raw["service_tier"]

    def feed(self, chunk):
        import json
        parts = chunk.split(b"\n")
        for i, part in enumerate(parts):
            final = i < len(parts) - 1
            if self.discard_line:
                if final:
                    self.discard_line = False
                continue
            self.pending += part
            if len(self.pending) > self.MAX_EVENT_BYTES:
                self.pending = b""
                self.discard_line = not final
                continue
            if not final:
                continue
            line, self.pending = self.pending.strip(), b""
            if line.startswith(b"data:"):
                try:
                    self.observe(json.loads(line[5:]))
                except (ValueError, UnicodeDecodeError):
                    pass
