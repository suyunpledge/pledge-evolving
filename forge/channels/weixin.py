"""WeChat (Weixin) channel via the Tencent iLink bot API.

Transport: JSON over HTTPS to ``https://ilinkai.weixin.qq.com/``. Auth is a
bearer token obtained by scanning a QR code once; the token is long-lived and
belongs to the *bot account*, not the user.

Endpoints used:

  ilink/bot/getupdates    long-poll for inbound messages (opaque cursor)
  ilink/bot/sendmessage   deliver one outbound message
  ilink/bot/sendtyping    typing indicator (optional, best-effort)
  ilink/bot/getconfig     fetch per-user config (typing ticket)

Design notes, each from a failure that has actually bitten:

* **Cursor is persisted.** The long-poll hands back an opaque buffer; dropping
  it either replays the backlog (duplicate replies) or skips messages. See
  ``channels.Cursor``.
* **msg_id is deduped.** The surface delivers at-least-once. Without a seen-set
  a retry makes the bot answer the same message twice.
* **Replies are chunked.** The surface caps message length; one oversized reply
  is rejected whole, so the user gets nothing. See ``channels.chunk_text``.
* **Token never lands in logs or config.** It is read from the credential file
  or an env var, and every log line passes through ``_redact``.
* **Fail closed.** A missing token raises at construction time rather than
  producing a channel that silently drops outbound messages.

The token file format is shared with the OpenClaw weixin plugin so an existing
login can be imported instead of re-scanning:

    {
      "token": "...",
      "baseUrl": "https://ilinkai.weixin.qq.com",
      "userId": "o9cq...@im.wechat",
      "savedAt": "2026-09-13T04:56:27.806Z"
    }
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import (
    Cursor,
    InboundMessage,
    SeenSet,
    chunk_text,
    register_channel,
)

# ---------------------------------------------------------------------------
# protocol constants (mirrored from the iLink bot API)
# ---------------------------------------------------------------------------

class PollTimeout(RuntimeError):
    """The long-poll window expired with no messages.

    Distinct from a transport error on purpose: "nothing happened" must not be
    reported as a failure, or every idle poll looks like an outage.
    """


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"

# MessageType / MessageState / MessageItemType
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TYPE_TEXT = 1
ITEM_TYPE_IMAGE = 2
ITEM_TYPE_VOICE = 3
ITEM_TYPE_FILE = 4
ITEM_TYPE_VIDEO = 5

DEFAULT_POLL_TIMEOUT_MS = 30_000
DEFAULT_SEND_TIMEOUT_MS = 20_000
DEFAULT_CHUNK_LIMIT = 2000


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _client_version(version: str = "2.4.3") -> int:
    """Encode a semver into the integer the API expects.

    ``major<<16 | minor<<8 | patch``; e.g. "2.4.3" -> 0x020403.
    """
    parts = []
    for piece in version.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _wechat_uin() -> str:
    """``X-WECHAT-UIN``: base64 of a random uint32 rendered as decimal text."""
    value = secrets.randbits(32)
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _redact(text: str, token: str) -> str:
    """Strip the token from anything headed for a log or an exception message."""
    if token and token in text:
        return text.replace(token, "***")
    return text


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@dataclass
class WeixinConfig:
    """One bot account's config row.

    ``token`` is resolved in this order (first hit wins):

    1. ``tokenEnv`` — the name of an env var holding the token (preferred:
       keeps the secret out of the config file entirely)
    2. ``tokenFile`` — a JSON file with a ``token`` field
    3. ``token`` — an inline value (last resort; ``doctor`` will flag it)

    An inline token is accepted because it is sometimes the only option, but it
    is a known-insecure shape and is surfaced as such.
    """

    enabled: bool = False
    account_id: str = "default"
    base_url: str = DEFAULT_BASE_URL
    token_env: str = ""
    token_file: str = ""
    token: str = ""
    bot_agent: str = "forge"
    session_scope: str = "per-peer"
    poll_timeout_ms: int = DEFAULT_POLL_TIMEOUT_MS
    send_timeout_ms: int = DEFAULT_SEND_TIMEOUT_MS
    chunk_limit: int = DEFAULT_CHUNK_LIMIT
    state_dir: str = ""

    @classmethod
    def from_row(cls, config: dict[str, Any]) -> "WeixinConfig":
        return cls(
            enabled=bool(config.get("enabled", False)),
            account_id=str(config.get("accountId", config.get("account_id", "default"))),
            base_url=str(config.get("baseUrl", config.get("base_url", DEFAULT_BASE_URL))).rstrip("/"),
            token_env=str(config.get("tokenEnv", config.get("token_env", ""))),
            token_file=str(config.get("tokenFile", config.get("token_file", ""))),
            token=str(config.get("token", "")),
            bot_agent=str(config.get("botAgent", config.get("bot_agent", "forge"))),
            session_scope=str(config.get("sessionScope", config.get("session_scope", "per-peer"))),
            poll_timeout_ms=int(config.get("pollTimeoutMs", config.get("poll_timeout_ms", DEFAULT_POLL_TIMEOUT_MS))),
            send_timeout_ms=int(config.get("sendTimeoutMs", config.get("send_timeout_ms", DEFAULT_SEND_TIMEOUT_MS))),
            chunk_limit=int(config.get("chunkLimit", config.get("chunk_limit", DEFAULT_CHUNK_LIMIT))),
            state_dir=str(config.get("stateDir", config.get("state_dir", ""))),
        )

    def resolve_token(self) -> str:
        """Resolve the bearer token, or raise if none is configured.

        Raising (rather than returning "") is deliberate: a channel without a
        token can only fail on every send, which is worse than not starting.
        """
        if self.token_env:
            value = os.environ.get(self.token_env, "").strip()
            if value:
                return value
        if self.token_file:
            path = Path(self.token_file).expanduser()
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    value = str(data.get("token", "")).strip()
                    if value:
                        return value
                except (json.JSONDecodeError, OSError) as exc:
                    raise RuntimeError(
                        f"token file {path} is unreadable or malformed: {exc}"
                    ) from exc
        if self.token:
            return self.token.strip()
        raise RuntimeError(
            "no weixin token configured: set tokenEnv (preferred), tokenFile, or token; "
            "run `forge channel weixin import` to adopt an existing login"
        )

    def has_token_source(self) -> bool:
        return bool(self.token_env or self.token_file or self.token)


# ---------------------------------------------------------------------------
# channel
# ---------------------------------------------------------------------------

class WeixinChannel:
    """The iLink bot channel. See module docstring for the protocol notes."""

    name = "weixin"

    def __init__(self, config: dict[str, Any] | Path) -> None:
        if isinstance(config, Path):
            config = {"enabled": True, "tokenFile": str(config)}
        self.cfg = WeixinConfig.from_row(config)
        self._token = self.cfg.resolve_token()
        self._client_version = _client_version()
        # cursor + dedup live under the state dir so a restart resumes cleanly
        state_root = Path(self.cfg.state_dir).expanduser() if self.cfg.state_dir else (
            Path.home() / ".forge" / "channels" / "weixin"
        )
        state_root.mkdir(parents=True, exist_ok=True)
        # Exposed so a caller can put its own bookkeeping (e.g. a serve lock)
        # next to the cursor it must not race with.
        self.state_dir = state_root
        self._cursor = Cursor(state_root / f"{self.cfg.account_id}.cursor.json")
        self._seen = SeenSet(state_root / f"{self.cfg.account_id}.seen.json")
        self._typing_ticket = ""

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._token}",
            "X-WECHAT-UIN": _wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(self._client_version),
        }

    def _payload(self, body: dict[str, Any]) -> dict[str, Any]:
        """Merge the request body with ``base_info``.

        Split out from ``_post`` so the wire shape can be asserted without a
        network call.
        """
        return {**body, "base_info": {"bot_agent": self.cfg.bot_agent}}

    def _post(self, endpoint: str, body: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        url = f"{self.cfg.base_url}/{endpoint}"
        data = json.dumps(self._payload(body), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=self._headers(), method="POST"
        )
        timeout = max(timeout_ms, 1000) / 1000.0
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = _redact(exc.read().decode("utf-8", "replace")[:300], self._token)
            raise RuntimeError(f"{endpoint} HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            # A read timeout is how a long-poll says "nothing yet". It is not a
            # failure, so it gets its own exception type the caller can tell
            # apart from a broken connection.
            raise PollTimeout(str(exc)) from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            raise RuntimeError(f"{endpoint} transport error: {_redact(repr(exc), self._token)}") from exc
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{endpoint} returned non-JSON: {_redact(raw[:200], self._token)}"
            ) from exc
        return parsed if isinstance(parsed, dict) else {}

    # -- inbound -----------------------------------------------------------

    # Long-poll windows: the server suggests one per response. Clamp the
    # adopted value so a bogus suggestion cannot pin the loop either way.
    MIN_POLL_MS = 5000
    MAX_POLL_MS = 60_000

    def poll(self, *, limit: int = 10) -> list[InboundMessage]:
        """Long-poll once and return normalised inbound messages.

        Returns ``[]`` when the poll window expires with nothing new — that is
        the common case and must not look like an error.

        The cursor advances on every successful poll, so a crash mid-batch does
        not replay messages already handed to the loop; dedup by ``msg_id``
        covers the remaining at-least-once window.
        """
        cursor_key = f"{self.name}:{self.cfg.account_id}"
        try:
            resp = self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": self._cursor.get(cursor_key)},
                self.cfg.poll_timeout_ms,
            )
        except PollTimeout:
            return []

        # `ret` is the envelope's general code; `errcode` is the more specific
        # one. Either being non-zero means the batch is not trustworthy.
        ret = resp.get("ret")
        errcode = resp.get("errcode")
        if ret not in (None, 0) or errcode not in (None, 0):
            # The server's own message goes through _redact too: it is not a
            # path we control, so it is not assumed token-free.
            detail = _redact(str(resp.get("errmsg") or ""), self._token)
            # -14 is a documented session timeout: the credential needs renewal.
            raise RuntimeError(
                f"getupdates failed ret={ret} errcode={errcode}: {detail}"
            )

        # Adopt the server's suggested window for the next poll.
        suggested = resp.get("longpolling_timeout_ms")
        if isinstance(suggested, int) and suggested > 0:
            self.cfg.poll_timeout_ms = max(self.MIN_POLL_MS, min(self.MAX_POLL_MS, suggested))

        new_cursor = resp.get("get_updates_buf")
        if isinstance(new_cursor, str) and new_cursor:
            self._cursor.set(cursor_key, new_cursor)

        out: list[InboundMessage] = []
        for raw in resp.get("msgs") or []:
            if not isinstance(raw, dict):
                continue
            msg = self._normalise(raw)
            if msg is None:
                continue
            if self._seen.seen(msg.msg_id):
                continue
            self._seen.add(msg.msg_id, save=False)
            out.append(msg)
            if len(out) >= limit:
                break
        # one write per batch instead of one per message
        if out:
            self._seen.save()
        return out

    def _normalise(self, raw: dict[str, Any]) -> InboundMessage | None:
        """Turn one protocol message into an ``InboundMessage``.

        Returns ``None`` for anything the loop should not see: bot-originated
        messages (echo), textless items, or messages with no sender.
        """
        # message_type 1 = USER, 2 = BOT. Ignore our own echoes.
        if int(raw.get("message_type") or 0) != 1:
            return None
        sender = str(raw.get("from_user_id") or "").strip()
        if not sender:
            return None

        text = self._extract_text(raw)
        if not text:
            return None

        return InboundMessage(
            channel=self.name,
            account=self.cfg.account_id,
            peer=sender,
            text=text,
            msg_id=str(raw.get("message_id") or raw.get("msg_id") or raw.get("client_id") or ""),
            group=str(raw.get("group_id") or ""),
            raw=raw,
        )

    @staticmethod
    def _extract_text(raw: dict[str, Any]) -> str:
        """Concatenate text from ``item_list``.

        Voice items carry a server-side transcription in ``voice_item.text``,
        which is used when present — for a text-first agent surface that is
        more useful than a media handle.
        """
        pieces: list[str] = []
        for item in raw.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            item_type = int(item.get("type") or 0)
            if item_type == ITEM_TYPE_TEXT:
                value = (item.get("text_item") or {}).get("text")
                if value:
                    pieces.append(str(value))
            elif item_type == ITEM_TYPE_VOICE:
                value = (item.get("voice_item") or {}).get("text")
                if value:
                    pieces.append(str(value))
            elif item_type in (ITEM_TYPE_IMAGE, ITEM_TYPE_FILE, ITEM_TYPE_VIDEO):
                # A media-only message is surfaced as a placeholder so the loop
                # can answer "I can't read that yet" instead of going silent.
                label = {ITEM_TYPE_IMAGE: "[图片]", ITEM_TYPE_FILE: "[文件]", ITEM_TYPE_VIDEO: "[视频]"}[item_type]
                pieces.append(label)
        return "".join(pieces).strip()

    # -- outbound ----------------------------------------------------------

    def send(self, to: str, text: str, *, group: str = "") -> int:
        """Deliver ``text`` to ``to``, chunked to the surface limit.

        Returns the number of chunks delivered, so a caller can tell "sent"
        from "there was nothing to send".
        """
        if not to:
            raise ValueError("send() requires a recipient")
        chunks = chunk_text(text, self.cfg.chunk_limit)
        for chunk in chunks:
            self._send_one(to, chunk)
        return len(chunks)

    def _send_one(self, to: str, text: str) -> None:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": secrets.token_hex(16),
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": ITEM_TYPE_TEXT, "text_item": {"text": text}}],
            }
        }
        self._post("ilink/bot/sendmessage", body, self.cfg.send_timeout_ms)

    def send_typing(self, to: str, *, typing: bool = True) -> bool:
        """Best-effort typing indicator. Returns False when unavailable.

        A typing indicator is a nicety; a failure must never abort a reply, so
        this swallows errors and reports the outcome instead of raising.
        """
        try:
            if not self._typing_ticket:
                cfg = self._post(
                    "ilink/bot/getconfig", {"ilink_user_id": to}, self.cfg.send_timeout_ms
                )
                self._typing_ticket = str(cfg.get("typing_ticket") or "")
            if not self._typing_ticket:
                return False
            self._post(
                "ilink/bot/sendtyping",
                {
                    "ilink_user_id": to,
                    "typing_ticket": self._typing_ticket,
                    "status": 1 if typing else 2,
                },
                self.cfg.send_timeout_ms,
            )
            return True
        except Exception:
            return False

    # -- diagnostics -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Probe the credential without sending anything.

        ``getconfig`` is a read-only endpoint, so this is safe to run before
        enabling the channel.
        """
        info: dict[str, Any] = {
            "channel": self.name,
            "account": self.cfg.account_id,
            "base_url": self.cfg.base_url,
            "token_source": self._token_source(),
            "token_present": bool(self._token),
            "poll_timeout_ms": self.cfg.poll_timeout_ms,
        }
        try:
            # The bot's own id is what getconfig keys on; read it from the token
            # file when available, else fall back to a config probe.
            probe = self._bot_user_id() or ""
            resp = self._post("ilink/bot/getconfig", {"ilink_user_id": probe}, 10_000)
            info["reachable"] = True
            info["ret"] = resp.get("ret")
            info["has_typing_ticket"] = bool(resp.get("typing_ticket"))
        except Exception as exc:
            info["reachable"] = False
            info["error"] = _redact(str(exc), self._token)
        return info

    def _token_source(self) -> str:
        if self.cfg.token_env and os.environ.get(self.cfg.token_env):
            return f"env:{self.cfg.token_env}"
        if self.cfg.token_file and Path(self.cfg.token_file).expanduser().is_file():
            return f"file:{self.cfg.token_file}"
        if self.cfg.token:
            return "inline (insecure — move to env or file)"
        return "none"

    def _bot_user_id(self) -> str:
        """Read the bot's own user id from the token file, when one is set."""
        if not self.cfg.token_file:
            return ""
        path = Path(self.cfg.token_file).expanduser()
        if not path.is_file():
            return ""
        try:
            return str(json.loads(path.read_text(encoding="utf-8-sig")).get("userId", ""))
        except (json.JSONDecodeError, OSError):
            return ""


# ---------------------------------------------------------------------------
# credential import (adopt an existing OpenClaw weixin login)
# ---------------------------------------------------------------------------

OPENCLAW_WEIXIN_DIR = Path.home() / ".openclaw-autoclaw" / "openclaw-weixin" / "accounts"


def find_openclaw_accounts() -> list[Path]:
    """List credential files from an OpenClaw weixin install, if present."""
    if not OPENCLAW_WEIXIN_DIR.is_dir():
        return []
    return sorted(p for p in OPENCLAW_WEIXIN_DIR.glob("*-im-bot.json"))


def import_openclaw_account(source: Path, dest: Path) -> dict[str, Any]:
    """Copy an existing credential into forge's credential store.

    Adopting a live login avoids a second QR scan for a bot that is already
    authorised. The token is copied verbatim; nothing is re-derived.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"credential not found: {source}")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    token = str(data.get("token") or "").strip()
    if not token:
        raise ValueError(f"credential at {source} has no token field")

    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "token": token,
                "baseUrl": data.get("baseUrl") or DEFAULT_BASE_URL,
                "userId": data.get("userId", ""),
                "savedAt": data.get("savedAt", ""),
                "importedFrom": str(source),
                "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass  # Windows may refuse; the file is still readable only by the user in practice
    return {
        "dest": str(dest),
        "account": source.stem.replace("-im-bot", ""),
        "baseUrl": data.get("baseUrl") or DEFAULT_BASE_URL,
        "userId": data.get("userId", ""),
    }


# register on import so ``channels.load_channel("weixin", ...)`` works
register_channel("weixin", WeixinChannel)

__all__ = [
    "WeixinChannel",
    "WeixinConfig",
    "find_openclaw_accounts",
    "import_openclaw_account",
    "OPENCLAW_WEIXIN_DIR",
]
