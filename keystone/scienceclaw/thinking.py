"""Reasoning-content extraction (port of ScienceClaw runner._extract_thinking).

Three vendor formats for chain-of-thought content, unified into one
(thinking, clean_text) pair:

1. DeepSeek via OpenAI-compatible API — ``reasoning_content`` key
2. Claude — content blocks with ``type="thinking"``
3. DeepSeek / Qwen native APIs — ``<think>...</think>`` tags in text

Ported without langchain message types: the input is whatever message
object or dict the transport produced; duck-typed field access.
"""
from __future__ import annotations

import re
from typing import Any

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_CONTENT_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def extract_thinking(message: Any) -> tuple[str, str]:
    """Return (thinking_content, clean_text) from any message shape.

    Accepts:
      * dicts with ``additional_kwargs.reasoning_content`` (DeepSeek)
      * messages whose ``content`` is a list of blocks (Claude)
      * plain strings possibly containing ``<think>`` tags
    """
    thinking = ""

    # ① additional_kwargs.reasoning_content (DeepSeek via OpenAI API)
    kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(message, dict):
        kwargs = message.get("additional_kwargs")
    if isinstance(kwargs, dict):
        reasoning = kwargs.get("reasoning_content", "")
        if isinstance(reasoning, str) and reasoning.strip():
            thinking = reasoning.strip()

    content = getattr(message, "content", None)
    if isinstance(message, dict):
        content = message.get("content")

    if content is None:
        return thinking, ""
    if not content:
        return thinking, ""

    # ② list content blocks (Claude style)
    if isinstance(content, list):
        thinking_parts: list[str] = []
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "thinking":
                    thinking_parts.append(block.get("thinking", ""))
                elif btype == "text":
                    text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        if thinking_parts:
            thinking = "\n".join(p for p in thinking_parts if p.strip()) or thinking
        clean = "\n".join(p for p in text_parts if p.strip())
        # nested <think> inside text blocks (Qwen via Claude-shaped API)
        nested = _THINK_CONTENT_RE.findall(clean)
        if nested:
            thinking = "\n".join(filter(str.strip, nested)) or thinking
            clean = _THINK_TAG_RE.sub("", clean).strip()
        return thinking.strip(), clean.strip()

    # ③ plain string, possibly with <think> tags (DeepSeek / Qwen native)
    if isinstance(content, str):
        found = _THINK_CONTENT_RE.findall(content)
        if found:
            thinking = "\n".join(p for p in found if p.strip()) or thinking
            clean = _THINK_TAG_RE.sub("", content).strip()
            return thinking.strip(), clean
        return thinking, content

    return thinking, ""


def strip_think_tags(text: str) -> str:
    """Remove <think> blocks from a text blob (display helper)."""
    return _THINK_TAG_RE.sub("", text).strip()


__all__ = ["extract_thinking", "strip_think_tags"]
