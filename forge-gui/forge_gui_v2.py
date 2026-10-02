"""forge 图形界面 v3：Agent-native 四层桌面工作区。

设计结构：
    · 顶栏：品牌 / 当前项目 / 必要运行状态；详细 telemetry 按需展开
    · Activity Bar：窄导航轨，只表达一级位置
    · Sidebar：只显示当前一级功能的上下文
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
import shutil
import socket
import subprocess
import sys
import threading
import tempfile
import time
import tkinter as tk
from ui_icons import IconCanvas, IconButton, icon_image
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
    GenerationCancelled,
)
from interaction_model import (read_attachment, compose_prompt, task_command, task_outcome,
                               select_provider, gateway_settings)
from interaction_model import model_label as served_model_label
import sub_agent as team   # 子 Agent 分工 + Agent 集群（移植自 AI Platform）

import chat_widgets as cw  # noqa: E402
import brand_marks
import provider_catalog as catalog  # noqa: E402
import gui_theme as theme  # noqa: E402
from model_picker import ModelPicker  # noqa: E402
from gui_theme import (  # noqa: E402
    C,
    FONT_MONO_XS,
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
    divider,
    dot,
    emoji_font,
    glyph_button,
    pill_button,
    progress_bar,
    round_rect,
    rounded_label,
    rounded_points,
    show_popover_menu,
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
MIN_SIZE = (940, 700)
ACTIVITY_WIDTH = 152
SIDEBAR_WIDTH = 220
SIDEBAR_COLLAPSE_AT = 1240
WORKSPACE_COLLAPSE_AT = 1080
WORKSPACE_RESTORE_AT = 1320
FORGE_REPO_HINT = os.environ.get("FORGE_REPO", "").strip()
DEFAULT_FORGE_HOME = Path.home() / ".forge"

# 打包成桌面应用（PyInstaller）后：__file__ 指向临时解包目录，
# 资源与配置要相对「exe 所在目录」定位。
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else HERE
DESKTOP_CONFIG_NAME = "forge-desktop.json"

# 自动启动/自恢复的退避节奏（秒）。原实现只重试一次都没有，这里补上。
AUTOSTART_RETRY_DELAYS = (1.5, 3.0, 6.0, 12.0, 20.0)

IS_WINDOWS = platform.system() == "Windows"

# 主导航（顶栏与侧栏共用；key -> (标签, 图标)）
# Navigation icons are semantic vectors, independent of the system emoji font.
NAV_ITEMS = [
    ("chat", "对话", "💬"),
    ("task", "任务", "✅"),
    ("agents", "Agents", "🤖"),
    ("tools", "工具集", "🧰"),
    ("knowledge", "知识库", "📚"),
    ("evolution", "演化", "🧬"),
    ("files", "文件与项目", "📁"),
    ("config", "配置", "⚙️"),
]
NAV_LABEL = {key: label for key, label, _g in NAV_ITEMS}
NAV_GLYPH = {key: glyph for key, _l, glyph in NAV_ITEMS}
# 主轨道只呈现已经可以完成工作的视图。仍保留旧视图及其调用入口，
# 但将规划中页面降到「更多」，避免把占位页冒充成正式产品能力。
PRIMARY_NAV = {"chat", "task", "tools", "config"}


class ActivityGlyph(tk.Canvas):
    """统一 20px 线性导航图标；避免系统字形/emoji 在不同机器上跳变。"""

    def __init__(self, parent, key: str, command, *, bg: str, fg: str):
        super().__init__(parent, width=22, height=22, bg=bg, bd=0,
                         highlightthickness=0, cursor="hand2")
        self._key, self._fg, self._command = key, fg, command
        self.bind("<Button-1>", lambda _e: self._command())
        self._draw()

    def configure(self, cnf=None, **kw):
        fg = kw.pop("fg", None)
        kw.pop("activebackground", None)
        kw.pop("activeforeground", None)
        if fg is not None:
            self._fg = fg
        result = super().configure(cnf, **kw) if cnf is not None else super().configure(**kw)
        self._draw()
        return result

    config = configure

    def _draw(self):
        self.delete("all")
        c, w = self._fg, 1.5
        if self._key == "chat":
            self.create_rectangle(4, 4, 18, 16, outline=c, width=w)
            self.create_line(8, 16, 6, 19, 12, 16, fill=c, width=w)
        elif self._key == "task":
            self.create_rectangle(4, 4, 18, 18, outline=c, width=w)
            self.create_line(7, 11, 10, 14, 16, 7, fill=c, width=w)
        elif self._key == "tools":
            for y, x in ((6, 9), (11, 14), (16, 7)):
                self.create_line(4, y, 18, y, fill=c, width=w)
                self.create_oval(x - 2, y - 2, x + 2, y + 2, outline=c, width=w)
        elif self._key == "config":
            self.create_oval(6, 6, 16, 16, outline=c, width=w)
            self.create_oval(9, 9, 13, 13, outline=c, width=w)
            for x1, y1, x2, y2 in ((11, 3, 11, 6), (11, 16, 11, 19),
                                   (3, 11, 6, 11), (16, 11, 19, 11)):
                self.create_line(x1, y1, x2, y2, fill=c, width=w)
        elif self._key == "files":
            self.create_polygon(3, 7, 9, 7, 11, 9, 19, 9, 19, 18, 3, 18,
                                outline=c, fill="", width=w)
        elif self._key == "agents":
            for x, y in ((6, 7), (16, 7), (11, 16)):
                self.create_oval(x - 2, y - 2, x + 2, y + 2, outline=c, width=w)
            self.create_line(8, 8, 10, 14, 14, 8, fill=c, width=w)
        else:
            self.create_oval(4, 4, 18, 18, outline=c, width=w)
            self.create_line(7, 11, 15, 11, fill=c, width=w)

# 沉思模式（forge thinking.mode 三档；GUI 里对齐参考稿输入卡的工具条）
THINKING_LABELS = {"off": "关闭", "smart": "智能", "on": "开启"}
THINKING_CHOICES = [
    ("off", "关闭", "不启用沉思"),
    ("smart", "智能", "按任务复杂度自动决定（推荐）"),
    ("on", "开启", "始终启用沉思"),
]

# Composer 的统一模型设置：前四档透传给支持 reasoning_effort 的模型；
# 末档同时打开 Forge 任务沉思。默认不发送额外推理强度。
REASONING_CHOICES = [
    ("off", "Light", "快速回答，不发送额外推理强度"),
    ("low", "Standard", "简短推理，适合直接问题"),
    ("medium", "Deep", "在速度与推理深度之间平衡"),
    ("high", "Intense", "更充分地分析复杂问题"),
    ("contemplate", "Scrutiny", "高强度推理，并启用 Forge 任务沉思"),
]
REASONING_LABELS = {value: label for value, label, _hint in REASONING_CHOICES}

# 任务视图的策略档位（对应 forge run 的 routing strategy）
# 旧名 economy/balanced 仍被核心识别（routing.LEGACY_STRATEGIES），但 GUI 只写新名。
STRATEGY_CHOICES = [
    ("base", "Base", "成本优先，失败时可升级"),
    ("medium", "Medium", "默认：按任务难度选模型"),
    ("premium", "Premium", "优先用最强模型"),
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


def _desktop_config_path() -> Path:
    """桌面应用的本地配置（记住 Forge 目录等）：优先 exe 同目录，否则 ~/.forge。"""
    try:
        if os.access(str(APP_DIR), os.W_OK):
            return APP_DIR / DESKTOP_CONFIG_NAME
    except OSError:
        pass
    DEFAULT_FORGE_HOME.mkdir(parents=True, exist_ok=True)
    return DEFAULT_FORGE_HOME / DESKTOP_CONFIG_NAME


def load_desktop_config() -> dict:
    path = _desktop_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_desktop_config(**updates) -> None:
    path = _desktop_config_path()
    data = load_desktop_config()
    data.update(updates)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass


def _autostart_enabled() -> bool:
    """gateway 是否在打开窗口时自动启动（默认开；FORGE_NO_AUTOSTART=1 关闭）。

    独立成函数是为了让测试能 mock 掉，避免测试里真起 gateway 子进程。
    """
    return os.environ.get("FORGE_NO_AUTOSTART", "").strip() not in (
        "1", "true", "yes", "on")


def split_file_ref(ref: str) -> tuple[str, int | None]:
    """把聊天里的文件引用拆成 (路径, 行号)。

    支持：`forge/loop.py`、`forge/loop.py:120`、`forge/loop.py:120-140`、
    markdown 链接 `[forge/loop.py](forge/loop.py)`（取括号里的目标）。
    """
    text = (ref or "").strip()
    if text.startswith("[") and "](" in text and text.endswith(")"):
        text = text[text.index("](") + 2:-1].strip()
    line: int | None = None
    head, sep, tail = text.rpartition(":")
    if sep and head and tail:
        first = tail.split("-", 1)[0].strip()
        if first.isdigit():
            text = head
            line = int(first)
    return text, line


def relative_to_repo(ws, path: str) -> str | None:
    """把路径换算成「相对工作区仓库根」的正斜杠路径；失败返回 None。"""
    try:
        root = None
        summary_fn = getattr(ws, "workspace_summary", None)
        if callable(summary_fn):
            root = summary_fn().get("repo")
        if not root:
            root = str(getattr(ws, "_repo_root", "") or "")
        if not root:
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        resolved = candidate.resolve()
        return str(resolved.relative_to(Path(root).resolve())).replace("\\", "/")
    except Exception:
        return None


def _asset_dirs() -> list[Path]:
    """界面资源可能所在目录（打包后在 _MEIPASS，源码态在模块旁）。"""
    dirs: list[Path] = []
    if FROZEN:
        base = Path(getattr(sys, "_MEIPASS", ""))
        if str(base):
            dirs.append(base / "assets")
    dirs.append(HERE / "assets")
    dirs.append(APP_DIR / "assets")
    return dirs


def _asset_path(name: str) -> Path | None:
    for d in _asset_dirs():
        try:
            p = d / name
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    """快速判断端口是否已被监听（微秒级，不启子进程）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def kill_process_tree(proc, timeout: float = 5.0) -> bool:
    """杀掉进程及其全部子进程。

    必须杀树：gateway 会派生子进程，只 kill 父进程会让子进程逃逸并占住端口，
    下次启动就会「端口被占用」——而且窗口关了服务还在后台跑。

    返回进程是否已经退出。Windows 的 taskkill 即使失败也只会给非零退出码，
    不会抛异常，所以必须检查 returncode，并继续走 Popen 的 terminate/kill 回退。
    """
    if proc is None:
        return True
    try:
        alive = proc.poll() is None
    except Exception:
        return False
    if not alive:
        return True
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False)
            if result.returncode == 0:
                try:
                    proc.wait(timeout=min(2.0, max(0.2, timeout)))
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if proc.poll() is not None:
                    return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    # 非 Windows、taskkill 失败或 taskkill 返回后进程仍在：先温和后强硬。
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
            return proc.poll() is not None
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return proc.poll() is not None
    except Exception:
        return False


def load_brand_logo(target_px: int, master=None):
    """加载品牌标志为 tk.PhotoImage（无 PIL 依赖：只用预生成 PNG + 整数缩放）。

    返回 (image, keep_alive)；找不到资源时返回 (None, None)，调用方回退到程序化绘制。
    """
    # 预生成尺寸：52 / 78 / 104；挑最接近的整数倍（zoom 放大、subsample 缩小）
    for size in (104, 78, 52):
        path = _asset_path(f"forge-logo-{size}.png")
        if path is None:
            continue
        try:
            img = tk.PhotoImage(master=master, file=str(path))
        except tk.TclError:
            continue
        if size >= target_px:
            factor = max(1, round(size / target_px))
            if factor > 1:
                try:
                    img = img.subsample(factor, factor)
                except tk.TclError:
                    pass
        else:
            factor = max(1, round(target_px / size))
            if factor > 1:
                try:
                    img = img.zoom(factor, factor)
                except tk.TclError:
                    pass
        return img, img
    return None, None


def _python_exe() -> str:
    """跑 run.py 用的解释器。

    打包后 sys.executable 是 GUI 自己（再带上 run.py 参数会又开一个窗口），
    所以必须另找一个真正的 Python：FORGE_PYTHON → py launcher → PATH →
    常见安装目录。
    """
    if not FROZEN and sys.executable:
        return sys.executable
    # 1) 显式指定优先
    env_py = os.environ.get("FORGE_PYTHON", "").strip()
    if env_py and Path(env_py).is_file():
        return env_py
    # 2) 用户自己装的 Python（比 PATH 更可靠——PATH 上可能挂着别的内嵌解释器）
    for base in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python",
                 Path("C:/"),
                 Path(os.environ.get("ProgramFiles", "C:/Program Files"))):
        try:
            if not base.is_dir():
                continue
            for sub in sorted(base.glob("Python3*"), reverse=True):
                for name in ("python.exe", "python3.exe"):
                    exe = sub / name
                    if exe.is_file():
                        return str(exe)
        except OSError:
            continue
    # 3) PATH
    for cand in ("python", "python3"):
        found = shutil.which(cand)
        if found:
            return found
    return "python"


def _find_run_py() -> Path | None:
    """探测 run.py。

    顺序：env FORGE_REPO → 桌面配置记住的目录 → exe/脚本同级与上层 →
    常见位置（用户目录下的 pledge-evolving 等）→ .openclaw/tmp 副本。
    """
    if FORGE_REPO_HINT:
        p = Path(FORGE_REPO_HINT) / "run.py"
        if p.is_file():
            return p
    saved = str(load_desktop_config().get("forge_repo", "")).strip()
    if saved:
        p = Path(saved) / "run.py"
        if p.is_file():
            return p
    for base in (APP_DIR, HERE):
        p = base / "run.py"
        if p.is_file():
            return p
        for parent in (base.parent, base.parent.parent, base.parent.parent.parent):
            p = parent / "run.py"
            if p.is_file():
                return p
    home = Path.home()
    for name in ("pledge-evolving", "forge", "forge-repo"):
        p = home / name / "run.py"
        if p.is_file():
            return p
    try:
        for candidate in (home / ".openclaw-autoclaw").glob("forge-*/run.py"):
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
        desktop_prefs = load_desktop_config()
        self._model_favorites = [str(v) for v in desktop_prefs.get("model_favorites", [])
                                 if str(v).strip()]
        self._reasoning_effort = str(desktop_prefs.get("reasoning_effort", ""))
        try:
            self.user_rows: list[dict] = load_user_layer(self.home)
        except (OSError, ValueError) as exc:
            self.user_rows = []
            self._load_error = str(exc)
        self.gateway_proc: subprocess.Popen | None = None
        # 常驻语义：打开即自启；掉了自动拉起；用户主动停才不复起
        self._gateway_user_stopped = False
        self._gateway_restart_times: list[float] = []
        self._gateway_autostarted = False
        self._autostart_after_id = None
        self._autostart_attempts = 0
        self._autostart_log_path = DEFAULT_FORGE_HOME / "gui" / "autostart.log"
        self._restart_pending = False
        self._suppress_model_trace = False
        self.gateway_port = 8799
        self.gateway_url = f"http://127.0.0.1:{self.gateway_port}"
        self.client = ForgeGatewayClient(self.gateway_url)

        # 矫治后待保存的内容
        self._pending_rows: list[dict] = []
        self._organized_input = ""
        self._editor_clean_text = ""
        self._chat_history: list[ChatMessage] = []
        self._attachments = []
        self._include_history = True
        self._session_custom_title = ""
        self._cancel_event = threading.Event()
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
        self._set_status(self._load_error or "就绪",
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
        self._activity_labels: dict[str, list] = {}
        self._views: dict[str, tk.Frame] = {}
        self._active_view = "chat"
        self._ws_packed = False
        self._last_workspace_tab = "file_tree"
        self._workspace_auto_hidden = False
        self._sidebar_visible = True
        self._sidebar_user_hidden = False
        self._sidebar_auto_hidden = False
        self._sidebar_force_open = False
        self._responsive_after_id = None
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
        self.split.add(center, minsize=500, stretch="always")

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
        bot.pack_configure(before=body)

        try:
            cw.set_file_link_handler(self._open_file_ref)
        except AttributeError:
            pass
        self._show_view("chat")
        self._refresh_history()
        self._start_sysmon()
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self.root.bind("<Destroy>", self._cancel_responsive_callback, add="+")
        self.root.bind("<Control-n>", lambda _e: self._conversation_shortcut("new"))
        self.root.bind("<Control-k>", lambda _e: self._conversation_shortcut("search"))
        self.root.bind("<Control-l>", lambda _e: self._conversation_shortcut("input"))
        # 像 AutoClaw 一样：打开窗口就把 gateway 拉起来（等 UI 建好再起，
        # 免得抢启动时间、也免得状态栏还没就绪）。
        # FORGE_NO_AUTOSTART=1 可关闭（测试用）。
        # 切模型 → 必要时换 provider（gateway 一次只服务一个上游）
        self.model_var.trace_add("write", self._on_model_changed)
        if _autostart_enabled():
            # 先把状态标成「启动中」：打包版解包要几秒，这段时间界面不能看着像没反应。
            self.gw_status_var.set("● 启动中…")
            self._autostart_log("窗口打开，准备自动拉起 gateway")
            self._autostart_after_id = self.root.after(250, self._autostart_gateway)

    # ── 顶栏 ──────────────────────────────────────────────
    def _build_topbar(self):
        # 品牌头像（对话消息里的 Forge 头像）：注入给 chat_widgets
        try:
            av_img, av_keep = load_brand_logo(30, master=self.root)
            if av_img is not None:
                cw.set_brand_avatar(av_img, av_keep)
        except Exception:
            pass
        chrome = tk.Frame(self.root, bg=C["bg"])
        chrome.pack(fill=tk.X)
        self._chrome = chrome
        bar = tk.Frame(chrome, bg=C["bg"], height=50)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        # 品牌区
        brand = tk.Frame(bar, bg=C["bg"])
        brand.pack(side=tk.LEFT, padx=(14, 12))
        brand_img, brand_keep = load_brand_logo(24, master=self.root)
        if brand_img is not None:
            holder = tk.Frame(brand, bg=C["bg"])
            holder.pack(side=tk.LEFT, padx=(0, 8))
            shown = tk.Label(holder, image=brand_img, bg=C["bg"], bd=0,
                             highlightthickness=0)
            shown.pack()
            self._brand_logo_refs = (brand_img, brand_keep, holder, shown)
        else:
            logo = tk.Canvas(brand, width=24, height=24, bg=C["bg"],
                             highlightthickness=0, bd=0)
            round_rect(logo, 0, 0, 23, 23, 7, fill=C["accent"], outline="")
            logo.create_polygon(2, 2, 18, 2, 2, 18, smooth=True, splinesteps=10,
                                fill="#6D63F0", outline="")
            logo.create_text(12, 12, text="F", fill="#FFFFFF",
                             font=(theme.UI_FAMILY, 11, "bold"))
            logo.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(brand, text="FORGE", bg=C["bg"], fg=C["text"],
                 font=(theme.UI_FAMILY, 12, "bold")).pack(side=tk.LEFT)

        self._project_chip = tk.Frame(bar, bg=C["surface2"], padx=9, pady=4)
        self._project_chip.pack(side=tk.LEFT, padx=(8, 0))
        IconCanvas(self._project_chip, "files", size=18, bg=C["surface2"], fg=C["accent2"]).pack(
            side=tk.LEFT, padx=(0, 5))
        self._project_name_var = tk.StringVar(value=self._repo_root().name)
        tk.Label(self._project_chip, textvariable=self._project_name_var,
                 bg=C["surface2"], fg=C["subtext"], font=FONT_SMALL).pack(side=tk.LEFT)

        # 保留「更多」能力合约，可见入口移到 Activity Bar 底部。
        self._more_menu = tk.Menu(self.root, tearoff=0, bg=C["surface"],
                                  fg=C["text"], activebackground=C["accent_soft"],
                                  activeforeground=C["accent_text"],
                                  font=FONT_SMALL, bd=1, relief=tk.FLAT)
        self._more_menu.add_command(label="状态详情",
                                    command=self._toggle_telemetry)
        self._more_menu.add_command(label="收起 / 展开侧边栏",
                                    command=self._toggle_sidebar)
        self._more_menu.add_separator()
        self._more_menu.add_command(label="项目文件", command=lambda: self._nav_click("files"))
        self._more_menu.add_separator()
        for key in ("agents", "knowledge", "evolution"):
            self._more_menu.add_command(
                label=f"{NAV_LABEL[key]}（预览）", command=lambda k=key: self._show_view(k))
        # 右侧只保留 Workspace 与必要运行状态。
        right = tk.Frame(bar, bg=C["bg"])
        right.pack(side=tk.RIGHT, padx=(0, 14))
        self.ws_toggle_btn = pill_button(right, "▤ 工作区", self._toggle_workspace,
                                         kind="quiet", bg=C["bg"], font=FONT_SMALL,
                                         padx=10)
        self.ws_toggle_btn.pack(side=tk.LEFT, padx=(0, 10))
        self.gw_status_var = tk.StringVar(value="● 离线")
        self.gw_status_lbl = tk.Label(right, textvariable=self.gw_status_var,
                                      bg=C["bg"], fg=C["muted"], font=FONT_CAPTION)
        self.gw_status_lbl.pack(side=tk.LEFT, padx=(0, 6))
        self._telemetry_btn = pill_button(right, "▾ 状态", self._toggle_telemetry,
                                          kind="quiet", bg=C["bg"],
                                          font=FONT_CAPTION, padx=7)
        self._telemetry_btn.pack(side=tk.LEFT)

        # 详细 telemetry 默认收起，但 Gateway 控制和资源读数仍保留。
        self._telemetry_panel = tk.Frame(chrome, bg=C["sidebar"], height=38)
        self._telemetry_panel.pack_propagate(False)
        detail = tk.Frame(self._telemetry_panel, bg=C["sidebar"])
        detail.pack(side=tk.RIGHT, padx=14)
        self._build_gateway_card(detail)
        for key, text in (("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM")):
            self._build_metric(detail, key, text)
        self._telemetry_expanded = False
        def fit_topbar(event):
            if event.width < 1180:
                self._project_chip.pack_forget()
            elif not self._project_chip.winfo_manager():
                self._project_chip.pack(side=tk.LEFT, padx=(8, 0), after=brand)
        bar.bind("<Configure>", fit_topbar)

        self._topbar_divider = tk.Frame(chrome, bg=C["border"], height=1)
        self._topbar_divider.pack(fill=tk.X)

    def _toggle_telemetry(self):
        self._telemetry_expanded = not self._telemetry_expanded
        if self._telemetry_expanded:
            self._telemetry_panel.pack(fill=tk.X, before=self._topbar_divider)
            self._telemetry_btn.configure(text="▴ 状态")
        else:
            self._telemetry_panel.pack_forget()
            self._telemetry_btn.configure(text="▾ 状态")

    def _build_metric(self, parent, key: str, text: str):
        base = parent.cget("bg")
        box = tk.Frame(parent, bg=base)
        box.pack(side=tk.LEFT, padx=(0, 12))
        row = tk.Frame(box, bg=base)
        row.pack(anchor=tk.W)
        tk.Label(row, text=text, bg=base, fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        value = tk.Label(row, text="—", bg=base, fg=C["ter"], font=FONT_MICRO)
        value.pack(side=tk.LEFT, padx=(4, 0))
        self._metric_labels[key] = value
        bar = progress_bar(box, 0, width=22, height=2)
        bar.pack(anchor=tk.W, pady=(2, 0))
        self._metric_bars[key] = bar

    def _set_bar(self, canvas: tk.Canvas, pct):
        try:
            canvas.delete("all")
            w, h = int(canvas.cget("width")), int(canvas.cget("height"))
            round_rect(canvas, 0, 0, w, h, h / 2, fill=C["border_hi"], outline="")
            if pct:
                filled = max(2, int(w * max(0.0, min(100.0, float(pct))) / 100.0))
                round_rect(canvas, 0, 0, filled, h, h / 2, fill=C["border_hi"],
                           outline="")
        except (tk.TclError, ValueError):
            pass

    def _build_gateway_card(self, parent):
        # 降权：展开状态带里的一小块，不单独占一张卡。
        base = parent.cget("bg")
        card = tk.Frame(parent, bg=base)
        card.pack(side=tk.LEFT, padx=(0, 10))
        self.gw_detail_status_lbl = tk.Label(card, textvariable=self.gw_status_var,
                                             bg=base, fg=C["muted"],
                                             font=FONT_MICRO)
        self.gw_detail_status_lbl.pack(side=tk.LEFT, padx=(0, 5))
        tk.Label(card, text="Gateway", bg=base, fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.gateway_port))
        self.port_spin = tk.Spinbox(card, from_=1024, to_=65535, width=5,
                                    textvariable=self.port_var, font=FONT_MONO_XS,
                                    bg=base, fg=C["ter"], bd=0,
                                    buttonbackground=base, relief=tk.FLAT,
                                    insertbackground=C["accent"],
                                    highlightthickness=0, justify=tk.CENTER)
        self.port_spin.pack(side=tk.LEFT, padx=(4, 8))
        self.gw_btn = IconButton(card, text="▶ 启动", command=self._toggle_gateway,
                                bg=base, fg=C["accent_text"],
                                activebackground=C["hover"],
                                activeforeground=C["text"], font=FONT_MICRO,
                                relief=tk.FLAT, bd=0, padx=7, pady=1,
                                cursor="hand2", highlightthickness=0)
        self.gw_btn.pack(side=tk.LEFT)
        # 常驻语义下主按钮=确保运行；停止放在右键菜单里
        self._gw_menu = tk.Menu(self.root, tearoff=0, bg=C["surface"],
                                fg=C["text"], activebackground=C["accent_soft"],
                                activeforeground=C["accent_text"],
                                font=FONT_SMALL, bd=1, relief=tk.FLAT)
        self._gw_menu.add_command(label="重启 gateway", image=icon_image(self.root, "refresh"), compound=tk.LEFT,
                                  command=self._toggle_gateway)
        self._gw_menu.add_separator()
        self._gw_menu.add_command(label="停止 gateway", image=icon_image(self.root, "stop"), compound=tk.LEFT,
                                  command=self._stop_gateway_from_menu)
        self.gw_btn.bind("<Button-3>", self._popup_gw_menu)
        attach_tooltip(self.gw_btn, "打开即自动启动；左键重启，右键可停止")

    # ── 左侧栏 ────────────────────────────────────────────
    def _make_activity_item(self, parent, key: str, label: str, glyph: str):
        """Activity Bar 的单一一级入口：图标 + 名称。

        以前只放图标、名称塞进 tooltip，结果是“只有图标没有名字”，
        新用户根本不知道每个入口是什么。现在图标与名称同屏。
        """
        holder = tk.Frame(parent, bg=C["activity"], width=ACTIVITY_WIDTH,
                          height=38)
        holder.pack_propagate(False)
        marker = tk.Frame(holder, bg=C["activity"], width=2)
        marker.pack(side=tk.LEFT, fill=tk.Y)
        inner = tk.Frame(holder, bg=C["activity"])
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(9, 6))
        icon = IconCanvas(inner, key, size=20, bg=C["activity"], fg=C["ter"])
        icon.pack(side=tk.LEFT)
        text = tk.Label(inner, text=label, bg=C["activity"], fg=C["ter"],
                        font=FONT_SMALL, anchor="w")
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        attach_tooltip(icon, label)
        attach_tooltip(text, label)
        for widget in (holder, inner, icon, text):
            widget.bind("<Button-1>", lambda _e, k=key: self._nav_click(k))
            widget.bind("<Enter>",
                        lambda _e, k=key: self._paint_activity(k, hover=True))
            widget.bind("<Leave>", lambda _e, k=key: self._paint_activity(k))
            widget.configure(cursor="hand2")
        self._nav_widgets.setdefault(key, []).append((holder, inner))
        self._activity_labels.setdefault(key, []).append((icon, text))
        self._activity_markers[key] = marker
        return holder

    def _paint_activity(self, key, *, hover=False):
        active = key == getattr(self, "_active_nav", "chat")
        bg = C["sel"] if active else C["hover"] if hover else C["sidebar"]
        fg = C["text"] if active or hover else C["ter"]
        for holder, inner in self._nav_widgets.get(key, []):
            holder.configure(bg=bg)
            inner.configure(bg=bg)
        for icon, text in self._activity_labels.get(key, []):
            icon.configure(bg=bg, fg=fg)
            text.configure(bg=bg, fg=fg)

    def _build_sidebar(self, parent):
        # 概念图：单栏侧栏（无窄轨道）。rail/Activity Bar 已移除，
        # 但保留 self.activity_bar 与两个 dict 引用（其他方法仍会读）。
        rail = tk.Frame(parent, bg=C["sidebar"], width=0)
        self.activity_bar = rail          # 保留引用，不 pack（宽度 0）
        self._activity_markers: dict[str, tk.Frame] = {}
        self._activity_labels: dict[str, list] = {}

        side = tk.Frame(parent, bg=C["sidebar"], width=SIDEBAR_WIDTH)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        self.sidebar = side

        self._sidebar_panels: dict[str, tk.Frame] = {}
        chat_panel = tk.Frame(side, bg=C["sidebar"])
        self._sidebar_panels["chat"] = chat_panel

        # ── 1) 顶部：＋ 新建对话 ──
        new_btn = pill_button(chat_panel, "＋  新建对话", self._new_session,
                              kind="quiet", bg=C["sidebar"], font=FONT_UI, padx=0)
        new_btn.pack(fill=tk.X, padx=12, pady=(14, 10))

        # ── 2) 主导航（概念图只展示 3 项，其余入口走「⋯ 更多」）──
        nav_host = tk.Frame(chat_panel, bg=C["sidebar"])
        nav_host.pack(fill=tk.X, padx=6, pady=(0, 4))
        for key in ("chat", "task", "tools"):
            holder = self._make_side_nav(nav_host, key, NAV_LABEL[key],
                                         NAV_GLYPH[key])
            holder.pack(fill=tk.X, pady=1)
        # 更多入口：Agents / 知识库 / 演化 / 文件与项目 / 配置
        more_holder = self._make_side_nav_more(chat_panel)
        more_holder.pack(fill=tk.X, padx=6, pady=(2, 6))

        divider(chat_panel, bg=C["border"]).pack(fill=tk.X, padx=12, pady=(4, 2))

        # ── 3) 智能体分组（扫描 ~/.openclaw-autoclaw/agents/）──
        self._build_agents_group(chat_panel)

        divider(chat_panel, bg=C["border"]).pack(fill=tk.X, padx=12, pady=(2, 2))

        # ── 4) 最近对话（时间分组）──
        head = tk.Frame(chat_panel, bg=C["sidebar"])
        head.pack(fill=tk.X, padx=14, pady=(8, 4))
        tk.Label(head, text="最近对话", bg=C["sidebar"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        glyph_button(head, "搜索", self._toggle_session_search, bg=C["sidebar"],
                     fg=C["muted"], size=9, tooltip="搜索对话 · Ctrl+K").pack(
            side=tk.RIGHT)
        self._search_visible = False
        self.session_search_var = tk.StringVar()
        self.session_search = tk.Entry(
            chat_panel, textvariable=self.session_search_var,
            bg=C["surface2"], fg=C["text"], bd=0, relief=tk.FLAT,
            insertbackground=C["accent"], font=FONT_SMALL, highlightthickness=1,
            highlightbackground=C["border_hi"], highlightcolor=C["accent"])
        self.session_search_var.trace_add("write", lambda *_: self._refresh_history())

        self.history_area = cw.ScrollArea(chat_panel, bg=C["sidebar"], pady=2)
        self.history_area.pack(fill=tk.BOTH, expand=True, padx=6)
        self.history_box = self.history_area.inner

        # ── 5) 底部固定：模型选择 + 设置 + 关于 + 收起 ──
        divider(chat_panel, bg=C["border"]).pack(fill=tk.X, padx=12, side=tk.BOTTOM)
        bottom = tk.Frame(chat_panel, bg=C["sidebar"])
        bottom.pack(fill=tk.X, padx=10, pady=(6, 8), side=tk.BOTTOM)

        model_row = tk.Frame(bottom, bg=C["sidebar"])
        model_row.pack(fill=tk.X, pady=(0, 6))
        # 模型 pill：点击聚焦输入卡的模型下拉（真控件只有一处，避免两处不同步）
        self.side_model_pill = pill_button(
            model_row, f"▣ {self.model_var.get()}" if hasattr(self, "model_var")
            else "▣ default",
            self._open_model_menu, kind="quiet", bg=C["surface2"],
            font=FONT_SMALL, padx=8)
        self.side_model_pill.pack(fill=tk.X)
        attach_tooltip(self.side_model_pill, "当前模型 · 点击切换")

        tools_row = tk.Frame(bottom, bg=C["sidebar"])
        tools_row.pack(fill=tk.X)
        pill_button(tools_row, "⚙ 设置", lambda: self._show_view("config"),
                    kind="quiet", bg=C["sidebar"], font=FONT_MICRO,
                    padx=8).pack(side=tk.LEFT, fill=tk.X, expand=True)
        pill_button(tools_row, "ⓘ 关于", self._show_about, kind="quiet",
                    bg=C["sidebar"], font=FONT_MICRO, padx=8).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        self.sidebar_toggle_btn = glyph_button(
            tools_row, "≪", self._toggle_sidebar, bg=C["sidebar"],
            fg=C["muted"], size=11, tooltip="收起 / 展开侧边栏")
        self.sidebar_toggle_btn.pack(side=tk.RIGHT)

        # 其他一级功能各有自己的上下文 Sidebar（切视图时显示）。
        for key, label, glyph in NAV_ITEMS:
            if key == "chat":
                continue
            panel = self._build_context_sidebar(side, key, label, glyph)
            self._sidebar_panels[key] = panel
        chat_panel.pack(fill=tk.BOTH, expand=True)

        # _more_btn 保留引用（_popup_more_menu 读它的坐标）
        self._more_btn = more_holder

    def _make_side_nav(self, parent, key: str, label: str, glyph: str):
        """单栏导航行：图标 + 名称（概念图样式，选中态底色块）。"""
        holder = tk.Frame(parent, bg=C["sidebar"], cursor="hand2")
        inner = tk.Frame(holder, bg=C["sidebar"])
        inner.pack(fill=tk.X, padx=6, pady=1)
        icon = IconCanvas(inner, key, size=20, bg=C["sidebar"], fg=C["ter"])
        icon.pack(side=tk.LEFT)
        text = tk.Label(inner, text=label, bg=C["sidebar"], fg=C["ter"],
                        font=FONT_SMALL, anchor="w")
        text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        attach_tooltip(icon, label)
        attach_tooltip(text, label)
        for widget in (holder, inner, icon, text):
            widget.bind("<Button-1>", lambda _e, k=key: self._nav_click(k))
            widget.bind("<Enter>",
                        lambda _e, k=key: self._paint_activity(k, hover=True))
            widget.bind("<Leave>", lambda _e, k=key: self._paint_activity(k))
            widget.configure(cursor="hand2")
        self._nav_widgets.setdefault(key, []).append((holder, inner))
        self._activity_labels.setdefault(key, []).append((icon, text))
        # 选中态 marker：与 _set_nav_active 兼容
        marker = tk.Frame(holder, bg=C["sidebar"], width=2)
        marker.pack(side=tk.LEFT, fill=tk.Y)
        self._activity_markers[key] = marker
        return holder

    def _make_side_nav_more(self, parent):
        """「⋯ 更多」行：打开菜单（Agents / 知识库 / 演化 / 文件与项目 / 配置）。"""
        holder = tk.Frame(parent, bg=C["sidebar"], cursor="hand2")
        inner = tk.Frame(holder, bg=C["sidebar"])
        inner.pack(fill=tk.X, padx=6, pady=1)
        icon = IconCanvas(inner, "more", size=20, bg=C["sidebar"], fg=C["muted"])
        icon.pack(side=tk.LEFT)
        text = tk.Label(inner, text="更多", bg=C["sidebar"], fg=C["muted"],
                        font=FONT_SMALL, anchor="w")
        text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        attach_tooltip(text, "Agents / 知识库 / 演化 / 文件与项目 / 配置")

        def popup(_event=None):
            menu = getattr(self, "_more_menu", None)
            if menu is None:
                return
            try:
                x = holder.winfo_rootx()
                y = holder.winfo_rooty() + holder.winfo_height()
                menu.tk_popup(x, y)
            finally:
                try:
                    menu.grab_release()
                except tk.TclError:
                    pass
        for widget in (holder, inner, icon, text):
            widget.bind("<Button-1>", popup)
            widget.configure(cursor="hand2")
        return holder

    def _build_agents_group(self, parent):
        """侧栏「智能体」分组：扫描 agents 目录列出行；+ 添加智能体。"""
        section = tk.Frame(parent, bg=C["sidebar"])
        section.pack(fill=tk.X, padx=14, pady=(6, 4))
        tk.Label(section, text="智能体", bg=C["sidebar"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        plus = IconCanvas(section, "plus", size=16, bg=C["sidebar"], fg=C["subtext"],
                          command=lambda: self._prompt_create_agent())
        plus.pack(side=tk.RIGHT)
        plus.bind("<Button-1>", lambda _e: self._prompt_create_agent())
        attach_tooltip(plus, "添加智能体")

        list_box = tk.Frame(parent, bg=C["sidebar"])
        list_box.pack(fill=tk.X, padx=6)
        self._agents_list_frame = list_box

        agents_root = Path.home() / ".openclaw-autoclaw" / "agents"
        try:
            children = sorted([q for q in agents_root.iterdir() if q.is_dir()],
                              key=lambda q: q.name.lower())
        except OSError:
            children = []
        shown = children[:8]
        for q in shown:
            row = tk.Frame(list_box, bg=C["sidebar"], cursor="hand2")
            row.pack(fill=tk.X, pady=1, padx=2)
            IconCanvas(row, "model", size=18, bg=C["sidebar"], fg=C["accent2"]).pack(
                side=tk.LEFT, padx=(8, 6))
            tk.Label(row, text=q.name, bg=C["sidebar"], fg=C["subtext"],
                     font=FONT_SMALL, anchor="w").pack(side=tk.LEFT, pady=3,
                                                       fill=tk.X, expand=True)
            attach_tooltip(row, str(q))
            for w in (row, *row.winfo_children()):
                w.bind("<Button-1>",
                       lambda _e, path=str(q): self._show_agent_info(path))
                w.bind("<Enter>", lambda _e, r=row: r.configure(bg=C["hover"]))
                w.bind("<Leave>", lambda _e, r=row: r.configure(bg=C["sidebar"]))
        if len(children) > len(shown):
            more = tk.Label(list_box, text=f"… 还有 {len(children) - len(shown)} 个",
                            bg=C["sidebar"], fg=C["muted"], font=FONT_MICRO,
                            anchor="w", padx=8, cursor="hand2")
            more.pack(fill=tk.X, pady=2)
            more.bind("<Button-1>", lambda _e: self._nav_click("agents"))
        if not children:
            tk.Label(list_box, text="还没有智能体", bg=C["sidebar"],
                     fg=C["muted"], font=FONT_MICRO, anchor="w",
                     padx=8).pack(fill=tk.X, pady=4)
        add = IconButton(list_box, text="＋ 添加智能体", bg=C["sidebar"], fg=C["ter"],
                         font=FONT_MICRO, anchor="w", padx=8, relief=tk.FLAT, bd=0,
                         cursor="hand2", command=lambda: self._prompt_create_agent())
        add.pack(fill=tk.X, pady=(4, 2))

    def _show_about(self):
        try:
            from tkinter import messagebox
            messagebox.showinfo(
                "关于 forge",
                f"forge v{APP_VERSION}\n"
                f"Agent Framework · 深色工作台\n\n"
                f"仓库：{self._repo_root()}\n"
                f"Gateway：{self.gateway_url if self.gateway_proc else '未启动'}",
                parent=self.root)
        except Exception:
            self._set_status(f"forge v{APP_VERSION}", "info")

    def _build_context_sidebar(self, parent, key: str, label: str, glyph: str):
        descriptions = {
            "task": "编排与执行 forge run",
            "agents": "Agent 成员与调度上下文",
            "tools": "当前环境的能力与开关",
            "knowledge": "记忆、策展与检索",
            "evolution": "迭代账本与回归对比",
            "files": "项目文件由 Workspace 承载",
            "config": "Provider、模型与 Gateway 配置",
        }
        panel = tk.Frame(parent, bg=C["sidebar"])
        head = tk.Frame(panel, bg=C["sidebar"])
        head.pack(fill=tk.X, padx=14, pady=(18, 8))
        IconCanvas(head, key, size=22, bg=C["sidebar"], fg=C["accent2"]).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(head, text=label, bg=C["sidebar"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        tk.Label(panel, text=descriptions.get(key, ""), bg=C["sidebar"],
                 fg=C["muted"], font=FONT_SMALL, justify=tk.LEFT,
                 anchor="w", wraplength=SIDEBAR_WIDTH - 28).pack(
            fill=tk.X, padx=14, pady=(0, 18))
        section = tk.Label(panel, text="当前视图", bg=C["sidebar"],
                           fg=C["muted"], font=FONT_MICRO, anchor="w")
        section.pack(fill=tk.X, padx=14, pady=(0, 6))
        row = tk.Frame(panel, bg=C["sel"], padx=10, pady=8)
        row.pack(fill=tk.X, padx=8)
        tk.Label(row, text=label, bg=C["sel"], fg=C["text"],
                 font=FONT_SMALL, anchor="w").pack(fill=tk.X)
        if key == "files":
            pill_button(panel, "打开 Workspace", lambda: self._open_workspace("file_tree"),
                        kind="accent_soft", bg=C["sidebar"]).pack(
                fill=tk.X, padx=12, pady=(18, 0))
        return panel

    def _toggle_session_search(self):
        self._search_visible = not self._search_visible
        if self._search_visible:
            self.session_search.pack(fill=tk.X, padx=12, pady=(0, 6), before=self.history_area)
            self.session_search.focus_set()
        else:
            self.session_search_var.set("")
            self.session_search.pack_forget()
        self._refresh_history()

    def _conversation_shortcut(self, action):
        # 模态窗口或浮层打开时，由当前交互自行处理快捷键。
        if self.root.grab_current() is not None:
            return None
        self._show_view("chat")
        if action == "new":
            self._new_session()
        elif action == "search":
            self._set_sidebar_visible(True)
            if not self._search_visible:
                self._toggle_session_search()
            self.session_search.focus_set()
        else:
            self.input_card.focus_entry()
        return "break"

    def _show_sidebar_for(self, key: str):
        panel = self._sidebar_panels.get(key) or self._sidebar_panels.get("chat")
        for other in self._sidebar_panels.values():
            if other is not panel:
                other.pack_forget()
        if panel is not None and not panel.winfo_manager():
            panel.pack(fill=tk.BOTH, expand=True)

    def _set_sidebar_visible(self, visible: bool, *, automatic=False):
        if visible == self._sidebar_visible:
            return
        if visible:
            self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
            self.sidebar_toggle_btn.configure(text="≪")
        else:
            self.sidebar.pack_forget()
            self.sidebar_toggle_btn.configure(text="≫")
        self._sidebar_visible = visible
        if not automatic:
            self._sidebar_user_hidden = not visible
            self._sidebar_auto_hidden = False

    def _toggle_sidebar(self):
        if self._active_view != "chat":
            self._sidebar_force_open = not self._sidebar_visible
        self._set_sidebar_visible(not self._sidebar_visible)

    def _on_root_configure(self, event):
        if event.widget is not self.root or self._closing:
            return
        if self._responsive_after_id is not None:
            try:
                self.root.after_cancel(self._responsive_after_id)
            except (tk.TclError, ValueError):
                pass
        self._responsive_after_id = self.root.after(100, self._apply_responsive_layout)

    def _cancel_responsive_callback(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self._responsive_after_id is not None:
            try:
                self.root.after_cancel(self._responsive_after_id)
            except (tk.TclError, ValueError):
                pass
            self._responsive_after_id = None

    def _apply_responsive_layout(self):
        self._responsive_after_id = None
        if self._closing or not self.root.winfo_exists():
            return
        width = self.root.winfo_width()
        should_hide_sidebar = (width < SIDEBAR_COLLAPSE_AT or
                               (self._ws_packed and width < WORKSPACE_RESTORE_AT) or
                               (self._active_view != "chat" and
                                not self._sidebar_force_open))
        if should_hide_sidebar and self._sidebar_visible:
            self._sidebar_auto_hidden = True
            self._set_sidebar_visible(False, automatic=True)
        elif (not should_hide_sidebar and self._sidebar_auto_hidden
              and not self._sidebar_user_hidden):
            self._sidebar_auto_hidden = False
            self._set_sidebar_visible(True, automatic=True)

        if width < WORKSPACE_COLLAPSE_AT and self._ws_packed:
            self._workspace_auto_hidden = True
            self._close_workspace(automatic=True)
        elif width >= WORKSPACE_RESTORE_AT and self._workspace_auto_hidden:
            self._workspace_auto_hidden = False
            self._open_workspace(self._last_workspace_tab, automatic=True)

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
            if key == "agents":
                # agents 视图 = 真实配置面板（集群/分工/记忆模式）
                self._build_agents_team_panel(frame)
            else:
                self._build_stub_view(frame, key)
            self._views[key] = frame

    # ── agents 视图：Agent 集群与子 Agent 分工配置面板 ──
    def _build_agents_team_panel(self, parent):
        """真实配置面板（替换占位 stub）。

        数据存 ~/.forge/agent-cluster.json（sub_agent.load_config/save_config）。
        模型下拉直接显示 provider 的真模型 id（与运行时直发上游一致）。
        """
        cfg = team.load_config()
        self._team_cfg = cfg

        holder = tk.Frame(parent, bg=C["bg"])
        holder.pack(fill=tk.BOTH, expand=True)
        # 可滚动（面板可能很高）
        canvas = tk.Canvas(holder, bg=C["bg"], highlightthickness=0, bd=0)
        vbar = tk.Scrollbar(holder, orient=tk.VERTICAL, command=canvas.yview,
                            bg=C["surface2"], troughcolor=C["bg"],
                            relief=tk.FLAT, bd=0, highlightthickness=0, width=8)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(canvas, bg=C["bg"])
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        self._agents_panel_canvas = canvas

        # ── 所有 provider 的真模型 id（去重，作为下拉候选项）──
        model_ids: list[str] = []
        for r in self.user_rows:
            conf = r.get("config") or {}
            if "baseURL" in conf and conf.get("model"):
                mid = str(conf["model"])
                if mid not in model_ids:
                    model_ids.append(mid)
        if not model_ids:
            model_ids = ["（先在配置里添加 Provider）"]

        # ── 头部说明 ──
        tk.Label(body, text="Agent 集群与分工",
                 bg=C["bg"], fg=C["text"], font=FONT_TITLE,
                 anchor="w").pack(fill=tk.X, padx=20, pady=(18, 2))
        tk.Label(body,
                 text="把一轮请求拆给多个 worker 并行跑，再汇总注入主对话。"
                      "分工与集群互斥（同开会双份消耗）。核心逻辑移植自 AI Platform。",
                 bg=C["bg"], fg=C["muted"], font=FONT_SMALL, anchor="w",
                 wraplength=640, justify=tk.LEFT).pack(fill=tk.X, padx=20,
                                                        pady=(0, 12))

        # ── 1) Agent 集群 ──
        card1 = RoundedCard(body, radius=R_CARD, fill=C["surface"],
                            outline=C["border_hi"], padx=16, pady=14, bg=C["bg"])
        card1.pack(fill=tk.X, padx=20, pady=(0, 12))
        c1 = card1.content
        head1 = tk.Frame(c1, bg=C["surface"])
        head1.pack(fill=tk.X)
        tk.Label(head1, text="Agent 集群", bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        tk.Label(head1, text="多路同构编程方案并行，主模型综合对比",
                 bg=C["surface"], fg=C["muted"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(10, 0))
        self._team_cluster_var = tk.BooleanVar(
            value=bool(cfg["cluster"].get("enabled")))
        tk.Checkbutton(head1, text="启用", variable=self._team_cluster_var,
                       bg=C["surface"], fg=C["text"], selectcolor=C["sel"],
                       activebackground=C["surface"], activeforeground=C["text"],
                       font=FONT_SMALL).pack(side=tk.RIGHT)

        count_row = tk.Frame(c1, bg=C["surface"])
        count_row.pack(fill=tk.X, pady=(8, 4))
        tk.Label(count_row, text="路数", bg=C["surface"], fg=C["ter"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 8))
        self._team_cluster_count = tk.IntVar(
            value=max(1, min(4, int(cfg["cluster"].get("count") or 2))))
        for n in (1, 2, 3, 4):
            tk.Radiobutton(count_row, text=str(n), variable=self._team_cluster_count,
                           value=n, bg=C["surface"], fg=C["text"],
                           selectcolor=C["sel"], activebackground=C["surface"],
                           activeforeground=C["text"], font=FONT_SMALL).pack(
                side=tk.LEFT, padx=(0, 6))
        tk.Label(count_row, text="（每路可选不同模型）", bg=C["surface"],
                 fg=C["muted"], font=FONT_MICRO).pack(side=tk.LEFT)

        # 每路模型下拉（最多 4 路，按当前 count 显示）
        self._team_lane_combo: list[ttk.Combobox] = []
        lanes_cfg = list(cfg["cluster"].get("lanes") or [])
        lane_box = tk.Frame(c1, bg=C["surface"])
        lane_box.pack(fill=tk.X, pady=(4, 0))
        for i in range(4):
            row = tk.Frame(lane_box, bg=C["surface"])
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=f"方案 {i + 1}", bg=C["surface"], fg=C["ter"],
                     font=FONT_SMALL, width=8, anchor="w").pack(side=tk.LEFT)
            var = tk.StringVar()
            lane = lanes_cfg[i] if i < len(lanes_cfg) else {}
            initial = lane.get("model") or (model_ids[0] if model_ids else "")
            var.set(initial)
            combo = ttk.Combobox(row, textvariable=var, values=model_ids,
                                 state="readonly", font=FONT_SMALL)
            combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            self._team_lane_combo.append(combo)
            self._team_lane_combo[-1]._model_var = var  # 取值用

        # ── 2) 子 Agent 分工 ──
        card2 = RoundedCard(body, radius=R_CARD, fill=C["surface"],
                            outline=C["border_hi"], padx=16, pady=14, bg=C["bg"])
        card2.pack(fill=tk.X, padx=20, pady=(0, 12))
        c2 = card2.content
        head2 = tk.Frame(c2, bg=C["surface"])
        head2.pack(fill=tk.X)
        tk.Label(head2, text="子 Agent 分工", bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        tk.Label(head2, text="每个预设 = 一个并行 worker（温度固定 0.3）",
                 bg=C["surface"], fg=C["muted"], font=FONT_SMALL).pack(
            side=tk.LEFT, padx=(10, 0))
        self._team_subs_var = tk.BooleanVar(
            value=bool(cfg["sub_agents"].get("enabled")))
        tk.Checkbutton(head2, text="启用", variable=self._team_subs_var,
                       bg=C["surface"], fg=C["text"], selectcolor=C["sel"],
                       activebackground=C["surface"], activeforeground=C["text"],
                       font=FONT_SMALL).pack(side=tk.RIGHT)

        self._team_preset_rows: list[dict] = []
        preset_box = tk.Frame(c2, bg=C["surface"])
        preset_box.pack(fill=tk.X, pady=(8, 4))
        self._team_preset_box = preset_box

        # 新增行按钮（从模板 + 空白）
        btn_row = tk.Frame(c2, bg=C["surface"])
        btn_row.pack(fill=tk.X, pady=(6, 0))
        pill_button(btn_row, "＋ 从模板添加", self._team_add_from_template,
                    kind="ghost", bg=C["surface"], font=FONT_SMALL).pack(
            side=tk.LEFT, padx=(0, 6))
        pill_button(btn_row, "＋ 空白预设", lambda: self._team_add_preset(
            {"id": f"sub-{int(time.time())}", "role": "worker",
             "system_prompt": "直接给出要点，保持简洁。"}),
            kind="ghost", bg=C["surface"], font=FONT_SMALL).pack(side=tk.LEFT)

        # 载入已有预设
        for preset in list(cfg["sub_agents"].get("presets") or []):
            self._team_add_preset(preset)

        # ── 3) 记忆模式 ──
        card3 = RoundedCard(body, radius=R_CARD, fill=C["surface"],
                            outline=C["border_hi"], padx=16, pady=12, bg=C["bg"])
        card3.pack(fill=tk.X, padx=20, pady=(0, 12))
        c3 = card3.content
        mem_row = tk.Frame(c3, bg=C["surface"])
        mem_row.pack(fill=tk.X)
        tk.Label(mem_row, text="记忆模式", bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        self._team_mem_var = tk.StringVar(
            value=cfg.get("memory_mode") or "isolated")
        for val, tip in (("isolated", "隔离：子任务只看分工任务，最省 token"),
                         ("unified", "统一：注入最近对话背景")):
            tk.Radiobutton(mem_row, text=val, variable=self._team_mem_var,
                           value=val, bg=C["surface"], fg=C["text"],
                           selectcolor=C["sel"], activebackground=C["surface"],
                           activeforeground=C["text"],
                           font=FONT_SMALL).pack(side=tk.LEFT, padx=(10, 0))
            attach_tooltip(mem_row.winfo_children()[-1], tip)

        # ── 4) 保存 ──
        foot = tk.Frame(body, bg=C["bg"])
        foot.pack(fill=tk.X, padx=20, pady=(4, 24))
        pill_button(foot, "保存配置", self._team_save, kind="primary",
                    bg=C["bg"], font=FONT_UI).pack(side=tk.LEFT)
        tk.Label(foot, text="配置文件：~/.forge/agent-cluster.json",
                 bg=C["bg"], fg=C["muted"], font=FONT_MICRO).pack(
            side=tk.LEFT, padx=(12, 0))

    def _team_add_preset(self, preset: dict):
        """分工预设一行：role + 模型下拉 + prompt + 删除。"""
        box = self._team_preset_box
        row = tk.Frame(box, bg=C["surface"], highlightthickness=1,
                       highlightbackground=C["border_hi"])
        row.pack(fill=tk.X, pady=2)
        model_ids = [r.get("config", {}).get("model", "")
                     for r in self.user_rows
                     if "baseURL" in (r.get("config") or {})
                     and r.get("config", {}).get("model")]
        model_ids = list(dict.fromkeys(model_ids)) or ["（无 Provider）"]

        top = tk.Frame(row, bg=C["surface"])
        top.pack(fill=tk.X, padx=8, pady=(6, 2))
        tk.Label(top, text="角色", bg=C["surface"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(0, 4))
        role_var = tk.StringVar(value=str(preset.get("role") or "worker"))
        tk.Entry(top, textvariable=role_var, width=14, bg=C["input_bg"],
                 fg=C["text"], font=FONT_SMALL, relief=tk.FLAT, bd=0,
                 insertbackground=C["accent"], highlightthickness=1,
                 highlightbackground=C["border_hi"],
                 highlightcolor=C["accent"]).pack(side=tk.LEFT, ipady=2,
                                                  padx=(0, 8))
        tk.Label(top, text="模型", bg=C["surface"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(0, 4))
        model_var = tk.StringVar(value=str(preset.get("model") or model_ids[0]))
        combo = ttk.Combobox(top, textvariable=model_var, values=model_ids,
                             state="readonly", width=24, font=FONT_SMALL)
        combo.pack(side=tk.LEFT, padx=(0, 6))
        tk.Checkbutton(top, text="跑", variable=tk.BooleanVar(
            value=bool(preset.get("enabled", True))), bg=C["surface"],
            fg=C["text"], selectcolor=C["sel"], activebackground=C["surface"],
            font=FONT_SMALL).pack(side=tk.RIGHT)
        del_btn = glyph_button(top, "✕", lambda r=row: r.destroy(),
                               bg=C["surface"], fg=C["muted"], size=10,
                               tooltip="删除这条预设")
        del_btn.pack(side=tk.RIGHT, padx=(0, 4))

        prompt_var = tk.StringVar(value=str(preset.get("system_prompt") or ""))
        tk.Label(row, text="P", bg=C["surface"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(8, 2), pady=6)
        tk.Entry(row, textvariable=prompt_var, bg=C["input_bg"], fg=C["text"],
                 font=FONT_SMALL, relief=tk.FLAT, bd=0,
                 insertbackground=C["accent"], highlightthickness=1,
                 highlightbackground=C["border_hi"],
                 highlightcolor=C["accent"]).pack(side=tk.LEFT, fill=tk.X,
                                                  expand=True, padx=(0, 8),
                                                  ipady=2, pady=(0, 6))
        self._team_preset_rows.append(
            {"id": str(preset.get("id") or f"sub-{len(self._team_preset_rows)}"),
             "role_var": role_var, "model_var": model_var,
             "prompt_var": prompt_var, "row": row})

    def _team_add_from_template(self):
        cfg = self._team_cfg
        templates = list(cfg.get("templates") or [])
        added = [t for t in templates
                 if not any(r["role_var"].get() == t["role"]
                            for r in self._team_preset_rows)]
        if not added:
            self._set_status("模板都已添加（或没有预设模板）", "info")
            return
        self._team_add_preset(dict(added[0]))
        self._set_status(f"已添加模板：{added[0]['role']}（可继续添加）", "ok")

    def _team_save(self):
        cfg = team.load_config()
        # 集群
        lanes = []
        for i, combo in enumerate(self._team_lane_combo):
            if i < self._team_cluster_count.get():
                lanes.append({"model": combo._model_var.get()})
        cfg["cluster"] = {"enabled": bool(self._team_cluster_var.get()),
                          "count": int(self._team_cluster_count.get()),
                          "lanes": lanes}
        # 分工（从仍存在的行读取）
        presets = []
        for row in self._team_preset_rows:
            try:
                if not row["row"].winfo_exists():
                    continue
            except tk.TclError:
                continue
            presets.append({
                "id": row["id"],
                "role": row["role_var"].get().strip() or "worker",
                "system_prompt": row["prompt_var"].get(),
                "model": row["model_var"].get(),
                "enabled": True,
            })
        cfg["sub_agents"] = {"enabled": bool(self._team_subs_var.get()),
                             "presets": presets}
        cfg["memory_mode"] = self._team_mem_var.get()
        team.save_config(cfg)
        self._team_cfg = cfg
        self._set_status(
            f"已保存：集群 {'开' if cfg['cluster']['enabled'] else '关'}"
            f"（{cfg['cluster']['count']} 路）· 分工 "
            f"{'开' if cfg['sub_agents']['enabled'] else '关'}"
            f"（{len(presets)} 预设）· 记忆 {cfg['memory_mode']}", "ok")

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
        IconCanvas(head, key, size=24, bg=C["surface"], fg=C["accent2"]).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(head, text=title, bg=C["surface"], fg=C["text"], font=FONT_TITLE).pack(side=tk.LEFT)
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
            self._show_sidebar_for("files")
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
        self._sidebar_force_open = False
        self._set_nav_active(key)
        self._show_sidebar_for(key)
        self._apply_responsive_layout()
        if key == "config":
            self._update_status_label()

    def _set_nav_active(self, key: str):
        self._active_nav = key
        for nav_key, widgets in self._nav_widgets.items():
            active = nav_key == key
            for holder, inner in widgets:
                try:
                    base = holder.master.cget("bg")
                except Exception:
                    base = C["bg"]
                bg = C["sel"] if active else base
                holder.configure(bg=bg)
                inner.configure(bg=bg)
            fg = C["text"] if active else C["ter"]
            for icon, text in self._activity_labels.get(nav_key, []):
                icon.configure(bg=C["sel"] if active else C["sidebar"], fg=fg)
                text.configure(bg=C["sel"] if active else C["sidebar"], fg=fg)
            marker = getattr(self, "_activity_markers", {}).get(nav_key)
            if marker is not None:
                marker.configure(bg=C["accent"] if active else C["sidebar"])

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

    def _open_workspace(self, tab: str = "file_tree", *, automatic=False):
        if self.workspace is None:
            self._set_status(f"工作区面板不可用：{self._ws_error}", "warn")
            return
        self._last_workspace_tab = tab
        if not automatic:
            self._workspace_auto_hidden = False
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
        self.root.after_idle(self._apply_responsive_layout)

    def _close_workspace(self, *, automatic=False):
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
        if not automatic:
            self._workspace_auto_hidden = False
        try:
            self.ws_toggle_btn.configure(text="▤ 工作区")
        except (tk.TclError, AttributeError):
            pass

    def _toggle_workspace(self):
        if self._ws_packed:
            self._close_workspace()
        else:
            self._open_workspace(self._last_workspace_tab)

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
        return [s for s in sessions if isinstance(s, dict)
                and isinstance(s.get("messages", []), list)]

    def _write_sessions(self, sessions: list[dict]):
        temp_path = None
        try:
            payload = {"sessions": sessions[:40]}
            path = self._sessions_path()
            # 写前给现有文件留 .bak（上次快照），防止误覆盖丢历史
            try:
                if path.is_file() and path.stat().st_size > 0:
                    shutil.copy2(path, path.with_suffix(".json.bak"))
            except OSError:
                pass
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".sessions-", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except OSError as exc:
            self._set_status(f"会话记录保存失败：{exc}", "warn")
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _session_title(self) -> str:
        if self._session_custom_title:
            return self._session_custom_title
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
        last_group = None
        now = time.time()
        for session in sessions:
            try:
                age = max(0, now - float(session.get("updated", 0)))
            except (ValueError, TypeError):
                age = float("inf")
            import datetime as _dt
            _today = _dt.date.today()
            _d = _dt.datetime.fromtimestamp(float(session.get("updated", 0))).date() \
                if session.get("updated") else None
            if _d == _today:
                group = "今天"
            elif _d == _today - _dt.timedelta(days=1):
                group = "昨天"
            elif age < 604800:
                group = "最近 7 天"
            else:
                group = "更早"
            if group != last_group and not query:
                tk.Label(box, text=group, bg=C["sidebar"], fg=C["muted"],
                         font=FONT_MICRO, anchor="w", padx=12, pady=7).pack(fill=tk.X)
                last_group = group
            sid = str(session.get("id", ""))
            active = sid == active_id
            row = tk.Frame(box, bg=C["sel"] if active else C["sidebar"],
                           cursor="hand2",
                           highlightthickness=0,
                           highlightbackground=C["sel_border"] if active else C["sidebar"])
            row.pack(fill=tk.X, pady=1, padx=2)
            marker = tk.Frame(row, bg=C["accent"] if active else row["bg"], width=2)
            marker.pack(side=tk.LEFT, fill=tk.Y)
            row._active_marker = marker
            title = str(session.get("title", "未命名对话"))
            title_label = tk.Label(row, text=title, bg=row["bg"],
                     fg=C["text"] if active else C["subtext"], font=FONT_SMALL,
                     anchor=tk.W)
            title_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(9, 8), pady=6)
            attach_tooltip(title_label, title)
            for widget in (row, *row.winfo_children()):
                widget.bind("<Button-1>", lambda _e, s=sid: self._load_session(s))
                widget.bind("<Enter>", lambda _e, r=row, a=active: self._paint_history(r, a, True))
                widget.bind("<Leave>", lambda _e, r=row, a=active: self._paint_history(r, a, False))

    @staticmethod
    def _paint_history(row, active, hover):
        bg = C["sel"] if active else C["hover"] if hover else C["sidebar"]
        row.configure(bg=bg)
        for child in row.winfo_children():
            child.configure(bg=bg)
        marker = getattr(row, "_active_marker", None)
        if marker is not None:
            marker.configure(bg=C["accent"] if active else bg)

    def _new_session(self):
        if self._sending:
            self._set_status("正在生成回复，完成后可新建对话", "info")
            return
        self._archive_current_session()
        self._chat_history.clear()
        self._attachments.clear()
        self._session_custom_title = ""
        self._include_history = True
        self.send_var.set("")
        self._update_context_summary()
        self._session_id = f"s{int(time.time() * 1000)}"
        if hasattr(self, "chat_area"):
            self._show_chat_start()
        try:
            self.chat_title_var.set("新对话")
            self.chat_sub_var.set("")
        except AttributeError:
            pass
        self._refresh_history()
        self._set_status("已新建对话", "info")
        self.input_card.focus_entry()

    def _load_session(self, sid: str):
        if self._sending:
            self._set_status("正在生成回复，完成后可切换对话", "info")
            return
        if sid == self._session_id:
            return
        session = next((s for s in self._load_sessions() if str(s.get("id")) == sid), None)
        if session is None:
            return
        self._archive_current_session()
        self._chat_history = [ChatMessage(str(m.get("role", "user")),
                                          str(m.get("content", "")))
                              for m in session.get("messages", []) if isinstance(m, dict)
                              and m.get("role") in ("user", "assistant")
                              and isinstance(m.get("content"), str)]
        self._session_id = sid
        self._session_custom_title = str(session.get("title", "对话"))
        self._attachments.clear()
        self.send_var.set("")
        self._include_history = True
        self._update_context_summary()
        if hasattr(self, "chat_area"):
            self.chat_area.clear()
            for msg in self._chat_history:
                if msg.role == "user":
                    self.chat_area.add_user(msg.content)
                elif msg.content.strip():
                    agent = self.chat_area.add_agent(app=self)
                    agent.render_markdown(msg.content)
        try:
            self.chat_title_var.set(str(session.get("title", "对话")))
            self.chat_sub_var.set("")
        except AttributeError:
            pass
        self._refresh_history()
        self._set_status(f"已载入对话：{session.get('title', '')}", "info")
        self.input_card.focus_entry()

    # ── 任务视图（forge run）───────────────────────────────
    def _show_chat_start(self):
        """用真实入口填补首次打开的空白，不代替用户自动发送请求。"""
        self.chat_area.show_empty(
            "今天想完成什么？",
            ("描述目标、添加文件，然后与 Forge 一起推进。",),
            actions=(
                ("开始对话", "提问、讨论方案或梳理需求",
                 self.input_card.focus_entry),
                ("交给 Forge 一个任务", "运行工具并在时间线里跟踪进度",
                 lambda: self._show_view("task")),
                ("查看项目工作区", "浏览文件、变更、Diff 与预览",
                 lambda: self._open_workspace("file_tree")),
            ),
        )

    def _build_task_view(self, parent):
        head = tk.Frame(parent, bg=C["chat"])
        head.pack(fill=tk.X, padx=20, pady=(16, 10))
        tk.Label(head, text="任务", bg=C["chat"], fg=C["text"],
                 font=FONT_TITLE).pack(side=tk.LEFT)
        IconButton(head, text="＋ 新建任务", command=self._clear_task_view,
                  bg=C["chat"], fg=C["ter"], activebackground=C["hover"],
                  activeforeground=C["text"], font=FONT_SMALL, relief=tk.FLAT, bd=0,
                  padx=10, pady=3, cursor="hand2",
                  highlightthickness=1, highlightbackground=C["border_hi"]
                  ).pack(side=tk.RIGHT)
        tk.Label(parent, text="交代目标与验收标准，Forge 会执行并汇报过程；产出可在工作区查看。",
                 bg=C["chat"], fg=C["ter"], font=FONT_SMALL, anchor=tk.W,
                 justify=tk.LEFT, wraplength=760).pack(fill=tk.X, padx=20)

        ctl = tk.Frame(parent, bg=C["chat"])
        ctl.pack(side=tk.BOTTOM, fill=tk.X, padx=20, pady=(10, 12))
        tk.Label(ctl, text="交给 Forge 的任务", bg=C["chat"], fg=C["subtext"],
                 font=FONT_SMALL, anchor=tk.W).pack(fill=tk.X, pady=(0, 6))
        self.task_var = tk.StringVar()
        entry = tk.Entry(ctl, textvariable=self.task_var, bg=C["input_bg"],
                         fg=C["text"], insertbackground=C["accent"], font=FONT_UI,
                         relief=tk.FLAT, bd=0, highlightthickness=1,
                         highlightbackground=C["border_hi"], highlightcolor=C["accent"])
        entry.pack(fill=tk.X, ipady=10, ipadx=10)
        entry.bind("<Return>", lambda _e: self._run_task())

        strategy_row = tk.Frame(ctl, bg=C["chat"])
        strategy_row.pack(fill=tk.X, pady=(8, 0))
        tk.Label(strategy_row, text="策略", bg=C["chat"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(0, 6))
        self._strategy_row = tk.Frame(strategy_row, bg=C["chat"])
        self._strategy_row.pack(side=tk.LEFT)
        self._task_strategy = "medium"
        self._render_strategy_chips()
        row = tk.Frame(ctl, bg=C["chat"])
        row.pack(fill=tk.X, pady=(8, 0))
        self.task_stop_btn = pill_button(row, "■ 停止", self._stop_task, kind="danger",
                                         bg=C["chat"])
        self.task_stop_btn.pack(side=tk.RIGHT)
        self.task_run_btn = pill_button(row, "▶ 运行任务", self._run_task,
                                        kind="primary", bg=C["chat"], font=FONT_UI)
        self.task_run_btn.pack(side=tk.RIGHT, padx=(0, 8))
        pill_button(row, "▤ 工作区", lambda: self._open_workspace("file_tree"),
                    kind="ghost", bg=C["chat"]).pack(side=tk.RIGHT, padx=(0, 8))

        self.task_area = cw.MessageArea(parent, bg=C["chat"])
        self.task_area.pack(fill=tk.BOTH, expand=True)
        self.task_area.show_empty("让 Forge 开始工作",
                                  ("在下方描述目标与完成标准。",
                                   "执行步骤、工具调用与结果会汇成一条时间线。"))

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
        if self._task_running:
            self._set_status("任务运行中；策略修改将在结束后开放", "info")
            return
        self._task_strategy = value
        self._render_strategy_chips()

    def _clear_task_view(self):
        if getattr(self, "_task_running", False):
            self._set_status("任务正在运行，先停止再新建", "info")
            return
        self.task_area.show_empty("让 Forge 开始工作",
                                  ("在下方描述目标与完成标准。",
                                   "执行步骤、工具调用与结果会汇成一条时间线。"))

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
        self._task_msg = self.task_area.add_agent(role="任务执行",
                                                  subtitle=f"策略：{label} · 工作区：{self.run_py.parent}")
        self._task_msg.stream_text("正在执行 forge run；实际工具记录将在任务返回后显示。")
        self._task_msg.set_status("运行中…")
        self._task_running = True
        self._task_cancel_event = threading.Event()
        self._running_strategy = self._task_strategy
        self._task_started = time.time()
        self.task_run_btn.configure(state=tk.DISABLED)
        self._set_status(f"任务已下发：{task[:40]}", "info")

        cmd = task_command(_python_exe(), self.run_py, self.home, task, self._task_strategy)
        env = {**os.environ, **env_for()}
        cwd = str(self.run_py.parent)

        def worker():
            try:
                task_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if IS_WINDOWS:
                    task_flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8",
                                        errors="replace",
                                        creationflags=task_flags)
                self._task_proc = proc
                if self._task_cancel_event.is_set():
                    self._terminate_task_process(proc)
                out, err = proc.communicate()
                self._post_ui(self._task_finished, proc.returncode, out, err)
            except Exception as exc:  # pragma: no cover
                self._post_ui(self._task_failed, f"{type(exc).__name__}: {exc}")

        self._task_proc = None
        threading.Thread(target=worker, daemon=True).start()

    def _stop_task(self):
        proc = getattr(self, "_task_proc", None)
        if not getattr(self, "_task_running", False):
            return
        self._task_cancel_event.set()
        if proc is not None:
            threading.Thread(target=self._terminate_task_process, args=(proc,), daemon=False).start()
        self._set_status("已请求停止任务", "warn")

    @staticmethod
    def _terminate_task_process(proc):
        # 任务和 gateway 共用同一套经过校验的进程树清理；只 terminate 父进程
        # 会让工具子进程留在后台，表现为 Forge 已关但进程仍杀不掉。
        return kill_process_tree(proc)

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
        self._task_proc = None
        self.task_run_btn.configure(state=tk.NORMAL)
        if getattr(self, "_task_msg", None) is not None:
            self._task_msg.set_status("")
            self._task_msg.stream_text("")
            self._task_msg.add_note(f"任务未能完成：{message}", tone="error")
        self._set_status(f"任务失败：{message}", "error")

    def _task_finished(self, code: int, out: str, err: str):
        self._task_running = False
        self._task_proc = None
        self.task_run_btn.configure(state=tk.NORMAL)
        elapsed = max(0.0, time.time() - getattr(self, "_task_started", time.time()))
        msg = getattr(self, "_task_msg", None)
        data = self._parse_task_output(out)
        cancelled = getattr(self, "_task_cancel_event", threading.Event()).is_set()
        outcome, tone = task_outcome(code, data, cancelled)
        if msg is None:
            self._set_status("任务结束（视图已切换）", "info")
            return
        msg.set_status("")
        msg.stream_text("")

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
                        "desc": str(step.get("decision") or "未知") + " · " + note[:35],
                        "detail": str(step.get("result") or note),
                        "elapsed": f"#{step.get('index', '')}",
                        "ok": step.get("decision") == "ok",
                    })
                msg.add_tool_card(rows, title="执行步骤")
            text = str(data.get("text") or "").strip()
            if text:
                msg.render_markdown(text)
            else:
                msg.render_markdown("（本次没有返回文本）")
            usage = data.get("usage") or {}
            parts = [f"退出码 {code}", f"用时 {elapsed:.1f}s"]
            if isinstance(usage, dict):
                tokens = usage.get("total_tokens") or usage.get("tokens")
                if tokens:
                    parts.append(f"tokens {tokens}")
            parts.insert(0, outcome)
            msg.add_note(" · ".join(parts), tone=tone)
        else:
            raw = (out or "").strip() or (err or "").strip() or "（没有输出）"
            msg.render_markdown(raw)
            msg.add_note(f"退出码 {code} · 用时 {elapsed:.1f}s（未能解析结构化结果）",
                         tone=tone)
        if err and isinstance(data, dict):
            tail = err.strip().splitlines()[-4:]
            if tail:
                msg.add_note("stderr：\n" + "\n".join(tail), tone="muted")

        # 面向工作区的动作
        if self.workspace is not None:
            self.workspace.refresh()
        changed = self._changed_file_count()
        actions = []
        if changed is None:
            actions.append({"label": "查看仓库变更", "kind": "primary",
                            "command": lambda: self._open_workspace("changes")})
        elif changed > 0:
            actions.append({"label": f"仓库变更 ({changed})", "kind": "primary",
                            "command": lambda: self._open_workspace("changes")})
        actions.append({"label": "打开工作区", "command": lambda: self._open_workspace("file_tree")})
        if isinstance(data, dict):
            actions.extend(self._file_actions(str(data.get("text") or "")))
        if changed:
            actions.append({"label": "预览选中文件", "command": lambda: self._open_workspace("preview")})
        msg.add_actions(actions)

        if self.workspace is not None:
            self._push_terminal(f"[task] {outcome} · 退出码 {code} · 用时 {elapsed:.1f}s")
            self._push_terminal((out + "\n" + err)[-100000:])
        msg.add_note("仓库变更包含原有未提交内容，不代表全部由本次任务产生。")
        self._set_status(f"{outcome}（退出码 {code}，用时 {elapsed:.1f}s）", tone)

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
        pill_button(left, "API 密钥", self._open_api_keys, kind="accent_soft",
                    bg=C["surface"], icon="\U0001F511").pack(
            fill=tk.X, pady=(10, 0))

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
        # 两条路径：① 一键配置（下面选温度即可）；② 手写配置文件（直改用户层 JSON，所有参数自己控）。
        one_click = tk.Frame(left, bg=C["input_bg"])
        one_click.pack(fill=tk.X, pady=(12, 0))
        tk.Label(one_click, text="一键配置 · 采样温度", bg=C["input_bg"],
                 fg=C["text"], font=FONT_UI_BOLD).pack(anchor=tk.W)
        tk.Label(one_click,
                 text="Agent 场景 0.7 更稳；只接受默认温度的模型（GPT-6 / Claude 6 / "
                      "Sol / Kimi K3 / K2.6）会自动跳过；Claude 协议不发任何采样参数。",
                 bg=C["input_bg"], fg=C["muted"], font=FONT_SMALL,
                 wraplength=330, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 6))
        temp_row = tk.Frame(one_click, bg=C["input_bg"])
        temp_row.pack(anchor=tk.W)
        self.temperature_var = tk.StringVar(value=self._current_temperature_preset())
        self._temperature_buttons = {}
        for value, label in (("0.7", "0.7 · 均衡"), ("1.0", "1.0 · 保守"),
                             ("", "不设置")):
            btn = tk.Button(
                temp_row, text=label, bg=C["surface2"], fg=C["text"],
                activebackground=C["accent_soft"], activeforeground=C["accent"],
                font=FONT_UI, relief=tk.FLAT, padx=10, pady=4, cursor="hand2",
                command=lambda v=value: self._apply_temperature_preset(v))
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._temperature_buttons[value] = btn
        self._paint_temperature_buttons()
        IconButton(one_click, text="手写配置文件（所有参数自己控）", icon="external",
                  bg=C["surface2"], fg=C["link"],
                  activebackground=C["link_soft"], activeforeground=C["link"],
                  font=FONT_UI, relief=tk.FLAT, padx=10, pady=4, cursor="hand2",
                  command=self._open_user_layer_file).pack(anchor=tk.W, pady=(8, 0))
        IconButton(left, text="打开用户层目录", icon="external", bg=C["surface2"], fg=C["text"],
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
        # 供应商目录：19 家常见厂商 + 各自的套餐/订阅端点，点一下填模板
        tk.Button(btn_bar, text="供应商目录", bg=C["surface2"], fg=C["link"],
                  activebackground=C["link_soft"], activeforeground=C["link"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._open_provider_catalog, cursor="hand2"
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
        if self._reasoning_effort not in REASONING_LABELS:
            self._reasoning_effort = {
                "off": "off", "smart": "medium", "on": "contemplate",
            }.get(self._thinking_mode, "off")
        self.reasoning_var = tk.StringVar(value=self._reasoning_effort)
        # model_var 需要早于会话头创建（模型 chip 要显示它）
        self.model_var = tk.StringVar(value="default")
        head = tk.Frame(parent, bg=C["chat"])
        head.pack(fill=tk.X, padx=20, pady=(16, 10))
        left = tk.Frame(head, bg=C["chat"])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title_row = tk.Frame(left, bg=C["chat"])
        title_row.pack(anchor=tk.W, fill=tk.X)
        self.chat_title_var = tk.StringVar(value="新对话")
        tk.Label(title_row, textvariable=self.chat_title_var, bg=C["chat"],
                 fg=C["text"], font=FONT_TITLE).pack(side=tk.LEFT)
        self.chat_sub_var = tk.StringVar(value="")
        subtitle = tk.Label(parent, textvariable=self.chat_sub_var, bg=C["chat"], fg=C["muted"],
                 font=FONT_MICRO, anchor=tk.W, justify=tk.LEFT,
                 wraplength=520)
        def fit_subtitle(*_):
            if self.chat_sub_var.get():
                subtitle.pack(fill=tk.X, padx=20, pady=(0, 8), after=head)
            else:
                subtitle.pack_forget()
        self.chat_sub_var.trace_add("write", fit_subtitle)
        subtitle.bind("<Configure>", lambda event: subtitle.configure(wraplength=max(160, event.width)))

        # 会话头只保留：标题 + 当前模型 + 设置。温度等高级参数收进 ⚙ 菜单，
        # 「新对话」的主入口在左侧栏（这里只留一个低调的 ＋）。
        right = tk.Frame(head, bg=C["chat"])
        right.pack(side=tk.RIGHT, anchor=tk.N, before=left)
        self.temp_var = tk.StringVar(value="0.7")
        self.session_menu_btn = glyph_button(right, "⋯", self._popup_session_menu, bg=C["chat"],
                     fg=C["ter"], size=12,
                     tooltip="对话操作与设置")
        self.session_menu_btn.pack(side=tk.RIGHT)
        self.clear_chat_btn = pill_button(right, "＋", self._new_session,
                                         kind="quiet", bg=C["chat"], padx=8)
        self.model_chip = None  # 模型选择统一放在 Composer。


        self.chat_area = cw.MessageArea(parent, bg=C["chat"])
        self._chat_empty = True

        # 模型选择器（在输入卡底栏里，由 _make_model_picker 建）
        self.model_combo = None

        self.input_card = cw.InputCard(
            parent, bg=C["chat"],
            placeholder="让 Forge 构建、修复或调查……",
            on_send=self._do_send,
            on_stop=self._stop_send,
            on_paste=self._paste_into_input,
            on_model=self._open_model_menu,
            model_var=self.model_var,
            on_thinking=self._open_thinking_menu,
            thinking_text=self._thinking_label(),
            footer_left="空闲",
            model_widget=self._make_model_picker,
            on_attach=self._attach_files,
            on_context=self._open_context,
            on_commands=self._open_commands,
        )
        self.input_card.pack(fill=tk.X, side=tk.BOTTOM, padx=20, pady=(0, 6))
        self.context_summary = tk.StringVar(value="历史上下文：开启 · 附件：0")
        self.context_summary_label = tk.Label(
            parent, textvariable=self.context_summary, bg=C["chat"], fg=C["ter"],
            font=FONT_MICRO, anchor="w", padx=20)
        self.chat_area.pack(fill=tk.BOTH, expand=True)
        self.send_entry = self.input_card.entry
        self.send_var = self.input_card.send_var
        self.think_pill = self.input_card.think_pill
        self.think_pill.pack_forget()  # 任务专属控制仍可从会话菜单进入。
        self.request_status_var = tk.StringVar(value="空闲")
        self._show_chat_start()

    def _make_model_picker(self, host):
        """模型选择器：胶囊上带厂商标志，弹出列表逐项带标志与厂商名。

        ttk.Combobox 没法逐项显示图标，所以换成 ModelPicker；它保留了
        ``configure(values=...)`` / ``cget("values")`` 这一小片 Combobox 接口。
        """
        self.model_combo = ModelPicker(
            host, self.model_var, values=["default"],
            bg=C["input_bg"],
            provider_lookup=lambda model: select_provider(self.user_rows, model),
            router_lookup=self._model_router_config,
            on_select=self._on_model_picked,
            on_router=self._open_router_from_picker,
            router_choices=STRATEGY_CHOICES,
            on_router_strategy=self._set_router_strategy,
            on_settings=self._open_api_keys,
            favorites=self._model_favorites,
            on_favorite=self._save_model_favorites,
            thinking_var=self.reasoning_var,
            thinking_choices=REASONING_CHOICES,
            on_thinking=self._set_reasoning_effort,
        )
        self.model_combo.pack(side=tk.LEFT, padx=(0, 8))
        attach_tooltip(self.model_combo, "选择模型（来自已启用的 Provider）")
        return self.model_combo

    def _on_model_picked(self, value: str) -> None:
        """从下拉里选中一个模型：model_var 的 trace 负责后续（必要时重启 gateway）。"""
        brand = brand_marks.detect(
            model=value, provider=select_provider(self.user_rows, value))
        if brand is not None:
            self._set_status(f"已选择 {value}（{brand.label}）", "info")

    def _model_router_config(self) -> dict:
        """返回配置层真实 Router 数据；没有配置时不向 Picker 虚构 Auto。"""
        for row in self.user_rows:
            if str(row.get("id")) == "model" or str(row.get("name")) == "model:router":
                conf = row.get("config") or {}
                if isinstance(conf, dict) and isinstance(conf.get("routing"), dict):
                    return conf
        return {}

    def _open_router_from_picker(self, strategy: str) -> None:
        supported = {value for value, _label, _hint in STRATEGY_CHOICES}
        if strategy in supported and not self._task_running:
            self._task_strategy = strategy
            self._render_strategy_chips()
        self._show_view("task")
        self._set_status("已打开 Forge Auto 任务路由；策略来自当前 model:router 配置", "info")

    def _router_row(self) -> dict | None:
        for row in self.user_rows:
            if str(row.get("id")) == "model" or str(row.get("name")) == "model:router":
                return row
        return None

    # ── 一键配置：采样温度 ──────────────────────────────
    # ── 供应商目录（含套餐端点）──────────────────────────
    def _open_provider_catalog(self, query: str = "") -> None:
        """列出每个供应商及其各种接入方式，选中即填好配置模板。

        对标 Cherry Studio / Chatbox：不让用户在空白框里猜 baseURL。
        同一家的标准 API 与订阅套餐（Coding Plan 等）**分开列**，因为端点不同。
        这里只填 baseURL / wire / model，**不碰密钥**。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("供应商目录")
        dialog.geometry("820x640")
        dialog.transient(self.root)
        dialog.configure(bg=C["bg"])
        dialog.minsize(620, 460)

        head = tk.Frame(dialog, bg=C["bg"], padx=20, pady=16)
        head.pack(fill=tk.X)
        tk.Label(head, text="供应商目录", bg=C["bg"], fg=C["text"],
                 font=FONT_TITLE).pack(anchor=tk.W)
        tk.Label(head, text="每个供应商单独一条；标准 API 与订阅套餐分开列。"
                            "点一行即把 baseURL / wire 填进下面的输入框，你再补 apiKey。",
                 bg=C["bg"], fg=C["muted"], font=FONT_SMALL,
                 wraplength=740, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 10))

        search_row = tk.Frame(head, bg=C["bg"])
        search_row.pack(fill=tk.X)
        search_var = tk.StringVar(value=query)
        entry = tk.Entry(search_row, textvariable=search_var, bg=C["input_bg"],
                         fg=C["text"], insertbackground=C["accent"], font=FONT_UI,
                         relief=tk.FLAT, highlightthickness=1,
                         highlightbackground=C["border_hi"],
                         highlightcolor=C["accent"])
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        tk.Label(search_row, text="搜名称 / 别名 / 域名", bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT, padx=(8, 0))

        listing = cw.ScrollArea(dialog, bg=C["surface"], padx=0, pady=0)
        listing.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 12))

        footer = tk.Frame(dialog, bg=C["bg"], padx=20, pady=10)
        footer.pack(fill=tk.X)
        tk.Label(footer, text="「待确认」= 该地址我没核到一手来源，若报错请以官网控制台为准",
                 bg=C["bg"], fg=C["muted"], font=FONT_CAPTION).pack(side=tk.LEFT)
        tk.Button(footer, text="关闭", bg=C["surface2"], fg=C["text"],
                  activebackground=C["border"], activeforeground=C["text"],
                  font=FONT_UI, relief=tk.FLAT, padx=14, pady=5,
                  command=dialog.destroy, cursor="hand2").pack(side=tk.RIGHT)

        def pick(preset, plan):
            snippet = catalog.config_snippet(preset, plan)
            self._clear_placeholder()
            self.input_text.delete("1.0", tk.END)
            self.input_text.insert("1.0", snippet)
            self.input_text.focus_set()
            self._set_status(
                f"已填入 {preset.name} · {plan.label} 的模板，补上 apiKey 后点「整理并预览」",
                "ok")
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        def render(*_args):
            for child in list(listing.inner.winfo_children()):
                child.destroy()
            hits = catalog.find(search_var.get())
            if not hits:
                tk.Label(listing.inner, text="没有匹配的供应商", bg=C["surface"],
                         fg=C["muted"], font=FONT_UI, pady=24).pack()
                return
            for preset in hits:
                card = tk.Frame(listing.inner, bg=C["surface"],
                                highlightthickness=1,
                                highlightbackground=C["border"])
                card.pack(fill=tk.X, pady=(0, 8))
                top = tk.Frame(card, bg=C["surface"], padx=12, pady=8)
                top.pack(fill=tk.X)
                icon, keep = brand_marks.mark_icon(preset.brand or None, 16)
                if icon is not None:
                    holder = tk.Label(top, image=icon, bg=C["surface"])
                    holder.image = icon
                    holder.pack(side=tk.LEFT, padx=(0, 7))
                tk.Label(top, text=preset.name, bg=C["surface"], fg=C["text"],
                         font=FONT_UI_BOLD).pack(side=tk.LEFT)
                tone = {catalog.SOURCE_CONFIRMED: C["ok"],
                        catalog.SOURCE_USER: C["ok"],
                        catalog.SOURCE_DOCS: C["muted"]}.get(
                    preset.source, C["warn"])
                tk.Label(top, text=f"· {preset.source_label}", bg=C["surface"],
                         fg=tone, font=FONT_MICRO).pack(side=tk.LEFT, padx=(7, 0))
                if preset.docs:
                    tk.Label(top, text=preset.docs, bg=C["surface"], fg=C["muted"],
                             font=FONT_CAPTION).pack(side=tk.RIGHT)
                plans_row = tk.Frame(card, bg=C["surface"], padx=12)
                plans_row.pack(fill=tk.X, pady=(0, 9))
                for plan in preset.plans:
                    # usable=False：本框架用不了（协议不支持）——置灰且不可点，
                    # 而不是给一份装不上的模板。
                    usable = getattr(plan, "usable", True)
                    chip = tk.Button(
                        plans_row,
                        text=plan.label if usable else f"{plan.label}",
                        bg=C["surface2"] if usable else C["input_bg"],
                        fg=C["text"] if usable else C["placeholder"],
                        activebackground=C["accent_soft"],
                        activeforeground=C["accent"],
                        font=FONT_MICRO, relief=tk.FLAT, padx=9, pady=3,
                        cursor="hand2" if usable else "arrow",
                        state=tk.NORMAL if usable else tk.DISABLED,
                        command=(lambda p=preset, pl=plan: pick(p, pl))
                        if usable else None)
                    chip.pack(side=tk.LEFT, padx=(0, 6))
                    tip = plan.base_url
                    if plan.note:
                        tip = f"{plan.base_url}\n{plan.note}"
                    plan_source = plan.effective_source(preset)
                    if plan_source == catalog.SOURCE_UNVERIFIED:
                        tip += "\n（该地址标记为待确认）"
                    attach_tooltip(chip, tip)

        search_var.trace_add("write", lambda *_: render())
        entry.bind("<Escape>", lambda _e: dialog.destroy())
        render()
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _current_temperature_preset(self) -> str:
        """看当前所有 provider 写的是什么温度；不一致就返回空串（不设置）。"""
        values = set()
        for row in self.user_rows:
            conf = row.get("config") or {}
            if "baseURL" not in conf:
                continue
            raw = conf.get("temperature")
            values.add("" if raw in (None, "") else f"{float(raw):g}")
        if len(values) == 1:
            return next(iter(values))
        return ""

    def _paint_temperature_buttons(self) -> None:
        current = self.temperature_var.get()
        for value, btn in getattr(self, "_temperature_buttons", {}).items():
            selected = value == current
            btn.configure(bg=C["accent_soft"] if selected else C["surface2"],
                          fg=C["accent"] if selected else C["text"],
                          font=FONT_UI_BOLD if selected else FONT_UI)

    def _apply_temperature_preset(self, value: str) -> bool:
        """一键把温度写到所有 provider 行。

        空串 = 不写这个字段（走服务端默认）。真正「能不能发」由 forge.sampling
        在发请求时决定：只接受默认温度的模型、以及 Claude 协议，都会自动跳过。
        """
        try:
            latest = load_user_layer(self.home)
        except (OSError, ValueError) as exc:
            self._set_status(f"读取配置失败：{exc}", "error")
            return False
        rows = copy.deepcopy(latest)
        touched = 0
        for row in rows:
            conf = row.get("config")
            if not isinstance(conf, dict) or "baseURL" not in conf:
                continue
            if value == "":
                if conf.pop("temperature", None) is not None:
                    touched += 1
            elif conf.get("temperature") != float(value):
                conf["temperature"] = float(value)
                touched += 1
        if touched:
            try:
                save_user_layer(self.home, rows, expected_rows=latest)
            except (OSError, ValueError) as exc:
                self._set_status(f"温度写入失败：{exc}", "error")
                return False
            self.user_rows = rows
        self.temperature_var.set(value)
        self._paint_temperature_buttons()
        label = {"0.7": "0.7（均衡，Agent 推荐）", "1.0": "1.0（保守）"}.get(
            value, "不设置（走服务端默认）")
        self._set_status(f"采样温度已设为{label}；共更新 {touched} 个 provider", "ok")
        return True

    def _open_user_layer_file(self) -> None:
        """手写配置：直接打开用户层 JSON（所有参数自己控）。"""
        path = Path(self.home) / "forge.patch.json"
        if not path.exists():
            self._set_status(f"用户层文件不存在：{path}", "warn")
            return
        self._open_path(path)

    def _set_router_strategy(self, strategy: str) -> bool:
        """把智能路由策略写回 model:router（以前只能看，不能改）。

        写的是真实的用户层配置（routing.strategy），forge run 下一次就按新策略走。
        """
        supported = {value for value, _label, _hint in STRATEGY_CHOICES}
        if strategy not in supported:
            return False
        if self._task_running:
            self._set_status("任务运行中；策略修改将在结束后开放", "info")
            return False
        row = self._router_row()
        if row is None:
            self._set_status("用户层里没有 model:router 配置，无法保存路由策略", "warn")
            return False
        try:
            latest = load_user_layer(self.home)
            rows = copy.deepcopy(latest)
            target = None
            for item in rows:
                if str(item.get("id")) == "model" or str(item.get("name")) == "model:router":
                    target = item
                    break
            if target is None:
                self._set_status("配置已变化，请刷新后重试", "warn")
                return False
            conf = target.setdefault("config", {})
            routing = conf.get("routing")
            if not isinstance(routing, dict):
                routing = {}
            routing["strategy"] = strategy
            conf["routing"] = routing
            save_user_layer(self.home, rows, expected_rows=latest)
        except (OSError, ValueError) as exc:
            self._set_status(f"路由策略保存失败：{exc}", "error")
            return False
        self.user_rows = rows
        self._task_strategy = strategy
        try:
            self._render_strategy_chips()
        except Exception:
            pass
        label = next((l for v, l, _h in STRATEGY_CHOICES if v == strategy), strategy)
        self._set_status(f"智能路由策略已设为「{label}」；下一次 forge run 生效", "ok")
        return True

    # ── API 密钥 ────────────────────────────────────────
    def _masked_key(self, key: str) -> str:
        """只回显足够辨认的前后缀，中间用省略号（不把密钥整串放回界面）。"""
        key = str(key or "")
        if not key:
            return ""
        if len(key) <= 10:
            return key[:2] + "…"
        return f"{key[:6]}…{key[-4:]}"

    def _key_targets(self) -> list[dict]:
        """需要密钥的 provider 列表（按用户层里的真实 provider 行）。"""
        out = []
        for row in self.user_rows:
            conf = row.get("config") or {}
            if "baseURL" not in conf:
                continue
            rid = str(row.get("id") or "")
            out.append({
                "id": rid,
                "label": conf.get("modelLabel") or conf.get("model") or rid,
                "brand": brand_marks.detect(model=conf.get("modelLabel"), provider=conf),
                "host": str(conf.get("baseURL") or ""),
                "disabled": bool(row.get("disabled")),
                # ref = 面板保存后会注入的名字（canonical）；config_ref = 配置里现写的引用。
                # 两者不同时，保存一次就会被修正一致。
                "ref": self._canonical_key_env(rid),
                "config_ref": self._key_env_name(conf),
            })
        return out

    @staticmethod
    def _key_env_name(conf: dict) -> str:
        """从 apiKey 的 $expr 里取出要注入的环境变量名（UI 只展示名字，不碰值）。"""
        api_key = conf.get("apiKey")
        if isinstance(api_key, dict):
            m = re.fullmatch(
                r"get\(['\"]env\.([A-Za-z_][A-Za-z0-9_]*)['\"]\s*,\s*['\"]['\"]\)",
                str(api_key.get("$expr", "")).strip())
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _canonical_key_env(rid: str) -> str:
        """面板保存密钥时真正会被注入的 env 名。

        必须与 ``secret_store.env_for`` 完全一致，否则会出现「面板显示已配置、
        gateway 却报密钥未设置」——**直接复用 env_for 而不是手写规则**，
        以后改名也不会两者跑偏（整理器生成的随机引用 FORGE_KEY_XXXX
        就是靠这一步在保存时被改写回来的）。
        """
        if not rid:
            return ""
        return next(iter(env_for({rid: "x"})), "")

    def _repair_key_ref(self, rid: str) -> bool:
        """把某 provider 行的 apiKey 引用改写成面板真正注入的 env 名。

        返回是否发生了改写；失败不影响密钥已写入（只提示）。
        """
        canonical = self._canonical_key_env(rid)
        if not canonical:
            return False
        try:
            latest = load_user_layer(self.home)
        except (OSError, ValueError):
            return False
        rows = copy.deepcopy(latest)
        target = next((r for r in rows if str(r.get("id")) == rid), None)
        if target is None:
            return False
        conf = target.setdefault("config", {})
        if self._key_env_name(conf) == canonical:
            return False
        conf["apiKey"] = {"$expr": f"get('env.{canonical}', '')"}
        try:
            save_user_layer(self.home, rows, expected_rows=latest)
        except (OSError, ValueError):
            return False
        self.user_rows = rows
        try:
            self._refresh_provider_list()
        except Exception:
            pass
        return True

    def _open_api_keys(self, _provider=None) -> None:
        """API 密钥面板：逐 providers 看状态 + 直接录入/更新。

        以前只能靠「粘一坨 JSON 让矫治器猜」或从 AutoClaw 导入，
        没有一个地方能明确看到「哪家还没配密钥」。
        """
        from secret_store import save as _save_secrets
        dialog = tk.Toplevel(self.root)
        dialog.title("API 密钥")
        dialog.geometry("760x600")
        dialog.transient(self.root)
        dialog.configure(bg=C["bg"])
        dialog.minsize(560, 420)

        head = tk.Frame(dialog, bg=C["bg"], padx=20, pady=16)
        head.pack(fill=tk.X)
        IconCanvas(head, "key", size=22, bg=C["bg"], fg=C["accent2"]).pack(
            side=tk.LEFT, padx=(0, 8))
        tk.Label(head, text="API 密钥", bg=C["bg"], fg=C["text"],
                 font=FONT_TITLE).pack(side=tk.LEFT)
        self.key_status_var = tk.StringVar()
        tk.Label(head, textvariable=self.key_status_var, bg=C["bg"], fg=C["muted"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(head, text="密钥只写入 ~/.forge/secrets.json（不入日志、不进命令行）",
                 bg=C["bg"], fg=C["muted"], font=FONT_CAPTION).pack(side=tk.RIGHT)

        rows_host = cw.ScrollArea(dialog, bg=C["surface"], padx=0, pady=0)
        rows_host.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 12))

        footer = tk.Frame(dialog, bg=C["bg"], padx=20, pady=12)
        footer.pack(fill=tk.X)
        pill_button(footer, "完成", dialog.destroy, kind="primary",
                    bg=C["bg"]).pack(side=tk.RIGHT)

        def refresh_rows():
            for child in list(rows_host.inner.winfo_children()):
                child.destroy()
            secrets = _load_secrets()
            targets = self._key_targets()
            configured = 0
            for target in targets:
                card = tk.Frame(rows_host.inner, bg=C["surface"],
                                highlightthickness=1,
                                highlightbackground=C["border"])
                card.pack(fill=tk.X, pady=(0, 8))
                top = tk.Frame(card, bg=C["surface"], padx=12, pady=10)
                top.pack(fill=tk.X)
                left = tk.Frame(top, bg=C["surface"])
                left.pack(side=tk.LEFT, fill=tk.X, expand=True)
                line = tk.Frame(left, bg=C["surface"])
                line.pack(fill=tk.X)
                brand = target["brand"]
                if brand is not None:
                    icon, keep = brand_marks.mark_icon(brand, 16)
                    if icon is not None:
                        holder = tk.Label(line, image=icon, bg=C["surface"])
                        holder.image = icon
                        holder.pack(side=tk.LEFT, padx=(0, 6))
                tk.Label(line, text=str(target["label"]), bg=C["surface"],
                         fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
                if target["disabled"]:
                    tk.Label(line, text="已停用", bg=C["surface"], fg=C["warn"],
                             font=FONT_MICRO).pack(side=tk.LEFT, padx=(8, 0))
                env_name = target["ref"] or ""
                current = secrets.get(target["id"]) or (
                    os.environ.get(env_name, "") if env_name else "")
                if current:
                    configured += 1
                    state_text = f"✅ 已配置  {self._masked_key(current)}"
                    state_fg = C["ok"]
                else:
                    state_text = "⚠️ 未配置"
                    state_fg = C["warn"]
                meta = f"{target['host']}" + (f"   ·   {env_name}" if env_name else "")
                if target.get("config_ref") and target["config_ref"] != env_name:
                    meta += f"   ·   配置现引用 {target['config_ref']}（保存后修正）"
                tk.Label(left, text=meta, bg=C["surface"], fg=C["muted"],
                         font=FONT_CAPTION, anchor="w").pack(fill=tk.X, pady=(3, 0))
                tk.Label(top, text=state_text, bg=C["surface"], fg=state_fg,
                         font=FONT_SMALL).pack(side=tk.RIGHT, padx=(10, 0))

                entry_row = tk.Frame(card, bg=C["surface"], padx=12)
                entry_row.pack(fill=tk.X, pady=(0, 10))
                var = tk.StringVar()
                entry = tk.Entry(entry_row, textvariable=var, show="•",
                                 bg=C["input_bg"], fg=C["text"],
                                 insertbackground=C["accent"], font=FONT_MONO,
                                 relief=tk.FLAT, highlightthickness=1,
                                 highlightbackground=C["border_hi"],
                                 highlightcolor=C["accent"])
                entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
                attach_tooltip(entry, "粘贴新的 API Key 后点保存；留空不改动")

                def save_key(rid=target["id"], v=var, e=entry):
                    value = v.get().strip()
                    if not value:
                        self._set_status("密钥为空，未做修改", "info")
                        return
                    cur = _load_secrets()
                    cur[rid] = value
                    try:
                        _save_secrets(cur)
                    except OSError as exc:
                        self._set_status(f"密钥写入失败：{exc}", "error")
                        return
                    v.set("")
                    e.configure(highlightbackground=C["border_hi"])
                    # 关键一步：把配置里的引用改写成面板真正注入的名字。
                    # 否则（例如整理器生成的 FORGE_KEY_XXXX 引用）会出现
                    # 「面板显示已配置、gateway 却报密钥未设置」。
                    repaired = self._repair_key_ref(rid)
                    note = "（已同步修正配置引用）" if repaired else ""
                    self._set_status(
                        f"{rid} 的密钥已更新（仅本机密钥库）{note}", "ok")
                    refresh_rows()

                def clear_key(rid=target["id"]):
                    cur = _load_secrets()
                    if rid in cur:
                        cur.pop(rid, None)
                        try:
                            _save_secrets(cur)
                        except OSError as exc:
                            self._set_status(f"密钥清理失败：{exc}", "error")
                            return
                        self._set_status(f"{rid} 的密钥已删除", "warn")
                    refresh_rows()

                pill_button(entry_row, "保存", save_key, kind="primary",
                            bg=C["surface"]).pack(side=tk.LEFT, padx=(6, 0))
                pill_button(entry_row, "清除", clear_key, kind="quiet",
                            bg=C["surface"]).pack(side=tk.LEFT, padx=(6, 0))
                entry.bind("<Return>", lambda _e, f=save_key: f())
            if not targets:
                tk.Label(rows_host.inner, text="用户层里还没有 provider 配置",
                         bg=C["surface"], fg=C["muted"], font=FONT_SMALL,
                         pady=20).pack()
            self.key_status_var.set(f"{configured} / {len(targets)} 家已配置")

        refresh_rows()
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _open_provider_settings_from_picker(self, provider: dict | None) -> None:
        self._show_view("config")
        if provider:
            target_model = str(provider.get("model") or "")
            for idx, row in enumerate(self.user_rows):
                conf = row.get("config") or {}
                if target_model and conf.get("model") == target_model:
                    try:
                        self.provider_list.selection_clear(0, tk.END)
                        self.provider_list.selection_set(idx)
                        self.provider_list.see(idx)
                        self._on_provider_select()
                    except (tk.TclError, AttributeError):
                        pass
                    break

    def _save_model_favorites(self, values) -> None:
        self._model_favorites = [str(v) for v in values]
        save_desktop_config(model_favorites=self._model_favorites)

    def _open_model_menu(self):
        """点标题栏模型胶囊时的兜底：直接展开模型列表。"""
        try:
            self.model_combo.open_menu()
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
            self._session_custom_title = name.strip()
            self.chat_title_var.set(name.strip())
            self._archive_current_session()
            self._refresh_history()
            self._set_status(f"对话已重命名为「{name.strip()}」", "ok")

    def _update_context_summary(self):
        self.context_summary.set(
            f"历史上下文：{'开启' if self._include_history else '关闭'} · "
            f"本轮文本附件：{len(self._attachments)}")
        label = getattr(self, "context_summary_label", None)
        if label is not None:
            if self._attachments or not self._include_history:
                label.pack(fill=tk.X, side=tk.BOTTOM, after=self.input_card)
            else:
                label.pack_forget()

    def _attach_files(self):
        if self._sending:
            self._set_status("本轮正在生成，结束后可修改附件", "info")
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="添加 UTF-8 文本附件",
                                            initialdir=str(self._repo_root()))
        for path in paths:
            try:
                item = read_attachment(path)
                pending = [a for a in self._attachments if a["path"] != item["path"]] + [item]
                compose_prompt(self.send_var.get(), pending)
                self._attachments = pending
            except (OSError, ValueError) as exc:
                self._set_status(f"{Path(path).name}：{exc}", "warn")
                break
        self._update_context_summary()

    def _open_context(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("本轮上下文 · 发送前可检查")
        dialog.geometry("720x540")
        dialog.transient(self.root)
        dialog.configure(bg=C["chat"])
        use_history = tk.BooleanVar(value=self._include_history)
        preview = scrolledtext.ScrolledText(dialog, wrap="word", bg=C["input_bg"],
                                            fg=C["text"], font=FONT_MONO_SM)
        def refresh():
            self._include_history = use_history.get()
            self._update_context_summary()
            selected = self._chat_history if self._include_history else []
            try:
                prompt = compose_prompt(self.send_var.get(), self._attachments)
            except ValueError as exc:
                prompt = str(exc)
            payload = [{"role": m.role, "content": m.content} for m in selected]
            payload.append({"role": "user", "content": prompt})
            preview.configure(state=tk.NORMAL)
            preview.delete("1.0", tk.END)
            preview.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
            preview.configure(state=tk.DISABLED)
        tk.Checkbutton(dialog, text="发送当前会话的历史消息", variable=use_history,
                       command=refresh, bg=C["chat"], fg=C["text"], selectcolor=C["surface2"],
                       state=tk.DISABLED if self._sending else tk.NORMAL).pack(anchor="w", padx=12, pady=8)
        tk.Label(dialog, text="附件以添加时的文本快照发送。下方展示消息角色及实际内容。",
                 bg=C["chat"], fg=C["ter"]).pack(anchor="w", padx=12)
        items = tk.Frame(dialog, bg=C["chat"])
        items.pack(fill=tk.X, padx=12, pady=6)
        for attachment in list(self._attachments):
            row = tk.Frame(items, bg=C["chat"])
            row.pack(fill=tk.X)
            tk.Label(row, text=Path(attachment["path"]).name, bg=C["chat"],
                     fg=C["text"]).pack(side=tk.LEFT)
            def remove(item=attachment, widget=row):
                if item in self._attachments:
                    self._attachments.remove(item)
                widget.destroy()
                refresh()
            tk.Button(row, text="移除", command=remove,
                      state=tk.DISABLED if self._sending else tk.NORMAL).pack(side=tk.RIGHT)
        preview.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        refresh()

    def _open_commands(self):
        items = (
            {"label": "新建对话", "detail": "/new", "command": self._new_session},
            {"label": "任务执行", "detail": "/task · 使用 Forge Router",
             "command": lambda: self._show_view("task")},
            {"separator": True},
            {"label": "展开 / 收起工作区", "detail": "/workspace",
             "command": self._toggle_workspace},
            {"label": "查看仓库变更", "detail": "/changes",
             "command": lambda: self._open_workspace("changes")},
            {"label": "检查发送上下文", "detail": "/context",
             "command": self._open_context},
            {"label": "功能开关", "detail": "/tools",
             "command": lambda: self._show_view("tools")},
        )
        return show_popover_menu(self.input_card, items, title="工具与命令",
                                 width=310, prefer_above=True)

    def _run_local_command(self, text):
        command, _, argument = text.partition(" ")
        commands = {"/new": self._new_session, "/workspace": self._toggle_workspace,
                    "/changes": lambda: self._open_workspace("changes"),
                    "/context": self._open_context,
                    "/tools": lambda: self._show_view("tools"),
                    "/help": self._open_commands}
        if command == "/task":
            self.task_var.set(argument.strip())
            self._show_view("task")
            self.send_var.set("")
            return True
        if command in commands and not argument.strip():
            self.send_var.set("")
            commands[command]()
            return True
        return False

    def _file_actions(self, text):
        """Only link existing files mentioned by the real response, within this project."""
        root = self._repo_root().resolve()
        actions, seen = [], set()
        for candidate in re.findall(r"`([^`\n]+)`", text):
            try:
                path = (root / candidate).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path in seen:
                    continue
            except (OSError, ValueError):
                continue
            seen.add(path)
            def open_file(target=path):
                self._open_workspace("file_tree")
                if self.workspace is not None:
                    self.workspace.open_file(target)
            actions.append({"label": f"打开 {path.name}", "command": open_file})
            if len(actions) == 3:
                break
        return actions
    # ── 沉思模式（forge 的 thinking.mode：off / smart / on）──
    def _read_thinking_mode(self) -> str:
        for row in self.user_rows:
            if str(row.get("id")) == "thinking":
                mode = str((row.get("config") or {}).get("mode", "off")).lower()
                return mode if mode in ("off", "smart", "on") else "off"
        return "off"

    def _thinking_label(self) -> str:
        return f"任务沉思 · {THINKING_LABELS.get(self._thinking_mode, '关闭')}"

    def _open_thinking_menu(self, anchor=None):
        items = [
            {"label": label, "detail": hint,
             "selected": self._thinking_mode == mode,
             "command": lambda m=mode: self._set_thinking_mode(m)}
            for mode, label, hint in THINKING_CHOICES
        ]
        return show_popover_menu(anchor or self.session_menu_btn, items, title="任务沉思",
                                 width=330)

    def _set_thinking_mode(self, mode: str, *, announce=True):
        if self._feature_dirty:
            self._set_status("请先保存或还原功能开关的修改，再切换任务沉思", "warn")
            return False
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
            return False
        self.user_rows = rows
        self._thinking_mode = mode
        self.input_card.set_thinking_text(self._thinking_label())
        self.think_pill.configure(bg=C["accent_soft"] if mode != "off" else C["input_bg"])
        self._rebuild_feature_toggles(force=True)
        if announce:
            self._set_status(
                f"沉思模式已设为「{THINKING_LABELS[mode]}」；forge run 任务即时生效，"
                f"运行中的服务需重启以应用", "ok")
        return True

    def _set_reasoning_effort(self, effort: str):
        if effort not in REASONING_LABELS:
            return False
        task_mode = ("off" if effort == "off" else
                     "on" if effort == "contemplate" else "smart")
        if task_mode != self._thinking_mode:
            if not self._set_thinking_mode(task_mode, announce=False):
                return False
        self._reasoning_effort = effort
        self.reasoning_var.set(effort)
        save_desktop_config(reasoning_effort=effort)
        label = REASONING_LABELS[effort]
        if effort == "off":
            detail = "当前对话不发送额外推理强度"
        elif effort == "contemplate":
            detail = "对话使用高强度推理，Forge 任务启用沉思"
        else:
            detail = "支持思考强度的模型将在下一条消息生效"
        self._set_status(f"思考强度已设为「{label}」；{detail}", "ok")
        return True

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
            # 关键：name 是显示名（如 mimo），id 才是 API 真实模型名
            # （如 mimo-v2.6-flash）。发给上游的必须是 id。
            entry = models[0] if isinstance(models[0], dict) else {}
            mid = str(entry.get("id") or entry.get("name") or "default")
            friendly = str(entry.get("name") or "").strip()
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
            # UI 显示友好名；与真实 id 不同才写，避免冗余字段
            if friendly and friendly != mid:
                conf["modelLabel"] = friendly
            else:
                conf.pop("modelLabel", None)
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
                label = conf.get("modelLabel") or model
                state = "关闭" if r.get("disabled") else "开启"
                # 有友好名时顺带显示真实 id，方便排查「上游不认模型」这类问题
                shown = (f"{label}  ({model})" if label and model and label != model
                         else (label or "未指定模型"))
                display.append(f"[{state}] {rid}  ·  {shown}")
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
                m = r["config"].get("modelLabel") or r["config"].get("model") or r.get("id")
                if m and m not in models:
                    models.append(m)
        models = ["default"] + [m for m in models if m and m != "default"]
        self.model_combo.configure(values=models)
        self._sync_model_chip()
        if models and self.model_var.get() not in models:
            # 程序化回落不算「用户切模型」，别触发 gateway 重启
            self._suppress_model_trace = True
            try:
                self.model_var.set(models[0])
            finally:
                self._suppress_model_trace = False
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
        if self.gateway_proc or self._sending or self._task_running:
            self._set_status("请在任务与请求结束、gateway 停止后切换 Forge 目录", "warn")
            return
        folder = filedialog.askdirectory(title="选择包含 run.py 的 Forge 目录", parent=self.root)
        if not folder:
            return
        candidate = Path(folder) / "run.py"
        if not candidate.is_file():
            self._set_status("所选目录不包含 run.py，请选择 Forge 根目录", "error")
            return
        self.run_py = candidate
        if self.workspace is not None:
            self.workspace.set_repo_root(candidate.parent)
        save_desktop_config(forge_repo=str(candidate.parent))
        self.path_lbl.configure(text="forge 目录已就绪")
        self._set_status(f"已记住 Forge 目录：{folder}", "ok")

    def _sync_model_chip(self):
        chip = getattr(self, "model_chip", None)
        if chip is None:
            return
        try:
            chip.set_text(f"▣ {self.model_var.get()}")
        except Exception:
            pass

    def _on_model_changed(self, *_args):
        """下拉换模型：若目标 provider 与当前 gateway 的不同，自动重启 gateway。

        gateway 是单上游代理（一个 --upstream + 一份 model-map），所以换 provider
        必须重启，否则客户端会用新模型名去打旧上游（表现为 400 unsupported model）。
        """
        if self._suppress_model_trace or self._closing:
            return
        if self._sending:
            self._set_status("正在生成回复，模型切换会在本轮结束后生效", "info")
            return
        if not (self.gateway_proc and self.gateway_proc.poll() is None):
            return                      # 没在跑就不用管，下次启动自然用新模型
        new_label = self.model_var.get()
        current = getattr(self, "_gateway_provider", None) or {}
        self._sync_model_chip()
        if served_model_label(current) == new_label:
            return
        target = select_provider(self.user_rows, new_label)
        if target is None:
            return
        self._set_status(f"切换模型为 {new_label}，正在重启 gateway …", "info")
        self._restart_gateway()

    def _autostart_gateway(self):
        """打开窗口后的自动启动（失败按退避重试，不再「一次失败就永久离线」）。"""
        self._autostart_after_id = None
        if self._closing or self._gateway_user_stopped:
            # 定时器入口也要拦：用户在退避期间点了「停止」，重试就不能再拉起。
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        if self.gateway_proc and self.gateway_proc.poll() is None:
            return
        self._autostart_attempts += 1
        self._gateway_autostarted = True
        if self._start_gateway(autostart=True):
            self._autostart_attempts = 0
            self._autostart_log("gateway 已拉起")
            return
        self._schedule_autostart_retry(self.status_var.get() or "启动失败")

    def _autostart_log(self, message: str) -> None:
        """把自启过程落到 ~/.forge/gui/autostart.log。

        下次再有人说「它没自己起来」，这里能看到到底停在哪一步。
        """
        try:
            path = self._autostart_log_path
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp}  {message}\n")
            lines = path.read_text(encoding="utf-8").splitlines()[-200:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _schedule_autostart_retry(self, reason: str) -> None:
        """自启/自恢复失败后的退避重试。

        原实现的注释写着「配置好目录后会自动重试」，但根本没有重试：
        一次失败就永久停在离线，用户只能手点。现在真的重试。
        """
        if self._closing or self._gateway_user_stopped:
            # 用户主动停过：已排队的重试也要作废，不能偷偷把 gateway 又拉起来。
            self._autostart_log("重试已作废（窗口关闭 / 用户已手动停止）")
            return
        if not _autostart_enabled():
            # FORGE_NO_AUTOSTART=1（测试 / 需要手动控制的场景）就不该自己重试
            return
        index = max(0, self._autostart_attempts - 1)
        if index >= len(AUTOSTART_RETRY_DELAYS):
            self._autostart_log(f"重试 {index} 次仍未起来，暂停自动重试：{reason}")
            self.gw_status_var.set("● 离线")
            self._set_status(f"gateway 自动启动失败（{reason}）；"
                             f"可点「启动」重试，或先选好模型 / Provider", "warn")
            return
        delay = AUTOSTART_RETRY_DELAYS[index]
        self._autostart_log(f"第 {self._autostart_attempts} 次尝试没起来（{reason}），"
                            f"{delay:g}s 后重试")
        self.gw_status_var.set(f"● 重试中({delay:g}s)")
        try:
            self._autostart_after_id = self.root.after(int(delay * 1000),
                                                       self._autostart_gateway)
        except tk.TclError:
            pass

    def _toggle_gateway(self):
        """主按钮 = 确保 gateway 在运行：在线则重启，离线则启动。"""
        if self._sending:
            self._set_status("正在生成回复，稍后再操作 gateway", "info")
            return
        if self.gateway_proc and self.gateway_proc.poll() is None:
            self._restart_gateway()
        else:
            self._start_gateway()

    def _restart_gateway(self):
        """重启：先停（标记为程序性停止，不触发自动拉起），停完再启动。"""
        if not (self.gateway_proc and self.gateway_proc.poll() is None):
            self._start_gateway()
            return
        self._gateway_user_stopped = True       # 防止 stop 过程被自动拉起打断
        self._restart_pending = True
        self._stop_gateway()

    def _stop_gateway_from_menu(self):
        """用户主动停止：之后不再自动拉起，直到再次启动。"""
        self._gateway_user_stopped = True
        self._restart_pending = False
        self._cancel_autostart_timer()
        self._stop_gateway()
        self._set_status("gateway 已手动停止（再次点击「启动」可恢复）", "warn")

    def _cancel_autostart_timer(self) -> None:
        timer = self._autostart_after_id
        self._autostart_after_id = None
        if timer is None:
            return
        try:
            self.root.after_cancel(timer)
        except (tk.TclError, ValueError):
            pass

    def _start_gateway(self, autostart: bool = False) -> bool:
        """拉起 gateway。返回是否真的起了进程（供自启重试判断）。"""
        if self._closing:
            # 关窗过程中可能有延迟定时器刚到点；别在退出路上又拉一个进程出来。
            return False
        if autostart and self._gateway_user_stopped:
            # 防住「重试回调已进入、用户刚好点击停止」的竞态。
            return False
        if self.gateway_proc and self.gateway_proc.poll() is None:
            return True
        if not self.run_py:
            if autostart:
                # 自动启动场景不弹目录选择框，只提示
                self._gateway_down("未找到 Forge 目录（请点「选择 Forge 目录」后再启动）")
                self._set_status("未找到 Forge 目录，gateway 未启动；装入目录后会自动重试",
                                 "warn")
                return False
            self._choose_forge_repo()
            if not self.run_py:
                return False
        # 走到这里表示确实要启动：清掉「用户停过」的标记
        self._gateway_user_stopped = False
        try:
            port = int(self.port_var.get())
        except ValueError:
            self._set_status("端口须为 1024–65535 的整数", "warn")
            self._gateway_down("端口非法，未启动")
            return False
        if not 1024 <= port <= 65535:
            self._set_status("端口须为 1024–65535 的整数", "warn")
            self._gateway_down("端口非法，未启动")
            return False
        self.gateway_port = port
        self.gateway_url = f"http://127.0.0.1:{port}"
        self.client.base_url = self.gateway_url

        # 端口如果被「本程序遗留的 gateway」占着，先清掉——否则新进程起不来，
        # 而健康检查会连到旧进程，UI 显示「在线」却用的是过期配置。
        self._clear_stale_gateway_on_port(port)

        # 探活：secret_store（GUI 本地密钥库）注入 env，gateway 子进程继承
        env = {**os.environ, **env_for()}
        upstream = select_provider(self.user_rows, self.model_var.get())
        try:
            upstream_url, upstream_key, upstream_model = gateway_settings(upstream, env)
        except ValueError as exc:
            self._set_status(str(exc), "warn")
            self._gateway_down("未配置可用 Provider，未启动")
            return False
        env["FORGE_GATEWAY_KEY"] = upstream_key
        # 客户端看到的模型名（= UI 下拉里的那个，可能是友好别名），
        # 与上游真实模型名分开：别名发给客户端，真实名由 --model-map 替换。
        served_model = served_model_label(upstream) or upstream_model
        model_map = (f"{served_model}={upstream_model}"
                     if served_model and served_model != upstream_model else "")
        # Keep credentials in the child environment, never command-line arguments.
        cmd = [_python_exe(), str(self.run_py), "gateway",
               "--upstream", upstream_url, "--upstream-wire", "openai",
               "--client-wire", "openai", "--models", served_model,
               "--port", str(port), "--home", str(self.home)]
        if model_map:
            cmd.extend(["--model-map", model_map])

        if port_in_use(port):
            # 清理后仍被占：照旧拉起（交给健康检查/看门狗判断），但记一笔便于事后排查。
            self._set_status(f"端口 {port} 仍被占用，gateway 可能启动失败", "warn")
            self._autostart_log(f"端口 {port} 清理后仍被占用，仍尝试启动")
        else:
            self._set_status(f"启动 gateway：{' '.join(cmd[-4:])} ...", "info")

        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace", cwd=str(self.run_py.parent))
        if IS_WINDOWS:
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        try:
            self.gateway_proc = subprocess.Popen(cmd, env=env, **kwargs)
            self._gateway_provider = copy.deepcopy(upstream)
        except Exception as e:
            self._set_status(f"启动失败：{e}", "error")
            self._gateway_down("启动失败")
            return False

        self.gw_status_var.set("● 启动中")
        self.gw_btn.configure(state=tk.DISABLED, text="启动中…")
        self.port_spin.configure(state=tk.DISABLED)
        proc = self.gateway_proc
        probe = ForgeGatewayClient(self.gateway_url)
        threading.Thread(target=self._drain_gateway_log, args=(proc,), daemon=True).start()
        threading.Thread(target=self._gateway_watchdog, args=(proc, probe), daemon=True).start()
        return True

    def _first_active_provider(self) -> dict | None:
        for r in self.user_rows:
            conf = r.get("config") or {}
            if "baseURL" in conf and "model" in conf and not r.get("disabled"):
                return conf
        return None

    def _clear_stale_gateway_on_port(self, port: int) -> None:
        """若目标端口被一个「forge gateway」进程占着，先把它请走（含子进程）。

        整个函数对异常免疫：这段是尽力而为的清理，任何失败都不该影响启动流程
        （测试环境里 subprocess 常被 mock，更不该把异常抛给调用方）。
        """
        try:
            self._clear_stale_gateway_on_port_inner(port)
        except Exception:
            pass

    def _clear_stale_gateway_on_port_inner(self, port: int) -> None:
        if not IS_WINDOWS:
            return
        # 先做零成本判断：端口空着就直接返回，不必去问 Windows
        if not port_in_use(port):
            return
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                 f"-ErrorAction SilentlyContinue | Select-Object -First 1 "
                 f"-ExpandProperty OwningProcess)"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return
        if not out.isdigit():
            return
        pid = int(out)
        try:
            info = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
                 f"-ErrorAction SilentlyContinue).CommandLine"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        if "run.py" not in (info or "") or "gateway" not in (info or ""):
            # 不是我们的 gateway，别动（可能是用户自己的服务）
            self._set_status(f"端口 {port} 被其它程序占用（PID {pid}），gateway 可能起不来", "warn")
            return
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._push_terminal(f"[gateway] 已清理占用端口 {port} 的旧进程（PID {pid}）")
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _popup_more_menu(self, event=None):
        try:
            x = self._more_btn.winfo_rootx()
            y = self._more_btn.winfo_rooty() + self._more_btn.winfo_height() + 2
            self._more_menu.tk_popup(x, y)
        finally:
            try:
                self._more_menu.grab_release()
            except tk.TclError:
                pass

    def _popup_session_menu(self, event=None):
        """将低频操作归到统一浮层，保留完整的原有配置入口。"""
        show_popover_menu(self.session_menu_btn, [
            {"label": "重命名对话", "command": self._rename_session},
            {"label": "新建对话", "detail": "Ctrl+N", "command": self._new_session},
            {"separator": True},
            {"label": "上下文与附件", "command": self._open_context},
            {"label": f"回复温度 · {self.temp_var.get()}",
             "command": self._open_temperature_menu},
            {"label": "打开配置", "command": lambda: self._show_view("config")},
        ], title="对话操作", width=320)

    def _open_temperature_menu(self):
        show_popover_menu(self.session_menu_btn, [
            {"label": value, "selected": self.temp_var.get() == value,
             "command": lambda v=value: self._set_temperature(v)}
            for value in ("0.2", "0.5", "0.7", "1.0", "1.5")
        ], title="回复温度", width=240)

    def _set_temperature(self, value: str):
        self.temp_var.set(value)
        self._set_status(f"温度已设为 {value}", "info")

    def _open_file_ref(self, ref: str) -> bool:
        """聊天里点到文件引用：展开工作区 → 打开该文件 → 有改动则同时显示 diff。"""
        if not ref:
            return False
        ws = getattr(self, "workspace", None)
        if ws is None:
            self._set_status("工作区面板不可用，无法打开该文件", "warn")
            return False
        target, line = split_file_ref(ref)
        self._open_workspace("file_tree")
        opened = False
        for call in ("reveal_file", "open_file"):
            fn = getattr(ws, call, None)
            if not callable(fn):
                continue
            try:
                if call == "reveal_file":
                    fn(target, line=line)
                else:
                    fn(target)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            self._set_status(f"无法在工作区打开 {target}", "warn")
            return False
        # 有未提交改动时优先给 diff（用户点引用多半就是想看改了什么）
        try:
            rel = relative_to_repo(ws, target)
            summary = ws.workspace_summary()
            if rel and rel in set(summary.get("recent") or []):
                ws.show_diff(rel)
                self._set_status(f"{rel} 有未提交改动，已显示 diff", "info")
        except Exception:
            pass
        return True

    def _popup_gw_menu(self, event):
        try:
            self._gw_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._gw_menu.grab_release()

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
        if self.gateway_proc is not proc or proc.poll() is None:
            return
        code = proc.returncode
        self._gateway_down(f"进程已退出（代码 {code}）")
        if self._closing:
            return
        if getattr(self, "_restart_pending", False):
            # 这是「重启」流程里的停止，紧接着启动
            self._restart_pending = False
            self._gateway_user_stopped = False
            try:
                self._autostart_after_id = self.root.after(300, self._autostart_gateway)
            except tk.TclError:
                pass
            return
        if self._gateway_user_stopped:
            return
        # 意外退出 → 自动拉起（60 秒内最多 5 次，退避递增）
        now = time.monotonic()
        self._gateway_restart_times = [t for t in self._gateway_restart_times
                                       if now - t < 60]
        if len(self._gateway_restart_times) >= 5:
            self._set_status("gateway 反复退出，已暂停自动重启；请检查配置/端口占用", "error")
            return
        self._gateway_restart_times.append(now)
        delay = min(30, 2 ** len(self._gateway_restart_times))
        self.gw_status_var.set(f"● 重启中({delay}s)")
        self._set_status(f"gateway 退出（代码 {code}），{delay} 秒后自动重启", "warn")
        try:
            self._autostart_after_id = self.root.after(
                int(delay * 1000), self._autostart_gateway)
        except tk.TclError:
            pass

    def _gateway_stop_failed(self, proc):
        if self.gateway_proc is proc:
            self.gw_btn.configure(state=tk.NORMAL, text="⟳ 重启")
            self.gw_status_var.set("● 停止失败")
            self._set_status("未能停止 gateway，请重试", "error")

    def _gateway_timeout(self, proc):
        if self.gateway_proc is proc and proc.poll() is None:
            self.gw_status_var.set("● 未就绪")
            self.gw_status_lbl.configure(fg=C["warn"])
            self.gw_detail_status_lbl.configure(fg=C["warn"])
            self.gw_btn.configure(text="⟳ 重启", bg=C["accent"],
                                  state=tk.DISABLED if self._sending else tk.NORMAL)
            self._set_status("gateway 启动未就绪；可点「重启」重试，或检查端口/配置", "warn")
            # 起来但一直不健康，多半是端口/配置问题：收掉这个进程再退避重试，
            # 否则它会一直挂着，而用户看到的就是「打开了但没起来」。
            if self._gateway_autostarted and not self._gateway_user_stopped:
                self._autostart_log("启动后 30s 未就绪，收掉进程并重试")
                try:
                    # 要传进程对象：kill_process_tree 会调 proc.poll()，
                    # 只传 pid 会抛 AttributeError，清理实际不会发生。
                    kill_process_tree(proc)
                except Exception:
                    pass
                self.gateway_proc = None
                # 超时也是一次失败尝试：不计数就会出现 index=-1（取到最后一档 20s）
                # 且永远碰不到次数上限。
                self._autostart_attempts += 1
                self._schedule_autostart_retry("启动未就绪")

    def _gateway_up(self, proc):
        if self.gateway_proc is not proc or proc.poll() is not None:
            return
        self.gw_status_var.set(f"● 在线 ({self.gateway_port})")
        self.gw_status_lbl.configure(fg=C["ok"])
        self.gw_detail_status_lbl.configure(fg=C["ok"])
        self.gw_btn.configure(text="⟳ 重启", bg=C["surface2"], fg=C["body"],
                              state=tk.DISABLED if self._sending else tk.NORMAL)
        self.port_spin.configure(state=tk.DISABLED if self._sending else tk.NORMAL)
        self._set_status(f"gateway 在线：{self.gateway_url}", "ok")

    def _gateway_down(self, reason: str = "已停止"):
        self.gw_status_var.set("● 离线")
        self.gw_status_lbl.configure(fg=C["muted"])
        self.gw_detail_status_lbl.configure(fg=C["muted"])
        self.gw_btn.configure(text="▶ 启动", bg=C["accent"], fg="#FFFFFF",
                              state=tk.DISABLED if self._sending else tk.NORMAL)
        self.port_spin.configure(state=tk.DISABLED if self._sending else tk.NORMAL)
        self._set_status(f"gateway {reason}", "warn")
        self.gateway_proc = None

    def _stop_gateway(self):
        if not self.gateway_proc:
            return
        proc = self.gateway_proc
        self.gw_btn.configure(state=tk.DISABLED, text="停止中…")
        self._stopping_proc = proc
        self.gw_status_var.set("● 停止中")
        def terminate():
            try:
                kill_process_tree(proc)
            finally:
                try:
                    gone = proc.poll() is not None
                except Exception:
                    gone = True
                self._post_ui(self._gateway_exited if gone
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

    # ── Agent 集群 / 子 Agent 分工（移植自 AI Platform） ──

    def _plan_sidecars(self, cfg: dict, prompt: str) -> dict | None:
        """决定本轮要不要跑 sidecar；返回计划或 None。

        语义照抄 route.ts：
          - 子 Agent 分工开启且有预设 → 走分工
          - 否则集群开启 → 走集群（1-4 路同构方案）
          - 两者都开时分工优先（互斥，防双份注入）
        """
        try:
            subs = team.enabled_sub_agent_presets(cfg)
        except Exception:
            subs = []
        if subs:
            context = None
            if cfg.get("memory_mode") == "unified":
                context = self._agent_shared_context()
            return {"kind": "sub", "presets": subs, "context": context}
        cluster = cfg.get("cluster") or {}
        if cluster.get("enabled"):
            context = None
            if cfg.get("memory_mode") == "unified":
                context = self._agent_shared_context()
            return {"kind": "cluster", "count": int(cluster.get("count") or 2),
                    "lanes": list(cluster.get("lanes") or []), "context": context}
        return None

    @staticmethod
    def _find_provider_by_model(user_rows: list[dict], model_id: str):
        """按真实模型 id 反查 provider config（子任务模型可能属于另一家）。"""
        if not model_id:
            return None
        for row in user_rows:
            conf = row.get("config") or {}
            if "baseURL" in conf and str(conf.get("model")) == model_id:
                if not row.get("disabled"):
                    return conf
        return None

    def _agent_shared_context(self) -> str:
        """unified 记忆模式：把最近对话拼成共享上下文（简单摘要，不打模型）。"""
        lines = []
        for msg in list(self._chat_history)[-6:]:
            who = "用户" if msg.role == "user" else "Forge"
            lines.append(f"{who}: {msg.content[:400]}")
        return "（最近对话背景）\n" + "\n".join(lines) if lines else ""

    def _run_sidecars(self, plan: dict, cfg: dict, cancel_event) -> str:
        """在 worker 线程里执行子任务；返回注入文本（"" 表示无注入）。

        返回前把每路状态用 _post_ui 刷到聊天消息行上（工具卡片形式）。
        """
        env = {**os.environ, **env_for()}
        default_provider = select_provider(self.user_rows, self.model_var.get())
        try:
            default_model = str((default_provider or {}).get("model") or "")
        except Exception:
            default_model = ""
        kind = plan["kind"]
        context = plan.get("context")

        if kind == "sub":
            agents = team.build_sub_agents(plan["presets"],
                                           self._last_sidecar_prompt,
                                           context_text=context)
            if not agents:
                return ""
            # 模型反查 provider：preset 指定的模型可能属于另一家 provider
            for a in agents:
                if a.provider is None:
                    a.provider = self._find_provider_by_model(
                        self.user_rows, a.model_name) or default_provider
                    if a.provider is None and a.model_name:
                        # 模型不属于任何已配置 provider：回落主模型，
                        # 避免把 A 家模型名发到 B 家的 baseURL
                        a.model_name = None
            self._post_ui(self._set_request_status,
                          f"子任务并行 {len(agents)} 路…")
            results = team.run_sub_agents(agents, env,
                                          default_provider=default_provider,
                                          default_model=default_model)
            injection = team.sub_agent_injection(results)
        else:
            lanes = []
            for lane in plan.get("lanes") or []:
                provider_id = lane.get("provider")
                provider = None
                if provider_id:
                    provider = next(
                        (r.get("config") for r in self.user_rows
                         if r.get("id") == provider_id and "baseURL" in
                         (r.get("config") or {})), None)
                lane_model = lane.get("model") or ""
                if lane_model and provider is None:
                    provider = self._find_provider_by_model(
                        self.user_rows, lane_model)
                    if provider is None:
                        lane_model = ""   # 回落主模型，保持 model/provider 一致
                lanes.append({"provider": provider, "model": lane_model})
            self._post_ui(self._set_request_status,
                          f"Agent 集群 {plan['count']} 路并行…")
            results = team.run_cluster(
                self._last_sidecar_prompt, plan["count"], lanes, env,
                default_provider=default_provider,
                default_model=default_model, context_text=context)
            injection = team.cluster_injection(results)

        if cancel_event is not None and cancel_event.is_set():
            return ""
        # UI：把每路结果做成工具卡片挂在本轮消息上
        rows = []
        for r in results:
            rows.append({
                "name": f"{r.role}",
                "desc": (r.model_used or default_model) +
                        ("" if r.ok else f" · {r.error[:60]}"),
                "elapsed": f"{r.duration_ms / 1000:.1f}s",
                "ok": r.ok,
            })
        title = ("子 Agent 分工" if kind == "sub" else "Agent 集群")
        self._post_ui(self._post_sidecar_card, title, rows)
        return injection if any(r.ok for r in results) else ""

    def _post_sidecar_card(self, title: str, rows: list[dict]):
        msg = getattr(self, "_agent_msg", None)
        if msg is None or not rows:
            return
        try:
            msg.add_tool_card(rows, title=title, expanded=False)
        except Exception:
            pass

    def _do_send(self):
        if self._sending:
            return
        text = self.send_var.get().strip()
        if not text:
            return
        if self._run_local_command(text):
            return
        try:
            prompt = compose_prompt(text, self._attachments)
        except ValueError as exc:
            self._set_status(str(exc), "warn")
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
        self._abort_requested = False
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event
        # 重构后 send_var 不再绑 entry.textvariable，要分别清空。
        try:
            if self.input_card is not None and self.input_card.entry is not None:
                self.input_card.entry.delete("1.0", tk.END)
        except (tk.TclError, AttributeError):
            pass
        self.send_var.set("")
        self._chat_empty = False
        self._last_sidecar_prompt = prompt   # compose 后的完整 prompt（含附件）
        self.chat_area.add_user(prompt)
        self._agent_msg = self.chat_area.add_agent(app=self)
        self._agent_msg.set_status("生成中…")
        self._agent_msg.stream_text("")
        messages = (list(self._chat_history) if self._include_history else []) + [ChatMessage("user", prompt)]
        retained_history = list(self._chat_history) + [ChatMessage("user", prompt)]
        self.input_card.set_busy(True)
        self.clear_chat_btn.configure(state=tk.DISABLED)
        self.gw_btn.configure(state=tk.DISABLED)
        self.port_spin.configure(state=tk.DISABLED)
        self._set_request_status("正在连接…")
        self._set_status(f"请求 → {self.model_var.get()} …", "info")

        model = self.model_var.get()
        # ── Agent 集群 / 子 Agent 分工（移植自 AI Platform sub-agent.ts）──
        # 分工优先，与集群互斥（照抄 route.ts：同开会双份子调用+双份注入）。
        team_cfg = team.load_config()
        sidecar_plan = self._plan_sidecars(team_cfg, prompt)
        if self.gateway_proc is not None and getattr(self, "_gateway_provider", None):
            provider = select_provider(self.user_rows, model)
            running = self._gateway_provider
            if provider is None or any(provider.get(k) != running.get(k)
                                       for k in ("baseURL", "wire", "apiKey")):
                self._chat_failed("所选模型的 Provider 与正在运行的 Gateway 不同，请停止并重新启动 Gateway", text)
                self._send_finished()
                return
            model = str(provider["model"])
        client = self.client

        def worker():
            try:
                ok, msg = client.health()
                if not ok:
                    raise GatewayError(f"gateway 未连接：{msg}。请先启动 gateway 后重试。")
                if cancel_event.is_set():
                    raise GenerationCancelled("已停止生成")
                # 子任务并行（阻塞 worker 线程即可，不卡 UI；UI 状态由 _post_ui 刷）
                if sidecar_plan is not None:
                    if cancel_event.is_set():
                        raise GenerationCancelled("已停止生成")
                    injection = self._run_sidecars(sidecar_plan, team_cfg,
                                                   cancel_event)
                    if injection:
                        # 插入本轮 messages 的最后一条 user 之前（AI Platform 协议）
                        insert_at = max(0, len(messages) - 1)
                        messages.insert(insert_at,
                                        ChatMessage("system", injection))
                    if cancel_event.is_set():
                        raise GenerationCancelled("已停止生成")
                self._post_ui(self._set_request_status, "正在生成…")
                acc: list[str] = []

                def on_chunk(piece: str):
                    if cancel_event.is_set():
                        raise GenerationCancelled("已停止生成")
                    acc.append(piece)
                    self._post_ui(self._append_stream_delta, piece)

                client.stream_chat(
                    messages, model=model,
                    temperature=temp,
                    reasoning_effort=("high" if self._reasoning_effort == "contemplate"
                                      else None if self._reasoning_effort == "off"
                                      else self._reasoning_effort),
                    on_chunk=on_chunk, cancel_event=cancel_event,
                )
                full = "".join(acc)
                if cancel_event.is_set():
                    raise GenerationCancelled("已停止生成")
                self._post_ui(self._chat_succeeded, retained_history, full, text)
            except GenerationCancelled:
                self._post_ui(self._chat_cancelled, text)
            except Exception as e:
                error_text = f"{type(e).__name__}: {e}"
                self._post_ui(self._chat_failed, error_text, text)
            finally:
                self._post_ui(self._send_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _chat_succeeded(self, messages, full, original_prompt=None):
        if self._abort_requested:
            self._chat_cancelled(original_prompt if original_prompt is not None else messages[-1].content)
            return
        self._chat_history = messages + [ChatMessage("assistant", full)]
        self._attachments.clear()
        self._update_context_summary()
        self.chat_title_var.set(self._session_title())
        msg = getattr(self, "_agent_msg", None)
        if msg is not None:
            msg.set_status("")
            msg.render_markdown(full or "（空回复）")
            actions = [{"label": "打开工作区",
                        "command": lambda: self._open_workspace("file_tree")}]
            actions.extend(self._file_actions(full))
            msg.add_actions(actions)
        self._archive_current_session()
        self._refresh_history()
        self._set_status("回复完成", "ok")

    def _chat_cancelled(self, prompt):
        if self._agent_msg is not None:
            self._agent_msg.set_status("")
            self._agent_msg.add_note("已停止。未完成回复未加入后续上下文。", tone="warn")
        if not self.send_var.get():
            self.send_var.set(prompt)
        self._set_status("已停止生成，原消息已保留", "warn")

    def _chat_failed(self, message, prompt):
        if self._abort_requested:
            self._chat_cancelled(prompt)
            return
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
        self._cancel_event.set()
        self._set_request_status("正在停止…")
        self._set_status("已请求停止；等待网络返回或超时，期间不会发送新请求", "warn")
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
        try:
            self.model_combo.close_menu()
        except (AttributeError, tk.TclError):
            pass
        if self._responsive_after_id is not None:
            try:
                self.root.after_cancel(self._responsive_after_id)
            except (tk.TclError, ValueError):
                pass
            self._responsive_after_id = None
        self._cancel_autostart_timer()
        self._cancel_event.set()
        task_cancel = getattr(self, "_task_cancel_event", None)
        if task_cancel is not None:
            task_cancel.set()
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
        processes = []
        for proc in (getattr(self, "_task_proc", None), self.gateway_proc):
            try:
                alive = proc is not None and proc.poll() is None
            except Exception:
                alive = False
            if alive and all(existing is not proc for existing in processes):
                processes.append(proc)
        if not processes:
            self.root.destroy()
            return

        # 先把整个 Forge 界面（包括浮层）从桌面上拿走，再在后台完成有界清理。
        # 主循环保留到子进程确认退出，避免 root 一毁掉，清理回调也跟着消失。
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        finished = threading.Event()

        def terminate_children():
            try:
                for proc in processes:
                    if not kill_process_tree(proc):
                        # 极短重试处理 taskkill 返回和 Popen 状态刷新之间的窗口。
                        kill_process_tree(proc, timeout=2.0)
            finally:
                finished.set()

        def finish_close():
            if not finished.is_set():
                try:
                    self.root.after(40, finish_close)
                except tk.TclError:
                    pass
                return
            try:
                self.root.destroy()
            except tk.TclError:
                pass

        threading.Thread(target=terminate_children, daemon=False).start()
        finish_close()


# ─── 入口 ──────────────────────────────────────────────


def _diagnose() -> int:
    """打印/落盘运行环境诊断（打包后无控制台时看 exe 旁的 forge-diagnose.json）。"""
    info = {
        "version": APP_VERSION,
        "frozen": FROZEN,
        "app_dir": str(APP_DIR),
        "module_dir": str(HERE),
        "sys_executable": sys.executable,
        "python_exe": _python_exe(),
        "run_py": str(_find_run_py() or ""),
        "forge_home": str(DEFAULT_FORGE_HOME),
        "config_path": str(_desktop_config_path()),
        "config_data": load_desktop_config(),
    }
    # 品牌标志是否随包带上了（打包态最容易漏的资源）
    try:
        logo_path = None
        for n in ("forge-logo-104.png", "forge-logo-78.png", "forge-logo-52.png"):
            found = _asset_path(n)
            if found is not None:
                logo_path = found
                break
        info["brand_logo"] = str(logo_path) if logo_path else ""
        info["brand_logo_ok"] = bool(logo_path)
        if logo_path is not None:
            root = tk.Tk()
            root.withdraw()
            img, _keep = load_brand_logo(26)
            info["brand_logo_px"] = f"{img.width()}x{img.height()}" if img else ""
            root.destroy()
    except Exception as exc:
        info["brand_logo_error"] = f"{type(exc).__name__}: {exc}"
    # 模型品牌标志：漏一个就会在界面上「有的模型没图标」，打包后一眼可查
    try:
        have = [b.key for b in brand_marks.BRANDS if brand_marks.mark_path(b, 16) is not None]
        info["brand_marks_ok"] = len(have)
        info["brand_marks_total"] = len(brand_marks.BRANDS)
        info["brand_marks_missing"] = [b.key for b in brand_marks.BRANDS
                                       if b.key not in set(have)]
    except Exception as exc:
        info["brand_marks_error"] = f"{type(exc).__name__}: {exc}"
    text = json.dumps(info, ensure_ascii=False, indent=2)
    try:
        print(text)
    except Exception:
        pass
    try:
        (APP_DIR / "forge-diagnose.json").write_text(text, encoding="utf-8")
    except OSError:
        pass
    return 0


def main():
    if "--diagnose" in sys.argv:
        raise SystemExit(_diagnose())
    _setup_dpi()
    root = tk.Tk()
    app = ForgeGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
