# AutoClaw 交互源码只读审查

审查日期：2026-10-10。范围仅限本机可公开读取的已安装渲染器资源；未运行 AutoClaw，未读取凭据、聊天历史或用户存储。

## 已检查的安装物

- 安装目录：`C:\Users\匡溯昀\AppData\Local\Programs\AutoClaw2`
- 产品版本：`AutoClaw2.exe` 为 `2.0.4.201`；应用包 `resources\app\package.json` 声明 `zwork` `2.0.4`。
- 渲染器 bundle：`resources\app\dist\renderer\assets\session-history-CYQxBQ9D.js`，7,206,573 bytes，SHA-256 `20E79AE9A8ED9A13D734CB4E80F27F29D859E0D6DB26EE250A5908CD84BD1A9A`。
- 该 bundle 经压缩为一行，以下均为 **line 1 byte offset**，便于复核；只列短标识符，不复制闭源实现。

## 交互证据

| 主题 | bundle 位置 | 可验证的短标识符/行为 |
| --- | --- | --- | --- |
| 输出跟随与滚动 | offset `6,188,876` | `followOutput`、`atBottom`、`shouldFollow`、`scrollToIndex(... index:"LAST")` |
| 尺寸变化时的滚动 | offset `6,179,585` | `notAtBottomBecause==="SIZE_INCREASED"` 触发跳底的分支 |
| 发送与草稿恢复 | offsets `687,856`–`693,000` | `zwork-client-outbox`、`send-history`、`writeDraftSnapshot`、`safetyStatus:"checking"` |
| 失败记录处理 | 附近 `blocked` / `error` 分支（未固定偏移） | `blocked` / `error`、`sessionPersisted`、`removeMessage` |

## 局限

- `followOutput` 位于打包的虚拟列表依赖中；它证明该安装包包含这种交互能力，**不证明** AutoClaw 当前聊天页面实际已启用该选项，也不证明其具体产品策略。
- bundle 为压缩产物，offset 比源代码行号稳定性低，会随版本更新改变。
- 本报告不对 AutoClaw 的运行时状态、服务端行为或未检查的用户数据作推断。
