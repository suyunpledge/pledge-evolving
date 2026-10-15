"""Tool-result offloading (port of ScienceClaw offload_middleware.py).

Cursor-style context management: when a tool result exceeds a
threshold, write the full result to a workspace file and hand the
agent a summary + path reference. The agent reads the complete data
back on demand with its file tools. This kills the classic failure
where multi-turn histories truncate tool output and silently lose
information.

Ported without langchain: the middleware becomes a plain callable
``ToolResultOffloader`` that forge's tool dispatch wraps around any
tool execution; the async event-loop dance in upstream's
``wrap_tool_call`` collapses into a sync call because forge executes
tools on its own worker threads already.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

DEFAULT_THRESHOLD = 3000          # chars, aggressive tools (search/exec)
DEFAULT_SUMMARY_LENGTH = 1500     # chars of preview inside the summary
RELAXED_THRESHOLD = 30000         # chars, file tools (read/edit/ls/grep)
DOUBLE_THRESHOLD = 6000           # chars, unknown tools

AGGRESSIVE_TOOLS = frozenset({
    "web_search", "web_crawl", "execute", "sandbox_exec",
    "terminal_execute", "shell_exec", "tooluniverse_run",
})
RELAXED_TOOLS = frozenset({
    "read_file", "edit_file", "write_file", "ls", "glob", "grep",
})

_OFFLOAD_DIR = "research_data"


def extract_text(result: Any) -> str | None:
    """Pull the string payload out of assorted tool-result shapes."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("content", "output", "text", "result"):
            val = result.get(key)
            if isinstance(val, str) and len(val) > 100:
                return val
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    return None


def make_summary(text: str, file_path: str, *, preview_length: int = DEFAULT_SUMMARY_LENGTH) -> str:
    """Summary with a file reference, mirroring upstream's wording."""
    preview = text[:preview_length]
    if len(text) > preview_length:
        preview += "\n..."
    return (
        f"[完整结果已落盘 {file_path}（{len(text)} 字符）。"
        f"用 read_file(\"{file_path}\") 取完整数据。"
        f"注意：该文件是工具原始输出；在脚本里使用前先提取所需字段再写干净文件。]\n\n"
        f"{preview}"
    )


@dataclass
class OffloadPolicy:
    """Which tools offload at which threshold (upstream's two-tier split)."""

    aggressive: frozenset = AGGRESSIVE_TOOLS
    relaxed: frozenset = RELAXED_TOOLS
    aggressive_threshold: int = DEFAULT_THRESHOLD
    relaxed_threshold: int = RELAXED_THRESHOLD
    default_threshold: int = DOUBLE_THRESHOLD
    summary_length: int = DEFAULT_SUMMARY_LENGTH

    def threshold_for(self, tool_name: str) -> int:
        if tool_name in self.aggressive:
            return self.aggressive_threshold
        if tool_name in self.relaxed:
            return self.relaxed_threshold
        return self.default_threshold

    def should_offload(self, tool_name: str | None, text: str | None) -> bool:
        if not tool_name or not text:
            return False
        return len(text) > self.threshold_for(tool_name)


class _Writer(Protocol):
    def __call__(self, path: Path, text: str) -> None: ...


def _fs_writer(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass
class ToolResultOffloader:
    """Wrap a tool call; offload oversized results to workspace files.

    Usage in forge's dispatch (sync, no middleware stack)::

        offloader = ToolResultOffloader(workspace)
        result = offloader.wrap("web_search", "call-1", tool_fn, args)
    """

    workspace: Path
    policy: OffloadPolicy = field(default_factory=OffloadPolicy)
    writer: _Writer = _fs_writer
    offload_dir: str = _OFFLOAD_DIR
    # stats (upstream's diagnostic hook)
    offload_count: int = 0
    last_path: str = ""

    def _file_path(self, tool_name: str, call_id: str) -> Path:
        short = hashlib.md5(f"{call_id}{time.time()}".encode()).hexdigest()[:8]
        safe = re.sub(r"[/\s]", "_", tool_name)
        return self.workspace / self.offload_dir / f"{safe}_{short}.md"

    def wrap(
        self,
        tool_name: str,
        call_id: str,
        handler: Callable[[], Any],
    ) -> Any:
        """Execute the tool; replace oversized text with summary + path."""
        result = handler()
        text = extract_text(result)
        if not self.policy.should_offload(tool_name, text):
            return result
        path = self._file_path(tool_name, call_id)
        try:
            self.writer(path, text)
            self.offload_count += 1
            self.last_path = str(path)
            summary = make_summary(text, str(path), preview_length=self.policy.summary_length)
        except OSError:
            return result  # disk trouble: keep the original text
        return _replace_text(result, summary)


def _replace_text(result: Any, new_text: str) -> Any:
    """Swap the text payload while preserving the result's shape."""
    if isinstance(result, str):
        return new_text
    if isinstance(result, dict):
        for key in ("content", "output", "text", "result"):
            if key in result and isinstance(result[key], str):
                out = dict(result)
                out[key] = new_text
                return out
    if hasattr(result, "content") and isinstance(getattr(result, "content"), str):
        try:
            result.content = new_text
            return result
        except Exception:
            return new_text
    return new_text


__all__ = [
    "DEFAULT_THRESHOLD", "OffloadPolicy", "ToolResultOffloader",
    "extract_text", "make_summary",
]
