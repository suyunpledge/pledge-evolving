"""IM channels: an inbound/outbound contract for messaging surfaces.

Why this exists: forge's agent loop takes a task string and returns a report.
Every messaging surface (WeChat, Telegram, Slack, a webhook) wants the same
thing — turn an inbound message into a task, turn the report back into a
message. Writing that glue once per surface produces N copies of the same
three bugs (duplicate delivery, lost replies, crossed sessions).

So a channel here is a two-method contract:

* ``poll()`` yields inbound messages (long-poll or webhook drain)
* ``send()`` delivers one outbound message

What the contract deliberately does **not** do:

* it does not own the agent loop (the loop stays in ``loop.py``)
* it does not own session keys (``session_for()`` is a pure function so the
  mapping is testable without a network)
* it does not retry forever (a channel that cannot deliver raises; the caller
  decides whether to drop or requeue)

Channels are **off by default**. A channel only loads when its config row is
present and enabled — the same deny-by-default posture as policy and tools.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol


# ---------------------------------------------------------------------------
# inbound message
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    """One inbound message, normalised across surfaces.

    ``peer`` is the *other* party (a user id, not a display name). ``group`` is
    empty for direct messages. ``text`` is the extracted plain text — media
    extraction is the channel's problem, not the loop's.
    """

    channel: str
    account: str
    peer: str
    text: str
    msg_id: str = ""
    group: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return bool(self.group)

    def to_raw(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "account": self.account,
            "peer": self.peer,
            "group": self.group,
            "text": self.text,
            "msg_id": self.msg_id,
        }


# ---------------------------------------------------------------------------
# session mapping (pure function — testable without a network)
# ---------------------------------------------------------------------------

def session_for(msg: InboundMessage, scope: str = "per-peer") -> str:
    """Map an inbound message to a session key.

    Scopes mirror the lesson learned on multi-account setups: sharing one
    session bucket across accounts or peers crosses conversations.

    * ``shared``     — one bucket for the whole channel (single-user bots)
    * ``per-peer``   — account + peer (default; each DM is its own lane)
    * ``per-account-channel-peer`` — account + group + peer (strictest; use
      when several accounts are logged in and groups are involved)
    """
    if scope == "shared":
        return f"channel:{msg.channel}"
    if scope == "per-account-channel-peer":
        lane = msg.group or msg.peer
        return f"channel:{msg.channel}:{msg.account}:{lane}:{msg.peer}"
    # default: per-peer
    lane = msg.group or msg.peer
    return f"channel:{msg.channel}:{msg.account}:{lane}"


# ---------------------------------------------------------------------------
# channel contract
# ---------------------------------------------------------------------------

class Channel(Protocol):
    """The two-method contract every messaging surface implements."""

    name: str

    def poll(self, *, limit: int = 10) -> Iterable[InboundMessage]:
        """Yield inbound messages. Must not block past the channel timeout."""
        ...

    def send(self, to: str, text: str, *, group: str = "") -> None:
        """Deliver one outbound message. Raises on permanent failure."""
        ...


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

_CHANNELS: dict[str, type] = {}


def register_channel(name: str, cls: type) -> None:
    """Register a channel implementation under ``name``."""
    _CHANNELS[name] = cls


def available_channels() -> list[str]:
    return sorted(_CHANNELS)


def load_channel(name: str, config: dict[str, Any]) -> Channel:
    """Instantiate a registered channel with its config row.

    Raises ``KeyError`` when the channel is not registered — a typo in config
    must fail loudly rather than silently disabling the surface.
    """
    if name not in _CHANNELS:
        raise KeyError(
            f"unknown channel {name!r}; registered: {', '.join(available_channels()) or '(none)'}"
        )
    return _CHANNELS[name](config)


# ---------------------------------------------------------------------------
# durable cursor (so a restart does not replay the whole backlog)
# ---------------------------------------------------------------------------

class Cursor:
    """A tiny JSON cursor persisted per (channel, account).

    Long-poll APIs hand back an opaque buffer; losing it means either replaying
    old messages (duplicate replies) or skipping new ones. Persisting it is the
    difference between a bot that survives a restart and one that double-answers.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            self._data = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            # A corrupt cursor is not fatal — start clean rather than refuse to boot.
            self._data = {}

    def get(self, key: str) -> str:
        return self._data.get(key, "")

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# dedup (surfaces retry; the same msg_id must not be answered twice)
# ---------------------------------------------------------------------------

class SeenSet:
    """Bounded set of already-handled message ids.

    Messaging APIs deliver at-least-once. Without dedup, a retry produces a
    duplicate reply, which in a group chat looks like the bot talking to itself.
    """

    def __init__(self, path: Path, max_items: int = 2000) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self._items: list[str] = []
        self._index: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else []
            if isinstance(data, list):
                self._items = [str(x) for x in data][-self.max_items:]
                self._index = set(self._items)
        except (json.JSONDecodeError, OSError):
            self._items, self._index = [], set()

    def seen(self, msg_id: str) -> bool:
        return bool(msg_id) and msg_id in self._index

    def add(self, msg_id: str) -> None:
        if not msg_id or msg_id in self._index:
            return
        self._items.append(msg_id)
        self._index.add(msg_id)
        # evict oldest beyond the cap
        while len(self._items) > self.max_items:
            old = self._items.pop(0)
            self._index.discard(old)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._items[-self.max_items:], ensure_ascii=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# reply chunking (surfaces cap message length; long replies must be split)
# ---------------------------------------------------------------------------

def chunk_text(text: str, limit: int = 2000) -> list[str]:
    """Split a reply into surface-sized chunks, preferring paragraph breaks.

    A single 12k-character agent report sent to a surface with a 2k cap is
    rejected whole — the user gets nothing. Splitting on paragraph then line
    boundaries keeps the reply readable and deliverable.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        # prefer paragraph break, then newline, then space
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c]


__all__ = [
    "InboundMessage",
    "Channel",
    "Cursor",
    "SeenSet",
    "chunk_text",
    "session_for",
    "register_channel",
    "load_channel",
    "available_channels",
]
