"""Font-independent UI icons and pre-rendered emoji. Runtime: tkinter only.

The application logo remains in brand_marks/assets/forge-logo-*.png.
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
import i18n

# Coordinates use a 24 × 24 grid; all icons share a 1.7px rounded stroke.
# Shapes are also consumed by desktop/make_ui_assets.py.
ICONS = {
    "chat": [("poly", (4, 4, 20, 4, 20, 16, 11, 16, 6, 20, 6, 16, 4, 16))],
    "task": [("rect", (5, 4, 19, 21)), ("rect", (9, 2, 15, 6)), ("line", (8, 13, 11, 16, 16, 10))],
    "agents": [("rect", (4, 7, 20, 20)), ("line", (12, 3, 12, 7)), ("circle", (11, 1, 13, 3)),
               ("circle", (7, 11, 9, 13)), ("circle", (15, 11, 17, 13)), ("line", (9, 17, 15, 17))],
    "tools": [("line", (3, 6, 21, 6)), ("line", (3, 12, 21, 12)), ("line", (3, 18, 21, 18)),
              ("circle", (7, 4, 11, 8)), ("circle", (14, 10, 18, 14)), ("circle", (5, 16, 9, 20))],
    "knowledge": [("poly", (3, 4, 8, 4, 12, 7, 16, 4, 21, 4, 21, 19, 16, 19, 12, 22, 8, 19, 3, 19)),
                  ("line", (12, 7, 12, 22))],
    "evolution": [("line", (7, 3, 16, 9, 8, 15, 17, 21)), ("line", (17, 3, 8, 9, 16, 15, 7, 21)),
                  ("line", (9, 5, 15, 5)), ("line", (9, 12, 15, 12)), ("line", (9, 19, 15, 19))],
    "files": [("poly", (3, 5, 10, 5, 12, 8, 21, 8, 21, 20, 3, 20)), ("line", (3, 10, 21, 10))],
    "file": [("poly", (6, 2, 14, 2, 19, 7, 19, 22, 6, 22)), ("line", (14, 2, 14, 7, 19, 7)),
             ("line", (9, 12, 16, 12)), ("line", (9, 16, 15, 16))],
    "code": [("line", (8, 7, 3, 12, 8, 17)), ("line", (16, 7, 21, 12, 16, 17)), ("line", (14, 4, 10, 20))],
    "config": [("poly", (9, 3, 15, 3, 16, 6, 19, 7, 21, 11, 19, 14, 19, 17, 15, 20, 12, 19,
                           9, 21, 5, 18, 5, 15, 3, 12, 5, 8, 8, 7)), ("circle", (9, 9, 15, 15))],
    "search": [("circle", (3, 3, 16, 16)), ("line", (15, 15, 21, 21))],
    "plus": [("line", (12, 4, 12, 20)), ("line", (4, 12, 20, 12))],
    "minus": [("line", (4, 12, 20, 12))],
    "close": [("line", (6, 6, 18, 18)), ("line", (18, 6, 6, 18))],
    # Filled dots keep the overflow affordance legible at 16–20px. Outlined
    # dots blur into three small rings on Windows' non-retina scaling.
    "more": [("dot", (3.5, 10.5, 6.5, 13.5)), ("dot", (10.5, 10.5, 13.5, 13.5)),
             ("dot", (17.5, 10.5, 20.5, 13.5))],
    "chevron_right": [("line", (9, 5, 16, 12, 9, 19))],
    "chevron_left": [("line", (15, 5, 8, 12, 15, 19))],
    "chevron_down": [("line", (5, 9, 12, 16, 19, 9))],
    "chevron_up": [("line", (5, 15, 12, 8, 19, 15))],
    "hexagram": [("line", (5, 5, 19, 5)), ("line", (8, 12, 11, 12)),
                 ("line", (13, 12, 16, 12)), ("line", (5, 19, 19, 19))],
    "refresh": [("line", (20, 9, 17, 5, 12, 3, 7, 5, 4, 9, 4, 14, 7, 19, 12, 21, 17, 19, 20, 15)),
                ("line", (20, 3, 20, 9, 14, 9))],
    "external": [("line", (13, 3, 21, 3, 21, 11)), ("line", (21, 3, 11, 13)),
                 ("line", (9, 4, 4, 4, 4, 20, 20, 20, 20, 15))],
    "sidebar": [("rect", (3, 4, 21, 20)), ("line", (9, 4, 9, 20)), ("line", (5, 8, 7, 8)), ("line", (5, 12, 7, 12))],
    "workspace": [("rect", (3, 4, 21, 20)), ("line", (3, 9, 21, 9)), ("line", (10, 9, 10, 20))],
    "diff": [("circle", (4, 2, 8, 6)), ("circle", (4, 18, 8, 22)), ("circle", (17, 9, 21, 13)),
             ("line", (6, 6, 6, 18)), ("line", (6, 7, 12, 7, 16, 11, 17, 11))],
    "send": [("line", (12, 20, 12, 4)), ("line", (5, 11, 12, 4, 19, 11))],
    "play": [("poly", (7, 3, 21, 12, 7, 21))],
    "stop": [("rect", (6, 6, 18, 18))],
    "check": [("line", (4, 12, 10, 18, 21, 5))],
    "check_circle": [("circle", (2, 2, 22, 22)), ("line", (6, 12, 10, 16, 18, 8))],
    # Keep the warning outline one grid unit inside the box so its stroke does
    # not clip at small sizes.
    "warning": [("poly", (12, 2, 22, 20, 2, 20)), ("line", (12, 8, 12, 14)), ("dot", (11, 16.5, 13, 18.5))],
    "error": [("circle", (2, 2, 22, 22)), ("line", (8, 8, 16, 16)), ("line", (16, 8, 8, 16))],
    "info": [("circle", (2, 2, 22, 22)), ("line", (12, 11, 12, 17)), ("circle", (11.5, 6, 12.5, 7))],
    "copy": [("rect", (8, 8, 21, 22)), ("line", (16, 4, 16, 2, 3, 2, 3, 16, 5, 16))],
    "thumb_up": [("poly", (8, 20, 8, 10, 12, 3, 15, 3, 15, 10, 21, 10, 19, 20)), ("rect", (2, 10, 6, 20))],
    "thumb_down": [("poly", (8, 4, 8, 14, 12, 21, 15, 21, 15, 14, 21, 14, 19, 4)), ("rect", (2, 4, 6, 14))],
    "paperclip": [("line", (8, 12, 15, 5, 18, 5, 20, 7, 20, 10, 10, 20, 6, 20, 3, 17, 3, 13, 13, 3)),
                  ("line", (15, 8, 7, 16))],
    "key": [("circle", (3, 3, 12, 12)), ("line", (11, 11, 21, 21, 23, 19)), ("line", (17, 17, 20, 14))],
    "model": [("poly", (12, 2, 21, 7, 21, 17, 12, 22, 3, 17, 3, 7)),
              ("line", (3, 7, 12, 12, 21, 7)), ("line", (12, 12, 12, 22))],
    "terminal": [("rect", (2, 4, 22, 20)), ("line", (6, 9, 10, 12, 6, 15)), ("line", (13, 16, 18, 16))],
    "preview": [("poly", (1, 12, 5, 7, 12, 4, 19, 7, 23, 12, 19, 17, 12, 20, 5, 17)), ("circle", (9, 9, 15, 15))],
    "image": [("rect", (3, 3, 21, 21)), ("circle", (6, 6, 10, 10)), ("line", (3, 18, 10, 12, 14, 16, 17, 12, 21, 17))],
    "user": [("circle", (8, 3, 16, 11)), ("line", (3, 21, 4, 16, 8, 14, 16, 14, 20, 16, 21, 21))],
    "edit": [("poly", (4, 15, 15, 4, 20, 9, 9, 20, 3, 21)), ("line", (12, 7, 17, 12))],
    "folder_open": [("poly", (3, 5, 10, 5, 12, 8, 21, 8, 21, 11, 6, 11, 3, 20, 3, 5)),
                    ("poly", (6, 11, 23, 11, 20, 20, 3, 20))],
    "save": [("poly", (3, 3, 17, 3, 21, 7, 21, 21, 3, 21)), ("rect", (7, 3, 15, 9)), ("rect", (7, 14, 17, 21))],
    # 品牌火花（FORGE 火焰）：外焰单笔闭合 + 内焰小瓣。任务书示例图顶部
    # 即火焰图标 + FORGE；颜色由调用方用 gui_theme 的 brand_spark token
    # （橙），不在此写死。
    "flame": [("poly", (12, 1, 15.5, 6, 20, 10.5, 18.5, 16.5, 13, 21.5,
                        12, 23, 11, 21.5, 5.5, 16.5, 4, 10.5, 8.5, 6)),
              ("poly", (12, 8, 14.5, 11.5, 13.5, 15.5, 10.5, 14.5, 11, 11.5))],
}

ICONS['clock'] = [('circle',(3,3,21,21)),('line',(12,6,12,12,16,14))]
ICONS['connector'] = [('rect',(7,8,17,17)),('line',(9,3,9,8)),('line',(15,3,15,8)),('line',(12,17,12,22))]

ALIASES = {
    '◷':'clock', '🔌':'connector',
    "💬": "chat", "✅": "check_circle", "☑": "task", "🤖": "agents", "🧰": "tools",
    "📚": "knowledge", "🧬": "evolution", "📁": "files", "📂": "folder_open", "📄": "file",
    "🐍": "code", "⚙": "config", "⚙️": "config", "🔍": "search", "⌗": "search",
    "＋": "plus", "+": "plus", "−": "minus", "✕": "close", "×": "close", "✖": "close",
    "⋯": "more", "…": "more", "▸": "chevron_right", "▾": "chevron_down", "▴": "chevron_up", "⌃": "chevron_up",
    "⌄": "chevron_down", "◀": "chevron_left", "▶": "play", "↑": "send", "■": "stop",
    "⟳": "refresh", "↗": "external", "☷": "sidebar", "☰": "sidebar", "▤": "workspace",
    "⑂": "diff", "✓": "check", "✔": "check", "⚠": "warning", "⚠️": "warning",
    "❌": "error", "✗": "error", "ℹ": "info", "⧉": "copy", "👍": "thumb_up", "👎": "thumb_down",
    "📎": "paperclip", "🔑": "key", "▣": "model", "⬡": "model", "☷": "hexagram", "✎": "edit", "🖼": "image",
    "📋": "task", "💾": "save",
}


def icon_key(value):
    return value if value in ICONS else ALIASES.get(value)


def split_icon_text(text):
    prefix, separator, rest = text.partition(" ")
    key = icon_key(prefix)
    return (key, rest.lstrip()) if key and separator else (None, text)


def draw_icon(canvas, name, *, x=0, y=0, size=24, fg="#9A9AA8", tag="icon"):
    name = icon_key(name) or "file"
    factor = size / 24
    width = max(1.2, factor * 1.7)
    for kind, points in ICONS[name]:
        coordinates = [v * factor + (x if i % 2 == 0 else y) for i, v in enumerate(points)]
        if kind == "line":
            canvas.create_line(*coordinates, fill=fg, width=width, capstyle=tk.ROUND,
                               joinstyle=tk.ROUND, tags=tag)
        elif kind == "circle":
            canvas.create_oval(*coordinates, outline=fg, width=width, tags=tag)
        elif kind == "rect":
            canvas.create_rectangle(*coordinates, outline=fg, width=width, tags=tag)
        elif kind == "dot":
            canvas.create_oval(*coordinates, outline="", fill=fg, tags=tag)
        else:
            canvas.create_polygon(*coordinates, outline=fg, fill="", width=width,
                                  joinstyle=tk.ROUND, tags=tag)


class IconCanvas(tk.Canvas):
    def __init__(self, parent, name, *, size=20, bg="#141414", fg="#9A9AA8", command=None):
        super().__init__(parent, width=size, height=size, bg=bg, bd=0, highlightthickness=0,
                         takefocus=bool(command), cursor="hand2" if command else "arrow")
        self.name, self._fg, self._size = (icon_key(name) or "file") if name else None, fg, size
        self._text = name
        self._command = command
        self._base_bg = bg
        self._state = tk.NORMAL
        self._hovered = False
        self._pressed = False
        self._focused = False
        if command:
            self.bind("<Return>", self._activate)
            self.bind("<space>", self._activate)
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)
            self.bind("<ButtonPress-1>", self._press)
            self.bind("<ButtonRelease-1>", self._release, add="+")
            self.bind("<FocusIn>", self._focus_in)
            self.bind("<FocusOut>", self._focus_out)
        self._draw()

    @staticmethod
    def _blend(color, target, amount):
        """Blend #RRGGBB colors without timers or per-frame animation."""
        if not (isinstance(color, str) and len(color) == 7 and color.startswith("#")):
            return color
        try:
            source = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        except ValueError:
            return color
        mixed = tuple(round(value + (goal - value) * amount) for value, goal in zip(source, target))
        return "#%02X%02X%02X" % mixed

    def _visual_bg(self):
        if self._state == tk.DISABLED:
            return self._base_bg
        if self._pressed:
            return self._blend(self._base_bg, (0, 0, 0), .14)
        if self._hovered:
            return self._blend(self._base_bg, (255, 255, 255), .10)
        return self._base_bg

    def _visual_fg(self):
        return self._blend(self._fg, (128, 128, 128), .52) if self._state == tk.DISABLED else self._fg

    def _redraw_visual(self):
        if not hasattr(self, "name"):
            return
        enabled = self._state == tk.NORMAL and self._command
        super().configure(bg=self._visual_bg(), cursor="hand2" if enabled else "arrow",
                          takefocus=bool(enabled))
        self._draw()

    def _activate(self, _event=None):
        if self._state == tk.NORMAL and self._command:
            self._command()
        return "break"

    def _enter(self, _event=None):
        self._hovered = self._state == tk.NORMAL
        self._redraw_visual()

    def _leave(self, _event=None):
        self._hovered = self._pressed = False
        self._redraw_visual()

    def _press(self, _event=None):
        if self._state == tk.NORMAL:
            self._pressed = True
            self._redraw_visual()
            self._command()

    def _release(self, _event=None):
        if self._pressed:
            self._pressed = False
            self._redraw_visual()

    def _focus_in(self, _event=None):
        self._focused = True
        self._redraw_visual()

    def _focus_out(self, _event=None):
        self._focused = False
        self._redraw_visual()

    def _draw(self):
        self.delete("icon")
        self.delete("focus")
        if self.name:
            draw_icon(self, self.name, size=self._size, fg=self._visual_fg())
        if self._focused and self._state == tk.NORMAL:
            inset = max(1, round(self._size / 16))
            self.create_rectangle(inset, inset, self._size - inset, self._size - inset,
                                  outline="#A19BFF", width=1, tags="focus")

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        if "fg" in kwargs:
            self._fg = kwargs.pop("fg")
        if "bg" in kwargs:
            self._base_bg = kwargs["bg"]
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            if self._state != tk.NORMAL:
                self._hovered = self._pressed = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            self.name = icon_key(self._text.strip()) if self._text else None
        result = super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)
        if hasattr(self, "name"):
            self._redraw_visual()
        return result

    config = configure

    def cget(self, key):
        if key == "fg":
            return self._fg
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "ui"
_manifest = None


def _metadata():
    global _manifest
    if _manifest is None:
        try:
            _manifest = json.loads((_ROOT / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _manifest = {"icons": {}, "emoji": {}}
    return _manifest


def _tile(master, atlas_name, index, tile_size):
    # Cache on the owning Tk interpreter, never on the process/default root.
    owner = master._root()
    cache = getattr(owner, "_forge_ui_images", None)
    if cache is None:
        cache = owner._forge_ui_images = {}
    key = (atlas_name, index, tile_size)
    if key in cache:
        return cache[key]
    try:
        # Tk's PNG decoder stalls the UI on the full 1,382-emoji sheet.
        # Build-time pages cap each decode to 256 sprites, retaining exact
        # pixels and interpreter-local caching without a runtime dependency.
        source_name, source_index = atlas_name, index
        if atlas_name.startswith('emoji-'):
            page_name = atlas_name[:-4] + f'-page-{index // 256}.png'
            if (_ROOT / page_name).is_file():
                source_name, source_index = page_name, index % 256
        atlas_key = ("atlas", source_name)
        if atlas_key not in cache:
            cache[atlas_key] = tk.PhotoImage(master=master, file=str(_ROOT / source_name))
        atlas = cache[atlas_key]
        x, y = (source_index % 32) * tile_size, (source_index // 32) * tile_size
        tile = tk.PhotoImage(master=master, width=tile_size, height=tile_size)
        tile.tk.call(str(tile), "copy", str(atlas), "-from", x, y, x + tile_size, y + tile_size)
        cache[key] = tile
        return tile
    except (tk.TclError, OSError):
        return None


PALETTE = {"muted": "#A2ADC5", "text": "#EDF1F7", "accent": "#7B9CFF",
           "ok": "#48B89A", "warn": "#E5B45C", "error": "#E57373",
           "disabled": "#8894AD"}


def icon_image(master, name, *, size=20, fg="#9A9AA8"):
    name = icon_key(name)
    index = _metadata()["icons"].get(name)
    if index is None:
        return None
    def distance(color):
        try:
            return sum((int(fg[i:i+2], 16) - int(color[i:i+2], 16)) ** 2 for i in (1, 3, 5))
        except ValueError:
            return 0
    color = min(PALETTE, key=lambda key: distance(PALETTE[key]))
    size = 20 if size <= 20 else 24 if size <= 24 else 28
    return _tile(master, f"icons-{color}-{size}.png", index, size)


class IconButton(tk.Button):
    """Native activation, bitmap icons and optional event-driven visual feedback."""
    _COLOR_ALIASES = {"background": "bg", "foreground": "fg"}

    def __init__(self, parent, *, icon=None, icon_size=20, **kwargs):
        self._feedback = None
        kwargs = {self._COLOR_ALIASES.get(key, key): value
                  for key, value in kwargs.items()}
        self._explicit_icon = icon_key(icon) if icon else None
        self._icon_size = icon_size
        self._icon_name = None
        text = kwargs.get("text", "")
        localized_text = text
        text = i18n.resolve(text, parent)
        kwargs["text"] = text
        name, label = split_icon_text(text)
        if icon_key(text):
            name, label = icon_key(text), ""
        self._icon_name = self._explicit_icon or name
        fg = (kwargs.get("disabledforeground", "#9A9AA8") if kwargs.get("state") == tk.DISABLED
              else kwargs.get("fg", "#9A9AA8"))
        image = icon_image(parent, self._icon_name, size=icon_size, fg=fg)
        if image is not None:
            kwargs.update(text=label, image=image, compound=tk.LEFT)
        super().__init__(parent, **kwargs)
        self._icon_reference = image
        i18n.remember(self, localized_text, method="_set_localized_text")

    def _set_localized_text(self, text):
        self.configure(text=text)

    def set_feedback(self, palette):
        """Theme supplies colours; native Button keeps mouse/Space activation.

        Keep intended colours separately from transient hover/disabled paint so
        status updates made while disabled are restored correctly on enable.
        No animation, timer, global binding or replacement widget is involved.
        """
        first = self._feedback is None
        self._feedback = dict(palette)
        custom_border = getattr(self, "_feedback_outline_override", None)
        if custom_border is not None:
            self._feedback["hover_border"] = custom_border
        if first:
            keys = ("bg", "fg", "activebackground", "activeforeground", "highlightbackground",
                    "highlightcolor", "disabledforeground", "cursor", "takefocus")
            self._feedback_base = {key: self.cget(key) for key in keys}
            self._feedback_hovered = self._feedback_pressed = self._feedback_focused = False
            for sequence, changes in (
                ("<Enter>", {"hovered": True}), ("<Leave>", {"hovered": False, "pressed": False}),
                ("<ButtonPress-1>", {"pressed": True}), ("<ButtonRelease-1>", {"pressed": False}),
                ("<FocusIn>", {"focused": True}), ("<FocusOut>", {"focused": False, "pressed": False}),
            ):
                self.bind(sequence, lambda _e, values=changes: self._change_feedback(**values), add="+")
            self.bind("<Return>", self._keyboard_invoke, add="+")
        self._feedback_visual = None
        self._paint_feedback()
        return self

    def _keyboard_invoke(self, _event=None):
        self.invoke()  # Native invoke already rejects disabled state.
        return "break"

    def _change_feedback(self, **changes):
        for key, value in changes.items():
            setattr(self, "_feedback_" + key, value)
        self._paint_feedback()

    def _paint_feedback(self):
        if self._feedback is None or not self.winfo_exists():
            return
        base, palette = self._feedback_base, self._feedback
        disabled = str(self.cget("state")) == tk.DISABLED
        if disabled:
            self._feedback_hovered = self._feedback_pressed = False
            bg, fg, border = palette["disabled_bg"], base["disabledforeground"], palette["disabled_border"]
        else:
            active = self._feedback_hovered or self._feedback_pressed
            bg = (palette["pressed_bg"] if self._feedback_pressed else
                  base["activebackground"] if self._feedback_hovered else base["bg"])
            fg = base["activeforeground"] if active else base["fg"]
            border = (base["highlightcolor"] if self._feedback_focused else
                      palette["hover_border"] if active else base["highlightbackground"])
        visual = {"bg": bg, "fg": fg, "activebackground": bg, "activeforeground": fg,
                  "highlightbackground": border,
                  "cursor": "arrow" if disabled else base["cursor"],
                  "takefocus": 0 if disabled else base["takefocus"]}
        if visual == self._feedback_visual:
            return
        tk.Button.configure(self, **visual)
        if self._icon_name:
            image = icon_image(self, self._icon_name, size=self._icon_size, fg=fg)
            if image is not None:
                tk.Button.configure(self, image=image, compound=tk.LEFT)
                self._icon_reference = image
        self._feedback_visual = visual

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        kwargs = {self._COLOR_ALIASES.get(key, key): value
                  for key, value in kwargs.items()}
        if self._feedback is not None and kwargs:
            for key, value in kwargs.items():
                if key in self._feedback_base:
                    self._feedback_base[key] = value
                if key == "highlightbackground":
                    # A caller's semantic outline (e.g. orange planning) must
                    # survive hovering, not revert to the default purple/grey.
                    self._feedback["hover_border"] = value
                    self._feedback_outline_override = value
            self._feedback_visual = None
        if "text" in kwargs:
            i18n.remember(self, kwargs["text"], method="_set_localized_text")
            text = i18n.resolve(kwargs["text"], self)
            kwargs["text"] = text
            name, label = split_icon_text(text)
            if icon_key(text):
                name, label = icon_key(text), ""
            self._icon_name = self._explicit_icon or name
            kwargs["text"] = label
        if self._icon_name and any(key in kwargs for key in ("text", "fg", "state", "disabledforeground")):
            disabled = str(kwargs.get("state", self.cget("state"))) == tk.DISABLED
            fg = (kwargs.get("disabledforeground", self.cget("disabledforeground")) if disabled
                  else kwargs.get("fg", self.cget("fg")))
            image = icon_image(self, self._icon_name, size=self._icon_size, fg=fg)
            if image is not None:
                kwargs.update(image=image, compound=tk.LEFT)
                self._icon_reference = image
        elif "text" in kwargs and not self._icon_name:
            kwargs["image"] = ""
            self._icon_reference = None
        result = super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)
        if kwargs:
            self._paint_feedback()
        return result

    config = configure


def emoji_parts(text):
    """Yield complete emoji clusters; unknown clusters remain exact Unicode text."""
    def starts_at(position):
        cp = ord(text[position])
        return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                or cp in (0x231A, 0x231B, 0x23F0, 0x23F3)
                or (text[position] in "0123456789#*"
                    and (text[position+1:position+2] == "\u20e3"
                         or text[position+1:position+3] == "\ufe0f\u20e3")))
    index = 0
    while index < len(text):
        char = text[index]
        cp = ord(char)
        if not starts_at(index):
            end = index + 1
            while end < len(text) and not starts_at(end):
                end += 1
            yield text[index:end], False
            index = end
            continue
        end = index + 1
        if 0x1F1E6 <= cp <= 0x1F1FF and end < len(text) and 0x1F1E6 <= ord(text[end]) <= 0x1F1FF:
            end += 1
        while end < len(text):
            other = ord(text[end])
            if other in (0xFE0E, 0xFE0F, 0x20E3) or 0x1F3FB <= other <= 0x1F3FF or 0xE0020 <= other <= 0xE007F:
                end += 1
            elif other == 0x200D and end + 1 < len(text):
                end += 2
            else:
                break
        yield text[index:end], True
        index = end


def emoji_image(master, cluster, *, size=24):
    # FE0E explicitly requests text presentation; honor it.
    if "\ufe0e" in cluster:
        return None
    normalized = cluster.replace("\ufe0f", "")
    index = _metadata()["emoji"].get(normalized)
    if index is None:
        return None
    size = 20 if size <= 20 else 24 if size <= 24 else 32
    return _tile(master, f"emoji-{size}.png", index, size)
