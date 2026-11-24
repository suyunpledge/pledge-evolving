# Keystone Office 设计稿：Agent 自带的本地办公套件

日期：2026-11-24　状态：**基础编辑已接入 Work，以下为完整设计目标**
界面样稿：[`design/office-suite/index.html`](../design/office-suite/index.html)（`design-mockup` 预览服务，`/office-suite/`）　
办公编辑器和 AI 编辑工具均内置于 Work，共用本地文件模型。腾讯会议 MCP 独立负责会议服务，用户可按需连接。

当前版本支持段落与样式、单元格与常用公式、幻灯片标题和正文、本地保存、AI 编辑建议及按作者撤销。可从 docx / xlsx / pptx 导入可编辑内容并导出新副本。图表、复杂排版和高级格式的保真往返仍是后续目标。

---

## 1. 产品定义

Keystone Office 是 Work Mode 里的一个本地办公空间，包含**文档、表格、演示**三种编辑器。

- **用户**像用 WPS / Office 一样直接编辑：打字、改格式、填表、拖动幻灯片元素。
- **AI** 不生成文件再交给用户，而是走**内部路径**：直接调用文档模型的编辑操作，与用户的键盘输入进入同一条操作总线。用户能实时看到 AI 在哪里改、改了什么，可以逐条接受、拒绝或撤销。
- 所有数据保存在本机工作区，可以导入导出 docx / xlsx / pptx / pdf / md / csv。

一句话：**同一份文档，人和 AI 是两个作者，共用一套编辑操作和一份历史。**

### 1.1 核心体验（验收用的用户故事）

1. 用户在文档里选中一段，输入"改得正式一点"。AI 的修改以**建议**形式出现在原位（删除线 + 新文字），用户按 `Tab` 接受、`Esc` 拒绝。
2. 用户说"按这份销售表做一份 8 页季度汇报"。演示编辑器里新建文件，幻灯片一页页出现，AI 光标标注"Keystone 正在编辑第 3 页"；用户可以同时修改已完成的第 1 页。
3. 用户在表格里写到一半，让 AI"把 C 列按地区汇总到新工作表并画柱状图"。AI 新建工作表、写入 `SUMIF` 公式、插入图表；公式由本地引擎实时计算，数值立即可见。
4. 用户点"撤销 AI 的上一次修改"，只回退 AI 那一批操作，用户之后自己的修改保留。
5. 打开一份别人发来的 docx，编辑后导出 docx 发回；导入时提示哪些元素无法编辑（已原样保留为只读块）。

### 1.2 非目标（第一版不做）

多人在线协作与云同步；宏与 VBA；邮件合并；完整排版级分页（页眉页脚第一版只做简单形式）；Excel 全部函数（见 §6.2）；旧二进制格式 `.doc/.xls/.ppt`。

---

## 2. 总体架构

```
┌───────────────────────── Qt 桌面（Work Mode）─────────────────────────┐
│  OfficePage.qml                                                         │
│  ┌──────────────┐ ┌─────────────────────────────────┐ ┌─────────────┐ │
│  │ 文件与模板    │ │ WebEngineView：编辑器（本地 JS） │ │ AI 侧栏      │ │
│  │ (QML)        │ │  docs / sheets / slides 视图     │ │ (QML，复用   │ │
│  └──────────────┘ └──────────────┬──────────────────┘ │  对话组件)   │ │
│                                  │ QWebChannel          └──────┬──────┘ │
└──────────────────────────────────┼─────────────────────────────┼────────┘
                                   ▼                             ▼
┌──────────────────────── OfficeService（Python，宿主）─────────────────────┐
│ 会话表：doc_id → DocumentSession                                          │
│   ├─ Sequencer：唯一的操作排序点（版本号、变基、冲突裁决）               │
│   ├─ Model：文档 / 表格 / 演示的规范模型（内存）                          │
│   ├─ FormulaEngine：依赖图 + 增量重算（表格）                             │
│   ├─ Journal：追加写操作日志 + 定期快照（崩溃恢复、版本历史）             │
│   └─ Presence：谁在哪里（用户光标、AI 光标、AI 锁定区域）                 │
└───────────▲───────────────────────────────▲──────────────────────────────┘
            │ 用户操作（来自编辑器）         │ AI 操作（来自工具）
            │                               │
            │                ┌──────────────┴──────────────┐
            │                │ keystone/office_tools.py     │
            │                │ office_open / query / apply  │
            │                │ / create / export / render   │
            │                │ → Policy、审批、会话记录      │
            │                └──────────────▲──────────────┘
            │                               │
            │                         Agent Loop（内核）
```

关键原则：

1. **模型在 Python，编辑器只是视图。** 规范模型、排序、公式计算、历史都在 `keystone/office/`（标准库实现）。这样 AI 在不打开界面时也能编辑（CLI、微信、定时任务），界面打开时看到的是同一份实时数据。
2. **一条操作总线。** 用户按键和 AI 工具调用都变成同一种 `Op`，经过同一个 Sequencer。没有"AI 改文件、界面再重新加载"这回事。
3. **编辑器跑在已有的 QtWebEngine 里。** `BrowserPane.qml` 已经引入 QtWebEngine，富文本编辑、虚拟滚动表格、SVG 幻灯片在 Web 技术栈上成熟得多。编辑器资源随应用打包、完全离线、独立的 `WebEngineProfile`，不加载任何远程内容。
4. **复用现有内核能力**：Policy / 审批卡片、SecretScope、artifacts、checkpoint、会话记录、子代理，都不另起一套。

### 2.1 为什么不选其他方案

| 方案 | 优点 | 否决原因 |
|---|---|---|
| 纯 QML 原生编辑器 | 无 Web 层 | QML 富文本（`TextArea` / `QTextDocument`）做不好列表嵌套、表格、批注、建议模式；工作量最大 |
| 整套引入现成 Web 办公套件（如 Univer 一类开源项目） | 起步快、功能多 | 模型和命令系统在 JS 里，AI 路径只能经过界面进程，CLI/微信无法编辑；体积和许可需单独评估。**列为 M0 技术验证的对照项**，不作为默认方案 |
| 以 docx/xlsx/pptx 文件本身为存储 | 与 Office 直接互通 | 每次编辑都要改 XML，无法做细粒度历史、作者归属和实时同步 |

---

## 3. 文档模型

### 3.1 通用约定

- 每个可寻址元素都有**稳定 ID**（`b_7Kq2`），插入、移动不会改变已有 ID。AI 与 UI 都按 ID 定位，不按"第几段""查找文字"定位。
- 文本内容用 **runs**：`[{text, marks:{bold, italic, underline, color, link, code, comment_ids}}]`。
- 每个元素记录 `author`（`user` / `ai:<task_id>`）和 `rev`（最后修改的版本号），用于归属标记和冲突检测。

### 3.2 文档（Docs）

```
Document { id, title, page:{size, margins, orientation}, styles{}, blocks:[Block] }
Block =
  heading{level 1-6, runs} | paragraph{runs, align, indent, style}
| list{ordered, items:[{runs, level, children?}]}
| table{cols:[width], rows:[[Cell{blocks}]], header_rows, merges}
| image{asset_id, width, alt, caption} | chart{ref: sheet_id/chart_id, snapshot}
| page_break | toc | divider | callout{tone, blocks}
| opaque{source:"docx", xml_ref, preview}      ← 导入时无法编辑的内容，原样保留
Comment { id, anchor:{block, from, to}, author, text, replies, resolved }
Suggestion { id, author, ops:[Op], anchor, state: open|accepted|rejected }
```

### 3.3 表格（Sheets）

```
Workbook { id, sheets:[Sheet], names{}, styles{} }
Sheet { id, name, size:{rows, cols}, cells: sparse{ "B7": Cell }, col_widths, row_heights,
        merges, freeze, filters, conditional_formats, charts:[Chart], data_validations }
Cell { v: number|string|bool|null|error, f?: "=SUMIF(...)", fmt?: "#,##0.00", style? }
Chart { id, type: bar|line|pie|scatter|area, ranges, title, anchor, options }
```

- `f` 存公式原文，`v` 是引擎算出的值（只读，不由 AI 或 UI 直接写入）。
- 单元格存储稀疏；行列插入删除通过操作重写受影响的引用（与 Excel 行为一致）。

### 3.4 演示（Slides）

```
Deck { id, title, size:{w:1280,h:720}, theme:Theme, layouts:[Layout], slides:[Slide] }
Slide { id, layout_id, background, shapes:[Shape], notes:runs, transition? }
Shape = text{box, runs-blocks, autofit} | image{box, asset_id, crop} | shape{kind, box, fill, line, text?}
      | table{box, …同文档表格} | chart{box, ref} | group{shapes}
box = {x, y, w, h, rotate}（像素，基于 1280×720 画布）
Theme { colors{primary, accent1-6, bg, text}, fonts{heading, body}, spacing }
```

- 版式（Layout）定义占位区；AI 优先用"版式 + 占位内容"生成页面，而不是自由坐标，这样页面整齐且主题可一键替换。

### 3.5 文件存储

- 工作区下的 `Office/`（可配置）。每个文档一个文件：`.kdoc` / `.ksheet` / `.kslides`。
- 文件是 zip 包：`model.json`（最新快照）、`journal/`（操作日志分段）、`assets/`（图片，SHA-256 命名去重）、`meta.json`（作者、版本、导入来源与保真报告）。
- 写入策略：编辑时操作先追加写入日志（fsync 批量，100ms 内落盘）；每 500 个操作或关闭时生成快照并原子替换。异常退出后用"快照 + 日志尾"恢复。
- 同时保留 checkpoint：AI 每次大批量修改（一个任务事务）前，触发一次 `CheckpointStore.snapshot`，作为日志之外的第二道保险。

---

## 4. 操作（Op）与操作总线

### 4.1 Op 格式

```json
{"op": "text.insert", "at": {"block": "b_7Kq2", "offset": 18}, "text": "15 个工作日", "marks": {}}
{"op": "text.delete", "at": {"block": "b_7Kq2", "offset": 12}, "len": 4}
{"op": "block.insert", "after": "b_7Kq2", "block": {"type": "paragraph", "runs": [...]}}
{"op": "cell.set", "sheet": "s_1", "ref": "B7", "f": "=B5-B6"}
{"op": "range.set", "sheet": "s_1", "ref": "A2:C40", "values": [[...]]}
{"op": "shape.update", "slide": "sl_3", "shape": "sh_2", "box": {"x": 80, "y": 120}}
{"op": "slide.insert", "after": "sl_3", "layout": "title_bullets", "content": {...}}
```

完整操作集按三种编辑器分组定义在 `keystone/office/ops/`，每个 Op 有：参数校验、`apply(model)`、`invert(model)`（用于撤销）、`transform(other)`（用于变更基）。

### 4.2 事务

多个 Op 组成一个 **Transaction**：`{tx_id, author, base_version, ops, label, mode}`。

- 用户的一次连续输入（停顿 800ms 内）合并为一个事务，对应一次撤销。
- AI 的一次工具调用是一个事务；一个 AI 任务的所有事务挂在同一个 `task_id` 下，可整体撤销。
- `mode`：`direct`（直接生效）或 `suggest`（作为建议，见 §5.2）。

### 4.3 排序与冲突

单机、单进程，所以不需要分布式协同算法，只需要一个**中心排序点**：

1. 每个文档会话有一个单调递增的 `version`。所有事务进入 Sequencer 的队列，按到达顺序应用。
2. 事务带 `base_version`。如果期间有其他事务落地：
   - **不同元素**：直接应用（大多数情况）。
   - **同一文本块**：对文本插入、删除做偏移变换（只需处理字符串内插入/删除这一种情况）。
   - **同一单元格 / 同一形状属性**：后到者覆盖，但若后到者是 AI 而先到者是用户，则 AI 事务**被拒绝**，返回冲突信息让 AI 重新读取后再改。
3. **用户优先**：用户光标所在的块，AI 的直接修改自动降级为建议，避免改掉用户正在打的字。
4. 编辑器本地先乐观应用用户操作（保证打字零延迟），收到 Sequencer 的确认或变换后的版本再对齐。

### 4.4 撤销与历史

- **按作者撤销**：`Ctrl+Z` 撤销当前用户自己的上一个事务；"撤销 AI 修改"撤销 AI 的上一个事务或整个任务。被撤销的事务与之后的事务冲突时，只回退不冲突的部分，并提示哪些地方已被后续修改覆盖。
- **版本历史**：时间线上按事务显示（作者、标签、时间），可预览任意版本、恢复、或命名保存（"发给客户的版本"）。
- **归属标记**：可开启"显示 AI 修改"，AI 写入的文本、单元格、形状带淡色底纹和作者提示。

---

## 5. AI 的内部路径

### 5.1 工具（`keystone/office_tools.py`）

宿主 `trusted=True` 注册，名字加入 `BUILTIN_TOOL_NAMES`。默认工具面只暴露前三个，其余 deferred。

| 工具 | 读/写 | 作用 |
|---|---|---|
| `office_query` | 读 | 读取打开的文档：大纲、指定块/区域、当前用户选区、批注、版本号。大结果转 artifacts |
| `office_apply` | 写 | 提交一个事务（ops + base_version + mode + label）。返回新版本号、应用结果、冲突、表格重算后的值变化 |
| `office_create` | 写 | 新建文档/表格/演示，可指定模板或从规格一次生成初稿 |
| `office_open` | 读 | 打开工作区里的已有 Office 文件或导入 docx/xlsx/pptx，返回 doc_id 和保真报告 |
| `office_export` | 写 | 导出为 docx / xlsx / pptx / pdf / md / csv 到工作区 |
| `office_render` | 读 | 渲染指定页面/幻灯片为 PNG（存 artifacts），并返回排版诊断（溢出、重叠、对比度不足、超出页面） |
| `office_lint` | 读 | 只返回排版诊断和公式诊断，不出图，供不支持视觉的模型使用 |

AI 拿到的上下文永远包括：**用户当前选区**、**当前版本号**、**用户正在编辑的块**（AI 应避开）。

### 5.2 AI 写入模式（按文档设置，可在 AI 侧栏切换）

| 模式 | 行为 | 默认用于 |
|---|---|---|
| 只读 | AI 只能 `office_query` / `office_render` | 打开的外部文件 |
| 建议 | AI 的修改全部以建议出现，用户逐条接受/拒绝 | 已有文档、合同、正式文件 |
| 直接编辑 | 立即生效，带 AI 归属标记，可按任务撤销 | AI 新建的文档、空白文档 |

这对应 Keystone 现有权限模型：模式不授予权限。`office_apply` 仍是写工具，经过 Policy 判定；"直接编辑"相当于该文档范围内的写许可，由用户在侧栏明确授予，记录在会话里，可随时收回。

### 5.3 一次 AI 编辑的流程

```
用户在 AI 侧栏输入（带选区） → Agent Loop
  → office_query(doc, selection, around=2)         读当前内容与版本 v41
  → office_apply(doc, base=v41, mode=suggest, ops=[…], label="改为正式语气")
       Sequencer：v41→v43 之间用户改过别的段落 → 无冲突，应用为 v44
       编辑器收到广播 → 原位显示建议 + AI 光标闪过
  → （表格/演示）office_lint 或 office_render 自查 → 有溢出则再提交一个修正事务
  → 回复用户："已提出 3 处修改，见文中标记。"
```

### 5.4 让 AI 高效的设计（沿用 model3d 的经验）

- **结构化而非坐标**：演示用版式和占位符；文档用块；表格用区域写入 `range.set` 一次写几百格。
- **一次调用拿回证据**：`office_apply` 的返回直接带溢出、公式错误、受影响单元格的新值；第一行是结论（`OK v44` 或 `CONFLICT b_7Kq2 changed by user at v43`）。
- **技能正文** `keystone/bundled/office/SKILL.md`：Op 速查、常见错误和修法、版式清单。

---

## 6. 引擎

### 6.1 文档排版

排版只在编辑器（Web）里做：连续滚动视图为默认，"页面视图"按纸张尺寸分页显示。排版诊断（某块超出页面、表格过宽）由编辑器计算后通过 QWebChannel 回报给 OfficeService，供 `office_lint` 使用。无界面时 `office_lint` 只做结构诊断，并注明"未排版"。

### 6.2 表格公式引擎（Python，标准库）

- 解析器：Excel 公式语法（含跨表引用、绝对/相对引用、区域、命名区域）→ AST。
- 依赖图：单元格级正反向依赖；修改后只重算受影响子图；检测循环引用。
- 函数：第一批约 80 个常用函数（数学、统计、逻辑、文本、日期、`VLOOKUP/XLOOKUP/INDEX/MATCH`、`SUMIF(S)/COUNTIF(S)/AVERAGEIF(S)`）。不支持的函数显示 `#NAME?` 并在 `office_lint` 中列出。
- 性能目标：10 万个非空单元格的表，单格修改后重算 < 50ms（增量）；全量重算 < 2s。
- 导出 xlsx 时同时写公式和计算值，Excel/WPS 打开后可直接显示。

### 6.3 演示渲染

SVG 渲染，主题变量驱动。文本自适应规则：先缩小行距，再缩小字号到下限，仍放不下则报告溢出（不无限缩小）。缩略图由编辑器离屏渲染生成；无界面时 `office_render` 返回"需要打开界面"，`office_lint` 用字符宽度估算溢出。

---

## 7. 界面设计（Qt Work Mode）

详见样稿 `design/office-suite/index.html`。以下是结构说明。

### 7.1 入口

- Work Mode 左侧导航新增"办公"。点进去是 Office 主页：最近文件、新建（文档/表格/演示）、模板、从文件导入。
- 对话里 AI 创建或修改的文档以**文档卡片**出现，点击直接在办公空间打开并定位到修改处。

### 7.2 编辑页布局

```
┌ 顶栏：文件名 · 保存状态 · 版本历史 · 导出 · 分享(禁用，不在本版) ─────────────┐
├ 工具栏（按类型变化，单行；更多选项收进下拉） ──────────────────────────────────┤
│ ┌ 大纲/工作表/幻灯片列表 ┐ ┌──────── 编辑区 ────────┐ ┌──── AI 侧栏 ────┐ │
│ │ 可折叠，默认 220px      │ │                         │ │ 写入模式：建议 ▾ │ │
│ │                        │ │  AI 光标与建议标记        │ │ 上下文：选区 3 段 │ │
│ │                        │ │                         │ │ 对话 / 任务进度   │ │
│ │                        │ │                         │ │ 待处理建议 (3)    │ │
│ └────────────────────────┘ └─────────────────────────┘ └ 输入框 ──────────┘ │
└ 状态栏：字数/选区统计/求和 · 本地已保存 v44 · AI 正在编辑第 3 页 ──────────────┘
```

- AI 侧栏可收起（`Ctrl+J`），宽 360px；收起后在编辑区右下角保留一个浮动输入入口。
- 选中内容后出现**浮动 AI 条**：改写、扩写、缩写、翻译、解释、自定义指令。
- 建议的交互：原位显示删除线 + 新文本；悬停出现"接受 / 拒绝 / 查看理由"；侧栏"待处理建议"列表可批量接受。键盘：`Alt+]` / `Alt+[` 在建议间跳转，`Tab` 接受，`Esc` 拒绝（仅在焦点位于建议上时）。
- AI 光标：带"Keystone"标签的彩色光标，使用 `Theme.ember` 区分用户的 `Theme.accent`；AI 正在修改的块左侧显示细色条和"编辑中"文字（状态不只靠颜色）。

### 7.3 三种编辑器

- **文档**：段落样式下拉、粗斜体下划线、列表、对齐、插入（表格、图片、图表、分页、目录）、批注。左栏为标题大纲。
- **表格**：公式栏（显示 `f`，可编辑）、单元格格式、排序筛选、冻结、插入图表；左栏为工作表列表；状态栏显示选区求和/平均/计数。
- **演示**：左栏缩略图（拖动排序）、中间画布（拖动、缩放、对齐参考线）、右侧可切换"属性"与"AI"；版式和主题选择器；底部备注栏；放映模式全屏。

### 7.4 设计规范

沿用 `Theme.qml` 与 Visual System 2.0：Work 舒适密度、深色玻璃表面、焦点环 `focusRing` 2px、所有操作键盘可达、状态不只靠颜色、正文对比度 ≥ 4.5。编辑画布（纸张、幻灯片）本身是浅色，模拟真实输出效果；画布外的界面保持深色。WebEngine 内的编辑器 CSS 变量由 `Theme.qml` 生成，保证单一来源。

---

## 8. 导入导出

以下是导入导出层的目标覆盖范围。

| 格式 | 导入 | 导出 |
|---|---|---|
| docx | 标题、段落、列表、表格、图片、批注、修订（转为建议）；其余转 `opaque` 只读块 | 全部模型元素；`opaque` 原样写回 |
| xlsx | 值、公式、格式、合并、冻结、条件格式（常见类型）、图表（常见类型） | 同上，公式 + 计算值 |
| pptx | 版式占位、文本、图片、形状、表格、备注；复杂动画丢弃并报告 | 全部模型元素 |
| pdf | 不导入 | 由编辑器打印渲染导出 |
| md / csv | 导入导出（md ↔ 文档，csv ↔ 单个工作表） | |

导入后生成**保真报告**（保存在 `meta.json`，打开时顶部提示一次）：哪些元素可编辑、哪些保留为只读、哪些丢弃。

---

## 9. 安全

在上一份方案的 §7（公式注入、宏、恶意压缩包、XML 实体、外部链接、提示注入、密钥）基础上，增加：

- **编辑器隔离**：独立 `WebEngineProfile`，禁止网络请求（请求拦截器拒绝 `file:` 以外和 `qrc:` 以外的所有 URL）、禁用 JS 跨域、禁止弹窗和下载；QWebChannel 只暴露 `OfficeBridge` 一个对象，且只接受 Op 提交与查询，不暴露文件系统。
- **文档内容不可信**：文档文字进入 AI 上下文时标 untrusted。导入文档中的"给 AI 的指令"不会被执行；AI 侧栏上下文区显示"包含来自外部文件的内容"。
- **外链与图片**：文档里的远程图片导入时不自动下载，显示占位；用户点击后经审批下载并存入 `assets/`。
- **AI 写入审计**：每个 AI 事务记录在会话日志（task_id、工具调用、操作摘要），版本历史可追溯到对话。

---

## 10. 模块与文件规划

```
keystone/office/
  model/        document.py  sheet.py  deck.py  ids.py  assets.py
  ops/          text.py  block.py  cell.py  shape.py  slide.py  transform.py
  session.py    DocumentSession：Sequencer、Presence、订阅
  journal.py    日志 + 快照 + 恢复
  formula/      lexer.py  parser.py  graph.py  functions/*.py
  ooxml/        package.py  docx.py  xlsx.py  pptx.py（导入导出）
  lint.py       结构与估算诊断
keystone/office_tools.py
keystone/bundled/office/SKILL.md
keystone-gui/qt_frontend/
  office_service.py   宿主：会话表、与 gateway/Policy 连接
  office_bridge.py    QWebChannel 对象（Op 提交、订阅、诊断回报）
  qml/OfficePage.qml  主页与编辑页框架、AI 侧栏
  office_web/         编辑器前端（打包后离线资源，不依赖 CDN）
    docs/  sheets/  slides/  shared/(op 客户端、主题变量、建议渲染)
```

---

## 11. 里程碑

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| **M0 技术验证**（1–2 周） | 文档模型最小集 + Sequencer + QWebChannel + 一个最简富文本编辑器；AI 通过 `office_apply` 插入段落，界面实时出现；同时评估"整套引入现成 Web 套件"的对照方案 | 用户连续打字时 AI 并发修改另一段，无丢字、无闪烁；端到端延迟 < 50ms |
| **M1 文档** | 完整文档模型与编辑器、建议模式、按作者撤销、版本历史、docx 导入导出、AI 侧栏 | §1.1 故事 1、4、5 通过 |
| **M2 表格** | 表格模型、公式引擎（首批函数）、虚拟滚动网格、图表、xlsx 导入导出 | 故事 3 通过；10 万格重算性能达标 |
| **M3 演示** | 演示模型、版式与主题、SVG 画布、缩略图、`office_render`/`office_lint`、pptx 导入导出、放映 | 故事 2 通过；AI 生成的 8 页汇报零溢出 |
| **M4 打磨** | 模板库、跨文档引用（表格图表嵌入文档/演示并联动）、PDF 导出、无障碍与人因评审 | Visual System 与人因清单通过 |

## 12. 测试

- **模型与操作**：每个 Op 的 `apply → invert` 往返；`transform` 的性质测试（任意两个并发操作，两种顺序结果一致）。
- **并发**：模拟用户每 30ms 一次按键 + AI 每 200ms 一个事务，跑 10 分钟，校验最终模型一致、无丢失。
- **崩溃恢复**：在写日志的任意位置强行终止，重启后能恢复到最后一个完整事务。
- **公式**：与 openpyxl 读回 + 预先由 Excel 计算的样本表比对数值。
- **导入导出**：样本集（含 WPS 生成的文件）往返；用 python-docx / openpyxl / python-pptx 作为独立评分器；人工抽查 Office 与 WPS 打开无修复提示。
- **AI 端到端**：照 `scripts/verify_model3d_ab.py` 写 `verify_office_ab.py`，用真实模型跑任务清单，产物由独立评分器评分，记录步数、耗时、成本。
- **界面**：Qt 截图对比、键盘可达性测试（沿用 `test_human_factors.py` 的方法）。

## 13. 待定问题

1. **编辑器前端是自研还是基于开源编辑内核**：富文本建议基于 ProseMirror 一类成熟内核（只用其编辑与渲染，模型以 Python 侧为准）；表格网格与幻灯片画布自研。M0 结束时定。
2. **存储位置**：默认放工作区 `Office/`，还是放 Keystone home 由用户选择是否同步到工作区？建议放工作区，跟随项目备份和 Git。
3. **AI 默认写入模式**：新建文档默认"直接编辑"、打开已有文档默认"建议"——是否需要全局可配置？
4. **Android 与 VS Code**：第一版只在 Qt 桌面提供编辑器；其他入口可通过工具让 AI 编辑，用户只能导出查看。是否需要移动端只读预览？
5. **打包体积**：编辑器前端资源预计 3–6 MB（压缩后），QtWebEngine 已随应用分发，不新增运行时。
