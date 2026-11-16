# MiMo：缓存命中与整体 token 消耗

本轮从上一轮已修复的长任务调用链继续优化。优先去除重复数据、避免压缩丢失操作线索和错误任务交接；没有缩短用户正文、截断原生工具结果、删除原生推理块，或切换用户的推理设置来制造收益。

## 代码改动

- **分层规则按新增条目追加。** 过去每发现一个目录，便把根规则及已发现目录规则一起重新投影，父级内容重复计入后续每个请求。RulesSnapshot 提供新增条目；projector 只在已发送前缀后追加它们，原父级消息保持不变。没有根规则、首次发现子目录规则的情况也走追加。
- **变更/撤销完整清理。** 旧实现记录了 superseded 字符串，却没有在后续替换时清掉更早的快照。Agent 现在跟踪所有宿主投影片段；任一已有来源变更或移除时清理这些片段，再呈现当前完整快照，并失效旧缓存观测。项目规则仍是受 Policy 限制的仓库数据，不能授权工具或提升权限。
- **压缩和续接携带片段状态。** 各个有效规则片段都被 pin，可信会话续接保存并恢复该清单。规划请求排除已呈现片段，只把完整当前规则附上一次，避免“历史规则 + 当前规则”重复。
- **任务正文只发送一次。** 请求中原本已包含完整任务，TaskContract 又复制前 500 字作为 objective。Agent 现在省略邻接投影中的重复 objective；独立 render_projection、render_anchor、UI 事件和宿主目标状态仍保留目标。
- **摘要优先保留观测。** 过去三组工具调用的长 ID/参数常占满 800 字符，真正结果（尤其末尾的游标）被挤掉。新摘录优先收录最近两份工具结果的少量首尾，再放短调用线索；OpenAI 和 Anthropic 都支持。摘录仍明确为 unverified，完整源先归档，工具配对和原生重放不被改写，完成状态仍由环境探针验收。
- **高级路由接收明确宿主任务。** Premium 最终整合阶段原来取“最后一条 user 消息”，可能拿到新增规则或工具反馈。内置 SmartRouter 现在接收宿主明确传入的最新任务；该参数只供路由使用，不透传 HTTP。整合请求仍只带任务与草稿，不额外转发规则、完整对话或工具历史。

- **明确工具步骤与最终交付。** 修复过程中 Pro 出现提前结束整轮请求的情况，造成验收失败后额外模型往返。最终系统提示明确单次工具响应只是一个步骤，全部工作和验证完成后才能给最终答复，并遵循用户要求的格式，替换原来容易与 JSON 要求冲突的 plain prose 表述。

- **预算暂停也保留可信原生历史。** max_steps 退出过去会清空整个请求快照。现在只在完整、已知结果的交换结束后保存预算检查点；仍返回未完成状态，不把停止提示伪装成模型答案或任务成功。客户端历史必须精确匹配，权限/规则/路由 guard 继续验收，未知结果、取消和协议错误不保存。最终完成与预算暂停共用一个快照提交入口。

## 实测设计

继续使用 scripts/verify_mimo_long_context.py，本轮新增 --baseline-dir、--scoped-rules、--retries，以及输入/输出总量和恢复错误的独立统计。A 为本轮开始前的 loop/compaction 源码备份，B 为修改后实现；两组使用相同账户、模型、推理设置、工具声明、原始用户任务、固定数据和隔离工作区。

第一轮处理 3 批，第二轮恢复并处理 2 批。每批必须先 read_file 读取 batch-N/request.json，再使用该目录规则的 policy_tag 和真实游标调用 process_batch。每批返回 48 条合成库存记录，原始结果 4,638 字符；实际 checkpoint 最终必须有 5 批、963 units、正确顺序的全部头记录。宿主环境探针和独立文件断言同时验收。

两组首个完整请求 JSON 不再相同：省略重复 objective 正是待测改动。早期报告独立核对相同工具和相同前四条消息；最终协议修正对照中系统提示有意改变，改为核对相同工具及前三类固定数据（记忆、根规则、运行信封），且断言原始任务仍完整位于请求中；B 还断言每个实际 HTTP 请求的四条固定消息未改变。没有把请求字数减少当作厂商命中率；比例只用真实 usage 中已观测的 cached_tokens / input，缺失回执单列。

本地软预算为 16,000 字符。Flash 路由验收门槛为 16,000 token，用于频繁触发压缩；Pro 为 65,536 token，检查低压力延期和续接。输出额度同为 4,096 token。这是宿主实验约束，不是模型的标称最大窗口。所有付费请求只发送隔离夹具，没有读取业务文件或改写用户 provider 配置。

成功任务由真实完成状态和最终结果决定，clean_execution 另记零工具错误及严格读取次数。已恢复的错误仍计入 token、请求数和错误计数；不会过滤掉这些额外消费。JSON 外面若有完整 json 围栏会单独标注，内容仍须完全匹配预期。

## Flash：规则/摘要优化阶段的完整 A/B

原始报告：reports/mimo-token-flash-progress-ab.json。两组均完成两轮，所有动作成功，无工具错误，固定头部和工具声明相同。该记录产生于最后的系统结束协议修正之前，未冒称该提示词修正也在此记录中验证。

| 策略 | 输入 token（含已缓存） | 输出 token（含推理） | 总 token | 已观测命中率 | 压缩 | 成功模型请求 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A 修改前 | 60,788 | 1,414 | 62,202 | 61.62% | 6 | 17 |
| B 修改后 | 60,394 | 913 | 61,307 | 71.95% | 4 | 16 |

总 token 下降约 1.44%，输入下降约 0.65%，输出下降约 35.43%。已观测缓存为 A 36,544 / 59,303，B 43,456 / 60,394；A 有一次缺失缓存回执，B 没有。A/B 的观测分母不完全对称，不将 10.33 个百分点当成普遍收益。B 少一次 checkpoint 读取和一次模型往返；随机推理长度和恢复行为也影响总体消费，不能全部归因于缓存。

## 预算暂停续接：最终代码的 Flash A/B

原始报告：reports/mimo-token-pause-flash-ab.json；脚本 scripts/verify_mimo_budget_resume.py。两组先以 1 步预算真实读取 values.json，明确以 max_steps 未完成退出，再增加后续预算并要求继续完成原任务。文件中的数值没有写进用户提示，必须来自实际工具观测；最终必须调用 calculator 并得到 42。

| 策略 | 实际文件读取 | 成功模型请求 | 输入 token | 输出 token | 总 token | 可信快照续接 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| A 修改前 | 2 | 4 | 5,644 | 158 | 5,802 | 否 |
| B 最终代码 | 1 | 3 | 4,400 | 159 | 4,559 | 是 |

两组最终均正确，B 总 token 下降 **21.42%**、输入下降 22.04%。B 首个续接请求保留了真实 native 调用、结果和推理字段，不把预算停止文案加入模型历史；两种协议的配对和字节前缀延续还有独立离线回归。

首个续接请求的已观测命中率为 A 1,024 / 1,356（75.52%）、B 1,280 / 1,495（85.62%）。**不能据此宣称整段命中率提高**：整段续接 A 为 87.73%、B 为 83.50%；包含冷启动时 A 又有一次缺失回执。主要可验证收益是避免一次重新读文件和模型往返。HTTP 尝试数两组均为 5，分别包含 1 / 2 次 TLS 失败，失败消费未知。

## Pro 的回退和未完成证据

本轮没有获得稳定的 Pro 双指标收益，不能只展示有利请求。

- reports/mimo-token-pro-final-retry-ab.json 是一次两组都完成两轮的有效对照，但 B 额外读取了规则、checkpoint，并发生一次提前交付后的验收修复。A/B 总 token 为 83,805 / 93,996，**增加 12.16%**；命中率为 73.72% / 70.05%。这份结果保留为真实回退，没有当作成功优化。
- reports/mimo-token-pro-final-ab.json 中 B 完成两轮且无工具错误，命中率 82.37%；A 连续 TLS 失败，无法据此做有效配对比较。
- reports/mimo-token-pro-progress-ab.json 中 B 选错 policy_tag 后，被测试工具混淆的错误文案引导去改正确的 cursor，最终超出步骤预算。测试工具后来拆分 cursor / scope / quota 错误，并给两组统一提供初始零状态 checkpoint；不是把这项夹具修正冒充生产优化。
- reports/mimo-token-pro-protocol-ab.json 在明确完成协议后，A 遇到步骤上限，B 触及 4,096 输出上限。没有为了通过测试再提高生成额度。
- reports/mimo-token-pro-bounded-ab.json 将复杂目录工作负载的测试步数统一调整为 20（非生产默认值），A 完成两轮，B 首轮真实文件及环境探针通过，但答案是“说明 + JSON 围栏”，未满足严格输出要求，因此没有继续第二轮，也没有把它混入完整任务的性能表。
- reports/mimo-token-flash-abba.json 和 reports/mimo-token-pro-warm-ab.json 是早期阶段记录，包含恢复成功但被当时“零工具错误”口径标为失败的样本；保留原始标记。后续任务完成、严格格式与无错误执行口径分开记录，恢复动作的 token 不会被删除。

因此当前确认的是 Flash 场景中的重复上下文和暂停续接收益，以及具体调用链缺陷修复。Pro 的随机推理、规则再读取和输出格式仍有明显波动，未证实总体 token 或缓存稳定改善。最终结束协议没有足够的 Pro 完整配对证据，不能承诺其能独立解决这些模型行为。

## 验证和复跑

核心回归 472 项（2 跳过），客户端/运行链 208 项（1 跳过），合计 **677 项通过、3 项跳过、0 失败**；框架自检 **595/595**。源码预算 151 个文件通过，Windows CRLF 规则下 Git 空白检查通过。

测试覆盖：新增目录及无根规则场景的 append-only 原生前缀、变更/删除清除所有旧片段、压缩与续接后的完整有效规则、规划阶段只携带一份完整规则、单次任务正文与独立完整 anchor、两种协议的摘要结果尾部保留、Premium 宿主任务及参数不透传，以及预算暂停不冒充完成、完整原生交换续接、旧客户端历史拒绝和错误/取消清理。

```powershell
python -m unittest forge.test_token_efficiency forge.test_conversation_prefix forge.test_task_planning -q
python -m unittest discover -s forge -t . -q
python -m forge.cli selftest
python scripts/budget_check.py
git -c core.whitespace=cr-at-eol diff --check

# 真实 API，使用已有 MiMo 配置；每个命令的 HTTP 尝试都有上限。
python scripts/verify_mimo_budget_resume.py --live --model mimo-v2.6-flash --request-limit 16 --output reports/pause-repeat.json
# 目录工作负载需 --baseline-dir 指向本轮备份（见 reports/mimo-token-backup.txt）。
python scripts/verify_mimo_long_context.py --live --model mimo-v2.6-flash --scoped-rules --baseline-dir <backup> --strategies baseline improved --request-limit 40 --output reports/scoped-repeat.json
```

Flash 长任务记录在最后的协议提示和暂停检查点改动之前产生；最终代码的预算暂停场景另有真实 API 结果，路由及多种边界由离线实际调用链覆盖。没有把所有阶段的指标混成一次“最终版本完整对照”。

本轮所有诊断和复测合计 267 次 HTTP 尝试、256 次成功响应和 11 次 TLS/URLError 失败；成功回执输入 1,071,386、输出 48,881 token。失败尝试的服务端消费未知，未当作已核对账单。导出的压缩源档案已经逐份校验配对及摘要，临时夹具清理后仍可复核。

日志：reports/mimo-token-core-final.log、reports/mimo-token-runtime-final.log、reports/mimo-token-selftest-final.log。本轮独立补丁 reports/mimo-token.patch，文件清单 reports/mimo-token-files.json，备份位置 reports/mimo-token-backup.txt。它们只包含本轮改动，不包含此前工作区其他修改。
