# lead-verdict · Step1 编程模式（主持人 + 综合汇总席）

- 审查人：lead（主持人 / 综合席）
- 审查时间：2026-09-18 13:25（UTC+8；落盘前时钟核对 13:23:25）
- 审查基线：`agent-forge` 工作树（HEAD=`1948de4`；修改：`forge/tools.py` / `forge/cli.py` / `forge/loop.py` / `forge/selftest.py`；新增：`bundles/modes/`、`forge/bundles/modes/`、`reviews/`）
- 复核方法：通读 diff + 相关模块源码（policy / checkpoint / registry / config / cli）；独立重跑 selftest / doctor / dump-config；自写 12 项边缘探针 + 5 组 CLI 组合矩阵；本机实际构建 wheel 检查打包内容；运行离线 smoke。
- 结论速览（v2 · 13:57 定稿）：三席合并后发现 **P0（`apply_patch` 路径沙箱逃逸）**——修复通道已修复、lead 独立复验闭环（修复后五路全拒 + 修复前修订模拟复现 + 482/482）；**P1-1 wheel 打包** 已修复复验；**P1-2 测试缺口** 部分完成；其余 P2/P3 见 §5。当前工作树：selftest 482/482 exit=0、doctor 7/7、wheel 含 `modes/`。

---

## §1 独立复核硬结果（三席共享证据）

| 复核项 | 交付声明 | 我的复核结果 |
| --- | --- | --- |
| `selftest` | 477/477, exit 0 | ✅ 复跑一致：`477/477 checks passed`，exit=0（13:21:14→13:21:31；日志 `.openclaw/tmp/selftest-recheck-0918.log`） |
| `doctor --coding` | 7/7 | ✅ 复跑一致（8 rows from layers ['base.json','coding.json']） |
| `dump-config --coding` | acceptEdits / 40 / smart / 13 | ✅ 复跑一致 |
| 两副本一致 | 两份同步 | ✅ SHA256 相同 `4210DD52…9C61F`（实测） |
| **wheel 打包实证** | （未声明） | ❌ **新发现：wheel 内只有 `forge/bundles/base.json`，`modes/coding.json` 未随包**（见 L-01） |
| 组合矩阵（5 组） | — | ✅ 实测通过，见 §3（发现 conservative+coding 口径问题，L-19） |
| 边缘探针（12 项） | — | ✅ 完成，发现 3 处小问题（L-09/L-10/L-15） |
| `smoke --dry-run` | — | ✅ exit 0（S3/S4/S5 均 ok；注：smoke 不走 `_compose`，未覆盖 `--coding` 路径） |
| 真实 API 端到端 | — | ⏭ 未做（本复核环境无 `FORGE_*` 凭据继承；建议 Step2 验收补一次真实 provider + `--coding` 小任务） |

---

## §2 逐条意见

### A. 打包与交付一致性

**L-01 [ISSUE] wheel 未打包 `modes/coding.json`，安装版 `--coding` 会直接报错**
- 描述：`pyproject.toml:29-31` 的 package-data 只声明 `"bundles/*.json"`（不含子目录）。我本机实际执行 `pip wheel . --no-deps` 构建并检查 zip 内容：仅命中 `forge/bundles/base.json`。源码树里 `forge/bundles/` 存在所以本地运行正常；一旦走 pip/wheel 安装，`cli.py:47-49` 会 `SystemExit: --coding bundle not found`。
- 建议：package-data 增加 `"bundles/modes/*.json"`；同时给 D2 系列补一条 `d2:coding-bundle-ships` 断言；Step2 收口时在干净 venv 里装 wheel 实跑 `--coding` 作验收。
- 影响范围：打包分发型安装（发布前必修）；源码树运行不受影响。

**L-02 [ISSUE] 双副本漂移断言未覆盖新文件**
- 描述：`d2:bundle-copies-identical`（`selftest.py:2798-2801`）只逐字节比较 `base.json`。现在 `bundles/modes/` 与 `forge/bundles/modes/` 各存一份 coding.json，只改一份会静默漂移（且运行时优先读 `forge/bundles/`，repo 副本可能被忽略）。当前两份相同，属隐患而非现状。
- 建议：把断言扩为"两棵树中相同相对路径的全部文件"逐一比较（至少覆盖 `modes/*.json`），或改为发布时单源生成。
- 影响范围：长期维护与发布一致性（低危、修复便宜）。

**L-03 [OK] 交付完整性**
- 描述：四个文件 + 两份 bundle + reviews/ 均就位；修改量（+207/-4）与简报声明吻合；工作树未提交（HEAD=1948de4）——即审查对象是"待提交快照"，建议三席通过后按轮次提交并在 ledger 记一笔。
- 建议：汇总完成后统一 commit。
- 影响范围：流程/可追溯。

**L-04 [OK] `WRITE_TOOLS` 表化接线完整**
- 描述：`policy.py:127` 单一定义；`loop.py:727/749` 两处 checkpoint 触发点全部改为表引用；selftest 有对表断言；全仓库扫描仅这三处使用该集合，无残留硬编码 `{"write_file","shell_exec"}`。`notebook_edit` 虽在表内但内置注册表目前未注册该工具（无实际影响）。
- 建议：无需动作。
- 影响范围：核心机制一致性。

### B. 设计决定回应（对应简报 §三 3.1 / 3.2 / 3.3）

**L-05 [OK] 3.1 `modes/` 子目录方案方向正确**
- 描述：`_compose` 默认 `glob("*.json")` 不递归子目录，与 `d2:cli-default-bundle-resolves`（默认必须是纯 base）自洽；我实测默认 dump-config 无 tools 行、`--coding` 才叠加，行为符合设计说明。
- 建议：采纳。
- 影响范围：第四轴（任务形态）扩展基础。

**L-06 [QUESTION] 3.1 后半：`include` 显式声明 vs 目录约定**
- 描述：目录结构隐含"什么会被默认加载"这一语义，未来 mode 变多后会散落。当前无规范文档。
- 建议：短期在 README 写明"`modes/` 不参与默认合成、只能显式叠加"；中期考虑 bundle 条目级 `include`/`dependsOn` 字段（coding 声明依赖 base），Step2 前定下规范。
- 影响范围：扩展性/规范化。

**L-07 [ISSUE] 3.2 `shell_exec` 保留在编码 expose + 无头 ask→deny 的组合，导致 Step1 编码模式"不能执行命令"**
- 描述：headless 下 `resolve_ask` 把 ASK 收敛为 DENY（实测路径：registry.invoke → policy.resolve_ask）。即 `--coding` 目前真实能力 = 读 + 写（工作区内），无法跑任何命令/测试。
- 建议：Step2 引入 `run_tests` 时先拍板执行面策略：给 `shell_exec` 提供显式 opt-in（如 `--allow-exec` / 新 profile / policy 行），而不是从 expose 表静默移除；README 需写明 Step1 的这一边界。
- 影响范围：编码模式可用性预期；Step2 设计输入。

**L-08 [QUESTION] 3.3 checkpoint 粒度与成本**
- 描述：变更后 5 个写工具全部触发影子快照，每次快照 = `git add -A` + 无条件 `commit --allow-empty`（`checkpoint.py`）。长编码任务下快照数与耗时随步数线性增长；大工作区 `add -A` 开销累积（正确性无问题，降级路径完备：snapshot 失败返回 None 不炸流程）。
- 建议：可选优化（可延后）：快照前做 dirty 检查（无变更跳过 commit）；或分级（文件写类强制、`shell_exec` 按需）。
- 影响范围：性能/仓库存量；非正确性。

### C. 工具实现细节（补充席 2 / 席 3 关注点）

**L-09 [ISSUE] `edit_file.start_line=0` 被静默当作 1**
- 描述：`int(start_line or 1)` 使 0 被吞成 1（负数走拒绝、0 走放行，不一致）。探针实测：`start_line=0,end_line=1` 成功替换第 1 行。
- 建议：改显式 `None` 判断；0 与负数统一语义（拒绝或文档化）。
- 影响范围：边界语义/API 一致性（小修）。

**L-10 [ISSUE] `edit_file` 同时给 `old` 与 `start_line/end_line` 时静默走行模式，`old` 被忽略**
- 描述：探针实测（old="bbb" 保留、第 1 行被替换）。参数二义时无提示，容易让调用方误以为做了字符串替换。
- 建议：拒绝二义组合（明确报错），或在工具描述中写明优先级规则。
- 影响范围：误用风险（小修）。

**L-11 [QUESTION] `read_range` 与 `edit_file` 边界策略不对称**
- 描述：read 端宽容（start<1→1、end>len→截断，探针 5 实测 -5/99 正常返回 1-3 行）；edit 端严格（越界即拒绝，探针 7/7b）。同族工具两种策略。
- 建议：或对齐（推荐 read 保持宽容但文档写明），或统一严格。
- 影响范围：一致性/预期管理。

**L-12 [OK] `apply_patch` 原子性实测成立**
- 描述：跨文件场景（a.txt 有效块 + b.txt 无效块）→ 整体拒绝、a 未动（探针 3）；同文件多块按 buffer 链式递进正确（探针 11）；空列表/非列表/空 old/文件不存在/锚点歧义全部在写盘前拦截（探针 9 + 读码）。selftest 亦覆盖同文件回退。
- 建议：采纳。
- 影响范围：核心正确性。

**L-13 [QUESTION] "零残留"的边界：提交阶段无回滚**
- 描述：验证全部通过后按文件逐个 `write_text`；若提交中途进程失败（磁盘满/被杀）会半写。"原子"的实际语义 = validate-then-write（预检原子），非事务原子。
- 建议：文档措辞收敛，或单文件写入改 `tempfile + os.replace`；极端场景，可延后。
- 影响范围：边界场景/文档。

**L-14 [OK] `replace_all` 无无限执行风险**
- 描述：单次 `str.replace` 一遍完成，无循环；空 old 被拦；计数输出正确（探针 4：3 replacement(s)）。规模上限 = 文件大小 × 替换串，受常规内存约束。
- 建议：无需动作（回应席 2 关注点 3 的复核结论）。
- 影响范围：安全复核结论。

**L-15 [ISSUE] `file_outline` 缩进失真 + 截断无标记（minor）**
- 描述：缩进按 `len(indent)//4` 估算，tab / 2 空格风格文件层级全失真（探针 6：tab 文件 4 个符号全部 depth=0）；200 符号截断静默。
- 建议：indent 归一（tab=4）或直接输出原文缩进；截断时置 `meta.truncated` 或在尾部加提示。
- 影响范围：读体验（小修）。

### D. 测试覆盖

**L-16 [ISSUE] 17 条新断言存在明显覆盖缺口**
- 描述：未触达的路径包括——`edit_file` 行区间模式（约其代码 1/3，当前 **0 执行**）、锚点歧义拒绝（count>1）、`replace_all`、越界三种情形（s<1 / e<s / e>len）、空或非列表 `patches`、`edits` 别名、`read_range` 负值/超尾、`file_outline` 上限；两副本一致性（L-02）与打包断言（L-01）也未覆盖。
- 建议：至少补 10 条，优先级从高到低：行区间（含空 new 删行）、越界三情形、歧义拒绝、replace_all、空 patches、edits 别名。
- 影响范围：回归安全网；编码流奠基轮的测试债会在 Step2+ 放大。

**L-17 [OK] 断言结构质量好**
- 描述：正/负例并存（missing-anchor、atomic-refuse）；"默认不泄漏"检查（default-has-no-coding-policy）；用假工具 `media_probe` 证明 expose 真收敛（而非只看配置文本）。
- 建议：采纳。
- 影响范围：测试设计参考。

### E. 行为面 / 文档 / 流程（综合视角）

**L-18 [QUESTION] 默认工具面 9→13 与 "expose 收敛" 的实际口径**
- 描述：新 4 工具全局注册；实测默认 registry（expose=None）= 13 项、已含全部新工具（"行为零变化"注释仅对 expose 接线成立）。另实测：`--coding` 的 expose 13 项 == 当前内置全量 13 项，**隐藏数 = 0**——收敛机制为 Step2+/贡献工具铺路，Step1 实际零收窄。
- 建议：README/变更说明写清这两点；若希望默认不暴露新工具，Step2 再议注册侧策略。
- 影响范围：全模式行为面文档。

**L-19 [QUESTION] `--coding × --profile` 组合口径**（实测矩阵见 §3）
- 描述：profile 覆盖 policy 行（与注释一致）；但 `conservative + --coding` 时 expose 仍 13 项（写工具"可见不可用"），提示面不一致；`balanced + --coding` 与纯 `--coding` 权限等价（均 acceptEdits），仅 allow 表列数不同（10 vs 12，实际无权限差异）。
- 建议：README 给组合矩阵；或按"权限与工具面同步收窄"原则处理 conservative 分支。
- 影响范围：边缘组合/帮助文本。

**L-20 [QUESTION] `--coding` 对不走 `_compose` 的命令无效果**
- 描述：旗标加在公共选项（`cli.py:489`），但 `smoke` 等命令不经 compose，静默 inert（`--profile` 同现状）。帮助文本未限定范围。
- 建议：帮助文本注明"作用于 compose 型命令（run / dump-config / doctor）"，或后续给 smoke 补支持。
- 影响范围：口径（低）。

**L-21 [QUESTION] 评审材料笔误**
- 描述：`step1-review-brief.md` 中 `werkzeug/tools.py`（实为 `forge/tools.py`）与 `apply_pool`（实为 `apply_patch`）。
- 建议：更正，防下游误引。
- 影响范围：材料可信度。

**L-22 [QUESTION] 版本/契约/文档同步策略**
- 描述：`__init__`/pyproject 均停 0.7.0；`contract.md`、`README.md` 无 coding 条目。若按"Step4 统一文档"处理可接受，但三轮联审里没人负责的话会成为尾债。
- 建议：本轮汇总时显式确认由谁在何时补（建议 Step2 收口时一并补 contract 与 README 的最小条目）。
- 影响范围：治理/可追溯。

**L-23 [QUESTION] Step2 预风险（供三席合并讨论）**
- 描述：① `run_tests` 白名单应复用既有 `deny_patterns` / `forbidden_programs` 防线，避免第二套安全语义漂移；② `--tools` CLI 与 bundle `tools.expose` 的优先级需先定义（建议 CLI > config > 无，且任何情况不可越过 deny）；③ "编码契约片段注入 `extra_system`" 走既有 sanitise 管道。
- 建议：在 Step2 任务书里逐条给出决策点再开工。
- 影响范围：Step2 设计输入。

---

## §3 附录：探针与组合矩阵实测

**组合矩阵（真实 CLI `dump-config`）：**

| 组合 | policy.mode | sandbox | allow_n | asks | maxSteps | expose_n |
| --- | --- | --- | --- | --- | --- | --- |
| 默认 | default | workspace-write | 7 | [shell_exec] | 12 | 无 |
| `--coding` | acceptEdits | workspace-write | 12 | [shell_exec] | 40 | 13 |
| `--coding --profile conservative` | read-only | read-only | 6 | [] | 40 | 13 ⚠ |
| `--coding --profile balanced` | acceptEdits | workspace-write | 10 | [shell_exec] | 40 | 13 |
| `--profile aggressive --i-know --coding` | dontAsk | danger-full-access | 11 | [] | 40 | 13 |

**边缘探针（12 项，脚本 `.openclaw/tmp/step1-probes/probe_step1.py`）：**

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | `start_line=0` | ⚠ 静默按 1 处理并成功替换（L-09） |
| 2 | `old` + 行区间同给 | ⚠ 行模式胜出、old 静默忽略（L-10） |
| 3 | apply_patch 跨文件原子 | ✅ 整体拒绝、a 未动 |
| 4 | replace_all | ✅ 3 处替换、计数正确 |
| 5 | read_range 负值/超尾 | ✅ clamp 到有效范围（L-11 口径） |
| 5b | read_range start>len | ✅ 拒绝 |
| 6 | file_outline tab 缩进 | ⚠ 层级全 0（L-15） |
| 7 | edit 区间越尾 | ✅ 拒绝 |
| 7b | edit 反向区间 | ✅ 拒绝 |
| 8 | `edits` 别名 | ✅ 生效 |
| 9 | 空 patches | ✅ 拒绝 |
| 10 | 空 new（删行） | ✅ 正确、无残留换行 |

---

## §4 初判优先级（初判快照 —— 已被 §5.5 定稿取代，保留备追溯）

- **P0（阻断）**：无。
- **P1（Step2 启动前修）**：L-01 打包缺陷；L-16 测试缺口（含行区间 0 覆盖）；L-07 执行面策略拍板（设计决策）。
- **P2（本轮或下轮随修）**：L-02 漂移断言、L-09 / L-10 / L-15 小修、L-08 快照成本评估、L-18 / L-19 文档口径。
- **P3（记录/延后）**：L-06 / L-11 / L-13 / L-20 / L-21 / L-22 / L-23。

---

## §5 三席汇总（定稿）— 2026-09-18 13:57（含修复动态）

### 5.1 席位与材料

| 席位 | 署名 | verdict 文件 | 落盘时间 |
| --- | --- | --- | --- |
| lead（综合） | lead | `lead-verdict.md`（本文件；§5 于 13:57 定稿） | 13:25 |
| security | DeepSeek 席（WorkBuddy 任务渠道） | `security-verdict.md` | 13:38 |
| coder | MiMo 席（TRAE 任务渠道） | `coder-verdict.md` | 13:33 |
| 集群草案 | 操作员汇总稿 | `cluster-summary.md` | 13:41（本 §5 与其对齐，并补充 lead 复核与修复动态） |

### 5.2 三席共识（C1–C6）

| # | 共识内容 | 席位 |
| --- | --- | --- |
| C1 | `edit_file` 行区间越界（负/反向/超尾）全部正确拒绝、拒绝不落盘 | lead / DS / MiMo |
| C2 | `apply_patch` 校验阶段原子性成立（任一 block 校验失败 → 全不落盘） | lead / DS / MiMo（lead 探针 3 跨文件实证） |
| C3 | `replace_all` 无无限循环风险（单次 str.replace，一遍完成） | lead / DS |
| C4 | `WRITE_TOOLS` 表化方向正确、接线完整（loop 两处 + 断言；全仓库无残留硬编码） | lead / DS / MiMo |
| C5 | `shell_exec` 在无头编码模式下静默 deny，执行面缺口三席同向确认 | lead / DS / MiMo |
| C6 | `test_coding_mode` 有覆盖缺口（行区间 0 执行；边界/别名/歧义等） | lead / DS / MiMo |

### 5.3 分歧点裁定（D1–D8）

| # | 分歧 | 裁定 | 依据 |
| --- | --- | --- | --- |
| D1 | `apply_patch` 提交阶段原子性 | ISSUE 成立（P2，未修） | DS 实测（写盘中途失败 → 首个文件已改、无回滚）；lead 读码路径一致；MiMo 判 OK 系未覆盖 I/O 失败路径 |
| D2 | **`apply_patch` 路径沙箱逃逸** | **P0 成立，且已修复+复验（见 5.4）** | DS 实跑三路径；lead 以「修复前修订」模拟复现（ok=True、越界文件被写）；修复后五路全拒 |
| D3 | `edit_file start_line=0`（另含浮点 2.7 截断） | ISSUE 成立（P2，未修） | DS + lead 双向实测一致 |
| D4 | `edit_file` old+行区间同给 → 静默走行模式 | ISSUE 成立（P2，未修） | DS + lead 实测一致 |
| D5 | wheel 打包缺 `modes/coding.json` | **P1 成立，已修复+复验**（lead 独有发现，DS/MiMo 未覆盖打包面） | lead wheel 实测（修复前缺、修复后含） |
| D6 | `file_outline` 缩进失真 | minor ISSUE（P2，未修） | lead 探针（tab 文件层级全 0）；MiMo「足够」不改变实测缺陷 |
| D7 | 测试覆盖缺口 | 部分修复：P0 断言 5 条已补；其余缺口仍开放（P1-2 余量） | 三席清单合并 |
| D8（新增） | `shell_exec` 处置路线 | 采用梯度：Step2 以 `run_tests` 白名单为主 + ask 后备；无头 expose 裁剪 + doctor 提示一并评估；不设「立即移除」硬条件 | DS（无头移除提议）vs MiMo（保留后备至 Step3）取融合 |

### 5.4 【修复动态】13:43–13:57（修复通道 + lead 复验 + lead 修正）

| 项 | 修复内容 | 文件 | lead 复验结果 |
| --- | --- | --- | --- |
| P0-1 沙箱逃逸 | `Policy.evaluate` 沙箱目标提取改为「touching ∪ patches/edits[].path」并集；无容器参数时回退旧逻辑 | `forge/policy.py`（13:43） | ✅ 五路全拒（绝对 / `../` / decoy / edits 别名 / 只读沙箱）；`verify_p0_fix.py` 5/5；pre-fix 模拟确认原漏洞（ok=True） |
| P1-1 wheel 打包 | package-data 增加 `bundles/modes/*.json` | `pyproject.toml`（13:43） | ✅ 重建 wheel：`forge/bundles/modes/coding.json` 已在包内 |
| P0 回归断言 | `test_coding_mode` 追加 5 条 sandbox 断言 | `forge/selftest.py`（13:45） | ⚠️ 初版三处缺陷（见下），**lead 已修正**：13:57 全量 **482/482 exit=0** |
| 独立验证脚本 | `verify_p0_fix.py`（5 断言 + 退出码） | 仓库根（13:52，未跟踪） | ✅ 全过；建议正式归档（`verify/` 或 reviews/） |

**lead 对回归块的修正（三处）**：① 原块缩进错位、被排除在 `TemporaryDirectory` 作用域外，引用了已随上下文销毁的首个临时工作区（`_decoy.write_text` 直接 FileNotFoundError，整测试套 crashed）；② 明细串笔误 `read_text_text`（即使缩进修好也会 AttributeError）；③ 只读用例 `ToolContext` 缺 `workspace` 参数。修正案：重写为自持 `tmp3` 作用域 + `_ctx3` 独立上下文；备份 `.openclaw/tmp/step1-probes/selftest.py.bak-lead-fix`；修正后 482/482（日志 `.openclaw/tmp/selftest-postfix2-0918.log`）。

### 5.5 最终优先级排序（定稿）

**P0**

- P0-1 `apply_patch` 路径沙箱逃逸 —— ✅ **已修复并复验**（并集提取 + 5 断言 + 独立验证）。待办：随本批 commit；建议追加 handler 级兜底（纵深防御）与 `resolve()` 路径归一（顺带解 P2-4）。

**P1**

- P1-1 wheel 未打包 `modes/coding.json` —— ✅ **已修复并复验**（wheel 重建检查通过）。待办：干净 venv 实测 + d2 系列补 modes 断言。
- P1-2 测试缺口 —— ◐ **部分完成**：5 条 P0 断言已补；仍缺：行区间全谱（start>end、空 new 删行、0/负值/超尾拒绝）、歧义拒绝、replace_all、空/非列表 patches、`edits` 别名、路径别名（归一后应合并或拒绝）、`read_range` 边界、`file_outline` 上限/无符号。目标补至 ~25 条。

**P2（本轮或 Step2 随修）**

- P2-1 `edit_file`：`start_line=0` 静默按 1、浮点截断（int(2.7)）、`old`+行区间互斥缺失 → 拒绝或文档化（含 3 条断言）。
- P2-2 `apply_patch` 提交阶段非原子（I/O 失败半写）→ 单文件 tempfile+replace，或 meta 记录已提交清单。
- P2-3 路径别名静默丢改动 + `meta.files` 谎报（`a.txt` vs `sub/../a.txt`；lead 13:52 复现）→ buffers 键 `resolve()` 归一。
- P2-4 `shell_exec` 无头口径：expose 裁剪 + doctor 提示（Step2 设计会拍板，参考 D8）。
- P2-5 `file_outline` 缩进（tab=4 归一）+ 200 截断标记。

**P3（记录/延后）**

- P3-1 双副本漂移断言未覆盖 `modes/`（现两副本一致；扩断言）。
- P3-2 `read_range` 全文件截断 UX（meta.truncated）；clamp 与 edit reject 的语义说明。
- P3-3 `file_outline` 对 `type` 别名/装饰器输出范围文档化。
- P3-4 `replace_all` 文档注释（规模建议；DoS 不构成风险已裁）。
- P3-5 默认工具面 9→13 与 expose 实际零收窄（13/13）的口径说明（README）。
- P3-6 `--coding × --profile` 组合矩阵文档（含 conservative 组合「可见不可用」说明）。
- P3-7 读侧不受沙箱约束属已登记边界 → 在 coding bundle 说明中显式写明。
- P3-8 版本/契约/文档同步（0.7.0 未动；contract/README 无 coding 条目）。
- P3-9 Step2 预风险：`run_tests` 白名单复用既有防线；`--tools` 优先级（建议 CLI > config > 无，且不可越 deny）；`extra_system` 走既有 sanitise 管道。

### 5.6 证据索引

- verdict：`reviews/{lead-verdict,security-verdict,coder-verdict}.md`、`reviews/cluster-summary.md`；修复验证脚本 `verify_p0_fix.py`（仓库根）。
- lead 探针/日志：`.openclaw/tmp/step1-probes/{probe_step1.py, probe_p0_check.py, probe_prefix_check.py, fix_selftest_block.py}`；selftest 日志 `selftest-recheck-0918.log`（477/477 修复前）、`selftest-postfix-0918.log`（478 崩溃）、`selftest-postfix2-0918.log`（482 全绿）；wheel 产物 `wheelhouse/`（修复前）与 `wheelhouse2/`（修复后）；回归块备份 `selftest.py.bak-lead-fix`。
- DS 探针持久副本：`workspace\deepseek\review-artifacts\step1-security\`。

### 5.7 下一步建议（收口动作）

1. ✅ 已提交并推送：`62269f2`（9 文件，+430/−7，含 verify 脚本归档 `verify/probe_step1_p0_sandbox.py`）；远端 ls-remote 复核一致。
2. 下一轮：P1-2 余量断言 + P2-1..P2-5 小修 + P0 handler 级兜底（建议由 agent-f5hc9 执行）。
3. Step2 设计会前置决策：D8（shell_exec 路线）、P3-9（预风险三条）、P2-1/P2-3 参数语义。
4. （补记 14:06）一轮小修完成：P2-1 / P2-2 / P2-3 / P2-5 + P0 handler 兜底；selftest 482→503（503/503）；独立审计维持基线（17 项，FAIL 1=已声明边界）；本地提交 `5703afe` + `7a521cf`（audit 刷新；推送待核）。
