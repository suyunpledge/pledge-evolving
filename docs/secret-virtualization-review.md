# Secret Virtualization 安全实现与验收

审查日期：2026-10-08。仅源码、测试与说明；没有生成 EXE 或打包副本。

## 实现与安全边界

`forge/secrets.py` 提供独立 `SecretStore` 抽象和进程内 `MemorySecretStore`。
真实映射不落 Session、项目文件或工具响应。每个 session/operation 使用独立随机 scope；
引用为 `{{SECRET_REF:128-bit-random-id}}`，同一 scope 内稳定，ID 不使用 Secret 的哈希、前后缀或长度。
Facade 没有公开的 resolve/list/export。关闭 scope 会撤销映射。
跨进程 Gateway 使用客户端随机 session header 隔离引用；GUI 新建/切换对话时旋转 scope。
普通 Router 调用默认创建新的 operation scope，也允许可信宿主显式传入 session scope。

检测结合配置解析、字段名、厂商上下文、已知 Key 格式、Bearer、Cookie/credentials 容器、
URL 用户信息密码以及完整/未闭合 Private Key 块。JSON/YAML/TOML/.env 的整个文件先检测，
再截行、截长度或做 grep。带类型的数字/布尔敏感字段恢复时保持类型。
检测无法识别所有无字段标识、无已知格式的任意字符串；未识别的密钥应由可信宿主预注册。
不能把字段检测描述为对未知 Secret 的数学上完备分类器。

### 三个不变量的执行点

1. **SecretValue 不进入模型 context/request JSON**：Agent/子 Agent、Router（含自定义 transport）、
   SmartRouter、HttpTransport、GUI Client、附件、Gateway 所有 JSON 转发路径、Batch 输入均经过保护。
2. **模型持有 Ref 但不能 resolve**：Policy 对 resolve/export 在 BYPASS 和 `allow=['*']` 下仍硬拒绝；
   没有通用读取 Store 或解析 Ref 的工具。路径、绝对路径、符号链接、硬链接别名均检查。
3. **最小可信执行组件解析**：经过审批的配置 patch 在可信提交组件内恢复值；Vendor Adapter
   在构造最终 HTTP 鉴权头时恢复值。Endpoint/TLS 绑定，禁用自动重定向与 HTTP wire debug。

第三条包含用户明确要求的配置原子保存：该操作必须把原 Secret 写回原配置字段，
否则无法满足“API Key 不变并正常保存”。它不会把恢复后的配置返回给模型。
**HTTP Authorization/x-api-key 本身必须携带真实凭据；“请求不存在 SecretValue”指模型输入 JSON/context，
不包括发往用户已授权厂商的鉴权头。** 捕获报告不保存鉴权头。

### 这不是任意 Python 的 OS 沙箱

Python private 属性、子进程超时、插件 ack 均不能阻止同一 OS 用户的原生代码读取文件/环境变量，
也不能保护宿主进程内存。因此 protected Agent 禁止 shell/notebook/native 工具和原生插件 import/register/call，
原生 federation subprocess 也默认拒绝；受控声明式插件继续通过 Forge Registry → Policy → 工具桥执行。
Agent 不能改写 Forge 执行源码、受保护控制目录或通过配置 patch 提升 Policy/模块/插件授权。
旧 worker 仅保留可信宿主明确传入 `secret_isolation=False` 的接口，生命周期回归在此模式运行；
这个 opt-out **不具有 Secret 隔离保证**，GUI 没有替模型开启此开关。

不承诺防御同一账户的外部恶意进程、调试器、OS 内存转储或用户主动运行的本机 Python。
后续启用不可信原生插件需要真正的 OS 隔离、受限文件系统/网络及独立凭据 broker。
输出脱敏是补充防线，不是任意恶意代码的沙箱，也不保证覆盖所有自定义编码方式。

## edit_config 使用

检查：`edit_config({"path":"models.json"})`。
返回受保护配置，以及不含原文摘要的随机 `meta.revision`。

提交（引用保持原位置，模型只需给普通字段的 patch）：

```json
{
  "path": "models.json",
  "revision": "<上次检查返回的 revision>",
  "patch": [
    {"op": "replace", "path": "/model", "value": "another-model"},
    {"op": "replace", "path": "/cache/ttl", "value": 600}
  ]
}
```

支持 add/replace/remove/test，JSON Pointer 路径，最多 100 操作，文件最大 1 MiB。
校验引用所有权、位置和 protected 字段，保留空值与环境变量引用；拒绝伪造、互换、搬到普通字段和原文重写。
请求地址、厂商、代理、TLS 验证、认证 headers 等变更也绑定 Secret 使用授权，防止保留 Key 却改到攻击者端点。
跨线程及协作 Forge 进程使用锁、revision 和原文 CAS；验证、序列化重读、fsync、同目录原子 replace 后才返回成功。
外部编辑器不遵守 Forge 锁时仍有极短的最终比较/replace 竞争窗口，重新检查可发现之后的变更。

默认禁止新增/替换/删除 Secret。可信的人类审批控制器可调用
`authorize_config_secret` 或 `authorize_config_destination`，绑定 scope、文件、revision、字段和精确引用/目的地。
通配符 grant 不生效，提交后一次性消耗授权。`VendorCredential.from_reference` 接受已拥有的 Ref 和精确
`secret.use` grant，创建绑定的 Adapter 凭据；工厂不会读取真实值，最后 `_header` 才消费它。
这些接口都不在 LLM 工具 schema 中。第一版没有另加可由模型自行点击的通用 Secret 审批工具。

| Capability | LLM 默认 | 可信宿主授权 |
|---|---|---|
| secret.reference | ALLOW，可显式 deny/ask | 不返回真实值 |
| secret.use/create/replace/delete | ASK，headless 未批准即拒绝 | 精确能力 + 文件/版本/字段绑定 grant |
| secret.resolve/export | 始终 DENY | 不提供 agent 工具；内部执行组件使用私有 grant |

READ_ONLY 文件访问和 Secret 权限独立；能读文件不等于能读 Secret。

格式限制：JSON 重排缩进；YAML/TOML 保存可能重排格式、丢失注释；.env 未修改的行和注释保留。
YAML anchors/aliases/custom tags 与重复配置键拒绝，避免共享节点写入歧义。
YAML 需要 PyYAML；Python 3.10 的 TOML 需要 tomli，可安装项目 `secrets` optional extra；缺依赖时拒绝编辑而非放行原文。

## 第二道出口防线

共享 Redactor 覆盖工具结果/错误、logging（含 traceback）、Agent events、Session/历史/异常尾部备份、
记忆、子任务消息、Gateway 响应与日志、GUI 消息/状态/历史、插件输出/审计、cost ledger、Batch receipts。
已登记的原值和 JSON/URL/base64 形式替换为 `[REDACTED_SECRET]`。
SSE 内容、thinking、tool arguments 按逻辑 channel 缓冲可能的敏感前缀，跨帧/末帧也不先输出半个 Secret。
JSON/JSONL 响应采用结构化脱敏，避免标点 Secret 破坏协议分隔符。
有体积、深度、Store 容量和检测耗时回归，超出限制 fail closed。
凭据极短或与普通协议标量相同可能导致保守误脱敏；不能以 JSON 必然含有字符 `{` 为理由宣称泄露了密码 `{`。

GUI 原有 Key 编辑器保留，展示固定完整遮罩 + `Protected`，不显示前后缀或原值长度。
附件和配置检查只给非阻塞保护数量提示。Forge 的公开 rollout `session_id` 保持原身份，
只对该宿主字段跳过凭据字段分类，其内容仍经过已知值脱敏；嵌套 Session Token 不豁免。

兼容 GUI 密钥文件仍为原来的用户目录 `~/.forge/secrets.json`，没有迁入项目或 Session，
其路径和原子保存临时文件被登记禁止 Agent 读取。它是既有的本地明文存储，
POSIX 0600 **不等于 Windows 加密或系统凭据保险箱**。本次新增的引用映射仅在内存。
没有自动清除旧历史文件或把已有项目明文密钥迁移到 OS Keychain；旧历史读取/重新保存时脱敏。

## 攻击/故障矩阵

“此前行为”为本次修复前相关路径，“修复后”为受保护模式。

| 攻击/故障场景 | 此前行为 | 风险 | 修复后 | 自动化测试 |
|---|---|---|---|---|
| T1 普通 model/cache 修改 | 完整读取/重写配置 | Key 进入 context | 结构化 patch，可信恢复，值不变 | T1_patch_preserves_secrets_and_updates_model_cache |
| T2 多 Key/session | 原值可见 | 串值/跨会话关联 | 128-bit 随机稳定 scope Ref；默认 operation 隔离 | T2_stable_refs、router_default_operations |
| T3 Prompt Injection 要求 resolve/export | 依赖工具可读范围 | 模型获取凭据 | 无 resolver 工具，Policy 硬拒绝，即使 BYPASS | T3_resolve_export_hard_denied_even_bypass |
| T4 日志/debug/trace/历史 | 部分出口未过滤 | 持久泄露 | 共享脱敏及异常链抑制，禁 wire debug | T4_exception_session、logging、http_boundary |
| T5 父/子 Agent 消息 | 可携带文件原文 | 扩散 | 输入/报告/events/子任务统一保护 | T5_parent_child_real_agent_boundary、nested_messages |
| T6 原生插件/工具 | 同一用户 native 权限 | 绕过 Store/Policy | protected 默认 import 前拒绝；声明式受控执行 | T6_no_native_execution、desktop native_plugin_default |
| T7 Ref 伪造/互换/删除/原文重写 | 无结构化 Ref 校验 | Secret 损坏或写出 | 验证所有权/位置/字段；精确一次性 grant | T7_forgery_swap_deletion、host_approved_reference_copy |
| 把 Secret 藏在假的 Ref ID 中 | 过宽 placeholder 豁免 | 绕过已知值脱敏 | 只豁免严格随机 ID 语法，ID 等于已知凭据仍脱敏 | fake_reference_syntax_cannot_hide_a_known_secret |
| T8 Store 绝对/软/硬链接路径 | 通用读取可达 | 绕过目录边界 | canonical path + 文件身份 + 读取前拒绝 | T8_store_path、symbolic_link_and_revocation |
| T9 HTTP 崩溃/鉴权错误/重定向 | exception/headers 可能泄露 | Debug 泄露或转发 Key | 脱敏后异常，无原异常链，无 credential redirect | T9_http_error_and_trace、vendor_endpoint_binding |
| T10 真正厂商请求 | 未证明 payload 隔离 | Key 进入厂商模型输入 | 发送前捕获并断言，Key 只用于 auth | T10_captured_payload；verify_secrets 实际 DS/MiMo |
| 地址/代理/TLS 变更保留 Key | patch 可换 baseURL | 凭据送给攻击者 | 目的地变化绑定人类 secret.use 审批 | credential_destination_cannot_be_changed、bound_host_approval |
| 并发配置修改 | 无跨进程事务约束 | 覆盖他人变更 | 线程/进程锁、revision、CAS，单赢家 | concurrent_edits、cooperating_processes |
| 数字 Secret/转义/临时文件 | 文本检测可能漏类型/字段 | 原值进入输入或破坏 JSON | 语义类型保护、JSON 边界、厂商字段文本识别 | numeric_secret、punctuation_secret、vendor_fields_in_plain_attachment |
| 巨大插件字符串 | 正则存在二次扫描风险 | UI/worker 卡死 | 有界字段/URL 扫描、长度/容量限制 | detection_has_a_bounded_cost_for_oversized_plugin_text |
| 改 Policy/宿主模块提升权限 | 配置可变更控制字段 | 绕过整个执行边界 | 配置控制字段与宿主代码写入拒绝 | config_cannot_escalate、cannot_plant_host_modules |

## 验证记录

最终离线结果：核心 **569/569**；安全合同/请求执行/模型适配 unittest **94/94**；
GUI 安全/交互/Client/子任务/适配检查 **48/48**；原插件 adversarial/runtime/capability 回归 **94/94**。
脚本式适配断言 57/57、厂商断言 77/77、读范围断言 31/31、新模块断言也通过。
完整 tests discovery 的 12 个 unittest 通过，另有脚本式断言打印通过结果。
并行运行及机器长时间暂停期间曾出现 legacy worker 时间门限失败；单独重跑保持原门限全部通过，未放宽断言。
回归还发现 Windows CacheWarmth 构造器未加锁读取会阻止另一进程的原子 replace：
构造读取已复用原事务锁，原“3 进程共 90 条”用例先失败，再连续三次通过；没有改缓存规划算法。

新增安全合同位于 `forge/test_secret_virtualization.py` 与 `forge-gui/test_secret_boundary.py`。
关键缺陷先新增失败用例，再修复（原文进入模型、Ref 篡改、目的地变更、数字类型、跨 operation Ref、控制字段提升）。
进程/线程未处理异常、Tk crash callback、logging extras/stack_info 也有出口过滤和攻击测试，
不向 crash callback 传递含原值的 exception/traceback 对象。
真实 paid T10：DeepSeek 成功 51 tokens，MiMo 成功 67 tokens，共 118 tokens。
捕获完整受保护请求 JSON，发送前检查原 Key/已登记变体不存在；不保存 Authorization。
用户原先给每个目标模型的上限是 5,000,000 tokens，此探针每模型设置 20,000 reserve 上限，只发送小请求。
千问账户既有欠费阻塞未重试；没有声称通过千问真实联网验证。
探针显式 `--execute` 才联网，不在 unittest discovery 中付费。
本机捕获文件在系统 Temp 下 `forge-secret-t10-review.json`，不作为项目 Secret 文件提交。

可复验命令（离线，合成 Secret；GUI runner 将真实用户状态替换为临时目录）：

```powershell
python -m forge.selftest
python -m unittest forge.test_secret_virtualization forge.test_execution_review forge.test_model_adaptation forge.test_read_scope
python -m unittest discover -s tests
python forge-gui/run_offline_checks.py test_secret_boundary test_interactions
python forge-gui/run_offline_checks.py test_plugin_adversarial test_plugin_runtime test_plugin_capabilities
```

部署需要重启 Forge/Gateway，使已有进程改用新的请求边界。没有打包、发布或改 EXE。
