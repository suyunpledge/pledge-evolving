"""Typed memory with dual-write storage.

Borrowed from WorkBuddy/CodeBuddy (Markdown for humans + a ``RAW_JSON`` block
for machines, four typed classes) and OpenClaw (a bounded slice of memory is
injected into context, the rest stays on disk).

Every entry carries provenance. Entries written by the agent itself (rather
than by a human or an installer) are marked ``agent`` and can be **archived**
by a curator pass — archived, never silently deleted, and always reversible.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from .secrets import redact

RAW_START = "<!-- RAW_JSON_START -->"
RAW_END = "<!-- RAW_JSON_END -->"

KINDS = ("user", "feedback", "project", "reference",
          "pitfall", "lesson", "note")  # 知识库三态（knowledge.py）


@dataclass
class Entry:
    kind: str
    text: str
    source: str = "human"
    pinned: bool = False
    archived: bool = False
    created_at: float = field(default_factory=time.time)
    tags: tuple[str, ...] = ()

    def to_raw(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "pinned": self.pinned,
            "archived": self.archived,
            "created_at": self.created_at,
            "tags": list(self.tags),
        }

    @staticmethod
    def from_raw(raw: dict[str, Any]) -> "Entry":
        return Entry(
            kind=str(raw.get("kind", "project")),
            text=redact(str(raw.get("text", ""))),
            source=str(raw.get("source", "human")),
            pinned=bool(raw.get("pinned", False)),
            archived=bool(raw.get("archived", False)),
            created_at=float(raw.get("created_at", time.time())),
            tags=tuple(raw.get("tags") or ()),
        )


class MemoryStore:
    """One Markdown file per scope, with an embedded machine-readable block."""

    _lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: list[Entry] = []

    # -- load / save -----------------------------------------------------
    @staticmethod
    def _recover_from_view(text: str) -> list:
        """F-06：JSON 块损坏时从 markdown 可读视图恢复条目。"""
        import re as _re
        entries = []
        pattern = _re.compile(r"^- \*\*\[([\w-]+)\]\*\*\s*(📌 )?(.+)$", _re.M)
        for match in pattern.finditer(text):
            kind, _pin, body = match.group(1), match.group(2), match.group(3)
            if kind not in KINDS:
                continue
            entries.append(Entry(kind=kind, text=body.strip(),
                                 source="view-recovery",
                                 tags=("recovered",)))
        return entries

    def load(self) -> "MemoryStore":
        if self.path.is_file():
            text = self.path.read_text(encoding="utf-8", errors="replace")
            block = _between(text, RAW_START, RAW_END)
            # F-06（QA 审查）：三种损坏形态都要走恢复——
            # ① JSON 块存在但解析失败；② 块存在但单条坏；③ 块被截断
            # （有 START 无 END，_between 返回空）。统一回退解析 markdown
            # 可读视图恢复条目，坏块不再清零整个库；save() 重建权威块。
            truncated = text.find(RAW_START) >= 0 and text.find(RAW_END) < 0
            if block or truncated:
                try:
                    payload = json.loads(block)
                    self.entries = []
                    for raw in payload.get("entries", []):
                        try:
                            self.entries.append(Entry.from_raw(raw))
                        except Exception:
                            continue  # 单条坏数据跳过
                except json.JSONDecodeError:
                    self.entries = self._recover_from_view(text)
                if truncated and not self.entries:
                    self.entries = self._recover_from_view(text)
        return self

    def save(self) -> None:
        # F-04（QA 审查）：跨实例并发写同一路径的 last-writer-wins 防护——
        # 写前重读磁盘，合并磁盘上多出的条目（本实例未见过的），再落盘。
        try:
            if self.path.is_file():
                disk = MemoryStore(self.path)
                disk.load()
                known = {(e.text.strip(), e.kind) for e in self.entries}
                for entry in disk.entries:
                    if (entry.text.strip(), entry.kind) not in known:
                        self.entries.append(entry)
        except Exception:
            pass  # 磁盘读取失败不阻断保存（保持原行为）
        lines = ["# Memory", "", "<!-- human-readable view; the JSON block below is authoritative -->", ""]
        for entry in self.entries:
            if entry.archived:
                continue
            mark = "📌 " if entry.pinned else ""
            lines.append(f"- **[{entry.kind}]** {mark}{entry.text}")
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "entries": [e.to_raw() for e in self.entries],
        }
        lines += ["", RAW_START, json.dumps(payload, ensure_ascii=False, indent=2), RAW_END, ""]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(redact("\n".join(lines)), encoding="utf-8")
        tmp.replace(self.path)  # atomic swap; never leave a half-written memory

    # -- api -------------------------------------------------------------
    def remember(self, text: str, *, kind: str = "project", source: str = "human",
                 tags: Iterable[str] = ()) -> Entry:
        text, source, tags = redact(text), redact(source), tuple(redact(list(tags)))
        if kind not in KINDS:
            raise ValueError(f"unknown memory kind {kind!r}; expected one of {KINDS}")
        with self._lock:
            for existing in self.entries:
                # F-05（QA 审查）：同文本再次 remember 时同步更新
                # kind/tags/source（不再静默丢弃第二个调用方的语义）。
                if existing.text.strip() == text.strip():
                    existing.kind = kind
                    existing.tags = tuple(tags) if tags else existing.tags
                    if source:
                        existing.source = source
                    return existing
            entry = Entry(kind=kind, text=text.strip(), source=source, tags=tuple(tags))
            self.entries.append(entry)
            self.save()
            return entry

    def recall(self, kind: str | None = None, *, include_archived: bool = False,
               limit: int = 50) -> list[Entry]:
        out = []
        for entry in self.entries:
            if entry.archived and not include_archived:
                continue
            if kind and entry.kind != kind:
                continue
            out.append(entry)
        out.sort(key=lambda e: (not e.pinned, -e.created_at))
        return out[:limit]

    def context_slice(self, max_chars: int = 2000) -> str:
        """The bounded slice that gets injected into the system prompt."""
        chunks: list[str] = []
        used = 0
        for entry in self.recall():
            line = f"[{entry.kind}] {entry.text}"
            if used + len(line) > max_chars:
                break
            chunks.append(line)
            used += len(line) + 1
        return "\n".join(chunks)

    def curate(self, *, archive_agent_entries: bool = True, archive_after: float = 0.0) -> dict[str, int]:
        """Curator pass: archive (never delete) agent-authored knowledge.

        Same contract as Hermes' curator: bundled/human assets are exempt,
        pinned entries are exempt, and the operation is reversible.
        """
        archived = 0
        for entry in self.entries:
            if entry.archived or entry.pinned:
                continue
            if archive_agent_entries and entry.source == "agent" and entry.created_at >= archive_after:
                entry.archived = True
                archived += 1
        if archived:
            self.save()
        # F-08（QA 审查）：kept = 活跃（未归档）条目数——先前已归档的
        # 不应被计入 kept。
        kept = sum(1 for e in self.entries if not e.archived)
        return {"archived": archived, "kept": kept}

    def restore(self, text: str) -> bool:
        for entry in self.entries:
            if entry.text.strip() == text.strip() and entry.archived:
                entry.archived = False
                self.save()
                return True
        return False

    def stats(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {k: 0 for k in KINDS}
        archived = 0
        for entry in self.entries:
            by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
            archived += int(entry.archived)
        return {"total": len(self.entries), "archived": archived, "by_kind": by_kind}


def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    j = text.find(end)
    if i < 0 or j < 0 or j <= i:
        return ""
    return text[i + len(start):j].strip()


from .compaction import ContextBudget  # noqa: F401  (canonical location)


__all__ = ["ContextBudget", "Entry", "KINDS", "MemoryStore", "RAW_END", "RAW_START"]
