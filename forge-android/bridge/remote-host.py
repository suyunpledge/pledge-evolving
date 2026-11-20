# -*- coding: utf-8 -*-
"""remote_host — 桌面端「被遥控」服务：手机 forge 的远程会话入口。

默认监听 127.0.0.1，可经 adb reverse 使用。局域网模式使用
`--lan --certfile CERT --keyfile KEY`，手机通过 WSS 核验桌面证书。
首次配对使用一次性短码，重连使用配对时签发的随机凭证。
重置配对会撤销连接，审批只接收发起请求的那条连接的答复。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import ipaddress
import json
import re
import secrets
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any

# forge 可导入：env FORGE_ROOT 优先，然后脚本向上逐级 + /opt/forge
import os as _os
import sys as _sys
from pathlib import Path as _P

_cands = []
_env_root = _os.environ.get("FORGE_ROOT")
if _env_root:
    _cands.append(_P(_env_root))
_d = _P(__file__).resolve()
for _ in range(4):                      # 向上 4 级（bridge/ → forge-android/ → repo）
    _cands.append(_d)
    _d = _d.parent
_cands.append(_P("/opt/forge"))
for _root in _cands:
    if (_root / "forge" / "loop.py").exists():
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        break

from forge.loop import Agent, LoopLimits, build_agent
from forge.config import load_config
from forge.model import ModelRouter


class RemoteHost:
    """被遥控端状态：配对码 + 已配对设备 + 会话 Agent 表。"""

    def __init__(self, home: Path, workspace: Path, forge_root: Path):
        self.home = home
        self.workspace = workspace
        self.forge_root = forge_root
        self.pair_code: str = ""
        self.paired: dict[str, str] = {}               # device_id -> session credential
        self.sessions: dict[str, Agent] = {}
        self.running: set[str] = set()
        self._auth_lock = threading.RLock()
        self._expires = 0.0
        self._attempts = 0

    def new_pair_code(self) -> str:
        with self._auth_lock:
            self.pair_code = f"{secrets.randbelow(1000000):06d}"
            self._expires = time.monotonic() + 300
            self._attempts = 0
            return self.pair_code

    def reset_auth(self):
        with self._auth_lock:
            self.paired.clear()
            self.new_pair_code()

    def authenticated(self, device_id: str, token: str) -> bool:
        with self._auth_lock:
            expected = self.paired.get(device_id)
            return bool(expected and isinstance(token, str) and len(token) == len(expected)
                        and secrets.compare_digest(expected.encode(), token.encode()))

    def pair(self, device_id: str, code: str) -> str:
        with self._auth_lock:
            if (not self.pair_code or self._attempts >= 8
                    or time.monotonic() >= self._expires or len(self.paired) >= 64):
                return ""
            self._attempts += 1
            if (not isinstance(code, str) or len(code) != 6
                    or not secrets.compare_digest(self.pair_code.encode(), code.encode())):
                return ""
            self.pair_code = ""
            token = secrets.token_urlsafe(32)
            self.paired[device_id] = token
            return token

    def agent_for(self, device_id: str) -> Agent:
        agent = self.sessions.get(device_id)
        if agent is None:
            bundles = sorted((self.forge_root / "bundles").glob("*.json"))
            cfg = load_config(self.home, bundles=bundles)
            router = ModelRouter.from_config(cfg)
            agent = build_agent(
                home=self.home, workspace=self.workspace, config=cfg,
                router=router,
                limits=LoopLimits(max_steps=12, max_depth=2, spawn_budget=8),
                session_path=self.home / "sessions" / f"remote-{device_id}.jsonl",
                mount_contrib=True,
            )
            self.sessions[device_id] = agent
        return agent


async def serve(host: str, port: int, home: Path, workspace: Path,
                forge_root: Path, *, tls=None) -> None:
    """remote-host 主循环（run.py remote-host 入口调用）。"""
    import websockets

    rh = RemoteHost(home, workspace, forge_root)
    # 测试态：FORGE_REMOTE_TEST_CODE 固定配对码（冒烟用；生产随机）
    loopback = ipaddress.ip_address(host).is_loopback
    if not loopback and tls is None:
        raise ValueError("LAN remote control requires TLS")
    rh.new_pair_code()
    if loopback and _os.environ.get("FORGE_REMOTE_TEST_CODE"):
        rh.pair_code = _os.environ["FORGE_REMOTE_TEST_CODE"]
    scheme = "wss" if tls else "ws"
    print(f"[remote-host] listening on {scheme}://{host}:{port}/remote", flush=True)
    if loopback or sys.stdout.isatty():
        print(f"[remote-host] pair code: {rh.pair_code}", flush=True)
    # 配对码弹窗（共享 rh；headless 静默跳过）
    import threading as _threading
    _threading.Thread(target=_show_code_dialog, args=(rh,), daemon=True).start()

    async def handler(ws):
        device_id = None
        pending = {}
        active = None
        stop = threading.Event()
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            dev = first.get("device_id", "")
            if (first.get("type") != "pair" or not isinstance(dev, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", dev)):
                await ws.send(json.dumps({"type": "pair_failed"}))
                return
            token = first.get("token", "")
            issued = rh.pair(dev, first.get("code", "")) if not token else ""
            token = issued or token
            if not rh.authenticated(dev, token):
                await ws.send(json.dumps({"type": "pair_failed"}))
                return
            device_id = dev
            await ws.send(json.dumps({"type": "paired", **({"token": issued} if issued else {})}))

            loop = asyncio.get_running_loop()

            async def emit(ev: dict[str, Any]):
                await ws.send(json.dumps(ev, ensure_ascii=False))

            await emit({"type": "ready", "device": device_id,
                        "tools": ["computer_observe", "computer_act",
                                  "run", "approval.response"]})

            while rh.authenticated(device_id, token):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                except TimeoutError:
                    continue
                if not rh.authenticated(device_id, token):
                    return
                msg = json.loads(raw)
                kind = msg.get("type")

                if kind == "run":
                    task = str(msg.get("task", "")).strip()
                    if not task:
                        await emit({"type": "error", "error": "empty task"})
                        continue
                    if device_id in rh.running:
                        await emit({"type": "error",
                                    "error": "a task is already running"})
                        continue
                    rh.running.add(device_id)

                    async def execute(task, history):
                        try:
                            agent = rh.agent_for(device_id)
                            await run_task(agent, task, history, emit, pending, stop, loop)
                        except websockets.ConnectionClosed:
                            pass
                        except Exception:
                            await emit({"type": "run_failed", "error": "Remote task failed"})
                        finally:
                            rh.running.discard(device_id)

                    active = asyncio.create_task(execute(task, msg.get("history") or []))

                elif kind == "approval.response":
                    fut = pending.pop(msg.get("request_id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(msg.get("approved") is True)

                elif kind == "ping":
                    await emit({"type": "pong"})

                else:
                    await emit({"type": "error", "error": f"unknown {kind!r}"})
        except (websockets.ConnectionClosed, TimeoutError):
            pass
        except Exception as exc:              # noqa: BLE001 — handler 全捕获排障
            import traceback
            print(f"[remote-host] handler error: {exc}", flush=True)
            traceback.print_exc()
        finally:
            stop.set()
            for fut in pending.values():
                if not fut.done():
                    fut.set_result(False)
            if active is not None:
                await asyncio.gather(active, return_exceptions=True)

    async with websockets.serve(handler, host, port, max_size=8 << 20, ssl=tls, origins=[None]):
        await asyncio.Future()   # run forever


async def run_task(agent: Agent, task: str, history: list, emit,
                   pending: dict, stop: threading.Event, loop) -> None:
    """跑一轮远程任务；审批走手机（approval.request/response 往返）。"""
    done = loop.create_future()

    def observer(event: dict[str, Any]) -> None:
        data = dict(event)
        etype = data.pop("type", "step")
        asyncio.run_coroutine_threadsafe(
            emit({"type": f"agent.{etype}", **data}), loop)

    # 桌面 computer_act 审批桥：桌面 approve 回调被 ComputerSession 调用
    # （同步）——转发到手机 WS 等允许/拒绝。v0.5：若桌面 GUI 审批已配置
    # 则手机端也可见（双通道记录）；此处用手机作为远程审批人。
    def approve_via_phone(target_id: str, args: dict) -> bool:
        request_id = f"ap-{secrets.token_hex(16)}"
        fut = Future()

        async def request():
            if stop.is_set():
                return
            pending[request_id] = fut
            await emit({"type": "approval.request", "request_id": request_id,
                        "target": target_id, "action": args})

        try:
            asyncio.run_coroutine_threadsafe(request(), loop).result(timeout=5)
            return not stop.is_set() and fut.result(timeout=120)
        except Exception:                       # noqa: BLE001 — 超时/断线=拒绝
            return False
        finally:
            loop.call_soon_threadsafe(pending.pop, request_id, None)

    def _worker():
        comp = getattr(agent, "_computer_session", None)
        old_approve = getattr(comp, "approve", None)
        old_cancel = getattr(agent, "cancellation_event", None)
        try:
            if comp is not None:
                comp.approve = approve_via_phone
            agent.cancellation_event = stop
            with agent.session:
                report = agent.run(task, history=history)
            summary = {
                "answer": report.text, "steps": len(report.steps),
                "cost": float(report.usage.get("cost") or 0.0),
            }
            loop.call_soon_threadsafe(done.set_result, summary)
        except Exception as exc:                # noqa: BLE001
            loop.call_soon_threadsafe(done.set_exception, exc)
        finally:
            if comp is not None:
                comp.approve = old_approve
            agent.cancellation_event = old_cancel

    loop.run_in_executor(None, _worker)
    try:
        summary = await done
        await emit({"type": "run_done", **summary})
    except Exception as exc:                    # noqa: BLE001
        await emit({"type": "run_failed", "error": str(exc)})


def _show_code_dialog(rh: "RemoteHost") -> None:
    """桌面配对码弹窗：大号显示当前码；重置=换码并踢出全部已配对设备。

    headless / 无显示环境静默跳过（控制台仍有配对码输出）。
    """
    try:
        import tkinter as tk

        root = tk.Tk()
        root.title("forge 遥控配对")
        root.geometry("360x280")
        root.attributes("-topmost", True)

        tk.Label(root, text="手机「🖥️ 遥控 → 匹配电脑」输入此配对码",
                 font=("Microsoft YaHei UI", 10)).pack(pady=(18, 2))
        code_var = tk.StringVar(value=rh.pair_code)
        tk.Label(root, textvariable=code_var, fg="#1a6fc4",
                 font=("Consolas", 44, "bold")).pack(pady=6)
        state_var = tk.StringVar(value=f"已配对设备：{len(rh.paired)} 台")

        def refresh_state(*_):
            code_var.set(rh.pair_code or "已使用")
            state_var.set(f"已配对设备：{len(rh.paired)} 台")
            root.after(1000, refresh_state)

        def reset():
            rh.reset_auth()
            code_var.set(rh.pair_code)
            state_var.set("已重置，旧设备需重新配对")

        tk.Button(root, text="生成新配对码", command=rh.new_pair_code,
                  relief="flat").pack(pady=3)
        tk.Button(root, text="重置配对码（踢出全部设备）", command=reset,
                  relief="flat", fg="#c0392b").pack(pady=8)
        tk.Label(root, textvariable=state_var, fg="#777").pack()
        root.protocol("WM_DELETE_WINDOW", root.iconify)   # 关窗=藏，服务不停
        root.after(1000, refresh_state)
        root.mainloop()
    except Exception:                       # noqa: BLE001 — headless 容错
        pass


# ── CLI 接线（run.py remote-host）──────────────────────────────
def cli_main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(prog="remote-host",
                                 description="桌面被遥控服务（手机 forge 遥控入口）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="监听 0.0.0.0（手机直连；默认 127.0.0.1 需 adb reverse）")
    ap.add_argument("--certfile", type=Path)
    ap.add_argument("--keyfile", type=Path)
    ap.add_argument("--home", default=None)
    ap.add_argument("--workspace", default=".")
    args = ap.parse_args(argv)
    tls = None
    if args.lan:
        if not args.certfile or not args.keyfile:
            ap.error("--lan requires --certfile and --keyfile for trusted TLS")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.certfile, args.keyfile)

    forge_root = Path(__file__).resolve().parent
    home = Path(args.home) if args.home else forge_root / ".forge-home"
    workspace = Path(args.workspace).resolve()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    if args.lan:
        print("[remote-host] LAN 模式：请确认防火墙放行该端口")
    try:
        asyncio.run(serve(host, args.port, home, workspace, forge_root, tls=tls))
    except KeyboardInterrupt:
        print("\n[remote-host] bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
