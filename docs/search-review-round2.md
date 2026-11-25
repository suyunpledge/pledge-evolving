# 联网搜索复测（2026-11-25）

在 Windows 本机调用与 Work 共用的 `web_backends.search`，每题取前三条，关闭 Bocha 和 Brave 密钥。判定标准是至少一条结果直接讨论查询中的具体 API 或操作。表中是一次前后对照，搜索源和排序会随时间变化。

| 查询 | 请求源 | 修复前 | 修复后实际来源与结果 |
| --- | --- | --- | --- |
| Python pathlib Path resolve documentation | auto | Python 首页、下载和在线 IDE | Bing；Pathlib Resolve 专题与 resolve/absolute 对比 |
| SQLite WAL checkpoint documentation | Google | 无结果 | Bing；SQLite 官方 Checkpoint a database |
| Qt QML ListView 官方文档 | auto | 中文搜索源无结果 | DuckDuckGo；Qt 官方 ListView QML Type |
| Git worktree add 文档 | auto | 中文搜索源无结果 | DuckDuckGo；讨论从已有远程分支添加 worktree |
| TypeScript tsconfig strict 官方文档 | Google | 无结果 | DuckDuckGo；TypeScript 官方 strict 配置页 |

同一次复测中，MCP 官网首页原始提取只有导航文字和来源链接。修复后读取该页声明的同源 Markdown 版本，获得约 3,795 字符正文。人工核对的是主题相关性，五题中三题的前三条含官方文档。
