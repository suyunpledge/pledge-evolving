"""Context compaction: budget-aware message summarisation.

Extracted from memory.py (0.7.0) to separate the compaction policy from
the memory store. This module owns:

* ``ContextBudget`` — size-based compaction with system-pmessage pinning
* ``CompactionPolicy`` — pricing-aware budget ceiling

The separation makes compaction independently testable and allows
alternative compaction strategies (e.g. semantic summarisation) without
touching the memory store.
"""

from __future__ import annotations

from typing import Any, Callable


class ContextBudget:
    """Compaction policy: keep a recent tail, summarise the rest.

    OpenClaw compacts on a threshold and always preserves pinned material and
    the running summary; the compaction event is written to the session log so
    the decision stays auditable.
    """

    def __init__(self, *, max_chars: int = 24000, keep_tail: int = 6) -> None:
        self.max_chars = max_chars
        self.keep_tail = keep_tail

    def size(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages)

    def should_compact(self, messages: list[dict[str, Any]]) -> bool:
        return self.size(messages) > self.max_chars

    def compact(self, messages: list[dict[str, Any]], summariser=None,
                *, max_chars: int | None = None) -> list[dict[str, Any]]:
        gate = self.should_compact if max_chars is None else \
            (lambda msgs: self.size(msgs) > max_chars)
        if not gate(messages):
            return messages
        if len(messages) <= self.keep_tail:
            squeezed: list[dict[str, Any]] = []
            changed = False
            for m in messages:
                if str(m.get("role", "")) == "system":
                    squeezed.append(m)
                    continue
                content = str(m.get("content", ""))
                cap = max(self.max_chars // 20, 200)  # adaptive truncation floor
                if len(content) > cap:
                    changed = True
                    squeezed.append({**m, "content": content[:cap]})
                else:
                    squeezed.append(m)
            return squeezed if changed else messages
        pinned = [m for m in messages if str(m.get("role", "")) == "system"]
        rest = [m for m in messages if str(m.get("role", "")) != "system"]
        head, tail = rest[: -self.keep_tail], rest[-self.keep_tail:]
        if not head:
            return messages
        if summariser is None:
            flat = " ".join(str(m.get("content", ""))[:200] for m in head)
            summary = f"[compacted {len(head)} earlier messages] {flat[:800]}"
        else:
            summary = summariser(head)
        return [*pinned, {"role": "user", "content": summary}, *tail]


__all__ = ["ContextBudget"]
