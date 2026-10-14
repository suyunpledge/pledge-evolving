"""布局域回归测试：P0-L1 Agent 栏溢出 + pane 顺序 + 增量挂载 + M1 骨架守护。

背景（Phase 1 实测，1440x900 为 launch() 默认尺寸、1200x760 为测试夹具）：
  P0-L1  Agent 栏 11 个子控件中底部 5 个渲染在窗口下缘之外，用户看不到
         权限/复审/恢复/子Agent/取消/执行测试/任务角色/独立模型/模型回显。
         两层根因：
           (a) log_tabs 是唯一 expand，其后 8 个固定组仍按 side=top 顺序
               堆叠，窗口高度不足时 Tk 从序列尾部开始裁剪；
           (b) apply_layout 拿 panes.panes()（返回窗口路径名**字符串**）
               与控件对象比较，恒为 False —— 「已在集合则 continue」永不
               命中、「不在集合才 forget」对每个在装面板都成立，于是每次
               都全量 forget+add，且 insert 目标位计数恒为 0，三个面板
               依次插到队首，顺序反转成 agent|editor|project，fractions
               语义错位，agent 栏被压到 20%（1200px 下仅 236px），栏内
               模型组横向放不下，run_model 直接 unmapped。
         修复：固定组 side='bottom' 先占位；live 统一换算控件对象再比较，
               并在 forget 后重读（否则 len(live) 过期会让 insert 越界）。

  M1 骨架（顶部导航栏 + 全宽底部工具面板 + 多字段状态栏）：
         输出 Notebook 从 editor_frame 内的竖向 pane 上移到外层竖向
         Panedwindow，与三栏并列 → 底部面板跨三栏全宽；导航栏激活态
         经 StringVar trace 与模式下拉同源；状态栏四字段 +
         右侧瞬态消息（self.status 契约保留）。IDE 收起时底部面板
         让位（_sync_bottom_panel 二态切换，不走增量挂载路径）。

  M2 欢迎页（welcome.py）：
         未打开项目时中央区（editor_frame）显示欢迎页而非空的编辑器
         本体（_build_welcome 建视图、_sync_welcome 按 source 二态
         切换）；近期项目来自 state.value('recent')，点击行经
         on_recent 回调走既有 open_project。切换不新增 pane 挂载路径。

  M3 Explorer（explorer.py）：
         左栏重构为「48px 活动栏（文件/搜索/Git/运行/扩展/设置）+
         文件面板」——文件树 / Git 变更 / 搜索 / 文件操作移入新模块；
         app._build_project 只做挂载与契约转交（self.tree /
         self.changes 仍指向面板内 Treeview，self.search /
         self.diff_view / self.expanded 仍归 app），_project_scan
         的渲染循环委派给 ExplorerPanel.populate。Git porcelain 码
         归一化为 M/A/D/U/R/C/X，字母徽标 + 主题色 + 图例文字三重
         冗余；文件操作经 ProjectBoundary 校验。

  M4 编辑器增强（editor_area.py）：
         单标签页一个 EditorArea——行号槽（与正文同字体、共享滚动）
         + 当前行高亮 + 搜索替换（Ctrl+F/Esc）+ gutter 修改标记
         （缓冲区 vs document.original 行级 diff）+ 分屏只读参考
         镜像 + 另存为（文档改绑新路径）+ Python 语法高亮（按接口
         约定消费 gui_theme.setup_code_tags / highlight_python）。
         row['widget'] 契约不变（EditorArea.text 即该 Text）；
         标签页右侧新增分屏/更多图标按钮位。另存为写盘经
         boundary 校验与原子替换，与 Document._save 同规则。
         打包器超订坍缩第三例：tab_row 内 Notebook（含默认 80x24
         请求的 Text）超订腔体，后 pack 的 actions 帧恒得 1x1 且
         不映射——按钮位先 pack 抢占配额、Notebook 后 pack 吸收
         赤字（side 定视觉位置、pack 次序定让位优先级）。同族：
         M1 底部面板 height=6 小请求、editor_area 分屏重 pack。

  M5 底部面板四标签（bottom_panel.py）：
         可见标签差异/终端/测试/问题（示例图基线标签与次序），标签
         计数徽标取真实数据源——差异 = Git 变更文件数（Explorer
         变更清单行数）、测试 = 当前任务测试记录数、问题 = 当前任务
         失败记录数（TaskEvidence 只读投影）；无数据显示 0，不伪造。
         views 五键不变，'输出' 为 '测试' 别名（任务书 Output =
         构建输出/运行日志/测试结果/工具执行结果，与测试结果同页），
         write_output 既有调用点零改动。差异页增删行着色（+/-
         行按 git 语义上色，@@ hunk 头 accent）；文件选择与修改前后
         对比沿用 M3 已接线的 inspect_diff 链路。

本文件即上述修复与改造的守护，也是集成验收口径「P0-L1 两尺寸映射断言」
的落地。只测布局容器层，不触碰 Agent 栏内部控件语义（Codex 域）。
"""
import subprocess
import time
import unittest
from pathlib import Path
import tempfile
from unittest import mock
import tkinter as tk
import _tkinter

from gui_theme import C, apply_ttk_theme
from forge.development.state import ModeState
from development_mode.app import DevelopmentWorkbench
from development_mode import explorer
from development_mode.editor_area import EditorArea, save_document_as
from development_mode.explorer import ExplorerPanel
from development_mode.layout import PRESETS, StatusBar


class _WorkbenchFixture:
    """只借 WorkbenchTests 的真实 Tk 夹具与 step/pump 辅助，不带它的用例。

    直接继承 WorkbenchTests 会让 pytest 把父类全部用例重跑一遍
    （test_app 全量 90s+，直接超时），故用 mixin 只取夹具。

    ``open_project_on_start`` 默认 True（既有行为）；欢迎页等需要
    「未打开项目」初始态的用例置 False 即可，夹具其余部分不变。
    """

    open_project_on_start = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'repo'
        self.project.mkdir()
        (self.project / 'one.py').write_text('print(1)\n', encoding='utf-8')
        self.root = tk.Tk()
        self.root.geometry('1200x760')
        self.root.attributes('-alpha', 0)
        self.addCleanup(self.cleanup_tk)
        apply_ttk_theme(self.root)
        self.state = ModeState(self.base / 'home')
        self.state.switch('development')
        self.app = DevelopmentWorkbench(self.root, self.state)
        self.app.pack(fill='both', expand=True)
        if self.open_project_on_start:
            self.app.open_project(self.project)
            self.pump(lambda: self.app.source is not None and bool(self.app.tree.get_children()))

    def cleanup_tk(self):
        import gc
        if self.app.winfo_exists():
            self.pump(lambda: self.app._jobs == 0)
        self.root.destroy()
        self.app = None
        self.root = None
        gc.collect()

    def step(self):
        for _ in range(16):
            if not self.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                break

    def pump(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.step()
            if condition():
                return
            time.sleep(.015)
        self.fail('Tk condition timed out: ' + self.app.status.get())


class LayoutRegressionTests(_WorkbenchFixture, unittest.TestCase):
    """P0-L1 / pane 顺序 / 增量挂载的回归守护（只测布局容器层）。"""

    def _pane_names(self):
        """按当前挂载顺序返回 ['project'|'editor'|'agent', ...]。"""
        out = []
        for name in self.app.panes.panes():
            widget = self.app.panes.nametowidget(name)
            for label, frame in (('project', self.app.project_frame),
                                 ('editor', self.app.editor_frame),
                                 ('agent', self.app.agent_frame)):
                if widget is frame:
                    out.append(label)
                    break
        return out

    def _agent_children(self):
        return list(self.app.agent_frame.winfo_children())

    # ── 1. pane 顺序与 fractions 语义 ──────────────────────────────
    def test_pane_order_is_project_editor_agent(self):
        """apply_layout 的增量挂载必须保持 project|editor|agent 顺序。

        顺序一旦反转，fractions『sash0=project 右界』的语义就错位，
        agent 栏会从 40% 被压到 20%。"""
        self.assertEqual(self._pane_names(),
                         ['project', 'editor', 'agent'])

    def test_repeated_apply_layout_keeps_order_and_is_incremental(self):
        """面板集合不变时：顺序稳定，且 forget/insert/add 一次都不调用。

        原缺陷下每次调用都全量 forget+add（51f6f82 要消除的闪白路径
        复活），这里用 mock 计数把『增量』钉死。"""
        from unittest import mock
        with mock.patch.object(self.app.panes, 'forget') as forget, \
             mock.patch.object(self.app.panes, 'insert') as insert, \
             mock.patch.object(self.app.panes, 'add') as add:
            for _ in range(3):
                self.app.apply_layout()
                self.step()
        self.assertEqual(forget.call_count, 0, '集合不变不应 forget')
        self.assertEqual(insert.call_count, 0, '集合不变不应 insert')
        self.assertEqual(add.call_count, 0, '集合不变不应 add')
        self.assertEqual(self._pane_names(),
                         ['project', 'editor', 'agent'])

    # ── 2. 两种尺寸下 Agent 栏不溢出 ───────────────────────────────
    def test_agent_column_does_not_overflow_at_default_and_narrow_sizes(self):
        """1440x900（launch 默认）与 1200x760 下，Agent 栏子控件全部在栏内。"""
        for geometry in ('1440x900', '1200x760'):
            with self.subTest(geometry=geometry):
                root = self.app.winfo_toplevel()
                root.geometry(geometry)
                # 先 step 处理几何变更：apply_layout 按 pane 实测尺寸放
                # sash，读旧尺寸会放错位（实测 5px 级栏底错位）。
                self.step()
                self.app.apply_layout()
                # 高负载下重排走 idle，单步 step() 排不空（pythonw 整跑
                # 间歇复现，solo 即过）：泵到子控件全部 mapped 再断言
                # ——真不映射仍会超时失败，断言强度不变（M2 同款容错）。
                self.pump(lambda: all(c.winfo_ismapped()
                                      for c in self._agent_children()))
                self.step()
                bar_bottom = (self.app.agent_frame.winfo_rooty()
                              - root.winfo_rooty()
                              + self.app.agent_frame.winfo_height())
                for child in self._agent_children():
                    bottom = (child.winfo_rooty() - root.winfo_rooty()
                              + child.winfo_height())
                    self.assertTrue(child.winfo_ismapped(),
                                    f'{geometry} 子控件未映射: {child}')
                    self.assertLessEqual(
                        bottom, bar_bottom + 1,
                        f'{geometry} 子控件越出栏底: {child} '
                        f'bottom={bottom} 栏底={bar_bottom}')

    def test_agent_column_controls_all_mapped_at_both_sizes(self):
        """验收测试那 9 个代表控件在两种尺寸下都 mapped。"""
        widgets = (self.app.task_label, self.app.perm_badge,
                   self.app.context_label, self.app.input,
                   self.app.send_button, self.app.model_choice,
                   self.app.cancel_button, self.app.delegate_button,
                   self.app.run_model)
        for geometry in ('1440x900', '1200x760'):
            with self.subTest(geometry=geometry):
                root = self.app.winfo_toplevel()
                root.geometry(geometry)
                # 同上：几何变更的重排走 idle，泵到全部 mapped 再断言。
                self.pump(lambda: all(w.winfo_ismapped() for w in widgets))
                for widget in widgets:
                    self.assertTrue(widget.winfo_ismapped(),
                                    f'{geometry} {widget} 未映射')

    def test_agent_column_is_wide_enough_for_model_row(self):
        """agent 栏宽度必须容纳模型组（原 236px 下 run_model 被裁掉）。

        row4 内容需求实测约 311px（任务角色 52 + 下拉 90 + 独立模型 93 +
        模型回显 64 + 间距）；栏宽按 fractions 语义应占 40%，1200px 下
        约 468px。这里用 340px 作下限留出余量。"""
        root = self.app.winfo_toplevel()
        root.geometry('1200x760')
        self.step()
        self.app.apply_layout()
        self.step()
        width = self.app.agent_frame.winfo_width()
        self.assertGreaterEqual(width, 340,
                                f'agent 栏宽 {width} 不足以容纳模型组')

    def test_log_tabs_is_the_only_expand_slave(self):
        """log_tabs 必须是 agent_frame 内唯一 expand，否则固定组会被压缩。"""
        expanding = [c for c in self._agent_children()
                     if c.pack_info().get('expand')]
        self.assertEqual(len(expanding), 1)
        self.assertIs(expanding[0], self.app.log_tabs)

    # ── 3. 预设切换 / 折叠往返不回归 ───────────────────────────────
    def test_preset_roundtrip_restores_three_panes_in_order(self):
        """focus 预设只留一栏，切回 agent 必须恢复三栏且顺序不变。"""
        for preset in ('editor', 'focus', 'agent', 'review'):
            with self.subTest(preset=preset):
                self.app.preset.set(preset)
                self.app.change_preset()
                self.step()
        self.assertEqual(self._pane_names(),
                         ['project', 'editor', 'agent'])

    def test_toggle_focus_and_ide_never_empty_the_panes(self):
        """toggle_focus / toggle_ide 任意往返后 slave 数不得归零。

        原缺陷下 forget 后未重读 live，len(live) 过期会让 insert 以
        pos=0 越界（Slave index 0 out of bounds）。"""
        sequence = [
            ('editor', 'ide'), ('focus', 'focus'), ('focus', 'focus'),
            ('agent', 'ide'), ('review', 'ide'), ('review', 'focus'),
            ('editor', 'focus'), ('focus', 'ide'), ('agent', 'ide'),
        ]
        for preset, action in sequence:
            with self.subTest(preset=preset, action=action):
                self.app.preset.set(preset)
                self.app.change_preset()
                if action == 'ide':
                    self.app.toggle_ide()
                else:
                    self.app.toggle_focus()
                self.step()
                self.assertGreaterEqual(len(self.app.panes.panes()), 1,
                                        f'{preset}/{action} 后 slave 归零')
        self.app.preset.set('agent')
        self.app.change_preset()
        self.app.toggle_ide() if not self.app.state.layout['visible'] else None
        self.step()
        self.assertEqual(self._pane_names(),
                         ['project', 'editor', 'agent'])

    # ── 4. M1 骨架：导航栏 / 全宽底部面板 / 多字段状态栏 ───────────
    def test_navigation_bar_exposes_all_entries(self):
        """顶部导航栏必须提供品牌之外的 5 个导航项（2 模式 + 3 动作）。"""
        for key in ('general', 'development', 'project', 'run', 'settings'):
            self.assertIsNotNone(self.app.nav.item(key), f'导航栏缺少 {key}')
        self.assertIsNone(self.app.nav.item('nonexistent'))

    def test_navigation_active_mode_follows_mode_switch(self):
        """模式切换后导航栏激活项必须跟随（与 header 下拉同一 StringVar 真源）。"""
        self.assertEqual(self.app.nav.active_mode, 'development')
        self.app.state.switch('general')
        self.step()
        self.assertEqual(self.app.nav.active_mode, 'general')
        self.app.state.switch('development')
        self.step()
        self.assertEqual(self.app.nav.active_mode, 'development')

    def test_navigation_action_dispatches_via_callback(self):
        """动作项点击必须经 on_navigate 分发（布局组件不自己执行业务）。"""
        seen = []
        original = self.app.nav._on_navigate
        self.app.nav._on_navigate = lambda key: seen.append(key)
        try:
            for key in ('project', 'run', 'settings'):
                self.app.nav.item(key).event_generate('<Button-1>')
                self.step()
        finally:
            self.app.nav._on_navigate = original
        self.assertEqual(seen, ['project', 'run', 'settings'])

    def test_bottom_panel_spans_full_width_across_three_columns(self):
        """底部工具面板必须与三栏同宽（全宽），且是 panes 的姊妹 slave。"""
        self.root.update_idletasks()
        self.step()
        vertical = self.app.vertical
        width = vertical.winfo_width()
        self.assertGreater(width, 100)
        self.assertEqual(self.app.bottom.winfo_width(), width,
                         '底部面板宽度应等于外层竖向 pane 宽（全宽）')
        self.assertGreater(
            self.app.bottom.winfo_width(),
            self.app.project_frame.winfo_width() + self.app.agent_frame.winfo_width(),
            '底部面板应宽于左右两栏之和（证明它跨三栏而非只占中央栏）')
        slaves = [vertical.nametowidget(n) for n in vertical.panes()]
        self.assertEqual(slaves, [self.app.panes, self.app.bottom])

    def test_bottom_panel_hides_with_collapsed_ide_and_returns(self):
        """IDE 收起时底部面板让位，展开时补回且 [三栏, 底部] 次序不变。

        同时卫仪 place_sashes/capture_layout 的单 pane 保护：收起态下
        外层竖向 pane 只剩一个 slave，此时写 sashpos(0) 会越界。
        """
        vertical = self.app.vertical
        self.app.toggle_ide()
        self.step()
        self.app.place_sashes()      # 收起态调用不得抛错
        self.app.capture_layout()
        self.step()
        slaves = [vertical.nametowidget(n) for n in vertical.panes()]
        self.assertNotIn(self.app.bottom, slaves, '收起态底部面板应让位')
        self.app.toggle_ide()
        self.step()
        slaves = [vertical.nametowidget(n) for n in vertical.panes()]
        self.assertEqual(slaves, [self.app.panes, self.app.bottom],
                         '展开态应恢复 [三栏, 底部面板] 次序')

    def test_bottom_panel_keeps_all_five_output_pages(self):
        """迁移不得丢页：五输出页与 self.output/self.output_views 别名契约保留。"""
        self.assertEqual(len(self.app.bottom.views), 5)
        for name in ('Diff', '终端', '测试', '问题', '输出'):
            self.assertIn(name, self.app.bottom.views)
        self.assertIs(self.app.output, self.app.bottom.notebook)
        self.assertIs(self.app.output_views, self.app.bottom.views)
        self.app.write_output('输出', 'M1 冒烟')
        self.step()
        self.assertIn('M1 冒烟', self.app.output_views['输出'].get('1.0', 'end'))

    def test_status_bar_fields_default_to_placeholder_and_update(self):
        """四字段初始为占位，update 按值显示、未知键忽略（组件级）。"""
        bar = StatusBar(self.root, status_var=tk.StringVar(value=''))
        self.addCleanup(bar.destroy)
        for key in ('git', 'agent', 'model', 'project'):
            self.assertEqual(bar.value(key), '—', f'{key} 初始应为占位')
        bar.update(git='main · 2 处变更', agent='forge', model='默认编程',
                   project='pledge-evolving', unknown='ignored')
        self.assertEqual(bar.value('git'), 'main · 2 处变更')
        self.assertEqual(bar.value('agent'), 'forge')
        self.assertEqual(bar.value('model'), '默认编程')
        self.assertEqual(bar.value('project'), 'pledge-evolving')

    def test_status_bar_fields_are_fed_by_project_scan(self):
        """接线：_project_scan 的扫描结果必须推送到四字段。"""
        self.app._refresh_status_fields({'branch': 'main', 'status': {'a.py': 'M'}})
        self.step()
        self.assertEqual(self.app.status_bar.value('git'), 'main · 1 处变更')
        self.assertEqual(self.app.status_bar.value('project'),
                         self.app.source.root.name)
        # 瞬态消息：self.status 的行为与文案零变化（既有测试契约）
        self.app.status.set('M1 瞬态消息')
        self.step()
        self.assertIn('M1 瞬态消息', self.app.status.get())

    def test_vertical_sash_persists_bottom_fraction(self):
        """拖动竖向 sash 后 capture_layout 必须持久化（含 clamp），
        place_sashes 能把 sash 拉回持久化位置（2px 容差内）。"""
        self.root.update_idletasks()
        self.step()
        vertical = self.app.vertical
        height = vertical.winfo_height()
        self.assertGreater(height, 100)
        vertical.sashpos(0, int(height * 0.5))
        self.app.capture_layout()
        self.step()
        self.assertAlmostEqual(self.state.layout['output'], 0.5, delta=0.05)
        vertical.sashpos(0, int(height * 0.02))
        self.app.capture_layout()
        self.step()
        self.assertGreaterEqual(self.state.layout['output'], 0.15,
                                'output 必须被 clamp 到 >= .15')
        self.app.place_sashes()
        self.step()
        restored = int(height * self.state.layout['output'])
        self.assertLessEqual(abs(vertical.sashpos(0) - restored), 2)


class WelcomeViewTests(_WorkbenchFixture, unittest.TestCase):
    """M2 欢迎页守护：无项目 → 欢迎页；打开项目 → 编辑器本体。

    只测中央区二态切换与近期项目渲染/回调，不测欢迎页内部视觉细节。
    切换发生在 editor_frame 内的两个子帧之间，三栏 pane 的挂载顺序
    与增量逻辑（51f6f82）不在本类范围（由 LayoutRegressionTests 守护）。
    """

    open_project_on_start = False

    def _recent_labels(self):
        box = self.app.welcome._recent_box
        return [w for w in box.winfo_children()
                if isinstance(w, tk.Label) and w.cget('text') != '暂无项目记录']

    def _pump_content(self):
        """等欢迎页内容层映射到位。

        Tk 的映射按层级逐级传播（实测 root mapped 后每层还有数百 ms
        级联，编辑器 tabs/tree 同样如此——存量行为），只 pump
        welcome 自身标志会在过渡态中途返回、其子控件仍 unmapped。
        以内容层代表控件（打开项目按钮）为准。"""
        self.pump(lambda: (self.app.welcome.winfo_ismapped()
                           and self.app.welcome.open_button.winfo_ismapped()))

    def test_welcome_shown_without_project_and_hidden_after_open(self):
        """无项目：欢迎页 mapped、编辑器本体隐藏；打开项目后互换。"""
        self._pump_content()
        self.assertFalse(self.app.editor_body.winfo_ismapped(),
                         '无项目时编辑器本体应让位')
        self.assertIsNone(self.app.source)
        self.app.open_project(self.project)
        self.pump(lambda: self.app.source is not None)
        self.pump(lambda: not self.app.welcome.winfo_ismapped())
        self.assertTrue(self.app.editor_body.winfo_ismapped(),
                        '项目已载入后应切回编辑器本体')
        self.assertTrue(self.app.tabs.winfo_ismapped())

    def test_welcome_lists_recent_projects_and_row_opens_project(self):
        """近期项目来自 state.value('recent')（remember_project 写入），
        点击行经 on_recent 回调打开对应项目。"""
        self.state.remember_project(str(self.project))
        self.app._sync_welcome()  # 真实显示路径：每次展示前刷新近期列表
        # 等最深层级（近期行）映射后再点击——未映射控件不派发事件
        self.pump(lambda: self._recent_labels()
                  and self._recent_labels()[0].winfo_ismapped())
        self.assertEqual(self.app.welcome.recent_count(), 1)
        labels = self._recent_labels()
        self.assertEqual(len(labels), 1)
        # remember_project 存的是 resolve() 后的长路径（tempfile 在
        # Windows 可能给出 8.3 短名），断言按持久化口径比长名。
        self.assertIn(str(self.project.resolve()), labels[0].cget('text'))
        labels[0].event_generate('<Button-1>')
        self.pump(lambda: self.app.source is not None)
        self.assertEqual(Path(self.app.source.root),
                         self.project.resolve())

    def test_welcome_empty_recent_shows_placeholder(self):
        """无近期记录时显示空态提示，不计入 recent_count。"""
        self.step()
        self.assertEqual(self.app.welcome.recent_count(), 0)
        texts = [w.cget('text') for w in
                 self.app.welcome._recent_box.winfo_children()]
        self.assertIn('暂无项目记录', texts)

    def test_welcome_survives_mode_roundtrip(self):
        """模式切换（general↔development）会让 editor_frame 被
        apply_layout 摘除/重挂；无项目状态下欢迎页必须随之回归可见，
        且编辑器本体保持让位（_sync_welcome 状态不被重挂扰动）。"""
        self._pump_content()
        self.state.switch('general')
        self.pump(lambda: len(self.app.panes.panes()) == 1)
        self.state.switch('development')
        self._pump_content()
        self.assertEqual(len(self.app.panes.panes()), 3)
        self.assertFalse(self.app.editor_body.winfo_ismapped())
        self.assertIsNone(self.app.source)


class ExplorerViewTests(_WorkbenchFixture, unittest.TestCase):
    """M3 Explorer 守护：活动栏结构 / 视图切换 / Git 状态语义化 /
    文件操作（边界校验）/ 真实 git 仓库的端到端填充。

    只测布局容器层与面板公开行为，不触碰 Agent 栏内部控件语义
    （Codex 域）。状态语义用合成 scan 值（hermetic）+ 一路真实
    git 端到端（覆盖 _project_scan → populate 委派）。
    """

    def _tree_rows(self):
        """深度优先展开文件树：(iid, text, tags, values) 列表。"""
        rows = []

        def walk(iid):
            for child in self.app.tree.get_children(iid):
                rows.append((child, self.app.tree.item(child, 'text'),
                             self.app.tree.item(child, 'tags'),
                             self.app.tree.item(child, 'values')))
                walk(child)
        walk('')
        return rows

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=self.project, check=True,
                       capture_output=True, text=True)

    # ── 1. 活动栏结构与契约转交 ───────────────────────────────────
    def test_activity_bar_mounts_with_contracts(self):
        panel = self.app.explorer
        self.assertIsInstance(panel, ExplorerPanel)
        bar = panel._activity
        self.assertEqual(bar.winfo_width(), 48)
        self.assertEqual([key for key, _g, _l in bar.ITEMS],
                         ['files', 'search', 'git', 'run', 'extensions', 'settings'])
        self.assertEqual(bar.active, 'files')
        # 契约转交：app.tree / app.changes 就是面板内的 Treeview。
        self.assertIs(self.app.tree, panel.tree)
        self.assertIs(self.app.changes, panel.changes)
        # app.search 仍是面板 Entry 绑定的同一 StringVar。
        self.app.search.set('one')
        self.addCleanup(self.app.search.set, '')
        self.assertEqual(panel.search_entry.get(), 'one')
        # 默认停在「文件」标签。
        self.assertEqual(panel.notebook.tab(panel.notebook.select(), 'text'), '文件')

    def test_activity_bar_switches_views(self):
        panel = self.app.explorer
        bar = panel._activity
        bar.select('git')
        self.assertEqual(bar.active, 'git')
        self.assertEqual(panel.notebook.tab(panel.notebook.select(), 'text'),
                         'Git 变更')
        bar.select('files')
        self.assertEqual(panel.notebook.tab(panel.notebook.select(), 'text'), '文件')
        # 无真实数据源的入口：占位视图明说未接入（不伪造内容）。
        bar.select('extensions')
        self.pump(lambda: panel._placeholder.winfo_ismapped())
        self.assertEqual(panel._placeholder_title.cget('text'), '扩展')
        bar.select('files')
        self.pump(lambda: not panel._placeholder.winfo_ismapped())
        self.assertTrue(panel.notebook.winfo_ismapped())

    # ── 2. Git 状态语义化 ─────────────────────────────────────────
    def test_git_status_semantics_rendered_with_letter_badges(self):
        root = self.project.resolve()
        value = {
            'nodes': [
                {'path': str(root / 'a.py'), 'name': 'a.py', 'is_dir': False},
                {'path': str(root / 'b.py'), 'name': 'b.py', 'is_dir': False},
                {'path': str(root / 'c.py'), 'name': 'c.py', 'is_dir': False},
                {'path': str(root / 'x.py'), 'name': 'x.py', 'is_dir': False},
                {'path': str(root / 'pkg'), 'name': 'pkg', 'is_dir': True},
            ],
            'status': {'a.py': 'M', 'b.py': '??', 'c.py': 'D', 'x.py': 'UU'},
            'branch': 'main',
        }
        self.app.explorer.populate(value, root)
        rows = {Path(iid).name: (text, tags)
                for iid, text, tags, _values in self._tree_rows()}
        # 字母徽标 + tag（非颜色冗余 + 主题色双重编码）。
        self.assertEqual(rows['a.py'], ('M a.py', ('git-M',)))
        self.assertEqual(rows['b.py'], ('U b.py', ('git-U',)))
        self.assertEqual(rows['c.py'], ('D c.py', ('git-D',)))
        self.assertEqual(rows['x.py'], ('! x.py', ('git-X',)))
        self.assertEqual(rows['pkg'], ('pkg', ''))  # 无状态目录不加徽标
        changes = {iid: (self.app.changes.item(iid, 'text'),
                         self.app.changes.item(iid, 'tags'))
                   for iid in self.app.changes.get_children()}
        self.assertEqual(changes['a.py'], ('M a.py', ('git-M',)))
        self.assertEqual(changes['b.py'], ('U b.py', ('git-U',)))
        self.assertEqual(changes['c.py'], ('D c.py', ('git-D',)))
        self.assertEqual(changes['x.py'], ('! x.py', ('git-X',)))
        # 图例文字冗余：字母 + 中文标签同屏。
        legend = [w.cget('text') for w in panel_legend(self.app)]
        for expected in ('M 已修改', 'A 新增', 'D 删除', 'U 未跟踪', 'R 重命名'):
            self.assertIn(expected, legend)

    def test_scan_populates_semantic_status_from_real_git(self):
        """端到端：真实 git 仓库的 M / ?? 状态经 _project_scan 委派
        populate 后，树与变更清单都带语义徽标。"""
        self._git('init', '-q')
        self._git('add', '.')
        self._git('-c', 'user.name=t', '-c', 'user.email=t@t',
                  'commit', '-qm', 'init')
        (self.project / 'one.py').write_text('print(11)\n', encoding='utf-8')
        (self.project / 'two.py').write_text('print(2)\n', encoding='utf-8')
        self.app.refresh_project()
        self.pump(lambda: any('M one.py' == self.app.tree.item(i, 'text')
                              for i in self.app.tree.get_children()))
        rows = {Path(iid).name: self.app.tree.item(iid, 'text')
                for iid in self.app.tree.get_children()}
        self.assertEqual(rows['one.py'], 'M one.py')
        self.assertEqual(rows['two.py'], 'U two.py')
        changes = {iid: self.app.changes.item(iid, 'text')
                   for iid in self.app.changes.get_children()}
        self.assertEqual(changes['one.py'], 'M one.py')
        self.assertEqual(changes['two.py'], 'U two.py')

    # ── 3. 文件操作（经 ProjectBoundary 校验）─────────────────────
    def test_file_ops_create_rename_delete_through_boundary(self):
        root = self.project.resolve()
        with mock.patch('development_mode.explorer.simpledialog.askstring',
                        return_value='made.py'):
            self.app.explorer._new_file()
        self.assertTrue((root / 'made.py').is_file(), '新建文件应落盘')
        self.pump(lambda: any(Path(i).name == 'made.py'
                              for i in self.app.tree.get_children()))
        self.app.tree.selection_set(str(root / 'made.py'))
        with mock.patch('development_mode.explorer.simpledialog.askstring',
                        return_value='renamed.py'):
            self.app.explorer._rename()
        self.pump(lambda: any(Path(i).name == 'renamed.py'
                              for i in self.app.tree.get_children()))
        self.assertTrue((root / 'renamed.py').is_file())
        self.assertFalse((root / 'made.py').exists())
        self.app.tree.selection_set(str(root / 'renamed.py'))
        with mock.patch('development_mode.explorer.messagebox.askyesno',
                        return_value=True):
            self.app.explorer._delete()
        self.pump(lambda: not any(Path(i).name == 'renamed.py'
                                  for i in self.app.tree.get_children()))
        self.assertFalse((root / 'renamed.py').exists())

    def test_boundary_rejects_paths_outside_project(self):
        boundary = self.app.source.boundary
        with self.assertRaises(PermissionError):
            explorer.create_file(boundary, self.base, 'evil.py')
        with self.assertRaises(PermissionError):
            explorer.create_file(boundary, self.project / '.git', 'evil.py')
        with self.assertRaises(PermissionError):
            explorer.delete_path(boundary, self.base / 'repo' / '..' / 'x')

    def test_collapse_all_clears_expanded_state(self):
        root = self.project.resolve()
        (self.project / 'sub').mkdir()
        (self.project / 'sub' / 'x.py').write_text('x\n', encoding='utf-8')
        self.app.expanded.add(str(root / 'sub'))
        self.app.refresh_project()
        self.pump(lambda: any(Path(i).name == 'sub'
                              for i in self.app.tree.get_children()))
        self.assertTrue(self.app.tree.item(str(root / 'sub'), 'open'))
        self.app.explorer._collapse_all()
        self.pump(lambda: not self.app.expanded)
        self.assertFalse(self.app.tree.item(str(root / 'sub'), 'open'))


SAMPLE_PY = '''# 头部注释
def greet(name):
    text = "hello"
    return text + name


class Foo:
    pass
'''


class EditorAreaTests(_WorkbenchFixture, unittest.TestCase):
    """M4 编辑器增强守护：行号槽 / 当前行 / 高亮 / 修改标记 / 搜索替换 /
    分屏镜像 / 另存为。只测编辑器容器层与面板公开行为。"""

    def _open_sample(self):
        path = (self.project / 'sample.py')
        path.write_text(SAMPLE_PY, encoding='utf-8')
        key = str(path.resolve())
        self.app.open_file(path.resolve())
        self.pump(lambda: key in self.app.documents)
        return self.app.documents[key]

    # ── 1. 契约与行号槽 ───────────────────────────────────────────
    def test_editor_area_contracts_and_gutter(self):
        row = self._open_sample()
        area = row['area']
        self.assertIsInstance(area, EditorArea)
        # row['widget'] 契约：既有 save/reload/attach 消费点无感。
        self.assertIs(row['widget'], area.text)
        text, gutter = area.text, area.gutter
        lines = int(text.index('end-1c').split('.')[0])
        gutter_lines = gutter.get('1.0', 'end-1c').split('\n')
        self.assertEqual(len(gutter_lines), lines)
        self.assertGreaterEqual(int(gutter.cget('width')), 3)
        # 标签页右侧按钮位存在（示例图：分屏 / 更多）。
        self.assertTrue(hasattr(self.app, 'more_button'))

    # ── 1b. 标签行按钮位映射（打包器超订坍缩守护） ─────────────────
    def test_tab_row_action_buttons_stay_mapped(self):
        """1200x760 夹具下 Notebook 内含默认 80x24 请求的 Text（约 648px），
        会超订 tab_row 腔体（约 468px）：Tk 打包器超订时不重算分配，先
        pack 的 slave 拿满请求、后 pack 的 slave 恒得 1x1 且不映射。若
        Notebook 先于 actions pack，分屏/更多按钮整帧不可见。side 决定
        视觉位置、pack 次序决定让位优先级——小帧必须先 pack。"""
        self._open_sample()
        actions = self.app.more_button.master
        split = [w for w in actions.winfo_children()
                 if w is not self.app.more_button][0]
        self.pump(lambda: self.app.more_button.winfo_ismapped())
        for w in (actions, split, self.app.more_button):
            self.assertTrue(w.winfo_ismapped(),
                            '标签行右侧按钮位必须 mapped（超订坍缩回归）')
        # Notebook 吸收赤字：仍占左侧弹性区且 mapped。
        self.assertTrue(self.app.tabs.winfo_ismapped())
        self.assertGreater(self.app.tabs.winfo_width(), actions.winfo_width())

    # ── 1c. 编辑器滚动条映射（同族打包器坍缩守护） ─────────────────
    def test_editor_scrollbars_stay_mapped(self):
        """body 内固定尺寸滚动条必须先于 expand 的正文 pack：正文 Text
        默认 80 字符请求超订 body 腔体，后 pack 的竖向滚动条恒得 1x1
        且不映射（1200x760 实测，原 _document_loaded 的顺序本就滚动条
        优先，EditorArea 保持同一约定）。"""
        row = self._open_sample()
        area = row['area']
        body = area._body_frame
        hbar = [w for w in body.winfo_children()
                if w is not area._vbar and w.winfo_class() == 'TScrollbar'][0]
        # documents 键先于 tabs.add 落库——帧入 Notebook 后的重排走
        # idle，泵到两条滚动条都 mapped 再断言（结构性坍缩仍会超时失败）。
        self.pump(lambda: area._vbar.winfo_ismapped()
                  and hbar.winfo_ismapped())
        for w in (area._vbar, hbar):
            self.assertTrue(w.winfo_ismapped(),
                            '滚动条必须 mapped（超订坍缩回归）')
            self.assertGreater(w.winfo_width(), 1)
            self.assertGreater(w.winfo_height(), 1)

    def test_python_highlight_and_current_line(self):
        row = self._open_sample()
        area = row['area']
        text = area.text
        for tag in ('kw', 'st', 'cm', 'fn', 'cls'):
            self.assertGreater(len(text.tag_ranges(tag)), 0,
                               f'示例代码应产生 {tag} 高亮')
        text.mark_set('insert', '3.0')
        area._highlight_current_line()
        self.assertEqual(len(text.tag_ranges('current_line')), 2)

    # ── 2. gutter 修改标记 ────────────────────────────────────────
    def test_modified_line_marker_in_gutter(self):
        row = self._open_sample()
        area = row['area']
        area.text.insert('3.0', '# edited\n')
        area.refresh_markers()
        # 第 3 行是新插入行 → add 标记落在 gutter 第 3 行。
        ranges = area.gutter.tag_ranges('add')
        self.assertEqual(len(ranges), 2)
        self.assertEqual(str(ranges[0]), '3.0')
        # 撤去插入行，恢复三行基线。
        area.text.delete('3.0', '4.0')
        area.refresh_markers()
        self.assertEqual(len(area.gutter.tag_ranges('add')), 0)
        # 同行数原位替换（改写第 2 行内容，不动行数）→ mod 标记。
        area.text.delete('2.0', '2.end')
        area.text.insert('2.0', '    print("changed")')
        area.refresh_markers()
        mods = area.gutter.tag_ranges('mod')
        self.assertGreater(len(mods), 0)
        self.assertEqual(str(mods[0]), '2.0')

    # ── 3. 搜索替换 ───────────────────────────────────────────────
    def test_search_replace_flow(self):
        row = self._open_sample()
        area = row['area']
        area.open_search()
        self.pump(lambda: area.search_bar.winfo_ismapped())
        area._find_var.set('greet')
        area._refresh_hits()
        self.assertEqual(area._count_var.get(), '1 处匹配')
        self.assertEqual(len(area.text.tag_ranges('search_hit')), 2)
        area.find_next()
        self.assertEqual(len(area.text.tag_ranges('search_current')), 2)
        area._replace_var.set('salute')
        area.replace_current()
        self.assertIn('salute', area.text.get('1.0', 'end-1c'))
        area._find_var.set('hello')
        area._replace_var.set('hi')
        area.replace_all()
        self.assertIn('hi', area.text.get('1.0', 'end-1c'))
        self.assertEqual(area._count_var.get(), '已替换 1 处')
        area.close_search()
        self.assertFalse(area.search_bar.winfo_ismapped())
        self.assertEqual(len(area.text.tag_ranges('search_hit')), 0)

    # ── 4. 分屏参考镜像 ───────────────────────────────────────────
    def test_split_reference_mirror_syncs_and_hides(self):
        row = self._open_sample()
        area = row['area']
        area.toggle_reference()
        self.pump(lambda: area.reference.winfo_ismapped())
        self.assertIn('greet', area.reference.get('1.0', 'end-1c'))
        area.text.insert('1.0', '# mirror sync test\n')
        area._sync_reference()
        self.assertIn('mirror sync test',
                      area.reference.get('1.0', 'end-1c'))
        area.toggle_reference()
        self.assertFalse(area.reference.winfo_ismapped())

    # ── 4b. 标签行按钮的真实命令接线 ──────────────────────────────
    def test_split_button_invokes_split_current(self):
        """分屏图标按钮经 invoke 走 split_current——按钮位可见且接线真实
        （打包器超订坍缩修复后，mapped 与命令双保险）。"""
        row = self._open_sample()
        actions = self.app.more_button.master
        split = [w for w in actions.winfo_children()
                 if w is not self.app.more_button][0]
        split.invoke()
        self.pump(lambda: row['area'].reference.winfo_ismapped())
        split.invoke()
        self.assertFalse(row['area'].reference.winfo_ismapped())

    def test_more_button_wired_to_more_menu(self):
        self._open_sample()
        self.assertIn('_editor_more_menu', self.app.more_button['command'])

    def test_copy_current_path_to_clipboard(self):
        row = self._open_sample()
        self.app._copy_current_path()
        self.assertEqual(self.root.clipboard_get(),
                         str(row['document'].path))
        self.assertIn('已复制路径', self.app.status.get())

    # ── 5. 另存为 ─────────────────────────────────────────────────
    def test_save_as_rebinds_document_and_migrates_key(self):
        row = self._open_sample()
        old_key = str(row['document'].path)
        with mock.patch('development_mode.app.filedialog.asksaveasfilename',
                        return_value=str(self.project / 'copy.py')):
            self.app.save_as_current()
        self.pump(lambda: any(Path(k).name == 'copy.py'
                              for k in self.app.documents))
        new_key = str((self.project / 'copy.py').resolve())
        self.assertIn(new_key, self.app.documents)
        self.assertNotIn(old_key, self.app.documents)
        self.assertIs(self.app.documents[new_key], row)
        self.assertEqual(row['document'].path.name, 'copy.py')
        self.assertTrue((self.project / 'copy.py').is_file())
        self.assertEqual(self.app.tabs.tab(row['frame'], 'text'), 'copy.py')

    def test_save_document_as_rejects_paths_outside_project(self):
        row = self._open_sample()
        boundary = self.app.source.boundary
        with self.assertRaises(PermissionError):
            save_document_as(boundary, row['document'], self.base / 'evil.py')
        with self.assertRaises(PermissionError):
            save_document_as(boundary, row['document'],
                             self.project / '.git' / 'evil.py')


SAMPLE_DIFF = '''--- a/one.py
+++ b/one.py
@@ -1,2 +1,3 @@
 print(1)
-print(2)
+print(22)
+print(3)
'''


class BottomPanelTests(_WorkbenchFixture, unittest.TestCase):
    """M5 底部面板守护：四标签结构 / 真实计数徽标 / 输出别名契约 /
    差异页增删行着色。只测面板公开行为与 app 侧 provider 接线。"""

    def _tab_text(self, name):
        return self.app.bottom.notebook.tab(
            self.app.bottom._pages[name], 'text')

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=self.project, check=True,
                       capture_output=True, text=True)

    # ── 1. 四标签结构与徽标分布 ───────────────────────────────────
    def test_four_tabs_with_badges_and_plain_terminal(self):
        bottom = self.app.bottom
        labels = [bottom.notebook.tab(bottom._pages[n], 'text')
                  for n in ('Diff', '终端', '测试', '问题')]
        self.assertEqual(labels, ['差异(0)', '终端', '测试(0)', '问题(0)'])
        # 四个可见标签，无「输出」标签（并入测试页）。
        self.assertEqual(len(bottom.notebook.tabs()), 4)

    def test_output_page_aliases_test_page(self):
        """别名契约：views['输出'] 与 views['测试'] 同一 Text——
        write_output('输出') 既有调用点零改动，内容落在测试页。"""
        bottom = self.app.bottom
        self.assertIs(self.app.output_views['输出'],
                      self.app.output_views['测试'])
        self.assertIs(self.app.output, bottom.notebook)
        self.app.write_output('输出', 'ALIAS-LINE\n')
        self.assertIn('ALIAS-LINE',
                      self.app.output_views['测试'].get('1.0', 'end-1c'))
        self.assertTrue(bottom.select('输出'))
        self.assertEqual(bottom.notebook.tab('current', 'text'), '测试(0)')

    def test_select_zero_still_selects_diff_tab(self):
        """既有调用点 self.output.select(0) 的语义：tab 0 = 差异页。"""
        self.app.output.select(0)
        self.assertEqual(self.app.bottom.notebook.index('current'), 0)

    # ── 2. 真实计数徽标 ───────────────────────────────────────────
    def test_diff_badge_counts_real_git_changes(self):
        """端到端：真实 git 仓库的 M/?? 变更数进入差异徽标（扫描完成
        即刷新，不等标签切换）。"""
        self._git('init', '-q')
        self._git('config', 'user.name', 't')
        self._git('config', 'user.email', 't@t')
        self._git('add', '.')
        self._git('commit', '-qm', 'init')
        (self.project / 'one.py').write_text('print(11)\n', encoding='utf-8')
        (self.project / 'two.py').write_text('print(2)\n', encoding='utf-8')
        self.app.refresh_project()
        self.pump(lambda: self._tab_text('Diff') == '差异(2)')

    def test_badges_refresh_on_tab_change(self):
        """标签切换即刷新（<<NotebookTabChanged>> 接线）。"""
        bottom = self.app.bottom
        self.app.task_evidence.apply(
            {'type': 'test_result', 'run_id': 'r1', 'agent': 'human-test',
             'status': 'completed', 'exit_code': 0})
        bottom.notebook.select(1)          # 切到终端再切回，触发事件
        bottom.notebook.select(2)
        # select() 只投递虚拟事件——泵到徽标更新（接线失效则超时失败）。
        self.pump(lambda: self._tab_text('测试') == '测试(1)')

    def test_test_badge_counts_evidence_records(self):
        bottom = self.app.bottom
        self.app.task_evidence.apply(
            {'type': 'test_result', 'run_id': 'r1', 'agent': 'human-test',
             'status': 'completed', 'exit_code': 0})
        self.app.task_evidence.apply(
            {'type': 'test_result', 'run_id': 'r2', 'agent': 'human-test',
             'status': 'failed', 'exit_code': 1})
        bottom.refresh_badges()
        # 记录数（含失败）——徽标不区分成败，只数真实记录。
        self.assertEqual(self._tab_text('测试'), '测试(2)')

    def test_problem_badge_counts_failed_records(self):
        bottom = self.app.bottom
        self.app.task_evidence.apply(
            {'type': 'tool_completed', 'action_id': 'a1',
             'tool': 'shell_exec', 'ok': False, 'status': 'failed'})
        bottom.refresh_badges()
        self.assertEqual(self._tab_text('问题'), '问题(1)')
        # 失败的测试同时计入问题（任务书 Problems 含测试失败）。
        self.app.task_evidence.apply(
            {'type': 'test_result', 'run_id': 'r1', 'agent': 'human-test',
             'status': 'failed', 'exit_code': 1})
        bottom.refresh_badges()
        self.assertEqual(self._tab_text('问题'), '问题(2)')
        # 成功记录不计入问题。
        self.app.task_evidence.apply(
            {'type': 'tool_completed', 'action_id': 'a2',
             'tool': 'write_file', 'ok': True, 'status': 'completed'})
        bottom.refresh_badges()
        self.assertEqual(self._tab_text('问题'), '问题(2)')

    def test_badge_shows_zero_without_fabrication(self):
        """无数据时显示 0（示例图问题(0)），不伪造计数。"""
        self.assertEqual(self._tab_text('Diff'), '差异(0)')
        self.assertEqual(self._tab_text('测试'), '测试(0)')
        self.assertEqual(self._tab_text('问题'), '问题(0)')

    # ── 3. 差异页增删行着色 ───────────────────────────────────────
    def test_diff_page_highlights_added_removed_hunk_lines(self):
        bottom = self.app.bottom
        self.app.write_output('Diff', SAMPLE_DIFF, clear=True)
        bottom.refresh_badges()
        text = self.app.output_views['Diff']
        self.assertEqual(len(text.tag_ranges('diff_add')), 4)    # 2 行
        self.assertEqual(len(text.tag_ranges('diff_del')), 2)    # 1 行
        self.assertEqual(len(text.tag_ranges('diff_hunk')), 2)   # 1 行
        # 非差异内容不染色。
        self.app.write_output('Diff', '普通文本，没有 +/- 行\n', clear=True)
        bottom.refresh_badges()
        self.assertEqual(len(text.tag_ranges('diff_add')), 0)
        self.assertEqual(len(text.tag_ranges('diff_del')), 0)
        self.assertEqual(len(text.tag_ranges('diff_hunk')), 0)


class LayoutPresetTests(_WorkbenchFixture, unittest.TestCase):
    """M6 三布局预设守护：wanted 集合与权重 / Review Focus 内容侧重 /
    中央 Git Diff 渲染与着色 / 切换保留 / review_fractions 独立槽。
    只测布局容器层与 app 侧预设切换逻辑，不触碰 Agent 栏内部语义。"""

    def _git(self, *args):
        subprocess.run(['git', *args], cwd=self.project, check=True,
                       capture_output=True, text=True)

    def _pane_names(self):
        """按当前挂载顺序返回 ['project'|'editor'|'agent', ...]。"""
        out = []
        for name in self.app.panes.panes():
            widget = self.app.panes.nametowidget(name)
            for key, frame in (('project', self.app.project_frame),
                               ('editor', self.app.editor_frame),
                               ('agent', self.app.agent_frame)):
                if widget is frame:
                    out.append(key)
        return out

    def _use_preset(self, preset):
        self.app.preset.set(preset)
        self.app.change_preset()
        self.step()

    def _make_dirty_repo(self):
        """真实 git 仓库 + 一个已修改文件（含增删行）+ 一个未跟踪文件。"""
        self._git('init', '-q')
        self._git('config', 'user.name', 't')
        self._git('config', 'user.email', 't@t')
        self._git('add', '.')
        self._git('commit', '-qm', 'init')
        (self.project / 'one.py').write_text('print(9)\nprint(22)\n', encoding='utf-8')
        (self.project / 'two.py').write_text('print(2)\n', encoding='utf-8')
        self.app.refresh_project()
        self.pump(lambda: len(self.app.changes.get_children()) == 2)

    # ── 1. 预设表结构（layout.py PRESETS） ───────────────────────
    def test_presets_table_three_modes_share_one_workspace(self):
        """三预设 wanted 集合相同（同一套组件，不得三套 IDE）、权重互异
        且合法；focus 不在此表（二栏模式由 toggle_focus 驱动）。"""
        self.assertEqual(sorted(PRESETS), ['agent', 'editor', 'review'])
        for name, spec in PRESETS.items():
            with self.subTest(preset=name):
                self.assertEqual(spec['wanted'],
                                 ('project', 'editor', 'agent'))
                f = spec['fractions']
                self.assertLess(f[0], f[1])
                self.assertGreaterEqual(f[0], .05)
                self.assertLessEqual(f[1], .95)
                self.assertTrue(spec['label'])
        # 三档权重互异（Agent/Code/Review 各有侧重）。
        weights = {spec['fractions'] for spec in PRESETS.values()}
        self.assertEqual(len(weights), 3)
        # Review Focus：中央区占比过半（Git Diff 是焦点）。
        self.assertGreater(PRESETS['review']['fractions'][1]
                           - PRESETS['review']['fractions'][0], .4)

    def test_review_preset_mounts_three_panes_with_own_fractions(self):
        """切到 review：三栏齐全、顺序不变、权重取 review 默认值；
        agent/editor 预设权重不变（回归）。"""
        self._use_preset('review')
        self.assertEqual(self._pane_names(), ['project', 'editor', 'agent'])
        self.assertEqual(self.state.layout['preset'], 'review')
        self.assertEqual([round(v, 2) for v in self.state.layout['fractions']],
                         [round(v, 2) for v in PRESETS['review']['fractions']])
        self._use_preset('agent')
        self.assertEqual([round(v, 2) for v in self.state.layout['fractions']],
                         [.17, .49])
        self._use_preset('editor')
        self.assertEqual([round(v, 2) for v in self.state.layout['fractions']],
                         [.17, .76])

    # ── 2. Review Focus 内容侧重（共享组件、增量切换） ────────────
    def test_review_focus_emphasis_switches_views_without_destroying(self):
        """review：左栏切 Git 变更清单、中央切只读差异视图、右栏切任务
        页、底部切测试页；切回 agent：编辑器本体与文件树复位——所有
        控件始终存在（只 pack/unpack，不销毁）。"""
        self._use_preset('agent')
        self.assertTrue(self.app.editor_body.winfo_ismapped())
        self.assertFalse(self.app.review_view.winfo_ismapped())
        self._use_preset('review')
        self.pump(lambda: self.app.review_view.winfo_ismapped())
        self.assertFalse(self.app.editor_body.winfo_ismapped())
        self.assertFalse(self.app.welcome.winfo_ismapped())
        self.assertEqual(self.app.explorer._activity.active, 'git')
        self.assertEqual(self.app.bottom.notebook.tab('current', 'text'),
                         '测试(0)')
        self.assertEqual(self.app.log_tabs.tab('current', 'text'),
                         '任务 / 子 Agent')
        # 组件未被销毁（winfo_exists 对 forget 的控件仍为 1）。
        self.assertTrue(self.app.editor_body.winfo_exists())
        self.assertTrue(self.app.welcome.winfo_exists())
        self._use_preset('agent')
        self.pump(lambda: self.app.editor_body.winfo_ismapped())
        self.assertFalse(self.app.review_view.winfo_ismapped())
        self.assertEqual(self.app.explorer._activity.active, 'files')

    def test_review_central_diff_renders_and_colors(self):
        """端到端：review 下选中变更文件 → 中央只读视图渲染真实
        inspect_diff 文本并着色（add/del/hunk）。"""
        self._make_dirty_repo()
        self._use_preset('review')
        self.app.changes.selection_set('one.py')
        self.pump(lambda: 'one.py' in self.app.review_title['text'])
        text = self.app.review_text.get('1.0', 'end-1c')
        self.assertIn('+print(22)', text)
        self.assertIn('-print(1)', text)
        self.assertEqual(len(self.app.review_text.tag_ranges('diff_add')) // 2, 2)
        self.assertEqual(len(self.app.review_text.tag_ranges('diff_del')) // 2, 1)
        self.assertGreaterEqual(
            len(self.app.review_text.tag_ranges('diff_hunk')) // 2, 1)
        # 只读契约：视图 Text 恒 disabled。
        self.assertEqual(str(self.app.review_text['state']), 'disabled')

    def test_non_review_preset_keeps_show_diff_bottom_page(self):
        """非 review 预设：变更选择仍走既有 show_diff（底部 Diff 页），
        中央视图不抢占。"""
        self._make_dirty_repo()
        self._use_preset('agent')
        self.app.changes.selection_set('one.py')
        self.pump(lambda: '+print(22)' in self.app.output_views['Diff'].get('1.0', 'end-1c'))
        self.assertFalse(self.app.review_view.winfo_ismapped())

    # ── 3. 切换保留（任务书 §M6 显式要求） ───────────────────────
    def test_preset_switch_preserves_documents_dirty_and_chat(self):
        """任意预设往返：打开的文件 / 未保存编辑 / Agent 对话保留
        （容器不销毁即可——editor_body / agent_frame 全程在装）。"""
        target = (self.project / 'one.py').resolve()
        self.app.open_file(target)
        self.pump(lambda: self.app.documents.get(str(target)) is not None)
        row = self.app.documents[str(target)]
        row['area'].text.insert('1.0', '# dirty\n')
        # on_change 绑 <<Modified>> 虚事件——程序级 insert 后需泵一次
        # 事件循环，document_changed 才跑（dirty 同步置位）。
        self.pump(lambda: row['document'].dirty)
        self.assertTrue(row['document'].dirty)
        self.app.append(self.app.chat, 'PRESERVE-CHAT\n')
        for preset in ('review', 'agent', 'editor', 'review', 'focus',
                       'agent', 'review'):
            with self.subTest(preset=preset):
                self._use_preset(preset)
        self.assertIn(str(target), self.app.documents)
        self.assertTrue(self.app.documents[str(target)]['document'].dirty)
        self.assertIn('PRESERVE-CHAT', self.app.chat.get('1.0', 'end'))
        self.assertEqual(self.app.documents[str(target)]['area'].text.get(
            '1.0', 'end-1c').splitlines()[0], '# dirty')

    # ── 4. review_fractions 独立槽（81acf1b 裁决 2） ─────────────
    def test_review_fractions_persist_in_independent_slot(self):
        """review 下用户的 sash 调整写入 review_fractions 独立槽；切到
        其他预设（fractions 被覆盖）再切回 review，调整不丢失。"""
        self._use_preset('review')
        width = self.app.panes.winfo_width()
        self.assertGreater(width, 50)
        self.app.panes.sashpos(0, int(width * .25))
        self.app.panes.sashpos(1, int(width * .8))
        self.app.capture_layout()
        layout = self.state.layout
        self.assertAlmostEqual(layout['review_fractions'][0], .25, places=1)
        self.assertAlmostEqual(layout['review_fractions'][1], .8, places=1)
        # 切到 agent：fractions 被预设权重覆盖，独立槽不动。
        self._use_preset('agent')
        self.assertEqual([round(v, 2) for v in self.state.layout['fractions']],
                         [.17, .49])
        self.assertAlmostEqual(self.state.layout['review_fractions'][0],
                               .25, places=1)
        # 切回 review：从独立槽恢复（fractions 回到用户调整值）。
        self._use_preset('review')
        self.assertAlmostEqual(self.state.layout['fractions'][0], .25, places=1)
        self.assertAlmostEqual(self.state.layout['fractions'][1], .8, places=1)


class StatusBarTests(_WorkbenchFixture, unittest.TestCase):
    """M7 状态栏编辑器状态组 + 头部在线状态 + 品牌火花守护。

    数据源真实性：编码/换行/语言取自当前 Document 属性与扩展名
    映射；光标由 EditorArea 光标通知推送；就绪态现读 runtime；
    问题计数走 TaskEvidence 失败记录（与底部徽标同源）；无独立
    诊断器 → 警告恒 0；runtime 无在线数据 → 头部「离线」不伪造。
    """

    def _open_sample(self):
        path = (self.project / 'sample.py')
        path.write_text(SAMPLE_PY, encoding='utf-8')
        key = str(path.resolve())
        self.app.open_file(path.resolve())
        self.pump(lambda: key in self.app.documents)
        return self.app.documents[key]

    def _fields(self):
        bar = self.app.status_bar
        return {key: bar.value(key) for key in
                ('cursor', 'encoding', 'eol', 'language', 'ready',
                 'warnings', 'problems')}

    # ── 1. 编辑器状态组：文档驱动 ─────────────────────────────────
    def test_editor_fields_track_opened_document(self):
        """打开 .py 文档 → UTF-8 / LF / Python（真实 Document 属性）；
        M1 既有四字段与瞬态消息契约不受影响。"""
        row = self._open_sample()
        self.pump(lambda: self.app.status_bar.value('language') == 'Python')
        fields = self._fields()
        self.assertEqual(fields['encoding'], 'UTF-8')
        self.assertEqual(fields['eol'], 'LF')
        self.assertEqual(fields['language'], 'Python')
        # M1 契约：既有四字段键仍在；瞬态消息由 status.set 驱动
        # （此处为扫描完成文案，无项目初始态才是「等待」）。
        self.assertIn('repo', self.app.status_bar.value('project'))
        self.assertIn('repo', self.app.status.get())

    def test_editor_fields_placeholder_without_document(self):
        """无打开文件 → 光标/编码/换行/语言占位；警告/问题 0。"""
        self.app.close_current()
        self.step()
        fields = self._fields()
        for key in ('cursor', 'encoding', 'eol', 'language'):
            self.assertEqual(fields[key], '—', key)
        self.assertEqual(fields['warnings'], '0')
        self.assertEqual(fields['problems'], '0')

    def test_cursor_field_follows_insert(self):
        """光标通知（KeyRelease/ButtonRelease 同一汇聚点）→ 行列字段。
        注意：测试环境不可见窗口无焦点，合成 KeyRelease 事件不触发
        绑定——直接走 EditorArea.notify_cursor 公开路径（真实事件与
        该路径在 _after_caret 汇聚，链路同一）。"""
        row = self._open_sample()
        area = row['area']
        area.text.mark_set('insert', '3.4')
        area.notify_cursor()
        self.step()
        self.assertEqual(self.app.status_bar.value('cursor'), '行 3, 列 5')

    def test_ready_field_and_indicator_reflect_runtime(self):
        """就绪态现读 runtime：ready → 绿点「就绪」；busy → 黄点
        「运行中」；未就绪 → 灰点。圆点颜色经 set_indicator 语义
        token，不持有第二份状态。"""
        class _FakeRuntime:
            def __init__(self, ready, busy):
                self.ready, self.busy = ready, busy
        bar = self.app.status_bar
        self.app.runtime = _FakeRuntime(True, False)
        self.app._sync_ready_status()
        self.assertEqual(bar.value('ready'), '就绪')
        self.assertEqual(bar._indicator.itemcget('dot', 'fill'), C['ok'])
        self.app.runtime = _FakeRuntime(True, True)
        self.app._sync_ready_status()
        self.assertEqual(bar.value('ready'), '运行中')
        self.assertEqual(bar._indicator.itemcget('dot', 'fill'), C['warn'])
        self.app.runtime = _FakeRuntime(False, False)
        self.app._sync_ready_status()
        self.assertEqual(bar.value('ready'), '未就绪')
        self.assertEqual(bar._indicator.itemcget('dot', 'fill'), C['ter'])

    def test_problems_field_counts_real_failures(self):
        """问题字段 = TaskEvidence 失败记录（真实计数）；经
        handle_event 接线——失败 test_result 到达即刷新（与底部
        问题徽标同源）。无独立诊断器 → 警告恒 0 不伪造。"""
        self._open_sample()
        self.app.task_evidence.apply(
            {'type': 'test_result', 'run_id': 'r1', 'agent': 't',
             'status': 'failed', 'exit_code': 1})
        self.app.handle_event({'data': {'type': 'test_result', 'run_id': 'r1',
                                        'agent': 't', 'status': 'failed',
                                        'exit_code': 1}})
        self.step()
        self.assertEqual(self.app.status_bar.value('problems'), '1')
        self.assertEqual(self.app.status_bar.value('warnings'), '0')
        # 底部问题徽标同步（同源同事件）。
        self.assertEqual(self.app.bottom._pages and
                         self.app.bottom.notebook.tab(
                             self.app.bottom._pages['问题'], 'text'),
                         '问题(1)')

    # ── 2. 头部在线状态与品牌火花 ─────────────────────────────────
    def test_header_online_status_offline_without_fabrication(self):
        """runtime 无在线连接数据源 → 「离线」+ 灰点，不伪造计数；
        文案与圆点色经 _sync_online_status 一处可控。"""
        self.assertEqual(self.app.online_var.get(), '离线')
        self.assertEqual(self.app.online_dot.itemcget('dot', 'fill'), C['ter'])
        self.app._sync_online_status()
        self.assertEqual(self.app.online_var.get(), '离线')

    def test_brand_spark_icon_left_of_forge_label(self):
        """品牌火花（flame）在 FORGE 左侧，颜色 = brand_spark token
        （示例图橙色），与 chat 空态主视觉火花同源同色。"""
        nav = self.app.nav
        canvases = [w for w in nav.winfo_children()
                    if w.winfo_class() == 'Canvas']
        labels = [w for w in nav.winfo_children()
                  if w.winfo_class() == 'Label' and w.cget('text') == 'FORGE']
        self.assertEqual(len(canvases), 1)
        self.assertEqual(len(labels), 1)
        self.assertEqual(canvases[0].cget('fg'), C['brand_spark'])
        self.assertLess(canvases[0].winfo_rootx(),
                        labels[0].winfo_rootx())

    # ── 3. 两尺寸布局回归（编辑器状态组不越界） ───────────────────
    def test_status_editor_group_mapped_at_both_sizes(self):
        """1200x760 与 1440x900 下状态栏编辑器状态组 Label 全部
        mapped 且在根窗内（布局回归口径）。"""
        self._open_sample()
        for geometry in ('1200x760', '1440x900'):
            with self.subTest(geometry=geometry):
                self.root.geometry(geometry)
                self.pump(lambda: all(
                    w.winfo_ismapped() for w in self._status_labels()))
                rx1 = self.root.winfo_rootx() + self.root.winfo_width()
                for label in self._status_labels():
                    x1 = label.winfo_rootx() + label.winfo_width()
                    self.assertLessEqual(x1, rx1 + 1, f'{geometry} 越界: {label}')

    def _status_labels(self):
        """状态栏行内全部 Label（四字段 + 编辑器状态组 + 瞬态消息）。"""
        row = self.app.status_bar.winfo_children()[-1]
        out = []
        for cell in row.winfo_children():
            out.extend(w for w in cell.winfo_children()
                       if w.winfo_class() == 'Label')
            if cell.winfo_class() == 'Label':
                out.append(cell)
        return out


def panel_legend(app):
    """Git 标签页图例帧的子 Label（测试辅助，不暴露私有结构）。"""
    return [w for w in app.explorer.status_legend.winfo_children()
            if isinstance(w, tk.Label)]


if __name__ == '__main__':
    unittest.main()
