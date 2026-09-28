# -*- coding: utf-8 -*-
"""Forge Model Picker：Composer 内的一等模型与路由选择组件。

组件只展示调用方提供的 Provider / Router 真实数据，不修改路由业务逻辑。
同时保留旧 Combobox 使用到的最小兼容面，避免影响既有 GUI 合约。
"""
from __future__ import annotations

import math
import tkinter as tk
from urllib.parse import urlsplit

import brand_marks
from gui_theme import (C, FONT_CAPTION, FONT_EMOJI_SM, FONT_EMOJI_XS, FONT_MICRO,
                       FONT_SMALL, FONT_UI_BOLD, OVERLAY_WIDTH, position_popover)
from reasoning_slider import MiniReasoningTrack, ReasoningSlider

PICKER_WIDTH = OVERLAY_WIDTH
PICKER_MAX_HEIGHT = 560
ITEM_ICON = 20
# Tk 的 point 字体会随 Windows DPI 缩放；三层信息（名称、说明、能力标签）
# 需要留出足够的逻辑高度，避免 150% DPI 下最后一层被 pack 裁掉。
MODEL_ROW_HEIGHT = 106
FILTERS = (("all", "全部"), ("recommended", "推荐"), ("cloud", "云端"),
           ("local", "本地"), ("favorites", "收藏"))


def _endpoint(provider: dict | None) -> str:
    url = str((provider or {}).get("baseURL") or "")
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _is_local(provider: dict | None) -> bool:
    endpoint = _endpoint(provider).lower()
    raw = str((provider or {}).get("baseURL") or "")
    return endpoint in {"localhost", "127.0.0.1", "::1"} or "11434" in raw


def _pairs(value):
    """从 Router 配置的 pair / pair-list 中保守提取 (provider, model)。"""
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(v, str) for v in value):
            yield tuple(value)
            return
        for item in value:
            yield from _pairs(item)


class ModelPicker(tk.Frame):
    """固定宽度、可搜索、分组的模型选择浮层。"""

    def __init__(self, parent, textvariable, *, values=(), bg=None,
                 provider_lookup=None, router_lookup=None, on_select=None,
                 on_router=None, on_settings=None, favorites=(),
                 on_favorite=None, thinking_var=None, thinking_choices=(),
                 on_thinking=None, router_choices=(), on_router_strategy=None,
                 width_hint=144):
        base = bg or C["input_bg"]
        super().__init__(parent, bg=base, highlightthickness=1,
                         highlightbackground=C["border_hi"],
                         highlightcolor=C["accent"], bd=0, cursor="hand2")
        self.var = textvariable
        self._values = [str(v) for v in (values or ())]
        self._provider_lookup = provider_lookup
        self._router_lookup = router_lookup
        self._on_select = on_select
        self._on_router = on_router
        self._on_settings = on_settings
        self._on_favorite = on_favorite
        self._thinking_var = thinking_var
        self._thinking_choices = tuple(thinking_choices or ())
        self._on_thinking = on_thinking
        self._favorites = {str(v) for v in (favorites or ())}
        self._popup: tk.Toplevel | None = None
        self._rows: list[tuple[str, tk.Frame]] = []
        self._keep: list[tk.PhotoImage] = []
        self._base_bg = base
        self.width_hint = width_hint
        self._filter = "all"
        self._search_var: tk.StringVar | None = None
        self._list_inner: tk.Frame | None = None
        self._filter_widgets: dict[str, tuple[tk.Frame, tk.Label]] = {}
        self._thinking_widgets: dict[str, tuple[tk.Frame, tk.Label]] = {}
        self._router_choices = tuple(router_choices or ())
        self._on_router_strategy = on_router_strategy
        self._router_chips: dict[str, tuple[tk.Frame, tk.Label]] = {}
        self._router_heading: tk.Label | None = None
        self._router_detail: tk.Label | None = None

        self._body = tk.Frame(self, bg=base)
        self._body.pack(padx=8, pady=4)
        self._icon_box = tk.Frame(self._body, bg=base, width=ITEM_ICON, height=ITEM_ICON)
        self._icon_box.pack_propagate(False)
        self._icon_box.pack(side=tk.LEFT, padx=(0, 6))
        self._icon = tk.Label(self._icon_box, bg=base, bd=0)
        self._icon.place(relx=.5, rely=.5, anchor="center")
        self._fallback = tk.Canvas(self._icon_box, width=ITEM_ICON, height=ITEM_ICON,
                                   bg=base, highlightthickness=0, bd=0)
        self._labels = tk.Frame(self._body, bg=base)
        self._labels.pack(side=tk.LEFT)
        self._text = tk.Label(self._labels, bg=base, fg=C["body"], font=FONT_SMALL,
                              anchor="w")
        self._text.pack(fill=tk.X)
        # 思考强度：文字 + 一枚小滑杆示意（体现「第几档」，不接交互）
        self._thinking_row = tk.Frame(self._labels, bg=base)
        self._thinking_emoji = tk.Label(self._thinking_row, text="🧠", bg=base,
                                        font=FONT_EMOJI_XS)
        self._thinking_text = tk.Label(self._thinking_row, bg=base, fg=C["muted"],
                                       font=FONT_MICRO, anchor="w")
        self._thinking_mini: MiniReasoningTrack | None = None
        if self._thinking_choices:
            self._thinking_row.pack(fill=tk.X, pady=(1, 0))
            self._thinking_emoji.pack(side=tk.LEFT, padx=(0, 3))
            self._thinking_text.pack(side=tk.LEFT)
            self._thinking_mini = MiniReasoningTrack(
                self._thinking_row, self._thinking_choices, bg=base)
            self._thinking_mini.pack(side=tk.LEFT, padx=(6, 0))
        self._caret = tk.Canvas(self._body, width=10, height=10, bg=base,
                                highlightthickness=0, bd=0)
        self._caret.create_polygon(2, 3, 8, 3, 5, 7, fill=C["muted"], outline="")
        self._caret.pack(side=tk.LEFT, padx=(6, 0))

        bind_targets = [self, self._body, self._icon_box, self._icon,
                        self._fallback, self._labels, self._text,
                        self._thinking_row, self._thinking_emoji,
                        self._thinking_text, self._caret]
        if self._thinking_mini is not None:
            bind_targets.append(self._thinking_mini)
        for widget in bind_targets:
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", lambda _e: self._set_hover(True))
            widget.bind("<Leave>", lambda _e: self._set_hover(False))
        self._var_trace = self.var.trace_add("write", lambda *_: self.refresh())
        self._thinking_trace = (self._thinking_var.trace_add(
            "write", lambda *_: self.refresh_thinking())
            if self._thinking_var is not None else None)
        self.refresh()
        self.refresh_thinking()

    # -- Combobox compatibility -------------------------------------
    def set_values(self, values) -> None:
        new = [str(v) for v in (values or ())]
        if new == self._values:
            return
        self._values = new
        self.refresh()
        if self._popup is not None:
            self._rebuild_list()

    def configure(self, cnf=None, **kw):  # noqa: D401 - Tk convention
        if "values" in kw:
            self.set_values(kw.pop("values"))
        if not kw:
            return super().configure(cnf) if cnf is not None else super().configure()
        return super().configure(cnf, **kw) if cnf is not None else super().configure(**kw)

    config = configure

    def cget(self, key):
        return tuple(self._values) if key == "values" else super().cget(key)

    def __getitem__(self, key):
        return tuple(self._values) if key == "values" else super().__getitem__(key)

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str) -> None:
        self.var.set(value)

    def current(self, index: int | None = None):
        if index is None:
            try:
                return self._values.index(self.var.get())
            except ValueError:
                return -1
        if 0 <= index < len(self._values):
            self.var.set(self._values[index])
            return index
        return -1

    # -- data --------------------------------------------------------
    def _provider_for(self, model: str) -> dict | None:
        if self._provider_lookup is None:
            return None
        try:
            value = self._provider_lookup(model)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def _router(self) -> dict:
        if self._router_lookup is None:
            return {}
        try:
            value = self._router_lookup()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def brand_of(self, model: str):
        return brand_marks.detect(model=model, provider=self._provider_for(model))

    def _recommended_models(self) -> set[str]:
        raw = self._router()
        routing = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
        found = set()
        for key in ("primary", "fallback"):
            for _provider, model in _pairs(raw.get(key)):
                found.add(model)
        for key in ("tiers", "premium", "small"):
            for _provider, model in _pairs(routing.get(key)):
                found.add(model)
        return found

    def _matches_filter(self, value: str) -> bool:
        provider = self._provider_for(value)
        if self._filter == "favorites":
            return value in self._favorites
        if self._filter == "local":
            return _is_local(provider)
        if self._filter == "cloud":
            return not _is_local(provider)
        if self._filter == "recommended":
            candidates = self._recommended_models()
            real = str((provider or {}).get("model") or "")
            label = str((provider or {}).get("modelLabel") or "")
            return value in candidates or real in candidates or label in candidates
        return True

    # -- collapsed state --------------------------------------------
    def _paint_fallback(self, brand=None, *, bg=None) -> None:
        base = bg or self._base_bg
        self._fallback.configure(bg=base)
        self._fallback.delete("all")
        color = brand.color if brand is not None else C["accent2"]
        self._fallback.create_oval(1, 1, ITEM_ICON - 1, ITEM_ICON - 1,
                                   fill=C["surface2"], outline=color, width=1)
        letter = (brand.label if brand else "F")[:1].upper()
        self._fallback.create_text(ITEM_ICON / 2, ITEM_ICON / 2, text=letter,
                                   fill=color, font=(FONT_MICRO[0], 8, "bold"))

    def _set_hover(self, on: bool) -> None:
        if self._popup is not None:
            on = True
        bg = C["hover"] if on else self._base_bg
        for widget in (self, self._body, self._icon_box, self._icon,
                       self._labels, self._text, self._thinking_row,
                       self._thinking_emoji, self._thinking_text, self._caret):
            try:
                widget.configure(bg=bg)
            except tk.TclError:
                pass
        if self._thinking_mini is not None:
            self._thinking_mini.refresh()
        self._paint_fallback(self.brand_of(self.var.get() or "default"), bg=bg)

    def refresh(self) -> None:
        value = self.var.get() or "default"
        provider = self._provider_for(value)
        brand = self.brand_of(value)
        icon, keep = brand_marks.mark_icon(brand, ITEM_ICON)
        if icon is not None:
            self._keep = [keep]
            self._fallback.place_forget()
            self._icon.configure(image=icon)
            self._icon.place(relx=.5, rely=.5, anchor="center")
        else:
            self._keep = []
            self._icon.place_forget()
            self._paint_fallback(brand)
            self._fallback.place(relx=.5, rely=.5, anchor="center")
        if value == "default":
            shown = "默认 Provider"
            if provider:
                shown += f" · {provider.get('modelLabel') or provider.get('model') or ''}"
        else:
            shown = value
        self._text.configure(text=shown)

    def refresh_thinking(self) -> None:
        if self._thinking_var is None:
            return
        current = self._thinking_var.get()
        label = next((name for value, name, _hint in self._thinking_choices
                      if value == current), current or "Light")
        self._thinking_text.configure(text=f"思考强度 · {label}")
        if self._thinking_mini is not None:
            self._thinking_mini.refresh()
        self._paint_thinking_choices()

    def _current_model_label(self) -> str:
        """控制卡副标题：优先展示真实模型名，而不是 Provider 配置键。"""
        value = self.var.get() or "default"
        provider = self._provider_for(value) or {}
        return str(provider.get("modelLabel") or provider.get("model") or
                   ("默认 Provider" if value == "default" else value))

    # -- popup -------------------------------------------------------
    def _on_click(self, _event=None):
        self.open_menu()
        return "break"

    def open_menu(self) -> None:
        if self._popup is not None:
            self.close_menu()
            return
        if not self._values:
            return
        pop = tk.Toplevel(self)
        pop.withdraw()
        pop.overrideredirect(True)
        pop.configure(bg=C["border_hi"])
        try:
            pop.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._popup = pop
        self._keep = [k for k in self._keep if k is not None]

        shell = tk.Frame(pop, bg=C["surface"], padx=12, pady=12)
        shell.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        title = tk.Frame(shell, bg=C["surface"])
        title.pack(fill=tk.X)
        tk.Label(title, text="选择模型", bg=C["surface"], fg=C["text"],
                 font=FONT_UI_BOLD).pack(side=tk.LEFT)
        tk.Label(title, text="Provider 与路由", bg=C["surface"], fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.RIGHT)

        search_host = tk.Frame(shell, bg=C["input_bg"], highlightthickness=1,
                               highlightbackground=C["border_hi"])
        search_host.pack(fill=tk.X, pady=(10, 8))
        self._search_var = tk.StringVar()
        search = tk.Entry(search_host, textvariable=self._search_var,
                          bg=C["input_bg"], fg=C["text"], insertbackground=C["accent"],
                          relief=tk.FLAT, bd=0, font=FONT_SMALL)
        search.pack(fill=tk.X, padx=10, pady=7)
        search_hint = tk.Label(search_host, text="搜索模型、Provider 或能力……",
                               bg=C["input_bg"], fg=C["placeholder"],
                               font=FONT_SMALL, cursor="xterm")
        search_hint.place(x=11, rely=.5, anchor="w")
        search_hint.bind("<Button-1>", lambda _e: search.focus_set())

        def search_changed(*_):
            if self._search_var.get():
                search_hint.place_forget()
            elif search.focus_get() is not search:
                search_hint.place(x=11, rely=.5, anchor="w")
            self._rebuild_list()

        self._search_var.trace_add("write", search_changed)
        search.bind("<FocusIn>", lambda _e: search_hint.place_forget())
        search.bind("<FocusOut>", lambda _e: search_changed())

        filters = tk.Frame(shell, bg=C["surface"])
        filters.pack(fill=tk.X, pady=(0, 8))
        self._filter_widgets.clear()
        for key, label in FILTERS:
            host = tk.Frame(filters, bg=C["surface2"], cursor="hand2")
            host.pack(side=tk.LEFT, padx=(0, 5))
            text = tk.Label(host, text=label, bg=C["surface2"], fg=C["subtext"],
                            font=FONT_MICRO, padx=8, pady=4, cursor="hand2")
            text.pack()
            for widget in (host, text):
                widget.bind("<Button-1>", lambda _e, k=key: self._set_filter(k))
            self._filter_widgets[key] = (host, text)
        self._paint_filters()

        if self._router():
            self._build_router_card(shell)

        # 固定在底部，模型列表只占中间剩余空间；小窗口也不会把强度选项裁掉。
        if self._thinking_choices:
            self._build_thinking_section(shell)

        viewport = tk.Frame(shell, bg=C["surface"])
        viewport.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        canvas = tk.Canvas(viewport, bg=C["surface"], highlightthickness=0, bd=0,
                           width=PICKER_WIDTH - 26, height=330)
        scrollbar = tk.Scrollbar(viewport, orient=tk.VERTICAL, command=canvas.yview,
                                 bg=C["surface2"], troughcolor=C["surface"], bd=0,
                                 relief=tk.FLAT, width=8)
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = tk.Frame(canvas, bg=C["surface"])
        self._list_inner = inner
        win = canvas.create_window(0, 0, anchor="nw", window=inner)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        pop.bind("<MouseWheel>", lambda e: (canvas.yview_scroll(-1 if e.delta > 0 else 1,
                                                                  "units"), "break")[-1])
        pop.bind("<Escape>", lambda _e: self.close_menu())
        pop.bind("<Button-1>", self._maybe_close)
        self._rebuild_list()

        pop.update_idletasks()
        position_popover(pop, self, PICKER_WIDTH,
                         min(PICKER_MAX_HEIGHT, pop.winfo_reqheight()), align_right=True)
        pop.deiconify()
        try:
            pop.grab_set()
            search.focus_set()
        except tk.TclError:
            pass

    def _build_thinking_section(self, parent) -> None:
        section = tk.Frame(parent, bg=C["surface"])
        section.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        tk.Frame(section, bg=C["border"], height=1).pack(fill=tk.X, pady=(0, 9))
        # Codex 式控制卡：档位、当前模型、复位和能量滑轨形成一个视觉整体。
        self._thinking_slider = ReasoningSlider(
            section, self._thinking_var, self._thinking_choices,
            on_select=self._apply_thinking, bg=C["surface"],
            model_getter=self._current_model_label, default_value="medium")
        self._thinking_slider.pack(fill=tk.X)
        # 兼容既有合约：_thinking_widgets 仍是「档位 → (容器, 文字)」
        self._thinking_widgets = self._thinking_slider.label_widgets
        self._paint_thinking_choices()

    def _paint_thinking_choices(self) -> None:
        """重画档位高亮（滑杆自己负责绘制，这里只做转发，保留旧接口）。"""
        slider = getattr(self, "_thinking_slider", None)
        if slider is not None:
            slider.refresh()

    def _apply_thinking(self, value: str) -> None:
        """档位变化：交给调用方（会持久化并联动任务沉思），**不关弹层**。"""
        if self._on_thinking is not None:
            self._on_thinking(value)
        elif self._thinking_var is not None:
            self._thinking_var.set(value)

    def _select_thinking(self, value: str) -> None:
        """按档位取值。保留此名字以兼容旧调用方；同样不再关弹层。"""
        self._apply_thinking(value)

    def _build_router_card(self, parent) -> None:
        """Forge Auto 路由卡：不再是只读展示，用户可以直接选策略。"""
        raw = self._router()
        routing = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
        strategy = str(routing.get("strategy") or "medium")
        choices = self._router_choices or (
            ("base", "Base", "成本优先，失败时可升级"),
            ("medium", "Medium", "按任务难度选模型"),
            ("premium", "Premium", "优先用最强模型"))
        card = tk.Frame(parent, bg=C["accent_soft"],
                        highlightthickness=1, highlightbackground=C["accent_border"])
        card.pack(fill=tk.X, pady=(0, 6))
        tk.Frame(card, bg=C["accent"], width=3).pack(side=tk.LEFT, fill=tk.Y)
        body = tk.Frame(card, bg=C["accent_soft"], padx=10, pady=9)
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        line = tk.Frame(body, bg=C["accent_soft"])
        line.pack(fill=tk.X)
        tk.Label(line, text="🧭", bg=C["accent_soft"], font=FONT_EMOJI_XS).pack(
            side=tk.LEFT, padx=(0, 6))
        self._router_heading = tk.Label(
            line, text=f"Forge Auto · {strategy}", bg=C["accent_soft"],
            fg=C["text"], font=FONT_UI_BOLD)
        self._router_heading.pack(side=tk.LEFT)
        action = tk.Label(line, text="打开任务", bg=C["accent_soft"], fg=C["accent_text"],
                          font=FONT_CAPTION, cursor="hand2")
        action.pack(side=tk.RIGHT)
        detail_text = next((hint for value, _label, hint in choices if value == strategy),
                           "")
        self._router_detail = tk.Label(
            body, text=detail_text or "按选定的 Router 策略执行任务；普通对话仍用所选 Provider。",
            bg=C["accent_soft"], fg=C["subtext"], font=FONT_CAPTION, anchor="w",
            justify=tk.LEFT, wraplength=PICKER_WIDTH - 60)
        self._router_detail.pack(fill=tk.X, pady=(4, 0))

        chips = tk.Frame(body, bg=C["accent_soft"])
        chips.pack(fill=tk.X, pady=(7, 0))
        self._router_chips.clear()
        for value, label, hint in choices:
            host = tk.Frame(chips, bg=C["surface2"], cursor="hand2")
            host.pack(side=tk.LEFT, padx=(0, 5))
            text = tk.Label(host, text=label, bg=C["surface2"], fg=C["subtext"],
                            font=FONT_MICRO, padx=9, pady=4, cursor="hand2")
            text.pack()
            for widget in (host, text):
                widget.bind("<Button-1>", lambda _e, v=value: self._pick_router(v))
                widget.bind("<Enter>",
                            lambda _e, v=value: self._hover_router(v, True))
                widget.bind("<Leave>",
                            lambda _e, v=value: self._hover_router(v, False))
            host._forge_hint = hint
            self._router_chips[value] = (host, text)
        self._paint_router_chips(strategy)

        def open_router(_event=None):
            self.close_menu()
            if self._on_router is not None:
                self._on_router(strategy)

        for widget in (action,):
            widget.bind("<Button-1>", open_router)

    def _hover_router(self, value: str, on: bool) -> None:
        current = self._current_router_strategy()
        entry = self._router_chips.get(value)
        if entry is None or value == current:
            return
        bg = C["hover"] if on else C["surface2"]
        for widget in entry:
            try:
                widget.configure(bg=bg)
            except tk.TclError:
                pass

    def _current_router_strategy(self) -> str:
        raw = self._router()
        routing = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
        return str(routing.get("strategy") or "medium")

    def _paint_router_chips(self, current: str | None = None) -> None:
        current = current or self._current_router_strategy()
        for value, (host, text) in self._router_chips.items():
            selected = value == current
            bg = C["accent"] if selected else C["surface2"]
            host.configure(bg=bg, highlightthickness=0)
            text.configure(bg=bg,
                           fg="#FFFFFF" if selected else C["subtext"],
                           font=FONT_UI_BOLD if selected else FONT_MICRO)

    def _pick_router(self, value: str) -> None:
        """选策略：交给调用方持久化，**不关弹层**（可以接着改别的）。

        只在调用方确认成功后重画——保存失败 / 任务运行中被拒时，界面必须
        回到配置里的真实策略，不能显示成「已选中」。
        """
        if self._on_router_strategy is None:
            return
        try:
            ok = self._on_router_strategy(value)
        except Exception:
            ok = False
        if ok is False:
            # 落回真实配置：调用方可能刚把它改成别的（或没改）
            self._paint_router_chips()
            current = self._current_router_strategy()
            if self._router_heading is not None:
                self._router_heading.configure(text=f"Forge Auto · {current}")
            if self._router_detail is not None:
                hint = next((h for v, _l, h in self._router_choices if v == current), "")
                if hint:
                    self._router_detail.configure(text=hint)
            return
        self._paint_router_chips(value)
        if self._router_heading is not None:
            self._router_heading.configure(text=f"Forge Auto · {value}")
        if self._router_detail is not None:
            hint = next((h for v, _l, h in self._router_choices if v == value), "")
            if hint:
                self._router_detail.configure(text=hint)

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self._paint_filters()
        self._rebuild_list()

    def _paint_filters(self) -> None:
        for key, (host, label) in self._filter_widgets.items():
            active = key == self._filter
            bg = C["accent_soft"] if active else C["surface2"]
            host.configure(bg=bg, highlightthickness=1 if active else 0,
                           highlightbackground=C["accent_border"])
            label.configure(bg=bg, fg=C["accent_text"] if active else C["subtext"])

    def _rebuild_list(self) -> None:
        inner = self._list_inner
        if inner is None or not inner.winfo_exists():
            return
        for child in inner.winfo_children():
            child.destroy()
        self._rows = []
        query = self._search_var.get().strip().lower() if self._search_var else ""
        groups: dict[str, list[str]] = {}
        group_meta: dict[str, tuple[object, str]] = {}
        for value in self._values:
            provider = self._provider_for(value)
            brand = self.brand_of(value)
            endpoint = _endpoint(provider)
            haystack = " ".join((value, str((provider or {}).get("model") or ""),
                                 str((provider or {}).get("modelLabel") or ""),
                                 brand.label if brand else "", endpoint)).lower()
            if query and query not in haystack:
                continue
            if not self._matches_filter(value):
                continue
            key = brand.key if brand else (endpoint or "other")
            groups.setdefault(key, []).append(value)
            group_meta[key] = (brand, endpoint)
        if not groups:
            tk.Label(inner, text="没有匹配的模型", bg=C["surface"], fg=C["muted"],
                     font=FONT_SMALL, pady=24).pack(fill=tk.X)
            return
        current = self.var.get()
        for key, values in groups.items():
            brand, endpoint = group_meta[key]
            self._make_provider_header(inner, brand, endpoint, self._provider_for(values[0]))
            for value in values:
                row = self._make_model_row(inner, value, value == current)
                row.pack(fill=tk.X, pady=(0, 3))
                self._rows.append((value, row))

    def _brand_icon(self, parent, brand, *, bg: str, size=ITEM_ICON):
        box = tk.Frame(parent, bg=bg, width=28, height=28)
        box.pack_propagate(False)
        icon, keep = brand_marks.mark_icon(brand, size)
        if icon is not None:
            self._keep.append(keep)
            tk.Label(box, image=icon, bg=bg, bd=0).place(relx=.5, rely=.5, anchor="center")
        else:
            cv = tk.Canvas(box, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
            color = brand.color if brand else C["accent2"]
            cv.create_oval(1, 1, size - 1, size - 1, fill=C["surface2"],
                           outline=color, width=1)
            cv.create_text(size / 2, size / 2,
                           text=((brand.label if brand else "F")[:1].upper()),
                           fill=color, font=(FONT_MICRO[0], 8, "bold"))
            cv.place(relx=.5, rely=.5, anchor="center")
        return box

    def _make_provider_header(self, parent, brand, endpoint: str, provider: dict | None):
        row = tk.Frame(parent, bg=C["surface"])
        row.pack(fill=tk.X, pady=(8, 5))
        icon = self._brand_icon(row, brand, bg=C["surface"], size=16)
        icon.pack(side=tk.LEFT, padx=(2, 5))
        name = brand.label if brand else "Other Provider"
        tk.Label(row, text=name, bg=C["surface"], fg=C["subtext"],
                 font=FONT_CAPTION).pack(side=tk.LEFT)
        if endpoint:
            local = "Local" if _is_local(provider) else endpoint
            tk.Label(row, text=local, bg=C["surface"], fg=C["muted"],
                     font=FONT_MICRO).pack(side=tk.LEFT, padx=(7, 0))
        if self._on_settings is not None:
            settings = tk.Label(row, text="设置", bg=C["surface"], fg=C["muted"],
                                font=FONT_MICRO, cursor="hand2", padx=4)
            settings.pack(side=tk.RIGHT)
            settings.bind("<Button-1>", lambda _e, p=provider: (
                self.close_menu(), self._on_settings(p)))

    def _description(self, value: str, provider: dict | None) -> str:
        real = str((provider or {}).get("model") or value)
        endpoint = _endpoint(provider)
        if value == "default":
            return f"当前配置的首选 Provider · {real}"
        if real != value:
            return f"上游模型 {real}"
        if _is_local(provider):
            return "本地 Provider 模型"
        return f"通过 {endpoint} 提供" if endpoint else "已配置 Provider 模型"

    def _tags(self, value: str, provider: dict | None) -> list[str]:
        lower = f"{value} {(provider or {}).get('model', '')}".lower()
        tags = ["Local" if _is_local(provider) else "Cloud"]
        if any(token in lower for token in ("flash", "fast", "speed")):
            tags.append("Fast")
        if any(token in lower for token in ("code", "coder")):
            tags.append("Coding")
        if any(token in lower for token in ("vision", "-vl", "image")):
            tags.append("Vision")
        return tags[:3]

    def _make_model_row(self, parent, value: str, selected: bool) -> tk.Frame:
        provider = self._provider_for(value)
        brand = self.brand_of(value)
        base = C["sel"] if selected else C["surface2"]
        row = tk.Frame(parent, bg=base, height=MODEL_ROW_HEIGHT, cursor="hand2",
                       highlightthickness=1,
                       highlightbackground=C["accent_border"] if selected else C["surface2"])
        row.pack_propagate(False)
        indicator = tk.Frame(row, bg=C["accent"] if selected else base, width=3)
        indicator.pack(side=tk.LEFT, fill=tk.Y)
        icon = self._brand_icon(row, brand, bg=base)
        icon.pack(side=tk.LEFT, padx=(10, 8))
        text = tk.Frame(row, bg=base, cursor="hand2")
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=7)
        title_row = tk.Frame(text, bg=base, cursor="hand2")
        title_row.pack(fill=tk.X)
        shown = "默认 Provider" if value == "default" else value
        title = tk.Label(title_row, text=shown, bg=base, fg=C["text"],
                         font=FONT_UI_BOLD if selected else FONT_SMALL,
                         anchor="w", cursor="hand2")
        title.pack(side=tk.LEFT)
        if selected:
            check = tk.Canvas(title_row, width=14, height=14, bg=base,
                              highlightthickness=0, bd=0, cursor="hand2")
            check.create_oval(1, 1, 13, 13, fill=C["accent"], outline="")
            check.create_line(4, 7, 6, 9, 10, 5, fill="#FFFFFF", width=1.5)
            check.pack(side=tk.LEFT, padx=(7, 0))
        desc = tk.Label(text, text=self._description(value, provider), bg=base,
                        fg=C["subtext"], font=FONT_CAPTION, anchor="w", cursor="hand2")
        desc.pack(fill=tk.X, pady=(2, 0))
        tags = tk.Frame(text, bg=base, cursor="hand2")
        tags.pack(fill=tk.X, pady=(3, 0))
        for label in self._tags(value, provider):
            tk.Label(tags, text=label, bg=C["surface"], fg=C["muted"],
                     font=FONT_MICRO, padx=5, pady=1).pack(side=tk.LEFT, padx=(0, 4))
        favorite = tk.Canvas(row, width=28, height=28, bg=base,
                             highlightthickness=0, bd=0, cursor="hand2")
        favorite.pack(side=tk.RIGHT, padx=(3, 8))
        self._draw_star(favorite, value in self._favorites)

        def paint(bg: str):
            row.configure(bg=bg)
            indicator.configure(bg=C["accent"] if selected else bg)
            for widget in (icon, text, title_row, title, desc, tags, favorite):
                try:
                    widget.configure(bg=bg)
                except tk.TclError:
                    pass

        def pick(_event=None):
            self._select(value)
            return "break"

        def toggle_favorite(_event=None):
            if value in self._favorites:
                self._favorites.remove(value)
            else:
                self._favorites.add(value)
            self._draw_star(favorite, value in self._favorites)
            if self._on_favorite is not None:
                self._on_favorite(sorted(self._favorites))
            if self._filter == "favorites":
                self._rebuild_list()
            return "break"

        favorite.bind("<Button-1>", toggle_favorite)
        clickable = (row, indicator, icon, text, title_row, title, desc, tags)
        for widget in clickable:
            widget.bind("<Button-1>", pick)
            widget.bind("<Enter>", lambda _e: paint(C["hover"]))
            widget.bind("<Leave>", lambda _e: paint(base))
        for child in tags.winfo_children():
            child.bind("<Button-1>", pick)
        return row

    @staticmethod
    def _draw_star(canvas: tk.Canvas, active: bool) -> None:
        canvas.delete("all")
        points = []
        for idx in range(10):
            angle = -math.pi / 2 + idx * math.pi / 5
            radius = 8 if idx % 2 == 0 else 3.5
            points.extend((14 + math.cos(angle) * radius, 14 + math.sin(angle) * radius))
        canvas.create_polygon(points, fill=C["accent2"] if active else "",
                              outline=C["accent2"] if active else C["muted"], width=1)

    def _maybe_close(self, event=None) -> None:
        pop = self._popup
        if pop is None:
            return
        try:
            # 优先用事件坐标：点击位置才是判断依据。真实使用时两者一致，
            # 但用事件坐标更准确（合成事件、指针已经移开的情况都不会误判）。
            if event is not None and getattr(event, "x_root", None) is not None:
                x, y = event.x_root, event.y_root
            else:
                x, y = self.winfo_pointerxy()
            rx, ry = pop.winfo_rootx(), pop.winfo_rooty()
            inside = rx <= x < rx + pop.winfo_width() and ry <= y < ry + pop.winfo_height()
        except tk.TclError:
            inside = False
        if not inside:
            self.close_menu()

    def _select(self, value: str) -> None:
        self.var.set(value)
        self.close_menu()
        if self._on_select is not None:
            self._on_select(value)

    def close_menu(self) -> None:
        pop = self._popup
        self._popup = None
        self._rows = []
        self._list_inner = None
        self._search_var = None
        self._filter_widgets.clear()
        self._thinking_widgets.clear()
        if pop is None:
            return
        try:
            pop.grab_release()
        except tk.TclError:
            pass
        try:
            pop.destroy()
        except tk.TclError:
            pass
