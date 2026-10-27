# -*- coding: utf-8 -*-
"""assistant_mode — 手机端「个人助理」模式（bridge 侧）。

与 Coding/Work 桌面模式的关系：同一个 forge Agent 内核，不同任务前缀
与工具面侧重。个人助理模式 = 对话优先 + MCP 连接器工具（日程/笔记/
外部应用数据）+ 设备只读工具；不加载 Coding 的编辑器工具集。

桥路由：
- POST /assistant/connectors        增改连接器 {name, command[], env?, enabled?}
- POST /assistant/connectors/delete {name}
- POST /assistant/connectors/ensure {name?}  启动（缺省全部）
- GET  /assistant/connectors        状态快照
- POST /assistant/ask               {task, history?} —— 助理模式问答
  （内部走 /agent 同一 Agent，只是 expose 面含 MCP 工具 + 系统
  前缀注明个人助理语境；WS 会话 session=assistant-*）
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

import mcp_connectors as mc


class ConnectorBody(BaseModel):
    name: str
    command: list[str] | None = None
    env: dict[str, str] | None = None
    enabled: bool = True
    url: str | None = None      # http 型（官方远程 MCP）：与 command 二选一


class ConnectorName(BaseModel):
    name: str


class AskBody(BaseModel):
    task: str
    history: list[dict[str, Any]] | None = None


class CatalogInstallBody(BaseModel):
    id: str
    credentials: dict[str, str] = Field(default_factory=dict)


# 内置联网搜索（v0.4：搜索服务不再走连接器——Agent 直接可用）
try:
    from web_search_tools import WEB_SEARCH_TOOL
    _BUILTIN_ASSISTANT_TOOLS = [WEB_SEARCH_TOOL]
except Exception:                            # noqa: BLE001 — 缺依赖不阻塞
    _BUILTIN_ASSISTANT_TOOLS = []


ASSISTANT_SYSTEM_HINT = (
    "【个人助理模式】用户在手机上与你对话。优先使用可用的连接器工具"
    "（日历/笔记/外部应用）完成任务；设备操作只读为主。回答简短、"
    "适合手机屏幕阅读。你内置了 web_search 工具——联网搜索直接调用，"
    "不需要任何连接器。"
)


def attach_assistant_tools(agent) -> int:
    """将 ready MCP 工具和内置搜索挂入助理 Agent 的 registry。

    该适配器是公开的桥层契约：只接收拥有 ``registry`` 的 Forge Agent，
    不改变 Coding/Work 的 registry。每次问答刷新，支持运行中安装连接器。
    """
    from forge.tools import ToolResult, ToolSpec
    from forge.policy import ToolEffect
    registry = agent.registry
    attached = 0
    for item in mc.all_tools():
        name = item["qualified"]
        if any(s.name == name for s in registry.all_specs()):
            registry.unregister(name)

        def handler(args, _ctx, _item=item):
            result = mc.call(_item["server"], _item["tool"], args or {})
            if result.get("ok"):
                return ToolResult(True, content=str(result.get("text", "")),
                                  meta={"connector": _item["server"], "tool": _item["tool"]})
            return ToolResult(False, error=str(result.get("error", "connector call failed")),
                              content=str(result.get("text", "")),
                              meta={"connector": _item["server"], "tool": _item["tool"],
                                    "execution_state": "unknown" if not _item["readOnly"] else "completed"})

        read_only = bool(item.get("readOnly"))
        registry.register(ToolSpec(
            name=name,
            description=f"[{item['server']}] {item.get('description', '')}"[:700],
            handler=handler,
            read_only=read_only,
            tags=("connector", "mcp"),
            schema=item.get("inputSchema") or {"type": "object", "properties": {}},
        ), trusted=True,
            effect=ToolEffect.READ if read_only else ToolEffect.DELEGATED_WRITE)
        attached += 1

    for item in _BUILTIN_ASSISTANT_TOOLS:
        name = item.get("name")
        if not name:
            continue
        if any(s.name == name for s in registry.all_specs()):
            registry.unregister(name)

        def builtin_handler(args, _ctx, _item=item):
            result = _item["fn"](**(args or {}))
            if not isinstance(result, dict):
                result = {"result": result}
            return ToolResult(bool(result.get("ok", True)), content=json.dumps(result, ensure_ascii=False),
                              error=str(result.get("error", "")))

        registry.register(ToolSpec(name=name, description=item.get("description", ""),
                                   handler=builtin_handler, read_only=True,
                                   tags=("builtin", "network"),
                                   schema=item.get("parameters") or {}),
                          trusted=True, effect=ToolEffect.READ)
        attached += 1
    return attached


def register_routes(app) -> None:
    """把助理模式路由挂到 FastAPI app（forge_bridge 启动时调用）。"""

    @app.get("/assistant/catalog")
    def catalog_list():
        """预置连接器目录（分组视图）。

        结构：pinned（常用置顶，用户列举的核心服务）+ scenes[]（
        场景分组：办公协作/生活日常/开发运维/专业服务——UI 折叠渲染）。
        平铺 catalog 字段保留（兼容）。
        """
        from connector_catalog import CATALOG, grouped
        g = grouped()
        return {"pinned": g["pinned"], "scenes": g["scenes"],
                "regions": g.get("regions", []), "catalog": CATALOG}

    @app.post("/assistant/catalog/install")
    def catalog_install(body: "CatalogInstallBody"):
        """一键接入：目录 id + 凭据字段 → 建连接器并启动。

        url 中的 {VAR} 占位符用 env 值替换（腾讯地图这类 key-in-url 型）。
        """
        from connector_catalog import by_id
        import re as _re
        item = by_id(body.id)
        if item is None:
            return {"ok": False, "error": f"unknown catalog id: {body.id}"}
        env = {f["env"]: body.credentials.get(f["env"], "")
               for f in item["auth"]["fields"]}
        # 缺凭据检查（auth.kind=none 跳过）
        if item["auth"]["kind"] != "none":
            missing = [f["label"] for k, v in env.items() if not v
                       for f in item["auth"]["fields"] if f["env"] == k]
            if missing:
                return {"ok": False, "error": f"missing credentials: {missing}"}
        if item["transport"] == "http":
            url = item["url"]
            for k, v in env.items():
                url = url.replace("{" + k + "}", v)
            url = _re.sub(r"\{[A-Z_]+\}", "", url)   # 未填的可选占位清掉
            result = mc.upsert(body.id, env=env, url=url)
        else:
            result = mc.upsert(body.id, command=item["command"], env=env)
        if result.get("ok"):
            ensure = mc.ensure_one(body.id)
            result["connector"] = ensure
            result["name"] = item["name"]
            # upsert 成功只代表配置落盘；只有握手完成才允许 UI/Agent
            # 认为连接器可用，避免“安装成功但工具面为空”的假阳性。
            result["ok"] = bool(ensure.get("ok") and ensure.get("status") == "ready")
            if not result["ok"]:
                result["error"] = ensure.get("error") or "connector handshake failed"
        return result



    @app.post("/assistant/connectors")
    def upsert_connector(body: ConnectorBody):
        result = mc.upsert(body.name, body.command, body.env, body.enabled,
                           url=body.url)
        if result.get("ok"):
            ensure = mc.ensure_one(body.name)
            result["connector"] = ensure
        return result

    @app.post("/assistant/connectors/delete")
    def delete_connector(body: ConnectorName):
        return mc.remove(body.name)

    @app.post("/assistant/connectors/ensure")
    def ensure_connector(body: ConnectorName | None = None):
        if body and body.name:
            return mc.ensure_one(body.name)
        return mc.ensure_all()

    @app.get("/assistant/connectors")
    def list_connectors():
        return {"connectors": mc.status()}

    @app.post("/assistant/ask")
    async def assistant_ask(body: AskBody):
        """助理问答：与 /agent/run 同链路，任务前加语境提示。

        设备工具桥接：MCP 工具已并入 Agent 工具面（qualified 名），
        device.* 仍走 WS tool_call 通道（Kotlin 双通道执行）——两种
        扩展在同一轮对话里可混用。
        """
        from forge_bridge import _sessions, _build_session_agent, _agent_lock
        task = f"{ASSISTANT_SYSTEM_HINT}\n\n任务：{body.task}"
        session_id = "assistant-default"
        agent = _sessions.get(session_id)
        if agent is None:
            async with _agent_lock:
                agent = _sessions.get(session_id)
                if agent is None:
                    agent = _build_session_agent(session_id)
                    _sessions[session_id] = agent
        # 连接器可能在会话建立后才安装/重连；每轮刷新一次工具面，避免
        # 需要重启手机 Agent 才能看到新工具。
        attach_assistant_tools(agent)

        import asyncio

        done: asyncio.Future = asyncio.get_running_loop().create_future()

        def _worker():
            try:
                with agent.session:
                    report = agent.run(task, history=body.history or [])
                loop = done.get_loop()
                loop.call_soon_threadsafe(done.set_result, {
                    "answer": getattr(report, "text", "") or getattr(report, "answer", "") or "",
                    "steps": getattr(report, "steps", 0),
                    "cost": float(getattr(report, "cost", 0.0) or 0.0),
                })
            except Exception as exc:        # noqa: BLE001
                done.get_loop().call_soon_threadsafe(done.set_exception, exc)

        loop = asyncio.get_running_loop()
        # 不用 with ThreadPoolExecutor：with 退出会同步等待 worker，阻塞
        # FastAPI 事件循环，造成手机端看起来“发送后没反应”。
        loop.run_in_executor(None, _worker)
        try:
            summary = await done
            return {"ok": True, **summary}
        except Exception as exc:              # noqa: BLE001
            raise HTTPException(500, str(exc)) from exc
