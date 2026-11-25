"""Subagent orchestration: spawn, depth/budget control, output sanitisation.

Extracted from loop.py (0.7.0) to separate the subagent runtime from the
agent's main loop. This module owns:

* ``sanitise_child_output`` — de-poison child output to prevent injection
* ``SpawnHandler`` — the runtime callable that creates nested agents
* ``BudgetTracker`` — shared mutable budget across a spawn tree

The Agent class in loop.py delegates subagent creation to SpawnHandler;
this keeps loop.py focused on the main iteration cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Subagent output is untrusted: a child must not smuggle a directive
# back into the parent by impersonating a system turn. Regex alone was too
# narrow (full-width angle brackets, chat-template tokens and role prefixes all
# slipped through), so the pattern covers the shapes other frameworks' models
# actually emit.  (canonical copy — kept in sync with loop.py)
INJECTION_RE = re.compile(
    r"</?(?:system-reminder|system|instructions|assistant|im_start|im_end|s)\b[^>]*>"
    r"|＜/?(?:system|assistant|instructions)[^＞]*＞"
    r"|^\s*(?:SYSTEM|ASSISTANT|USER|DEVELOPER)\s*[:：]"
    r"|\[/?INST\]|<<\s*/?SYS\s*>>",
    re.I | re.M,
)


def sanitise_child_output(text: str) -> str:
    """Strip anything a child could use to impersonate a system directive."""
    cleaned = INJECTION_RE.sub("[redacted-directive]", text or "")
    return cleaned.strip()


@dataclass
class BudgetTracker:
    """Shared mutable budget across a spawn tree.

    The root agent owns the list; children share the same list object
    so budget decrements are visible across the tree.
    """

    values: list[int]

    @property
    def remaining(self) -> int:
        return self.values[0] if self.values else 0

    def consume(self, amount: int = 1) -> int:
        """Decrement budget. Returns remaining after decrement."""
        if self.values:
            self.values[0] = max(0, self.values[0] - amount)
        return self.remaining

    def exhausted(self) -> bool:
        return self.remaining <= 0


__all__ = [
    "BudgetTracker",
    "INJECTION_RE",
    "sanitise_child_output",
]
