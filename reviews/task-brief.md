# 三席联审任务简报 · Step1 编程模式

> 生成时间：2026-09-18
> 审查对象：forge Step1 编程模式（`tools.py` / `cli.py` / `loop.py` / `selftest.py` / `bundles/modes/coding.json`）
> 交付产物：三份审查意见 → `reviews/planner-verdict.md` / `reviews/security-verdict.md` / `reviews/lead-verdict.md`

---

## 席 1 · planner（架构审查）

**视角**：整体设计一致性、与现有三轴（profile/strategy/thinking）的正交性、未来扩展路径

**关注点**：
1. `bundles/modes/coding.json` 作为"第四轴"与 profile/strategy/thinking 是否真正正交？是否存在 profile=conservative 与 --coding 同时出现时的优先级问题？
2. `tools.expose` 配置行生效后，哪些工具会被隐藏？暴露表是否足够表达"最小权限"？
3. `loop.py` checkpoint 改为 `WRITE_TOOLS` 表化是否正确？`WRITE_TOOLS` 当前 = 5 项，这五项是否都应该触发快照？快照失败的降级路径是什么？
4. 下步 `run_tests`（白名单）和 `checkpoint` 联动是否有设计缺口？

**交付物**：`reviews/planner-verdict.md`

---

## 席 2 · security（安全审查）

**视角**：`edit_file`/`apply_patch` 的权限边界、沙箱绕过风险、checkpoint 完整性

**关注点**：
1. `edit_file` 的 `start_line/end_line` 越界检查是否正确？（负数、反向、超文件末尾三种情形）
2. `apply_patch` 的原子性是否在边界情形下仍成立？比如：block[0] 锚点存在于 A 文件，block[1] 锚点不存在于 B 文件，A 文件是否不被修改？
3. `replace_all=True` 时的 DoS 风险：恶意内容连续替换大文件是否会被无限执行？
4. `checkpoint` 改为 `WRITE_TOOLS` 后，`shell_exec` 是否也应该触发快照？`notebook_edit` 同样没覆盖，是否遗漏？
5. `shell_exec` 保留在编码 expose 表 + `ask` 门控的组合是否安全？无头模式下 ask → deny，会不会导致编码模式"只有读、不能跑测试"？

**交付物**：`reviews/security-verdict.md`

---

## 席 3 · coder/reviewer（代码质量审查）

**视角**：代码正确性、边界处理、测试覆盖度、自检是否足够

**关注点**：
1. `edit_file` 的锚点模糊拒绝（count > 1 && !replace_all）是否合理？`replace_all=True` 时是否有进度提示？
2. `file_outline` 正则 `^(async\s+)?(def|class)\s+([A-Za-z_]\w*)` 是否能覆盖 `@staticmethod` / `@property` 等装饰器场景？
3. `read_range` 的 `end` 缺省取 `len(lines)` 是否有意外行为？空文件怎么处理？
4. `selftest` `test_coding_mode` 的 17 条断言是否覆盖了足够多的边界情形？是否有遗漏（如 `edit_file` 的 `start_line > end_line` 情形、`apply_patch` 的空 patches 数组、`expose` 表为空等）？
5. `coding.json` 暴露表里的 `shell_exec` 在 Step1 是必要的，但 Step2 加入 `run_tests` 后是否需要立即替换？

**交付物**：`reviews/coder-verdict.md`

---

## 三席操作说明

| 席 | 任务 | 输出文件 |
| --- | --- | --- |
| planner | 架构设计审查 | `reviews/planner-verdict.md` |
| security | 安全权限审查 | `reviews/security-verdict.md` |
| coder/reviewer | 代码质量审查 | `reviews/coder-verdict.md` |

**执行方式**：
1. 读取 `reviews/step1-review-brief.md`（本文件）
2. 读取对应源码文件：`forge/tools.py`、`forge/cli.py`、`forge/loop.py`、`forge/selftest.py`、`bundles/modes/coding.json`
3. 按关注点逐条给出：`[OK/ISSUE/QUESTION] + 描述 + 建议 + 影响范围`
4. 写入对应 verdict 文件，并给操作员一个汇总摘要
