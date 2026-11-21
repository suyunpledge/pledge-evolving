# 3D 建模能力（model3d）

目标：用户要一个 3D 模型（"低多边形椅子""12 齿齿轮""能 3D 打印的手机支架"）时，Agent 用更少的回合交出正确、能用的文件。
沿用 2026-11-09 的结论（`keystone/vendors.py`：`file_stats` / `file_export`）：**工具层介入胜过提示层**。模型不再手写顶点或 STL 文本，而是描述"要什么"，一次工具调用拿回文件、回读证明、实测尺寸和拓扑检查。

## 接入路线（为什么是"内核工具 + 技能插件"两层）

| 层 | 位置 | 作用 |
|---|---|---|
| 几何引擎 | `keystone/model3d/`（纯标准库） | 规格 → 网格 → 校验 → 导出 → 预览 |
| Agent 工具 | `keystone/model3d_tools.py`，在 `build_builtin_registry` 注册 | `model3d_build`（写）、`model3d_inspect`（读，延迟加载） |
| 技能（内核） | `keystone/bundled/model3d/SKILL.md`，`capability.BUNDLED_ROOT` | 进能力索引；`skill_list {"name":"model3d"}` 载入正文 |
| 技能（桌面插件） | `keystone-gui/catalog.builtin.json` 条目 `skill-model3d` | 安装→知悉→启用→授予 `skill.read` 后出现 `keystone_model3d_guide` |

- 桌面插件体系（`keystone-plugin.json`）是**声明式**的：不执行插件 Python（`executes_code` 插件不向 Agent 开放），工具只能别名宿主文件操作或返回技能正文。几何计算放不进插件，只能是宿主代码。
- 内核工具必须由宿主 `trusted=True` 注册，否则 effect 为 UNKNOWN、不可调用。`model3d_build` 声明为写工具（`read_only=False`，参数名 `path`），因此自动经过：Policy 判定、工作区写锁、`execution.py` 写前检查点快照、写后反馈。附带产物（其它格式、预览 PNG）只能落在同目录同名下。无网络、无子进程。
- 两个工具名加入 `BUILTIN_TOOL_NAMES`，插件不能遮蔽。
- `model3d_inspect` 设为 deferred（`tool_search "inspect stl"` 可找到），默认工具面只多 `model3d_build` 一个（约 2.4k 字符声明）。
- 更重的引擎（Blender CLI / OpenSCAD / trimesh）没有接入：纯 Python 已覆盖需求，接入时应走受控工具 + 权限，而不是 `shell_exec`。

## 规格（spec）

```json
{"units": "mm", "model": {"op": "difference", "children": [
  {"op": "box", "size": [40, 40, 10]},
  {"op": "cylinder", "d": 20, "h": 12, "translate": [20, 20, -1]}]}}
```

- 体素：`box` `cylinder`（含圆锥 r1/r2）`sphere` `torus` `polyhedron`
- 二维轮廓 → 实体：`extrude`（`polygon` `rect`（圆角）`circle` `regular_polygon` `gear`（渐开线；`teeth` + `module`/`pitch_diameter`/`outer_diameter`），`holes` 打通孔）、`revolve`（(r,z) 半平面）
- 布尔：`union` `difference` `intersection`（BSP，csg.js 算法；迭代实现，无递归上限；接缝 T 形顶点修补后重三角化，结果边流形）
- 阵列与对称：`linear_array` `polar_array` `mirror_copy`
- 任意节点：`translate` `rotate`（度，X→Y→Z）`scale` `mirror` `name`
- 错误信息给出节点路径、该 op 允许的键和示例，例如 `model.children[1].op: unknown op 'cylnder'; use one of [...]`

## 同一结果里的校验

尺寸/包围盒（原单位 + 毫米）、三角形数、壳数、水密（开放边/非流形边）、朝向冲突、法线是否内翻、退化面、通孔数（亏格）、体积与表面积；打印用途额外检查是否落在 z=0、>45° 悬垂比例、尺寸是否像单位写错。`expect`（`size` `through_holes` `shells` `volume` `max_size`）把用户需求逐条判成 ok/FAIL；不通过时结果第一行即 `CHECKS FAILED`。
导出后从磁盘回读比对三角形数（STL 二进制/ASCII、OBJ：毫米；glTF/GLB：米、Y 轴向上）。预览：`<名>.preview.png`（等轴/前/顶/右四视图，内翻面洋红色）+ 文本模型可读的 ASCII 顶视/前视高度图（通孔显示为空白）。

## 验证

- `python -m unittest keystone.test_model3d`（几何、布尔、导出有效性、校验信息、工具权限、技能）
- `keystone-gui/test_plugin_model3d.py`（插件安装→知悉→启用→授权→加载正文→禁用撤销）
- `python scripts/verify_model3d_ab.py`：离线脚本模型演示（先把孔放在角上，工具检查判 `through_holes want 1, got 0 [FAIL]`，第二次修正后通过）
- `python scripts/verify_model3d_ab.py --live --tasks plate,gear,stand`：真实模型前后对照，产物由独立评分器从磁盘重新解析评分
