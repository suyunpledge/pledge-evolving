"""forge/knowledge.py —— 用户可配置的踩坑知识库。

定位：与 MemoryStore（会话记忆）并列的「任务教训库」。
- Agent 踩坑（失败/纠正/绕路）→ pitfall 条目入库
- 新任务发送前 → 关键词检索相关条目 → 注入为背景上下文
- 存储复用 MemoryStore 持久化（同库不同 kind），检索纯标准库关键词打分。
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from .memory import MemoryStore

_KB_KINDS = ("pitfall", "lesson", "note")

_WORD = re.compile(r"[\w\u4e00-\u9fff]+")


def _keywords(text: str, limit: int = 24) -> list[str]:
    """轻量分词：英文/数字按词，中文按 2-gram。"""
    words: list[str] = []
    for token in _WORD.findall(text or ""):
        if re.fullmatch(r"[0-9_]+", token):
            continue
        if re.fullmatch(r"[a-zA-Z][\w-]*", token):
            words.append(token.lower())
        else:
            chars = list(token)
            if len(chars) == 1:
                words.append(chars[0])
            words.extend(a + b for a, b in zip(chars, chars[1:]))
    seen: set[str] = set()
    out = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:limit]


class KnowledgeBase:
    """踩坑知识库：存储（复用 MemoryStore）+ 关键词检索。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.store = MemoryStore(self.path).load()
        self._lock = threading.Lock()

    def record(self, text: str, *, source: str = "agent",
               tags: tuple[str, ...] = (), kind: str = "pitfall") -> object:
        """踩坑入库（幂等：同文本不重复）。"""
        if kind not in _KB_KINDS:
            kind = "pitfall"
        with self._lock:
            return self.store.remember(text, kind=kind, source=source,
                                       tags=tags or ("knowledge-base",))

    def search(self, query: str, *, limit: int = 5) -> list[tuple[object, float]]:
        """按任务文本关键词检索知识条目，(entry, score) 降序。"""
        qk = set(_keywords(query))
        if not qk:
            return []
        now = time.time()
        scored = []
        for entry in self.store.recall(include_archived=False, limit=500):
            if entry.kind not in _KB_KINDS:
                continue
            ek = set(_keywords(entry.text))
            if not ek:
                continue
            hit = qk & ek
            if not hit:
                continue
            coverage = len(hit) / min(len(ek), 30)
            age_days = max(0.0, (now - entry.created_at) / 86400.0)
            score = coverage + min(len(hit), 6) * 0.02 + max(0.0, 0.1 - age_days * 0.002)
            scored.append((entry, round(score, 4)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def context_for(self, task_text: str, *, max_chars: int = 1200) -> str:
        """给一次任务生成知识库背景上下文（无命中返回空串）。"""
        hits = self.search(task_text)
        if not hits:
            return ""
        lines = []
        used = 0
        for entry, score in hits:
            tag = "pitfall" if entry.kind == "pitfall" else "lesson"
            line = f"- [{tag}] {entry.text}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def entries(self, *, limit: int = 200):
        return [e for e in self.store.recall(include_archived=False, limit=limit)
                if e.kind in _KB_KINDS]

    def stats(self) -> dict:
        es = self.entries(limit=1000)
        by_kind: dict[str, int] = {}
        for e in es:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {"total": len(es), "by_kind": by_kind}
