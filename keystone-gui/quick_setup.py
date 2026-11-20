"""一键配置模型（2026-11-14）：引导页与「普通模式」配置页共用。

选厂商 → 参数已按推荐值填好（模型、接口地址、温度），通常只需粘贴 API 密钥
→ 「测试连接」（可选）→ 「一键保存」。写入走 ``model_onboarding.apply_key``：
用户层 keystone.patch.json 只存密钥引用，明文密钥进密钥库。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

import i18n
import model_onboarding as mo
from i18n import tr
from gui_theme import (C, FONT_CAPTION, FONT_MICRO, FONT_MONO_SM, FONT_SMALL, FONT_UI,
                       FONT_UI_BOLD, bind_keyboard_action, mix, ui_px)

# 厂商卡上的短说明：源文案在 model_onboarding（不依赖 Tk），这里登记可翻译版本。
BLURBS = {
    "deepseek": tr("写代码强·便宜"),
    "moonshot": tr("长上下文"),
    "zhipu": tr("国内直连"),
    "qwen": tr("阿里云百炼"),
    "anthropic": tr("需海外网络"),
    "ollama": tr("Ollama 免密钥"),
}
NAMES = {"zhipu": tr("智谱 GLM"), "qwen": tr("通义千问"), "ollama": tr("本地模型")}
# model_onboarding 返回的固定错误文案（动态拼接的保持原样）。
_ERRORS = {
    "请先粘贴 API 密钥": tr("请先粘贴 API 密钥"),
    "密钥里有空格或换行，请重新复制": tr("密钥里有空格或换行，请重新复制"),
    "接口地址需以 http:// 或 https:// 开头": tr("接口地址需以 http:// 或 https:// 开头"),
    "密钥无效或没有权限，请检查是否复制完整": tr("密钥无效或没有权限，请检查是否复制完整"),
}


def _localize(message: str):
    return _ERRORS.get(message, message)


class QuickModelSetup(tk.Frame):
    """厂商条 + 参数表 + 测试 / 保存。``on_saved(result)`` 在保存成功后调用。"""

    def __init__(self, parent, home, *, bg=None, on_saved=None, advanced=True,
                 compact=False, show_actions=True):
        base = bg or C["surface"]
        super().__init__(parent, bg=base)
        self._base = base
        self._home = Path(home)
        self._on_saved = on_saved
        self._advanced = advanced
        self._events: queue.Queue = queue.Queue()
        self._poll_job = None
        self._busy = False
        self.vendor_id = mo.DEFAULT_VENDOR
        px = lambda value: ui_px(self, value)  # noqa: E731
        self._px = px
        field_bg = mix(base, "#000000", .28)
        self._field_bg = field_bg

        # ── 厂商条 ──
        i18n.Label(self, text=tr("选择模型服务"), bg=base, fg=C["subtext"],
                   font=FONT_CAPTION).pack(anchor="w")
        strip = tk.Frame(self, bg=base)
        strip.pack(fill=tk.X, pady=(px(6), px(8)))
        self._vendor_cards = {}
        columns = 3 if compact else 6
        for index, v in enumerate(mo.VENDORS):
            card = self._vendor_card(strip, v)
            card.grid(row=index // columns, column=index % columns, sticky="nsew",
                      padx=(0 if index % columns == 0 else px(6), 0), pady=(0, px(6)))
            strip.grid_columnconfigure(index % columns, weight=1, uniform="vendor")
            self._vendor_cards[v.id] = card

        # ── 参数 ──
        form = tk.Frame(self, bg=base)
        form.pack(fill=tk.X)
        form.grid_columnconfigure(1, weight=1)
        self.key_var = tk.StringVar(self)
        self.model_var = tk.StringVar(self)
        self.url_var = tk.StringVar(self)
        self.temperature_var = tk.StringVar(self, value=f"{mo.DEFAULT_TEMPERATURE:g}")
        row = 0
        self.key_entry = self._entry(form, row, tr("API 密钥"), self.key_var, show="•")
        self._key_link = i18n.Label(form, text=tr("获取密钥 ↗"), bg=base, fg=C["link"],
                                    font=FONT_CAPTION, cursor="hand2")
        self._key_link.grid(row=row, column=2, sticky="w", padx=(px(8), 0))
        self._key_link.bind("<Button-1>", lambda _e: self._open_console())
        bind_keyboard_action(self._key_link, self._open_console)
        row += 1
        model_label = i18n.Label(form, text=tr("模型"), bg=base, fg=C["subtext"], font=FONT_SMALL)
        model_label.grid(row=row, column=0, sticky="w", pady=px(4))
        self.model_combo = ttk.Combobox(form, textvariable=self.model_var, font=FONT_MONO_SM)
        self.model_combo.grid(row=row, column=1, sticky="ew", pady=px(4), ipady=px(2))
        row += 1
        self._advanced_rows = []
        url_entry = self._entry(form, row, tr("接口地址"), self.url_var, mono=True)
        self._advanced_rows.append(row)
        row += 1
        temp_entry = self._entry(form, row, tr("温度"), self.temperature_var, mono=True, width=8,
                                 stretch=False)
        i18n.Label(form, text=tr("默认 0.7：Agent 场景最稳"), bg=base, fg=C["faint"],
                   font=FONT_CAPTION).grid(row=row, column=2, sticky="w", padx=(px(8), 0))
        self._advanced_rows.append(row)
        self._form = form
        # 记下这两行的全部控件：grid_remove 后 grid_slaves() 不再列出它们。
        self._advanced_widgets = [w for w in form.grid_slaves()
                                  if int(w.grid_info().get("row", -1)) in self._advanced_rows]
        self.set_advanced(advanced)

        # ── 操作 ──
        import liquid_glass as glass
        actions = tk.Frame(self, bg=base)
        actions.pack(fill=tk.X, pady=(px(14) if show_actions else px(6), 0))
        self.save_button = self.test_button = None
        if show_actions:
            # 引导页把「测试 / 保存」并进自己的底栏，这里只留状态行。
            self.save_button = glass.glass_button(actions, tr("一键保存"), self.save,
                                                  kind="primary", bg=base, height=px(36))
            self.save_button.pack(side=tk.RIGHT)
            self.test_button = glass.glass_button(actions, tr("测试连接"), self.test,
                                                  kind="ghost", bg=base, height=px(36))
            self.test_button.pack(side=tk.RIGHT, padx=(0, px(8)))
        self.status_var = i18n.StringVar(self, value="")
        self.status = i18n.Label(actions, textvariable=self.status_var, bg=base, fg=C["muted"],
                                 font=FONT_CAPTION, anchor="w", justify=tk.LEFT)
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.key_entry.bind("<Return>", lambda _e: self.save())
        self.bind("<Destroy>", self._stop_poll, add="+")
        self.select_vendor(mo.DEFAULT_VENDOR)

    # ── 构件 ──
    def _entry(self, form, row, label, variable, *, show=None, mono=False, width=None,
               stretch=True):
        px = self._px
        i18n.Label(form, text=label, bg=self._base, fg=C["subtext"],
                   font=FONT_SMALL).grid(row=row, column=0, sticky="w", pady=px(4),
                                         padx=(0, px(12)))
        entry = tk.Entry(form, textvariable=variable, show=show or "", width=width or 20,
                         bg=self._field_bg, fg=C["text"], insertbackground=C["ember"],
                         relief=tk.FLAT, highlightthickness=1,
                         highlightbackground=mix(self._field_bg, "#FFFFFF", .10),
                         highlightcolor=C["ember"],
                         font=FONT_MONO_SM if mono else FONT_UI)
        entry.grid(row=row, column=1, sticky="ew" if stretch else "w", pady=px(4), ipady=px(5))
        return entry

    def _vendor_card(self, parent, v):
        px = self._px
        card = tk.Frame(parent, bg=self._base, cursor="hand2", padx=px(10), pady=px(8),
                        highlightthickness=1, highlightbackground=mix(self._base, "#FFFFFF", .08))
        badge = tk.Canvas(card, width=px(26), height=px(26), bg=self._base, highlightthickness=0,
                          bd=0)
        badge.create_oval(1, 1, px(26) - 1, px(26) - 1, fill=v.color,
                          outline=mix(v.color, "#FFFFFF", .35))
        badge.create_text(px(13), px(13), text=v.letter or v.name[:1], fill="#FFFFFF",
                          font=FONT_UI_BOLD)
        badge.pack(side=tk.LEFT, padx=(0, px(8)))
        text = tk.Frame(card, bg=self._base)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        name = i18n.Label(text, text=NAMES.get(v.id, v.name), bg=self._base, fg=C["text"],
                          font=FONT_UI_BOLD, anchor="w")
        name.pack(fill=tk.X)
        blurb = i18n.Label(text, text=BLURBS.get(v.id, v.blurb), bg=self._base, fg=C["muted"],
                           font=FONT_MICRO, anchor="w")
        blurb.pack(fill=tk.X)
        tag = None
        if v.badge:
            # 推荐标记用一颗熔铁色星：中英文都不挤占厂商名。
            tag = i18n.Label(card, text="★", bg=C["accent_soft"], fg=C["ember"],
                             font=FONT_MICRO, padx=px(4))
            tag.place(relx=1, x=-px(3), y=px(3), anchor="ne")
            from gui_theme import attach_tooltip
            attach_tooltip(tag, tr("推荐"))
        card._parts = [w for w in (card, badge, text, name, blurb) if w is not None]
        card._tag = tag
        for widget in card._parts:
            widget.bind("<Button-1>", lambda _e, vid=v.id: self.select_vendor(vid))
        bind_keyboard_action(card, lambda vid=v.id: self.select_vendor(vid))
        return card

    def set_advanced(self, show: bool):
        """开发者模式显示接口地址与温度；普通模式收起（仍用推荐值）。"""
        self._advanced = bool(show)
        for widget in self._advanced_widgets:
            if show:
                widget.grid()
            else:
                widget.grid_remove()

    # ── 状态 ──
    def select_vendor(self, vendor_id):
        v = mo.vendor(vendor_id)
        if v is None:
            return
        self.vendor_id = v.id
        for vid, card in self._vendor_cards.items():
            on = vid == v.id
            fill = mix(self._base, C["accent"], .16) if on else self._base
            card.configure(highlightbackground=C["ember"] if on else
                           mix(self._base, "#FFFFFF", .08))
            for widget in card._parts:
                if not isinstance(widget, tk.Canvas):
                    widget.configure(bg=fill)
                else:
                    widget.configure(bg=fill)
        self.model_combo.configure(values=list(v.models))
        self.model_var.set(v.models[0])
        self.url_var.set(v.base_url)
        self.key_entry.configure(state=tk.DISABLED if v.keyless else tk.NORMAL)
        if v.keyless:
            self.key_var.set("")
        self._key_link.configure(text=tr("本地运行，无需密钥") if v.keyless else tr("获取密钥 ↗"),
                                 fg=C["muted"] if v.keyless else C["link"],
                                 cursor="arrow" if v.keyless else "hand2")
        self._set_status("")

    def _open_console(self):
        v = mo.vendor(self.vendor_id)
        if v is not None and v.console:
            webbrowser.open(v.console)

    def _set_status(self, message, tone="muted"):
        colour = {"ok": C["ok"], "error": C["error"], "busy": C["ember"]}.get(tone, C["muted"])
        self.status_var.set(message)
        self.status.configure(fg=colour)

    def _temperature(self):
        text = self.temperature_var.get().strip()
        if not text:
            return None
        value = float(text)
        if not 0 <= value <= 2:
            raise ValueError
        return value

    # ── 测试 / 保存 ──
    def test(self):
        if self._busy:
            return
        vendor_id, key = self.vendor_id, self.key_var.get()
        self._busy = True
        self._set_status(tr("正在测试连接…"), "busy")

        def work():
            try:
                result = mo.test_connection(vendor_id, key)
            except Exception as exc:  # 网络栈的意外异常也要回到界面
                result = {"ok": False, "error": str(exc)}
            self._events.put(("test", result))
        threading.Thread(target=work, daemon=True).start()
        self._poll()

    def save(self):
        if self._busy:
            return None
        try:
            temperature = self._temperature()
        except ValueError:
            self._set_status(tr("温度需是 0 到 2 之间的数字"), "error")
            return None
        result = mo.apply_key(self._home, self.vendor_id, self.key_var.get(),
                              model=self.model_var.get(), base_url=self.url_var.get(),
                              temperature=temperature)
        if not result.get("ok"):
            self._set_status(_localize(result.get("error", "")), "error")
            return result
        self._set_status(tr("已保存：{model}", model=result["model"]), "ok")
        if callable(self._on_saved):
            self._on_saved(result)
        return result

    def _poll(self):
        self._poll_job = None
        try:
            while True:
                kind, result = self._events.get_nowait()
                if kind == "test":
                    self._busy = False
                    if result.get("ok"):
                        self._set_status(tr("连接成功"), "ok")
                    else:
                        self._set_status(_localize(result.get("error", "")), "error")
        except queue.Empty:
            pass
        if self._busy:
            try:
                self._poll_job = self.after(120, self._poll)
            except tk.TclError:
                pass

    def _stop_poll(self, event):
        if event.widget is self and self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None


__all__ = ["QuickModelSetup"]
