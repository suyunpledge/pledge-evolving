"""forge 图形界面 v2：管理面 + 交互客户端双标签。

管理面：
  - 配置列表与功能开关（来自当前用户层）
  - "添加 Provider" 面板：粘贴 JSON / 文本 → 矫治器自动整理 → 预览 → 保存
  - 环境变量助手：列出矫治后需要的 env，复制就能用
  - 启停 gateway（一键起 OpenAI 兼容 loopback）

交互客户端：
  - chat 风格的对话列表
  - 流式输出
  - 模型选择（来自已启用 Provider 的 model 字段）

零依赖：仅用 tkinter（Python 自带）+ 本仓 config_model.py + forge_client.py。

启动：
    python forge_gui_v2.py           # 有控制台
    pythonw forge_gui_v2.py          # 无控制台（Windows）
"""
from __future__ import annotations

import copy
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from config_model import (  # noqa: E402
    ConfigNormalizeError,
    load_user_layer,
    merge_with_user_layer,
    normalize,
    save_user_layer,
)
from secret_store import env_for, load as _load_secrets  # noqa: E402
from forge_client import (  # noqa: E402
    ChatMessage,
    ForgeGatewayClient,
    GatewayError,
)

# ─── 常量 ──────────────────────────────────────────────

WINDOW_SIZE = "1220x800"
MIN_SIZE = (940, 700)
FORGE_REPO_HINT = os.environ.get("FORGE_REPO", "").strip()
DEFAULT_FORGE_HOME = Path.home() / ".forge"

IS_WINDOWS = platform.system() == "Windows"

# 调色板 —— 对齐 AutoClaw 设计语言（取自 app.asar 的 --theme-* 浅色主题）
C = {
    "bg": "#f5f5f5",            # theme-bg
    "surface": "#ffffff",        # theme-panel
    "surface2": "#ebebeb",       # theme-surface-active-neutral
    "surface_subtle": "#f5f5f5",  # theme-surface-subtle
    "border": "#e5e5e5",         # theme-border
    "border_hi": "#e6e6e6",      # theme-border-hi
    "text": "#292929",           # theme-text
    "subtext": "#525252",        # theme-text-subtitle
    "muted": "#7a7a7a",          # theme-text-sec
    "ter": "#9e9e9e",            # theme-text-ter
    "placeholder": "#b0b0b0",    # theme-text-placeholder
    "accent": "#fc5d1e",         # theme-accent1（品牌橙）
    "accent_hover": "#e55318",   # theme-accent2
    "accent_soft": "#fdeee7",    # ≈ rgba(252,93,30,.08) on white
    "accent_border": "#fbd9c8",  # ≈ rgba(252,93,30,.16)
    "accent2": "#b8431a",        # 代码/次要强调（白底可读)
    "warn": "#c8872b",
    "warn_soft": "#faf3e8",
    "error": "#df5353",
    "error_soft": "#fdeeee",
    "ok": "#2f9e5b",
    "ok_soft": "#eaf6ef",
    "link": "#3b7dd8",
    "link_soft": "#e8f0fb",
    "info": "#4e7db7",
    "info_soft": "#eef3fa",
    "input_bg": "#ffffff",       # theme-input-bg
    "code_bg": "#f7f7f7",        # theme-code-inline-bg
    "msg_user_bg": "#fdf1ea",    # theme-msg-user 的实色近似
    "msg_agent_bg": "#f7f7f7",   # theme-msg-agent 的实色近似
    "scroll": "#d0d0d0",         # theme-scrollbar-thumb
}

# 圆角（AutoClaw: panel 14 / card 12 / pill 20）
R_PANEL, R_CARD, R_PILL, R_MD, R_SM = 14, 12, 20, 8, 6

# 沉思模式（forge thinking.mode 三档；GUI 里对齐 AutoClaw 工具条「目标模式」的位置）
THINKING_LABELS = {"off": "关闭", "smart": "智能", "on": "开启"}
THINKING_CHOICES = [
    ("off", "关闭", "不启用沉思"),
    ("smart", "智能", "按任务复杂度自动决定（推荐）"),
    ("on", "开启", "始终启用沉思"),
]

FONT_MONO = ("Cascadia Code", 10) if IS_WINDOWS else ("Menlo", 10)
FONT_UI = ("Segoe UI", 10) if IS_WINDOWS else ("Helvetica", 11)
FONT_UI_BOLD = ("Segoe UI", 10, "bold") if IS_WINDOWS else ("Helvetica", 11, "bold")
FONT_TITLE = ("Segoe UI", 20, "bold") if IS_WINDOWS else ("Helvetica", 20, "bold")
FONT_SMALL = ("Segoe UI", 9) if IS_WINDOWS else ("Helvetica", 9)
FONT_SECTION = ("Segoe UI", 12, "bold") if IS_WINDOWS else ("Helvetica", 12, "bold")
FONT_CAPTION = ("Segoe UI", 9) if IS_WINDOWS else ("Helvetica", 9)


def round_rect(canvas: "tk.Canvas", x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（平滑多边形），返回 item id。"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, splinesteps=16, **kw)


def pill_button(parent, text, command, *, kind="ghost", bg=None):
    """AutoClaw 风格的胶囊按钮（tk.Button 近似：扁平 + 内边距 + 圆角感）。"""
    bg = bg or C["bg"]
    palettes = {
        "primary": (C["accent"], "#ffffff", C["accent_hover"]),
        "ghost": (C["surface"], C["subtext"], C["surface2"]),
        "quiet": (bg, C["muted"], C["surface2"]),
        "danger": (C["surface"], C["error"], C["error_soft"]),
    }
    bgb, fg, hov = palettes.get(kind, palettes["ghost"])
    btn = tk.Button(parent, text=text, command=command, bg=bgb, fg=fg,
                    activebackground=hov, activeforeground=fg,
                    font=FONT_UI, relief=tk.FLAT, bd=0, padx=14, pady=6,
                    cursor="hand2", highlightthickness=0)
    return btn


# ─── 工具 ──────────────────────────────────────────────


def _setup_dpi():
    if IS_WINDOWS:
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _find_run_py() -> Path | None:
    """探测 run.py：env FORGE_REPO → 同级 → 上 3 层 → 常见副本（限深）。"""
    if FORGE_REPO_HINT:
        p = Path(FORGE_REPO_HINT) / "run.py"
        if p.is_file():
            return p
    here = HERE
    p = here / "run.py"
    if p.is_file():
        return p
    for parent in [here.parent, here.parent.parent, here.parent.parent.parent]:
        p = parent / "run.py"
        if p.is_file():
            return p
    # 最后扫一下常见的 .openclaw/tmp 副本——限深 4 层、防卡死
    try:
        for candidate in Path(Path.home() / ".openclaw-autoclaw").glob("forge-*/run.py"):
            if candidate.is_file():
                return candidate
    except OSError:
        pass
    return None


def _suggest_models(base_url: str) -> list[str]:
    """按 baseURL 域名给出该家常用的 API model id（都是官方真名）。"""
    hints = {
        "api.deepseek.com": ["deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-pro"],
        "open.bigmodel.cn": ["GLM-5.3", "glm-4.7", "glm-4.6-air"],
        "api.moonshot.cn": ["kimi-k2", "kimi-k2-turbo", "kimi-latest"],
        "dashscope.aliyuncs.com": ["qwen3-max", "qwen-flash", "qwen-plus", "qwen-turbo"],
        "api.stepfun.com": ["step-3.5-flash", "step-2-16k", "step-1v-8k"],
        "api.minimaxi.com": ["MiniMax-M2", "MiniMax-Text-01", "minimax-01"],
        "ark.cn-beijing.volces.com": ["doubao-seed-1-6-250615", "doubao-1-5-pro-32k-250115"],
        "api.xiaomimimo.com": ["mimo", "mimo-pro", "mimo-pro-ultra"],
        "api.tbox.cn": ["bailing-v1", "bailing-pro"],
        "api.qnaigc.com": ["gpt-5.2", "claude-sonnet-4-5", "gpt-5.2-mini"],
    }
    for host, models in hints.items():
        if host in base_url:
            return models
    return []


def _probe_user_layer() -> Path | None:
    """~/.forge/forge.patch.json（forge 默认 home）。"""
    DEFAULT_FORGE_HOME.mkdir(parents=True, exist_ok=True)
    return DEFAULT_FORGE_HOME / "forge.patch.json"


# ─── 主应用 ──────────────────────────────────────────────


class ForgeGuiApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("forge — 图形界面（管理 + 客户端）")
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*MIN_SIZE)
        self.root.configure(bg=C["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 状态
        self.run_py = _find_run_py()
        self.home = DEFAULT_FORGE_HOME
        self.user_layer_path = _probe_user_layer()
        self._load_error = ""
        try:
            self.user_rows: list[dict] = load_user_layer(self.home)
        except (OSError, ValueError) as exc:
            self.user_rows = []
            self._load_error = str(exc)
        self.gateway_proc: subprocess.Popen | None = None
        self.gateway_port = 8799
        self.gateway_url = f"http://127.0.0.1:{self.gateway_port}"
        self.client = ForgeGatewayClient(self.gateway_url)

        # 矫治后待保存的内容
        self._pending_rows: list[dict] = []
        self._organized_input = ""
        self._editor_clean_text = ""
        self._chat_history: list[ChatMessage] = []
        self._feature_entries: list[dict] = []
        self._features_expanded = False
        self._feature_dirty = False
        self._sending = False
        self._abort_requested = False
        self._closing = False
        self._ui_events = queue.Queue()
        self._editor_baseline = copy.deepcopy(self.user_rows)

        self._build_ui()
        self._refresh_provider_list()
        self._set_status(self._load_error or "就绪 · 开关选择后需保存；运行中的服务需重启以读取新配置",
                         "error" if self._load_error else "info")
        self._event_poll = self.root.after(40, self._drain_ui_events)

    def _post_ui(self, callback, *args):
        if not self._closing:
            self._ui_events.put((callback, args))

    def _drain_ui_events(self):
        for _ in range(200):
            try:
                callback, args = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())
        if not self._closing:
            self._event_poll = self.root.after(40, self._drain_ui_events)

    # ── UI 构造 ──────────────────────────────────────────
    @staticmethod
    def _style_scrollbar(widget):
        widget.vbar.configure(bg=C["surface2"], activebackground=C["border"],
                              troughcolor=C["input_bg"], relief=tk.FLAT,
                              bd=0, highlightthickness=0, width=10)

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab",
                        background=C["bg"],
                        foreground=C["subtext"],
                        padding=(16, 7),
                        font=FONT_UI_BOLD,
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C["accent_soft"]), ("active", C["surface_subtle"])],
                  foreground=[("selected", C["accent"]), ("active", C["text"])],
                  expand=[("selected", (0, 0, 0, 0))])
        style.configure("TFrame", background=C["bg"])
        style.configure("Vertical.TScrollbar", background=C["scroll"],
                        troughcolor=C["surface_subtle"], arrowcolor=C["ter"],
                        bordercolor=C["surface_subtle"], lightcolor=C["scroll"],
                        darkcolor=C["scroll"])
        style.map("Vertical.TScrollbar", background=[("active", C["ter"])])
        style.configure("TCombobox", fieldbackground=C["surface"], background=C["surface"],
                        foreground=C["text"], arrowcolor=C["muted"], padding=4,
                        bordercolor=C["border"], lightcolor=C["surface"],
                        darkcolor=C["surface"], relief=tk.FLAT)
        style.map("TCombobox", fieldbackground=[("readonly", C["surface"])],
                  foreground=[("readonly", C["text"])],
                  bordercolor=[("focus", C["accent"])])
        self.root.option_add("*TCombobox*Listbox.background", C["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent_soft"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", C["text"])

        # ── 顶部（AutoClaw chrome：浅灰底 + 细分隔线） ──
        chrome = tk.Frame(self.root, bg=C["bg"])
        chrome.pack(fill=tk.X)
        top = tk.Frame(chrome, bg=C["bg"], padx=20, pady=14)
        top.pack(fill=tk.X)
        brand = tk.Frame(top, bg=C["bg"])
        brand.pack(side=tk.LEFT)
        tk.Label(brand, text="FORGE", bg=C["bg"], fg=C["accent"],
                 font=("Segoe UI", 9, "bold") if IS_WINDOWS else ("Helvetica", 9, "bold")
                 ).pack(anchor=tk.W)
        tk.Label(brand, text="forge workspace", bg=C["bg"], fg=C["text"],
                 font=FONT_TITLE).pack(anchor=tk.W, pady=(2, 0))

        # gateway 控制（右上，白卡 + 细边）
        gw_frame = tk.Frame(top, bg=C["surface"], padx=12, pady=8,
                            highlightthickness=1, highlightbackground=C["border"])
        gw_frame.pack(side=tk.RIGHT, pady=(2, 0))
        self.gw_status_var = tk.StringVar(value="● 离线")
        self.gw_status_lbl = tk.Label(
            gw_frame, textvariable=self.gw_status_var, bg=C["surface"],
            fg=C["ter"], font=FONT_UI_BOLD,
        )
        self.gw_status_lbl.pack(side=tk.LEFT, padx=(0, 12))
        self.gw_btn = tk.Button(
            gw_frame, text="启动 gateway", bg=C["accent"], fg="#ffffff",
            activebackground=C["accent_hover"], activeforeground="#ffffff",
            font=FONT_UI_BOLD, relief=tk.FLAT, bd=0, padx=14, pady=5,
            command=self._toggle_gateway, cursor="hand2", highlightthickness=0,
        )
        self.gw_btn.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(gw_frame, text="端口", bg=C["surface"], fg=C["ter"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(10, 4))
        self.port_var = tk.StringVar(value=str(self.gateway_port))
        self.port_spin = tk.Spinbox(
            gw_frame, from_=1024, to_=65535, width=6,
            textvariable=self.port_var, font=FONT_MONO,
            bg=C["surface_subtle"], fg=C["text"], buttonbackground=C["surface"],
            relief=tk.FLAT, insertbackground=C["accent"], highlightthickness=0, bd=0,
        )
        self.port_spin.pack(side=tk.LEFT)

        tk.Frame(chrome, bg=C["border"], height=1).pack(fill=tk.X)

        # ── 标签页 ──
        nb = ttk.Notebook(self.root)
        nb.enable_traversal()
        self.tab_manage = tk.Frame(nb, bg=C["bg"])
        self.tab_features = tk.Frame(nb, bg=C["bg"])
        self.tab_client = tk.Frame(nb, bg=C["bg"])
        nb.add(self.tab_features, text="功能开关")
        nb.add(self.tab_manage, text="配置编辑")
        nb.add(self.tab_client, text="交互客户端")

        self._build_feature_panel(self.tab_features)
        self._build_manage_tab(self.tab_manage)
        self._build_client_tab(self.tab_client)

        # ── 底部状态栏 ──
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill=tk.X, side=tk.BOTTOM)
        bot = tk.Frame(self.root, bg=C["bg"], height=28)
        bot.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_var = tk.StringVar(value="")
        self.status_lbl = tk.Label(bot, textvariable=self.status_var, bg=C["bg"],
                 fg=C["text"], font=FONT_SMALL, anchor=tk.W, padx=20)
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.path_lbl = tk.Label(
            bot, text=self._status_label_text(),
            bg=C["bg"], fg=C["muted"], font=FONT_SMALL, padx=12,
        )
        self.path_lbl.pack(side=tk.RIGHT)
        nb.pack(fill=tk.BOTH, expand=True, padx=20, pady=(8, 10))

    # ── 标签 1：管理 ──────────────────────────────────────────
    def _build_manage_tab(self, parent):
        split = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=C["border"],
                               sashwidth=7, sashrelief=tk.FLAT, bd=0)
        split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        left = tk.Frame(split, bg=C["surface"], padx=14, pady=14)
        right_host = tk.Frame(split, bg=C["bg"])
        editor_canvas = tk.Canvas(right_host, bg=C["bg"], width=1, height=1, highlightthickness=0)
        editor_scroll = ttk.Scrollbar(right_host, orient=tk.VERTICAL, command=editor_canvas.yview)
        editor_canvas.configure(yscrollcommand=editor_scroll.set)
        right = tk.Frame(editor_canvas, bg=C["bg"], padx=14)
        editor_window = editor_canvas.create_window(0, 0, window=right, anchor="nw")
        def fit_editor(event):
            editor_canvas.itemconfigure(editor_window, width=event.width,
                                         height=max(event.height, right.winfo_reqheight()))
        editor_canvas.bind("<Configure>", fit_editor)
        right.bind("<Configure>", lambda _: editor_canvas.configure(scrollregion=editor_canvas.bbox("all")))
        editor_canvas.bind("<MouseWheel>", lambda event: editor_canvas.yview_scroll(
            -1 if event.delta > 0 else 1, "units"))
        self.editor_canvas = editor_canvas
        split.add(left, minsize=240, width=310)
        split.add(right_host, minsize=480)

        tk.Label(left, text="用户配置", bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(anchor=tk.W)
        self.provider_count_var = tk.StringVar(value="0 条用户配置")
        tk.Label(left, textvariable=self.provider_count_var, bg=C["surface"],
                 fg=C["muted"], font=FONT_SMALL).pack(anchor=tk.W, pady=(2, 12))
        self.provider_list = tk.Listbox(
            left, bg=C["input_bg"], fg=C["text"],
            selectbackground=C["surface2"], selectforeground=C["accent"],
            font=FONT_UI, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["border"], highlightcolor=C["accent"],
            activestyle="none", selectborderwidth=0, height=1, exportselection=False,
        )
        self.provider_list.pack(fill=tk.BOTH, expand=True)
        self.provider_list.bind("<<ListboxSelect>>", self._on_provider_select)
        tk.Label(left, text="选择条目可载入编辑区；下方可直接改模型名", bg=C["surface"],
                 fg=C["muted"], font=FONT_SMALL).pack(anchor=tk.W, pady=(12, 0))

        # ── 模型快捷编辑面板 ──
        self.model_edit_frame = tk.Frame(left, bg=C["input_bg"], highlightthickness=1,
                                         highlightbackground=C["border"])
        self.model_edit_frame.pack(fill=tk.X, pady=(10, 0), ipadx=10, ipady=8)
        tk.Label(self.model_edit_frame, text="模型快捷编辑", bg=C["input_bg"], fg=C["text"],
                 font=FONT_UI_BOLD).pack(anchor=tk.W)
        self.model_edit_target = tk.Label(self.model_edit_frame, text="（先在列表选中 provider）",
                                          bg=C["input_bg"], fg=C["muted"], font=FONT_SMALL,
                                          wraplength=240, justify=tk.LEFT)
        self.model_edit_target.pack(anchor=tk.W, pady=(2, 6))
        entry_row = tk.Frame(self.model_edit_frame, bg=C["input_bg"])
        entry_row.pack(fill=tk.X)
        self.model_edit_var = tk.StringVar()
        self.model_edit_entry = tk.Entry(entry_row, textvariable=self.model_edit_var,
                                         bg=C["bg"], fg=C["text"], insertbackground=C["accent"],
                                         font=FONT_MONO, relief=tk.FLAT,
                                         highlightthickness=1, highlightbackground=C["border"],
                                         highlightcolor=C["accent"])
        self.model_edit_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.model_apply_btn = tk.Button(entry_row, text="应用", bg=C["accent"], fg="#ffffff",
                                         activebackground=C["accent_hover"], activeforeground="#ffffff",
                                         font=FONT_UI_BOLD, relief=tk.FLAT, padx=10, pady=3,
                                         command=self._apply_model_edit, cursor="hand2",
                                         state=tk.DISABLED)
        self.model_apply_btn.pack(side=tk.LEFT, padx=(6, 0))
        # 常用模型 chips（按 baseURL 域名给建议）
        self.model_chips_frame = tk.Frame(self.model_edit_frame, bg=C["input_bg"])
        self.model_chips_frame.pack(fill=tk.X, pady=(6, 0))
        self.model_edit_entry.bind("<Return>", lambda _e: self._apply_model_edit())
        # 探活按钮
        probe_row = tk.Frame(self.model_edit_frame, bg=C["input_bg"])
        probe_row.pack(fill=tk.X, pady=(6, 0))
        self.model_probe_btn = tk.Button(probe_row, text="测试此 provider", bg=C["surface2"], fg=C["link"],
                                         activebackground=C["link_soft"], activeforeground=C["link"],
                                         font=FONT_UI, relief=tk.FLAT, padx=8, pady=2,
                                         command=self._probe_selected_provider, cursor="hand2",
                                         state=tk.DISABLED)
        self.model_probe_btn.pack(side=tk.LEFT)
        self.model_probe_var = tk.StringVar(value="")
        tk.Label(probe_row, textvariable=self.model_probe_var, bg=C["input_bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(left, text="打开用户层目录  ↗", bg=C["surface2"], fg=C["text"],
                  activebackground=C["border"], activeforeground=C["text"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=lambda: self._open_path(self.home), cursor="hand2"
                  ).pack(anchor=tk.W, pady=(10, 0))

        tk.Label(right, text="添加或编辑配置", bg=C["bg"], fg=C["text"],
                 font=FONT_SECTION).pack(anchor=tk.W)
        tk.Label(right, text="粘贴 Provider JSON 或地址与密钥，整理后确认预览再保存。",
                 bg=C["bg"], fg=C["muted"], font=FONT_SMALL
                 ).pack(anchor=tk.W, pady=(2, 10))
        self.input_text = scrolledtext.ScrolledText(
            right, height=5, bg=C["input_bg"], fg=C["muted"],
            insertbackground=C["accent"], font=FONT_MONO,
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["border"], highlightcolor=C["accent"],
            padx=12, pady=10, wrap=tk.WORD, undo=True,
        )
        self.input_text.pack(fill=tk.X)
        self._style_scrollbar(self.input_text)
        self._input_placeholder = "粘贴配置或输入内容，例如：\nbaseURL: https://api.example.com/v1\napiKey: sk-…\nmodel: example-model"
        self._placeholder_visible = True
        self.input_text.insert("1.0", self._input_placeholder)
        self.input_text.bind("<FocusIn>", self._clear_placeholder)
        self.input_text.bind("<FocusOut>", self._restore_placeholder)
        self.input_text.bind("<Control-Return>", self._organize_shortcut)
        self.input_text.bind("<<Modified>>", self._on_input_modified)
        self.input_text.edit_modified(False)

        btn_bar = tk.Frame(right, bg=C["bg"])
        btn_bar.pack(fill=tk.X, pady=(9, 14))
        self.organize_btn = tk.Button(
            btn_bar, text="整理并预览", bg=C["accent"], fg="#ffffff",
            activebackground=C["accent_hover"], activeforeground="#ffffff",
            font=FONT_UI_BOLD, relief=tk.FLAT, padx=16, pady=6,
            command=self._do_organize, cursor="hand2",
        )
        self.organize_btn.pack(side=tk.LEFT)
        tk.Button(btn_bar, text="粘贴", bg=C["surface2"], fg=C["text"],
                  activebackground=C["border"], activeforeground=C["text"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._paste_clipboard, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(btn_bar, text="清空", bg=C["surface2"], fg=C["subtext"],
                  activebackground=C["border"], activeforeground=C["text"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._clear_input, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        # 从 AutoClaw 用户层导入 provider（一次把 18 个 provider 写进 forge 用户层 + 密钥库）
        tk.Button(btn_bar, text="从 AutoClaw 导入", bg=C["surface2"], fg=C["accent"],
                  activebackground=C["accent_soft"], activeforeground=C["accent"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._import_from_autoclaw, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(btn_bar, text="Ctrl + Enter 整理", bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.RIGHT)

        preview_head = tk.Frame(right, bg=C["bg"])
        preview_head.pack(fill=tk.X, pady=(0, 7))
        tk.Label(preview_head, text="预览", bg=C["bg"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        tk.Label(preview_head, text="整理后的 patch JSON", bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT, padx=(10, 0))
        self.preview_text = scrolledtext.ScrolledText(
            right, height=6, bg=C["input_bg"], fg=C["accent2"],
            font=FONT_MONO, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["border"], padx=12, pady=10,
            wrap=tk.NONE, state=tk.DISABLED,
        )
        self._style_scrollbar(self.preview_text)
        warn_head = tk.Frame(right, bg=C["bg"])
        tk.Label(warn_head, text="提示与环境变量", bg=C["bg"],
                 fg=C["subtext"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        self.warning_count_var = tk.StringVar(value="无提示")
        tk.Label(warn_head, textvariable=self.warning_count_var,
                 bg=C["bg"], fg=C["muted"], font=FONT_SMALL
                 ).pack(side=tk.RIGHT)
        self.warn_text = scrolledtext.ScrolledText(
            right, height=3, bg=C["surface"], fg=C["warn"],
            font=FONT_SMALL, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["border"], padx=10, pady=8,
            wrap=tk.WORD, state=tk.DISABLED,
        )
        self._style_scrollbar(self.warn_text)
        footer = tk.Frame(right_host, bg=C["bg"], padx=14)
        tk.Label(footer, text="预览后保存", bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT)
        self.save_btn = tk.Button(
            footer, text="保存到用户层", bg=C["ok"], fg="#ffffff",
            activebackground=C["accent_hover"], activeforeground="#ffffff",
            font=FONT_UI_BOLD, relief=tk.FLAT, padx=16, pady=7,
            command=self._do_save, cursor="hand2", state=tk.DISABLED,
        )
        self.save_btn.pack(side=tk.RIGHT)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(9, 0))
        self.warn_text.pack(side=tk.BOTTOM, fill=tk.X)
        warn_head.pack(side=tk.BOTTOM, fill=tk.X, pady=(9, 5))
        self.preview_text.pack(fill=tk.BOTH, expand=True)
        editor_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        editor_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._set_warnings([])

    # ── 功能开关 ───────────────────────────────────────────────
    def _build_feature_panel(self, parent):
        self.feature_card = tk.Frame(
            parent, bg=C["surface"], highlightthickness=1,
            highlightbackground=C["border"], padx=12, pady=8,
        )
        self.feature_card.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        head = tk.Frame(self.feature_card, bg=C["surface"])
        head.pack(fill=tk.X)
        self.feature_title_var = tk.StringVar(value="▸ 功能开关")
        self.feature_toggle_btn = tk.Button(
            head, textvariable=self.feature_title_var, bg=C["surface"],
            fg=C["text"], activebackground=C["surface2"],
            activeforeground=C["accent"], font=FONT_UI_BOLD,
            relief=tk.FLAT, bd=0, padx=0, pady=2, anchor=tk.W,
            command=self._toggle_feature_panel, cursor="hand2",
        )
        self.feature_toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.feature_summary_var = tk.StringVar(value="读取中")
        tk.Label(head, textvariable=self.feature_summary_var, bg=C["surface"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.RIGHT)

        self.feature_body = tk.Frame(self.feature_card, bg=C["surface"])
        self.feature_feedback_var = tk.StringVar(value="勾选仅修改草稿。保存后，重启正在运行的 Forge / 通道服务以应用配置。")
        tk.Label(self.feature_body, textvariable=self.feature_feedback_var,
                 bg=C["surface"], fg=C["subtext"], font=FONT_SMALL,
                 anchor=tk.W, justify=tk.LEFT, wraplength=800
                 ).pack(fill=tk.X, pady=(10, 8))
        actions = tk.Frame(self.feature_body, bg=C["surface"])
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        self.feature_reset_btn = tk.Button(
            actions, text="还原未保存修改", bg=C["surface2"], fg=C["subtext"],
            activebackground=C["border"], activeforeground=C["text"],
            font=FONT_SMALL, relief=tk.FLAT, padx=10, pady=5,
            command=self._discard_feature_changes, cursor="hand2", state=tk.DISABLED,
        )
        self.feature_reset_btn.pack(side=tk.LEFT)
        tk.Button(actions, text="刷新已保存配置", command=self._reload_configuration,
                  bg=C["surface2"], fg=C["text"], activebackground=C["border"],
                  font=FONT_SMALL, relief=tk.FLAT, padx=10, pady=5,
                  cursor="hand2").pack(side=tk.LEFT, padx=8)
        tk.Button(actions, text="选择 Forge 目录", command=self._choose_forge_repo,
                  bg=C["surface2"], fg=C["text"], activebackground=C["border"],
                  font=FONT_SMALL, relief=tk.FLAT, padx=10, pady=5,
                  cursor="hand2").pack(side=tk.LEFT)
        self.feature_save_btn = tk.Button(
            actions, text="保存功能开关", bg=C["accent"], fg="#ffffff",
            activebackground=C["accent_hover"], activeforeground="#ffffff",
            font=FONT_UI_BOLD, relief=tk.FLAT, padx=14, pady=6,
            command=self._save_feature_toggles, cursor="hand2", state=tk.DISABLED,
        )
        self.feature_save_btn.pack(side=tk.RIGHT)
        viewport = tk.Frame(self.feature_body, bg=C["surface"])
        viewport.pack(fill=tk.BOTH, expand=True)
        self.feature_canvas = tk.Canvas(viewport, bg=C["surface"], highlightthickness=0,
                                        width=1, height=1)
        scrollbar = ttk.Scrollbar(viewport, orient=tk.VERTICAL, command=self.feature_canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.feature_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.feature_canvas.configure(yscrollcommand=scrollbar.set)
        self.feature_list = tk.Frame(self.feature_canvas, bg=C["surface"])
        self._feature_window = self.feature_canvas.create_window(0, 0, window=self.feature_list, anchor="nw")
        self.feature_list.bind("<Configure>", lambda _: self.feature_canvas.configure(
            scrollregion=self.feature_canvas.bbox("all")))
        self.feature_canvas.bind("<Configure>", lambda event: self.feature_canvas.itemconfigure(
            self._feature_window, width=event.width))
        self.feature_canvas.bind("<MouseWheel>", self._scroll_features)
        self._toggle_feature_panel()

    def _scroll_features(self, event):
        self.feature_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _toggle_feature_panel(self):
        self._features_expanded = not self._features_expanded
        if self._features_expanded:
            self.feature_body.pack(fill=tk.BOTH, expand=True)
        else:
            self.feature_body.pack_forget()
        self._update_feature_header()

    def _update_feature_header(self):
        count = len(self._feature_entries)
        arrow = "▾" if self._features_expanded else "▸"
        self.feature_title_var.set(f"{arrow} 功能开关")
        if not count:
            self.feature_summary_var.set("暂无可切换项目")
        elif self._feature_dirty:
            changes = sum(bool(e["var"].get()) != e["value"] for e in self._feature_entries)
            self.feature_summary_var.set(f"{changes} 项未保存 / 共 {count} 项")
        else:
            self.feature_summary_var.set(f"{count} 项可在这里选择")

    @staticmethod
    def _iter_boolean_paths(value: object, prefix: tuple[str, ...] = ()):
        if isinstance(value, bool):
            yield prefix, value
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    yield from ForgeGuiApp._iter_boolean_paths(item, prefix + (key,))

    def _feature_label(self, row_id: str, path: tuple[str, ...]) -> tuple[str, str]:
        key = " · ".join(path)
        if row_id == "model" and path == ("moa",):
            return "多模型协作（MOA）", "使用配置中的协作模型；是否执行由 Forge 运行方式决定"
        if row_id.startswith("channel:") and path == ("enabled",):
            return f"启用 {row_id.split(':', 1)[1]} 通道", "允许 Forge 接收和发送该通道的消息"
        return f"{row_id} · {key}", "高级布尔配置：勾选为 true，取消为 false"

    def _add_feature_toggle(self, entry: dict):
        index = len(self._feature_entries)
        card = tk.Frame(self.feature_list, bg=C["input_bg"], padx=8, pady=7,
                        highlightthickness=1, highlightbackground=C["border"])
        card.grid(row=index, column=0, sticky="ew", pady=4)
        self.feature_list.grid_columnconfigure(0, weight=1)
        var = tk.BooleanVar(value=entry["value"])
        entry["var"] = var
        toggle = tk.Checkbutton(
            card, text=entry["label"], variable=var, command=self._mark_features_dirty,
            bg=C["input_bg"], fg=C["text"], activebackground=C["input_bg"],
            activeforeground=C["accent"], selectcolor=C["surface2"],
            font=FONT_UI_BOLD, anchor=tk.W, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["input_bg"], highlightcolor=C["accent"],
            cursor="hand2",
        )
        state_var = tk.StringVar(value="")
        entry["state_var"] = state_var
        tk.Label(card, textvariable=state_var, bg=C["input_bg"], fg=C["accent"],
                 font=FONT_SMALL).pack(side=tk.RIGHT, padx=8)
        toggle.pack(anchor=tk.W)
        description = tk.Label(card, text=entry["description"], bg=C["input_bg"], fg=C["muted"],
                               font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT)
        description.pack(anchor=tk.W, padx=(23, 0), pady=(2, 0))
        card.bind("<Configure>", lambda event: (
            toggle.configure(wraplength=max(160, event.width - 210)),
            description.configure(wraplength=max(160, event.width - 210))))
        for widget in (card, toggle, description):
            widget.bind("<MouseWheel>", self._scroll_features)
        self._feature_entries.append(entry)

    def _rebuild_feature_toggles(self, force: bool = False):
        if self._feature_dirty and not force:
            return
        for child in self.feature_list.winfo_children():
            child.destroy()
        self._feature_entries = []
        self._feature_dirty = False
        for row in self.user_rows:
            row_id = str(row.get("id", "未命名"))
            conf = row.get("config") or {}
            if "baseURL" in conf or "disabled" in row:
                self._add_feature_toggle({
                    "kind": "provider", "row_id": row_id, "path": (),
                    "value": not bool(row.get("disabled")),
                    "label": f"{'启用 Provider' if 'baseURL' in conf else '启用配置条目'} · {row_id}",
                    "description": "关闭后 Forge 不加载此条目；已运行的服务需重启",
                })
            for path, value in self._iter_boolean_paths(conf):
                label, description = self._feature_label(row_id, path)
                self._add_feature_toggle({
                    "kind": "config", "row_id": row_id, "path": path,
                    "value": value, "label": label, "description": description,
                })
        if not self._feature_entries:
            tk.Label(self.feature_list, text="当前用户层没有可切换的布尔功能。添加 Provider 或通道配置后会自动出现在这里。",
                     bg=C["surface"], fg=C["muted"], font=FONT_SMALL, anchor=tk.W,
                     justify=tk.LEFT, wraplength=620).pack(fill=tk.X, pady=4)
        self._mark_features_dirty()

    def _mark_features_dirty(self):
        changes = 0
        for entry in self._feature_entries:
            changed = bool(entry["var"].get()) != entry["value"]
            changes += changed
            positive_switch = entry["kind"] == "provider" or entry["path"][-1] in ("enabled", "moa")
            state = (("开启" if entry["var"].get() else "关闭") if positive_switch
                     else ("是 / true" if entry["var"].get() else "否 / false"))
            entry["state_var"].set(f"{state} · {'未保存' if changed else '已保存'}")
        self._feature_dirty = bool(changes)
        self.feature_save_btn.configure(state=tk.NORMAL if changes else tk.DISABLED,
                                       bg=C["accent"] if changes else C["surface2"],
                                       disabledforeground=C["muted"],
                                       text=f"保存 {changes} 项修改" if changes else "保存功能开关")
        self.feature_reset_btn.configure(state=tk.NORMAL if changes else tk.DISABLED)
        self._update_feature_header()

    def _discard_feature_changes(self):
        self._rebuild_feature_toggles(force=True)
        self._set_status("已还原功能开关的未保存修改", "info")

    def _reload_configuration(self):
        if self._feature_dirty:
            self._set_status("开关还有未保存修改，请先保存或还原，再刷新", "warn")
            return
        try:
            rows = load_user_layer(self.home)
        except (OSError, ValueError) as exc:
            self._set_status(f"读取失败：{exc}", "error")
            return
        self.user_rows = rows
        self._refresh_provider_list()
        self.feature_feedback_var.set("已刷新磁盘上的配置。保存后的设置由下次启动的服务读取。")
        self._set_status("已刷新用户层配置", "ok")

    def _save_feature_toggles(self):
        if not self._feature_dirty:
            return
        try:
            latest = load_user_layer(self.home)
            rows = copy.deepcopy(latest)
            by_id = {row["id"]: row for row in rows}
            for entry in self._feature_entries:
                enabled = bool(entry["var"].get())
                if enabled == entry["value"]:
                    continue
                row = by_id.get(entry["row_id"])
                if row is None:
                    raise ConfigNormalizeError(f"条目 {entry['row_id']} 已被移除，请还原草稿并刷新")
                if entry["kind"] == "provider":
                    row["disabled"] = not enabled
                else:
                    target = row.get("config", {})
                    for key in entry["path"][:-1]:
                        if not isinstance(target.get(key), dict):
                            raise ConfigNormalizeError("配置结构已变化，请还原草稿并刷新")
                        target = target[key]
                    key = entry["path"][-1]
                    if type(target.get(key)) is not bool:
                        raise ConfigNormalizeError("配置类型已变化，请还原草稿并刷新")
                    target[key] = enabled
            save_user_layer(self.home, rows, expected_rows=latest)
        except (OSError, ValueError) as exc:
            self.feature_feedback_var.set(f"未保存：{exc}")
            self._set_status(f"保存功能开关失败：{exc}", "error")
            return
        self.user_rows = rows
        self._feature_dirty = False
        self._refresh_provider_list()
        self.feature_feedback_var.set("已保存。请重启正在运行的 Forge / 通道服务，让新设置生效。")
        self._set_status("功能开关已保存；运行中的服务需重启以应用", "ok")

    # ── 标签 2：客户端 ──────────────────────────────────────────
    def _build_client_tab(self, parent):
        heading = tk.Frame(parent, bg=C["bg"])
        heading.pack(fill=tk.X, pady=(14, 10))
        tk.Label(heading, text="交互客户端", bg=C["bg"], fg=C["text"],
                 font=FONT_SECTION).pack(anchor=tk.W)
        tk.Label(heading, text="连接本机 gateway，与已配置的模型对话。",
                 bg=C["bg"], fg=C["muted"], font=FONT_SMALL
                 ).pack(anchor=tk.W, pady=(2, 0))

        # ── 次要控制（模型刷新 / 温度 / 清空）──
        ctrl = tk.Frame(parent, bg=C["bg"])
        ctrl.pack(fill=tk.X, pady=(0, 8))
        tk.Label(ctrl, text="模型", bg=C["bg"], fg=C["ter"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 6))
        self.model_var = tk.StringVar(value="default")
        self.model_combo = ttk.Combobox(
            ctrl, textvariable=self.model_var, values=["default"],
            state="readonly", width=24, font=FONT_UI,
        )
        self.model_combo.pack(side=tk.LEFT)
        tk.Button(ctrl, text="刷新", command=self._reload_configuration,
                  bg=C["bg"], fg=C["muted"], activebackground=C["surface2"],
                  activeforeground=C["text"], font=FONT_SMALL, relief=tk.FLAT,
                  bd=0, padx=8, pady=3, cursor="hand2").pack(side=tk.LEFT, padx=(6, 0))

        self.temp_var = tk.StringVar(value="0.7")
        tk.Label(ctrl, text="温度", bg=C["bg"], fg=C["ter"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(18, 6))
        tk.Entry(ctrl, textvariable=self.temp_var, width=5,
                 bg=C["surface"], fg=C["text"], font=FONT_MONO,
                 relief=tk.FLAT, insertbackground=C["accent"], bd=0,
                 highlightthickness=1, highlightbackground=C["border"],
                 highlightcolor=C["accent"]).pack(side=tk.LEFT, ipady=3, ipadx=4)

        self.clear_chat_btn = tk.Button(
            ctrl, text="＋ 新对话", command=self._clear_chat,
            bg=C["surface"], fg=C["text"], activebackground=C["surface2"],
            activeforeground=C["accent"], font=FONT_UI, relief=tk.FLAT,
            bd=0, padx=14, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=C["border"])
        self.clear_chat_btn.pack(side=tk.RIGHT)

        # ── 对话区（白卡 + 细分隔线）──
        chat_wrap = tk.Frame(parent, bg=C["border"], padx=1, pady=1)
        self.chat_text = scrolledtext.ScrolledText(
            chat_wrap, bg=C["surface"], fg=C["text"],
            font=FONT_UI, relief=tk.FLAT, highlightthickness=0,
            padx=20, pady=18, wrap=tk.WORD,
            state=tk.DISABLED, spacing1=3, spacing3=5,
        )
        self.chat_text.pack(fill=tk.BOTH, expand=True)
        self._style_scrollbar(self.chat_text)
        self.chat_text.tag_configure("user", foreground=C["accent"], font=FONT_UI_BOLD)
        self.chat_text.tag_configure("assistant", foreground=C["subtext"], font=FONT_UI_BOLD)
        self.chat_text.tag_configure("muted", foreground=C["ter"])
        self.chat_text.configure(tabs=("1c",))
        self.chat_text.tag_configure("error", foreground=C["error"])
        self.chat_text.tag_configure("empty_title", foreground=C["text"],
                                      font=FONT_SECTION, spacing1=4, spacing3=10,
                                      justify="center")
        self.chat_text.tag_configure("empty_body", foreground=C["muted"],
                                      font=FONT_SMALL, justify="center",
                                      spacing1=2, spacing3=2)
        # 气泡（对照 AutoClaw：用户右对齐浅橙、AI 左对齐浅灰）
        self.chat_text.tag_configure("bubble_user", background=C["msg_user_bg"],
                                      lmargin1=220, lmargin2=220, rmargin=14,
                                      spacing1=8, spacing3=8, justify="right")
        self.chat_text.tag_configure("bubble_agent", background=C["msg_agent_bg"],
                                      lmargin1=14, lmargin2=14, rmargin=220,
                                      spacing1=4, spacing3=8)
        self.chat_text.tag_configure("error_bubble", background=C["error_soft"],
                                      foreground=C["error"],
                                      lmargin1=14, lmargin2=14, rmargin=220,
                                      spacing1=8, spacing3=8)
        self.chat_text.tag_configure("msg_meta", foreground=C["ter"],
                                      font=FONT_SMALL, spacing1=10, spacing3=2,
                                      lmargin1=14)
        self.chat_text.tag_configure("msg_meta_right", foreground=C["ter"],
                                      font=FONT_SMALL, spacing1=10, spacing3=2,
                                      lmargin1=220, justify="right")
        self.chat_text.tag_configure("agent_head", foreground=C["subtext"],
                                      font=FONT_UI_BOLD, spacing1=10, spacing3=2,
                                      lmargin1=14)
        self._hide_chat_scrollbar()
        self._show_chat_empty_state()

        # ── 输入卡（AutoClaw 风格圆角卡片 + 底部工具条）──
        self.send_var = tk.StringVar()
        self.input_card = tk.Canvas(parent, bg=C["bg"], highlightthickness=0,
                                    height=114, bd=0)
        self.input_card.pack(side=tk.BOTTOM, fill=tk.X)
        self._card_shape = round_rect(self.input_card, 1, 1, 10, 10, R_PILL,
                                      fill=C["input_bg"], outline=C["border"], width=1)

        self.send_entry = tk.Entry(
            self.input_card, textvariable=self.send_var, font=FONT_UI,
            bg=C["input_bg"], fg=C["text"], insertbackground=C["accent"],
            relief=tk.FLAT, bd=0, highlightthickness=0,
        )
        self.send_entry.bind("<Return>", lambda e: self._do_send())
        self._entry_win = self.input_card.create_window(0, 0, window=self.send_entry,
                                                        anchor="nw")
        # 占位提示必须是「内嵌控件」：canvas 图元会被 create_window 的
        # Entry 子窗口盖住，只有真实 widget 才画在它上面。
        self.entry_hint = tk.Label(self.input_card, text="输入消息，Enter 发送",
                                   bg=C["input_bg"], fg=C["placeholder"],
                                   font=FONT_UI, cursor="xterm",
                                   anchor="w", justify="left")
        self.entry_hint.bind("<Button-1>", lambda e: self.send_entry.focus_set())
        self._entry_hint = self.input_card.create_window(0, 0, window=self.entry_hint,
                                                         anchor="nw")
        # 同级 widget 的堆叠序由创建顺序决定，但 canvas 内嵌窗口的显示顺序
        # 会被 canvas 重排——显式 lift 一次确保占位文本压在 Entry 之上。
        try:
            self.entry_hint.lift(self.send_entry)
        except Exception:
            pass

        # ＋（粘贴剪贴板）
        self.plus_btn = tk.Button(
            self.input_card, text="＋", command=self._paste_into_input,
            bg=C["input_bg"], fg=C["muted"], activebackground=C["surface2"],
            activeforeground=C["text"], font=("Segoe UI", 13) if IS_WINDOWS else ("Helvetica", 13),
            relief=tk.FLAT, bd=0, padx=6, pady=0, cursor="hand2", highlightthickness=0)
        self._plus_win = self.input_card.create_window(0, 0, window=self.plus_btn, anchor="nw")

        # 模型名（工具条内联展示，与 AutoClaw 工具条一致）
        self.bar_tools = tk.Frame(self.input_card, bg=C["input_bg"])
        self._bar_tools_win = self.input_card.create_window(0, 0, window=self.bar_tools,
                                                            anchor="nw")
        self.bar_model_lbl = tk.Label(self.bar_tools, text=" default ",
                                      bg=C["surface_subtle"], fg=C["subtext"],
                                      font=FONT_SMALL, padx=8, pady=3)
        self.bar_model_lbl.pack(side=tk.LEFT)

        # 沉思模式（对齐 AutoClaw 工具条「目标模式」的位置；Forge 的对应概念是三档沉思）
        self._thinking_mode = self._read_thinking_mode()
        self.think_pill = tk.Button(
            self.bar_tools, text=self._thinking_label(), command=self._open_thinking_menu,
            bg=C["surface_subtle"], fg=C["subtext"], activebackground=C["accent_soft"],
            activeforeground=C["accent"], font=FONT_SMALL, relief=tk.FLAT, bd=0,
            padx=8, pady=3, cursor="hand2", highlightthickness=0)
        if self._thinking_mode != "off":
            self.think_pill.configure(fg=C["accent"], bg=C["accent_soft"])
        self.think_pill.pack(side=tk.LEFT, padx=(10, 0))

        # 圆形按钮：停止（深） / 发送（品牌橙）
        self._stop_circle = self.input_card.create_oval(0, 0, 0, 0,
                                                        fill=C["text"], outline="")
        self._stop_glyph = self.input_card.create_text(0, 0, text="■", fill="#ffffff",
                                                       font=("Segoe UI", 8))
        self._send_circle = self.input_card.create_oval(0, 0, 0, 0,
                                                        fill=C["surface2"], outline="")
        self._send_glyph = self.input_card.create_text(0, 0, text="↑", fill=C["ter"],
                                                       font=("Segoe UI", 12, "bold"))
        for item in (self._stop_circle, self._stop_glyph):
            self.input_card.tag_bind(item, "<Button-1>", lambda e: self._stop_send())
            self.input_card.tag_bind(item, "<Enter>", lambda e: self.input_card.configure(cursor="hand2"))
        for item in (self._send_circle, self._send_glyph):
            self.input_card.tag_bind(item, "<Button-1>", lambda e: self._do_send())
            self.input_card.tag_bind(item, "<Enter>", lambda e: self.input_card.configure(cursor="hand2"))
        self.input_card.itemconfigure(self._stop_circle, state="hidden")
        self.input_card.itemconfigure(self._stop_glyph, state="hidden")
        self._stop_visible = False

        self.input_card.bind("<Configure>", self._layout_input_card)
        self.send_var.trace_add("write", lambda *_: self._refresh_send_circle())

        # 卡下方说明（对齐 AutoClaw 的 "Agent 在本地运行，内容由AI生成"）
        foot = tk.Frame(parent, bg=C["bg"])
        foot.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 8))
        self.request_status_var = tk.StringVar(value="空闲")
        tk.Label(foot, textvariable=self.request_status_var, bg=C["bg"],
                 fg=C["ter"], font=FONT_CAPTION).pack(side=tk.LEFT)
        tk.Label(foot, text="forge 在本地运行，内容由 AI 生成", bg=C["bg"],
                 fg=C["ter"], font=FONT_CAPTION).pack(side=tk.RIGHT)

        # 对话区最后 pack（expand 会吃掉剩余空间，必须先给底部元素留位）
        chat_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

    # ── 输入卡布局（Canvas 内多控件手排）──
    def _layout_input_card(self, event=None):
        cv = self.input_card
        W = cv.winfo_width()
        if W <= 1:
            return
        H = int(cv.cget("height"))
        cv.coords(self._card_shape, *(self._rounded_points(1.5, 1.5, W - 1.5, H - 1.5, R_PILL)))
        pad = 18
        cv.coords(self._entry_win, pad + 4, 16)
        self._hint_xy = (pad + 6, 21)
        cv.coords(self._entry_hint, *self._hint_xy)
        try:
            self.entry_hint.lift(self.send_entry)
        except Exception:
            pass
        cv.itemconfigure(self._entry_hint, width=W - pad * 2 - 12, height=22)
        self.send_entry.configure(width=max(20, int((W - pad * 2 - 8) / 8)))
        cv.itemconfigure(self._entry_win, width=W - pad * 2 - 8, height=24)

        bar_y = H - 42
        cv.coords(self._plus_win, pad, bar_y - 2)
        cv.coords(self._bar_tools_win, pad + 38, bar_y - 2)
        self.bar_model_lbl.configure(text=f" {self.model_var.get()} ")

        cx_send, cy = W - 36, bar_y + 8
        r_send = 18
        cv.coords(self._send_circle, cx_send - r_send, cy - r_send,
                  cx_send + r_send, cy + r_send)
        cv.coords(self._send_glyph, cx_send, cy)
        cx_stop = W - 82
        r_stop = 15
        cv.coords(self._stop_circle, cx_stop - r_stop, cy - r_stop,
                  cx_stop + r_stop, cy + r_stop)
        cv.coords(self._stop_glyph, cx_stop, cy)
        self._refresh_send_circle()

    def _hint_pos(self):
        return getattr(self, "_hint_xy", (24, 21))

    @staticmethod
    def _rounded_points(x1, y1, x2, y2, r):
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
                x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

    def _refresh_send_circle(self):
        has_text = bool(self.send_var.get().strip())
        if hasattr(self, "_entry_hint"):
            self.input_card.itemconfigure(
                self._entry_hint, state="hidden" if has_text else "normal")
        if hasattr(self, "entry_hint"):
            self.input_card.coords(self._entry_hint, *self._hint_pos())
        if self._sending:
            self.input_card.itemconfigure(self._send_circle, fill=C["surface2"])
            self.input_card.itemconfigure(self._send_glyph, fill=C["muted"])
            self.input_card.itemconfigure(self._stop_circle, fill=C["text"], state="normal")
            self.input_card.itemconfigure(self._stop_glyph, state="normal")
            return
        self.input_card.itemconfigure(self._stop_circle, state="hidden")
        self.input_card.itemconfigure(self._stop_glyph, state="hidden")
        if has_text:
            self.input_card.itemconfigure(self._send_circle, fill=C["accent"])
            self.input_card.itemconfigure(self._send_glyph, fill="#ffffff")
        else:
            self.input_card.itemconfigure(self._send_circle, fill=C["surface2"])
            self.input_card.itemconfigure(self._send_glyph, fill=C["muted"])

    # ── 沉思模式（forge 的 thinking.mode：off / smart / on）──
    def _read_thinking_mode(self) -> str:
        for row in self.user_rows:
            if str(row.get("id")) == "thinking":
                mode = str((row.get("config") or {}).get("mode", "off")).lower()
                return mode if mode in ("off", "smart", "on") else "off"
        return "off"

    def _thinking_label(self) -> str:
        return f"◎ 沉思 · {THINKING_LABELS.get(self._thinking_mode, '关闭')}"

    def _open_thinking_menu(self):
        menu = tk.Menu(self.root, tearoff=0, bg=C["surface"], fg=C["text"],
                       activebackground=C["accent_soft"], activeforeground=C["accent"],
                       font=FONT_UI, bd=1, relief=tk.FLAT)
        var = tk.StringVar(value=self._thinking_mode)
        for mode, label, hint in THINKING_CHOICES:
            menu.add_radiobutton(label=f"{label}　{hint}", variable=var, value=mode,
                                 command=lambda m=mode: self._set_thinking_mode(m))
        try:
            menu.tk_popup(self.think_pill.winfo_rootx(),
                          self.think_pill.winfo_rooty() + self.think_pill.winfo_height())
        finally:
            menu.grab_release()

    def _set_thinking_mode(self, mode: str):
        import copy as _copy
        try:
            latest = load_user_layer(self.home)
            rows = _copy.deepcopy(latest)
            row = next((r for r in rows if str(r.get("id")) == "thinking"), None)
            if row is None:
                rows.append({"id": "thinking", "name": "thinking:mode",
                             "config": {"mode": mode}})
            else:
                row.setdefault("config", {})["mode"] = mode
            save_user_layer(self.home, rows, expected_rows=latest)
        except (OSError, ValueError) as exc:
            self._set_status(f"沉思模式保存失败：{exc}", "error")
            return
        self.user_rows = rows
        self._thinking_mode = mode
        self.think_pill.configure(text=self._thinking_label(),
                                  fg=C["accent"] if mode != "off" else C["subtext"],
                                  bg=C["accent_soft"] if mode != "off" else C["surface_subtle"])
        self._rebuild_feature_toggles(force=True)
        self._set_status(
            f"沉思模式已设为「{THINKING_LABELS[mode]}」；forge run 任务即时生效，"
            f"运行中的服务需重启以应用", "ok")

    def _paste_into_input(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self._set_status("剪贴板无文本", "warn")
            return
        current = self.send_var.get()
        sep = chr(10) if current else ""
        self.send_var.set((current + sep + text).strip())
        self.send_entry.focus_set()
        self._set_status(f"已粘贴 {len(text)} 字符到输入框", "info")


    def _status_label_text(self) -> str:
        parts = []
        if self.run_py:
            parts.append("forge 目录已就绪")
        else:
            parts.append("未找到 run.py")
        try:
            secrets = _load_secrets()
            parts.append("密钥库 " + str(len(secrets)) + " 项" if secrets else "密钥库为空")
        except Exception:
            parts.append("密钥库 ?")
        return "  ·  ".join(parts)

    def _update_status_label(self):
        try:
            self.path_lbl.configure(text=self._status_label_text())
        except Exception:
            pass

    def _import_from_autoclaw(self):
        """从 ~/.openclaw-autoclaw/openclaw.json 抓所有 provider，一次写入
        forge 用户层与密钥库。可用模型数量立即翻倍。
        """
        src = Path.home() / ".openclaw-autoclaw" / "openclaw.json"
        if not src.is_file():
            self._set_status(f"找不到 AutoClaw 配置：{src}", "error")
            return
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception as exc:
            self._set_status(f"AutoClaw 配置解析失败：{exc}", "error")
            return
        providers = (data.get("models") or {}).get("providers") or {}
        if not providers:
            self._set_status("AutoClaw 配置里没有 provider", "warn")
            return
        rows = list(self.user_rows)
        by_id = {r.get("id"): r for r in rows if r.get("id")}
        env_pairs = []
        skipped_placeholder = 0
        for pid, pconf in providers.items():
            bk = (pconf.get("baseUrl") or pconf.get("baseURL") or "").rstrip("/")
            ak = pconf.get("apiKey") or ""
            if not bk:
                continue
            models = pconf.get("models") or []
            if not models:
                continue
            mid = models[0].get("name") or models[0].get("id") or "default"
            if ak == "autoclaw-internal-proxy" or not ak or ak.startswith("Bearer "):
                skipped_placeholder += 1
                continue
            # safe_id 用「短前缀-6位hash」避免 UUID 类 id 截断后撞名
            raw_id = str(pid).lower()
            import hashlib as _hl
            short = re.sub(r"[^a-z0-9_-]+", "_", raw_id).strip("_-")[:24]
            tag = _hl.md5(raw_id.encode()).hexdigest()[:6]
            safe_id = (short + "_" + tag)[:40] or "provider"
            env_name = "FORGE_" + safe_id.upper().replace("-", "_") + "_KEY"
            row = by_id.get(safe_id)
            if row is None:
                row = {"id": safe_id, "name": f"provider:{safe_id}"}
                rows.append(row)
                by_id[safe_id] = row
            conf = dict(row.get("config") or {})
            conf["wire"] = "openai"
            conf["baseURL"] = bk
            conf["apiKey"] = {"$expr": f"get('env.{env_name}', '')"}
            conf["model"] = mid
            conf.setdefault("smallModel", mid)
            row["config"] = conf
            env_pairs.append((safe_id, ak, env_name))
        try:
            save_user_layer(self.home, rows)
        except (OSError, ValueError) as exc:
            self._set_status(f"写入用户层失败：{exc}", "error")
            return
        try:
            from secret_store import save as _save_secrets
            cur = _load_secrets()
            for safe_id, ak, _env in env_pairs:
                cur[safe_id] = ak
            _save_secrets(cur)
        except Exception as exc:
            self._set_status(f"密钥库写入失败：{exc}", "error")
            return
        self.user_rows = rows
        self._refresh_provider_list()
        self._update_status_label()
        self._set_status(
            f"已从 AutoClaw 导入 {len(env_pairs)} 个 provider（密钥写进 ~/.forge/secrets.json 不进日志；跳过 {skipped_placeholder} 个占位）",
            "ok",
        )

    # ── 占位提示 ──
    def _editor_is_dirty(self):
        text = "" if self._placeholder_visible else self.input_text.get("1.0", "end-1c").strip()
        return text != self._editor_clean_text

    def _clear_placeholder(self, _=None):
        if self._placeholder_visible:
            self.input_text.delete("1.0", tk.END)
            self.input_text.configure(fg=C["text"])
            self._placeholder_visible = False

    def _restore_placeholder(self, _=None):
        if not self.input_text.get("1.0", "end-1c").strip():
            self.input_text.insert("1.0", self._input_placeholder)
            self.input_text.configure(fg=C["muted"])
            self._placeholder_visible = True

    def _organize_shortcut(self, _=None):
        self._do_organize()
        return "break"

    def _on_input_modified(self, _=None):
        if not self.input_text.edit_modified():
            return
        self.input_text.edit_modified(False)
        if self._organized_input and self.input_text.get("1.0", "end-1c").strip() != self._organized_input:
            self._pending_rows = []
            self._organized_input = ""
            self._set_preview("")
            self._set_warnings([])
            self.save_btn.configure(state=tk.DISABLED)

    # ── 状态 ──
    def _set_status(self, msg: str, level: str = "info"):
        color = {
            "info": C["subtext"],
            "ok": C["ok"],
            "warn": C["warn"],
            "error": C["error"],
        }.get(level, C["subtext"])
        self.status_var.set(msg)
        self.status_lbl.configure(fg=color)

    # ── Provider 列表 ──
    def _refresh_provider_list(self):
        rows = list(self.user_rows)
        # 总是显示 model / policy 行用于参考
        display = []
        for r in rows:
            rid = r.get("id", "?")
            conf = r.get("config", {})
            if "baseURL" in conf:
                model = conf.get("model", "")
                state = "关闭" if r.get("disabled") else "开启"
                display.append(f"[{state}] {rid}  ·  {model or '未指定模型'}")
            else:
                # 非 provider 行
                keys = ", ".join(list(conf.keys())[:3])
                display.append(f"{rid}  ·  {keys or '元数据'}")
        self.provider_list.delete(0, tk.END)
        for d in display:
            self.provider_list.insert(tk.END, d)
        if not display:
            self.provider_list.insert(tk.END, "暂无用户配置")
        self.provider_count_var.set(f"{len(rows)} 条用户配置")
        # 模型下拉
        models = []
        for r in rows:
            if "baseURL" in r.get("config", {}) and not r.get("disabled"):
                m = r["config"].get("model") or r.get("id")
                if m and m not in models:
                    models.append(m)
        models = ["default"] + [m for m in models if m and m != "default"]
        self.model_combo.configure(values=models)
        if models and self.model_var.get() not in models:
            self.model_var.set(models[0])
        self._rebuild_feature_toggles()

    def _on_provider_select(self, _=None):
        sel = self.provider_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.user_rows):
            return
        if self._editor_is_dirty() and not messagebox.askyesno(
                "编辑内容尚未保存", "切换条目会替换当前编辑内容。要放弃未保存的编辑吗？", parent=self.root):
            return
        row = self.user_rows[idx]
        self._editor_baseline = copy.deepcopy(self.user_rows)
        # 把 row 放进输入区，便于编辑后重新矫治
        text = json.dumps(row, ensure_ascii=False, indent=2)
        self._editor_clean_text = text.strip()
        self.input_text.delete("1.0", tk.END)
        self.input_text.insert("1.0", text)
        self.input_text.configure(fg=C["text"])
        self._placeholder_visible = False
        self._pending_rows = []
        self._organized_input = ""
        self._set_preview("")
        self._set_warnings([])
        self.save_btn.configure(state=tk.DISABLED)
        self._set_status(f"已加载 {row.get('id', '?')} 到输入区，可编辑后重新整理", "info")
        self._sync_model_editor(row)

    def _sync_model_editor(self, row: dict | None):
        """列表选中变化时，更新左下角「模型快捷编辑」面板。"""
        conf = (row or {}).get("config") or {}
        is_provider = "baseURL" in conf
        self._model_edit_row_id = (row or {}).get("id")
        self.model_edit_target.configure(
            text=(f"{self._model_edit_row_id}\n{(conf.get('baseURL') or '')[:60]}" if is_provider
                  else "（非 provider 条目）"),
            fg=C["text"] if is_provider else C["muted"])
        self.model_edit_var.set(conf.get("model", "") if is_provider else "")
        self.model_apply_btn.configure(state=tk.NORMAL if is_provider else tk.DISABLED)
        self.model_probe_btn.configure(state=tk.NORMAL if is_provider else tk.DISABLED)
        self.model_probe_var.set("")
        # chips
        for w in self.model_chips_frame.winfo_children():
            w.destroy()
        if is_provider:
            suggestions = _suggest_models(conf.get("baseURL") or "")
            if suggestions:
                chip_row = tk.Frame(self.model_chips_frame, bg=C["input_bg"])
                chip_row.pack(anchor=tk.W)
                tk.Label(chip_row, text="常用:", bg=C["input_bg"], fg=C["muted"],
                         font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 4))
                for m in suggestions:
                    tk.Button(chip_row, text=m, bg=C["surface2"], fg=C["link"],
                              activebackground=C["link_soft"], activeforeground=C["link"],
                              font=FONT_SMALL, relief=tk.FLAT, padx=7, pady=1,
                              command=lambda mm=m: (self.model_edit_var.set(mm), self._apply_model_edit()),
                              cursor="hand2").pack(side=tk.LEFT, padx=(3, 0))

    def _apply_model_edit(self):
        """把模型快捷编辑框的值写进 user_rows 并保存。"""
        rid = getattr(self, "_model_edit_row_id", None)
        if not rid:
            return
        row = next((r for r in self.user_rows if r.get("id") == rid), None)
        if not row or "baseURL" not in (row.get("config") or {}):
            self._set_status("选中的不是 provider 条目", "warn")
            return
        new_model = self.model_edit_var.get().strip()
        if not new_model:
            self._set_status("模型名不能为空", "warn")
            return
        row["config"]["model"] = new_model
        row["config"].setdefault("smallModel", new_model)
        try:
            save_user_layer(self.home, self.user_rows)
        except (OSError, ValueError) as exc:
            self._set_status(f"保存失败：{exc}", "error")
            return
        self._refresh_provider_list()
        self._sync_model_editor(row)
        self._set_status(f"{rid} 的模型已改为 {new_model}（smallModel 同步）", "ok")

    def _probe_selected_provider(self):
        """用密钥库里的 key 真实打一发 /chat/completions，结果写在面板上。"""
        rid = getattr(self, "_model_edit_row_id", None)
        if not rid:
            return
        row = next((r for r in self.user_rows if r.get("id") == rid), None)
        if not row:
            return
        conf = row["config"]
        key = _load_secrets().get(rid, "")
        model = conf.get("model") or self.model_edit_var.get().strip()
        self.model_probe_var.set("请求中…")
        self.model_probe_btn.configure(state=tk.DISABLED)

        def worker():
            import urllib.request
            import urllib.error
            url = conf["baseURL"].rstrip("/") + "/chat/completions"
            body = json.dumps({"model": model, "messages": [{"role": "user", "content": "1+1=?"}], "max_tokens": 30}).encode()
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    j = json.loads(resp.read().decode())
                    reply = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    done(f"HTTP {resp.status} · {reply[:30]!r}", True)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:120].replace("\n", " ")
                done(f"HTTP {e.code} · {detail}", False)
            except Exception as e:
                done(f"{type(e).__name__}: {e}", False)

        def done(text, ok):
            def apply():
                self.model_probe_var.set(text)
                self.model_probe_btn.configure(state=tk.NORMAL)
            self._post_ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    # ── 矫治 + 预览 ──
    def _do_organize(self):
        text = self.input_text.get("1.0", "end-1c").strip()
        if not text or self._placeholder_visible:
            self._set_status("输入为空", "warn")
            return
        try:
            res = normalize(text, add_model=False)
        except ConfigNormalizeError as e:
            self._set_status(f"整理失败：{e}", "error")
            self._set_preview("")
            self._set_warnings([f"❌ {e}"])
            self.save_btn.configure(state=tk.DISABLED)
            self._pending_rows = []
            self._organized_input = ""
            return

        self._pending_rows = res.rows
        self._organized_input = text
        self._set_preview(res.to_json())

        warns = list(res.warnings)
        if res.env_hints:
            env_lines = ["  " + f"export {k}='你的密钥'   # {rid}" for rid, k in res.env_hints.items()]
            warns.append("\n🔑 待导出环境变量（启动前 source 一下，或在系统环境里设）：\n" + "\n".join(env_lines))
        self._set_warnings(warns)
        self.save_btn.configure(state=tk.NORMAL)
        self._set_status(
            f"预览已更新：{len(res.rows)} 条配置 · {len(res.warnings)} 项提示 · 尚未保存",
            "ok" if not res.warnings else "warn",
        )

    def _set_preview(self, text: str):
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state=tk.DISABLED)

    def _set_warnings(self, lines: list[str]):
        self.warning_count_var.set(f"{len(lines)} 项提示" if lines else "无提示")
        self.warn_text.configure(state=tk.NORMAL, fg=C["warn"] if lines else C["muted"])
        self.warn_text.delete("1.0", tk.END)
        self.warn_text.insert("1.0", "\n\n".join(lines) if lines else "整理后会在这里显示警告和环境变量。")
        self.warn_text.configure(state=tk.DISABLED)

    # ── 保存 ──
    def _do_save(self):
        if not self._pending_rows:
            self._set_status("没有可保存的内容", "warn")
            return
        if self.input_text.get("1.0", "end-1c").strip() != self._organized_input:
            self._pending_rows = []
            self.save_btn.configure(state=tk.DISABLED)
            self._set_status("输入内容已变化，请重新整理后再保存", "warn")
            return
        # 二次矫治（确保保存的是合法形态）
        try:
            again = normalize(json.dumps(self._pending_rows, ensure_ascii=False), add_model=False)
        except ConfigNormalizeError as e:
            self._set_status(f"再矫治失败：{e}", "error")
            return
        try:
            latest = load_user_layer(self.home)
            old = {r["id"]: r for r in self._editor_baseline}
            current = {r["id"]: r for r in latest}
            for row in again.rows:
                if current.get(row["id"]) != old.get(row["id"]):
                    raise ConfigNormalizeError("此条目在编辑期间已变化，请重新选择条目并编辑，以免覆盖新设置")
            merged = merge_with_user_layer(again.rows, latest)
            save_user_layer(self.home, merged, expected_rows=latest)
        except (OSError, ValueError) as e:
            self._set_status(f"保存失败：{e}", "error")
            self._set_warnings([str(e)])
            return
        self.user_rows = merged
        self._editor_baseline = copy.deepcopy(merged)
        self._pending_rows = []
        self._editor_clean_text = self.input_text.get("1.0", "end-1c").strip()
        self.save_btn.configure(state=tk.DISABLED)
        self._refresh_provider_list()
        self._set_status("配置已保存；运行中的 Forge / 通道服务需重启以应用", "ok")

    # ── 剪贴板 / 清空 / 打开 ──
    def _paste_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self._set_status("剪贴板无文本", "warn")
            return
        self.input_text.delete("1.0", tk.END)
        self.input_text.insert("1.0", text)
        self._editor_baseline = copy.deepcopy(self.user_rows)
        self.input_text.configure(fg=C["text"])
        self._placeholder_visible = False
        self._pending_rows = []
        self._organized_input = ""
        self._set_preview("")
        self._set_warnings([])
        self.save_btn.configure(state=tk.DISABLED)
        self._set_status(f"已粘贴 {len(text)} 字符", "info")

    def _clear_input(self):
        self.input_text.delete("1.0", tk.END)
        self._set_preview("")
        self._set_warnings([])
        self.save_btn.configure(state=tk.DISABLED)
        self._pending_rows = []
        self._organized_input = ""
        self._editor_baseline = copy.deepcopy(self.user_rows)
        self._editor_clean_text = ""
        self._restore_placeholder()
        self._set_status("已清空", "info")

    def _open_path(self, p: Path):
        if IS_WINDOWS:
            os.startfile(p)  # noqa
        else:
            subprocess.Popen(["xdg-open", str(p)])

    # ── Gateway 启停 ──
    def _choose_forge_repo(self):
        if self.gateway_proc or self._sending:
            self._set_status("请在请求结束、gateway 停止后切换 Forge 目录", "warn")
            return
        folder = filedialog.askdirectory(title="选择包含 run.py 的 Forge 目录", parent=self.root)
        if not folder:
            return
        candidate = Path(folder) / "run.py"
        if not candidate.is_file():
            self._set_status("所选目录不包含 run.py，请选择 Forge 根目录", "error")
            return
        self.run_py = candidate
        self.path_lbl.configure(text="forge 目录已就绪")
        self._set_status(f"本次会话使用 Forge：{folder}", "ok")

    def _toggle_gateway(self):
        if self._sending:
            return
        if self.gateway_proc and self.gateway_proc.poll() is None:
            self._stop_gateway()
        else:
            self._start_gateway()

    def _start_gateway(self):
        if self.gateway_proc and self.gateway_proc.poll() is None:
            return
        if not self.run_py:
            self._choose_forge_repo()
            if not self.run_py:
                return
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._set_status("端口须为 1024–65535 的整数", "warn")
            return
        if not 1024 <= port <= 65535:
            self._set_status("端口须为 1024–65535 的整数", "warn")
            return
        self.gateway_port = port
        self.gateway_url = f"http://127.0.0.1:{port}"
        self.client.base_url = self.gateway_url

        # 探活：secret_store（GUI 本地密钥库）注入 env，gateway 子进程继承
        env = {**os.environ, **env_for()}
        # 启动 gateway 子进程
        cmd = [sys.executable, str(self.run_py), "gateway",
               "--upstream", "openai",  # 默认走 openai 协议（用户可改）
               "--port", str(port)]
        # 用上游模型映射：默认 → fallback 链第一个 provider
        upstream = self._first_active_provider()
        if upstream:
            cmd.extend(["--model-map", f"default={upstream['model']}"])

        self._set_status(f"启动 gateway：{' '.join(cmd[-4:])} ...", "info")

        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace", cwd=str(self.run_py.parent))
        if IS_WINDOWS:
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        try:
            self.gateway_proc = subprocess.Popen(cmd, env=env, **kwargs)
        except Exception as e:
            self._set_status(f"启动失败：{e}", "error")
            return

        self.gw_status_var.set("● 启动中")
        self.gw_btn.configure(state=tk.DISABLED, text="启动中…")
        self.port_spin.configure(state=tk.DISABLED)
        proc = self.gateway_proc
        probe = ForgeGatewayClient(self.gateway_url)
        threading.Thread(target=self._drain_gateway_log, args=(proc,), daemon=True).start()
        threading.Thread(target=self._gateway_watchdog, args=(proc, probe), daemon=True).start()

    def _first_active_provider(self) -> dict | None:
        for r in self.user_rows:
            conf = r.get("config") or {}
            if "baseURL" in conf and "model" in conf and not r.get("disabled"):
                return conf
        return None

    def _gateway_watchdog(self, proc, probe):
        """只向主线程提交状态；旧进程的回调不能覆盖新进程。"""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self._closing:
            if proc.poll() is not None:
                return
            ok, _ = probe.health()
            if ok:
                self._post_ui(self._gateway_up, proc)
                return
            time.sleep(0.5)
        self._post_ui(self._gateway_timeout, proc)

    def _drain_gateway_log(self, proc):
        try:
            if proc.stdout:
                for _line in proc.stdout:
                    pass  # 持续排空管道，避免服务被日志堵住，也避免覆盖操作反馈。
                proc.stdout.close()
            proc.wait()
        finally:
            self._post_ui(self._gateway_exited, proc)

    def _gateway_exited(self, proc):
        if self.gateway_proc is proc and proc.poll() is not None:
            self._gateway_down(f"进程已退出（代码 {proc.returncode}）")

    def _gateway_stop_failed(self, proc):
        if self.gateway_proc is proc:
            self.gw_btn.configure(state=tk.NORMAL, text="■ 停止 gateway")
            self.gw_status_var.set("● 停止失败")
            self._set_status("未能停止 gateway，请重试", "error")

    def _gateway_timeout(self, proc):
        if self.gateway_proc is proc and proc.poll() is None:
            self.gw_status_var.set("● 未就绪")
            self.gw_btn.configure(text="■ 停止 gateway", bg=C["error"],
                                  state=tk.DISABLED if self._sending else tk.NORMAL)
            self._set_status("gateway 启动未就绪，请停止后检查配置再重试", "warn")

    def _gateway_up(self, proc):
        if self.gateway_proc is not proc or proc.poll() is not None:
            return
        self.gw_status_var.set(f"● 在线 ({self.gateway_port})")
        self.gw_status_lbl.configure(fg=C["ok"])
        self.gw_btn.configure(text="■ 停止 gateway", bg=C["error"],
                              state=tk.DISABLED if self._sending else tk.NORMAL)
        self._set_status(f"gateway 在线：{self.gateway_url}", "ok")

    def _gateway_down(self, reason: str = "已停止"):
        self.gw_status_var.set("● 离线")
        self.gw_status_lbl.configure(fg=C["muted"])
        self.gw_btn.configure(text="▶ 启动 gateway", bg=C["accent"],
                              state=tk.DISABLED if self._sending else tk.NORMAL)
        self.port_spin.configure(state=tk.DISABLED if self._sending else tk.NORMAL)
        self._set_status(f"gateway {reason}", "warn")
        self.gateway_proc = None

    def _stop_gateway(self):
        if not self.gateway_proc:
            return
        proc = self.gateway_proc
        self.gw_btn.configure(state=tk.DISABLED, text="停止中…")
        self.gw_status_var.set("● 停止中")
        def terminate():
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                self._post_ui(self._gateway_exited if proc.poll() is not None
                              else self._gateway_stop_failed, proc)
        threading.Thread(target=terminate, daemon=False).start()

    # ── 客户端：对话 ──
    def _hide_chat_scrollbar(self):
        try:
            self.chat_text.vbar.pack_forget()
        except Exception:
            pass

    def _show_chat_scrollbar(self):
        try:
            self.chat_text.vbar.pack(side="right", fill="y",
                                     before=self.chat_text.text)
        except Exception:
            pass

    def _show_chat_empty_state(self):
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        # 垂直居中：空态用等量空白 + 居中段落
        self.chat_text.insert(tk.END, "\n" * 6)
        self.chat_text.insert(tk.END, "开始新对话\n", "empty_title")
        self.chat_text.insert(tk.END, "右上启动 gateway，选择模型后在下方输入消息\n", "empty_body")
        self.chat_text.insert(tk.END,
                              "输入卡左下可切换「沉思」：关闭 / 智能 / 开启", "empty_body")
        self.chat_text.configure(state=tk.DISABLED)
        self._chat_empty = True

    def _clear_chat(self):
        if self._sending:
            self._set_status("正在生成回复，完成后可清空对话", "info")
            return
        self._chat_history.clear()
        self._show_chat_empty_state()
        self._set_status("对话已清空", "info")

    def _append_chat(self, role: str, text: str):
        """对照 AutoClaw 气泡样式渲染一条消息。
        用户 = 右对齐浅橙气泡 + 时间戳；AI = 「Forge」头部 + 浅灰气泡；错误 = 浅红标签。
        """
        self.chat_text.configure(state=tk.NORMAL)
        if self._chat_empty:
            self.chat_text.delete("1.0", tk.END)
            self._chat_empty = False
            self._show_chat_scrollbar()
        if self.chat_text.index("end-1c") != "1.0":
            self.chat_text.insert(tk.END, "\n\n")
        stamp = time.strftime("%H:%M")
        if role == "你":
            self.chat_text.insert(tk.END, f"你 · {stamp}\n", "msg_meta_right")
            if text:
                self.chat_text.insert(tk.END, f" {text} ", "bubble_user")
        elif role == "error":
            self.chat_text.insert(tk.END, f"错误 · {stamp}\n", "msg_meta")
            if text:
                self.chat_text.insert(tk.END, f" {text} ", "error_bubble")
        else:
            self.chat_text.insert(tk.END, "Forge\n", "agent_head")
            if text:
                self.chat_text.insert(tk.END, f" {text} ", "bubble_agent")
        self.chat_text.see(tk.END)
        self.chat_text.configure(state=tk.DISABLED)

    def _append_stream_delta(self, piece: str):
        follow_output = self.chat_text.yview()[1] >= 0.98
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(tk.END, piece, "bubble_agent")
        if follow_output:
            self.chat_text.see(tk.END)
        self.chat_text.configure(state=tk.DISABLED)

    def _do_send(self):
        if self._sending:
            return
        text = self.send_var.get().strip()
        if not text:
            return
        try:
            temp = float(self.temp_var.get())
        except ValueError:
            temp = None
        if temp is None or not 0 <= temp <= 2:
            self._set_status("温度须为 0–2 的数字", "warn")
            return
        if not self.gateway_proc:
            try:
                port = int(self.port_var.get())
                if not 1024 <= port <= 65535:
                    raise ValueError
            except ValueError:
                self._set_status("端口须为 1024–65535 的整数", "warn")
                return
            self.gateway_port = port
            self.gateway_url = f"http://127.0.0.1:{port}"
            self.client.base_url = self.gateway_url
        self._sending = True
        self.send_var.set("")
        self._append_chat("你", text)
        self._append_chat("assistant", "")
        messages = list(self._chat_history) + [ChatMessage("user", text)]
        self._refresh_send_circle()
        self.clear_chat_btn.configure(state=tk.DISABLED)
        self.gw_btn.configure(state=tk.DISABLED)
        self.port_spin.configure(state=tk.DISABLED)
        self.request_status_var.set("正在连接…")
        self._set_status(f"请求 → {self.model_var.get()} …", "info")

        model = self.model_var.get()
        client = self.client

        def worker():
            try:
                ok, msg = client.health()
                if not ok:
                    raise GatewayError(f"gateway 未连接：{msg}。请先启动 gateway 后重试。")
                self._post_ui(self.request_status_var.set, "正在生成…")
                acc: list[str] = []
                def on_chunk(piece: str):
                    if self._abort_requested:
                        raise GatewayError("已按用户要求中止")
                    acc.append(piece)
                    self._post_ui(self._append_stream_delta, piece)
                client.stream_chat(
                    messages, model=model,
                    temperature=temp, on_chunk=on_chunk,
                )
                full = "".join(acc)
                self._post_ui(self._chat_succeeded, messages, full)
            except Exception as e:
                error_text = f"{type(e).__name__}: {e}"
                self._post_ui(self._chat_failed, error_text, text)
            finally:
                self._post_ui(self._send_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _chat_succeeded(self, messages, full):
        self._chat_history = messages + [ChatMessage("assistant", full)]
        self._set_status("回复完成", "ok")

    def _chat_failed(self, message, prompt):
        self._append_chat("error", message)
        if not self.send_var.get():
            self.send_var.set(prompt)
        self._set_status("请求失败，消息已保留；检查 gateway 后可重试", "error")

    def _stop_send(self):
        """请求中止当前流式回复。

        urllib 没有暴露打断点，所以在工作线程侧用一个标志位：下一次 chunk
        回调时抛异常退出（forge_client 的 on_chunk 异常会被吞掉，所以我们
        改用自己的标志配合 _chat_failed 分支）。这里先即时反馈状态。
        """
        if not self._sending:
            return
        self._abort_requested = True
        self.request_status_var.set("正在停止…")
        self._set_status("已请求停止——当前这轮回复会在下一个数据块后结束", "warn")
        self._refresh_send_circle()

    def _send_finished(self):
        self._sending = False
        self._abort_requested = False
        self.clear_chat_btn.configure(state=tk.NORMAL)
        self.gw_btn.configure(state=tk.NORMAL)
        if not self.gateway_proc:
            self.port_spin.configure(state=tk.NORMAL)
        self.request_status_var.set("空闲")
        self._refresh_send_circle()

    # ── 关闭 ──
    def _on_close(self):
        if (self._feature_dirty or self._editor_is_dirty() or self._pending_rows) and not messagebox.askyesno(
                "有未保存的修改", "功能开关或编辑内容尚未保存。要放弃这些修改并退出吗？", parent=self.root):
            return
        self._closing = True
        self.root.after_cancel(self._event_poll)
        self._stop_gateway()
        self.root.destroy()


# ─── 入口 ──────────────────────────────────────────────


def main():
    _setup_dpi()
    root = tk.Tk()
    app = ForgeGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
