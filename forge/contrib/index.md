# 贡献模块索引

由 `forge/registry.py` 的一致性闸门维护。**入库** = 过闸门（静态禁令 + 动态加载 + 作者自带证据）；**适配入库** = 外壳由 legacy-script 适配层补，作者不变。

当前：**contributed 13 · healthy 13 · assertions 260 · 能力 33**，框架全量自检 **595/595**。

> **2026-09-15 复验闭环**：外部审计的两条 P0 已全部修复。闸门侧不再合成证据（无证据即隔离）；作者侧 Trae 重写两份模块（声明 `SELFTEST_CASES` + 补全契约入口 + 去 BOM + 工具名改 `server__tool` 双下划线），已重新过闸入库。`seam:server-prefixed-tools-round-trip` 从「已声明缺陷看守」变成**真实通过**——接缝是真修好了，不是被登记掉的。审计负对照（删空测试体仍入库）的同类洞已由 `smoke:judge-catches-no-work` 等断言常驻看守。

> **2026-09-17 沉思模式接入**：`thinking.py`（Trae rev.4.1 二修 + 集成方 v2.1/v2.2 验收微修：四阶段×五轮沉思引擎 + 三档模式）过闸入库。capability `thinking.mode` 由运行时**沉思套件席位**同轮消费——三档门控 off/smart/on（smart 判据 = 库函数 `looks_complex`，触发词或长文本）；启用后运行首尾两相（thinking/reflection）调用四钩与库函数 `converged`，计数落 `thinking.<hook>`；CLI `run --thinking` 可显式选档。「被真正调用」断言已进自检（460/460）。

## 入库（12）

| 模块 | 作者 | 行数 | 能力 |
| --- | --- | --- | --- |
| `heartbeat.py` | forge 参考实现 | 107 | `schedule.periodic`, `schedule.due-check` |
| `scheduler.py` | 阶跃龙虾（StepClaw） | 301 | `schedule.periodic/lease/gate` |
| `replay.py` | zcode | 749 | `session.replay/fork/diff` |
| `router.py` | zcode | 485 | `route.match/estimate/fallback` |
| `curator.py` | Hermes Agent | 587 | `knowledge.metabolize/review/ledger` |
| `teams.py` | WorkBuddy（CodeBuddy Code） | 485 | `team.deliver/address.at/tasks.advance/budget.split` |
| `compactor.py` | OpenCode | 236 | `context.should_compact/plan/merge_summary` |
| `hooks.py` | Hermes（改派自阶跃龙虾） | 425 | `hooks.authorize/dispatch/describe` |
| `catalog.py` | DeepSeek Harness（改派自 PI agent） | 305 | `models.catalog/chain` |
| `toolhost.py` | Trae（修复版 09-15） | 430+ | `tools.register_tools/authorize` |
| `mcp_bridge.py` | Trae（修复版 09-15，含 `server__tool` 改名） | 402+ | `mcp.plan/discover/health` |
| `thinking.py` | Trae（rev.4.1 二修；集成方 v2.1/v2.2 微修） | 676 | `thinking.mode` |
| `supervisor.py` | Forge runtime | 429 | `convergence/advisory` |

## 已隔离（0）

| 模块 | 作者 | 隔离原因 | 修法（已回传） |
| --- | --- | --- | --- |
| `toolhost.py` | Trae | 自检无逐条证据；另与 `mcp_bridge` 存在 tool-name grammar 自相矛盾 | 返回 `(name, ok, detail)` 行，或声明 `SELFTEST_CASES = <数量>` |
| `mcp_bridge.py` | Trae | 同上 | 同上；并把 `server/tool` 改为 `server__tool`（见 CONTRACT §1.6） |

## 已声明的接缝缺陷（不静默通过）

| 缺陷 ID | 内容 | 状态 |
| --- | --- | --- |
| `mcp.tool-name-grammar` | `mcp_bridge` 产出 `server/tool`，而 `register_tools` 拒绝 `/` → 带前缀的 MCP 工具在真实链路上会被全部丢弃 | 已回传作者；`selftest` 的接缝探针以「已声明缺陷」看守，出现**未声明**的断裂会直接 FAIL |

## 闸门实际判定

```
$ python run.py modules validate
api_version=1  contributed=12  healthy=12  assertions=240
  [ok  ] context.merge_summary  <- compactor
  …
  [ok  ] thinking.mode          <- thinking
  …
（无 [warn] / [FAIL]——2026-09-17 终态）
```

## 验证方式（集成者执行）

```bash
python run.py modules validate      # 1. 一致性闸门：静态禁令 + 动态加载 + 作者证据
python run.py modules selftests     #    展开每一条断言
python ../../.openclaw/tmp/agent-fw/verify_contrib.py   # 2. 集成者独立用例（52 项）
python ../../.openclaw/tmp/agent-fw/repro_audit.py      # 3. 复现外部审计的两条结论
python run.py selftest              # 4. 框架全量自检，含接缝探针
```

**贡献者的测试算参考，集成者的独立用例才算证据；两者都不算证据的，是「没有测试」。**

## 环境依赖（踩过的坑）

- **WorkBuddy CLI 强行走系统代理**：系统代理指向死端口时直接 502，`NO_PROXY=*` 无效。
- **Trae 交付的文件带 UTF-8 BOM**：registry 按 `utf-8-sig` 读，否则 `ast.parse` 第一行就失败。
- **各家自检输出形态不统一**：行级返回 / 打印 PASS-FAIL / 只返回失败列表 / 把整份报告塞进一条 detail —— 四种都要认，但**认不出证据时不能假装通过**。
- **模型网关必须常驻**：跑在临时后台会话里会随回合回收，Claude Code 随即 502。
- **审计遗留**：项目根目录有两个临时文件（`.openclaw_tmp_out.txt`、`.openclaw_tmp_probe.py`），删除需要人工批准，待清。
