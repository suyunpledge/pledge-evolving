"""布局域回归测试：P0-L1 Agent 栏溢出 + pane 顺序 + 增量挂载守护。

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

本文件即上述修复的守护，也是集成验收口径「P0-L1 两尺寸映射断言」的落地。
只测布局容器层，不触碰 Agent 栏内部控件语义（Codex 域）。
"""
import time
import unittest
from pathlib import Path
import tempfile
import tkinter as tk
import _tkinter

from gui_theme import apply_ttk_theme
from forge.development.state import ModeState
from development_mode.app import DevelopmentWorkbench


class _WorkbenchFixture:
    """只借 WorkbenchTests 的真实 Tk 夹具与 step/pump 辅助，不带它的用例。

    直接继承 WorkbenchTests 会让 pytest 把父类全部用例重跑一遍
    （test_app 全量 90s+，直接超时），故用 mixin 只取夹具。
    """

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


if __name__ == '__main__':
    unittest.main()
