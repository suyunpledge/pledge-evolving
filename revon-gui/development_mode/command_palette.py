"""Ctrl+Shift+P 命令面板：复用快速打开浮层（输入即筛、↑↓、Enter、Esc、点外关闭）。

命令清单由 app 每次打开时现取（``DevelopmentWorkbench._palette_commands``），
可用性随当前状态变化：不可用的命令仍列出、置灰并说明原因——让人知道
「有这个功能，只是现在还差一步」，而不是找不到。
"""
import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from gui_theme import C, FONT_CAPTION, FONT_UI

from .quick_open import QuickOpen, ROW_HEIGHT, VISIBLE_ROWS, rank


@dataclass(frozen=True)
class Command:
    key: str
    label: str
    group: str
    run: Callable[[], object]
    shortcut: str = ''
    unavailable: str = ''   # 非空 = 当前不可用的原因


class CommandPalette(QuickOpen):
    HINTS = (('↑↓', '选择'), ('Enter', '执行'), ('Esc', '关闭'))
    STATUS = '输入关键字筛选命令'

    def __init__(self, master, commands, *, on_close=None, on_unavailable=None):
        self._commands = {command.label: command for command in commands}
        self._on_unavailable = on_unavailable
        self.ran = None
        super().__init__(master, [command.label for command in commands], self._run,
                         on_close=on_close)

    def _run(self, label):
        command = self._commands.get(label)
        if command is None:
            return
        if command.unavailable:
            if self._on_unavailable is not None:
                self._on_unavailable(command)
            return
        self.ran = command.key
        command.run()

    def refresh(self):
        self._results = rank(self._paths, self.query.get())
        self._selected = 0
        try:
            self._empty.pack_forget()
            self._build_rows()
        except tk.TclError:
            return
        if not self._results:
            self.count.configure(text='无匹配')
            self._empty.configure(text='没有匹配的命令')
            self._empty.pack(pady=14)
        else:
            self.count.configure(text=f'{len(self._results)} 个命令')
            self._paint()
            hidden = len(self._results) - min(VISIBLE_ROWS, len(self._results))
            self.status.configure(text=f'还有 {hidden} 条，继续输入可缩小范围' if hidden else self.STATUS)
        self._resize()

    def _build_rows(self):
        for row in self._rows:
            row[0].destroy()
        self._rows = []
        for index, label_text in enumerate(self._results[:VISIBLE_ROWS]):
            command = self._commands[label_text]
            holder = tk.Frame(self._list, bg=C['surface'], height=ROW_HEIGHT)
            holder.pack(fill='x')
            holder.pack_propagate(False)
            label = tk.Label(holder, text=label_text, font=FONT_UI, bg=C['surface'],
                             fg=C['faint'] if command.unavailable else C['text'],
                             anchor='w', padx=0, bd=0)
            label.pack(side='left', fill='x', expand=True, padx=(12, 0))
            right = command.unavailable or '   '.join(p for p in (command.group, command.shortcut) if p)
            tail = tk.Label(holder, text=right, font=FONT_CAPTION, bg=C['surface'],
                            fg=C['warn'] if command.unavailable else C['ter'],
                            anchor='e', padx=0, bd=0)
            tail.pack(side='right', padx=(10, 12))
            for widget in (holder, label, tail):
                widget.bind('<Button-1>', lambda _e, i=index: self._pick(i))
                widget.bind('<Enter>', lambda _e, i=index: self._hover(i, True))
                widget.bind('<Leave>', lambda _e, i=index: self._hover(i, False))
                widget.configure(cursor='hand2')
            self._rows.append((holder, label, tail))

    def _paint(self):
        for index, (holder, label, tail) in enumerate(self._rows):
            active = index == self._selected
            command = self._commands[self._results[index]]
            bg = C['sel'] if active else C['surface']
            try:
                holder.configure(bg=bg)
                label.configure(bg=bg, fg=C['faint'] if command.unavailable else
                                ('#FFFFFF' if active else C['text']))
                tail.configure(bg=bg)
            except tk.TclError:
                pass

    def _complete(self):
        if self._results:
            self.query.set(self._results[self._selected])
            try:
                self.entry.icursor('end')
            except tk.TclError:
                pass
        return 'break'
