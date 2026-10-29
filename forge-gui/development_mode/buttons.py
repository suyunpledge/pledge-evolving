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
DEV_BUTTON_KINDS = ('primary', 'ghost', 'quiet', 'danger', 'accent_soft', 'ok')

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
    'ok': lambda base: (C['ok'], '#0B0B10', '#34D399', '#0B0B10', C['ok']),
}


def _bg_of(widget):
    try:
        return widget.cget('bg')
    except tk.TclError:
        return C['bg']


def dev_button(parent, text, command, *, kind='ghost', icon=None, icon_size=16,
               tooltip=None, font=None, padx=12, pady=5, state=tk.NORMAL, **kw):
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
    # v0.8：键盘可达——Tab 能走到、Enter 能触发（与 Space 同语义）。
    # tk.Button 默认只响应 Space，Enter 触发是桌面产品的通行预期
    # （对话框里按 Enter 确认），不补这一条，键盘用户会觉得"按钮按不动"。
    kw.setdefault('takefocus', True)
    btn = IconButton(parent, text=text, command=command, icon=icon,
                     icon_size=icon_size, bg=bg, fg=fg,
                     activebackground=hovbg, activeforeground=hovfg,
                     font=font or FONT_SMALL, relief=tk.FLAT, bd=0,
                     padx=padx, pady=pady, cursor='hand2', highlightthickness=1,
                     highlightbackground=border, highlightcolor=C['focus_ring'],
                     disabledforeground=C['disabled_fg'], state=state, **kw)
    if icon_only:
        btn.configure(width=max(26, icon_size + 10),
                      height=max(26, icon_size + 10))
    button_feedback(btn, kind=kind)
    btn.bind('<Return>', lambda _e: (btn.invoke(), 'break')[-1])
    btn.bind('<KP_Enter>', lambda _e: (btn.invoke(), 'break')[-1])
    if tooltip:
        attach_tooltip(btn, tooltip)
    return btn


# ── 键帽（kbd）──────────────────────────────────────────────────
# 成熟产品里快捷键是「键帽 + 说明」的固定语法（VS Code 的快捷键面板、
# Linear 的 ⌘K 提示、GitHub 的 <kbd>）。Coding Mode 此前把键位写成
# 一行纯文字（'Enter 发送'），既没有键帽的边界感，也没法区分修饰键
# 与字母键——扫读时和正文糊在一起，这就是"毛坯"的来源。
def keycap(parent, text, *, bg=None, tone='normal'):
    """单个键帽：4px 圆角 + 1px 描边 + 等宽 9px。

    ``tone='quiet'`` 用于低对比场景（浮层底部、深色输入带），
    只留描边不上底色，避免一排键帽把视线拽走。
    ``tone='accent'`` 用于高亮场景（推荐快捷键、活动模式）。
    """
    from gui_theme import FONT_MONO_XS, rounded_label
    base = bg or _bg_of(parent)
    if tone == 'accent':
        fill, outline, fg = C['accent_soft'], C['accent_border'], C['accent_text']
    elif tone == 'normal':
        fill, outline, fg = C['surface2'], C['border_hi'], C['subtext']
    else:  # quiet
        fill, outline, fg = base, C['border'], C['ter']
    return rounded_label(
        parent, text,
        fill=fill,
        outline=outline,
        fg=fg,
        font=FONT_MONO_XS, radius=4, padx=5, pady=1, bg=base,
        # 键帽没有外层 padding，画布就是控件边界；inset=1 才能让四条
        # 1px 描边都落在画布内（0.5 会裁掉右边和下边，键帽看起来像
        # 缺了半圈边框）。
        inset=1)


def key_hint(parent, key, text, *, bg=None, gap=6, tone='normal'):
    """``[Ctrl+P] 快速打开文件`` —— 键帽 + 说明的一体化提示。

    ``key`` 是 shortcuts 里的动作 id；文案与键位都从键位表取，
    提示和真实绑定不会再各说各话。
    """
    from . import shortcuts
    base = bg or _bg_of(parent)
    row = tk.Frame(parent, bg=base)
    keycap(row, shortcuts.hint(key, key), bg=base, tone=tone).pack(side='left')
    if text:
        tk.Label(row, text=text, bg=base,
                 fg=C['accent_text'] if tone == 'accent' else C['subtext'] if tone == 'normal' else C['ter'],
                 font=FONT_CAPTION).pack(side='left', padx=(gap, 0))
    return row
