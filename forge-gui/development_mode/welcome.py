"""M2 欢迎页（产品化收紧版）：未打开项目时中央区的占位视图。

不再是「标题 + 一行说明 + 打开按钮 + 文字近期项目」这样的工程师页，
按「产品化 IDE」惯例：品牌区 + 3 张快捷动作卡片 + 近期项目（带类型
图标与时间戳）+ 快捷键提示行。所有元素都走 ttk / tk 包装，颜色统一
走 gui_theme.C；hover/active 走标准 _hover / _leave 钩子。

挂载与切换在 ``app.py``：``_build_welcome`` 建视图，``_sync_welcome``
按 ``self.source`` 二态切换中央区（无项目 → 欢迎页；有项目 → 编辑器
本体）。切换只发生在 editor_frame 内的两个子帧之间（pack/pack_forget），
**不新增 pane 挂载路径**——apply_layout 的增量挂载逻辑（51f6f82 保护
清单）与 M1-fix 的 ``_settle_mount`` 冲刷均不受影响。
"""
import os
import time
import tkinter as tk
from tkinter import ttk

from gui_theme import (C, FONT_BRAND, FONT_SECTION, FONT_UI, FONT_UI_BOLD,
                       FONT_CAPTION, FONT_MICRO, FONT_SMALL,
                       RoundedCard, R_CARD, chip, divider)
from decor import dot_grid
from ui_icons import icon_image


# 3 张动作卡（icon / 标题 / 描述 / 命令）— 实际命令由 workbench 注入。
# icon 必须命中 ui_icons.ICONS 表；这里只挑已存在的：
#   files  打开项目（folder）
#   diff   克隆仓库（git pull → 变更集图）
#   plus   新建文件（+）
ACTIONS = (
    ('files',  '打开项目',  '选择本地目录作为工作区', 'open'),
    ('diff',   '克隆仓库',  '从 Git URL 拉取并打开',     'clone'),
    ('plus',   '新建文件',  '在工作区创建空文件',       'new_file'),
)


def _format_recent_time(path):
    """路径最近修改时间 → 友好字符串（无 mtime 取空串）。"""
    try:
        if not path or not os.path.exists(path):
            return ''
        mtime = os.path.getmtime(path)
        age = time.time() - mtime
        if age < 60:
            return '刚刚'
        if age < 3600:
            return f'{int(age / 60)} 分钟前'
        if age < 86400:
            return f'{int(age / 3600)} 小时前'
        return f'{int(age / 86400)} 天前'
    except Exception:
        return ''


def _detect_kind(path):
    """根据路径猜项目类型（用于显示图标）。"""
    if not path:
        return 'files'
    name = os.path.basename(path.rstrip('/\\')).lower()
    if name in ('.git',):
        return 'git'
    if name in ('.svn', '.hg'):
        return 'git'
    if os.path.isfile(os.path.join(path, 'pyproject.toml')) or \
       os.path.isfile(os.path.join(path, 'setup.py')) or \
       os.path.isfile(os.path.join(path, 'requirements.txt')):
        return 'python'
    if os.path.isfile(os.path.join(path, 'package.json')):
        return 'web'
    if os.path.isfile(os.path.join(path, 'Cargo.toml')):
        return 'rust'
    if os.path.isfile(os.path.join(path, 'go.mod')):
        return 'go'
    if os.path.isfile(os.path.join(path, 'pom.xml')) or \
       os.path.isfile(os.path.join(path, 'build.gradle')):
        return 'java'
    return 'files'


class WelcomeView(ttk.Frame):
    """产品化欢迎页：品牌 + 3 动作卡 + 近期项目 + 快捷键。"""

    def __init__(self, master, *, on_open, on_recent, on_clone=None,
                 recent_provider, shortcuts_provider=None, **kw):
        super().__init__(master, **kw)
        self._on_open = on_open
        self._on_recent = on_recent
        self._on_clone = on_clone
        self._recent_provider = recent_provider
        self._shortcuts_provider = shortcuts_provider
        self.recent_paths = []
        # 缓存各动作卡的内部控件（hover 效果用）
        self._action_cards = []
        self._build()

    # ── 公开访问器（测试 / 验收探针用）────────────────────────────
    @property
    def open_button(self):
        """「打开项目」按钮（兼容旧测试，指向第一张动作卡）。"""
        return self._action_cards[0].action_button if self._action_cards else None

    @property
    def action_cards(self):
        """3 张动作卡（hover 效果测试用）。"""
        return self._action_cards

    def recent_count(self):
        return len(self.recent_paths)

    # ── 构建 ──────────────────────────────────────────────────────
    def _build(self):
        # 留足够 padding 让 hero 在 1280-1920 窗口下都不挤
        self.configure(padding=(56, 36, 56, 24))

        # ① 品牌区：火花 + 标题 + 副标题，右侧点阵纹理（与 Work Mode 同一套
        #    decor 词汇，但用点阵而非主视觉横幅——工作台首屏要保持轻）
        brand = tk.Frame(self, bg=C['bg'])
        brand.pack(anchor='w', fill='x')
        # 火花符号（保留原有"FORGE 火花"）
        spark = tk.Canvas(brand, width=28, height=28, bg=C['bg'],
                          highlightthickness=0, bd=0)
        spark.create_polygon(14, 2, 18, 12, 26, 14, 18, 16,
                             14, 26, 10, 16, 2, 14, 10, 12,
                             smooth=True, fill=C['accent'], outline='')
        spark.pack(side='left', padx=(0, 10))
        tk.Label(brand, text='FORGE Coding', font=FONT_BRAND, bg=C['bg'],
                 fg=C['accent_text']).pack(side='left')
        # 右侧点阵纹理：低对比度背景肌理，让首屏不再是一片纯色
        texture = tk.Canvas(brand, width=180, height=30, bg=C['bg'],
                            highlightthickness=0, bd=0)
        texture.pack(side='right', padx=(0, 4))
        dot_grid(texture, 0, 0, 180, 30, bg=C['bg'], fg=C['accent2'],
                 step=13, r=0.9, amounts=(0.55, 0.78), tag='decor')
        tk.Label(self, text='打开或克隆一个项目，与 Agent 协同编码。',
                 font=FONT_UI, bg=C['bg'], fg=C['subtext']).pack(
            anchor='w', pady=(6, 20))

        # ② 3 张动作卡（RoundedCard 圆角卡，与 Work Mode 同一视觉语言）
        action_row = tk.Frame(self, bg=C['bg'])
        action_row.pack(anchor='w', fill='x', pady=(0, 20))
        for column, (glyph, title, desc, key) in enumerate(ACTIONS):
            card = self._build_action_card(action_row, glyph, title, desc, key)
            card.grid(row=0, column=column, padx=(0, 12), sticky='nsew')
            action_row.columnconfigure(column, weight=1, uniform='action')
            self._action_cards.append(card)

        # ③ 近期项目：标题 + 计数 + 分隔线 + 列表（按 mtime 倒序）
        recent_header = tk.Frame(self, bg=C['bg'])
        recent_header.pack(anchor='w', fill='x', pady=(0, 10))
        tk.Label(recent_header, text='近期项目', font=FONT_SECTION, bg=C['bg'],
                 fg=C['text']).pack(side='left')
        self._recent_count_lbl = tk.Label(recent_header, text='', font=FONT_CAPTION,
                                          bg=C['bg'], fg=C['ter'])
        self._recent_count_lbl.pack(side='left', padx=(10, 0))
        divider(self, color=C['border']).pack(fill='x', pady=(0, 10))
        # 列表容器（滚动窗口预留）
        self._recent_box = tk.Frame(self, bg=C['bg'])
        self._recent_box.pack(anchor='w', fill='x')

        # ④ 快捷键 chips（替代原来的纯文字行——chips 与 Work Mode 输入区同语言）
        shortcuts_bar = tk.Frame(self, bg=C['bg'])
        shortcuts_bar.pack(anchor='w', fill='x', pady=(24, 0))
        for hint in self._get_shortcut_chips():
            chip(shortcuts_bar, hint, bg=C['bg']).pack(side='left', padx=(0, 8))

        # 启动时先刷一次
        self.refresh()

    def _build_action_card(self, parent, glyph, title, desc, key):
        """单张动作卡：RoundedCard 圆角底 + 图标 + 标题 + 描述 + 点击/焦点。

        视觉与 Work Mode 的动作卡同源（gui_theme.RoundedCard：圆角 + surface
        填充 + hairline 描边），hover 走 set_fill 换底色，不再用裸 tk.Frame
        的直角边框——那是 Coding Mode 显得"简朴"的主要原因之一。
        """
        card = self._rounded_card(parent)
        # 图标（28×28，与品牌区火花一致）
        icon_holder = tk.Frame(card.content, bg=C['surface'], width=28, height=28)
        icon_holder.pack(anchor='w')
        icon_holder.pack_propagate(False)
        img = icon_image(icon_holder, glyph, size=20, fg=C['accent'])
        if img is not None:
            icon_lbl = tk.Label(icon_holder, image=img, bg=C['surface'], bd=0)
            icon_lbl.image = img
            icon_lbl.pack(expand=True)
        # 标题 + 描述（描述宽度跟随卡片实际宽度换行——固定 wraplength 在窄窗口
        # 会把「工作区创建空文件」横向切掉半句，观感毛坯。_fit_desc 在卡片
        # Resize 时同步 wraplength，宽窗口照常一行放下。）
        tk.Label(card.content, text=title, font=FONT_UI_BOLD, bg=C['surface'],
                 fg=C['text']).pack(anchor='w', pady=(8, 2))
        desc_lbl = tk.Label(card.content, text=desc, font=FONT_CAPTION,
                            bg=C['surface'], fg=C['subtext'],
                            wraplength=176, justify='left')
        desc_lbl.pack(anchor='w', fill='x', pady=(2, 0))

        def _fit_desc(_event=None, lbl=desc_lbl, box=card.content):
            try:
                width = box.winfo_width()
                if width > 48:
                    lbl.configure(wraplength=max(64, width - 6))
            except tk.TclError:
                pass
        desc_lbl.bind('<Configure>', _fit_desc)
        # sash 压缩卡片时 label 自身可能不触发 Configure——同时挂 content 层
        # P0-1（Kimi 审查）：必须 add='+'——不带 add 的 bind 会整体替换
        # RoundedCard 在 content 上注册的 _on_content（高度自适应），
        # 画布永远停在 height=1，三张卡坍缩成不可见。
        card.content.bind('<Configure>', _fit_desc, add='+')
        card.desc_label = desc_lbl  # v0.5：验证探针 / 后续主题化用
        # 不可见 button（承担点击 + 键盘焦点）。
        #
        # ★ 渲染修复：tk.Button 必须 lower() 回栈底。place() 默认把新控件
        # 抬到同级栈顶，而它带不透明 bg——会把此前 pack 进 content 的
        # 图标 / 标题 / 描述整块盖住，卡片在屏幕上退化成三个空方块
        # （"毛坯房"观感的主因）。lower() 后 content 回到上层，内容重新
        # 可见；点击路径不受影响：卡片内绝大多数区域由 content 覆盖，
        # 其上已绑 <Button-1>；未盖到的 padding 区仍会命中这个 button。
        # 键盘焦点与 Enter/Space 激活也继续由它承担。
        button = tk.Button(card, text='', bd=0, highlightthickness=0,
                            bg=C['surface'], activebackground=C['hover'],
                            cursor='hand2', command=self._on_action(key))
        button.place(relx=0, rely=0, relwidth=1, relheight=1)
        button.lower()
        # 键盘焦点环画在卡片描边上（button 本身被内容遮住，看不见）
        button.bind('<FocusIn>', lambda _e, c=card: self._paint_focus(c, True))
        button.bind('<FocusOut>', lambda _e, c=card: self._paint_focus(c, False))
        # hover 效果（卡片整张换底色）
        for w in (card, card.content, icon_holder):
            w.bind('<Enter>', lambda _e, c=card: self._paint_card(c, hover=True))
            w.bind('<Leave>', lambda _e, c=card: self._paint_card(c, hover=False))
            w.bind('<Button-1>', lambda _e, k=key: self._on_action(k)())
        button.bind('<Enter>', lambda _e, c=card: self._paint_card(c, hover=True))
        button.bind('<Leave>', lambda _e, c=card: self._paint_card(c, hover=False))
        # 把按钮和 key 都挂在 card 上：调用方需要时直接 card.action_button /
        # card.action_key 取，不再返回 dict（避免 _action_cards.append(dict)
        # 后某处误调 dict.grid()）。
        card.action_button = button
        card.action_key = key
        return card

    @staticmethod
    def _rounded_card(parent):
        """构造圆角动作卡（抽成一函数，便于统一改半径/描边）。"""
        return RoundedCard(parent, radius=R_CARD, fill=C['surface'],
                           outline=C['border_hi'], padx=14, pady=13, bg=C['bg'])

    def _paint_card(self, card, *, hover):
        """整张卡换 hover 态底色（RoundedCard 走 set_fill 重绘圆角底）。"""
        try:
            bg = C['hover'] if hover else C['surface']
            if hasattr(card, 'set_fill'):
                card.set_fill(bg)
            else:
                card.configure(bg=bg)
            for child in card.winfo_children():
                if isinstance(child, tk.Frame):
                    child.configure(bg=bg)
        except tk.TclError:
            pass

    def _paint_focus(self, card, focused):
        """键盘焦点环：卡片描边换 accent（hover 之外的第二种高亮）。"""
        try:
            color = C['accent_border'] if focused else C['border_hi']
            if hasattr(card, 'set_fill'):
                card.set_fill(C['surface'], color)
            else:
                card.configure(highlightbackground=color)
        except tk.TclError:
            pass

    def _on_action(self, key):
        """返回点击该动作卡时实际要执行的回调。"""
        if key == 'open':
            return self._open
        if key == 'clone':
            return self._clone
        if key == 'new_file':
            return self._new_file
        return self._open

    def _clone(self):
        if self._on_clone is not None:
            self._on_clone()
        else:
            # 兜底：复用 open（让 workbench 自己处理）
            self._open()

    def _new_file(self):
        # 暂未在工作台层接「新建文件」入口；先复用 open，等 workbench 加 on_new_file。
        self._open()

    def _open(self):
        if self._on_open is not None:
            self._on_open()

    def _open_recent(self, path):
        if self._on_recent is not None:
            self._on_recent(path)

    def _get_shortcut_chips(self):
        """快捷键 chips 文案列表。

        兼容旧 provider：若宿主提供的是整行文本（空格分隔多个快捷键），
        按空白拆成多枚 chip；若已是列表则直接用。
        """
        raw = None
        if callable(self._shortcuts_provider):
            try:
                raw = self._shortcuts_provider() or ''
            except Exception:
                raw = ''
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw if str(x).strip()]
        if raw:
            parts = [p for p in str(raw).split('  ') if p.strip()]
            if len(parts) > 1:
                return parts
            return [str(raw)]
        return ['Ctrl+Shift+P  命令面板', 'Ctrl+P  快速打开文件', 'Ctrl+B  切换侧栏']

    # ── 数据填充 ─────────────────────────────────────────────────
    def refresh(self):
        """按 recent_provider 重绘近期项目（按 mtime 倒序）。"""
        for child in self._recent_box.winfo_children():
            child.destroy()
        try:
            recent = [str(p) for p in (self._recent_provider() or [])]
        except Exception:
            recent = []
        # 按 mtime 倒序（最近打开的在前）
        def _key(path):
            try:
                return os.path.getmtime(path) if os.path.exists(path) else 0
            except Exception:
                return 0
        recent = sorted(recent, key=_key, reverse=True)
        self.recent_paths = recent
        # 更新右侧计数
        if hasattr(self, '_recent_count_lbl'):
            self._recent_count_lbl.configure(
                text=f'（{len(recent)}）' if recent else '（暂无）')
        if not recent:
            tk.Label(self._recent_box, text='暂无项目记录——打开一个目录后会自动出现在这里。',
                     font=FONT_UI, bg=C['bg'], fg=C['ter'],
                     justify='left').pack(anchor='w', pady=(4, 0))
            return
        for path in recent:
            self._build_recent_row(path)

    def _build_recent_row(self, path):
        """单条近期项目：类型图标 + 路径 + 时间戳。"""
        row = tk.Frame(self._recent_box, bg=C['bg'], cursor='hand2')
        row.pack(anchor='w', fill='x', pady=2)
        # 类型图标
        kind = _detect_kind(path)
        icon_holder = tk.Frame(row, bg=C['bg'], width=18, height=18)
        icon_holder.pack(side='left', padx=(0, 8))
        icon_holder.pack_propagate(False)
        img = icon_image(icon_holder, kind, size=16, fg=C['ter'])
        if img is not None:
            icon_lbl = tk.Label(icon_holder, image=img, bg=C['bg'], bd=0)
            icon_lbl.image = img
            icon_lbl.pack(expand=True)
        # 路径（主）
        name = os.path.basename(path.rstrip('/\\')) or path
        parent = os.path.dirname(path.rstrip('/\\'))
        main_lbl = tk.Label(row, text=name, font=FONT_UI, bg=C['bg'],
                            fg=C['link'], anchor='w', cursor='hand2')
        main_lbl.pack(side='left')
        if parent:
            tk.Label(row, text=f'  {parent}', font=FONT_CAPTION, bg=C['bg'],
                     fg=C['ter'], anchor='w').pack(side='left')
        # 时间戳（右侧）
        ts = _format_recent_time(path)
        if ts:
            tk.Label(row, text=ts, font=FONT_CAPTION, bg=C['bg'],
                     fg=C['ter']).pack(side='right')
        # hover：整行换 surface2 底色 + 主标签提亮，比原来的 hover 更明确
        def _hover(w, entering, label=main_lbl):
            try:
                w.configure(bg=C['hover'] if entering else C['bg'])
                label.configure(fg=C['accent_text'] if entering else C['link'])
            except tk.TclError:
                pass
        for w in (row, main_lbl):
            w.bind('<Enter>', lambda _e, ww=w: _hover(ww, True))
            w.bind('<Leave>', lambda _e, ww=w: _hover(ww, False))
            w.bind('<Button-1>', lambda _e, p=path: self._open_recent(p))
