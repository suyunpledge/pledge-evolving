"""Interactive visual specimens, not a second IDE or execution runtime."""
import tkinter as tk
from tkinter import ttk

import gui_theme as theme
from ui_icons import IconCanvas
from .components import ModeSwitcher, WorkspaceDeck, NoticeBanner
from .contracts import DENSITIES, NoticeSpec, WorkspaceKind
from .tokens import color, font, px


class VisualSystemDemo(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=color('surface.root'))
        self.buffers = {}
        self.tick = 0
        self._timer = None
        self._closed = False
        chrome = tk.Frame(self, bg=color('surface.root'), padx=px(self, 16), pady=px(self, 8))
        chrome.pack(fill=tk.X)
        tk.Label(chrome, text='Forge', font=theme.FONT_BRAND, bg=color('surface.root'),
                 fg=color('text.primary')).pack(side=tk.LEFT, padx=(0, px(self, 20)))
        self.switcher = ModeSwitcher(chrome, self.select)
        self.switcher.pack(side=tk.LEFT)
        tk.Label(chrome, text='Visual System 2.0 · 交互样例', bg=color('surface.root'),
                 fg=color('text.secondary'), font=font('caption')).pack(side=tk.RIGHT)
        self.deck = WorkspaceDeck(self, {WorkspaceKind.WORK: self._work, WorkspaceKind.CODING: self._coding})
        self.deck.pack(fill=tk.BOTH, expand=True)
        NoticeBanner(self, NoticeSpec('info', '后台状态示例',
            '切换保留草稿、文件标签与分栏；本样例不调用 Agent、不执行命令、不读取项目。',
            WorkspaceKind.WORK)).pack(fill=tk.X, padx=px(self, 12), pady=px(self, 8))
        self.bind('<Destroy>', self._destroyed, add='+')
        self.select(WorkspaceKind.WORK)
        self._tick()

    def select(self, kind):
        self.deck.select(kind)
        self.switcher.set_selected(kind)

    def _label(self, parent, text, *, role='label', bg=None):
        return tk.Label(parent, text=text, font=font(role), fg=color('text.body'),
                        bg=bg or color('surface.root'), anchor='w', justify=tk.LEFT)

    def _nav(self, parent, items, *, width):
        bar = tk.Frame(parent, bg=color('surface.navigation'), width=px(self, width))
        bar.pack(side=tk.LEFT, fill=tk.Y)
        bar.pack_propagate(False)
        for icon, label in items:
            theme.control_button(bar, text=label, icon=icon, font=font('label'),
                command=lambda: None, bg=color('surface.navigation'), anchor='w',
                icon_size=px(self, 16), padx=px(self, 12), pady=px(self, 8)).pack(fill=tk.X, padx=px(self, 8), pady=px(self, 4))
        return bar

    def _text(self, parent, value, *, code=False, height=5):
        widget = tk.Text(parent, height=height, wrap='none' if code else 'word',
                         bg=color('surface.editor' if code else 'surface.input'),
                         fg=color('text.body'), insertbackground=color('text.primary'),
                         font=font('code' if code else 'body'), relief=tk.FLAT, undo=True,
                         padx=px(self, 12), pady=px(self, 8),
                         highlightthickness=1, highlightbackground=color('border.default'),
                         highlightcolor=color('state.focus'))
        widget.insert('1.0', value)
        return widget

    def _work(self, parent):
        density = DENSITIES[WorkspaceKind.WORK]
        nav = self._nav(parent, [('chat', '会话'), ('task', '任务'), ('agents', 'Agent 协作'),
                                 ('tools', '自动化')], width=200)
        self._label(nav, '最近会话', bg=color('surface.navigation')).pack(fill=tk.X, padx=px(self, 20), pady=px(self, 16))
        self._label(nav, '项目交付简报\n每周资料整理', bg=color('surface.navigation')).pack(fill=tk.X, padx=px(self, 20))
        center = tk.Frame(parent, bg=color('surface.root'), padx=px(self, density.gutter))
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        column = tk.Frame(center, bg=color('surface.root'))
        column.pack(fill=tk.BOTH, expand=True)
        theme.bind_packed_column(column, max_width=theme.CONTENT_WIDTH, gutter=0)
        center = column
        self._label(center, '项目交付简报', role='title').pack(fill=tk.X, pady=px(self, 16))
        self.work_status = self._label(center, '示例后台状态')
        self.work_status.pack(fill=tk.X)
        conversation = self._text(center, '你\n请整理本周项目进展。\n\nForge\n'
            '我会先整理任务和已有材料，再生成可以审阅的交付简报。\n'
            '必要的任务细节在右侧按需打开，日常对话保持简洁。', height=10)
        conversation.configure(state='disabled', bg=color('surface.root'), highlightthickness=0)
        conversation.pack(fill=tk.BOTH, expand=True, pady=px(self, 12))
        draft = self._text(center, '继续补充说明…', height=4)
        draft.pack(fill=tk.X, pady=(0, px(self, 16)))
        self.buffers['work'] = draft

    def _coding(self, parent):
        self._nav(parent, [('files', '文件'), ('search', '搜索'), ('diff', 'Git'), ('play', '调试')], width=90)
        body = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.coding_panes = body
        files = ttk.Frame(body)
        self.project_label = self._label(files, 'project-example')
        self.project_label.pack(fill=tk.X, padx=px(self, 8), pady=px(self, 8))
        ttk.Style(self).configure('ForgeCoding.Treeview', rowheight=px(self, DENSITIES[WorkspaceKind.CODING].row_height))
        tree = ttk.Treeview(files, show='tree', selectmode='browse', style='ForgeCoding.Treeview')
        source = tree.insert('', 'end', text='src', open=True)
        tree.insert(source, 'end', text='runner.py · 已修改')
        tree.insert(source, 'end', text='models.py')
        tree.insert('', 'end', text='tests')
        tree.pack(fill=tk.BOTH, expand=True)
        body.add(files, weight=1)
        center = ttk.Panedwindow(body, orient=tk.VERTICAL)
        body.add(center, weight=4)
        self.editor_stack = center
        notebook = ttk.Notebook(center)
        center.add(notebook, weight=4)
        self.editor_tabs = notebook
        for name, value in [('runner.py *', 'def run_task(task):\n    # 示例草稿；不操作真实文件\n    return task\n'),
                            ('models.py', 'class ModelRoute:\n    pass\n')]:
            tab = ttk.Frame(notebook)
            editor = self._text(tab, value, code=True, height=18)
            editor.pack(fill=tk.BOTH, expand=True)
            notebook.add(tab, text=name)
            self.buffers[name] = editor
        bottom = ttk.Notebook(center)
        center.add(bottom, weight=1)
        for name, value in [('终端', '终端布局样例；未执行命令。'), ('任务', '示例任务 · 等待审阅'),
                            ('Diff', '- old_value\n+ updated_value'), ('问题', '问题面板布局样例')]:
            tab = ttk.Frame(bottom)
            self._label(tab, value).pack(fill=tk.BOTH, expand=True, padx=px(self, 12), pady=px(self, 8))
            bottom.add(tab, text=name)
        ai = ttk.Frame(body)
        body.add(ai, weight=2)
        self._label(ai, 'AI 助手', role='title').pack(fill=tk.X, padx=px(self, 12), pady=px(self, 12))
        self._label(ai, '开发会话 · project-example\n代码、工具与测试状态按任务关联。').pack(fill=tk.X, padx=px(self, 12))
        NoticeBanner(ai, NoticeSpec('warn', '权限提示样例',
            '项目：project-example\n操作：写入文件\n等待明确批准；切换模式不授予权限。',
            WorkspaceKind.CODING, 'example-task')).pack(fill=tk.X, padx=px(self, 8), pady=px(self, 16))
        draft = self._text(ai, '解释当前改动…', height=5)
        draft.pack(fill=tk.BOTH, expand=True, padx=px(self, 8), pady=px(self, 8))
        self.buffers['coding-chat'] = draft

    def _tick(self):
        if self._closed:
            return
        self.tick += 1
        self.work_status.configure(text=f'示例后台状态 · 心跳 {self.tick} · 切换不暂停')
        self._timer = self.after(250, self._tick)

    def _destroyed(self, event):
        if event.widget == self:
            self._closed = True
            if self._timer is not None:
                self.after_cancel(self._timer)


def main():
    root = tk.Tk()
    root.title('Forge Visual System 2.0 — 双工作空间布局样例')
    root.geometry('1360x850')
    root.minsize(920, 620)
    theme.apply_ttk_theme(root)
    VisualSystemDemo(root).pack(fill=tk.BOTH, expand=True)
    root.mainloop()


if __name__ == '__main__':
    main()
