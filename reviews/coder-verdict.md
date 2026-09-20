# Coder Verdict · Step1 编程模式代码质量审查

> 审查人：MiMo（席 3 · coder/reviewer）
> 时间：2026-09-18

---

## 1. `edit_file` 锚点模糊拒绝 + replace_all 进度提示

**[OK] 锚点模糊拒绝逻辑正确**

count == 0 → 拒绝（"不存在"）；count == 1 → 直接替换；count > 1 且 `replace_all` 未设 → 拒绝（"模糊"）。逻辑完备无死角。

**[OK] replace_all=True 返回 `meta={"matches": count}`**，调用方可知替换次数，等价进度提示。

**[QUESTION] replace_all DoS 风险**：`str.replace` 是 O(n) 内置，10MB 文件 + 极端 payload 理论上耗内存，但编程模式下 LLM 主动调用，不构成真实威胁。建议在 `apply_patch` 文档注释加"new 长度建议不超过 old*10"即可。影响：低。

---

## 2. `file_outline` 正则 vs 装饰器

**[OK] 当前正则对真实 Python 文件足够**

`@staticmethod`/`@property` 等装饰器各占一行，紧随其后的 `def`/`class` 行仍被正确匹配。单行装饰器定义（`@staticmethod\ndef foo(): pass`）也能匹配。

**[QUESTION] 是否支持 Python 3.12+ `type` 别名？** 当前只索引 def/class。建议在 docstring 中明确声明范围。影响：可忽略。

---

## 3. `read_range` end 缺省 + 空文件

**[OK] 所有边界正确处理**

空文件 → `s > e` 返回错误；start > end → 同理；超文件行数 → 钳位后拒绝。1-indexed 行号与 file_outline 一致。

**[QUESTION]** `read_range(path="big.py", start=1)`（忘传 end）会返回整个文件——无害但冗余。可在 meta 加 `"truncated"` 提示。影响：UX 优化。

---

## 4. `test_coding_mode` 17 条覆盖度

**[OK] 核心路径全覆盖**：注册正确性（4 条）、edit_file 正常/错误（2 条）、read_range/file_outline（2 条）、apply_patch 原子性+成功（2 条）、CLI 开关+配置叠加+expose（5 条）、WRITE_TOOLS 对齐（1 条）、expose 缩减（1 条）。

**[ISSUE] 遗漏边界情形**（建议补 3-5 条）：

| 遗漏 | 重要性 |
|---|---|
| `edit_file` start_line > end_line → 应拒绝 | 中 |
| `edit_file` replace_all=True 多处替换 | 中 |
| `apply_patch` 多块全成功 | 中 |
| `read_range` 空文件 → 应返回 error | 低 |
| `file_outline` 无符号文件 → "(no symbols found)" | 低 |

**建议**：补至 20 条，至少覆盖前 3 条。

---

## 5. shell_exec Step2 处理

**[OK] Step1 保留 shell_exec 正确**（无 run_tests 时唯一命令通道）。`ask` 表分级合理。

**[QUESTION]** Step2 建议路径：`run_tests` 白名单为主，`shell_exec` 保留在 `ask`（后备），Step3 再视覆盖率决定是否 deny。不建议 Step2 直接 deny（白名单无法覆盖 `pip install`/`git status` 等）。

---

## 6. 额外发现

**[OK] apply_patch 原子性**：两阶段（校验→提交），任一 block 失败全不落盘，实现正确。

**[OK] WRITE_TOOLS 表化**：5 项含所有 Step1 写工具，loop.py 三处 checkpoint 触点均引用此集合，40 步上限内快照成本可接受。

**[OK] CLI 防覆盖**：`--coding` 生效时不注入 balanced 默认 profile，避免冲掉 coding bundle 的 policy 行。

---

## 汇总

| 类别 | OK | ISSUE | QUESTION |
|---|---|---|---|
| edit_file | 2 | 0 | 1 |
| file_outline | 1 | 0 | 1 |
| read_range | 2 | 0 | 1 |
| test_coding_mode | 1 | 1（缺 3-5 条断言） | 0 |
| shell_exec Step2 | 1 | 0 | 1 |
| 额外 | 3 | 0 | 0 |
| **合计** | **10** | **1** | **4** |

**结论**：Step1 代码质量合格，无阻塞级 issue。唯一建议是 `test_coding_mode` 补充 3-5 条边界断言，其余均为文档/UX 优化。
