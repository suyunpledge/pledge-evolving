"""Sessions as an append-only event log.

Design borrowed from Codex rollouts: one JSONL file per session, first line is
``session_meta`` (cwd, model, full instruction snapshot), every following line
carries a monotonically increasing ``ordinal``. ``resume`` and ``fork`` are
pure functions of that log, and the log doubles as a debugger.

Derived state (an index, per session projections) is versioned and rebuildable
— the log is the fact, everything else is a cache.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


def new_id(prefix: str = "s") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


@dataclass
class Event:
    ordinal: int
    type: str
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> str:
        return json.dumps(
            {"ordinal": self.ordinal, "type": self.type, "ts": self.ts, **self.data},
            ensure_ascii=False,
        )

    @staticmethod
    def from_line(line: str) -> "Event":
        raw = json.loads(line)
        ordinal = int(raw.pop("ordinal", 0))
        etype = str(raw.pop("type", "unknown"))
        ts = float(raw.pop("ts", time.time()))
        return Event(ordinal=ordinal, type=etype, ts=ts, data=raw)


class Session:
    """One append-only run log."""

    def __init__(self, path: Path, meta: dict[str, Any] | None = None,
                 *, forked_from: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[Event] = []
        self.meta: dict[str, Any] = dict(meta or {})
        self.meta.setdefault("session_id", self.path.stem)
        self.meta.setdefault("started_at", time.time())
        if forked_from:
            self.meta["forked_from"] = forked_from
        self._fh = None

    # -- lifecycle -------------------------------------------------------
    def open(self) -> "Session":
        if not self.path.exists():
            self._fh = self.path.open("a", encoding="utf-8")
            self._write_line({"ordinal": 0, "type": "session_meta", "ts": time.time(), **self.meta})
        else:
            self._fh = self.path.open("a", encoding="utf-8")
        events: list[Event] = []
        for event in self.replay():
            if event.type == "session_meta":
                # the meta line is identity, not history
                self.meta = {**event.data, **self.meta, "session_id": self.path.stem}
                continue
            events.append(event)
        self.events = events
        # _write_line already opened _fh above; if file existed, replay() closed
        # the read handle, so we opened a fresh append handle above.  No second open.
        if self._fh is None:
            self._fh = self.path.open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Session":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writing ---------------------------------------------------------
    def _write_line(self, payload: dict[str, Any]) -> None:
        # Use the shared file handle to avoid dual-write race conditions.
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._fh.flush()

    def append(self, etype: str, **data: Any) -> Event:
        ordinal = (self.events[-1].ordinal + 1) if self.events else 1
        event = Event(ordinal=ordinal, type=etype, data=data)
        self.events.append(event)
        # one write path only: never a second channel that can disagree with it
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        self._fh.write(event.to_line() + "\n")
        self._fh.flush()
        return event

    # -- reading ---------------------------------------------------------
    def replay(self) -> Iterator[Event]:
        if not self.path.exists():
            return iter(())
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield Event.from_line(line)

    def messages(self) -> list[dict[str, Any]]:
        """Project the log back into chat messages for the next turn."""
        out: list[dict[str, Any]] = []
        for event in self.events:
            if event.type == "user_message":
                out.append({"role": "user", "content": event.data.get("content", "")})
            elif event.type == "assistant_message":
                out.append({"role": "assistant", "content": event.data.get("content", "")})
            elif event.type == "tool_call":
                out.append({
                    "role": "assistant",
                    "content": f"[tool:{event.data.get('tool')}] {json.dumps(event.data.get('args', {}), ensure_ascii=False)[:300]}",
                })
                out.append({"role": "user", "content": str(event.data.get("result", ""))[:1500]})
        return out

    def fork(self, path: Path, *, keep_last: int | None = None) -> "Session":
        """Copy the log (optionally truncated) into a new session file."""
        source = [e for e in self.events if e.type != "session_meta"]
        if keep_last is not None:
            source = source[-keep_last:]
        child = Session(path, meta={**self.meta, "session_id": Path(path).stem},
                        forked_from=str(self.meta.get("session_id")))
        child.open()
        for event in source:
            child.append(event.type, **event.data)
        return child

    def tokens(self) -> dict[str, Any]:
        prompt = sum(int(e.data.get("prompt_tokens", 0)) for e in self.events)
        completion = sum(int(e.data.get("completion_tokens", 0)) for e in self.events)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total": prompt + completion}


class SessionIndex:
    """Rebuildable projection over a session directory."""

    VERSION = 2

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.json"

    def rebuild(self) -> dict[str, Any]:
        rows = []
        for file in sorted(self.root.glob("*.jsonl")):
            rows.append(self._summarise(file))
        payload = {"version": self.VERSION, "generated_at": time.time(), "sessions": rows}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _summarise(self, file: Path) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        count = 0
        tokens = 0
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("type") == "session_meta":
                meta = {k: v for k, v in raw.items() if k not in {"type"}}
                continue
            count += 1
            tokens += int(raw.get("total_tokens", 0) or 0)
        return {
            "session_id": file.stem,
            "path": str(file),
            "events": count,
            "tokens": tokens,
            "meta": meta,
        }

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self.rebuild()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != self.VERSION:
            return self.rebuild()
        return payload


__all__ = ["Event", "Session", "SessionIndex", "new_id"]
