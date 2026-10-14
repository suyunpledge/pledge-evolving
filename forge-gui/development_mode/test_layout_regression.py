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

from gui_theme import apply_ttk_theme
from forge.development.state import ModeState
from development_mode.app import DevelopmentWorkbench
from development_mode import explorer
from development_mode.explorer import ExplorerPanel
from development_mode.layout import StatusBar


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
                self.step()
                self.app.apply_layout()
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
                self.step()
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
        """log_tabs 必须是 agent_frame 内唯一 expand，否则固定组会被压缩。

        Codex 二轮起 Agent 区由 agent_panel.AgentPanel(Frame) 整体承载，
        notebook 在容器内部 expand——断言适配两级结构：agent_frame 的唯一
        expand 是 AgentPanel 容器，且 log_tabs 别名指向其内部 notebook。"""
        expanding = [c for c in self._agent_children()
                     if c.pack_info().get('expand')]
        self.assertEqual(len(expanding), 1)
        from development_mode.agent_panel import AgentPanel
        self.assertIsInstance(expanding[0], AgentPanel)
        self.assertIs(self.app.log_tabs, self.app.agent_panel.notebook)

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

    def test_bottom_panel_keeps_all_six_pages(self):
        """v0.2：六 Tab（差异/测试/问题/输出/能力/审批）替代旧五 Tab。"""
        self.assertEqual(len(self.app.bottom.views), 6)
        for name in ('差异', '测试', '问题', '输出', '能力', '审批'):
            self.assertIn(name, self.app.bottom.views)
        # write_output 旧名「输出」仍生效（兼容别名）。
        self.app.write_output('输出', 'M1 冒烟')
        self.step()
        self.assertIn('M1 冒烟', self.app.bottom.views['输出'].get('1.0', 'end'))

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
        # v0.2：活动栏 9 图标（任务 / 知识 / 演化 / 审批 四个新 Tab）。
        self.assertEqual([key for key, _g, _l in bar.ITEMS],
                         ['files', 'search', 'git', 'tasks', 'run',
                          'knowledge', 'evolution', 'approvals', 'settings'])
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
        # v0.2：占位入口从「扩展」改为「设置」——活动栏切到 settings 时
        # notebook 收起，由 placeholder 接管（与 v0.1 行为对齐）。
        bar.select('settings')
        self.pump(lambda: panel._placeholder.winfo_ismapped())
        self.assertEqual(panel._placeholder_title.cget('text'), '设置')
        bar.select('files')
        # pump 同时等 notebook 重新映射（不是仅等 placeholder 收起）。
        self.pump(lambda: not panel._placeholder.winfo_ismapped()
                  and panel.notebook.winfo_ismapped())
        self.assertTrue(panel.notebook.winfo_ismapped())
        # v0.2：任务 / 知识 / 演化切到对应 notebook Tab（真实 Tab，非占位）。
        for key, expected in (('tasks', '任务'),
                              ('knowledge', '知识'),
                              ('evolution', '演化')):
            bar.select(key)
            self.pump(lambda: panel.notebook.winfo_ismapped())
            self.assertEqual(
                panel.notebook.tab(panel.notebook.select(), 'text'),
                expected,
                f'{key} → {expected} 切到了 {panel.notebook.tab(panel.notebook.select(), "text")}')
        # v0.2：审批触发 _show_approvals_tab（不要求 bottom 一定就绪，
        # 只验证不抛异常；bottom.install_*_view 在集成测试里覆盖）。
        bar.select('approvals')

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


def panel_legend(app):
    """Git 标签页图例帧的子 Label（测试辅助，不暴露私有结构）。"""
    return [w for w in app.explorer.status_legend.winfo_children()
            if isinstance(w, tk.Label)]


if __name__ == '__main__':
    unittest.main()
