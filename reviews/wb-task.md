# 三席联审 · WorkBuddy（CodeBuddy）任务

> 你负责审查：`edit_file` / `apply_patch` 的实现正确性与原子性
> 输出到：`reviews/security-verdict.md`

## 材料
- `reviews/step1-review-brief.md`（Step1 交付范围 + 关键 diff + 三处设计决定）
- `forge/tools.py`（重点看 `edit_file` / `apply_patch` 函数体）
- `forge/selftest.py`（看 `test_coding_mode`）

## 审查要点（安全视角）
1. `edit_file` 的行范围越界检查：负数、反向（start>end）、超文件末尾三种情形是否都被拒绝？
2. `apply_patch` 原子性：block[0] 锚点存在于 A 文件，block[1] 锚点不存在于 B 文件，A 文件是否不被修改？
3. `replace_all=True` 时的循环替换 DoS 风险：是否应该对替换次数加上限？
4. `checkpoint` 改为 `WRITE_TOOLS` 表后，`notebook_edit` 也在 WRITE_TOOLS 里但是否在 checkpoint 触发集里？
5. `shell_exec` 保留在编码 expose + ask 门控的组合：无头模式下 ask→deny，编码模式是否变成"只有读、不能跑测试"？

## 输出格式
逐条 `[OK/ISSUE/QUESTION] + 描述 + 建议 + 影响范围`
