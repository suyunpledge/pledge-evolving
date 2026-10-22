# 工具调用稳定性修复

基线：`8dc2944e943b0a46d9e6b91e5bc82463dc1074f1`。
分支：`fix/tool-invocation-stability`。环境：Windows / Python 3.12。

## 修复与接口边界

本轮修改公共 `forge/tools.py`，由 Agent 的 ActionExecutor、Gateway 和直接 ToolRegistry 调用共同使用。执行协议和权限授予逻辑沿用现有实现。

1. 原来的返回值检查只检查表层类型。嵌套对象、循环引用或非有限浮点数可能在工具执行后破坏结果持久化。现在验证严格 JSON 元数据，返回可记录的 `invalid_tool_result`；`ok` 必须是布尔值。写工具的无效结果保留 `execution_state=unknown`，读取工具记为 `completed` 且失败。没有自动重试或回滚。
2. 返回值及元数据脱离 handler 自己持有的对象。框架添加授权、检查点、动作身份等字段时，不污染 handler 缓存，后续修改原始元数据也不会改写返回记录。
3. 直接调用时，列表、字符串、布尔值、数字等参数在写锁和 handler 前返回 `invalid_arguments/not_started`。保留 `None` 等价于空参数对象的兼容行为。
4. ToolRegistry 在授权前和授权等待结束后检查取消 token。持有写锁后会重新执行这些检查，因此等锁期间取消不会继续派发。取消结果统一标记 `outcome=cancelled`。
5. 修复 `forge/runtime_lock.py` 中 Windows 首次创建锁文件的竞争：两个进程看到空文件，其中一个取得锁后，另一个尚未加锁就写初始化字节，触发 PermissionError。初始化移到取得 OS 锁之后，保留可重入与跨进程互斥语义。

修改文件：`forge/tools.py`、`forge/runtime_lock.py`、`tests/test_runtime_convergence.py` 和本报告。

## 验证

新增八个回归测试，覆盖多种损坏结果、实际写入后结果未知与新执行器去重、后续健康动作、元数据隔离、非法参数、等锁期间取消、授权期间取消、真实本地 HTTP Gateway 响应及空锁文件竞争。

- 锁修复前的执行协议专项：61/61 通过；全套测试暴露偶发跨进程竞争失败，随后压力复现为 PermissionError。
- 确定性空锁文件用例：同一测试对原版锁代码失败（PermissionError），修复版通过；另加 100 轮真实四进程版本竞争，101 次测试全部通过。
- `unittest discover -s tests -v`：177 个用例，176 通过、1 跳过，0 失败；跳过原因是 Windows 账户不能创建符号链接。这是 tests 目录测试集，不代表 forge 目录下所有独立 unittest 模块。
- `run.py selftest`：573/574 通过；唯一失败是 `global:readme-no-hardcoded-counts`，中文 README 的硬编码数量触发文档守卫。在未修改主分支上复跑同样是 573/574、相同失败，因此本轮没有新增 selftest 失败，也不宣称全量全绿。
- `git diff --check` 通过。

日志保存在用户目录：`forge-tool-stability-backend-verified.log`、`forge-tool-stability-selftest-verified.log`、`forge-tool-stability-baseline-selftest.log`、`forge-tool-stability-lock-baseline.log`、`forge-tool-stability-lock-fixed.log`。早期失败保留在 `forge-tool-stability-backend-final.log` 和 `forge-tool-stability-race-stress.log`，未将早期失败计作成功。

## 兼容性与限制

合法 ToolResult 保持原调用 API；元数据按 JSON 规范复制，元组会变为列表，数字字典键会变为字符串。自定义工具必须返回布尔 ok、字符串输出和可序列化元数据。非法返回值现在使用专门的错误码，普通 handler 异常仍为 `tool_error`。

取消检查不强制杀死已经进入 handler 的调用；运行中断仍需要 handler 配合 cancellation token。此次未增加可能重复产生副作用的自动重试，也不提供任意工具的 exactly-once 保证。

主检出保持原基线，修复仅提交独立分支；未合并 main。
