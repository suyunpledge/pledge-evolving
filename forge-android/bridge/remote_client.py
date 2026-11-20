# -*- coding: utf-8 -*-
"""remote_client — 手机端「遥控电脑」通道（bridge 内置，非连接器）。

手机侧两种模式（用户定稿）：
- 本地模式（默认）：任务跑在手机 PRoot 内的 forge（现状）
- 遥控模式：任务经 WS 转发到桌面 forge 执行（computer_* 可用），
  审批请求在手机弹窗（approval.request/response）

接入：设置页/助理页配一次（电脑 IP+端口+配对码）；
桥路由：
- POST /remote/pair {host, port, code} → 配对（存 mcp_connectors 同级配置）
- GET  /remote/status → {mode: local|remote, paired, device}
- POST /remote/mode {mode} → 切换（重启生效 or 即时）
- WS   /agent/ws 不变——桥在 run 前按 mode 分流：
       local → 本地 Agent；remote → 转发桌面
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictBool

import bridge_vault

HOME = Path(os.environ.get("FORGE_HOME", "/opt/forge/.forge-home"))
CONFIG_PATH = HOME / "remote_pair.json"

_PENDING: dict[str, asyncio.Future] = {}   # 审批 request_id -> future

_state: dict[str, Any] = {"mode": "local", "paired": False,
                          "host": "", "port": 0, "device_id": "",
                          "ws": None}


def load_cfg() -> dict[str, Any]:
    cfg = bridge_vault.load(CONFIG_PATH)
    if not cfg.get("token"):
        cfg["paired"] = False
    return cfg


def save_cfg(cfg: dict[str, Any]) -> None:
    bridge_vault.save(CONFIG_PATH, cfg)


def _desktop_uri(host: str, port: int) -> str:
    if not 1 <= port <= 65535:
        raise ValueError("Invalid desktop port")
    host = host.strip()
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError("Invalid desktop hostname") from None
        return f"wss://{host}:{port}/remote"
    scheme = "ws" if address.is_loopback else "wss"
    authority = f"[{address}]" if address.version == 6 else str(address)
    return f"{scheme}://{authority}:{port}/remote"


def status() -> dict[str, Any]:
    cfg = load_cfg()
    return {"mode": _state.get("mode", cfg.get("mode", "local")),
            "paired": bool(cfg.get("paired")),
            "device": cfg.get("device_id", ""),
            "host": cfg.get("host", ""), "port": cfg.get("port", 0)}


def set_mode(mode: str) -> dict[str, Any]:
    if mode not in ("local", "remote"):
        return {"ok": False, "error": f"invalid mode {mode!r}"}
    if mode == "remote" and not load_cfg().get("paired"):
        return {"ok": False, "error": "not paired — pair first"}
    _state["mode"] = mode
    return {"ok": True, "mode": mode}


async def pair(host: str, port: int, code: str) -> dict[str, Any]:
    """连桌面 remote-host，验证配对码，落配置。"""
    import websockets
    try:
        uri = _desktop_uri(host, port)
        device_id = load_cfg().get("device_id") or f"phone-{secrets.token_hex(16)}"
        async with websockets.connect(uri, open_timeout=8) as ws:
            await ws.send(json.dumps({"type": "pair",
                                      "device_id": device_id, "code": code}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            token = resp.get("token")
            if resp.get("type") != "paired" or not isinstance(token, str) or len(token) < 32:
                return {"ok": False, "error": "配对码错误或已被重置"}
        save_cfg({"paired": True, "host": host, "port": port,
                  "device_id": device_id, "token": token, "mode": "remote",
                  "paired_at": time.time()})
        _state.update(mode="remote")
        return {"ok": True, "device": device_id, "host": host, "port": port}
    except Exception as exc:                 # noqa: BLE001
        return {"ok": False, "error": f"connect failed: {exc}"[:200]}


async def unpair() -> dict[str, Any]:
    cfg = load_cfg()
    cfg["paired"] = False
    cfg.pop("token", None)
    cfg["mode"] = "local"
    save_cfg(cfg)
    _state.update(mode="local", ws=None)
    return {"ok": True}


class DesktopLink:
    """遥控模式下的桌面连接：run 转发 + 事件回流 + 审批弹窗桥。

    forge_bridge 的 WS handler 在 run 事件里判断 mode=remote 时用本类
    替代本地 Agent 执行；observer 事件直接从桌面 WS 透传给手机 UI。
    """

    def __init__(self, outgoing_emit):
        self.emit_app = outgoing_emit      # 手机 UI 的 emit（转发桌面事件）

    async def run(self, task: str, history: list) -> None:
        import websockets
        cfg = load_cfg()
        if not cfg.get("paired"):
            await self.emit_app({"type": "run_failed",
                                 "error": "remote mode without pairing"})
            return
        try:
            uri = _desktop_uri(cfg["host"], cfg["port"])
            async with websockets.connect(uri, open_timeout=8,
                                          max_size=8 << 20) as ws:
                await ws.send(json.dumps({"type": "pair",
                                          "device_id": cfg["device_id"],
                                          "token": cfg["token"]}))
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if hello.get("type") != "paired":
                    # 桌面重置过配对码——提示重新配对
                    await self.emit_app({"type": "run_failed",
                                         "error": "配对失效，请重新配对"})
                    return
                await ws.send(json.dumps({"type": "run", "task": task,
                                          "history": history}))
                while True:
                    raw = await ws.recv()
                    ev = json.loads(raw)
                    etype = ev.get("type")
                    if etype == "approval.request":
                        # 注册 pending future（/remote/approve set_result），
                        # 答复异步回传桌面 WS
                        rid = ev.get("request_id", "")
                        loop2 = asyncio.get_running_loop()
                        fut = loop2.create_future()
                        _PENDING[rid] = fut
                        await self.emit_app(ev)
                        async def _relay(rid, fut):
                            try:
                                approved = await asyncio.wait_for(fut, timeout=120)
                            except Exception:                  # noqa: BLE001
                                approved = False
                            _PENDING.pop(rid, None)
                            try:
                                await ws.send(json.dumps({
                                    "type": "approval.response",
                                    "request_id": rid, "approved": approved}))
                            except Exception:                  # noqa: BLE001
                                pass
                        asyncio.create_task(_relay(rid, fut))
                    elif etype == "run_done":
                        await self.emit_app(ev)
                        return
                    elif etype == "run_failed":
                        await self.emit_app(ev)
                        return
                    else:
                        await self.emit_app(ev)   # agent.* 步骤流透传
        except Exception as exc:             # noqa: BLE001
            await self.emit_app({"type": "run_failed",
                                 "error": f"desktop link: {exc}"[:200]})


class PairBody(BaseModel):
    host: str
    port: int
    code: str


class ModeBody(BaseModel):
    mode: str


class ApprovalBody(BaseModel):
    request_id: str
    approved: StrictBool


def register_routes(app) -> None:
    from fastapi import HTTPException

    @app.post("/remote/pair")
    async def do_pair(body: PairBody):
        return await pair(body.host, body.port, body.code)

    @app.post("/remote/unpair")
    async def do_unpair():
        return await unpair()

    @app.get("/remote/status")
    def do_status():
        return status()

    @app.post("/remote/mode")
    def do_mode(body: ModeBody):
        return set_mode(body.mode)

    @app.post("/remote/approve")
    async def do_approve(body: ApprovalBody):
        """手机 UI 的审批答复 → DesktopLink 内部 future。"""
        fut = _PENDING.get(body.request_id)
        if fut and not fut.done():
            fut.set_result(body.approved)
            return {"ok": True}
        raise HTTPException(404, "unknown request_id")
