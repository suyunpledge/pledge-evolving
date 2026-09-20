# Step1 编程模式 · 安全复验裁定（DeepSeek 席）

审查对象：`forge/tools.py`（`edit_file` / `apply_patch` / `read_range` / `file_outline`）、`forge/policy.py`、`forge/loop.py`、`bundles/modes/coding.json`
基线树：`workspace\agent-f5hc9\agent-forge`（Step1 交付件）
方法：只读审查 + 我方私有临时工作区实跑（探针：`.openclaw/tmp/sec-review/probe_step1_security.py`、`probe_step1_security2.py`），未改交付件、未跑真实模型调用。

**结论：三问中 1 条 OK、2 条 ISSUE；其中 Q2 含一个 P0 级路径沙箱逃逸。**

---

## Q1. `edit_file` 行范围越界检查是否都被拒绝

### [OK] 负数 / 反向 / 超末尾 / 空文件 —— 全部拒绝，且**拒绝时不落盘**

`tools.py:294-297`：

```python
s = int(start_line or 1)
e = int(end_line if end_line is not None else len(lines))
if s < 1 or e < s or e > len(lines):
    return ToolResult(ok=False, error=f"line range {s}-{e} out of bounds ...")
```

实跑 11 例（每例先重置文件、事后比对内容）：

| 用例 | 结果 | 文件是否被改 |
| --- | --- | --- |
| `start=-2, end=2` | 拒绝 `line range -2-2 out of bounds (file has 5 lines)` | 否 |
| `start=1, end=-1` | 拒绝 | 否 |
| `start=4, end=2`（反向） | 拒绝 | 否 |
| `start=1, end=99`（超末尾） | 拒绝 | 否 |
| `start=99`（仅 start 越界） | 拒绝 | 否 |
| `end_line=0`（仅 end） | 拒绝（`1-0`，`e < s` 兜住） | 否 |
| 空文件 + `start=1, end=1` | 拒绝（`file has 0 lines`） | 否 |
| 空文件 + `start=1, end=0` | 拒绝 | 否 |
| `start='abc'` | `ValueError`，被 `invoke` 兜住 → `ok=False`（循环不死） | 否 |
| `start=2, end=3`（合法） | 通过 | 是（预期） |

**影响范围**：无。三项边界（负/反向/超末尾）均已覆盖，且校验在写盘之前，失败路径零副作用。

### [ISSUE] 三处**静默强制转换**：`0`、浮点、以及「range 与 old 同时给出」

同一段代码有三个不走拒绝路径的输入（实测）：

| 输入 | 实际行为 | 风险 |
| --- | --- | --- |
| `start_line=0, end_line=2` | **`ok=True`，改写第 1-2 行** —— `0 or 1` 使 0 被静默提为 1 | 1-indexed API 下 `0` 是非法值，应报错而不是改写首行 |
| `start_line=2.7` | **`ok=True`，按第 2 行处理**（`int()` 截断） | 浮点行号静默降级 |
| `old="L2", start_line=1, end_line=1` | **`ok=True`；`old` 被完全忽略**，改写第 1 行 | 调用方以为在做锚点替换，实际做了别的编辑 |

第三条实测：`edit_file({"path":"q1.txt","old":"L2","new":"Y","start_line":1,"end_line":1})` → 结果 `'Y\nL2\nL3\nL4\nL5\n'`（`L2` 仍在，首行 `L1` 被换）。行范围分支（`tools.py:290`）先于锚点分支（`tools.py:300`）返回，`old` 从未参与判断。

**建议**：
1. 用 `is not None` 判据替代 `or`：`s = int(start_line) if start_line is not None else 1`；再把 `not isinstance(start_line, int) or isinstance(start_line, bool)` 归一到拒绝或明确文档化。
2. `old` 与 `start_line/end_line` **互斥**：同时给出直接拒绝并说明（避免"以为锚点、实际按行"的静默偏差）。
3. 加两条断言：`start_line=0` 必须拒绝；`old`+`start_line` 同时给出必须拒绝。

**影响范围**：仅命中"调用方主动传非法/冲突参数"的场景，且落在工作区内（越界仍拦）。**不构成提权**；但属于"参数被静默改写语义"，对一个给模型用的编辑工具是真实的误编辑源。

### [QUESTION] 行号语义是否要接受"仅 end_line"

`start_line=None, end_line=5` → 视为替换 1-5 行。文档写的是"overwrite a 1-indexed line range"，未说明单边缺省语义。请明确：单给 `end_line` 是"从第 1 行起"还是应拒绝？

---

## Q2. `apply_patch` 原子性：block[0] 在 A 存在、block[1] 不在 B，A 是否不被修改

### [OK] 你问的那个场景：**成立** —— A 不被修改

`tools.py:326-348` 两阶段：先全量校验进内存 `buffers`，全部通过后才在 `tools.py:349-350` 落盘。实测：

```
patches = [{A.txt: old="alpha"}, {B.txt: old="MISSING-ANCHOR"}]
-> ok=False, error='patch 1: anchor not found in B.txt'
-> A.txt 未改 = True    B.txt 未改 = True
```

**影响范围**：无。校验失败路径确实是零残留。

### [ISSUE · P0] `apply_patch` **完全绕过路径沙箱**——可写工作区外任意文件

根因是参数名与 `invoke`/`evaluate` 的目标提取约定不匹配：

- `tools.py:180`：`touching = [str(v) for k, v in args.items() if k in {"path","file","target"} and v]`
- `policy.py:198-203`：`if tool in WRITE_TOOLS: targets = list(touching) or [args 里的 path/file/target]`

`apply_patch` 的真实目标藏在 `patches` / `edits` 键里 —— 两个提取器都看不到它，于是 **`targets == []`，沙箱循环一次都不执行**。实测（`sandbox=workspace-write`，`allows_write(越界路径)` 已确认返回 `False`）：

```
apply_patch({patches:[{path:"<绝对路径>/outside/secret.txt", old:"VICTIM", new:"PWNED"}]})
-> ok=True ，越界文件内容 = 'PWNED-BY-APPLY-PATCH'        ← 沙箱被绕过
apply_patch({patches:[{path:"../outside/esc.txt", ...}]})
-> ok=True ，越界文件内容 = 'PWNED2'                       ← 相对穿越同样绕过

对照（同样路径、同样 sandbox）：
edit_file  -> ok=False 'denied by policy: edit_file'       ← 有 path 键 -> 正常拦截
write_file -> ok=False 'denied by policy: write_file'
```

**两个放大路径（均已实测）**：
1. **`edits` 别名同样绕过**（`tools.py:326` 接受 `patches` 或 `edits`）。
2. **decoy 路径可关闭检查**：额外提供一个合法 `path` 让 `touching` 非空 → `targets = list(touching)` 取到诱饵 → 只校验诱饵、真目标照样写：

```
apply_patch({"path":"decoy.txt",            # 诱饵（工作区内、合法）
             "patches":[{"path":"<越界>/victim2.txt", old:"SAFE", new:"PWNED-3"}]})
-> ok=True ，victim2 = 'PWNED-3'
```

3. **`sandbox=read-only` 也拦不住**：`read-only` 沙箱下 `write_file` 被拒（`denied by policy`），但 `apply_patch` 仍 `ok=True` 写穿到工作区外 —— 即**只读沙箱这一硬上限对 apply_patch 失效**。

**为什么这是 P0**：`coding.json` 的 policy 行注释明写"工作区内直写、**越界仍拦**"；`mode=acceptEdits` 又把 `apply_patch` 放进 `allow` 与 `tools.expose`——即**编码模式的预期主力编辑工具，恰好是唯一不受沙箱约束的写工具**。多文件补丁是它的设计用途，走这条路不需要任何特殊构造。

**建议**（任选其一，建议 ①）：
1. 在 `tools.py:180` 的目标提取里**展开容器参数**：对 `patches` / `edits` 取每块的 `path`；同时去掉 decoy 依赖——`evaluate` 侧改为「收集全部候选目标并集」而非 `list(touching) or [...]` 的短路。
2. 或者把沙箱校验下沉到 handler 内（`apply_patch` 对每个 block 的 `path` 调 `ctx.policy.sandbox.allows_write(...)`，任一越界即整体拒绝），作为与工具面解耦的兜底。
3. 无论选哪条，**必须加断言**：`apply_patch` 写到工作区外（绝对路径 + `../` 两条）→ 拒绝 + 目标文件字节不变；`sandbox=read-only` 下同理。

**影响范围**：任何持有 `apply_patch` 的会话——包括 `--coding`（预期内）、以及**任何把 `apply_patch` 列进 allow/expose 的配置**；子代理继承 `sandbox`（`policy.py` 的 `child()` 保留 sandbox），故只读子代理也不能靠沙箱挡住。破坏面 = 进程可写的任意路径（绝对路径与相对穿越均可）。

### [ISSUE] 提交阶段**不是原子的**：写盘中途失败会留下半套改动

`tools.py:349-350` 逐文件 `write_text`，没有回滚。实测（第 2 个文件只读）：

```
patches = [{A.txt: alpha->CHANGED}, {ro.txt: orig->NOPE}]
-> ok=False, error='PermissionError: [Errno 13] Permission denied ...'
-> A.txt 已被改 = True      ← 第一个文件已落盘，无回滚
-> ro.txt 内容 = 'orig'     ← 第二个未动
```

即"any failed block leaves all files untouched"对**校验失败**成立、对**写盘失败**不成立。触发条件：磁盘满、权限、文件被占用、路径是目录等。
**建议**：先写临时文件再 `os.replace` 原子改名（每文件仍非事务，但单文件不半写）；或至少把错误信息与 `meta` 里标出"已提交的文件清单"，让调用方知道需要补偿。
**影响范围**：多文件补丁在 I/O 异常下的一致性；不会扩大权限。

### [ISSUE] 路径别名导致**静默丢改动 + meta 谎报**

`buffers` 以 `Path` 为键，而 `_resolve`（`tools.py:460`）不做归一化（不 `resolve()`）。同一文件用两种写法即被当作两个缓冲：`Path("a.txt") != Path("sub/../a.txt")`。实测：

```
patches = [{a.txt: ONE->ONE-EDITED}, {sub/../a.txt: TWO->TWO-EDITED}]
-> ok=True, meta={'patches': 2, 'files': 2}
-> 最终内容 = 'ONE\nTWO-EDITED\n'      ← 第一个 block 的改动被静默丢弃（后写覆盖先写）
```

`meta.files=2` 与事实不符（实际只落了 1 个文件的 1 处改动）。同类写法还有 `./a.txt` 与绝对路径。
**建议**：`buffers` 的键改用 `path.resolve()`（或 `os.path.normcase` on Windows）；并在 `meta` 里报告"被覆盖/冲突的块"。加断言：同一文件两种路径写法出现在同一 patch 里 → 要么合并、要么拒绝。
**影响范围**：单次 `apply_patch` 内的改动丢失，且**报告与实际不符**——模型据此认为补丁已生效，后续步骤建立在错误状态上。

### [QUESTION] 是否要在 `meta` 里回传每块的落点

当前只回 `{patches, files}` 计数。若加上 `[{path, matches, replace_all}]`，上面两处"静默"都能被调用方自查。是否纳入 Step2？

---

## Q3. `shell_exec` 保留在编码 expose + ask 门控 → 无头下是否变成"只能读不能跑测试"

### [ISSUE] 确认：无头编码模式下 `shell_exec` **恒被拒**，但工具仍被广告给模型

裁决链（全链路实测）：

```
coding.json policy: mode=acceptEdits, sandbox=workspace-write, ask=["shell_exec"], expose 含 shell_exec
policy.py:208  if self._matches(self.ask, tool): return ALLOW if mode is BYPASS else ASK   -> ASK
policy.py:243  resolve_ask: ASK and non_interactive -> DENY
loop.py:865    build_agent(..., non_interactive: bool = True)      ← 默认 True
cli.py:110     cmd_run 调 build_agent 未覆盖该参数                ← 无头 run 即 non_interactive=True
```

实测决策表（`mode=acceptEdits`, `ask=["shell_exec"]`）：

| 工具 | 无头（`non_interactive=True`） | 交互（`False`） |
| --- | --- | --- |
| `read_file` / `write_file` / `edit_file` / `apply_patch` / `read_range` | allow | allow |
| **`shell_exec`** | **deny**（`ask`→`deny`） | **ask**（仍需人点头） |

所以答案是**肯定的**：`forge run --coding` 下模型看得见 `shell_exec`（`tools:expose` 13 项含它），但**任何命令都执行不了**——不只是"跑不了测试"，而是**连 `ls`/`python --version` 一类只读命令都跑不了**。任务的自我验证闭环（改完 → 跑测试 → 看结果）在 Step1 编码模式下**不可达**。

**与你 3.2 的设计说明的意见**：
- "Step1 还没有 `run_tests`，藏掉 `shell_exec` 就什么都跑不了" —— 这个理由在**交互**场景成立；但在**无头**场景下 `shell_exec` 本来就已经跑不了，保留在 expose 里只产生两个副作用：① 白占一个工具位并诱导模型反复尝试后失败（浪费步数）；② 让 `ask` 规则变成一个在默认运行方式下永不生效的"装饰性门控"。
- 这正是本项目反复出现的同一个模式（2.2 轮 G2、3.0 轮 H2、H11）：**声明了、广告了，但在主路径上不可达**。

**建议**：
1. **按运行模式裁剪 expose**：`non_interactive=True` 时不把 `shell_exec` 放进 `tools:expose`（或标 `deferred`，只在交互会话可被 `tool_search` 拉出）；交互会话保留。
2. 若 Step2 引入 `run_tests`（白名单命令），把它作为**无头可用的受控执行面**（白名单而非任意 shell），届时从 expose 移除 `shell_exec` —— 我同意你 3.2 里"Step2 就位后立即移除"的方向，建议把它写成 Step2 的验收条件之一，而不是可选项。
3. 加断言：无头 + coding → `shell_exec` 裁决为 `deny` **且不在 `visible()` 里**（现在只测了裁决，没测可见性——`coding:flag-sets-tool-surface` 恰好断言了反面：它检查 `apply_patch in exposed`，但没检查 `shell_exec` 是否也该被藏）。

**影响范围**：`forge run --coding`（无头，即默认用法）下编码模式的"执行—验证"能力缺失。**不是提权问题，是能力欺骗问题**（承诺了 13 个工具，其中一个永远拒绝）。另需注意：若使用者为了让 `shell_exec` 生效而改用 `--profile bypassPermissions` 或 `mode=dontAsk`，会**同时**放大 Q2 的 apply_patch 逃逸——两条修正应一起做。

### [QUESTION] `ask` 语义在无头下缺席，是否需要"显式声明不可交互"的位置

`resolve_ask` 把 ASK 收敛成 DENY 是合理的安全默认。但对"门控永不生效"这种配置，静默降级没有痕迹——建议 `doctor` 或 `dump-config` 对"`ask` 规则在 `non_interactive` 下必然降级"的情况给一行提示。是否纳入 Step2？

---

## 附：对 brief 三处设计决定（3.1 / 3.2 / 3.3）的意见

- **3.1 `modes/` 子目录**（[OK] 方向可行 / [QUESTION] 长期形态）：放 `modes/` 规避"顶层 json 全被当默认层"是正确的当下解法；但用**目录结构隐含依赖**在 bundle 增长后会变脆（例：`modes/` 里第二个文件是否也会被加载？谁负责 glob？）。建议 Step2/3 引入 `"include": [...]` 显式声明，`modes/` 退化为约定而非机制。
- **3.2 `shell_exec`**：见 Q3。
- **3.3 `WRITE_TOOLS` 表化**（[OK] 统一是对的 / [ISSUE] 建议按工具分级）：`WRITE_TOOLS` 现为 5 项（实测 `['apply_patch','edit_file','notebook_edit','shell_exec','write_file']`），`loop.py:727,749` 用它决定 checkpoint。但 `shell_exec` 的快照**成本高而价值低**（命令的副作用不可回滚，快照只保护文件）；建议拆 `CHECKPOINT_TOOLS ⊂ WRITE_TOOLS`（如 `write_file/edit_file/apply_patch/notebook_edit`），`shell_exec` 只审计不快照。这是成本优化，不是安全问题。

## 附：一条与本次三问相邻的既有边界（[QUESTION]）

读工具不受沙箱约束（实测 `read_file` / `read_range` 读工作区外路径 → `ok=True`）。`policy.py` 的设计里写操作才走沙箱（`if tool in WRITE_TOOLS`），读侧靠声明制与运行期白名单。这在你们的既有审查里属"已登记边界"，但**本次 brief 未提及**，而 Q3 恰恰在讨论"编码模式的能力面"——建议在 coding bundle 的说明里显式写一句：编码模式下**读不受限、写（除 apply_patch，见 Q2）受限**，避免使用者误以为编码模式是路径隔离的。

---

## 复跑

```powershell
cd .openclaw\tmp\sec-review      # 我方探针副本（只读审查；落盘均在私有 tmp 工作区）
python probe_step1_security.py   # Q1 11 例 / Q2 校验-提交-别名-沙箱 / Q3 决策链
python probe_step1_security2.py  # decoy 绕过 / edits 别名 / 只读沙箱 / 读侧边界
```

关键期望值（复跑判据）：
```
Q1: start=-2 / end=-1 / 4>2 / 1..99 / end=0 / 空文件 -> 全部 ok=False 且文件字节不变
Q1: start=0 -> ok=True（静默提为 1）   start=2.7 -> ok=True（静默截断）
Q1: old+start_line 同时给 -> old 被忽略，首行被改写
Q2: 校验失败 -> A.txt 不变；写盘失败 -> A.txt 已变（非原子）
Q2: a.txt 与 sub/../a.txt 同 patch -> 只剩后一块的改动，meta.files=2
Q2: apply_patch 越界（绝对 + ../）-> ok=True 且越界文件被改（edit_file/write_file 同路径为 denied）
Q2: sandbox=read-only 下 apply_patch 越界 -> 仍 ok=True
Q3: acceptEdits + ask[shell_exec] + non_interactive=True -> allow→ask→deny
```
