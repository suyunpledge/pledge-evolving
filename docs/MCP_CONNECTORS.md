# MCP 连接器目录与嵌入约定

手机端 `GET /assistant/catalog` 现在提供稳定的 `regions` 视图：

```
中国区 / 美国区
  └─ 分类
       └─ items（按 popularity 降序）
```

`pinned` 和 `scenes` 仍保留，供旧版客户端兼容。`popularity` 是 Forge
维护的相对常用程度，不是实时安装量；`verification` 标记为 `official`
或 `community`，方便 UI 在接入前提示可信度。当前目录已补齐 Exa、Microsoft
Learn 两个美国区免 Key 入口，并将 Linear、Figma、Stripe、百度地图改为官方
Streamable HTTP 地址。连接器的 MCP 工具只有在握手成功后才注册进
`assistant-default` Agent，Coding/Work Agent 不会被污染。

## 认证与官方端点复核

- [Linear MCP](https://linear.app/docs/mcp)：主端点 `https://mcp.linear.app/mcp`，旧 SSE 端点不再作为默认值。
- [Figma remote MCP](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/)：`https://mcp.figma.com/mcp`，OAuth。
- [Notion MCP](https://developers.notion.com/guides/mcp/get-started-with-mcp)：`https://mcp.notion.com/mcp`，OAuth。
- [Stripe MCP](https://docs.stripe.com/mcp)：`https://mcp.stripe.com`，OAuth 或 Agent API key。
- [百度地图 MCP](https://lbs.baidu.com/docs/ai?title=mcpserver%2Fquickstart)：`https://mcp.map.baidu.com/mcp?ak=...`。
- [Exa MCP](https://exa.ai/mcp)：`https://mcp.exa.ai/mcp`，免 Key 入口有速率限制。
- [Microsoft Learn MCP](https://learn.microsoft.com/en-us/training/support/mcp-developer-reference)：`https://learn.microsoft.com/api/mcp`，公开免认证的 Streamable HTTP。
- [Lark MCP 配置](https://github.com/larksuite/lark-openapi-mcp/blob/main/docs/usage/configuration/configuration.md)：stdio 命令必须包含 `mcp -a APP_ID -s APP_SECRET`。

## 嵌入状态语义

`catalog/install` 的 `ok=true` 只在配置写入并完成 initialize/tools-list
握手时返回。仅写入配置、进程启动失败、认证失败或远端返回非 2xx 都会返回
`ok=false`，同时保留 `connector.status` 和 `connector.error`。连接器工具
调用通过 Forge `ToolRegistry`，使用 `<server>__<tool>` 名称，继续经过宿主
Policy；远端 MCP 自述的 `readOnly` 不会单独获得宿主权限。

