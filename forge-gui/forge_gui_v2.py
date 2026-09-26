"""forge 图形界面 v3：深色三栏桌面端（对话 / 任务 / 侧栏 / 可收起工作区）。

设计对照参考稿：
    · 顶栏 56px：品牌 FORGE · v0.7.0 · Agent Framework + 主导航
      + 右侧 CPU/GPU/RAM 指标与 Gateway 状态卡
    · 左侧栏 200px：＋新建对话 / 主导航 / 最近对话
    · 中栏：对话（消息气泡·计划步骤·工具调用卡·完成块·输入卡）/
      任务（forge run 步骤）/ 工具（功能开关）/ 配置（配置编辑）
    · 右栏工作区（默认收起）：文件树 / 变更 / 代码 / diff / 预览 / 终端
    · 底部：状态栏

零依赖：tkinter（Python 自带）+ 本仓 config_model.py / forge_client.py /
secret_store.py / gui_theme.py / chat_widgets.py / workspace.py / sysmon.py。

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

import chat_widgets as cw  # noqa: E402
import gui_theme as theme  # noqa: E402
from gui_theme import (  # noqa: E402
    C,
    FONT_CAPTION,
    FONT_MICRO,
    FONT_MONO,
    FONT_MONO_SM,
    FONT_SECTION,
    FONT_SMALL,
    FONT_TITLE,
    FONT_UI,
    FONT_UI_BOLD,
    R_CARD,
    R_MD,
    R_PANEL,
    R_PILL,
    R_SM,
    RoundedCard,
    apply_ttk_theme,
    attach_tooltip,
    badge,
    circle_button,
    dot,
    glyph_button,
    pill_button,
    progress_bar,
    round_rect,
    rounded_points,
    style_scrollbar,
)
try:  # 工作区面板（右栏）
    from workspace import WorkspacePanel  # noqa: E402
except Exception as _ws_exc:  # pragma: no cover - 面板缺失时 GUI 仍可运行
    WorkspacePanel = None  # type: ignore[assignment]
    _WS_IMPORT_ERROR = str(_ws_exc)
else:
    _WS_IMPORT_ERROR = ""
try:  # 系统资源采样（顶栏指标）
    from sysmon import SysMon  # noqa: E402
except Exception as _sm_exc:  # pragma: no cover
    SysMon = None  # type: ignore[assignment]
    _SM_IMPORT_ERROR = str(_sm_exc)
else:
    _SM_IMPORT_ERROR = ""

# ─── 常量 ──────────────────────────────────────────────

APP_VERSION = "0.7.0"
WINDOW_SIZE = "1440x900"
MIN_SIZE = (1120, 720)
FORGE_REPO_HINT = os.environ.get("FORGE_REPO", "").strip()
DEFAULT_FORGE_HOME = Path.home() / ".forge"

IS_WINDOWS = platform.system() == "Windows"

# 主导航（顶栏与侧栏共用；key -> (标签, 图标)）
NAV_ITEMS = [
    ("chat", "对话", "▣"),
    ("task", "任务", "☑"),
    ("agents", "Agents", "⬡"),
    ("tools", "工具集", "✱"),
    ("knowledge", "知识库", "▤"),
    ("evolution", "演化", "⑂"),
    ("files", "文件与项目", "⌂"),
    ("config", "配置", "⚙"),
]
NAV_LABEL = {key: label for key, label, _g in NAV_ITEMS}
NAV_GLYPH = {key: glyph for key, _l, glyph in NAV_ITEMS}

# 沉思模式（forge thinking.mode 三档；GUI 里对齐参考稿输入卡的工具条）
THINKING_LABELS = {"off": "关闭", "smart": "智能", "on": "开启"}
THINKING_CHOICES = [
    ("off", "关闭", "不启用沉思"),
    ("smart", "智能", "按任务复杂度自动决定（推荐）"),
    ("on", "开启", "始终启用沉思"),
]

# 任务视图的策略档位（对应 forge run 的 routing strategy）
STRATEGY_CHOICES = [
    ("economy", "省钱", "只用便宜模型"),
    ("balanced", "均衡", "默认：按任务难度选模型"),
    ("premium", "强力", "优先用最强模型"),
]


# ─── 向后兼容的小工具（历史上这些名字定义在本文件里）──────────


def _rounded_points(x1, y1, x2, y2, r):
    return rounded_points(x1, y1, x2, y2, r)
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
        self.root.title(f"forge — v{APP_VERSION}（对话 · 任务 · 工作区）")
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
        self._session_id = f"s{int(time.time() * 1000)}"
        self._agent_msg = None
        self._task_msg = None
        self._task_proc = None
        self._task_running = False
        self._task_started = 0.0
        self._sysmon = None
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

    # ── UI 构造 ──────────────────────────────────────────
    @staticmethod
    def _style_scrollbar(widget):
        style_scrollbar(widget)

    def _build_ui(self):
        apply_ttk_theme(self.root)

        self._nav_widgets: dict[str, list] = {}
        self._views: dict[str, tk.Frame] = {}
        self._active_view = "chat"
        self._ws_packed = False
        self._metric_labels: dict[str, tk.Label] = {}
        self._metric_bars: dict[str, tk.Canvas] = {}

        # ── 顶部标题栏（品牌 + 主导航 + 指标 + gateway）──
        self._build_topbar()

        # ── 主体：左侧栏 | 中栏视图 | 右栏工作区 ──
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill=tk.BOTH, expand=True)

        self._build_sidebar(body)

        self.split = tk.PanedWindow(body, orient=tk.HORIZONTAL, bg=C["border"],
                                    sashwidth=6, sashrelief=tk.FLAT, bd=0,
                                    handlepad=0, opaqueresize=True)
        self.split.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        center = tk.Frame(self.split, bg=C["chat"])
        self.center = center
        self.split.add(center, minsize=520, stretch="always")

        self._build_views(center)
        self._build_workspace()

        # ── 底部状态栏 ──
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill=tk.X, side=tk.BOTTOM)
        bot = tk.Frame(self.root, bg=C["bg"], height=28)
        bot.pack(fill=tk.X, side=tk.BOTTOM)
        bot.pack_propagate(False)
        self.status_var = tk.StringVar(value="")
        self.status_lbl = tk.Label(bot, textvariable=self.status_var, bg=C["bg"],
                                   fg=C["body"], font=FONT_CAPTION, anchor=tk.W,
                                   padx=16)
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.path_lbl = tk.Label(bot, text=self._status_label_text(), bg=C["bg"],
                                 fg=C["muted"], font=FONT_CAPTION, padx=12)
        self.path_lbl.pack(side=tk.RIGHT)

        self._show_view("chat")
        self._refresh_history()
        self._start_sysmon()

    # ── 顶栏 ──────────────────────────────────────────────
    def _build_topbar(self):
        chrome = tk.Frame(self.root, bg=C["bg"])
        chrome.pack(fill=tk.X)
        bar = tk.Frame(chrome, bg=C["bg"], height=56)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        # 品牌区
        brand = tk.Frame(bar, bg=C["bg"])
        brand.pack(side=tk.LEFT, padx=(16, 10))
        logo = tk.Canvas(brand, width=26, height=26, bg=C["bg"],
                         highlightthickness=0, bd=0)
        round_rect(logo, 0, 0, 25, 25, 8, fill=C["accent"], outline="")
        logo.create_polygon(2, 2, 20, 2, 2, 20, smooth=True, splinesteps=10,
                            fill="#6D63F0", outline="")
        logo.create_text(13, 13, text="F", fill="#FFFFFF",
                         font=(theme.UI_FAMILY, 12, "bold"))
        logo.pack(side=tk.LEFT, padx=(0, 10))
        brand_txt = tk.Frame(brand, bg=C["bg"])
        brand_txt.pack(side=tk.LEFT)
        tk.Label(brand_txt, text="FORGE", bg=C["bg"], fg=C["text"],
                 font=theme.FONT_BRAND).pack(anchor=tk.W)
        sub = tk.Frame(brand_txt, bg=C["bg"])
        sub.pack(anchor=tk.W)
        tk.Label(sub, text="Agent Framework", bg=C["bg"], fg=C["ter"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        tk.Label(sub, text=f"v{APP_VERSION}", bg=C["surface2"], fg=C["muted"],
                 font=FONT_MICRO, padx=5).pack(side=tk.LEFT, padx=(6, 0))

        # 主导航
        nav = tk.Frame(bar, bg=C["bg"])
        nav.pack(side=tk.LEFT, padx=(22, 0))
        for key, label, glyph in NAV_ITEMS:
            holder, btn = self._make_nav_item(nav, key, label, glyph, big=True)
            holder.pack(side=tk.LEFT, padx=(0, 6))

        # 右侧：工作区开关 + gateway 卡 + 指标
        right = tk.Frame(bar, bg=C["bg"])
        right.pack(side=tk.RIGHT, padx=(0, 14))
        self.ws_toggle_btn = pill_button(right, "▤ 工作区", self._toggle_workspace,
                                         kind="ghost", bg=C["bg"], font=FONT_SMALL,
                                         padx=12)
        self.ws_toggle_btn.pack(side=tk.LEFT, padx=(0, 10))
        self._build_gateway_card(right)
        for key, text in (("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM")):
            self._build_metric(right, key, text)

        tk.Frame(chrome, bg=C["border"], height=1).pack(fill=tk.X)

    def _build_metric(self, parent, key: str, text: str):
        box = tk.Frame(parent, bg=C["bg"])
        box.pack(side=tk.LEFT, padx=(0, 12))
        row = tk.Frame(box, bg=C["bg"])
        row.pack(anchor=tk.W)
        tk.Label(row, text=text, bg=C["bg"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        value = tk.Label(row, text="—", bg=C["bg"], fg=C["body"], font=FONT_MICRO)
        value.pack(side=tk.LEFT, padx=(4, 0))
        self._metric_labels[key] = value
        bar = progress_bar(box, 0, width=30, height=3)
        bar.pack(anchor=tk.W, pady=(2, 0))
        self._metric_bars[key] = bar

    def _set_bar(self, canvas: tk.Canvas, pct):
        try:
            canvas.delete("all")
            w, h = int(canvas.cget("width")), int(canvas.cget("height"))
            round_rect(canvas, 0, 0, w, h, h / 2, fill=C["border_hi"], outline="")
            if pct:
                filled = max(2, int(w * max(0.0, min(100.0, float(pct))) / 100.0))
                round_rect(canvas, 0, 0, filled, h, h / 2, fill=C["accent_hover"],
                           outline="")
        except (tk.TclError, ValueError):
            pass

    def _build_gateway_card(self, parent):
        card = tk.Frame(parent, bg=C["surface2"], padx=10, pady=4,
                        highlightthickness=1, highlightbackground=C["border_hi"])
        card.pack(side=tk.LEFT, padx=(0, 12))
        self.gw_status_var = tk.StringVar(value="● 离线")
        self.gw_status_lbl = tk.Label(card, textvariable=self.gw_status_var,
                                      bg=C["surface2"], fg=C["muted"],
                                      font=FONT_SMALL)
        self.gw_status_lbl.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(card, text="Gateway", bg=C["surface2"], fg=C["ter"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.gateway_port))
        self.port_spin = tk.Spinbox(card, from_=1024, to_=65535, width=5,
                                    textvariable=self.port_var, font=FONT_MONO_SM,
                                    bg=C["surface2"], fg=C["body"], bd=0,
                                    buttonbackground=C["surface2"], relief=tk.FLAT,
                                    insertbackground=C["accent"],
                                    highlightthickness=0, justify=tk.CENTER)
        self.port_spin.pack(side=tk.LEFT, padx=(4, 8))
        self.gw_btn = tk.Button(card, text="▶ 启动", command=self._toggle_gateway,
                                bg=C["accent"], fg="#FFFFFF",
                                activebackground=C["accent_hover"],
                                activeforeground="#FFFFFF", font=FONT_MICRO,
                                relief=tk.FLAT, bd=0, padx=10, pady=2,
                                cursor="hand2", highlightthickness=0)
        self.gw_btn.pack(side=tk.LEFT)

    # ── 左侧栏 ────────────────────────────────────────────
    def _make_nav_item(self, parent, key: str, label: str, glyph: str, *, big=False):
        holder = tk.Frame(parent, bg=C["bg"] if big else C["sidebar"],
                          highlightthickness=1,
                          highlightbackground=C["bg"] if big else C["sidebar"])
        text = f"{glyph}  {label}" if not big else f"{glyph} {label}"
        btn = tk.Button(holder, text=text, command=lambda k=key: self._nav_click(k),
                        bg=holder["bg"], fg=C["ter"], activebackground=C["hover"],
                        activeforeground=C["text"],
                        font=FONT_SMALL if not big else FONT_UI,
                        relief=tk.FLAT, bd=0, padx=10 if not big else 12,
                        pady=4, cursor="hand2", highlightthickness=0, anchor=tk.W)
        btn.pack(fill=tk.X)
        self._nav_widgets.setdefault(key, []).append((holder, btn))
        return holder, btn

    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=C["sidebar"], width=200)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        self.sidebar = side

        new_btn = pill_button(side, "＋  新建对话", self._new_session, kind="primary",
                              bg=C["sidebar"], font=FONT_UI, padx=0)
        new_btn.pack(fill=tk.X, padx=12, pady=(14, 12))

        nav_host = tk.Frame(side, bg=C["sidebar"])
        nav_host.pack(fill=tk.X, padx=6)
        for key, label, glyph in NAV_ITEMS:
            holder, _btn = self._make_nav_item(nav_host, key, label, glyph)
            holder.pack(fill=tk.X, pady=1)

        head = tk.Frame(side, bg=C["sidebar"])
        head.pack(fill=tk.X, padx=14, pady=(16, 6))
        tk.Label(head, text="最近对话", bg=C["sidebar"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        glyph_button(head, "⌗", self._toggle_session_search, bg=C["sidebar"],
                     fg=C["muted"], size=12, tooltip="搜索对话").pack(side=tk.RIGHT)
        self._search_visible = False
        self.session_search_var = tk.StringVar()
        self.session_search = tk.Entry(side, textvariable=self.session_search_var,
                                       bg=C["surface2"], fg=C["text"], bd=0,
                                       relief=tk.FLAT, insertbackground=C["accent"],
                                       font=FONT_SMALL, highlightthickness=1,
                                       highlightbackground=C["border_hi"],
                                       highlightcolor=C["accent"])
        self.session_search_var.trace_add("write", lambda *_: self._refresh_history())

        self.history_area = cw.ScrollArea(side, bg=C["sidebar"], pady=2)
        self.history_area.pack(fill=tk.BOTH, expand=True, padx=6)
        self.history_box = self.history_area.inner

        more = tk.Label(side, text="•••   更多 …", bg=C["sidebar"], fg=C["muted"],
                        font=FONT_SMALL, anchor=tk.W, padx=14, pady=10,
                        cursor="hand2")
        more.pack(side=tk.BOTTOM, fill=tk.X)
        more.bind("<Button-1>", lambda _e: self._set_status(
            "更多功能（Agents / 知识库 / 演化）在主体中查看", "info"))

        tk.Frame(parent, bg=C["border"], width=1).pack(side=tk.LEFT, fill=tk.Y)

    def _toggle_session_search(self):
        self._search_visible = not self._search_visible
        if self._search_visible:
            self.session_search.pack(fill=tk.X, padx=12, pady=(0, 6))
            self.session_search.focus_set()
        else:
            self.session_search_var.set("")
            self.session_search.pack_forget()
        self._refresh_history()

    # ── 视图切换 ──────────────────────────────────────────
    def _build_views(self, center):
        self.tab_client = tk.Frame(center, bg=C["chat"])
        self._build_client_tab(self.tab_client)

        self.tab_task = tk.Frame(center, bg=C["chat"])
        self._build_task_view(self.tab_task)

        self.tab_features = tk.Frame(center, bg=C["bg"])
        host = tk.Frame(self.tab_features, bg=C["bg"], padx=20, pady=2)
        host.pack(fill=tk.BOTH, expand=True)
        self._build_feature_panel(host)

        self.tab_manage = tk.Frame(center, bg=C["bg"])
        host2 = tk.Frame(self.tab_manage, bg=C["bg"], padx=20, pady=2)
        host2.pack(fill=tk.BOTH, expand=True)
        self._build_manage_tab(host2)

        self._views = {
            "chat": self.tab_client,
            "task": self.tab_task,
            "tools": self.tab_features,
            "config": self.tab_manage,
        }
        for key in ("agents", "knowledge", "evolution", "files"):
            frame = tk.Frame(center, bg=C["bg"])
            self._build_stub_view(frame, key)
            self._views[key] = frame

    STUB_TEXT = {
        "agents": ("Agents", "forge 的 Agent 注册表与子 Agent 调度",
                   ("registry.py 里的成员定义", "subagent 派发与回执",
                    "多模型协作（MOA）编排"),
                   "Agent 面板会把 registry 里的成员、能力与最近一次调度画出来。"),
        "knowledge": ("知识库", "长期记忆与策展（memory / curator）",
                      ("会话记忆切片", "策展器打分与淘汰", "分级检索接入"),
                      "知识库面板会列出现有记忆条目、来源与最近命中。"),
        "evolution": ("演化", "自演化迭代账本（evolution / iteration-ledger）",
                      ("迭代记录与指标", "能力包升级", "回归对比"),
                      "演化面板会把 iteration-ledger.jsonl 画成时间线。"),
        "files": ("文件与项目", "在右侧工作区里浏览与改动项目文件",
                  ("文件树 / 变更 / 代码 / diff / 预览 / 终端",),
                  "点上方按钮或调用「打开工作区」即可展开右栏。"),
    }

    def _build_stub_view(self, parent, key: str):
        title, subtitle, bullets, note = self.STUB_TEXT.get(key, (key, "", (), ""))
        wrap = tk.Frame(parent, bg=C["bg"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=24, pady=24)
        card = RoundedCard(wrap, radius=R_PANEL, fill=C["surface"],
                           outline=C["border_hi"], padx=22, pady=20, bg=C["bg"])
        card.pack(fill=tk.X)
        head = tk.Frame(card.content, bg=C["surface"])
        head.pack(fill=tk.X)
        tk.Label(head, text=f"{NAV_GLYPH.get(key, '●')}  {title}", bg=C["surface"],
                 fg=C["text"], font=FONT_TITLE).pack(side=tk.LEFT)
        badge(head, "规划中", tone="muted", bg=C["surface"]).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(card.content, text=subtitle, bg=C["surface"], fg=C["ter"],
                 font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=680).pack(fill=tk.X, pady=(6, 10))
        for item in bullets:
            row = tk.Frame(card.content, bg=C["surface"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text="•", bg=C["surface"], fg=C["accent2"],
                     font=FONT_UI_BOLD, width=2).pack(side=tk.LEFT)
            tk.Label(row, text=item, bg=C["surface"], fg=C["body"],
                     font=FONT_SMALL).pack(side=tk.LEFT)
        tk.Label(card.content, text=note, bg=C["surface"], fg=C["muted"],
                 font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=680).pack(fill=tk.X, pady=(10, 0))
        actions = tk.Frame(card.content, bg=C["surface"])
        actions.pack(fill=tk.X, pady=(14, 0))
        if key == "files":
            pill_button(actions, "打开工作区", lambda: self._open_workspace("file_tree"),
                        kind="primary", bg=C["surface"]).pack(side=tk.LEFT)
        else:
            pill_button(actions, "回到对话", lambda: self._show_view("chat"),
                        kind="primary", bg=C["surface"]).pack(side=tk.LEFT)
            pill_button(actions, "打开工作区", lambda: self._open_workspace("file_tree"),
                        kind="ghost", bg=C["surface"]).pack(side=tk.LEFT, padx=(8, 0))

    def _nav_click(self, key: str):
        if key == "files":
            self._show_view("chat")
            self._open_workspace("file_tree")
            self._set_nav_active("files")
            return
        self._show_view(key)

    def _show_view(self, key: str):
        if key not in self._views:
            key = "chat"
        for other, frame in self._views.items():
            if other != key:
                frame.pack_forget()
        self._views[key].pack(fill=tk.BOTH, expand=True)
        self._active_view = key
        self._set_nav_active(key)
        if key == "config":
            self._update_status_label()

    def _set_nav_active(self, key: str):
        for nav_key, widgets in self._nav_widgets.items():
            active = nav_key == key
            for holder, btn in widgets:
                try:
                    base = holder.master.cget("bg")
                except Exception:
                    base = C["bg"]
                bg = C["sel"] if active else base
                holder.configure(bg=bg,
                                 highlightbackground=C["sel_border"] if active else base)
                btn.configure(bg=bg, fg=C["text"] if active else C["ter"],
                              activebackground=C["hover"] if active else base,
                              activeforeground=C["text"])

    # ── 右栏工作区 ────────────────────────────────────────
    def _repo_root(self) -> Path:
        if self.run_py:
            return self.run_py.parent
        return HERE.parent

    def _build_workspace(self):
        self.workspace = None
        self.ws_holder = tk.Frame(self.split, bg=C["bg"])
        if WorkspacePanel is None:
            self._ws_error = _WS_IMPORT_ERROR or "workspace 模块未安装"
            return
        self._ws_error = ""
        try:
            self.workspace = WorkspacePanel(self.ws_holder, app=self,
                                            repo_root=self._repo_root(),
                                            on_close=self._close_workspace)
        except Exception as exc:  # pragma: no cover
            self.workspace = None
            self._ws_error = str(exc)

    def _open_workspace(self, tab: str = "file_tree"):
        if self.workspace is None:
            self._set_status(f"工作区面板不可用：{self._ws_error}", "warn")
            return
        if not self._ws_packed:
            try:
                self.split.add(self.ws_holder, minsize=420, width=640,
                               stretch="never")
                self._ws_packed = True
            except tk.TclError:
                pass
        try:
            self.workspace.show()
            if not self.workspace.winfo_ismapped():
                self.workspace.pack(fill=tk.BOTH, expand=True)
        except Exception as exc:  # pragma: no cover
            self._set_status(f"打开工作区失败：{exc}", "error")
            return
        if tab == "changes":
            self.workspace.open_changes()
        elif tab == "preview":
            try:
                self.workspace._set_preview_sub("预览")
            except Exception:
                pass
        else:
            try:
                self.workspace.open_file_tree()
            except Exception:
                pass
        try:
            self.workspace.refresh()
        except Exception:
            pass
        self.ws_toggle_btn.configure(text="▤ 收起工作区")

    def _close_workspace(self):
        if self.workspace is not None:
            try:
                self.workspace.hide()
            except Exception:
                pass
        if self._ws_packed:
            try:
                self.split.forget(self.ws_holder)
            except tk.TclError:
                pass
            self._ws_packed = False
        try:
            self.ws_toggle_btn.configure(text="▤ 工作区")
        except (tk.TclError, AttributeError):
            pass

    def _toggle_workspace(self):
        if self._ws_packed:
            self._close_workspace()
        else:
            self._open_workspace("file_tree")

    # ── 顶栏指标 ──────────────────────────────────────────
    def _start_sysmon(self):
        self._sysmon = None
        if SysMon is None:
            for key, value in self._metric_labels.items():
                value.configure(text="—")
            return
        try:
            self._sysmon = SysMon(lambda payload: self._post_ui(self._on_metrics, payload),
                                  interval=2.0)
            self._sysmon.start()
        except Exception as exc:  # pragma: no cover
            self._sysmon = None
            self._set_status(f"系统指标不可用：{exc}", "warn")

    def _on_metrics(self, payload: dict):
        mapping = {"cpu": "cpu", "ram": "ram_pct", "gpu": "gpu"}
        for key, src in mapping.items():
            label = self._metric_labels.get(key)
            bar = self._metric_bars.get(key)
            if label is None:
                continue
            value = (payload or {}).get(src)
            if value is None:
                label.configure(text="—")
                if bar is not None:
                    self._set_bar(bar, 0)
                continue
            label.configure(text=f"{float(value):.0f}%")
            if bar is not None:
                self._set_bar(bar, value)
        gpu_name = (payload or {}).get("gpu_name")
        if gpu_name and not getattr(self, "_gpu_tip_done", False):
            self._gpu_tip_done = True
            attach_tooltip(self._metric_labels["gpu"], f"显卡：{gpu_name}")

    # ── 最近对话（会话持久化）─────────────────────────────
    def _sessions_path(self) -> Path:
        root = Path(self.home)
        try:
            gui_dir = root / "gui"
            gui_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return root / "gui-sessions.json"
        return gui_dir / "sessions.json"

    def _load_sessions(self) -> list[dict]:
        path = self._sessions_path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sessions = data.get("sessions") if isinstance(data, dict) else data
        if not isinstance(sessions, list):
            return []
        return [s for s in sessions if isinstance(s, dict)]

    def _write_sessions(self, sessions: list[dict]):
        try:
            payload = {"sessions": sessions[:40]}
            self._sessions_path().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._set_status(f"会话记录保存失败：{exc}", "warn")

    def _session_title(self) -> str:
        for msg in self._chat_history:
            if msg.role == "user" and msg.content.strip():
                first = msg.content.strip().splitlines()[0]
                return first if len(first) <= 26 else first[:26] + "…"
        return "新对话"

    def _archive_current_session(self):
        history = list(getattr(self, "_chat_history", []) or [])
        if not history:
            return
        sid = getattr(self, "_session_id", None) or f"s{int(time.time() * 1000)}"
        sessions = self._load_sessions()
        entry = {
            "id": sid,
            "title": self._session_title(),
            "updated": time.time(),
            "messages": [{"role": m.role, "content": m.content} for m in history],
        }
        sessions = [s for s in sessions if s.get("id") != sid]
        sessions.insert(0, entry)
        self._write_sessions(sessions)

    def _refresh_history(self):
        box = getattr(self, "history_box", None)
        if box is None:
            return
        for child in box.winfo_children():
            child.destroy()
        query = (self.session_search_var.get() if hasattr(self, "session_search_var")
                 else "").strip().lower()
        sessions = self._load_sessions()
        if query:
            sessions = [s for s in sessions if query in str(s.get("title", "")).lower()]
        if not sessions:
            tk.Label(box, text="还没有历史对话" if not query else "没有匹配的对话",
                     bg=C["sidebar"], fg=C["muted"], font=FONT_MICRO,
                     anchor=tk.W, padx=10, pady=8).pack(fill=tk.X)
            return
        active_id = getattr(self, "_session_id", None)
        for session in sessions[:14]:
            sid = str(session.get("id", ""))
            active = sid == active_id
            row = tk.Frame(box, bg=C["sel"] if active else C["sidebar"],
                           cursor="hand2",
                           highlightthickness=1,
                           highlightbackground=C["sel_border"] if active else C["sidebar"])
            row.pack(fill=tk.X, pady=1, padx=2)
            tk.Label(row, text="▣", bg=row["bg"], fg=C["accent2"],
                     font=FONT_MICRO).pack(side=tk.LEFT, padx=(8, 6), pady=4)
            tk.Label(row, text=str(session.get("title", "未命名对话")), bg=row["bg"],
                     fg=C["text"] if active else C["subtext"], font=FONT_SMALL,
                     anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True, pady=4)
            for widget in (row, *row.winfo_children()):
                widget.bind("<Button-1>", lambda _e, s=sid: self._load_session(s))

    def _new_session(self):
        if self._sending:
            self._set_status("正在生成回复，完成后可新建对话", "info")
            return
        self._archive_current_session()
        self._chat_history.clear()
        self._session_id = f"s{int(time.time() * 1000)}"
        if hasattr(self, "chat_area"):
            self.chat_area.show_empty()
        try:
            self.chat_title_var.set("新对话")
            self.chat_sub_var.set("在下方输入消息，或切到「任务」用 forge run 跑一个任务")
        except AttributeError:
            pass
        self._refresh_history()
        self._set_status("已新建对话", "info")

    def _load_session(self, sid: str):
        if self._sending:
            self._set_status("正在生成回复，完成后可切换对话", "info")
            return
        session = next((s for s in self._load_sessions() if str(s.get("id")) == sid), None)
        if session is None:
            return
        self._chat_history = [ChatMessage(str(m.get("role", "user")),
                                          str(m.get("content", "")))
                              for m in session.get("messages", []) if isinstance(m, dict)]
        self._session_id = sid
        if hasattr(self, "chat_area"):
            self.chat_area.clear()
            for msg in self._chat_history:
                if msg.role == "user":
                    self.chat_area.add_user(msg.content)
                elif msg.content.strip():
                    agent = self.chat_area.add_agent()
                    agent.render_markdown(msg.content)
        try:
            self.chat_title_var.set(str(session.get("title", "对话")))
            self.chat_sub_var.set("从历史对话载入 · 继续在下方输入即可")
        except AttributeError:
            pass
        self._refresh_history()
        self._set_status(f"已载入对话：{session.get('title', '')}", "info")

    # ── 任务视图（forge run）───────────────────────────────
    def _build_task_view(self, parent):
        head = tk.Frame(parent, bg=C["chat"])
        head.pack(fill=tk.X, padx=20, pady=(16, 10))
        tk.Label(head, text="任务", bg=C["chat"], fg=C["text"],
                 font=FONT_TITLE).pack(side=tk.LEFT)
        tk.Label(head, text="  forge run", bg=C["chat"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(8, 0), pady=(4, 0))
        tk.Button(head, text="＋ 新建任务", command=self._clear_task_view,
                  bg=C["chat"], fg=C["ter"], activebackground=C["hover"],
                  activeforeground=C["text"], font=FONT_SMALL, relief=tk.FLAT, bd=0,
                  padx=10, pady=3, cursor="hand2",
                  highlightthickness=1, highlightbackground=C["border_hi"]
                  ).pack(side=tk.RIGHT)
        tk.Label(parent, text="输入任务 → forge 在自己工作区里跑（可写文件、跑命令），"
                              "右侧工作区可看代码、diff 与预览。",
                 bg=C["chat"], fg=C["ter"], font=FONT_SMALL, anchor=tk.W,
                 justify=tk.LEFT, wraplength=760).pack(fill=tk.X, padx=20)
        tk.Frame(parent, bg=C["border"], height=1).pack(fill=tk.X, pady=(10, 0))

        ctl = tk.Frame(parent, bg=C["chat"])
        ctl.pack(fill=tk.X, padx=20, pady=(12, 8))
        self.task_var = tk.StringVar()
        entry = tk.Entry(ctl, textvariable=self.task_var, bg=C["input_bg"],
                         fg=C["text"], insertbackground=C["accent"], font=FONT_UI,
                         relief=tk.FLAT, bd=0, highlightthickness=1,
                         highlightbackground=C["border_hi"], highlightcolor=C["accent"])
        entry.pack(fill=tk.X, ipady=7, ipadx=8)
        entry.bind("<Return>", lambda _e: self._run_task())

        row = tk.Frame(ctl, bg=C["chat"])
        row.pack(fill=tk.X, pady=(8, 0))
        tk.Label(row, text="策略", bg=C["chat"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(0, 6))
        self._strategy_row = tk.Frame(row, bg=C["chat"])
        self._strategy_row.pack(side=tk.LEFT)
        self._task_strategy = "balanced"
        self._render_strategy_chips()
        self.task_stop_btn = pill_button(row, "■ 停止", self._stop_task, kind="danger",
                                         bg=C["chat"])
        self.task_stop_btn.pack(side=tk.RIGHT)
        self.task_run_btn = pill_button(row, "▶ 运行任务", self._run_task,
                                        kind="primary", bg=C["chat"], font=FONT_UI)
        self.task_run_btn.pack(side=tk.RIGHT, padx=(0, 8))
        pill_button(row, "▤ 打开工作区", lambda: self._open_workspace("file_tree"),
                    kind="ghost", bg=C["chat"]).pack(side=tk.RIGHT, padx=(0, 8))

        self.task_area = cw.MessageArea(parent, bg=C["chat"])
        self.task_area.pack(fill=tk.BOTH, expand=True)
        self.task_area.show_empty("还没有任务",
                                  ("输入任务后回车，forge 会在自己工作区里执行",
                                   "执行步骤、工具调用与产出都会显示在这里"))

    def _render_strategy_chips(self):
        row = getattr(self, "_strategy_row", None)
        if row is None:
            return
        for child in row.winfo_children():
            child.destroy()
        for value, label, hint in STRATEGY_CHOICES:
            chip = theme.chip(row, label, selected=(value == self._task_strategy),
                              command=lambda v=value: self._set_task_strategy(v))
            chip.pack(side=tk.LEFT, padx=(0, 6))
            attach_tooltip(chip, hint)

    def _set_task_strategy(self, value: str):
        self._task_strategy = value
        self._render_strategy_chips()

    def _clear_task_view(self):
        if getattr(self, "_task_running", False):
            self._set_status("任务正在运行，先停止再新建", "info")
            return
        self.task_area.show_empty("还没有任务",
                                  ("输入任务后回车，forge 会在自己工作区里执行",
                                   "执行步骤、工具调用与产出都会显示在这里"))

    def _run_task(self):
        if getattr(self, "_task_running", False):
            return
        task = self.task_var.get().strip()
        if not task:
            self._set_status("请输入任务内容", "warn")
            return
        if not self.run_py:
            self._choose_forge_repo()
            if not self.run_py:
                return
        self.task_area.clear()
        self.task_area.add_user(task)
        label = next((l for v, l, _h in STRATEGY_CHOICES if v == self._task_strategy),
                     self._task_strategy)
        self._task_msg = self.task_area.add_agent(role="Planner",
                                                  subtitle=f"策略：{label} · 工作区：{self.run_py.parent}")
        self._task_msg.stream_text("正在执行 forge run …")
        self._task_msg.set_status("运行中…")
        self._task_running = True
        self._task_started = time.time()
        self.task_run_btn.configure(state=tk.DISABLED)
        self._set_status(f"任务已下发：{task[:40]}", "info")

        cmd = [sys.executable, str(self.run_py), "run", task, "--json",
               "--profile", self._task_strategy]
        env = {**os.environ, **env_for()}
        cwd = str(self.run_py.parent)

        def worker():
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8",
                                        errors="replace")
                self._task_proc = proc
                out, err = proc.communicate()
                self._post_ui(self._task_finished, proc.returncode, out, err)
            except Exception as exc:  # pragma: no cover
                self._post_ui(self._task_failed, f"{type(exc).__name__}: {exc}")

        self._task_proc = None
        threading.Thread(target=worker, daemon=True).start()

    def _stop_task(self):
        proc = getattr(self, "_task_proc", None)
        if not getattr(self, "_task_running", False) or proc is None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        self._set_status("已请求停止任务", "warn")

    @staticmethod
    def _parse_task_output(out: str) -> dict | None:
        text = (out or "").strip()
        if not text:
            return None
        candidates = [text]
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
        for chunk in candidates:
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def _task_failed(self, message: str):
        self._task_running = False
        self.task_run_btn.configure(state=tk.NORMAL)
        if getattr(self, "_task_msg", None) is not None:
            self._task_msg.set_status("")
            self._task_msg.add_note(f"任务未能完成：{message}", tone="error")
        self._set_status(f"任务失败：{message}", "error")

    def _task_finished(self, code: int, out: str, err: str):
        self._task_running = False
        self.task_run_btn.configure(state=tk.NORMAL)
        elapsed = max(0.0, time.time() - getattr(self, "_task_started", time.time()))
        msg = getattr(self, "_task_msg", None)
        data = self._parse_task_output(out)
        if msg is None:
            self._set_status("任务结束（视图已切换）", "info")
            return
        msg.set_status("")

        if isinstance(data, dict):
            steps = data.get("steps") or []
            if steps:
                rows = []
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    note = str(step.get("note") or step.get("decision") or "")
                    rows.append({
                        "name": str(step.get("tool") or f"step {step.get('index', '?')}"),
                        "desc": note[:80],
                        "elapsed": f"#{step.get('index', '')}",
                        "ok": str(step.get("decision", "ok")) not in ("error", "failed"),
                    })
                msg.add_tool_card(rows, title="执行步骤")
            text = str(data.get("text") or "").strip()
            if text:
                msg.render_markdown(text)
            elif not steps:
                msg.render_markdown("（本次没有返回文本）")
            usage = data.get("usage") or {}
            parts = [f"退出码 {code}", f"用时 {elapsed:.1f}s"]
            if isinstance(usage, dict):
                tokens = usage.get("total_tokens") or usage.get("tokens")
                if tokens:
                    parts.append(f"tokens {tokens}")
            if data.get("stopped"):
                parts.append("提前停止")
            msg.add_note(" · ".join(parts), tone="ok" if code == 0 else "warn")
        else:
            raw = (out or "").strip() or (err or "").strip() or "（没有输出）"
            msg.render_markdown(raw)
            msg.add_note(f"退出码 {code} · 用时 {elapsed:.1f}s（未能解析结构化结果）",
                         tone="ok" if code == 0 else "error")
        if err and isinstance(data, dict):
            tail = err.strip().splitlines()[-4:]
            if tail:
                msg.add_note("stderr：\n" + "\n".join(tail), tone="muted")

        # 面向工作区的动作
        changed = self._changed_file_count()
        actions = []
        if changed is None:
            actions.append({"label": "查看修改的文件", "kind": "primary",
                            "command": lambda: self._open_workspace("changes")})
        elif changed > 0:
            actions.append({"label": f"查看修改的文件 ({changed})", "kind": "primary",
                            "command": lambda: self._open_workspace("changes")})
        actions.append({"label": "打开工作区", "command": lambda: self._open_workspace("file_tree")})
        if changed:
            actions.append({"label": "预览效果", "command": lambda: self._open_workspace("preview")})
        msg.add_actions(actions)

        if self.workspace is not None:
            try:
                self.workspace.refresh()
            except Exception:
                pass
            self._push_terminal(f"[task] 退出码 {code} · 用时 {elapsed:.1f}s · {self._task_strategy}")
        self._set_status(f"任务结束（退出码 {code}，用时 {elapsed:.1f}s）",
                         "ok" if code == 0 else "warn")

    def _changed_file_count(self):
        """尽量从工作区面板拿改动数量；拿不到就返回 None。"""
        ws = getattr(self, "workspace", None)
        if ws is None:
            return None
        for attr in ("changes_count", "changed_count"):
            fn = getattr(ws, attr, None)
            if callable(fn):
                try:
                    return int(fn())
                except Exception:
                    return None
        for attr in ("_changes", "changes"):
            data = getattr(ws, attr, None)
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return len(data)
        return None

    def _push_terminal(self, line: str):
        ws = getattr(self, "workspace", None)
        if ws is None:
            return
        try:
            ws.push_terminal(line)
        except Exception:
            pass
    @staticmethod
    def _tint_scrolledtext(widget, bg: str):
        """scrolledtext 的外层 Frame 默认是系统灰，跟着主题上色。"""
        try:
            widget.master.configure(bg=bg)
        except Exception:
            pass

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
        self._tint_scrolledtext(self.input_text, C["input_bg"])
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
        self._tint_scrolledtext(self.preview_text, C["input_bg"])
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
        self._tint_scrolledtext(self.warn_text, C["surface"])
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

    # ── 视图：对话 ────────────────────────────────────────
    def _build_client_tab(self, parent):
        self._thinking_mode = self._read_thinking_mode()
        head = tk.Frame(parent, bg=C["chat"])
        head.pack(fill=tk.X, padx=20, pady=(16, 10))
        left = tk.Frame(head, bg=C["chat"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title_row = tk.Frame(left, bg=C["chat"])
        title_row.pack(anchor=tk.W, fill=tk.X)
        self.chat_title_var = tk.StringVar(value="新对话")
        tk.Label(title_row, textvariable=self.chat_title_var, bg=C["chat"],
                 fg=C["text"], font=FONT_TITLE).pack(side=tk.LEFT)
        glyph_button(title_row, "✎", self._rename_session, bg=C["chat"],
                     fg=C["ter"], size=10, tooltip="重命名对话").pack(side=tk.LEFT,
                                                                   padx=(8, 0))
        self.chat_sub_var = tk.StringVar(
            value="连接本机 gateway 与已配置模型对话；右侧工作区可看代码、diff 与预览。")
        tk.Label(left, textvariable=self.chat_sub_var, bg=C["chat"], fg=C["ter"],
                 font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=720).pack(anchor=tk.W, pady=(3, 0))

        right = tk.Frame(head, bg=C["chat"])
        right.pack(side=tk.RIGHT, anchor=tk.N)
        self.clear_chat_btn = pill_button(right, "＋ 新对话", self._new_session,
                                          kind="ghost", bg=C["chat"])
        self.clear_chat_btn.pack(side=tk.RIGHT)
        glyph_button(right, "⋯", lambda: self._set_status(
            "更多：/ 命令、附件与上下文注入在后续版本接入", "info"),
            bg=C["chat"], fg=C["ter"], size=13, tooltip="更多").pack(side=tk.RIGHT,
                                                                     padx=(0, 4))
        temp_box = tk.Frame(right, bg=C["chat"])
        temp_box.pack(side=tk.RIGHT, padx=(0, 10))
        self.temp_var = tk.StringVar(value="0.7")
        tk.Label(temp_box, text="温度", bg=C["chat"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(0, 4))
        tk.Entry(temp_box, textvariable=self.temp_var, width=4, bg=C["surface2"],
                 fg=C["body"], font=FONT_MONO_SM, relief=tk.FLAT, bd=0,
                 insertbackground=C["accent"], highlightthickness=1,
                 highlightbackground=C["border_hi"],
                 highlightcolor=C["accent"]).pack(side=tk.LEFT, ipady=2)

        tk.Frame(parent, bg=C["border"], height=1).pack(fill=tk.X)

        self.chat_area = cw.MessageArea(parent, bg=C["chat"])
        self.chat_area.pack(fill=tk.BOTH, expand=True)
        self._chat_empty = True

        # 模型选择器（放进输入卡右组，保持 pill 观感）
        self.model_var = tk.StringVar(value="default")
        self.model_combo = None  # 由 _make_model_picker 在输入卡里建

        self.input_card = cw.InputCard(
            parent, bg=C["chat"],
            placeholder="输入消息，或输入 / 使用命令...",
            on_send=self._do_send,
            on_stop=self._stop_send,
            on_paste=self._paste_into_input,
            on_model=self._open_model_menu,
            model_var=self.model_var,
            on_thinking=self._open_thinking_menu,
            thinking_text=self._thinking_label(),
            footer_left="空闲",
            model_widget=self._make_model_picker,
        )
        self.input_card.pack(fill=tk.X, side=tk.BOTTOM)
        self.send_entry = self.input_card.entry
        self.send_var = self.input_card.send_var
        self.think_pill = self.input_card.think_pill
        self.request_status_var = tk.StringVar(value="空闲")
        self.chat_area.show_empty()

    def _make_model_picker(self, host):
        """把模型下拉做成输入卡右组里的一个 pill 观感控件。"""
        self.model_combo = ttk.Combobox(host, textvariable=self.model_var,
                                        values=["default"], state="readonly",
                                        width=15, font=FONT_SMALL)
        self.model_combo.pack(side=tk.LEFT, padx=(0, 8))
        attach_tooltip(self.model_combo, "选择模型（来自已启用的 Provider）")
        return self.model_combo

    def _open_model_menu(self):
        """点标题栏模型胶囊时的兜底：直接聚焦模型下拉。"""
        try:
            self.model_combo.focus_set()
            self.model_combo.event_generate("<Button-1>")
        except tk.TclError:
            pass

    def _rename_session(self):
        from tkinter import simpledialog
        current = self.chat_title_var.get()
        try:
            name = simpledialog.askstring("重命名对话", "对话名称：",
                                          initialvalue=current, parent=self.root)
        except tk.TclError:
            return
        if name and name.strip():
            self.chat_title_var.set(name.strip())
            self._archive_current_session()
            self._refresh_history()
            self._set_status(f"对话已重命名为「{name.strip()}」", "ok")
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
        self.input_card.set_thinking_text(self._thinking_label())
        self.think_pill.configure(bg=C["accent_soft"] if mode != "off" else C["input_bg"])
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
                for line in proc.stdout:
                    # 持续排空管道（避免服务被日志堵住），同时把日志喂给右栏终端
                    text = (line or "").rstrip()
                    if text:
                        self._post_ui(self._push_terminal, text)
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
        """保留旧接口：消息区自己管滚动条显隐。"""

    def _show_chat_scrollbar(self):
        """保留旧接口。"""

    def _refresh_send_circle(self):
        try:
            self.input_card._sync_send_state()
        except Exception:
            pass

    def _set_request_status(self, text: str):
        try:
            self.input_card.set_status(text)
        except Exception:
            pass
        try:
            self.request_status_var.set(text)
        except Exception:
            pass

    def _show_chat_empty_state(self):
        self._chat_empty = True
        self._agent_msg = None
        area = getattr(self, "chat_area", None)
        if area is not None:
            area.show_empty()

    def _clear_chat(self):
        if self._sending:
            self._set_status("正在生成回复，完成后可清空对话", "info")
            return
        self._chat_history.clear()
        self._show_chat_empty_state()
        self._set_status("对话已清空", "info")

    def _append_chat(self, role: str, text: str):
        """追加一条消息；assistant 返回消息对象供流式写入。"""
        area = getattr(self, "chat_area", None)
        if area is None:
            return None
        self._chat_empty = False
        if role == "你":
            return area.add_user(text)
        if role == "error":
            return area.add_notice(text, tone="error", title="错误")
        msg = area.add_agent()
        if text:
            msg.stream_text(text)
        return msg

    def _append_stream_delta(self, piece: str):
        msg = getattr(self, "_agent_msg", None)
        area = getattr(self, "chat_area", None)
        if msg is None or area is None:
            return
        follow_output = area.scroll.near_bottom()
        msg.append_stream(piece)
        if follow_output:
            area.scroll.scroll_to_end()

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
        self._chat_empty = False
        self.chat_area.add_user(text)
        self._agent_msg = self.chat_area.add_agent()
        self._agent_msg.set_status("生成中…")
        self._agent_msg.stream_text("")
        messages = list(self._chat_history) + [ChatMessage("user", text)]
        self.input_card.set_busy(True)
        self.clear_chat_btn.configure(state=tk.DISABLED)
        self.gw_btn.configure(state=tk.DISABLED)
        self.port_spin.configure(state=tk.DISABLED)
        self._set_request_status("正在连接…")
        self._set_status(f"请求 → {self.model_var.get()} …", "info")

        model = self.model_var.get()
        client = self.client

        def worker():
            try:
                ok, msg = client.health()
                if not ok:
                    raise GatewayError(f"gateway 未连接：{msg}。请先启动 gateway 后重试。")
                self._post_ui(self._set_request_status, "正在生成…")
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
        msg = getattr(self, "_agent_msg", None)
        if msg is not None:
            msg.set_status("")
            msg.render_markdown(full or "（空回复）")
            actions = [{"label": "打开工作区",
                        "command": lambda: self._open_workspace("file_tree")}]
            changed = self._changed_file_count()
            if isinstance(changed, int) and changed > 0:
                actions.insert(0, {"label": f"查看修改的文件 ({changed})",
                                   "kind": "primary",
                                   "command": lambda: self._open_workspace("changes")})
                actions.append({"label": "预览效果",
                                "command": lambda: self._open_workspace("preview")})
            msg.add_actions(actions)
        self._archive_current_session()
        self._refresh_history()
        self._set_status("回复完成", "ok")

    def _chat_failed(self, message, prompt):
        msg = getattr(self, "_agent_msg", None)
        if msg is not None:
            msg.set_status("")
            msg.add_note(f"请求失败：{message}", tone="error")
        area = getattr(self, "chat_area", None)
        if area is not None:
            area.add_notice(message, tone="error", title="gateway 请求失败")
        if not self.send_var.get():
            self.send_var.set(prompt)
        self._set_status("请求失败，消息已保留；检查 gateway 后可重试", "error")

    def _stop_send(self):
        """请求中止当前流式回复。

        urllib 没有暴露打断点，所以在工作线程侧用一个标志位：下一次 chunk
        回调时抛异常退出。这里先即时反馈状态。
        """
        if not self._sending:
            return
        self._abort_requested = True
        self._set_request_status("正在停止…")
        self._set_status("已请求停止——当前这轮回复会在下一个数据块后结束", "warn")
        self._refresh_send_circle()

    def _send_finished(self):
        self._sending = False
        self._abort_requested = False
        try:
            self.input_card.set_busy(False)
        except Exception:
            pass
        self.clear_chat_btn.configure(state=tk.NORMAL)
        self.gw_btn.configure(state=tk.NORMAL)
        if not self.gateway_proc:
            self.port_spin.configure(state=tk.NORMAL)
        self._set_request_status("空闲")
        self._refresh_send_circle()
    # ── 关闭 ──
    def _on_close(self):
        if (self._feature_dirty or self._editor_is_dirty() or self._pending_rows) and not messagebox.askyesno(
                "有未保存的修改", "功能开关或编辑内容尚未保存。要放弃这些修改并退出吗？", parent=self.root):
            return
        self._closing = True
        self._archive_current_session()
        try:
            self.root.after_cancel(self._event_poll)
        except (tk.TclError, ValueError):
            pass
        if self._sysmon is not None:
            try:
                self._sysmon.stop()
            except Exception:
                pass
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
