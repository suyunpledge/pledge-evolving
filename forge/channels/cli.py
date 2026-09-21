"""``forge channel`` — inspect, enable and run messaging channels.

Sub-commands:

  forge channel list              which channels are registered and configured
  forge channel weixin status     probe the credential without sending
  forge channel weixin import     adopt an existing OpenClaw weixin login
  forge channel weixin serve      run the inbound loop, dispatching to the agent

``serve`` is the interesting one: it long-polls the surface, maps each inbound
message to a session, runs the agent, and sends the report back. The loop is
deliberately thin — all the hard parts (cursor, dedup, chunking, session
mapping) live in the channel and the pure helpers next door, so they are
testable without a network.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from typing import Any
from pathlib import Path

from ..config import load_config
from ..loop import LoopLimits, build_agent
from ..model import ModelRouter
from . import InboundMessage, available_channels, load_channel, session_for
from .weixin import (
    OPENCLAW_WEIXIN_DIR,
    PollTimeout,
    WeixinChannel,
    find_openclaw_accounts,
    import_openclaw_account,
)

DEFAULT_CRED_DIR = Path.home() / ".forge" / "channels" / "weixin"


def _channel_rows(cfg) -> dict[str, dict]:
    """Collect every ``channel:<name>`` config row, keyed by channel name."""
    rows: dict[str, dict] = {}
    for row_id, name, conf in cfg.active():
        if row_id.startswith("channel:"):
            rows[row_id.split(":", 1)[1]] = dict(conf)
    return rows


def _compose(args):
    """Compose config with the same layer rules as the rest of the CLI."""
    from ..cli import BUNDLE_DIR

    return load_config(
        Path(args.home),
        bundles=sorted(BUNDLE_DIR.glob("*.json")),
        include_user_layer=True,
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_channel_list(args) -> int:
    cfg = _compose(args)
    rows = _channel_rows(cfg)
    registered = available_channels()

    print(f"registered channels: {', '.join(registered) or '(none)'}")
    print()
    if not rows:
        print("no channel configured (add a `channel:<name>` row to a config layer)")
        return 0

    width = max(len(k) for k in rows)
    print(f"{'channel':<{width}}  {'enabled':<8}  detail")
    print("-" * (width + 30))
    for name, conf in sorted(rows.items()):
        enabled = bool(conf.get("enabled"))
        detail = ""
        if name == "weixin":
            token_source = conf.get("tokenEnv") or conf.get("tokenFile") or (
                "inline (insecure)" if conf.get("token") else "none"
            )
            detail = f"account={conf.get('accountId', 'default')} token={token_source}"
        print(f"{name:<{width}}  {str(enabled):<8}  {detail}")
    return 0


# ---------------------------------------------------------------------------
# weixin status
# ---------------------------------------------------------------------------

def cmd_channel_weixin_status(args) -> int:
    cfg = _compose(args)
    rows = _channel_rows(cfg)
    conf = rows.get("weixin", {})

    # allow --token-file to override so status works before config is written
    if getattr(args, "token_file", ""):
        conf = {**conf, "tokenFile": args.token_file}
    if not conf:
        print("weixin channel is not configured.")
        print()
        accounts = find_openclaw_accounts()
        if accounts:
            print("found existing OpenClaw weixin credentials:")
            for p in accounts:
                print(f"  {p}")
            print()
            print("adopt one with:")
            print(f'  python run.py channel weixin import --source "{accounts[0]}"')
        else:
            print("no existing credential found; a QR login will be required.")
        return 1

    try:
        channel = WeixinChannel(conf)
    except Exception as exc:
        print(f"weixin channel could not start: {exc}")
        return 1

    info = channel.status()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info.get("reachable") else 1


# ---------------------------------------------------------------------------
# weixin import
# ---------------------------------------------------------------------------

def cmd_channel_weixin_import(args) -> int:
    source = getattr(args, "source", "")
    dest = getattr(args, "dest", "") or str(DEFAULT_CRED_DIR / "default.json")

    if not source:
        candidates = find_openclaw_accounts()
        if not candidates:
            print("no OpenClaw weixin credentials found to import.")
            print(f"looked in: {OPENCLAW_WEIXIN_DIR}")
            return 1
        if len(candidates) == 1:
            source = str(candidates[0])
            print(f"using the only candidate: {source}")
        else:
            print("multiple credentials found; pass --source:")
            for p in candidates:
                print(f"  --source \"{p}\"")
            return 1

    try:
        result = import_openclaw_account(Path(source), Path(dest))
    except Exception as exc:
        print(f"import failed: {exc}")
        return 1

    print(f"imported to {result['dest']}")
    print(f"  account : {result['account']}")
    print(f"  baseUrl : {result['baseUrl']}")
    print(f"  userId  : {result['userId']}")
    print()
    print("next: add a config row so the channel can load it —")
    print(json.dumps(
        {
            "id": "channel:weixin",
            "name": "channel:weixin",
            "config": {
                "enabled": True,
                "accountId": result["account"] or "default",
                "tokenFile": result["dest"],
                "sessionScope": "per-peer",
            },
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


# ---------------------------------------------------------------------------
# weixin serve
# ---------------------------------------------------------------------------

def cmd_channel_weixin_serve(args) -> int:
    """Long-poll the surface, run the agent per message, reply with the report.

    The agent's shared machinery (policy, tools, memory, capabilities) is built
    once; only the per-lane session differs between messages. Rebuilding it per
    turn was measurably wasteful and made the loop look heavier than it is.
    """
    cfg = _compose(args)
    rows = _channel_rows(cfg)
    conf = rows.get("weixin", {})
    if not conf.get("enabled"):
        print("weixin channel is not enabled; set enabled=true in the channel row.")
        return 1

    try:
        channel = WeixinChannel(conf)
    except Exception as exc:
        print(f"weixin channel could not start: {exc}")
        return 1

    # Two servers on one account race for the cursor and double-reply. Refuse
    # rather than corrupt state.
    lock = _acquire_lock(channel.state_dir / f"{channel.cfg.account_id}.serve.lock")
    if lock is None:
        print(
            f"another serve process holds the lock for account "
            f"{channel.cfg.account_id}; stop it first (or delete the lock file "
            f"if it is stale)."
        )
        return 1

    scope = conf.get("sessionScope", "per-peer")
    max_polls = int(getattr(args, "max_messages", 0) or 0)
    idle_stop = int(getattr(args, "idle_seconds", 0) or 0)
    home = Path(args.home)
    workspace = Path(args.workspace or Path.cwd())
    session_dir = home / "sessions" / "channels"

    router = ModelRouter.from_config(cfg)
    limits = LoopLimits(
        max_steps=int(cfg.get("loop", "maxSteps", 12)),
        max_depth=int(cfg.get("loop", "maxDepth", 2)),
        spawn_budget=int(cfg.get("loop", "spawnBudget", 8)),
    )

    print(f"weixin serve: account={channel.cfg.account_id} scope={scope}")
    print(f"  workspace : {workspace}")
    print(f"  sessions  : {session_dir}")
    print("polling for messages (Ctrl-C to stop)")
    if max_polls:
        print(f"  stop after {max_polls} poll(s)")
    if idle_stop:
        print(f"  stop after {idle_stop}s idle")

    stats = _ServeStats()
    agents = _LaneCache(max_size=getattr(args, "lane_cache", 128))

    try:
        while True:
            if max_polls and stats.polls >= max_polls:
                print(f"reached poll cap ({max_polls}); stopping.")
                break
            if idle_stop and stats.idle_seconds() > idle_stop:
                print(f"idle for {idle_stop}s; stopping.")
                break

            try:
                messages = channel.poll(limit=10)
            except KeyboardInterrupt:
                print("\ninterrupted; stopping.")
                break
            except PollTimeout:
                # normal: the window expired with nothing new
                stats.polls += 1
                continue
            except Exception as exc:
                print(f"poll error: {exc}", file=sys.stderr)
                stats.polls += 1
                stats.errors += 1
                time.sleep(min(30, 2 * stats.consecutive_errors + 1))
                continue

            stats.consecutive_errors = 0
            stats.polls += 1
            stats.polls_with_messages += 1 if messages else 0

            # heartbeat: lets another process tell we are alive (see D)
            _touch_lock(lock)

            for msg in messages:
                stats.touch()
                key = session_for(msg, scope)
                print(f"[{key}] {msg.peer}: {msg.text[:80]}")
                channel.send_typing(msg.peer)  # best-effort, never raises

                # one agent per lane, reused across messages in that lane
                agent = agents.get(key)
                if agent is None:
                    agent = _build_lane_agent(
                        home=home,
                        workspace=workspace,
                        session_dir=session_dir,
                        session_key=key,
                        config=cfg,
                        router=router,
                        limits=limits,
                    )
                    agents.put(key, agent)

                try:
                    report = agent.run(msg.text)
                    reply = _render_report(report)
                except Exception as exc:
                    reply = f"[forge] 运行失败：{exc}"
                    stats.errors += 1

                try:
                    chunks = channel.send(msg.peer, reply, group=msg.group)
                    stats.sent += chunks
                    stats.handled += 1
                except Exception as exc:
                    print(f"send failed to {msg.peer}: {exc}", file=sys.stderr)
                    stats.send_failures += 1
    finally:
        _release_lock(lock)

    print(stats.summary())
    return 0 if stats.errors == 0 else 1


class _LaneCache:
    """Bounded LRU of per-lane agents.

    Each agent holds a session, a policy, a tool registry and (lazily) a memory
    snapshot, so an unbounded dict grows with the number of distinct peers a
    long-running bot has ever seen. 128 lanes covers a busy group-chat bot with
    room to spare; the least recently served lane is dropped and rebuilt on
    demand — cheap relative to holding every lane forever.

    Eviction is safe because a lane's durable state is its session *file* on
    disk, not the in-memory agent. A rebuilt agent reopens the same log.
    """

    def __init__(self, max_size: int = 128) -> None:
        self._max = max(1, int(max_size))
        self._items: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str):
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, key: str, value) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)

    def keys(self):
        return list(self._items.keys())


class _ServeStats:
    """Loop bookkeeping, kept out of the loop body."""

    def __init__(self) -> None:
        self.polls = 0
        self.polls_with_messages = 0
        self.handled = 0
        self.sent = 0
        self.send_failures = 0
        self.errors = 0
        self.consecutive_errors = 0
        self._started = time.monotonic()
        self._last_message = time.monotonic()

    def touch(self) -> None:
        self._last_message = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_message

    def summary(self) -> str:
        elapsed = time.monotonic() - self._started
        return (
            f"done: {self.handled} message(s) handled, {self.sent} chunk(s) sent, "
            f"{self.errors} error(s), {self.send_failures} send failure(s) "
            f"over {self.polls} poll(s) in {elapsed:.1f}s"
        )


def lane_session_path(session_dir: Path, session_key: str) -> Path:
    """Map a session key to its log file.

    A key looks like ``channel:weixin:<account>:<peer>``. Colons are legal in a
    key but not in a Windows filename, and a key can be arbitrarily long, so it
    is sanitised and capped. The result is deterministic, which is what matters:
    the same conversation always lands in the same file.

    Extracted from ``_build_lane_agent`` so it can be tested directly rather
    than through a copy of the same arithmetic.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_key)
    return Path(session_dir) / f"{safe[:120]}.jsonl"


def _build_lane_agent(*, home, workspace, session_dir, session_key, config, router, limits):
    """Build one agent bound to a per-lane session file.

    The session *key* separates conversations logically; giving each key its own
    log file makes that separation real (and keeps a busy group chat from
    burying a DM in one shared file).
    """
    path = lane_session_path(session_dir, session_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    return build_agent(
        home=home,
        workspace=workspace,
        config=config,
        router=router,
        limits=limits,
        session_path=path,
    )


# A live holder refreshes its lock every poll iteration; a poll window is at
# most 60s, so anything older than this belongs to a process that is gone (or
# to a PID that has since been reused).
LOCK_STALE_SECONDS = 180


def _acquire_lock(path: Path, *, stale_after: float = LOCK_STALE_SECONDS):
    """Take the serve lock, or return None if a live holder has it.

    Liveness is "PID responds AND the lock is being refreshed". That pair fixes
    the three ways a PID-only check gets it wrong:

    * PID reused after a crash -- the new process with that PID is alive, but it
      is not refreshing *this* lock, so the lock is stale and we take over.
    * Another user's PID -- `os.kill(pid, 0)` raises PermissionError, so the PID
      looks alive forever; again the stale check releases it.
    * No way to probe PIDs at all (no tasklist, sandboxed) -- we fall back to
      staleness alone instead of assuming alive.

    Refusing to start is the safe default when a holder really is running, since
    two servers on one account race for the cursor and double-reply.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        pid, mtime = _read_lock(path)
        age = time.time() - mtime if mtime else float("inf")
        if pid and age < stale_after and _pid_alive(pid):
            return None  # a holder that is alive and still heartbeating
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def _read_lock(path: Path) -> tuple[int, float]:
    """Return ``(pid, mtime)`` from a lock file; ``(0, 0.0)`` if unreadable."""
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or 0)
    except (ValueError, OSError):
        pid = 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return pid, mtime


def _touch_lock(path: Path | None) -> None:
    """Refresh the lock's heartbeat so peers can tell we are still running."""
    if path is None:
        return
    try:
        os.utime(path, None)
    except OSError:
        pass


def _release_lock(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check that works on Windows and POSIX."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in out.stdout
        except Exception:
            return True  # can't tell -> assume alive, safer than double-serving
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _render_report(report) -> str:
    """Turn a RunReport into the text that goes back over the wire.

    The surface is a chat, so the answer text is what matters. The stop reason
    is appended only when the run did not finish normally -- that is the one
    piece of metadata a user needs when something went wrong, and noise when
    everything worked.
    """
    answer = (getattr(report, "text", "") or "").strip()
    stopped = str(getattr(report, "stopped", "") or "")
    if not answer:
        answer = "(forge produced no text)"
    # "final" is the normal ending; anything else is worth surfacing
    if stopped and stopped != "final":
        steps = len(getattr(report, "steps", []) or [])
        answer = f"{answer}\n\n[stopped: {stopped}, steps: {steps}]"
    return answer


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def cmd_channel(args) -> int:
    """Dispatch ``forge channel <target> <action>``.

    ``forge channel`` and ``forge channel list`` both resolve to the list view.
    """
    target = (getattr(args, "target", "") or "list").strip()
    action = (getattr(args, "action", "") or "status").strip()

    if target in ("", "list"):
        return cmd_channel_list(args)

    if target == "weixin":
        if action == "status":
            return cmd_channel_weixin_status(args)
        if action == "import":
            return cmd_channel_weixin_import(args)
        if action == "serve":
            return cmd_channel_weixin_serve(args)

    print(f"unknown channel command: {target} {action}".strip())
    print("usage: forge channel [list | <name> [status|import|serve]]")
    return 2
