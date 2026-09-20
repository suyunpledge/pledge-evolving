# 三席联审 · TRAE 任务

> 你负责审查：`edit_file` / `read_range` / `file_outline` 的代码正确性 + `test_coding_mode` 自检覆盖度
> 输出到：`reviews/coder-verdict.md`

## 材料
- `reviews/step1-review-brief.md`（Step1 交付范围 + 关键 diff + 三处设计决定）
- `forge/tools.py`（重点看 `edit_file` / `read_range` / `file_outline` 函数体）
- `forge/selftest.py`（重点看 `test_coding_mode` 17 条断言）

## 审查要点（代码质量视角）
1. `edit_file` 的锚点模糊拒绝（count>1 && !replace_all）是否合理？replace_all=True 时是否有进度提示？
2. `file_outline` 正则 `^(async\s+)?(def|class)\s+([A-Za-z_]\w*)` 能否覆盖 `@staticmethod` / `@property` 等装饰器场景？
3. `read_range` 的 end 缺省取 `len(lines)` 是否有意外行为？空文件怎么处理？
4. `test_coding_mode` 17 条断言是否覆盖了边界情形？遗漏哪些（edit_file 的 start_line>end_line、apply_patch 空 patches 数组、expose 为空等）？
5. `coding.json` 暴露表里的 shell_exec 在 Step1 是必要的，Step2 加入 run_tests 后是否需要立即从 expose 移除？

## 输出格式
逐条 `[OK/ISSUE/QUESTION] + 描述 + 建议 + 影响范围`
