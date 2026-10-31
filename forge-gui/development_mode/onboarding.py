"""Coding 上手清单（欢迎页顶部）：① 选模型 → ② 打开项目 → ③ 允许修改。

设计稿 ui-redesign-shots/mockup/coding-mode.html「新手第一次打开」。

* 只在需要时出现：没配模型，或者还没有任何近期项目（第一次用）；
  老用户直接看到原来的动作卡与近期项目。
* ① 在 Coding 里直接配模型（model_onboarding），不让新手去 Work Mode
  找「配置API」，也不出现「宿主」之类的词。
* ③ 只说明，不在这里授权：权限按项目授予、打开项目即重置为只读
  （app._project_loaded 的既有安全规则），授权入口在 Agent 面板。
"""
import threading
import tkinter as tk
import webbrowser
from pathlib import Path

from gui_theme import (C, FONT_CAPTION, FONT_SMALL, FONT_UI, FONT_UI_BOLD,
                       attach_tooltip, round_rect, ui_px)
from ui_icons import draw_icon

import model_onboarding as mo
from .buttons import dev_button


class SetupChecklist(tk.Frame):
    def __init__(self, master, *, home, on_open, on_clone=None, on_saved=None,
                 open_url=webbrowser.open, tester=mo.test_connection, saver=mo.apply_key,
                 model_only=False):
        super().__init__(master, bg=C['surface'], highlightthickness=1,
                         highlightbackground=C['line'])
        self._home = Path(home) if home else None
        self._on_open = on_open
        self._on_clone = on_clone
        self._on_saved = on_saved
        self._open_url = open_url
        self._tester = tester
        self._saver = saver
        self._vendor = mo.VENDORS[0].id
        self._editing_model = False
        self._busy = False
        self._tiles = {}
        self.key_var = tk.StringVar()
        self._model_only = model_only
        self._build()
        self.refresh()

    # ── 状态 ──────────────────────────────────────────────────────
    def model_ready(self):
        """没有模型配置来源（独立运行、未注入 home）时不拦新手：视为已就绪。"""
        return self._home is None or bool(mo.configured_models(self._home))

    def needed(self, has_recent):
        return not self.model_ready() or not has_recent

    @property
    def open_button(self):
        return self._open_btn

    # ── 构建 ──────────────────────────────────────────────────────
    def _build(self):
        pad = ui_px(self, 18)
        head = tk.Frame(self, bg=C['surface'])
        head.pack(fill='x', padx=pad, pady=(ui_px(self, 14), ui_px(self, 12)))
        tk.Label(head, text='开始之前', font=FONT_UI_BOLD, bg=C['surface'],
                 fg=C['text']).pack(side='left')
        tk.Label(head, text='两步就能让 Agent 开始干活', font=FONT_CAPTION, bg=C['surface'],
                 fg=C['faint']).pack(side='left', padx=(ui_px(self, 10), 0))
        self._progress = tk.Label(head, text='', font=FONT_CAPTION, bg=C['surface'], fg=C['faint'])
        self._progress.pack(side='right')
        self._rule(self)

        # ① 模型
        self._model_step = self._step(1, '选择一个 AI 模型',
                                      'Agent 靠它思考和写代码。选一家，粘贴 API 密钥即可；密钥只保存在本机。',
                                      tag='必需')
        body = self._model_body = tk.Frame(self._model_step.body, bg=C['surface'])
        grid = tk.Frame(body, bg=C['surface'])
        grid.pack(fill='x', pady=(ui_px(self, 10), 0))
        for index, vendor in enumerate(mo.VENDORS):
            tile = self._vendor_tile(grid, vendor)
            tile.grid(row=index // 3, column=index % 3, sticky='nsew',
                      padx=(0, ui_px(self, 8)), pady=(0, ui_px(self, 8)))
            grid.columnconfigure(index % 3, weight=1, uniform='vendor')
        self._key_label = tk.Label(body, text='', font=FONT_CAPTION, bg=C['surface'],
                                   fg=C['subtext'], anchor='w')
        self._key_label.pack(fill='x', pady=(ui_px(self, 2), ui_px(self, 4)))
        key_row = self._key_row = tk.Frame(body, bg=C['surface'])
        key_row.pack(fill='x')
        self.key_entry = tk.Entry(key_row, textvariable=self.key_var, show='•', font=FONT_UI,
                                  bg=C['chat'], fg=C['text'], insertbackground=C['text'],
                                  relief='flat', bd=0, highlightthickness=1,
                                  highlightbackground=C['border_hi'], highlightcolor=C['accent_border'])
        self.key_entry.pack(side='left', fill='x', expand=True, ipady=ui_px(self, 6))
        self.key_entry.bind('<Return>', lambda _e: self.save())
        attach_tooltip(self.key_entry, '粘贴所选厂商的 API 密钥（只保存在本机密钥库）')
        self.test_btn = dev_button(key_row, '测试连接', self.test, kind='ghost')
        self.test_btn.pack(side='left', padx=(ui_px(self, 8), 0))
        self.save_btn = dev_button(key_row, '保存并继续', self.save, kind='primary')
        self.save_btn.pack(side='left', padx=(ui_px(self, 6), 0))
        self._help = tk.Label(body, text='', font=FONT_CAPTION, bg=C['surface'], fg=C['accent2'],
                              cursor='hand2', anchor='w')
        self._help.pack(fill='x', pady=(ui_px(self, 6), 0))
        self._help.bind('<Button-1>', lambda _e: self._open_console())
        self.feedback = tk.Label(body, text='', font=FONT_CAPTION, bg=C['surface'], fg=C['subtext'],
                                 anchor='w', justify='left')
        self.feedback.pack(fill='x', pady=(ui_px(self, 2), 0))
        self._model_done = tk.Frame(self._model_step.body, bg=C['surface'])
        self._model_done_lbl = tk.Label(self._model_done, text='', font=FONT_SMALL,
                                        bg=C['surface'], fg=C['subtext'])
        self._model_done_lbl.pack(side='left')
        change = tk.Label(self._model_done, text='换一个', font=FONT_SMALL, bg=C['surface'],
                          fg=C['accent2'], cursor='hand2')
        change.pack(side='left', padx=(ui_px(self, 10), 0))
        change.bind('<Button-1>', lambda _e: self._edit_model())
        if self._model_only:
            # 弹窗只要第 ① 步：不建 ② ③，表单始终展开。
            self._editing_model = True
            self._select_vendor(self._vendor)
            return
        self._rule(self)

        # ② 项目
        step = self._step(2, '打开一个项目', 'Agent 只会在你选择的文件夹里工作。')
        row = tk.Frame(step.body, bg=C['surface'])
        row.pack(fill='x', pady=(ui_px(self, 10), 0))
        self._open_btn = dev_button(row, '打开文件夹', self._on_open, kind='primary', icon='files')
        self._open_btn.pack(side='left')
        if self._on_clone is not None:
            dev_button(row, '从 Git 克隆', self._on_clone, kind='ghost', icon='diff').pack(
                side='left', padx=(ui_px(self, 8), 0))
        self._project_step = step
        self._rule(self)

        # ③ 权限（只说明，授权在项目打开后）
        self._step(3, '允许 Agent 修改文件',
                   '打开项目后，在右侧Agent面板点「允许修改」。'
                   '默认只看不改，每个项目单独授权。', muted=True)
        self._select_vendor(self._vendor)

    def _rule(self, parent):
        tk.Frame(parent, bg=C['line'], height=1).pack(fill='x')

    def _step(self, number, title, desc, *, tag=None, muted=False):
        pad = ui_px(self, 18)
        row = tk.Frame(self, bg=C['surface'])
        row.pack(fill='x', padx=pad, pady=ui_px(self, 14))
        size = ui_px(self, 26)
        badge = tk.Canvas(row, width=size, height=size, bg=C['surface'], highlightthickness=0, bd=0)
        badge.pack(side='left', anchor='n', padx=(0, ui_px(self, 14)))
        col = tk.Frame(row, bg=C['surface'])
        col.pack(side='left', fill='x', expand=True)
        title_row = tk.Frame(col, bg=C['surface'])
        title_row.pack(fill='x')
        tk.Label(title_row, text=title, font=FONT_UI_BOLD, bg=C['surface'],
                 fg=C['subtext'] if muted else C['text']).pack(side='left')
        if tag:
            tk.Label(title_row, text=tag, font=FONT_CAPTION, bg=C['warn_soft'], fg=C['warn'],
                     padx=ui_px(self, 6)).pack(side='left', padx=(ui_px(self, 8), 0))
        desc_lbl = tk.Label(col, text=desc, font=FONT_SMALL, bg=C['surface'], fg=C['faint'] if muted
                            else C['subtext'], anchor='w', justify='left')
        desc_lbl.pack(fill='x', pady=(ui_px(self, 2), 0))
        desc_lbl.bind('<Configure>', lambda e, w=desc_lbl: w.configure(wraplength=max(120, e.width)))
        row.badge, row.number, row.body = badge, number, col
        self._paint_badge(row, 'todo')
        return row

    def _paint_badge(self, step, state):
        """state: todo / current / done。"""
        cv, size = step.badge, int(step.badge.cget('width'))
        cv.delete('all')
        if state == 'done':
            cv.create_oval(1, 1, size - 1, size - 1, fill=C['ok'], outline=C['ok'])
            glyph = ui_px(self, 14)
            draw_icon(cv, 'check', x=(size - glyph) / 2, y=(size - glyph) / 2, size=glyph, fg='#0B1A12')
            return
        current = state == 'current'
        cv.create_oval(1, 1, size - 1, size - 1, width=max(1, ui_px(self, 1.5)),
                       fill=C['accent_soft'] if current else C['surface'],
                       outline=C['accent2'] if current else C['border_hi'])
        cv.create_text(size / 2, size / 2, text=str(step.number), font=FONT_UI_BOLD,
                       fill=C['accent2'] if current else C['subtext'])

    def _vendor_tile(self, parent, vendor):
        tile = tk.Frame(parent, bg=C['chat'], highlightthickness=1, highlightbackground=C['line'],
                        cursor='hand2', padx=ui_px(self, 10), pady=ui_px(self, 8))
        size = ui_px(self, 26)
        logo = tk.Canvas(tile, width=size, height=size, bg=C['chat'], highlightthickness=0, bd=0)
        round_rect(logo, 0, 0, size - 1, size - 1, ui_px(self, 7), fill=vendor.color, outline='')
        logo.create_text(size / 2, size / 2, text=vendor.letter, fill='#FFFFFF', font=FONT_UI_BOLD)
        logo.pack(side='left', padx=(0, ui_px(self, 10)))
        text = tk.Frame(tile, bg=C['chat'])
        text.pack(side='left', fill='x', expand=True)
        name = tk.Label(text, text=vendor.name, font=FONT_UI_BOLD, bg=C['chat'], fg=C['text'], anchor='w')
        name.pack(fill='x')
        # 「推荐」放在说明行里（绿色），不和名字抢同一行的宽度。
        line = tk.Frame(text, bg=C['chat'])
        line.pack(fill='x')
        parts = [tile, logo, text, name, line]
        if vendor.badge:
            badge = tk.Label(line, text=vendor.badge, font=FONT_CAPTION, bg=C['chat'], fg=C['ok'])
            badge.pack(side='left', padx=(0, ui_px(self, 4)))
            parts.append(badge)
        blurb = tk.Label(line, text=vendor.blurb, font=FONT_CAPTION, bg=C['chat'], fg=C['faint'], anchor='w')
        blurb.pack(side='left')
        parts.append(blurb)
        for widget in parts:
            widget.bind('<Button-1>', lambda _e, v=vendor.id: self._select_vendor(v))
        tile.parts = parts
        self._tiles[vendor.id] = tile
        return tile

    # ── 交互 ──────────────────────────────────────────────────────
    def _select_vendor(self, vendor_id):
        self._vendor = vendor_id
        vendor = mo.vendor(vendor_id)
        for vid, tile in self._tiles.items():
            on = vid == vendor_id
            fill = C['accent_soft'] if on else C['chat']
            tile.configure(highlightbackground=C['accent_border'] if on else C['line'])
            for widget in tile.parts:
                widget.configure(bg=fill)
        if vendor.keyless:
            self.key_entry.configure(state='disabled', disabledbackground=C['raised'])
            self.key_var.set('')
            self._help.configure(text='本地模型无需密钥；请确认 Ollama 已在本机运行。', cursor='arrow', fg=C['faint'])
            self._key_label.configure(text='本地模型不需要密钥，直接保存即可')
        else:
            self.key_entry.configure(state='normal')
            self._help.configure(text=f'还没有密钥？打开 {vendor.name} 控制台获取 ↗', cursor='hand2',
                                 fg=C['accent2'])
            self._key_label.configure(text=f'粘贴 {vendor.name} 的 API 密钥（形如 sk-…）')
        self._say('')

    def _open_console(self):
        vendor = mo.vendor(self._vendor)
        if vendor and vendor.console:
            self._open_url(vendor.console)

    def _say(self, text, tone='subtext'):
        self.feedback.configure(text=text, fg=C[tone])

    def _set_busy(self, busy):
        self._busy = busy
        for button in (self.test_btn, self.save_btn):
            button.configure(state='disabled' if busy else 'normal')

    def test(self):
        if self._busy:
            return
        vendor_id, key = self._vendor, self.key_var.get()
        self._set_busy(True)
        self._say('正在连接…', 'faint')
        result = []
        threading.Thread(target=lambda: result.append(self._tester(vendor_id, key)),
                         daemon=True, name='Forge-model-test').start()

        def poll():
            if not self.winfo_exists():
                return
            if not result:
                self.after(120, poll)
                return
            self._set_busy(False)
            outcome = result[0]
            self._say(('✓ ' + outcome['message']) if outcome['ok'] else outcome['error'],
                      'ok' if outcome['ok'] else 'error')
        self.after(120, poll)

    def save(self):
        if self._busy or self._home is None:
            return
        outcome = self._saver(self._home, self._vendor, self.key_var.get())
        if not outcome['ok']:
            self._say(outcome['error'], 'error')
            return
        self.key_var.set('')
        self._editing_model = False
        self._say('')
        self.refresh()
        if self._on_saved is not None:
            self._on_saved(outcome)

    def _edit_model(self):
        self._editing_model = True
        self.refresh()

    def refresh(self):
        ready = self.model_ready()
        if self._model_only:
            if not self._model_body.winfo_manager():
                self._model_body.pack(fill='x')
            self._paint_badge(self._model_step, 'done' if ready else 'current')
            self._progress.configure(text='')
            return
        if self._home is None:
            self._model_step.pack_forget()
        show_form = not ready or self._editing_model
        if show_form:
            self._model_done.pack_forget()
            if not self._model_body.winfo_manager():
                self._model_body.pack(fill='x')
        else:
            self._model_body.pack_forget()
            names = [mo.vendor(i).name if mo.vendor(i) else i
                     for i in mo.configured_models(self._home)] if self._home else []
            self._model_done_lbl.configure(text='已配置：' + '、'.join(names[:4]) if names else '已配置')
            if not self._model_done.winfo_manager():
                self._model_done.pack(fill='x', pady=(ui_px(self, 6), 0))
        self._paint_badge(self._model_step, 'done' if ready else 'current')
        self._paint_badge(self._project_step, 'current' if ready else 'todo')
        done = int(ready)
        self._progress.configure(text=f'{done} / 2')
