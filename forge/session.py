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
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
from typing import Any, Iterator
from .secrets import redact


def _redact_record(record):
    """Forge's public rollout identity is not an HTTP session credential.

    Only the host-owned top-level session_id is exempt from field detection;
    its content still passes ordinary detection and known-value redaction.
    Nested configuration/session-token fields retain the normal secret gate.
    """
    if not isinstance(record, dict):
        return redact(record)
    public_id = record.get('session_id')
    safe = redact({k: v for k, v in record.items() if k != 'session_id'})
    if 'session_id' in record:
        safe['session_id'] = redact(public_id)
    return safe


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
            _redact_record({**self.data, "ordinal": self.ordinal, "type": self.type, "ts": self.ts}),
            ensure_ascii=False,
        )

    @staticmethod
    def from_line(line: str) -> "Event":
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("session record must be an object")
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
        self.meta: dict[str, Any] = _redact_record(dict(meta or {}))
        self._provided_meta = _redact_record(dict(meta or {}))
        self.meta.setdefault("session_id", self.path.stem)
        self.meta.setdefault("started_at", time.time())
        if forked_from:
            self.meta["forked_from"] = forked_from
        self._fh = None

    # -- lifecycle -------------------------------------------------------
    def open(self) -> "Session":
        if self._fh is not None:
            return self
        self._recover_tail()
        events: list[Event] = []
        for event in self.replay():
            if event.type == "session_meta":
                # the meta line is identity, not history
                self.meta = {**event.data, **self._provided_meta, "session_id": self.path.stem}
                continue
            events.append(event)
        self.events = events
        self._fh = self.path.open("a", encoding="utf-8")
        if self.path.stat().st_size == 0:
            try:
                self._write_line({**self.meta, "ordinal": 0, "type": "session_meta", "ts": time.time()})
            except BaseException:
                self.close()
                raise
        return self

    def _recover_tail(self) -> None:
        """Preserve an interrupted final record before removing it from the log."""
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        boundary = raw.rfind(b"\n") + 1
        # A corrupt completed record needs explicit repair, never silent removal.
        for line in raw[:boundary].decode("utf-8").splitlines():
            if line.strip():
                Event.from_line(line)
        tail = raw[boundary:]
        try:
            Event.from_line(tail.decode("utf-8"))
        except (ValueError, UnicodeError):
            backup = self.path.with_name(self.path.name + ".incomplete-" + uuid.uuid4().hex)
            backup.write_text(redact(tail.decode('utf-8', 'replace')), encoding='utf-8')
            with self.path.open("r+b") as stream:
                stream.truncate(boundary)
        else:
            with self.path.open("ab") as stream:
                stream.write(b"\n")

    def close(self) -> None:
        if self._fh is not None:
            handle = self._fh
            self._fh = None
            handle.close()

    def sync(self) -> None:
        """Durably record execution intent before tools with unknown outcomes."""
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())

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
        self._fh.write(json.dumps(_redact_record(payload), ensure_ascii=False) + "\n")
        self._fh.flush()

    def append(self, etype: str, **data: Any) -> Event:
        if self._fh is None:
            self.open()
        ordinal = (self.events[-1].ordinal + 1) if self.events else 1
        event = Event(ordinal=ordinal, type=etype, data=redact(data))
        # one write path only: never a second channel that can disagree with it
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        line = event.to_line() + "\n"
        try:
            self._fh.write(line)
            self._fh.flush()
        except OSError:
            # Reload the persisted log before any retry: a failed flush may
            # still have written a complete record, or left an incomplete tail.
            try:
                self.close()
            except OSError:
                pass
            raise
        self.events.append(event)
        return event

    # -- reading ---------------------------------------------------------
    def replay(self) -> Iterator[Event]:
        if not self.path.exists():
            return iter(())
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    event = Event.from_line(line)
                    event.data = _redact_record(event.data)
                    yield event

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
            if keep_last < 0:
                raise ValueError("keep_last must be nonnegative")
            source = source[-keep_last:] if keep_last else []
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

    VERSION = 3

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "index.json"

    def rebuild(self) -> dict[str, Any]:
        rows = []
        for file in sorted(self.root.glob("*.jsonl")):
            rows.append(self._summarise(file))
        payload = {"version": self.VERSION, "generated_at": time.time(), "sessions": rows}
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                             prefix=".index-", suffix=".tmp", delete=False) as stream:
                tmp = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        return payload

    def _summarise(self, file: Path) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        count = 0
        tokens = 0
        error = None
        workflow_events = deque(maxlen=512)
        workflow_id = None
        latest_phase = None
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            lines, error = [], str(exc)
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = _redact_record(json.loads(line))
                if not isinstance(raw, dict):
                    raise ValueError("session record must be an object")
                total_tokens = int(raw.get("total_tokens", 0) or 0)
            except (ValueError, TypeError) as exc:
                error = str(exc)
                break
            if raw.get("type") == "session_meta":
                meta = {k: v for k, v in raw.items() if k not in {"type"}}
                continue
            if raw.get("type") == "workflow_state" and raw.get("depth", 0) == 0:
                identifier = raw.get("run_id")
                if identifier != workflow_id:
                    workflow_id = identifier
                    workflow_events.clear()
                latest_phase = raw
            if workflow_id and raw.get("run_id") == workflow_id and raw.get("type") in {
                    "workflow_state", "tool_started", "tool_completed"}:
                workflow_events.append(raw)
            count += 1
            tokens += total_tokens
        from .execution_guard import workflow_status
        workflow = workflow_status([latest_phase, *workflow_events], workflow_id,
                                   assume_interrupted=False) if workflow_id else None
        return {
            "session_id": file.stem,
            "path": str(file),
            "events": count,
            "tokens": tokens,
            "meta": meta,
            **({"workflow": workflow} if workflow else {}),
            **({"error": error} if error else {}),
        }

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self.rebuild()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.rebuild()
        if not isinstance(payload, dict) or payload.get("version") != self.VERSION or not isinstance(payload.get("sessions"), list):
            return self.rebuild()
        return payload


__all__ = ["Event", "Session", "SessionIndex", "new_id"]
