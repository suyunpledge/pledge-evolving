# Step1 编程模式 · 三席联审材料包

## 一、交付范围（四个文件）

| 文件 | 变更类型 | 行数 |
| --- | --- | --- |
| `forge/tools.py` | 新增 `edit_file` / `apply_patch` / `read_range` / `file_outline` | +117 |
| `forge/cli.py` | `--coding` 开关、`_compose` 叠加编码层、防覆盖、`tools.expose` 接线 | +19 |
| `forge/loop.py` | checkpoint 触发条件 `WRITE_TOOLS` 表化 | 6 |
| `forge/selftest.py` | 新增 `test_coding_mode` 套件（17 条断言）并注册 | +69 |
| `bundles/modes/coding.json` + `forge/bundles/modes/coding.json` | **新增** 编码模式补丁层（两份同步） | 新 |

## 二、关键 diff（`edit_file` / `apply_patch` 实现要点）

### edit_file

```
路径：werkzeug/tools.py:edit_file  （reg.tool 装饰器）
只读：read_only=False
参数：path / old / new / replace_all / start_line / end_line
行为：精确锚点替换（行范围或字符串锚点）；锚点模糊时拒绝；
      replace_all=True 时批量替换
边界：start_line/end_line 越界拒绝；文件不存在拒绝
```

### apply_patch（原子）

```
路径：werkzeug/tools.py:apply_pool
只读：read_only=False
参数：patches（每项含 path / old / new / replace_all 可选）
行为：第一阶段 逐块校验（锚点存在性 + 歧义检查）→ 第二阶段 统一落盘
      → 任一 block 失败，所有文件原封不动（零残留）
参数接受：patches（JSON array）或 edits（别名）
```

### read_range / file_outline

```
read_range：1-indexed 行范围读取， cheaper than 整文件
file_outline：正则提取 def/class 签名（200 个符号上限）
两者 read_only 默认 True
```

## 三、设计决定（需要三席判断）

### 3.1 编码 bundle 放 `modes/` 子目录而非顶层

**理由**：`_compose` 默认 `glob("*.json")` 会把顶层所有 json 当默认层加载。放顶层 = 编码模式永远开启，而 `d2:cli-default-bundle-resolves` 自检正盯着默认必须是 base。

**问题**：`modes/` 子目录的约定是否是正确方向？未来是否应该让 bundle 支持 `"include"` 字段显式声明依赖，而非靠目录结构隐含？

### 3.2 `shell_exec` 保留在编码工具面

**理由**：Step1 还没有 `run_tests`，此时若藏掉 `shell_exec`，编码模式会连命令都跑不了。

**问题**：`shell_exec` 的 `ask` 门控在无头模式下收敛为 deny，这合理吗？还是应该在 Step2 的 `run_tests` 就位后立即把它从 expose 表移除？

### 3.3 checkpoint 表化

**变更**：`loop.py` 两处硬编码 `{write_file, shell_exec}` 改为 `from .policy import WRITE_TOOLS`。

**问题**：`WRITE_TOOLS` 当前 = 5 项（write_file/edit_file/apply_patch/shell_exec/notebook_edit），checkpoint 快照成本是否需要更细粒度控制（如只对 `write_file` / `apply_patch` 快照，跳过 shell_exec）？

## 四、自检结果

```
selftest: 477/477 通过（含 17 条 coding:* 新断言）exit 0
doctor --coding: 7/7 通过；8 rows from layers ['base.json','coding.json']
dump-config --coding: mode=acceptEdits maxSteps=40 thinking=smart expose=13项
```

## 五、下步规划（三席确认后执行）

| 步 | 内容 | 预计风险 |
| --- | --- | --- |
| Step 2 | `run_tests` 白名单 + `--tools` CLI + 编码契约片段注入 `extra_system` | 中（shell_exec 门控需要判断） |
| Step 3 | `forge/cluster.py`（AgentRole + Cluster）+ 四内置编程角色 | 高（编排器设计） |
| Step 4 | `coding.mode` contrib suite（纯函数过闸门）+ 文档 | 低 |
