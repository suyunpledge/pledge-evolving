"""W3：dev-mode 统一按钮与交互组件规范。

背景（2026-10-18 W3 工单）：dev-mode 里按钮有三种写法并存——
裸 ``ttk.Button``（无统一尺寸/状态反馈/提示）、手写 ``IconButton``
（各自配 padx/pady/颜色）、宿主侧 ``pill_button``（仅在 forge_gui_v2）。
视觉层级、命中尺寸、悬停/按下/禁用反馈不一致。

本模块提供 dev-mode 侧的统一工厂 :func:`dev_button`：

* 尺寸——主/次按钮命中高度 ≥24px（padx=12/pady=5 + 字体自然高度），
  图标按钮命中区 ≥24px；
* 视觉层级——kind ∈ primary / ghost / quiet / danger / accent_soft，
  色板与宿主 pill_button 同源（gui_theme.C token，不另起一套）；
* 反馈——全部经 gui_theme.button_feedback 挂 hover/pressed/disabled
  三态（含焦点环 focus_ring 与 disabledforeground）；
* 图标——走 ui_icons.IconButton 的矢量图标（icon= 键名），
  尺寸默认 16，随文字按钮 compound=LEFT；
* 提示——tooltip 经 gui_theme.attach_tooltip（可选参数）。

不复制宿主 pill_button 的实现（那在 forge_gui_v2 里 import 不到），
但色板与行为契约保持一致；dev-mode 内新增按钮一律走本工厂，
存量裸 ttk.Button 在 W3 分批替换（编辑器工具栏 / Agent 面板 / 对话框）。
"""
import tkinter as tk

from gui_theme import C, FONT_SMALL, FONT_CAPTION, button_feedback, attach_tooltip
from ui_icons import IconButton

# 视觉层级集合（与宿主 pill_button 的 kind 语义对齐）
DEV_BUTTON_KINDS = ('primary', 'ghost', 'quiet', 'danger', 'accent_soft')

# kind -> (bg, fg, hover_bg, hover_fg, border)
_PALETTES = {
    'primary': lambda base: (C['accent'], C['text'], C['accent_hover'], C['text'],
                             C['accent_border']),
    'ghost': lambda base: (C['surface2'], C['body'], C['hover'], C['text'],
                           C['border_hi']),
    'quiet': lambda base: (base, C['ter'], C['hover'], C['text'], base),
    'danger': lambda base: (C['surface2'], C['error'], C['error_soft'], C['error'],
                            C['border_hi']),
    'accent_soft': lambda base: (C['accent_soft'], C['accent_text'], C['accent'],
                                 '#FFFFFF', C['accent_border']),
}


def _bg_of(widget):
    try:
        return widget.cget('bg')
    except tk.TclError:
        return C['bg']


def dev_button(parent, text, command, *, kind='ghost', icon=None, icon_size=16,
               tooltip=None, font=None, padx=12, state=tk.NORMAL, **kw):
    """统一按钮工厂：尺寸/层级/反馈/图标/提示一处定版。

    参数与宿主 pill_button 对齐（kind 语义相同）；额外支持
    tooltip（attach_tooltip）与 icon_size（默认 16，矢量图标键名）。
    其余 **kw 原样传给 IconButton（width/height/anchor 等）。
    """
    if kind not in DEV_BUTTON_KINDS:
        kind = 'ghost'
    bg, fg, hovbg, hovfg, border = _PALETTES[kind](_bg_of(parent))
    # 纯图标按钮（无文字）命中区提升：tk.Button 的 padx/pady 在 image
    # 模式下不生效（Tk 按图像尺寸定几何），改用 width/height 像素覆盖。
    icon_only = not text and icon is not None
    btn = IconButton(parent, text=text, command=command, icon=icon,
                     icon_size=icon_size, bg=bg, fg=fg,
                     activebackground=hovbg, activeforeground=hovfg,
                     font=font or FONT_SMALL, relief=tk.FLAT, bd=0,
                     padx=padx, pady=5, cursor='hand2', highlightthickness=1,
                     highlightbackground=border, highlightcolor=C['focus_ring'],
                     disabledforeground=C['disabled_fg'], state=state, **kw)
    if icon_only:
        btn.configure(width=max(26, icon_size + 10),
                      height=max(26, icon_size + 10))
    button_feedback(btn, kind=kind)
    if tooltip:
        attach_tooltip(btn, tooltip)
    return btn
