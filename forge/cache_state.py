"""Cache continuity across a mixed fleet.

The problem the previous layer only *reported*
---------------------------------------------
Caches are isolated per vendor: an Anthropic cache entry is invisible to
DeepSeek, and vice versa. In a single-vendor fleet this costs nothing — every
hop reads the same warm prefix. In a mixed fleet every hop to a new vendor pays
the full cold prefix again, which silently erases the benefit of the long
system prompt the framework spent so much effort keeping stable.

Reporting that is not enough. The framework should *act* on it:

* **remember** where a prefix is warm, per vendor
* **prefer** a warm vendor when the quality tier allows it (stickiness)
* **price the hop** — "moving to X costs ¥Y of cold prefix" — so the routing
  decision is made on total cost, not on list price

The store is deliberately tiny and explicit: a dict of fingerprints with a
timestamp. It is not a cache itself — the vendor does the caching. This only
tracks *beliefs about warmth*, which is what routing needs.

Concurrency
-----------
An RLock protects threads and a bounded OS file lock protects processes.
Every transaction reloads shared state; mutations atomically replace JSON.
An old instance cannot resurrect revoked state by saving an old snapshot.
Warmth remains advisory telemetry, not proof of a future vendor cache hit.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .vendors import VendorProfile

# TTL in seconds, keyed by the label used in vendors.PROFILES.
_TTL_SECONDS: dict[str, float] = {
    "5m": 300.0,
    "1h": 3600.0,
    "30m": 1800.0,
    # Disk / managed caches are cleared "after a few hours to a few days" and
    # "periodically" respectively. A conservative 3 hours keeps the framework
    # from over-trusting warmth it cannot observe.
    "disk": 10_800.0,
    "managed": 10_800.0,
    "unknown": 0.0,
    "none": 0.0,
}

# A vendor that cannot be observed (unknown cache behaviour) is never treated
# as warm, because assuming warmth there is exactly the kind of invented
# discount the vendor table forbids.
_UNTRUSTWORTHY = {"unknown", "none"}


def prefix_fingerprint(payload: dict[str, Any], *, model: str = "",
                       endpoint: str = "", account: str = "", wire: str = "",
                       cache_mode: str = "") -> str:
    """Stable hash of everything a vendor's cache would key on.

    Included: tools, the top-level ``system`` value **in full structure**
    (so a non-text block such as an image differs from another image), any
    ``system``-role messages, and the cache namespace dimensions — model,
    endpoint and account. Those three matter because a cache entry is scoped
    to all of them: the same prompt on a different model, a different gateway
    or a different key is a different cache entry, and treating them as
    interchangeable would make the warmth store lie.

    Excluded: non-system messages, because they grow every turn — including
    them would make every fingerprint unique and the store useless.

    The account string is hashed, never stored, so a key cannot leak through
    the plan that carries the fingerprint.
    """
    system_messages = []
    for message in (payload.get("messages") or []):
        if isinstance(message, dict) and message.get("role") in ("system", "developer"):
            system_messages.append(message.get("content"))
    blob = json.dumps(
        {
            "tools": payload.get("tools") or [],
            "system": payload.get("system"),
            "system_messages": system_messages,
            "model": str(model or ""),
            "endpoint": str(endpoint or ""),
            "account": str(account or ""),
            "wire": wire, "cache_mode": cache_mode,
        },
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _ttl_seconds(profile: VendorProfile | None) -> float:
    if profile is None:
        return 10_800.0
    return _TTL_SECONDS.get(profile.cache_ttl, 0.0)


@dataclass
class Hop:
    """What moving a conversation from one vendor to another actually costs."""

    target: str
    cold_tokens: int
    cold_cost: float
    warm: bool
    reason: str

    def to_raw(self) -> dict[str, Any]:
        return {"target": self.target, "cold_tokens": self.cold_tokens,
                "cold_cost": round(self.cold_cost, 6), "warm": self.warm,
                "reason": self.reason}


class CacheWarmth:
    """Per-vendor belief about which prefixes are still cached.

    Persistable to JSON so warmth survives a process restart — a cache that
    lives 5 minutes is worth remembering across a CLI invocation, otherwise
    every cold start re-pays a prefix the vendor still has.
    """

    def __init__(self, path: Any = None) -> None:
        from pathlib import Path

        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        # vendor -> {fingerprint: last_seen_epoch}
        self._seen: dict[str, dict[str, float]] = {}
        if self.path and self.path.is_file():
            # Windows readers deny atomic replacement while their handle is
            # open. Even the initial read must share the transaction lock.
            with self._transaction():
                pass

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            seen = raw.get("seen") or {}
        except (json.JSONDecodeError, OSError, AttributeError, TypeError, ValueError):
            # 损坏的记录不该让一次请求失败——退回空表，宁可少算温暖度。
            self._seen = {}
            return
        clean: dict[str, dict[str, float]] = {}
        if isinstance(seen, dict):
            for vendor, entries in seen.items():
                if not isinstance(entries, dict):
                    continue
                bucket: dict[str, float] = {}
                for fingerprint, stamp in entries.items():
                    try:
                        value = float(stamp)
                        if math.isfinite(value) and value >= 0:
                            bucket[str(fingerprint)] = value
                    except (TypeError, ValueError):
                        continue
                if bucket:
                    clean[str(vendor)] = bucket
        self._seen = clean

    @contextmanager
    def _transaction(self, write=False):
        """Reload under an OS lock; locks release automatically after a crash.

        A busy or unavailable store falls back to an empty local belief. The
        wait is bounded because cache metadata must not stall model requests.
        """
        with self._lock:
            if not self.path:
                yield
                return
            fh = None
            locked = False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fh = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
                if fh.tell() == 0:
                    fh.write(b"0"); fh.flush()
                deadline = time.monotonic() + .5
                while True:
                    try:
                        fh.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("cache metadata lock busy")
                        time.sleep(.01)
                self._load()
            except OSError:
                self._seen = {}
            try:
                yield
                if write and locked:
                    self._write_locked()
            finally:
                if locked:
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                if fh:
                    fh.close()

    def _write_locked(self):
        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_text(json.dumps({"seen": self._seen, "saved_at": time.time()},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def save(self) -> None:
        # Mutations persist in their own transaction. Never write an old
        # instance snapshot back over a newer process's revocation.
        with self._transaction(write=True):
            pass

    def clear(self) -> None:
        with self._transaction(write=True):
            self._seen.clear()

    # -- recording --------------------------------------------------------
    def remember(self, vendor: str, fingerprint: str,
                 when: float | None = None) -> None:
        if not vendor or not fingerprint:
            return
        with self._transaction(write=True):
            self._seen.setdefault(str(vendor), {})[str(fingerprint)] = (
                when if when is not None else time.time())

    def forget(self, vendor: str, fingerprint: str) -> None:
        with self._transaction(write=True):
            self._seen.get(str(vendor), {}).pop(str(fingerprint), None)

    # -- querying ---------------------------------------------------------
    def is_warm(self, profile: VendorProfile, fingerprint: str,
                when: float | None = None) -> bool:
        if not fingerprint:
            return False
        if profile.id in _UNTRUSTWORTHY or profile.cache_ttl in _UNTRUSTWORTHY:
            return False
        ttl = _ttl_seconds(profile)
        if ttl <= 0:
            return False
        with self._transaction():
            last = self._seen.get(profile.id, {}).get(str(fingerprint))
        if last is None:
            return False
        now = when if when is not None else time.time()
        age = now - last
        # 未来时间戳（时钟回拨、损坏记录）不当作“热”——负年龄会永远 <= ttl，
        # 变成一条永不过期的假记录。
        if age < 0:
            return False
        return age <= ttl

    def warm_vendors(self, fingerprint: str,
                     profiles: Iterable[VendorProfile],
                     when: float | None = None) -> list[str]:
        return [p.id for p in profiles if self.is_warm(p, fingerprint, when)]

    # -- routing ----------------------------------------------------------
    def hop_cost(self, profile: VendorProfile, fingerprint: str, *,
                 prefix_tokens: int, base_input: float, calls_expected: int = 1,
                 when: float | None = None) -> Hop:
        """Price a hop to ``profile``.

        Warm → the prefix is read at the hit rate. Cold → the first call pays
        full rate for the prefix (and, on write-charging vendors, the write
        premium). The extra cost is what routing should be comparing against
        the list-price difference that made the hop look attractive.
        """
        warm = self.is_warm(profile, fingerprint, when)
        p = base_input / 1_000_000.0
        if warm:
            cost = prefix_tokens * p * profile.cache_read_multiplier
            return Hop(profile.id, 0, cost, True, "前缀在该厂商仍有缓存，按命中价续用")
        write_mult = profile.cache_write_multiplier if profile.explicit_cache else 1.0
        cost = prefix_tokens * p * write_mult
        return Hop(profile.id, prefix_tokens, cost, False,
                   f"该厂商无缓存，前缀需冷启动按全价重算（写入倍率 {write_mult:g}×）")

    def rank_candidates(self, candidates: Iterable[tuple[str, VendorProfile]],
                        fingerprint: str, *,
                        prefix_tokens: int, base_input: float,
                        when: float | None = None) -> list[dict[str, Any]]:
        """Order a candidate list by *total* cost, warmth included.

        The tier ordering the router produced reflects quality and list price.
        This pass adds the one thing the router cannot know: whether the
        conversation already has a warm prefix somewhere in the candidate set.
        A vendor that is one tier more expensive but warm can still be cheaper
        in practice, and this is where that becomes visible.
        """
        rows: list[dict[str, Any]] = []
        for row_id, profile in candidates:
            hop = self.hop_cost(profile, fingerprint, prefix_tokens=prefix_tokens,
                                base_input=base_input, when=when)
            rows.append({"row_id": row_id, "vendor": profile.id,
                         "warm": hop.warm, "hop_cost": round(hop.cold_cost, 6),
                         "reason": hop.reason})
        return sorted(rows, key=lambda r: (not r["warm"], r["hop_cost"]))

    def suggest(self, candidates: Iterable[tuple[str, VendorProfile]],
                fingerprint: str, *, prefix_tokens: int, base_input: float,
                current: str = "", when: float | None = None) -> str:
        """One-line recommendation for the run report."""
        ranked = self.rank_candidates(candidates, fingerprint,
                                      prefix_tokens=prefix_tokens,
                                      base_input=base_input, when=when)
        if not ranked:
            return ""
        top = ranked[0]
        if current and top["vendor"] != current:
            cur = next((r for r in ranked if r["vendor"] == current), None)
            if cur is not None and cur["warm"] is False and top["warm"] is True:
                return (f"建议切到 {top['vendor']}：该厂商仍有此前缀的缓存，"
                        f"比当前 {current} 的冷启动省 ¥{top['hop_cost']:.4f}")
        if any(r["warm"] for r in ranked):
            warm = next(r for r in ranked if r["warm"])
            return f"{warm['vendor']} 仍有此前缀的缓存，优先复用"
        return "候选厂商均无此前缀的缓存，本跳必然冷启动"

    # -- maintenance ------------------------------------------------------
    def prune(self, when: float | None = None,
              profiles: Iterable[VendorProfile] = ()) -> int:
        """Drop entries that can no longer be warm. Returns how many went."""
        now = when if when is not None else time.time()
        by_id = {p.id: p for p in profiles}
        removed = 0
        with self._transaction(write=True):
            for vendor, entries in list(self._seen.items()):
                ttl = _ttl_seconds(by_id.get(vendor))
                for fp, last in list(entries.items()):
                    if (now - last) > ttl or (now - last) < 0:
                        entries.pop(fp, None)
                        removed += 1
                if not entries:
                    self._seen.pop(vendor, None)
        return removed

    def to_raw(self) -> dict[str, Any]:
        with self._transaction():
            return {"vendors": {k: len(v) for k, v in self._seen.items()},
                    "entries": sum(len(v) for v in self._seen.values())}


__all__ = [
    "CacheWarmth",
    "Hop",
    "prefix_fingerprint",
]
