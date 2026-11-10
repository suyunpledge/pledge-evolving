# MiMo 缓存第三轮：新工具发现后的前缀稳定性

本轮直接检查缓存失配点，并以同模型、同账户、同工作区的 A/B/B/A 工具发现任务验证。没有切换推理模式、增加重复声明或填充文本来抬高命中数字。

## 找到并修复的问题

旧实现每次根据当前可见工具按名称排序。原来声明为 `[file_outline,...,write_file]`，发现 `aaa_total` 后会变成 `[aaa_total,file_outline,...,write_file]`：新工具插入已经发送的声明前方，改变后续整个前缀。跨轮系统提示还会重新拼接可见工具名，使已保存的真实工具历史因头部变化而放弃复用。

新增 `forge/tool_surface.py`：首次投影确定顺序，后续发现的工具追加在末尾；工具撤销立即移除，定义修改立即采用当前 schema，不会为了缓存保留失效能力。子 Agent 继承父声明顺序，但按自己的工具范围重新过滤，之后各自维护顺序。

Agent 的运行、缓存 guard、压缩事务预算统一使用这个投影。系统提示改为引用当前请求里的工具声明，避免再次复制可变工具名。独立调用 `toolwire.tool_declarations()` 的缺省字母排序保持兼容。

同时修复了工具声明清洗器的浅拷贝：补 description、移除空 enum 等操作现在作用于深拷贝，原始注册表和已投影的旧声明不会被后续清洗改写。权限、隐藏规则、延迟加载及执行确认仍经过原来的 Registry / ActionExecutor。

## 真实 API 对照

每个模型运行四个样本：字母重排 A → 会话追加 B → 会话追加 B → 字母重排 A。

每次任务都要求先 `tool_search` 发现一个可执行的算术夹具工具，再实际调用 `aaa_total(17,25)`，最后返回 42。宿主校验函数只执行一次、参数正确、搜索与执行步骤成功以及最终结果正确。两个策略使用完全相同的内置开发工具面和新增工具，区别只是新增后的位置。

四组最初的实际 HTTP JSON 摘要分别在各模型内完全相同。flash 还核对了忽略顺序后的工具声明摘要，证明变化是工具顺序而不是不同工具集合。

| 模型 / 策略 | 完成任务 | 已观测缓存 token / 已观测输入 | 加权命中率 | 缺失缓存回执 |
| --- | --- | ---: | ---: | ---: |
| pro / A 字母重排 | 2/2 | 2,816 / 8,763 | 32.14% | 0 |
| pro / B 会话追加 | 2/2 | 6,784 / 8,881 | 76.39% | 0 |
| flash / A 字母重排 | 2/2 | 4,416 / 7,644 | 57.77% | 1 |
| flash / B 会话追加 | 2/2 | 5,952 / 7,676 | 77.54% | 1 |

两个模型共 **8/8 工具任务通过、16 次实际工具动作成功**。每个样本都是三个成功模型请求，未增加模型轮数。pro 和 flash 完成对照时各有一次 TLS 失败后同模型重试，失败尝试保留在原始记录中。

pro 的已观测未缓存输入为 A 5,947、B 2,097 token。该批数据支持修复这类前缀破坏，但不代表完整账单或所有任务的固定收益。

进一步只看发现新工具后的第一个请求：pro 的 A 两次均明确为 0 命中；B 两次分别为 0 和 1,408 命中。flash 的 A 为 0/未知，B 为未知/1,024。因此追加声明不能保证每次命中；服务端仍有明显波动。最初请求即使 JSON 相同也有命中差异，整组比例包含这种波动，不能全部归因于排序。flash 的未知回执未按零处理，也未参与该命中率分母。

原始结果：`reports/mimo-cache3-order-pro-native.json`、`reports/mimo-cache3-order-flash.json`。本场景针对动态工具发现，不能直接和前两轮文件任务的整体比例比较。

## 实测暴露的协议冲突

第一轮尝试保留在 `reports/mimo-cache3-order-pro.json`：旧系统提示在提供原生工具接口时还要求输出 XML，pro 出现三次不完整文本工具调用，导致一个样本失败。这不是缓存优化成功的数据，未混入上表。

系统提示已改为优先原生工具接口，XML 只作为无原生接口时的兼容说明。随后完成的对照没有格式错误重试；flash 的八次工具调用均走原生返回，pro 有一次完整文本调用被既有兼容解析器接受。没有把不完整参数执行为动作，也未删除失败记录。

## 适用边界

追加顺序主要保留工具声明中已经存在的部分。新增 schema 仍会改变其后序列，不能保证系统提示或全部历史在服务端继续命中。工具被撤销、schema 更新、切换模型/端点/账户、上下文压缩等仍可能产生必要失配。

MiMo [官方计费说明](https://mimo.mi.com/docs/en-US/pricing) 将命中归因于请求前缀；其[推理系统说明](https://mimo.mi.com/docs/en-US/news/latest/updqate)也涉及缓存亲和路由和分层回收。本文仅以本机请求和 usage 判断客户端改动及实际观测，不推断某一次服务端未命中的具体原因。

## 验证与复验

新增测试覆盖两种原生协议、追加顺序、撤销与重新注册、隐藏工具、schema 更新、原始 schema 不变、子 Agent 顺序隔离，并通过真实 HttpTransport 请求体夹具验证工具发现后的跨轮历史延续。

核心回归 449 项（2 跳过），客户端/运行链 206 项（1 跳过），合计 **652 项通过、3 项跳过、0 失败**。框架自检 **595/595**；代码预算 149 个文件及 Git 空白检查通过。

包括协议问题定位的首次尝试，本轮共 33 次 HTTP 尝试、30 次成功响应及 3 次 TLS 失败。成功回执输入合计 44,597、输出 1,436 token；失败尝试的实际服务端消费未知，未作为已核对账单。

```powershell
python -m unittest forge.test_tool_surface forge.test_runtime_structure -q
python -m unittest discover -s forge -t . -q
python -m forge.cli selftest
python scripts/budget_check.py

# 真实 API，消耗现有 Token Plan 额度；每个模型上限 20 次 HTTP 尝试。
python scripts/verify_mimo_live.py --live --suite tool-order --model mimo-v2.6-pro --retries 1 --request-limit 20 --output reports/tool-order-repeat.json
```

日志为 `reports/mimo-cache3-surface-tests.log`、`reports/mimo-cache3-core.log`、`reports/mimo-cache3-runtime.log`、`reports/mimo-cache3-selftest.log`。独立补丁为 `reports/mimo-cache3.patch`，文件清单为 `reports/mimo-cache3-files.json`，开始时的工作区快照位置见 `reports/mimo-cache3-backup.txt`。
