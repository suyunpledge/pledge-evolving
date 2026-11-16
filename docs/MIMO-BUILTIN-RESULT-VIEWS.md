# 内置工具结果视图：调用链、MiMo 对照与验收

## 实际接入

`build_builtin_registry()` 现在默认给 **list_dir、find_files、grep、file_outline** 声明结果视图；宿主可用 `result_views=False` 关闭。自定义 ToolSpec 的默认行为仍不变。文件正文、read_range、shell_exec、review_changes、网络正文没有套用这一字段策略。

四个工具的 handler 继续返回原来的纯文本，供直接 registry.invoke 的调用方使用；它们额外提供有限的结构化展示数据。共享 ActionExecutor 先完成原始文件修订记录，再将完整、经过 secret scope 保护的原始返回保存到 ArtifactStore，向模型返回 summary、preview、notes 和 `_retained` 引用。内部 `_result_view_source` 在执行边界移除，不在动作回执中重复保存；原文也不是由预览重建。Agent、Gateway 和动作查询使用相同的结果。

预览策略：

- 原始返回不足 1400 字符时保持纯文本，不增加留存与 JSON 开销。
- 大结果选择前四、后四条完整行，中间明确标注省略；预览最多 800 字符，放不下的整行跳过。summary 分开记录 returned_rows、preview_rows 和工具原有的计数/截断标志。
- 路径、行号和符号仍来自实际工具输出；截断、无法解析等 notes 不从预览中丢弃。原有最多 100 个 grep 匹配、200 个文件/条目/符号的上限仍有效，计数不是整个仓库的总量。
- `_retained.characters` 给出保护后原文的字符长度，模型可据此选择读取范围。artifact_read 和 artifact_grep 的 schema 现在明确页大小上下界，并声明 grep 是字面子串搜索。
- 视图没有更短、超出 inline_limit、解析/存储失败、工具失败或执行状态未知时，继续使用既有回退路径。当前 registry 必须暴露 READ 效果且直接 ALLOW 的 artifact_read 才启用字段视图，不会改变工具授权或打开隐藏工具。

## 长任务和多轮链路修复

会话 guard 现在包含注册时冻结的 result_fields：即使工具 schema 和 handler 名称相同，宿主更改视图策略也会使旧会话快照失效。正常使用相同策略的新 Agent 则继续复用已发送的请求历史。

压缩原先只摘录最后两份工具输出，会丢失较早的留存引用。现在另保留最近四个结构化视图/原有留存结果的引用，显式标记为 unverified，重复压缩时继续携带。它们只是查找线索，不是事实或授权；不会把普通用户文本里的哈希或工具输出中的裸哈希升级为引用。四个引用之外的旧输出仍保存在 ArtifactStore，但摘要不承诺记住无限数量的历史引用。

MiMo 实测还暴露了 `grep(path=...)` 的调用习惯：先前未知 path 被忽略，扩大了实际扫描范围；后来单文件路径又被拒绝。现在 grep/find_files 在 root 缺省时接受 path 位置别名，显式 root 优先；grep 支持单个文件，沿用同一套公开路径、读取权限、文件大小、secret 保护及匹配逻辑，没有增加另一条读取实现。find_files 继续以目录为搜索根。

## 对照设置

脚本 `scripts/verify_mimo_builtin_views.py` 使用真实 MiMo API 和真实内置工具，没有替换工具 handler。每个样本三轮：alpha 目录审计、beta 目录审计、取回 beta 的中间 AUDIT_CONTROL 记录。每轮重建 Agent，用相同 home 和普通聊天历史恢复宿主磁盘快照，覆盖桌面端常见路径。

夹具每目录 71 个清单文件和一个 Python 模块，模块有 91 个符号、40 条审计记录。第一、二轮必须实际调用四种发现工具，再读取 release_marker 返回值、写 JSON、read_file 复核；第三轮写入记录 025 的审计代码。每轮都有独立 json_value 环境探针验收，另检查源文件完全未改变。优化组还要求在 artifact 工具的实际返回中观察到预览未包含的记录 025，不能只凭最终答案判成功。

两臂运行同一版本的 Agent、ContextBudget、工具 handler/schema、路由、输出上限和源文件，只有 result_views 开关不同。每组都校验首个完整 HTTP payload 摘要相同。已有六组历史问答是隔离的合成审计记录，用于触发初始压缩；它们不是当前证据。context_chars=9000、本地 contextWindow=16000、maxOutputTokens=4096、工具 inline_limit 为生产默认 4000；没有宣称测试了厂商最大窗口。

正式 A/B 的第二轮在两种模型、两种策略中都成功复用磁盘快照，实际首个请求逐字保留上一轮最后一个已发送请求。个别第三轮在复用快照后立即触发必要压缩，不能把 cache_prefix_reused 事件直接当作完整前缀仍未变化的证据。所有实际请求和导出归档都验证工具调用配对。

## 实测结果

第一次 Flash pilot 只保留头部行：缓存命中率从 70.10% 升到 80.02%，但总 token 从 70266 **增加到 77071（+9.68%）**。额外定位末尾符号和多次取回抵消了结果变短的收益，因此调整为首尾预览、加入原文长度和分页参数声明。原始报告保留，未以命中率替代总消耗验收。

调整后的 A/B：

| 模型 / 策略 | 输入 token | 输出 token | 总 token | 已知缓存 token / 已知输入 | 命中率 | 压缩次数 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| Pro baseline | 56756 | 1595 | 58351 | 28928 / 56756 | 50.97% | 8 |
| Pro improved | 50627 | 2638 | 53265 | 38784 / 50627 | 76.61% | 6 |
| Flash baseline | 56639 | 1361 | 58000 | 34176 / 51068 | 66.92% | 7 |
| Flash improved | 55995 | 1071 | 57066 | 42752 / 51746 | 82.62% | 3 |

Pro 总 token 降低 **8.72%**，Flash 降低 **1.61%**。Pro 两臂无工具错误；Flash baseline 有一次 malformed read_file 参数被正常拒绝后纠正，improved 有一次单文件 grep 被拒绝后纠正。没有自动猜补损坏参数。Flash 两臂各有一份缓存回执未知，涉及输入分别 5571、4249 token，计入总消耗，但从已知命中率分母排除，没有视为零命中。

上述 Pro/Flash A/B 完成后才补齐单文件 grep，随后用完整三轮 Flash **improved → baseline** 反向顺序对照验证最终实现。它与前一组不是同一版本的 ABBA，不能合并伪称严格 ABBA。反向对照数据在报告 `mimo-builtin-flash-reverse-ba.json` 中单独保留。

| 最终 Flash 反向对照 | 输入 token | 输出 token | 总 token | 已知缓存 token / 已知输入 | 命中率 | 压缩次数 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| baseline（后运行） | 58791 | 1722 | 60513 | 36416 / 58791 | 61.94% | 9 |
| improved（先运行） | 52231 | 1173 | 53404 | 35264 / 52231 | 67.52% | 4 |

最终这一组总 token 降低 **11.75%**，两臂均无工具错误、没有未知缓存回执，首个 HTTP payload 相同。优化组第三轮实际调用 artifact_grep 查到 025，再以 `grep(path="beta/module.py", ..., context=2)` 复核，完成写入与读取验收。15 次成功模型请求、20 次工具动作，对照组为 17 次请求、24 次动作。优化组第二、三轮请求均逐字续接上轮最后已发送请求；baseline 第二轮在复用磁盘快照后先压缩，实际 HTTP 前缀因此发生变化。

这些是小样本实际消耗，含必要取回、模型额外验证和推理输出；工具调用数量没有强行对齐。命中率、总消耗和策略顺序都存在波动，不推广为任意任务的固定收益，也没有把 token 报表当作真实发票或速度证明。

证据：

- [首轮回退记录](../reports/mimo-builtin-flash-ab.json)
- [Pro A/B](../reports/mimo-builtin-pro-final-ab.json)
- [Flash A/B](../reports/mimo-builtin-flash-final-ab.json)
- [最终 Flash 反向对照](../reports/mimo-builtin-flash-reverse-ba.json)
- [证据复核汇总](../reports/mimo-builtin-evidence.json)

连同 pilot，本轮共 **140 次 HTTP 尝试、140 次成功 usage 回执**；回执输入 **475009**、输出 **12927** token，没有传输错误。缓存详情未知不等于主 usage 未知，也不等于未计费。这些是本轮实际 API 消耗，未做发票核验。

复核了 25 份不同导出路径的原始返回，全部匹配内容哈希和字符长度；其中正式三组的 27 次留存动作还与当前真实 handler 在相同夹具/调用参数下的原始文本逐字比较一致。35 次结果视图引用校验通过；49 份压缩归档的摘要和工具配对均有效。导出原文和归档分别位于对应报告旁的 `*-artifacts/`、`*-archives/`，没有只保留摘要作为证据。

## 验证与复现

新增九个核心测试方法和一个 Gateway HTTP 测试，覆盖四个工具的真实执行、纯文本兼容、原文精确取回、首尾预览、secret 保护、截断提示、隐藏/禁止/关闭视图、反复压缩、两种原生 wire、多轮续接、策略失效、位置别名和单文件搜索。原有 JSON 字段视图测试继续通过。

核心 497 项（跳过 2），runtime 294 项（跳过 1），合计 **788 通过、3 跳过**；selftest **595/595**。runtime 第一次全量运行曾因本机前台窗口切换而在 NativeKeyboardTests 正确拒绝输入；后续全量运行通过，未修改计算机输入的安全门禁。最后的引用识别和单文件搜索修复又运行了核心全量、定向回归、Gateway 和自检；目录超过 200 项时的纯文本兼容修复另通过 41 项定向回归。预算 156 文件、48463 行。

复现会消耗 API，建议另选报告文件名：

```powershell
python scripts/verify_mimo_builtin_views.py --live --model mimo-v2.6-pro --request-limit 48 --output reports/new-builtin-pro.json
python scripts/verify_mimo_builtin_views.py --live --model mimo-v2.6-flash --strategies improved baseline --request-limit 40 --output reports/new-builtin-flash.json
python -m unittest forge.test_builtin_views forge.test_result_view forge.test_coding_tools forge.test_token_efficiency -q
python -m unittest discover -s forge -t . -q
$env:PYTHONPATH='tests;.'
python -m unittest discover -s tests -q
python -m forge.cli selftest
python scripts/budget_check.py
```

只读取本机既有 MiMo provider 凭据，不写配置、不导出密钥；发送的任务和源码均为隔离合成夹具。本轮增量补丁、哈希清单和原始备份记录分别为 `reports/mimo-builtin.patch`、`reports/mimo-builtin-files.json`、`reports/mimo-builtin-backup.txt`。它们以本轮开始时的工作区为基准，不合入其他既有修改。
