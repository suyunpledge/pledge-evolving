# 模型接入与路由适配审查

后续请求执行、逐阶段回执、跨进程缓存与真实厂商探测结果，见 [请求执行审查](request-execution-review.md)。本文件保留上一轮审查时的边界和数据。

日期：2026-10-07。范围：本地新增 `vendors/adapters/cache_state/context_plan`、Provider/ModelRouter/SmartRouter、setup、GUI 配置导入；另完成此前消息黑屏修复。保留现有插件系统与界面风格，没有打包或生成 EXE。

## 已修复的问题

| 场景 | 审查时的行为 / 风险 | 修复 | 自动化测试 |
| --- | --- | --- | --- |
| GUI 导入平铺 Provider JSON | vendor、cacheControl、adapt 等停留在 row 顶层，后端读取不到 | 归入 config，保留服务、限速、温度、headers 和模型列表 | `test_provider_adaptation` |
| GUI 导入平铺路由 JSON | model 行被误当 Provider，routing 被默认值替代 | 识别 model 行并保留完整 routing | `test_provider_adaptation` |
| 向导保存 API Key | keys 参数没有参与配置生成，进程退出后环境表达式取不到密钥 | 保存明确收集的 key；调用方未提供 key 时仍支持环境表达式 | `test_wizard_keys_survive_a_process_restart_and_medium_is_first` |
| 向导生成 medium 策略 | tiers 第一项是 lite，SmartRouter 实际从 lite 首发 | medium 作为主对话档；lite 用于 small，premium 用于集成 | 同上 |
| 单端点调用不同厂商模型 | profile 始终根据 default_model，可能套错计费和协议规则 | 根据本次实际 model 识别；CLI 上下文预算跟随策略实际首发模型 | 实际模型 / 策略画像测试 |
| cacheControl=off / explicit | 字段没有接入规划；off 仍可能注入，explicit 可能失效 | 明确接入模式；显式指令不依赖模型价格表是否完整 | 开关和单次调用测试 |
| Anthropic 画像 + OpenAI wire | 可能把 Anthropic cache_control 塞进 OpenAI 工具 schema | 显式缓存字段同时受厂商与 wire 约束 | `test_anthropic_markers_never_leak_to_openai_wire` |
| 请求复用 / 并发 | 原地给调用方的 tools/messages 注入字段，可能污染下一次调用 | 注入前复制 payload，原始工具和历史保持不变 | 请求不变性测试 |
| OpenAI 格式系统提示 | 系统提示既没进入缓存指纹，也没进入前缀门槛计算 | 同时提取 system/developer 消息和顶层 system，避免重复计数 | 指纹与 token 估算测试 |
| 多模态前缀 | 指纹只取 text，不同非文本内容可能撞到相同指纹 | 保留结构化内容与工具字段顺序 | 结构化前缀测试 |
| 缓存成本估算 | 消息尾部被按缓存折扣计费；warm 首次调用被当免费；1h 写入仍按 5m 价格 | 只给稳定前缀折扣，尾部全价；每次读均计价；按 TTL、间隔计算重写 | 尾部成本 / warm / 1h TTL 测试 |
| 同厂商不同账户 / 模型 / 端点 | 成功响应就假定整个厂商缓存已热 | 指纹按端点、wire、模型、凭据摘要与缓存模式隔离；仅有缓存 usage 证据才记录 | 命名空间与实际 usage 测试 |
| 缓存状态损坏或并发保存 | 非数字时间戳可能让模型请求失败；直接覆写文件可能出现部分内容 | 容错加载，拒绝未来时间戳；线程锁与临时文件原子替换 | 损坏记录 / 并发读写保存测试 |
| usage 为负数、Infinity、NaN | int 转换可能抛异常，或产生负账目 | 使用经过校验的归一结果，不再回读畸形原字段 | 完整 HttpTransport mock 测试 |
| setup / CLI 上下文策略 | 顶层 mode 未读取；字符预算按 token 阈值裁剪 | 支持实际配置行，正确换算字符单位，尊重 disabled | context 配置、预算和 disabled 测试 |
| SmartRouter / premium | 请求 plan 被丢弃，合并 usage 丢失缓存和 reasoning 数量 | 保留当前请求 plan，汇总两阶段 usage 新字段 | SmartRouter plan / premium usage 测试 |
| 百炼稳定前缀 | 标记最后一条变化的用户消息，稳定系统提示没有标记 | 优先标记稳定 system/developer 消息；无系统消息保留兼容回退 | 百炼标记位置测试 |
| 多工具 Anthropic 请求 | 超过 32 个工具时直接跳过标记 | 工具数量不限制单个尾部断点 | 60 工具测试 |
| 旧单 Provider 配置接口 | PROVIDERS 与 _build_patch 消失，原回归导入失败 | 保留兼容入口，包括原有本地与 custom 数据项 | `tests/test_setup_patch.py` |
| 发送后聊天区变黑 | 内部内容缩到约 798px，scrollregion 仍为 2690px，自动滚到底部显示空白 | 合并监听消息内部布局变化，同步实时 bbox；手动滚动与销毁清理保持正常 | `test_message_delivery`，真实历史隔离重放与现场显示 |

## 验证

- `python -m unittest forge.test_model_adaptation`：30 项通过。
- `python -m forge.test_adapters`：48/48；`python -m forge.test_vendors`：73/73。
- `python -m forge.selftest`：569/569（本地核心检查）。
- `python -m unittest discover -s tests`：12 项通过，另执行该目录脚本中的顶层断言。
- `python tests/test_setup_patch.py`：旧配置接口全部断言通过。
- `forge-gui/run_offline_checks.py test_message_delivery test_chat_widgets test_market_responsiveness.ImeEventTests test_ui_polish`：37 项通过。
- GUI Provider 导入、原配置真值表和客户端测试：11 项通过。
- 消息收缩与窗口变化覆盖 100%、125%、150%、175%、200% Tk 字体/DPI 缩放；没有修改 Windows 的显示设置。实际 Emoji 消息的五档缩放测试也通过。
- 已用源码启动 Forge，打开原有会话，确认回复保持可见。没有向真实模型发送测试对话。

## 保证边界

- 缓存计划是估算与观测记录，不能证明上游一定命中，更不能决定插件权限。所有折扣与收益按当前本地画像计算；完整厂商价格目录和全部模型能力未做在线实调用验证。
- `cacheControl=off` 阻止 Forge 主动注入标记；厂商自身的隐式缓存不能通过这个本地开关保证关闭。
- `Completion.plan` 是当前请求的计划；premium 合并后的 usage 包含两个阶段，plan 本身不是两个阶段的完整费用账单。
- `defer/reasoning_budget/cliff_action` 是规划提示，未新增调度器，也未把这些提示描述成已执行的限流、思考限额或动态压缩。
- CacheWarmth 的线程锁只协调同一个进程内的实例操作，原子替换防止部分文件；多进程不会因此获得完整事务协调。这些记录只作建议，不作为安全或权限依据。
- GUI 的直接 Gateway 转发路径与 HttpTransport 路由路径不同。本次保留该结构，没有声称所有 GUI 流式请求都已经使用新的成本规划器。
- 新向导的交互目录仍以云厂商为主；兼容的 `_build_patch` 可以配置原有本地/custom Provider，GUI 原有自定义 Provider 功能保留。
- 原 EXE 没有重新打包，源码修复入口为桌面 `Forge（源码修复版）.lnk`。

## 协议依据

- [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)：稳定前缀层级、5m/1h TTL 与不同写入倍率。
- [阿里云百炼 context cache](https://www.alibabacloud.com/help/en/model-studio/context-cache)：消息内容块标记、稳定位置、5 分钟 TTL，以及工具定义不独立打标记。
