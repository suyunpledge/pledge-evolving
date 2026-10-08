# Forge for VS Code

在 VS Code 侧边栏直接使用 Forge 内核：**装上插件 → 填一次 API 密钥 → 开始对话**。

## 小白三步上手

1. **安装**：把 `forge-agent-x.y.z.vsix` 拖进 VS Code 窗口（或扩展面板右上角 `…` → 从 VSIX 安装）。
2. **打开面板**：点左侧活动栏的 **Forge** 图标。面板会显示三步向导：
   - **选 Python**：点「自动检测」按钮即可（扫 PATH 和常见安装位置，自动验证版本 ≥3.10 且能导入 forge）；扫不到就手动粘贴 `python.exe` 的完整路径。
   - **填 API 密钥**：下拉选厂商、粘贴密钥、点保存。密钥只写入本机 `~/.forge/secrets.json`（0600 权限、原子写入），**面板不回显、不进聊天记录、不进任何日志**。
   - **连接内核**：点「连接内核」。连接只读本地配置，不访问厂商、不扣费。
3. **对话**：在输入框描述任务。选中代码后可右键「解释/审查选中代码」，或用「+ 当前文件」把打开的文件加入上下文。

> 第一次点连接时 VS Code 会问「是否信任此工作区」——必须信任才能启动 Python 引擎。受限模式下向导会明确告诉你原因，不会静默失败。

## 常见错误与自助修复

| 看到什么 | 原因 | 怎么办 |
|---|---|---|
| 错误弹窗带「配置 API 密钥」按钮 | 密钥缺失或 401 | 点按钮回向导第 2 步重填 |
| 错误弹窗带「检查 Python」按钮 | Python 路径不对 / 装不了 forge | 点按钮回向导第 1 步重新检测 |
| `这个解释器导入不了 forge` | 该 Python 环境没装 Forge | 在设置里填 `forge.enginePath` 指向包含 `forge/vscode_bridge.py` 的源码根目录 |
| 向导不出现 | 工作区未受信任 | 点 VS Code 提示里的「信任」 |

## 功能

- 多轮对话、真实工具执行进度与 token 用量。
- 从现有 Forge 配置读取模型，支持自动路由或明确选择某个模型。
- 添加当前文件或选区上下文，支持未保存的编辑器缓冲区。
- 编辑器右键「解释选中代码」「审查选中代码」。
- 默认只读，可手动切换工作区编辑。未保存文件在执行修改前需要保存。
- 停止、执行超时、新建对话、内核重连与 VS Code 原生修改列表。
- 中文/英文界面，跟随 VS Code 语言；暗色、浅色与高对比度主题使用编辑器原生颜色。

## 设置项

| 设置 | 说明 |
|---|---|
| `forge.pythonPath` | Python 3.10+ 的 `python.exe`。向导的「自动检测」会自动填好 |
| `forge.enginePath` | Forge 源码根目录（含 `forge/vscode_bridge.py`）。若该 Python 环境已安装 Forge 包可留空 |
| `forge.homePath` | Forge 配置目录。留空使用 `~/.forge` |
| `forge.language` | 界面语言，auto 跟随 VS Code |

## 运行当前源码（开发者）

需要 VS Code 1.96+、Node.js 22+（开发时）、Python 3.10+ 和当前 Forge 源码。

```powershell
cd <仓库>\vscode-extension
npm.cmd ci
npm.cmd run compile
code.cmd .
```

在这个扩展项目中按 **F5** 启动 Extension Development Host。

## 重新打包 vsix

```powershell
npm.cmd run compile   # 生成 out/
# 然后用仓库脚本打包（无需下载 vsce）：
python <workspace>\build_vsix.py
code.cmd --install-extension <输出>\forge-agent-x.y.z.vsix
```

## 安全边界

- 密钥永远不进 webview 状态、不进 VS Code 持久化存储、不进模型上下文；面板保存密钥时值走子进程 stdin，不出现在命令行参数里。
- 工具执行受 Forge Policy 约束：默认只读；「工作区编辑」也只写当前工作区；原生执行（shell）在受保护代理中被硬拒。
- 输出经全局脱敏注册表过滤；已知密钥的任何编码变体（JSON 转义、URL 编码、base64）都会被替换为标记。
