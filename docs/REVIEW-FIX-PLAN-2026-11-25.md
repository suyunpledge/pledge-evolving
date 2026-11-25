# 外部评审修复方案（交给 Codex 执行）

日期：2026-11-25　来源：通过 Claude API 以 Agent 身份运行框架后给出的评审
工作位置：主检出目录 `pledge-evolving` 的 `main`（建议新开分支 `fix/review-2026-11-25`）

## 约束（所有任务通用）

- 内核 `revon/` 只用 Python 标准库，不新增第三方依赖。
- 每项改动都配测试；提交前跑 `python run.py selftest` 和涉及到的 Qt 测试（`revon-gui/qt_frontend/test_*.py`，渲染测试不要强制 `QT_QPA_PLATFORM=offscreen`）。
- 每个任务单独提交，提交信息用中文，格式与仓库现有提交一致（`fix(scope): …`）。
- 不改与任务无关的文件；不推送。

---

## P0　截断提示让模型用不存在的工具（已确认的 bug）

**现象**：超长工具输出被截断后，提示模型调用 `artifact_read` / `artifact_grep`，但桌面 Work 模式下模型拿不到这两个工具，只能改用 `read_range` 绕开。

**根因（已核实）**：
1. `revon/artifacts.py` 的 `summary()`（约 150 行）**无条件**写出“Use artifact_read(...) or artifact_grep(...)”。
2. `revon/builtin_views.py` 的 `HELP` 被拼进 `grep`、`find_files`、`list_dir`、`file_outline` 的工具说明（`revon/tools.py` 约 892、908、1006、1306 行），同样不看读取工具是否可用。
3. 桌面端允许清单 `revon-gui/qt_frontend/permissions.py` 的 `MEDIATED` 不包含 `artifact_read`、`artifact_grep`，沙箱档位下它们不会暴露给模型。
4. 对比：`revon/execution.py` 约 394 行在保存结果视图（`result_fields`）前，已经检查了 `artifact_read` 是否注册、是否为只读、是否被策略允许。只有这一处判断过可用性。

**修改**：
1. 把 `artifact_read`、`artifact_grep` 加入桌面 `MEDIATED`，并确认 `authorize()` 把它们判定为 `ToolEffect.READ`。它们只读当前工作区的 artifact 存储，句柄不能当路径用，所以不扩大读取范围。同时检查 `revon/bundles/agents/*.json` 和 `bundles/modes/*.json`：凡是暴露了 `grep`、`read_file` 的 Agent 或模式，都要把这两个工具加入 `expose` 和 `allow`。
2. 把 `execution.py` 里的可用性判断提成一个函数，例如 `artifact_reader_available(registry, policy)`。`retain_result()` 和 `summary()` 接收这个结果：
   - 可用时，提示保持不变；
   - 不可用时，改为“完整输出已保存（id），但当前会话没有读取工具；请缩小范围后重新查询（例如给 grep 加 path、glob，或用 read_range 读取具体行）”，并且不提 `artifact_*`。
3. `HELP` 后缀改为按注册表决定是否出现：在生成工具声明时（`tool_surface` 或 registry 输出 schema 的地方）判断，读取工具不可用就去掉这句。
4. `revon/result_view.py` 的 `REFERENCE_LABEL` 也做同样的条件处理。

**验收**：
- 新测试一：注册表里没有 `artifact_read` 时，超长 `grep` 结果的摘要和这四个工具的说明里都不出现 `artifact_read` / `artifact_grep`。
- 新测试二：桌面 `sandbox` 档位下，`authorize('artifact_read', …)` 返回允许；产生一个超长输出后，用返回的 id 调 `artifact_read` 能读到第二页。
- 现有 `revon.test_artifacts`、`test_builtin_views`、`test_result_view`、`qt_frontend.test_permissions` 全部通过。

---

## P1　README 的限制说明与实际实现不一致

**现象**：README 第 181 行写“Reads are not sandboxed”，但 `revon/policy.py` 已有 `read_scope`（`ReadScope.AUTO` / `workspace` / `ALL`），Agent bundle 也在用 `"readScope": "workspace"`。

**修改**：
1. 查清 `ReadScope.AUTO` 在 CLI、桌面各权限档位下的实际效果：哪些路径能读，`READ_PATH_TOOLS` 以外的工具（如 `shell_exec`、MCP）是否受限。
2. 按实际情况重写 `README.md` 和 `README.zh.md` 的 Limitations 一节，写清楚三件事：
   - 文件读取工具受 `readScope` 约束，默认范围是什么；
   - 什么情况下仍能越出工作区，例如 `readScope: all`、能执行命令的工具；
   - 这不是操作系统级隔离。
3. 同时核对其他限制项是否仍然成立并相应更新：连接池、能力包签名、记忆检索。
4. 运行 `python run.py selftest`，用实际通过数更新 README 徽章（目前写的是 598/598）。

**验收**：README 中英文版说法一致，每条限制都能对应到代码里的具体位置；徽章数字与实际运行结果一致。

---

## P1　显式 `allow` 可以越过模式默认限制，缺少提醒

**现象**：README 自己承认，显式 `allow` 规则可以绕过当前模式的默认限制，配错了不会有任何提示。

**修改（只加提醒，不改语义，避免破坏现有配置）**：
1. 在 `revon/policy.py` 加一个纯函数 `override_warnings(policy) -> list[str]`：找出 `allow` 中会产生写入、执行或联网效果、而当前 `mode` 或 `sandbox` 默认不允许的工具，例如 `sandbox: read-only` 却 `allow: ["write_file"]`。
2. 在以下三处显示这些警告：
   - CLI：`python run.py doctor`（如果没有这个命令，就放在 `config` 校验或启动日志里）；
   - 桌面：Qt“模型与配置”页的策略区，用警告色标签显示，并说明可能的后果；
   - 加载 Agent bundle 时（`revon/agent_templates.py`）。
3. 警告文案举例：“`allow` 中的 write_file 会越过只读模式；如果这不是你的本意，请移除。”

**验收**：单测覆盖“只读模式加 write 类 allow”产生警告、正常配置不产生警告；桌面配置页能看到警告。

---

## P2　仓库根目录整洁度

**现状（已核实）**：根目录 143 个条目，其中 83 个是 `.codex-*.log`。它们已被 git 忽略、不在仓库里，但会让本地目录和 Agent 浏览时显得很乱。另外，根目录还提交了不少过程文档，例如 `plan.md`、`plan-v4.md`、`briefs-round2.md`、`briefs-round3.md`、`roundtable.md`、`iteration.md`、`relay-tasks.md`、`design-qa.md`、`contract.md`。

**修改**：
1. 让 Codex 的过程日志不再写到仓库根目录，改写到 `.codex/logs/`（已被忽略）或系统临时目录。查一下这些日志是哪个脚本或配置写出来的，从源头改。
2. 把现有的 `.codex-*.log` 移到 `.codex/logs/`。只移动，不删除。
3. 把上面这些过程文档用 `git mv` 移到 `docs/history/`，并全文搜索引用（README、docs、脚本），同步更新路径。`contract.md` 如果仍被代码或测试引用，就保留在根目录。
4. 在 README 的“项目结构”一节补充一张简短的目录说明表。

**验收**：根目录条目明显减少（不算被忽略的文件，目标 40 个以内）；全仓库没有指向已移动文件的失效引用；selftest 通过。

---

## P2　明确各个入口的支持级别（只改文档）

六个入口（CLI、Qt、Tk、VS Code、微信、Android）维护压力大。在 README 的入口表里加一列“支持级别”：

- **主力**：CLI、Qt 桌面（包括今天合入的 macOS/Linux 支持）。
- **实验性**：Tk + Coding Mode、VS Code 扩展、微信通道、Android。

同时说明实验性入口不保证每次发布都完整测试。不删代码。

---

## 暂不处理（记录在 README 的路线图里即可）

- HTTP 连接池（`revon/http_transport.py`）：可以按主机复用 `http.client` 连接，但会牵动超时和重试逻辑，单独立项。
- 能力包签名校验（`capability.install`）。
- 记忆的向量检索。
- 自研 HTTP、SSE、协议转换的正确性：后续用录制的真实响应做回放测试来覆盖，单独立项。

## 建议顺序

P0 → P1（README）→ P1（allow 警告）→ P2（根目录）→ P2（支持级别）。每完成一项就单独提交，P0 优先合入。
