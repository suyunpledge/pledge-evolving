# 三席联审汇总 · Step1 编程模式

> 汇总人：操作员（lead）13:40 UTC+8
> 席位：lead（综合）/ DeepSeek（security）/ MiMo（coder/reviewer）

---

## 一、席位结论速览

| 席位 | 结论 | OK | ISSUE | QUESTION |
|---|---|---|---|---|
| Lead | 功能通过，P1 打包缺陷 + P2 测试债 | 12 | 4 | 7 |
| DeepSeek（sec） | 1 OK / 2 ISSUE（含 1 P0 逃逸） | 1 | 4（P0×1） | 3 |
| MiMo（coder） | 代码质量合格，无阻塞 | 10 | 1 | 4 |

---

## 二、三席共识点

| # | 共识内容 | 席位 |
|---|---|---|
| C1 | `edit_file` 行区间越界（负/反向/超尾）全部正确拒绝 | lead / DS / MiMo |
| C2 | `apply_patch` 校验阶段原子性成立（任一 block 失败全不落盘） | lead / DS / MiMo |
| C3 | `replace_all` 无无限循环风险（`str.replace` 一遍完成） | lead / DS |
| C4 | `WRITE_TOOLS` 表化方向正确，接线完整 | lead / DS / MiMo |
| C5 | `shell_exec` 在无头编码模式下被静默 deny（执行面缺口一致确认） | lead / DS / MiMo |
| C6 | `test_coding_mode` 17 条覆盖核心路径，但行区间模式（0 执行）和边界断言存在缺口 | lead / DS / MiMo |

---

## 三、三席分歧点

| # | 分歧点 | Lead 意见 | DS 意见 | MiMo 意见 |
|---|---|---|---|---|
| D1 | `apply_patch` 提交阶段原子性 | ISSUE（写盘失败半写） | ISSUE（同上） | OK（忽略此边界） |
| D2 | `apply_patch` 路径沙箱逃逸 | 未直接命中（未见实跑） | **P0 ISSUE**（越界可写） | 未覆盖 |
| D3 | `edit_file start_line=0` | ISSUE（应拒绝） | ISSUE（静默提为 1） | 未提及 |
| D4 | `edit_file old + start_line` 同时给出 | ISSUE（静默走行模式） | ISSUE（同上） | 未提及 |
| D5 | wheel 打包 `modes/coding.json` 缺失 | **P1 ISSUE**（安装时崩溃） | 未覆盖 | 未覆盖 |
| D6 | `file_outline` 缩进失真 | ISSUE（tab→depth=0） | 未提及 | OK（足够） |
| D7 | 17 条断言覆盖缺口 | ISSUE（0 执行行区间） | ISSUE（含路径别名） | ISSUE（缺 3-5 条） |

**分歧优先级**：D2 > D5 > D3 > D4 > D1 > D6 > D7

---

## 四、最终优先级排序

### P0（阻断，Step2 前必须修）

**P0-1 `apply_patch` 路径沙箱完全绕过** [DS Q2 · P0]
- 根因：`patches`/`edits` 容器内的 `path` 不被 `touching` 提取器看到，沙箱零次执行
- 两条实锤路径：① 绝对路径写工作区外；② `../` 相对穿越；③ decoy 合法 `path` 关掉全部检查
- 修复：handler 内对每块 `path` 调 `sandbox.allows_write()`；或 `evaluate` 侧展开容器做并集
- 影响：任何持有 `apply_patch` 的会话均可写工作区外

### P1（Step2 启动前必须修）

**P1-1 wheel 未打包 `modes/coding.json`** [Lead L-01]
- 修复：`pyproject.toml` package-data 加 `"bundles/modes/*.json"`
- 验收：干净 venv 装 wheel，`forge --coding doctor` 通过

**P1-2 `test_coding_mode` 行区间模式零覆盖** [Lead L-16 · MiMo]
- 缺失：行越界三情形、start>end、空 new 删行、歧义拒绝、replace_all、空 patches、edits 别名
- 目标：补 10 条，覆盖上方清单

### P2（本轮或 Step2 随修）

**P2-1 `edit_file start_line=0` 静默提为 1** [Lead L-09 · DS Q1]
- 修复：`s = int(start_line) if start_line is not None else 1`；0 与负一统拒绝或文档化

**P2-2 `edit_file old + start_line` 静默走行模式** [Lead L-10 · DS Q1]
- 修复：同时给 `old` 和行区间参数 → 明确拒绝并说明

**P2-3 `apply_patch` 提交阶段非原子** [Lead L-13 · DS Q2]
- 触发：写盘中途失败（磁盘满/权限/占用）→ 部分文件已改
- 建议：单文件 `tempfile + os.replace`；或 meta 记录"已提交文件清单"供补偿

**P2-4 路径别名静默丢改动** [DS Q2]
- 修复：`buffers` 键用 `path.resolve()` 归一；meta 报告冲突块

**P2-5 shell_exec 无头 deny + 仍广告** [Lead L-07 · DS Q3 · MiMo]
- 现状：`shell_exec` 在 expose 里、non_interactive→deny、调用方无感知
- Step2：`run_tests` 入列后立即从 expose 移除 `shell_exec`；在此之前 doctor 标注"ask 规则在无头下降级"

**P2-6 `file_outline` 缩进 + 截断** [Lead L-15]
- 修复：tab=4 归一；截断置 `meta.truncated`

### P3（记录/延后）

**P3-1** 双副本漂移断言未覆盖 `modes/` [Lead L-02]
**P3-2** `read_range` 全文件截断 UX [Lead L-11 · MiMo]
**P3-3** `file_outline` type 别名支持 [MiMo]
**P3-4** replace_all DoS 文档注释 [MiMo]
**P3-5** 默认工具面 9→13 与 expose 收敛口径 [Lead L-18]
**P3-6** `--coding × --profile` 组合文档 [Lead L-19]
**P3-7** 路径沙箱只读侧无覆盖（读工具不受限）[DS 附]
**P3-8** 版本/契约/文档同步策略 [Lead L-22]
**P3-9** Step2 预风险：run_tests 白名单复用 / --tools 优先级 / extra_system 管道 [Lead L-23]

---

## 五、三席分歧深度说明

### 关于 P0（DS 独有，其余两席未覆盖）

Lead 的探针侧重路径沙箱的"工具面正确性"（registry/CLI/checkpoint），MiMo 聚焦函数实现正确性。两者均未构造越界路径文件探针。DS 用了私有 tmp 工作区实跑绝对路径 + `../` + decoy，三条路径全部实证越界可写。此结论**不受其余两席观点影响**，按 P0 处理。

### 关于 P1-2 测试缺口（三席高度一致）

三席独立发现同一缺口：`edit_file` 行区间模式在 selftest 里 0 执行。Lead 探针 7 条覆盖越界场景；DS 探针 11 条覆盖同一区域并发现静默语义问题；MiMo 在自检统计里发现行区间未被注册。三方独立收敛，建议列入 P1。

### 关于 shell_exec 执行面（三席同向）

三席均注意到 shell_exec 在无头编码模式下被 deny，但强调不同侧面：
- Lead：能力面"承诺 13 工具、一个永远拒绝"（执行面缺口）
- DS：指出"ask 规则装饰性门控"和"若用户 bypass 权限会同时放大 P0"
- MiMo：从 Step2 路径规划角度建议保留 ask 作后备
三席意见互补，汇总结论：Step2 引入 run_tests 前需明确执行面策略，并在 doctor 里标注现状。

---

## 六、下一步建议

1. **立即修 P0-1**：`apply_patch` 沙箱绕过（handler 内 per-block 检查或 evaluate 展开容器）
2. **立即修 P1-1**：wheel package-data 补 `modes/*.json`，venv 验收
3. **Step2 前修 P1-2**：selftest 补 10 条行区间/边界断言
4. **Step2 设计会前**：确认 P2-5 shell_exec 执行面策略 + P2-2 old+start 互斥规则
5. **三席同意的风险**：D2/D3/D4 属于"参数静默语义偏差"，不构成提权，但给模型编辑工具带来误操作风险——排 P2 合理，不需要阻断发布

---

*附：DeepSeek 探针脚本已存持久副本 `deepseek/review-artifacts/step1-security/`（含 `security-verdict.md` + `probe_step1_security.py` + `probe_step1_security2.py`），tmp 目录清理不影响复跑；操作员探针存 `.openclaw/tmp/step1-probes/`（12 项）。*
