"""请求消息投影层：确定性装配 + 规则合并策略（吸收自 Loom 的 project() 形态）。

从 loop.run() 抽出的纯函数集合。两条结构不变式落在这里之后，可以用性质
测试直接锁定，而不是靠运行期黑盒录制比对：

1. 装配顺序恒定：system → 冻结记忆 → 项目规则 → 历史 → 尾部上下文 → 任务。
   头部（system+记忆+规则）跨轮字节稳定；任何易变内容只允许出现在尾部。
2. 规则合并 append-only 优先：旧快照纯新增时保留已发出的旧消息、把新快照
   接在已发送前缀之后；只有旧内容确已过时才移除重插（正确性优先）。

所有函数都不修改传入的列表/字典。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Sequence


def head(system: str, memory_messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """请求头部：system 之后的第一个可变层是冻结记忆。"""
    if not isinstance(system, str) or not system:
        raise ValueError("system prompt must be a non-empty string")
    return [{"role": "system", "content": system}, *memory_messages]


def append_turn(messages: Sequence[dict[str, Any]],
                history: Sequence[dict[str, Any]],
                tail_messages: Sequence[dict[str, Any]],
                task: str) -> list[dict[str, Any]]:
    """尾部装配：历史 → 尾部上下文（信封/记忆增量/RAG 注入）→ 本轮任务。

    顺序是缓存不变式：历史紧接规则之后（跨轮前缀延续），易变内容只落在
    历史与任务之间，任务永远是最后一条消息。
    """
    if not isinstance(task, str) or not task:
        raise ValueError("task must be a non-empty string")
    return [*messages, *history, *tail_messages, {"role": "user", "content": task}]


def merge_rules(messages: Sequence[dict[str, Any]], *, old_context: str,
                snapshot_context: str, previous_sources: Sequence[Any],
                snapshot_sources: Sequence[Any] = (),
                prefix_sent: int, rule_task_content: Any = None,
                previous_contexts: Sequence[str] = (), append_context: str | None = None
                ) -> tuple[list[dict[str, Any]], str]:
    """d44ad30 规则合并策略（纯函数化）。

    返回 (新消息列表, outcome)，不修改输入：
    - "unchanged"：快照与当前上下文一致，原样返回。
    - "appended"：纯新增（旧快照各来源都在新快照中且旧消息已随前缀发出）——
      旧消息保留，新条目（append_context，缺省为完整快照）接在已发送前缀之后。
    - "replaced"：旧内容已过时——移除全部已投影片段，新快照插回原位置；
      无原位置则插在历史任务消息之前。
    - "removed"：新快照为空——移除全部已投影片段。
    """
    if snapshot_context == old_context:
        return list(messages), "unchanged"
    sent = min(int(prefix_sent or 0), len(messages))
    contexts = {c for c in [old_context, *previous_contexts] if c}
    old_index = next((i for i, m in enumerate(messages)
                      if m.get("role") == "user" and isinstance(m.get('content'), str)
                      and m['content'] in contexts), None)
    # d44ad30 原语义：旧快照的每个来源（路径+版本）都原样保留在新快照的
    # 来源清单中 = 纯新增（例如读到子目录后新发现了 src/AGENTS.md）。
    # 注意按清单成员检查，不是对渲染文本做子串检查。
    sources_present = all(s in snapshot_sources for s in previous_sources)
    if (sources_present and snapshot_context and sent
            and (not contexts or old_index is not None and old_index < sent)):
        out = list(messages)
        out.insert(sent, {"role": "user", "content": append_context or snapshot_context})
        return out, "appended"
    out = [m for m in messages if not (m.get("role") == "user"
           and isinstance(m.get('content'), str) and m['content'] in contexts)]
    if snapshot_context:
        anchor = old_index if old_index is not None else next(
            (i for i, m in enumerate(out) if m.get("role") == "user"
             and m.get("content") == rule_task_content), len(out))
        out.insert(min(anchor, len(out)), {"role": "user", "content": snapshot_context})
        return out, "replaced"
    return out, "removed"


def head_digest(system: str, tool_names: Iterable[str]) -> str:
    """头部+工具面的稳定指纹（公式与原 loop 内实现逐字节一致，勿排序）。"""
    blob = str(system) + "|" + ", ".join(tool_names)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = ["append_turn", "head", "head_digest", "merge_rules"]
