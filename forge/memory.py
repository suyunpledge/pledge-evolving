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
import hashlib
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
    # QA-03（独立 QA 回归）：agent 写入的记忆默认不进生效上下文，
    # 经宿主审批（promote_to_context / curate）放行后才注入 prompt。
    approved: bool = True
    entry_id: str = ""
    valid_from: float = 0.0
    valid_to: float | None = None
    superseded_by: str = ""
    transferable_to: tuple[str, ...] = ()
    topic: str = ""

    def __post_init__(self) -> None:
        if not self.valid_from:
            self.valid_from = self.created_at
        if not self.entry_id:
            seed = f"{self.created_at:.6f}\0{self.kind}\0{self.text}"
            self.entry_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @property
    def current(self) -> bool:
        return not self.archived and not self.superseded_by and self.valid_to is None

    def to_raw(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "approved": self.approved,
            "pinned": self.pinned,
            "archived": self.archived,
            "created_at": self.created_at,
            "tags": list(self.tags),
            "id": self.entry_id,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "superseded_by": self.superseded_by,
            "transferable_to": list(self.transferable_to),
            "topic": self.topic,
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
            approved=bool(raw.get("approved", True)),
            entry_id=str(raw.get("id", "")),
            valid_from=float(raw.get("valid_from", raw.get("created_at", time.time()))),
            valid_to=(float(raw["valid_to"]) if raw.get("valid_to") is not None else None),
            superseded_by=str(raw.get("superseded_by", "")),
            transferable_to=tuple(str(x) for x in (raw.get("transferable_to") or ())),
            topic=str(raw.get("topic", "")),
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
                                 tags=("recovered",), approved=True))
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
                # 合并按 text 对齐：kind 变化（F-05 语义更新）不产生双条，
                # 磁盘旧 kind 版本被内存新语义取代（不复活）；磁盘版已归档
                # 则同步归档态（不让 save 救回顾档条目）。
                known = {e.text.strip() for e in self.entries}
                for entry in disk.entries:
                    if entry.text.strip() not in known:
                        self.entries.append(entry)
                    else:
                        for mine in self.entries:
                            if mine.text.strip() == entry.text.strip():
                                mine.archived = mine.archived or entry.archived
                                break
        except Exception:
            pass  # 磁盘读取失败不阻断保存（保持原行为）
        lines = ["# Memory", "", "<!-- human-readable view; the JSON block below is authoritative -->", ""]
        for entry in self.entries:
            if entry.archived or entry.superseded_by or entry.valid_to is not None:
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
                if existing.text.strip() == text.strip() and existing.current:
                    existing.kind = kind
                    existing.tags = tuple(tags) if tags else existing.tags
                    if source:
                        existing.source = source
                    return existing
            entry = Entry(kind=kind, text=text.strip(), source=source, tags=tuple(tags),
                          approved=(source != "agent"))
            self.entries.append(entry)
            self.save()
            return entry

    def recall(self, kind: str | None = None, *, include_archived: bool = False,
               limit: int = 50) -> list[Entry]:
        out = []
        for entry in self.entries:
            if not include_archived and not entry.current:
                continue
            if kind and entry.kind != kind:
                continue
            out.append(entry)
        out.sort(key=lambda e: (not e.pinned, -e.created_at))
        return out[:limit]

    def supersede(self, text: str, replacement: str, *, kind: str | None = None,
                  source: str = "human", tags: Iterable[str] = (),
                  transferable_to: Iterable[str] = (), topic: str = "") -> Entry:
        """Seal one current entry and append its replacement.

        The old row remains in the authoritative file with ``valid_to`` and a
        ``superseded_by`` pointer.  Normal recall returns only the new leaf;
        ``include_archived=True`` retains the full audit trail.
        """
        old = next((entry for entry in self.entries
                    if entry.current and entry.text.strip() == str(text).strip()
                    and (kind is None or entry.kind == kind)), None)
        if old is None:
            raise KeyError(f"unknown current memory entry: {text!r}")
        replacement = str(replacement).strip()
        if not replacement:
            raise ValueError("replacement memory text must not be empty")
        now = time.time()
        new = Entry(kind=kind or old.kind, text=redact(replacement),
                    source=redact(source), tags=tuple(redact(list(tags))) or old.tags,
                    approved=(source != "agent"), created_at=now,
                    transferable_to=tuple(str(x) for x in transferable_to), topic=str(topic))
        old.valid_to = now
        old.superseded_by = new.entry_id
        with self._lock:
            self.entries.append(new)
            self.save()
        return new

    def validate_chains(self) -> list[str]:
        """Return supersession-chain defects without mutating the store."""
        by_id = {entry.entry_id: entry for entry in self.entries if entry.entry_id}
        problems: list[str] = []
        for entry in self.entries:
            target = entry.superseded_by
            if not target:
                continue
            if target not in by_id:
                problems.append(f"{entry.entry_id}: missing superseded target {target}")
                continue
            seen = {entry.entry_id}
            cursor = target
            while cursor:
                if cursor in seen:
                    problems.append(f"{entry.entry_id}: supersession cycle")
                    break
                seen.add(cursor)
                cursor = by_id.get(cursor).superseded_by if by_id.get(cursor) else ""
        return problems

    def promote_to_context(self, text: str) -> bool:
        """QA-03: 宿主/审批流程显式放行一条 agent 记忆进入生效上下文。"""
        with self._lock:
            for entry in self.entries:
                if entry.text.strip() == text.strip():
                    entry.approved = True
                    self.save()
                    return True
        return False

    @property
    def generation(self) -> tuple[int, float]:
        """Cheap fingerprint of the current (non-archived) entries.

        remember / supersede / restore / curate 都会改变活跃条目数或最新
        created_at，这对二元组足以检出每一次内容变化，而无需每请求对全
        部文本做哈希。调用方用它冻结记忆快照（前缀缓存稳定）并探测增长。
        """
        current = [e for e in self.entries if e.current]
        newest = max((e.created_at for e in current), default=0.0)
        return (len(current), newest)

    def context_slice(self, max_chars: int = 2000) -> str:
        """The bounded slice that gets injected into the system prompt."""
        chunks: list[str] = []
        used = 0
        for entry in self.recall():
            # QA-03：未放行的 agent 记忆不进注入上下文。
            if getattr(entry, "approved", True) is False:
                continue
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
