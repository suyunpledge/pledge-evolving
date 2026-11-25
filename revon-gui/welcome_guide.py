"""首次启动导航（2026-11-14）：语言 → 使用模式 → 一键连接模型。

整窗覆盖层：底下是满强度的梦幻极光与星点，中间一张液态玻璃卡。玻璃位图
直接画在极光画布上（PNG 带 alpha），亮边、透镜带与投影都与背后的色光叠合；
卡片内部取「玻璃色 × 背后极光色」，像磨砂玻璃透出底色。

只在真实启动且尚未完成引导时出现（见 revon_gui_v2.main），也可从命令面板
与配置页重新打开。完成时回调 ``on_finish(language, experience, saved)``。
"""
from __future__ import annotations

import tkinter as tk

import i18n
from i18n import tr
from gui_theme import (C, FONT_CAPTION, FONT_MONO_XS, FONT_SMALL, FONT_UI, FONT_UI_BOLD,
                       IS_WINDOWS, bind_keyboard_action, mix, ui_px)
import aurora
import liquid_glass as glass

EXPERIENCES = ("simple", "developer")
LANGUAGE_CHOICES = (("zh-CN", "中文", "简体中文界面"), ("en", "English", "English interface"))
CJK_TITLE = ("Microsoft YaHei UI" if IS_WINDOWS else "PingFang SC", 20, "bold")


class WelcomeGuide(tk.Frame):
    STEPS = ("language", "experience", "model")

    def __init__(self, root, *, home, language="zh-CN", experience="simple", on_finish=None,
                 on_language=None):
        super().__init__(root, bg=C["chat"])
        self.root = root
        self._home = home
        self._on_finish = on_finish
        self._on_language = on_language
        self.language = language if language in dict((c, n) for c, n, _ in LANGUAGE_CHOICES) else "zh-CN"
        self.experience = experience if experience in EXPERIENCES else "simple"
        self.saved = None
        self.step = 0
        px = lambda value: ui_px(root, value)  # noqa: E731
        self._px = px
        self.canvas = tk.Canvas(self, bg=C["chat"], highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._backdrop = aurora.AuroraBackdrop(self.canvas, strength=1.35, stars=True)
        self._card_item = self.canvas.create_image(0, 0, anchor="nw", tags=("card",))
        self._card_photo = None
        self._card_key = None
        self.card_fill = C["glass"]
        self.body = tk.Frame(self.canvas, bg=self.card_fill)
        self._body_item = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self._footer_brand = self.canvas.create_text(0, 0, text="REVON", fill=mix(C["chat"], "#FFFFFF", .35),
                                                     font=FONT_MONO_XS, anchor="s")
        self._layout_job = None
        self.canvas.bind("<Configure>", self._schedule_layout, add="+")
        self.body.bind("<Configure>", self._schedule_layout, add="+")
        self.bind("<Destroy>", self._cleanup, add="+")
        self.bind_all_keys()
        self._build_step()

    # ── 外壳 ──
    def show(self):
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self._schedule_layout()

    def bind_all_keys(self):
        self.bind("<Escape>", lambda _e: "break")

    def _schedule_layout(self, _event=None):
        if self._layout_job is None:
            try:
                self._layout_job = self.after_idle(self._layout)
            except tk.TclError:
                pass

    def _cleanup(self, event):
        if event.widget is self and self._layout_job is not None:
            try:
                self.after_cancel(self._layout_job)
            except tk.TclError:
                pass
            self._layout_job = None

    def _layout(self):
        self._layout_job = None
        try:
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        except tk.TclError:
            return
        if cw <= 20 or ch <= 20:
            return
        px = self._px
        pad, shadow, dy = px(26), px(22), px(8)
        width = min(px(680), cw - px(48))
        inner_w = width - 2 * pad
        self.canvas.itemconfigure(self._body_item, width=inner_w)
        self.body.update_idletasks()
        height = min(self.body.winfo_reqheight() + 2 * pad, ch - px(24))
        x = (cw - width) // 2
        y = max(px(12), (ch - height) // 2 - px(10))
        # 卡片内部色：玻璃色 × 背后极光色，像磨砂玻璃透出的底色。
        fill = self._target_fill()
        if fill != self.card_fill:
            self._repaint_fill(fill)
        key = (width, height, fill)
        if key != self._card_key:
            spec = glass.GlassSpec(radius=px(28), fill=fill, shadow=shadow, shadow_dy=dy,
                                   shadow_alpha=.5, rim=1.0, glow=1.15, band=10, sheen=1.0,
                                   sheen_h=px(22), bracket=.85, bracket_len=px(34),
                                   bracket_colour=C["ember"])
            self._card_photo = glass.glass_photo(self.canvas, width + 2 * shadow,
                                                 height + 2 * shadow + dy, spec)
            self.canvas.itemconfigure(self._card_item, image=self._card_photo)
            self._card_key = key
        self.canvas.coords(self._card_item, x - shadow, y - shadow + dy)
        self.canvas.coords(self._body_item, x + pad, y + pad)
        self.canvas.itemconfigure(self._body_item, height=height - 2 * pad)
        self.canvas.coords(self._footer_brand, cw / 2, ch - px(14))
        # 卡片几乎占满高度时不画底部字标（否则压在卡片上）。
        self.canvas.itemconfigure(self._footer_brand,
                                  state="normal" if y + height < ch - px(40) else "hidden")
        self.canvas.tag_raise("card")
        self.canvas.tag_raise(self._footer_brand)

    def _repaint_fill(self, fill):
        old = self.card_fill
        self.card_fill = fill

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    if str(child.cget("bg")).upper() == old.upper():
                        child.configure(bg=fill)
                except tk.TclError:
                    pass
                walk(child)
        try:
            self.body.configure(bg=fill)
        except tk.TclError:
            return
        walk(self.body)

    # ── 步骤 ──
    def _clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _target_fill(self):
        """卡片内部色只取画布中心的极光色：与卡片高度无关，换步骤时不跳色。"""
        try:
            cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        except tk.TclError:
            return self.card_fill
        if cw <= 20 or ch <= 20:
            try:
                cw, ch = self.root.winfo_width(), self.root.winfo_height()
            except tk.TclError:
                return self.card_fill
        tint = aurora.colour_at(cw, ch, cw / 2, ch / 2, strength=1.35)
        return mix(C["glass"], tint, .38)

    def _build_step(self):
        self._clear()
        px = self._px
        self.card_fill = self._target_fill()
        self.body.configure(bg=self.card_fill)
        fill = self.card_fill
        name = self.STEPS[self.step]
        head = tk.Frame(self.body, bg=fill)
        head.pack(fill=tk.X)
        i18n.Label(head, text=f"{self.step + 1:02d} / {len(self.STEPS):02d}", bg=fill,
                   fg=C["ember"], font=FONT_MONO_XS).pack(side=tk.LEFT)
        dots = tk.Canvas(head, width=px(84), height=px(8), bg=fill, highlightthickness=0, bd=0)
        for i in range(len(self.STEPS)):
            x0 = i * px(28)
            colour = C["ember"] if i <= self.step else mix(fill, "#FFFFFF", .16)
            dots.create_rectangle(x0, px(2), x0 + px(22), px(5), fill=colour, outline="")
        dots.pack(side=tk.RIGHT)
        titles = {
            "language": (tr("选择语言 · Choose your language"),
                         tr("之后可以在「配置」里随时切换。")),
            "experience": (tr("选择使用模式"),
                           tr("决定界面里能看到多少设置。之后可以在「配置」里切换。")),
            "model": (tr("连接一个模型"),
                      tr("参数已按推荐值填好，通常只需粘贴 API 密钥。也可以先跳过。")),
        }
        title, subtitle = titles[name]
        i18n.Label(self.body, text=title, bg=fill, fg=C["text"], font=CJK_TITLE,
                   anchor="w", justify=tk.LEFT).pack(fill=tk.X, pady=(px(10), px(2)))
        sub = i18n.Label(self.body, text=subtitle, bg=fill, fg=C["subtext"], font=FONT_SMALL,
                         anchor="w", justify=tk.LEFT)
        sub.pack(fill=tk.X, pady=(0, px(14)))
        sub.bind("<Configure>", lambda e, w=sub: w.configure(wraplength=max(40, e.width)))
        getattr(self, f"_step_{name}")()
        self._footer()
        self._schedule_layout()

    def _tile(self, parent, title, lines, *, selected, command, badge=None, icon=None):
        """可选卡片：玻璃胶囊底，选中时熔铁色亮边 + 对勾。"""
        px = self._px
        base = self.card_fill
        fill = mix(base, C["accent"], .14) if selected else mix(base, "#FFFFFF", .04)
        tile = tk.Frame(parent, bg=base, padx=px(16), pady=px(14), cursor="hand2")
        backdrop = glass.GlassBackdrop(tile, glass.GlassSpec(
            radius=px(16), fill=fill, rim=1.0 if selected else .6,
            rim_colour=C["ember"] if selected else "#FFFFFF", glow=.9, band=5, sheen=.8,
            sheen_h=px(14), inner_line=.05))
        tile._backdrop = backdrop
        top = tk.Frame(tile, bg=fill)
        top.pack(fill=tk.X)
        widgets = [top]
        if icon:
            from glass_ornaments import icon_chip
            chip = icon_chip(top, icon, size=px(30), bg=fill,
                             tone=C["ember"] if selected else C["subtext"])
            chip.pack(side=tk.LEFT, padx=(0, px(10)))
            widgets.append(chip)
        label = i18n.Label(top, text=title, bg=fill, fg=C["text"], font=(FONT_UI[0], 13, "bold"),
                           anchor="w")
        label.pack(side=tk.LEFT)
        widgets.append(label)
        if badge:
            tag = i18n.Label(top, text=badge, bg=C["accent_soft"], fg=C["accent_text"],
                             font=FONT_CAPTION, padx=px(6))
            tag.pack(side=tk.LEFT, padx=(px(8), 0))
        if selected:
            from ui_icons import IconCanvas
            check = IconCanvas(top, "check_circle", size=px(20), bg=fill, fg=C["ember"])
            check.pack(side=tk.RIGHT)
            widgets.append(check)
        for line in lines:
            text = i18n.Label(tile, text=line, bg=fill, fg=C["subtext"], font=FONT_SMALL,
                              anchor="w", justify=tk.LEFT)
            text.pack(fill=tk.X, pady=(px(4), 0))
            text.bind("<Configure>", lambda e, w=text: w.configure(wraplength=max(40, e.width)))
            widgets.append(text)
        for widget in (tile, *widgets):
            widget.bind("<Button-1>", lambda _e: command())
        bind_keyboard_action(tile, command)
        tile.configure(highlightthickness=0)
        return tile

    def _step_language(self):
        px = self._px
        grid = tk.Frame(self.body, bg=self.card_fill)
        grid.pack(fill=tk.X)
        for column, (code, name, hint) in enumerate(LANGUAGE_CHOICES):
            tile = self._tile(grid, name, (hint,), selected=self.language == code,
                              command=lambda c=code: self._choose_language(c), icon="globe")
            tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else px(12), 0))
            grid.grid_columnconfigure(column, weight=1, uniform="lang")

    def _step_experience(self):
        px = self._px
        grid = tk.Frame(self.body, bg=self.card_fill)
        grid.pack(fill=tk.X)
        choices = (
            ("simple", tr("普通模式"), "sparkles", tr("推荐"),
             (tr("开箱即用：一键配置模型，参数有推荐默认值"),
              tr("界面简洁，只保留对话、任务与常用工具"))),
            ("developer", tr("开发者模式"), "code", None,
             (tr("全部可自定义：模型配置文件、Provider JSON、采样参数"),
              tr("开放 Coding 工作台、演化与全部高级设置"))),
        )
        for column, (key, title, icon, badge, lines) in enumerate(choices):
            tile = self._tile(grid, title, lines, selected=self.experience == key, badge=badge,
                              icon=icon, command=lambda k=key: self._choose_experience(k))
            tile.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else px(12), 0))
            grid.grid_columnconfigure(column, weight=1, uniform="mode")

    def _step_model(self):
        from quick_setup import QuickModelSetup
        self.setup = QuickModelSetup(self.body, self._home, bg=self.card_fill,
                                     advanced=self.experience == "developer", compact=True,
                                     on_saved=self._model_saved, show_actions=False)
        self.setup.pack(fill=tk.X)

    def _footer(self):
        px = self._px
        fill = self.card_fill
        bar = tk.Frame(self.body, bg=fill)
        bar.pack(fill=tk.X, pady=(px(16), 0))
        last = self.step == len(self.STEPS) - 1
        primary_text = tr("开始使用") if last else tr("下一步")
        self.next_button = glass.glass_button(bar, primary_text, self.next, kind="primary",
                                              bg=fill, height=px(38), padx=26)
        self.next_button.pack(side=tk.RIGHT)
        if last:
            self.skip_button = glass.glass_button(bar, tr("跳过，稍后配置"), self.finish,
                                                  kind="quiet", bg=fill, height=px(38))
            self.skip_button.pack(side=tk.RIGHT, padx=(0, px(8)))
            test = glass.glass_button(bar, tr("测试连接"), lambda: self.setup.test(),
                                      kind="ghost", bg=fill, height=px(38))
            test.pack(side=tk.RIGHT, padx=(0, px(8)))
        if self.step > 0:
            back = glass.glass_button(bar, tr("上一步"), self.back, kind="ghost", bg=fill,
                                      height=px(38))
            back.pack(side=tk.LEFT)
        glass.focus_when_mapped(self.next_button)

    # ── 交互 ──
    def _choose_language(self, code):
        self.language = code
        if callable(self._on_language):
            self._on_language(code)
        else:
            locale = getattr(self.root, "_keystone_locale", None)
            if locale is not None:
                locale.switch(code)
        self._build_step()

    def _choose_experience(self, key):
        self.experience = key
        self._build_step()

    def _model_saved(self, result):
        self.saved = result
        try:
            self.next_button.configure(text=i18n.resolve(tr("开始使用 →"), self))
        except tk.TclError:
            pass

    def next(self):
        if self.step < len(self.STEPS) - 1:
            self.step += 1
            self._build_step()
            return
        setup = getattr(self, "setup", None)
        # 填了密钥却没点保存：「开始使用」顺手保存；保存失败就停在这一步。
        if self.saved is None and setup is not None and setup.key_var.get().strip():
            result = setup.save()
            if not result or not result.get("ok"):
                return
        self.finish()

    def back(self):
        if self.step > 0:
            self.step -= 1
            self._build_step()

    def finish(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        callback = self._on_finish
        language, experience, saved = self.language, self.experience, self.saved
        self.destroy()
        if callable(callback):
            callback(language, experience, saved)


__all__ = ["EXPERIENCES", "LANGUAGE_CHOICES", "WelcomeGuide"]
