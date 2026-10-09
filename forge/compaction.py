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
import json


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
        return sum(len(str(m.get("content", ""))) +
                   (len(json.dumps(m["tool_calls"], ensure_ascii=False)) if m.get("tool_calls") else 0)
                   for m in messages)

    def should_compact(self, messages: list[dict[str, Any]]) -> bool:
        return self.size(messages) > self.max_chars

    def compact(self, messages: list[dict[str, Any]], summariser=None,
                *, max_chars: int | None = None,
                pinned_contents: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Keep system/task/plan and recent complete tool exchanges.

        Compaction changes the earlier prefix and can invalidate provider
        cache entries. Subsequent appends can reuse the new prefix, but a
        preserved tail alone is not proof of a cache hit. This deterministic
        excerpt summary is lossy; it does not invent verified file outcomes.
        """
        gate = self.should_compact if max_chars is None else (lambda msgs: self.size(msgs) > max_chars)
        if not gate(messages):
            return messages

        if len(messages) <= self.keep_tail:
            squeezed: list[dict[str, Any]] = []
            changed = False
            for m in messages:
                if (str(m.get("role", "")) == "system" or m.get("content") in pinned_contents
                        or m.get("tool_calls") or m.get("role") == "tool"
                        or isinstance(m.get("content"), list)):
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
        pinned = [m for m in messages if m.get("role") != "system" and
                  isinstance(m.get("content"), str) and m["content"] in pinned_contents]
        rest = [m for m in messages if m.get("role") != "system" and m not in pinned]
        cut = max(0, len(rest) - max(1, self.keep_tail))
        # pi-style valid cut points: never retain an orphan native tool result.
        # Keep the entire assistant call + result group, including batched calls.
        while cut > 0:
            message = rest[cut]
            content = message.get("content")
            result_block = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if message.get("role") != "tool" and not result_block:
                break
            cut -= 1
        head, tail = rest[:cut], rest[cut:]

        if not head:
            return messages

        if summariser is None:
            # Recent evidence matters more than the oldest messages. Preserve
            # requested file paths/tool names without claiming a call succeeded.
            requests = [json.dumps(m["tool_calls"], ensure_ascii=False)[:240]
                        for m in head if m.get("tool_calls")][-3:]
            flat = " ".join(str(m.get("role", "")) + ": " + str(m.get("content", ""))[:200]
                            for m in head[-4:])
            if requests:
                flat = "Earlier tool requests (outcomes not asserted): " + " ".join(requests) + "\n" + flat
            summary = f"[compacted {len(head)} earlier messages] {flat[:800]}"
        else:
            summary = summariser(head)

        # A changed prefix is expected; native call/result pairs remain valid.
        return [*system, *pinned, {"role": "user", "content": summary}, *tail]


__all__ = ["ContextBudget"]
