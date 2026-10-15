"""Graph-state checkpoints: pause any run, resume exactly where it stopped.

Upstream LangGraph's BaseCheckpointSaver contract (libs/checkpoint,
5eb60ae) adapted to forge: plain JSON documents, JSONL append for the
file saver, no serde dependency. This is the *state* twin of forge's
shadow-git checkpoints (forge/checkpoint.py snapshots files; this
snapshots graph execution state).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecutionCheckpoint:
    """State snapshot at a superstep boundary (upstream Checkpoint v1)."""

    thread_id: str
    step: int
    channel_values: dict[str, Any] = field(default_factory=dict)
    channel_versions: dict[str, int] = field(default_factory=dict)
    versions_seen: dict[str, dict[str, int]] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    # node -> writes applied before the pause (resume replays them)
    pending_writes: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "thread_id": self.thread_id,
                "step": self.step,
                "channel_values": self.channel_values,
                "channel_versions": self.channel_versions,
                "versions_seen": self.versions_seen,
                "ts": self.ts,
                "pending_writes": {
                    node: list(w) for node, w in self.pending_writes.items()
                },
            },
            default=_json_default,
        )

    @classmethod
    def from_json(cls, raw: str) -> "ExecutionCheckpoint":
        data = json.loads(raw)
        return cls(
            thread_id=data["thread_id"],
            step=data["step"],
            channel_values=data.get("channel_values", {}),
            channel_versions=data.get("channel_versions", {}),
            versions_seen=data.get("versions_seen", {}),
            ts=data.get("ts", 0.0),
            pending_writes=data.get("pending_writes", {}),
        )


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


class BaseGraphSaver:
    """Contract: get/put/list per thread (upstream BaseCheckpointSaver)."""

    def get(self, thread_id: str) -> ExecutionCheckpoint | None:
        raise NotImplementedError

    def put(self, checkpoint: ExecutionCheckpoint) -> None:
        raise NotImplementedError

    def list(self, thread_id: str, limit: int = 20) -> list[ExecutionCheckpoint]:
        raise NotImplementedError


class InMemorySaver(BaseGraphSaver):
    """Thread-safe in-memory saver (tests, single-session use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, list[ExecutionCheckpoint]] = {}

    def get(self, thread_id: str) -> ExecutionCheckpoint | None:
        with self._lock:
            history = self._threads.get(thread_id)
            return history[-1] if history else None

    def put(self, checkpoint: ExecutionCheckpoint) -> None:
        with self._lock:
            self._threads.setdefault(checkpoint.thread_id, []).append(checkpoint)

    def list(self, thread_id: str, limit: int = 20) -> list[ExecutionCheckpoint]:
        with self._lock:
            return list(self._threads.get(thread_id, [])[::-1][:limit])


class FileGraphSaver(BaseGraphSaver):
    """Append-only JSONL persistence, one file per thread.

    The file lives under ``home/graph_checkpoints/<thread_id>.jsonl``.
    Every ``put`` appends one line; ``get`` reads the last line. Compact
    enough for forge's per-session volumes and trivially inspectable
    (each line is a complete state snapshot — grep-able, diff-able).
    """

    def __init__(self, home: Path) -> None:
        self.root = Path(home) / "graph_checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, thread_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in thread_id)
        return self.root / f"{safe}.jsonl"

    def get(self, thread_id: str) -> ExecutionCheckpoint | None:
        path = self._path(thread_id)
        if not path.exists():
            return None
        last: ExecutionCheckpoint | None = None
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        last = ExecutionCheckpoint.from_json(line)
                    except json.JSONDecodeError:
                        continue  # torn tail line: keep the previous good one
        return last

    def put(self, checkpoint: ExecutionCheckpoint) -> None:
        path = self._path(checkpoint.thread_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(checkpoint.to_json() + "\n")

    def list(self, thread_id: str, limit: int = 20) -> list[ExecutionCheckpoint]:
        path = self._path(thread_id)
        if not path.exists():
            return []
        out: list[ExecutionCheckpoint] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(ExecutionCheckpoint.from_json(line))
                    except json.JSONDecodeError:
                        continue
        return out[::-1][:limit]


__all__ = [
    "ExecutionCheckpoint",
    "BaseGraphSaver",
    "InMemorySaver",
    "FileGraphSaver",
]
