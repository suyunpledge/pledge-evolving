"""Context compaction: budget-aware message summarisation.

Extracted from memory.py (0.7.0) to separate the compaction policy from
the memory store. This module owns:

* ``ContextBudget`` — size-based compaction with system-pmessage pinning
* the pricing-aware budget ceiling is supplied by the runtime loop

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
                   (len(json.dumps(m["tool_calls"], ensure_ascii=False)) if m.get("tool_calls") else 0) +
                   sum(len(json.dumps(v, ensure_ascii=False)) for k, v in m.items()
                       if k not in {'role', 'content', 'tool_calls', 'tool_call_id', 'name'})
                   for m in messages)

    def should_compact(self, messages: list[dict[str, Any]]) -> bool:
        return self.size(messages) > self.max_chars

    def compact(self, messages: list[dict[str, Any]], summariser=None,
                *, max_chars: int | None = None,
                pinned_contents: tuple[str, ...] = (), prefix_messages=()) -> list[dict[str, Any]]:
        """Keep system/task/plan and recent complete tool exchanges.

        Compaction changes the earlier prefix and can invalidate provider
        cache entries. Subsequent appends can reuse the new prefix, but a
        preserved tail alone is not proof of a cache hit. This deterministic
        excerpt summary is lossy; it does not invent verified file outcomes.
        """
        gate = self.should_compact if max_chars is None else (lambda msgs: self.size(msgs) > max_chars)
        if not gate(messages):
            return messages

        if len(messages) <= self.keep_tail and not prefix_messages:
            squeezed: list[dict[str, Any]] = []
            changed = False
            for m in messages:
                if (str(m.get("role", "")) in {"system", "developer", "user"}
                        or m.get("content") in pinned_contents
                        or m.get("tool_calls") or m.get("role") == "tool"
                        or isinstance(m.get("content"), list)):
                    squeezed.append(m)
                    continue
                content = str(m.get("content", ""))
                cap = max((self.max_chars if max_chars is None else max_chars) // 20, 200)
                if len(content) > cap:
                    changed = True
                    squeezed.append({**m, "content": content[:cap]})
                else:
                    squeezed.append(m)
            return squeezed if changed else messages

        # 2026-11-04 forge 真实任务实测修复（MiniMax 400 "tool id not found (2013)"）：
        # pinned_contents 可能含空串（无 plan 时 plan.context() 返回 ''），空结果的
        # tool 消息（grep 零命中 content=''）会精确匹配 '' 被当作钉住材料提升到
        # system 之后——既孤儿化（严格校验的厂商直接 400）又乱序（其余厂商静默吞
        # 下损坏历史）。两道防御：
        #   ① 空串不进 pinned 集合；
        #   ② tool 消息与带 tool_calls 的 assistant 永不可被钉住提升（钉住语义
        #      只对普通 system/user 课文有意义）。
        pin_set = {c for c in pinned_contents if c}
        prefix = list(prefix_messages)
        if messages[:len(prefix)] != prefix:
            raise ValueError('Compaction prefix no longer matches the source')
        body = messages[len(prefix):]
        system = [m for m in body if str(m.get("role", "")) in {"system", "developer"}]
        pinned = [m for m in body
                  if m.get("role") not in ("system", "developer", "tool")
                  and not m.get("tool_call_id") and not m.get("tool_calls")
                  and isinstance(m.get("content"), str) and m["content"] in pin_set]
        rest = [m for m in body if m.get("role") not in ("system", "developer") and m not in pinned]
        if not rest:
            return messages
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
        def build(at):
            head, tail = rest[:at], rest[at:]
            if not head:
                return messages
            summary = summariser(head) if summariser is not None else self._excerpt(head)
            if prefix_messages:
                # 保留源消息的相对顺序，尤其不把本轮任务提升到历史前方。
                # 摘要位于任务之前，避免路由器把摘要误认成最新用户任务。
                protected = {id(m) for m in [*system, *pinned]}
                retained, cursor = [], 0
                for message in body:
                    if id(message) in protected:
                        retained.append(message)
                    else:
                        if cursor >= at:
                            retained.append(message)
                        cursor += 1
                return [*prefix, {'role': 'user', 'content': summary}, *retained]
            return [*prefix, *system, *pinned, {"role": "user", "content": summary}, *tail]

        candidate = build(cut)
        if prefix_messages:
            from .context_transaction import valid_tool_pairs
            # Agent 的目标是回收出余量；至少保留最后一个完整交换，不截断推理/工具块。
            protected_last = any(messages[-1] is m for m in [*system, *pinned])
            last = len(rest) if protected_last else len(rest) - 1
            while last > 0 and not valid_tool_pairs(rest[last:]):
                last -= 1
            target = self.max_chars if max_chars is None else max_chars
            while self.size(candidate) > target and cut < last:
                cut += 1
                while cut < last and not valid_tool_pairs(rest[cut:]):
                    cut += 1
                candidate = build(cut)
        return candidate

    @staticmethod
    def _excerpt(head):
        from .compaction_notes import excerpt_messages
        return excerpt_messages(head)


__all__ = ["ContextBudget"]
