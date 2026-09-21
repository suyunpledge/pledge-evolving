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
        """Compress old messages, preserving the system prompt prefix and a
        stable tail so that KV cache hits survive compaction.

        Design aligned with DeepSeek Harness's compaction semantics:

        1. **System prompt is never touched** — it is the shared prefix for
           all cache hits. Memory has already been moved out of it (P0-1).
        2. **Tail is pinned** — the last ``keep_tail`` messages are always
           sent verbatim. This gives the model recent context and, crucially,
           keeps the post-compaction message sequence an **append-only
           extension** of what came before. The next run's prefix matches.
        3. **Head is replaced in-place** — the compressed head becomes a
           single summary message. Its position (after system, before tail)
           is deterministic, so it does not shift the tail.

        The net effect: after compaction the message sequence is
        ``[system, summary, tail]``.  The next run appends
        ``[memory, task]``, making the new sequence
        ``[system, summary, tail, memory, task]`` — a strict prefix extension
        of the compacted form.
        """
        gate = self.should_compact if max_chars is None else             (lambda msgs: self.size(msgs) > max_chars)
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
                cap = max(self.max_chars // 20, 200)
                if len(content) > cap:
                    changed = True
                    squeezed.append({**m, "content": content[:cap]})
                else:
                    squeezed.append(m)
            return squeezed if changed else messages

        system = [m for m in messages if str(m.get("role", "")) == "system"]
        rest = [m for m in messages if str(m.get("role", "")) != "system"]
        head = rest[:-self.keep_tail]
        tail = rest[-self.keep_tail:]

        if not head:
            return messages

        if summariser is None:
            flat = " ".join(str(m.get("content", ""))[:200] for m in head)
            summary = f"[compacted {len(head)} earlier messages] {flat[:800]}"
        else:
            summary = summariser(head)

        # Result: [system, summary, *tail]
        # Tail stays in place; next run appends memory+task after it,
        # making the new sequence a strict prefix extension.
        return [*system, {"role": "user", "content": summary}, *tail]


__all__ = ["ContextBudget"]
