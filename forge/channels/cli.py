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
import sys
import time
from pathlib import Path

from ..config import load_config
from . import InboundMessage, available_channels, load_channel, session_for
from .weixin import (
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
    home = Path(args.home)
    from ..cli import BUNDLE_DIR

    bundles = sorted(BUNDLE_DIR.glob("*.json"))
    return load_config(home, bundles=bundles, include_user_layer=True)


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
            print(f"looked in: {find_openclaw_accounts.__globals__.get('OPENCLAW_WEIXIN_DIR', '')}")
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
    """Long-poll the surface, run the agent per message, reply with the report."""
    from ..loop import LoopLimits, build_agent
    from ..model import ModelRouter

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

    scope = conf.get("sessionScope", "per-peer")
    max_rounds = int(getattr(args, "max_messages", 0) or 0)
    idle_stop = int(getattr(args, "idle_seconds", 0) or 0)

    # share one router across messages — building it per message would re-read
    # config and re-resolve credentials on every turn
    router = ModelRouter.from_config(cfg)
    workspace = Path(args.workspace or Path.cwd())

    print(f"weixin serve: account={channel.cfg.account_id} scope={scope}")
    print("polling for messages (Ctrl-C to stop)")
    if max_rounds:
        print(f"will stop after {max_rounds} poll(s)")
    if idle_stop:
        print(f"will stop after {idle_stop}s of no messages")

    handled = 0
    polls = 0
    started = time.monotonic()
    last_message_at = time.monotonic()

    while True:
        if max_rounds and polls >= max_rounds:
            print(f"reached poll cap ({max_rounds}); stopping.")
            break
        if idle_stop and (time.monotonic() - last_message_at) > idle_stop:
            print(f"idle for {idle_stop}s; stopping.")
            break

        try:
            messages = list(channel.poll(limit=10))
        except KeyboardInterrupt:
            print("\ninterrupted; stopping.")
            break
        except Exception as exc:
            print(f"poll error: {exc}", file=sys.stderr)
            time.sleep(2)
            polls += 1
            continue

        polls += 1
        for msg in messages:
            last_message_at = time.monotonic()
            key = session_for(msg, scope)
            print(f"[{key}] {msg.peer}: {msg.text[:80]}")
            try:
                channel.send_typing(msg.peer)
            except Exception:
                pass

            try:
                agent = build_agent(
                    home=Path(args.home),
                    workspace=workspace,
                    config=cfg,
                    router=router,
                    limits=LoopLimits(
                        max_steps=int(cfg.get("loop", "maxSteps", 12)),
                        max_depth=int(cfg.get("loop", "maxDepth", 2)),
                        spawn_budget=int(cfg.get("loop", "spawnBudget", 8)),
                    ),
                )
                report = agent.run(msg.text)
                reply = _render_report(report)
            except Exception as exc:
                reply = f"[forge] 运行失败：{exc}"

            try:
                channel.send(msg.peer, reply, group=msg.group)
            except Exception as exc:
                print(f"send failed to {msg.peer}: {exc}", file=sys.stderr)
                continue
            handled += 1

    elapsed = time.monotonic() - started
    print(f"done: {handled} message(s) handled in {elapsed:.1f}s over {polls} poll(s)")
    return 0


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
