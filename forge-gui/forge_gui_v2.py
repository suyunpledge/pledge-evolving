"""forge 图形界面 v3：Agent-native 四层桌面工作区。

设计结构：
    · 顶栏：品牌 / 当前项目 / 必要运行状态；详细 telemetry 按需展开
    · Sidebar：对话主导航与可折叠目录，其余页面显示当前功能的上下文
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

import i18n
from i18n import tr

import copy
import json
import os
import platform
import queue
import shlex
import re
import shutil
import socket
import subprocess
import sys
import threading
import tempfile
import time
import uuid
import tkinter as tk
from ui_icons import IconCanvas, IconButton, icon_image
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk
from i18n import dialogs as messagebox

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent))  # host Forge registry, never plugin directories

from config_model import (  # noqa: E402
    ConfigNormalizeError,
    load_user_layer,
    merge_with_user_layer,
    normalize,
    save_user_layer,
)
from secret_store import env_for, load as _load_secrets  # noqa: E402
from forge.secrets import redact, install_logging_redaction
install_logging_redaction()
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
    bind_keyboard_action,
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
try:  # 插件市场 / 工具市场（缺失时工具集视图退化为只读提示）
    from plugin_market import (  # noqa: E402
        KIND_LABELS,
        PERMISSION_LABELS, GRANTABLE_CAPABILITIES, workspace_key,
        Marketplace,
        build_tool_catalog,
    )
    from plugin_capabilities import CapabilityRuntime  # noqa: E402
    _MARKET_IMPORT_ERROR = ""
except Exception as _mk_exc:  # pragma: no cover
    Marketplace = None  # type: ignore[assignment]
    build_tool_catalog = None  # type: ignore[assignment]
    KIND_LABELS = {}  # type: ignore[assignment]
    CapabilityRuntime = None  # type: ignore[assignment]
    PERMISSION_LABELS, GRANTABLE_CAPABILITIES = {}, frozenset()
    _MARKET_IMPORT_ERROR = str(_mk_exc)

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
GATEWAY_LAUNCH_TIMEOUT_MS = 30_000

IS_WINDOWS = platform.system() == "Windows"

# 主导航（顶栏与侧栏共用；key -> (标签, 图标)）
# Navigation icons are semantic vectors, independent of the system emoji font.
NAV_ITEMS = [
    ("chat", tr("对话"), "💬"),
    ("task", tr("任务"), "✅"),
    ("agents", "Agents", "🤖"),
    ("tools", tr("工具 / 插件市场"), "🧰"),
    ("knowledge", tr("知识库"), "📚"),
    ("evolution", tr("演化"), "🧬"),
    ("files", tr("文件与项目"), "📁"),
    ("config", tr("配置"), "⚙️"),
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
THINKING_LABELS = {"off": tr("关闭"), "smart": tr("智能"), "on": tr("开启")}
PLANNING_LABELS = {"high": tr("事前规划：高"), "medium": tr("事前规划：中"),
                   "low": tr("事前规划：低"), "none": tr("事前规划：无")}
THINKING_CHOICES = [
    ("off", tr("关闭"), "不启用沉思"),
    ("smart", tr("智能"), "按任务复杂度自动决定（推荐）"),
    ("on", tr("开启"), "始终启用沉思"),
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


_DESKTOP_CONFIG_LOCK = threading.RLock()


def load_desktop_config() -> dict:
    # Windows readers do not grant delete sharing. Serialize local readers with
    # atomic replacement, and keep read/modify/write updates together.
    with _DESKTOP_CONFIG_LOCK:
        path = _desktop_config_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def save_desktop_config(**updates) -> bool:
    with _DESKTOP_CONFIG_LOCK:
        return _save_desktop_config_locked(updates)


def _save_desktop_config_locked(updates) -> bool:
    path = _desktop_config_path()
    data = load_desktop_config()
    data.update(updates)
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".desktop-", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


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


def bindable_gateway_port(port: int) -> int:
    """Connect probes miss Windows excluded ports: verify binding as well.

    Only permission denial permits an OS-selected fallback. An occupied port
    remains an error, so we cannot accidentally connect to somebody else's service.
    The child still owns the final bind and may fail if another process races it.
    """
    if port == 0:
        # OS 分配一个可用端口（被占降级 / 首次启动都走这里）
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if IS_WINDOWS:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", port))
        return port
    except PermissionError as exc:
        if getattr(exc, "winerror", None) != 10013:
            raise
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


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
        from forge.secrets import install_tk_exception_redaction
        install_tk_exception_redaction(root)
        self.root = root
        desktop_prefs = load_desktop_config()
        self.root._forge_locale = i18n.Translator(desktop_prefs.get("language", "zh-CN"))
        self._set_localized_title()
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*MIN_SIZE)
        self.root.configure(bg=C["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 状态
        self.run_py = _find_run_py()
        self._project_workspace = (Path(desktop_prefs['project_workspace'])
                                   if isinstance(desktop_prefs.get('project_workspace'),str) and desktop_prefs['project_workspace'] else None)
        self.home = DEFAULT_FORGE_HOME
        self.user_layer_path = _probe_user_layer()
        self._load_error = ""
        favorites = desktop_prefs.get("model_favorites", [])
        self._model_favorites = [v for v in favorites if isinstance(v, str) and v.strip()] if isinstance(favorites, list) else []
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
        self._gateway_starting = False
        self._gateway_launch_cancel = threading.Event()
        self._gateway_launch_after_id = None
        self._gateway_launch_lock = threading.Lock()
        self._gateway_pending_procs = []
        self._gateway_ready_proc = None
        self._suppress_model_trace = False
        try:
            saved_port = int(desktop_prefs.get("gateway_port", 8799))
            self.gateway_port = saved_port if 1024 <= saved_port <= 65535 else 8799
        except (TypeError, ValueError):
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
        self._session_id = "s" + uuid.uuid4().hex
        self._agent_msg = None
        self._task_msg = None
        self._task_proc = None
        self._task_running = False
        self._task_started = 0.0
        self._sysmon = None
        self._feature_entries: list[dict] = []
        self._features_expanded = False
        self._feature_dirty = False
        self._team_dirty = False
        self._sending = False
        self._abort_requested = False
        self._closing = False
        self._ui_events = queue.Queue()
        self._background_jobs = {}
        self._editor_baseline = copy.deepcopy(self.user_rows)

        self._build_ui()
        self._team_dirty = False
        self._update_team_feedback()
        self._refresh_provider_list()
        self._set_status(self._load_error or tr("就绪"),
                         "error" if self._load_error else "info")
        self._event_poll = self.root.after(40, self._drain_ui_events)
        from desktop_features import DesktopFeatures
        self.desktop_features = DesktopFeatures(self)

    def _set_localized_title(self):
        self.root.title(i18n.resolve(tr("forge — v{version}（对话 · 任务 · 工作区）",
                                       version=APP_VERSION), self.root))

    def _change_language(self, code):
        code = i18n.normalize_language(code)
        locale = self.root._forge_locale
        try:
            saved = save_desktop_config(language=code)
        except (OSError, ValueError):
            saved = False
        if not saved:
            self.language_var.set(i18n.LANGUAGES[locale.language])
            self._set_status(tr("语言设置保存失败，请重试。"), "error")
            return False
        locale.switch(code)
        if hasattr(self,'_team_inherit'):
            dirty=self._team_dirty
            old=self._team_inherit
            self._team_inherit=i18n.resolve(tr('跟随主模型'),self.root)
            for combo in self._team_lane_combo:
                combo.configure(values=[self._team_inherit,*self._team_choices])
                if combo._model_var.get()==old: combo._model_var.set(self._team_inherit)
            for row in self._team_preset_rows:
                if not row['row'].winfo_exists(): continue
                row['model_combo'].configure(values=[self._team_inherit,*self._team_choices])
                if row['model_var'].get()==old: row['model_var'].set(self._team_inherit)
            self._team_dirty=dirty
            self._update_team_feedback()
        self.language_var.set(i18n.LANGUAGES[code])
        self._set_localized_title()
        self._sync_view_navigation()
        self._set_status(tr("界面语言已切换为 {language}", language=i18n.LANGUAGES[code]))
        return True

    def _post_ui(self, callback, *args):
        if not self._closing:
            self._ui_events.put((callback, args))

    def _submit_background(self, kind, work, apply):
        """Tk-owned scheduling: one reader per kind, latest request wins."""
        if self._closing:
            return
        job = self._background_jobs.setdefault(kind, {
            "generation": 0, "running": False, "pending": None})
        job["generation"] += 1
        job["pending"] = (job["generation"], work, apply)
        if not job["running"]:
            self._launch_background(kind, job)

    def _launch_background(self, kind, job):
        generation, work, apply = job["pending"]
        job["pending"] = None
        job["running"] = True

        def worker():
            try:
                result, error = work(), None
            except Exception as exc:
                result, error = None, exc
            self._post_ui(self._finish_background, kind, generation, apply, result, error)

        job['thread'] = threading.Thread(target=worker, daemon=True, name=f"Forge-{kind}")
        job['thread'].start()

    def _finish_background(self, kind, generation, apply, result, error):
        job = self._background_jobs[kind]
        job["running"] = False
        try:
            if not self._closing and generation == job["generation"]:
                if error is None:
                    apply(result)
                else:
                    if kind == "market":
                        self.market_stat_var.set(tr("市场读取失败，可点击刷新重试"))
                    if kind == "market-action":
                        self._market_action_busy = False
                    if kind == "market-sync":
                        self._market_sync_busy = False
                    self._set_status(f"读取 {kind} 失败：{error}", "warn")
        finally:
            if not self._closing and job["pending"] is not None:
                self._launch_background(kind, job)

    def _drain_ui_events(self):
        deadline = time.monotonic() + 0.012
        for _ in range(200):
            try:
                callback, args = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())
            if time.monotonic() >= deadline:
                break
        if not self._closing:
            self._event_poll = self.root.after(40, self._drain_ui_events)

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
        self._view_history = []
        self._ws_packed = False
        self._last_workspace_tab = "file_tree"
        self._workspace_auto_hidden = False
        self._sidebar_visible = True
        self._sidebar_user_hidden = False
        self._sidebar_auto_hidden = False
        self._sidebar_force_open = False
        self._responsive_after_id = None
        self._metric_labels: dict[str, i18n.Label] = {}
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

        self._build_view_navigation(center)
        self._build_views(center)
        self._build_workspace()

        # ── 底部状态栏 ──
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill=tk.X, side=tk.BOTTOM)
        bot = tk.Frame(self.root, bg=C["bg"], height=theme.ui_px(self.root, 28))
        bot.pack(fill=tk.X, side=tk.BOTTOM)
        bot.pack_propagate(False)
        self.status_var = i18n.StringVar(self.root, value="")
        self.status_lbl = i18n.Label(bot, textvariable=self.status_var, bg=C["bg"],
                                   fg=C["body"], font=FONT_CAPTION, anchor=tk.W,
                                   padx=16)
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.path_lbl = i18n.Label(bot, text=self._status_label_text(), bg=C["bg"],
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
        self.root.bind("<Alt-Left>", self._navigate_back, add="+")
        self.root.bind("<Escape>", self._navigation_escape, add="+")
        # 像 AutoClaw 一样：打开窗口就把 gateway 拉起来（等 UI 建好再起，
        # 免得抢启动时间、也免得状态栏还没就绪）。
        # FORGE_NO_AUTOSTART=1 可关闭（测试用）。
        # 切模型 → 必要时换 provider（gateway 一次只服务一个上游）
        self.model_var.trace_add("write", self._on_model_changed)
        if _autostart_enabled():
            # 先把状态标成「启动中」：打包版解包要几秒，这段时间界面不能看着像没反应。
            self.gw_status_var.set(tr("● 启动中…"))
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
        bar = tk.Frame(chrome, bg=C["bg"], height=theme.ui_px(self.root, 50))
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        self.sidebar_reveal_btn = glyph_button(
            bar, "☰", self._toggle_sidebar, bg=C["bg"], size=11,
            tooltip=tr("展开 / 收起侧边栏"))
        self.sidebar_reveal_btn.pack(side=tk.LEFT, padx=(10, 0))

        # 品牌区
        brand = tk.Frame(bar, bg=C["bg"])
        brand.pack(side=tk.LEFT, padx=(14, 12))
        brand_img, brand_keep = load_brand_logo(24, master=self.root)
        if brand_img is not None:
            holder = tk.Frame(brand, bg=C["bg"])
            holder.pack(side=tk.LEFT, padx=(0, 8))
            shown = i18n.Label(holder, image=brand_img, bg=C["bg"], bd=0,
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
        i18n.Label(brand, text="FORGE", bg=C["bg"], fg=C["text"],
                 font=(theme.UI_FAMILY, 12, "bold")).pack(side=tk.LEFT)

        self._project_chip = tk.Frame(bar, bg=C["surface2"], padx=9, pady=4)
        self._project_chip.pack(side=tk.LEFT, padx=(8, 0))
        IconCanvas(self._project_chip, "files", size=18, bg=C["surface2"], fg=C["accent2"]).pack(
            side=tk.LEFT, padx=(0, 5))
        self._project_name_var = tk.StringVar(value=self._active_workspace().name)
        i18n.Label(self._project_chip, textvariable=self._project_name_var,
                 bg=C["surface2"], fg=C["subtext"], font=FONT_SMALL).pack(side=tk.LEFT)

        # 保留「更多」能力合约，可见入口移到 Activity Bar 底部。
        self._more_menu = i18n.Menu(self.root, tearoff=0, bg=C["surface"],
                                  fg=C["text"], activebackground=C["accent_soft"],
                                  activeforeground=C["accent_text"],
                                  font=FONT_SMALL, bd=1, relief=tk.FLAT)
        self._more_menu.add_command(label=tr("状态详情"),
                                    command=self._toggle_telemetry)
        self._more_menu.add_command(label=tr("收起 / 展开侧边栏"),
                                    command=self._toggle_sidebar)
        self._more_menu.add_separator()
        self._more_menu.add_command(label=NAV_LABEL["task"], command=lambda: self._show_view("task"))
        self._more_menu.add_command(label=tr("项目文件"), command=lambda: self._nav_click("files"))
        self._more_menu.add_command(label=tr('定时任务'), command=lambda:self.desktop_features.open(0))
        self._more_menu.add_command(label=tr('连接器'), command=lambda:self.desktop_features.open(1))
        self._more_menu.add_command(label=tr('代码预览'), command=lambda:self.desktop_features.open(2))
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
        self.desktop_features_btn = pill_button(right,tr('连接与任务'),lambda:self.desktop_features.open(),
                                                kind='ghost',icon='◷',bg=C['bg'],font=FONT_SMALL,padx=8)
        self.gw_status_var = i18n.StringVar(self.root, value=tr("● 离线"))
        self.gw_status_lbl = i18n.Label(right, textvariable=self.gw_status_var,
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
            self._telemetry_btn.configure(text=tr("▴ 状态"))
        else:
            self._telemetry_panel.pack_forget()
            self._telemetry_btn.configure(text=tr("▾ 状态"))

    def _build_metric(self, parent, key: str, text: str):
        base = parent.cget("bg")
        box = tk.Frame(parent, bg=base)
        box.pack(side=tk.LEFT, padx=(0, 12))
        row = tk.Frame(box, bg=base)
        row.pack(anchor=tk.W)
        i18n.Label(row, text=text, bg=base, fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        value = i18n.Label(row, text="—", bg=base, fg=C["ter"], font=FONT_MICRO)
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
        self.gw_detail_status_lbl = i18n.Label(card, textvariable=self.gw_status_var,
                                             bg=base, fg=C["muted"],
                                             font=FONT_MICRO)
        self.gw_detail_status_lbl.pack(side=tk.LEFT, padx=(0, 5))
        i18n.Label(card, text="Gateway", bg=base, fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.gateway_port))
        self.port_spin = tk.Spinbox(card, from_=1024, to_=65535, width=5,
                                    textvariable=self.port_var, font=FONT_MONO_XS,
                                    bg=base, fg=C["ter"], bd=0,
                                    buttonbackground=base, relief=tk.FLAT,
                                    insertbackground=C["accent"],
                                    highlightthickness=0, justify=tk.CENTER)
        self.port_spin.pack(side=tk.LEFT, padx=(4, 8))
        self.gw_btn = IconButton(card, text=tr("▶ 启动"), command=self._toggle_gateway,
                                bg=base, fg=C["accent_text"],
                                activebackground=C["hover"],
                                activeforeground=C["text"], font=FONT_MICRO,
                                relief=tk.FLAT, bd=0, padx=7, pady=1,
                                cursor="hand2", highlightthickness=0)
        self.gw_btn.pack(side=tk.LEFT)
        # 常驻语义下主按钮=确保运行；停止放在右键菜单里
        self._gw_menu = i18n.Menu(self.root, tearoff=0, bg=C["surface"],
                                fg=C["text"], activebackground=C["accent_soft"],
                                activeforeground=C["accent_text"],
                                font=FONT_SMALL, bd=1, relief=tk.FLAT)
        self._gw_menu.add_command(label=tr("重启 gateway"), image=icon_image(self.root, "refresh"), compound=tk.LEFT,
                                  command=self._toggle_gateway)
        self._gw_menu.add_separator()
        # 读范围：只影响「读」，不动写沙箱。默认 workspace 与 balanced 预设一致
        # （行为零变化）；选「全部磁盘」就是「写锁在工作区、读可到处读」。
        self._read_scope_var = tk.StringVar(
            value=str(load_desktop_config().get("read_scope", "workspace")))
        self._gw_menu.add_radiobutton(
            label=tr("读取范围：仅工作区"), value="workspace",
            variable=self._read_scope_var,
            command=lambda: self._set_read_scope("workspace"))
        self._gw_menu.add_radiobutton(
            label=tr("读取范围：全部磁盘"), value="all",
            variable=self._read_scope_var,
            command=lambda: self._set_read_scope("all"))
        self._gw_menu.add_separator()
        self._gw_menu.add_command(label=tr("停止 gateway"), image=icon_image(self.root, "stop"), compound=tk.LEFT,
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
        text = i18n.Label(inner, text=label, bg=C["activity"], fg=C["ter"],
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
            holder.configure(bg=bg, highlightbackground=bg)
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

        side = tk.Frame(parent, bg=C["sidebar"], width=theme.ui_px(parent, SIDEBAR_WIDTH))
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        self.sidebar = side
        self._sidebar_pack_options = side.pack_info()

        self._sidebar_panels: dict[str, tk.Frame] = {}
        chat_panel = tk.Frame(side, bg=C["sidebar"])
        self._sidebar_panels["chat"] = chat_panel

        # Stable primary navigation remains accessible from task and settings views.
        shortcuts = tk.Frame(chat_panel, bg=C["sidebar"])
        shortcuts.pack(fill=tk.X, padx=8, pady=(14, 10))
        self.sidebar_shortcuts = {}
        for column, (key, label, action) in enumerate((
                ("chat", tr("对话"), self._return_to_chat),
                ("tools", tr("插件"), self._open_plugin_market),
                ("schedules", tr("定时任务"), lambda: self.desktop_features.open(0)))):
            shortcuts.grid_columnconfigure(column, weight=1, uniform="shortcuts")
            button = pill_button(shortcuts, label, action, kind="ghost",
                                 bg=C["sidebar"], font=FONT_MICRO, padx=2, width=1)
            button.grid(row=0, column=column, sticky="nsew", padx=1)
            self.sidebar_shortcuts[key] = button
        def fit_shortcuts(event):
            # Use the fixed container width. Deriving each button's request from
            # its own grid allocation can oscillate at fractional DPI scaling.
            width = max(20, event.width // 3 - 10)
            for button in self.sidebar_shortcuts.values():
                if int(button.cget("wraplength")) != width:
                    button.configure(wraplength=width)
        shortcuts.bind("<Configure>", fit_shortcuts)

        # ── 2) 功能导航 ──
        nav_host = tk.Frame(chat_panel, bg=C["sidebar"])
        nav_host.pack(fill=tk.X, padx=6, pady=(0, 4))
        i18n.Label(nav_host, text=tr("功能导航"), bg=C["sidebar"], fg=C["muted"],
                 font=FONT_MICRO, anchor="w").pack(fill=tk.X, padx=8, pady=(2, 6))
        self.sidebar_navigation = {}
        for key, label, glyph in (("connectors", tr("连接器"), "connector"),
                                  ("knowledge", NAV_LABEL["knowledge"], "knowledge"),
                                  ("evolution", NAV_LABEL["evolution"], "evolution"),
                                  ("config", tr("配置API"), "config")):
            holder = self._make_side_nav(nav_host, key, label, glyph)
            holder.pack(fill=tk.X, pady=1)
            self.sidebar_navigation[key] = holder
        # 更多入口：Agents / 知识库 / 演化 / 文件与项目 / 配置
        more_holder = self._make_side_nav_more(chat_panel)
        more_holder.pack(fill=tk.X, padx=6, pady=(2, 6))

        # 导航与历史之间：全宽分隔线 + 上下留白（视觉分层，别混成一块）

        divider(chat_panel, bg=C["border_hi"]).pack(fill=tk.X, padx=0, pady=(12, 6))

        # ── 4) 最近对话（时间分组）──
        head = tk.Frame(chat_panel, bg=C["sidebar"])
        head.pack(fill=tk.X, padx=14, pady=(10, 4))
        # 分组标题强化，跟导航项形成层级差
        i18n.Label(head, text=tr("历史记录"), bg=C["sidebar"], fg=C["ter"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        glyph_button(head, "搜索", self._toggle_session_search, bg=C["sidebar"],
                     fg=C["muted"], size=9, tooltip=tr("搜索对话 · Ctrl+K")).pack(
            side=tk.RIGHT)
        self._search_visible = False
        self.session_search_var = tk.StringVar()
        self.session_search = tk.Entry(
            chat_panel, textvariable=self.session_search_var,
            bg=C["surface2"], fg=C["text"], bd=0, relief=tk.FLAT,
            insertbackground=C["accent"], font=FONT_SMALL, highlightthickness=1,
            highlightbackground=C["border_hi"], highlightcolor=C["accent"])
        self.session_search_var.trace_add("write", lambda *_: self._refresh_history())

        self.history_area = cw.ScrollArea(chat_panel, bg=C["sidebar_history"], pady=6)
        self.history_area.pack(fill=tk.BOTH, expand=True, padx=6)
        self.history_box = self.history_area.inner

        # ── 5) 底部固定：模型选择 + 设置 + 关于 + 收起 ──
        foot_divider = divider(chat_panel, bg=C["border"])
        bottom = tk.Frame(chat_panel, bg=C["sidebar"])
        # Fixed actions reserve their space before the expanding history canvas.
        bottom.pack(fill=tk.X, padx=10, pady=(6, 8), side=tk.BOTTOM, before=self.history_area)
        foot_divider.pack(fill=tk.X, padx=12, side=tk.BOTTOM, before=self.history_area)

        model_row = tk.Frame(bottom, bg=C["sidebar"])
        # Keep the legacy accessor, with the actual visible selector in the composer.
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
        pill_button(tools_row, tr("ⓘ 关于"), self._show_about, kind="quiet",
                    bg=C["sidebar"], font=FONT_MICRO, padx=8).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        self.sidebar_toggle_btn = glyph_button(
            tools_row, "≪", self._toggle_sidebar, bg=C["sidebar"],
            fg=C["muted"], size=11, tooltip=tr("收起 / 展开侧边栏"))
        self.sidebar_toggle_btn.pack(side=tk.RIGHT)

        # Retain legacy context panel accessors; global navigation stays shared.
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
        bind_keyboard_action(holder, lambda: self._nav_click(key))
        inner = tk.Frame(holder, bg=C["sidebar"])
        inner.pack(fill=tk.X, padx=8, pady=7)
        icon = IconCanvas(inner, glyph, size=20, bg=C["sidebar"], fg=C["ter"])
        icon.pack(side=tk.LEFT)
        text = i18n.Label(inner, text=label, bg=C["sidebar"], fg=C["ter"],
                        font=FONT_SMALL, anchor="w")
        text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        def fit_label(event):
            width = max(40, event.width)
            if int(text.cget("wraplength")) != width:
                text.configure(wraplength=width)
        text.bind("<Configure>", fit_label)
        attach_tooltip(icon, label)
        attach_tooltip(text, label)
        for widget in (holder, inner, icon, text):
            widget.bind("<Button-1>", lambda _e, k=key: self._nav_click(k))
            widget.bind("<Enter>",
                        lambda _e, k=key: self._paint_activity(k, hover=True), add="+")
            widget.bind("<Leave>", lambda _e, k=key: self._paint_activity(k), add="+")
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
        inner.pack(fill=tk.X, padx=8, pady=7)
        icon = IconCanvas(inner, "more", size=20, bg=C["sidebar"], fg=C["muted"])
        icon.pack(side=tk.LEFT)
        text = i18n.Label(inner, text=tr("更多"), bg=C["sidebar"], fg=C["muted"],
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
        bind_keyboard_action(holder, popup)
        return holder

    def _build_agents_group(self, parent):
        """侧栏「智能体」分组：扫描 agents 目录列出行；+ 添加智能体。"""
        agents_root = Path.home() / ".openclaw-autoclaw" / "agents"
        try:
            children = sorted([q for q in agents_root.iterdir() if q.is_dir()],
                              key=lambda q: q.name.lower())
        except OSError:
            children = []
        section = tk.Frame(parent, bg=C["sidebar"])
        section.pack(fill=tk.X, padx=14, pady=(6, 4))
        self._agents_expanded = False
        self._agents_group_count = len(children)
        self._agents_group_header = section
        self._agents_toggle_btn = pill_button(
            section, f"▸ 智能体 · {len(children)}", self._toggle_agents_group,
            kind="quiet", bg=C["sidebar"], font=FONT_CAPTION, padx=0)
        self._agents_toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        attach_tooltip(self._agents_toggle_btn, "展开已有 AutoClaw 智能体目录；Forge 分工预设在 Agents 页面配置")
        plus = glyph_button(section, "＋", self._prompt_create_agent,
                            bg=C["sidebar"], size=11, tooltip=tr("添加 Forge 分工预设"))
        plus.pack(side=tk.RIGHT)

        outer = tk.Frame(parent, bg=C["sidebar"], height=min(180, 38 * len(children) + 42))
        outer.pack_propagate(False)
        self._agents_list_frame = outer
        scroll = cw.ScrollArea(outer, bg=C["sidebar"], padx=0, pady=0)
        scroll.pack(fill=tk.BOTH, expand=True)
        list_box = scroll.inner
        for q in children:
            row = tk.Frame(list_box, bg=C["sidebar"], cursor="hand2")
            row.pack(fill=tk.X, pady=1, padx=2)
            IconCanvas(row, "model", size=18, bg=C["sidebar"], fg=C["accent2"]).pack(
                side=tk.LEFT, padx=(8, 6))
            i18n.Label(row, text=q.name, bg=C["sidebar"], fg=C["subtext"],
                     font=FONT_SMALL, anchor="w").pack(side=tk.LEFT, pady=3,
                                                       fill=tk.X, expand=True)
            attach_tooltip(row, str(q))
            bind_keyboard_action(row, lambda path=str(q): self._show_agent_info(path))
            for w in (row, *row.winfo_children()):
                w.bind("<Button-1>",
                       lambda _e, path=str(q): self._show_agent_info(path))
                w.bind("<Enter>", lambda _e, r=row: r.configure(bg=C["hover"]), add="+")
                w.bind("<Leave>", lambda _e, r=row: r.configure(bg=C["sidebar"]), add="+")
        if not children:
            i18n.Label(list_box, text=tr("还没有智能体"), bg=C["sidebar"],
                     fg=C["muted"], font=FONT_MICRO, anchor="w",
                     padx=8).pack(fill=tk.X, pady=4)
        add = IconButton(list_box, text=tr("＋ 添加智能体"), bg=C["sidebar"], fg=C["ter"],
                         font=FONT_MICRO, anchor="w", padx=8, relief=tk.FLAT, bd=0,
                         cursor="hand2", command=lambda: self._prompt_create_agent())
        add.pack(fill=tk.X, pady=(4, 2))

    def _toggle_agents_group(self):
        self._agents_expanded = not self._agents_expanded
        if self._agents_expanded:
            self._agents_list_frame.pack(fill=tk.X, padx=6, after=self._agents_group_header)
        else:
            self._agents_list_frame.pack_forget()
        arrow = "▾" if self._agents_expanded else "▸"
        self._agents_toggle_btn.configure(text=f"{arrow} 智能体 · {self._agents_group_count}")

    def _prompt_create_agent(self):
        """Use the existing Forge subagent preset editor; save remains explicit."""
        self._show_view("agents")
        self._team_add_preset({"id": f"sub-{time.time_ns()}", "role": "worker", "system_prompt": ""})
        self._team_dirty = True
        self._set_status("已添加分工预设；填写角色与提示词后点击保存，可在本页启用子 Agent 分工", "info")

    def _show_agent_info(self, path):
        agent_path = Path(path)
        if not agent_path.is_dir():
            self._set_status("智能体目录已不存在，请刷新侧边栏", "warn")
            return
        messagebox.showinfo(tr("智能体目录"), f"{agent_path.name}\n{agent_path}\n\n这是现有 AutoClaw 智能体目录。Forge 的分工预设在 Agents 页面配置。", parent=self.root)

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
        i18n.Label(head, text=label, bg=C["sidebar"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        i18n.Label(panel, text=descriptions.get(key, ""), bg=C["sidebar"],
                 fg=C["muted"], font=FONT_SMALL, justify=tk.LEFT,
                 anchor="w", wraplength=SIDEBAR_WIDTH - 28).pack(
            fill=tk.X, padx=14, pady=(0, 18))
        section = i18n.Label(panel, text=tr("当前视图"), bg=C["sidebar"],
                           fg=C["muted"], font=FONT_MICRO, anchor="w")
        section.pack(fill=tk.X, padx=14, pady=(0, 6))
        row = tk.Frame(panel, bg=C["sel"], padx=10, pady=8)
        row.pack(fill=tk.X, padx=8)
        i18n.Label(row, text=label, bg=C["sel"], fg=C["text"],
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
        # Opening a page must not replace the global navigation with a dead end.
        panel = self._sidebar_panels.get("files" if key == "files" else "chat")
        for other in self._sidebar_panels.values():
            if other is not panel:
                other.pack_forget()
        if panel is not None and not panel.winfo_manager():
            panel.pack(fill=tk.BOTH, expand=True)

    def _set_sidebar_visible(self, visible: bool, *, automatic=False):
        if visible == self._sidebar_visible:
            return
        if visible:
            # pack_forget discards both options and position. Reuse the same
            # widget/dock options before the permanent main split, as at startup.
            self.sidebar.pack(**self._sidebar_pack_options, before=self.split)
            self.sidebar_toggle_btn.configure(text="≪")
        else:
            self._sidebar_pack_options = self.sidebar.pack_info()
            self.sidebar.pack_forget()
            self.sidebar_toggle_btn.configure(text="≫")
        self._sidebar_visible = visible
        if not automatic:
            self._sidebar_user_hidden = not visible
            self._sidebar_auto_hidden = False

    def _toggle_sidebar(self):
        width = self.root.winfo_width()
        self._sidebar_force_open = not self._sidebar_visible and (
            self._active_view != "chat" or width < SIDEBAR_COLLAPSE_AT or
            (self._ws_packed and width < WORKSPACE_RESTORE_AT))
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
        self._cancel_history_render()
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
        # 2026-10-11 用户要求：任何模式下侧边栏都在。只有窗口过窄才收起。
        should_hide_sidebar = not self._sidebar_force_open and (
            width < SIDEBAR_COLLAPSE_AT or
            (self._ws_packed and width < WORKSPACE_RESTORE_AT))
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
    def _build_view_navigation(self, center):
        """Keep an exit outside each page's scrolling/collapsible content."""
        bar = self.view_navigation = tk.Frame(center, bg=C["surface2"], padx=12, pady=6)
        bar.grid_columnconfigure(1, weight=1)
        self.view_back_btn = glyph_button(bar, tr("◀ 返回对话"), self._navigate_back,
                                          bg=C["surface2"], fg=C["text"], size=10,
                                          tooltip=tr("返回上一页 · Alt+←；保留当前编辑内容"))
        self.view_back_btn.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.view_title_var = i18n.StringVar(self.root)
        self.view_title_label = i18n.Label(bar, textvariable=self.view_title_var,
                                         bg=C["surface2"], fg=C["muted"], font=FONT_CAPTION,
                                         anchor="w")
        self.view_title_label.grid(row=0, column=1, sticky="ew")
        self.view_chat_btn = glyph_button(bar, tr("💬 回到对话"), self._return_to_chat,
                                          bg=C["surface2"], fg=C["text"], size=10,
                                          tooltip=tr("回到当前对话 · Esc；不新建或清空对话"))
        self.view_chat_btn.grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _sync_view_navigation(self):
        files_mode = self._active_view == "chat" and self._active_nav == "files"
        if self._active_view == "chat" and not files_mode:
            self.view_navigation.pack_forget()
            return
        target = "chat" if files_mode or not self._view_history else self._view_history[-1]
        self.view_back_btn.configure(text=tr("◀ 返回{page}", page=NAV_LABEL.get(target, tr("对话"))))
        self.view_title_var.set(NAV_LABEL.get("files" if files_mode else self._active_view, ""))
        if target != "chat":
            self.view_chat_btn.grid()
        else:
            self.view_chat_btn.grid_remove()
        self.view_navigation.pack(fill=tk.X, before=self._views[self._active_view])

    def _navigate_back(self, _event=None):
        if self.root.grab_current() is not None:
            return None
        if self._active_view == "chat":
            if self._active_nav == "files":
                self._return_to_chat()
                return "break"
            return None
        target = self._view_history.pop() if self._view_history else "chat"
        self._show_view(target, record_history=False)
        return "break"

    def _return_to_chat(self):
        self._view_history.clear()
        self._show_view("chat", record_history=False)

    def _navigation_escape(self, event):
        if self.root.grab_current() is not None or event.widget.winfo_toplevel() is not self.root:
            return None
        if self._active_view != "chat" or self._active_nav == "files":
            self._return_to_chat()
            return "break"
        return None

    def _build_views(self, center):
        self.tab_client = tk.Frame(center, bg=C["chat"])
        self._build_client_tab(self.tab_client)

        self.tab_task = tk.Frame(center, bg=C["chat"])
        self._build_task_view(self.tab_task)

        self.tab_features = tk.Frame(center, bg=C["bg"])
        # 工具集视图 = 两个并列 tab：市场（新增）/ 功能开关（原有）。
        # 默认停在功能开关，保持既有行为与测试断言不变。
        seg = tk.Frame(self.tab_features, bg=C["bg"], padx=20)
        seg.pack(fill=tk.X, pady=(10, 0))
        self._tools_tab_buttons = {}
        for key, label in (("market", tr("🛒 市场")), ("features", tr("⚙ 功能开关"))):
            btn = pill_button(seg, label, lambda k=key: self._switch_tools_tab(k),
                              kind="ghost", bg=C["bg"])
            btn.pack(side=tk.LEFT, padx=(0, 8))
            self._tools_tab_buttons[key] = btn
        self._tools_tab = "features"

        # 市场面板惰性构建：没点开「市场」tab 之前一个控件都不建，
        # 默认路径（功能开关）保持与加市场之前完全一样的开销。
        self.market_holder = tk.Frame(self.tab_features, bg=C["bg"])
        self._market_built = False

        self.feature_holder = tk.Frame(self.tab_features, bg=C["bg"], padx=20, pady=2)
        host = self.feature_holder
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
        self._lazy_views = set()
        for key in ("agents", "knowledge", "evolution", "files"):
            frame = tk.Frame(center, bg=C["bg"])
            if key == "agents":
                # agents 视图 = 真实配置面板（集群/分工/记忆模式）
                self._build_agents_team_panel(frame)
            else:
                # Keep frame parents/order stable, defer unused controls until
                # first navigation instead of delaying the initial chat window.
                self._lazy_views.add(key)
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
        theme.bind_scoped_wheel(canvas, body)
        self._agents_panel_canvas = canvas

        # ── 所有 provider 的真模型 id（去重，作为下拉候选项）──
        from phase_client import model_catalog
        self._team_choices = {label: pair for pair, label in model_catalog(self.user_rows)}
        self._team_inherit = i18n.resolve(tr("跟随主模型"),self.root)
        model_ids = [self._team_inherit, *self._team_choices]

        # ── 头部说明 ──
        i18n.Label(body, text=tr("Agent 集群与分工"),
                 bg=C["bg"], fg=C["text"], font=FONT_TITLE,
                 anchor="w").pack(fill=tk.X, padx=20, pady=(18, 2))
        i18n.Label(body,
                 text=tr("为对话并行准备方案或分工结果，再交给主模型汇总。"
                      "选择一种模式；启用后会增加模型调用次数。"),
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
        i18n.Label(head1, text=tr("Agent 集群"), bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        description1 = i18n.Label(c1, text=tr("并行准备多份方案，交给主模型综合对比"),
                 bg=C["surface"], fg=C["muted"],
                 font=FONT_SMALL, anchor="w", justify=tk.LEFT)
        description1.pack(fill=tk.X, pady=(4, 0))
        description1.bind("<Configure>", lambda e: description1.configure(wraplength=max(120, e.width)))
        self._team_cluster_var = tk.BooleanVar(
            value=bool(cfg["cluster"].get("enabled")))
        i18n.Checkbutton(head1, text=tr("启用"), variable=self._team_cluster_var,
                       command=lambda: self._team_select_mode("cluster"),
                       bg=C["surface"], fg=C["text"], selectcolor=C["sel"],
                       activebackground=C["surface"], activeforeground=C["text"],
                       font=FONT_SMALL).pack(side=tk.RIGHT)

        count_row = tk.Frame(c1, bg=C["surface"])
        count_row.pack(fill=tk.X, pady=(8, 4))
        i18n.Label(count_row, text=tr("方案数"), bg=C["surface"], fg=C["ter"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 8))
        self._team_cluster_count = tk.IntVar(
            value=max(1, min(4, int(cfg["cluster"].get("count") or 2))))
        for n in (1, 2, 3, 4):
            i18n.Radiobutton(count_row, text=str(n), variable=self._team_cluster_count,
                           value=n, bg=C["surface"], fg=C["text"],
                           selectcolor=C["sel"], activebackground=C["surface"],
                           activeforeground=C["text"], font=FONT_SMALL).pack(
                side=tk.LEFT, padx=(0, 6))
        i18n.Label(count_row, text=tr("（每路可选不同模型）"), bg=C["surface"],
                 fg=C["muted"], font=FONT_MICRO).pack(side=tk.LEFT)

        # 每路模型下拉（最多 4 路，按当前 count 显示）
        self._team_lane_combo: list[ttk.Combobox] = []
        lanes_cfg = list(cfg["cluster"].get("lanes") or [])
        lane_box = tk.Frame(c1, bg=C["surface"])
        lane_box.pack(fill=tk.X, pady=(4, 0))
        for i in range(4):
            row = tk.Frame(lane_box, bg=C["surface"])
            row.pack(fill=tk.X, pady=1)
            i18n.Label(row, text=f"方案 {i + 1}", bg=C["surface"], fg=C["ter"],
                     font=FONT_SMALL, width=8, anchor="w").pack(side=tk.LEFT)
            var = tk.StringVar()
            lane = lanes_cfg[i] if i < len(lanes_cfg) else {}
            initial = self._team_selection_label(lane)
            var.set(initial)
            combo = ttk.Combobox(row, textvariable=var, values=model_ids,
                                 state="readonly", font=FONT_SMALL)
            combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            self._team_lane_combo.append(combo)
            combo._lane_row = row
            self._team_lane_combo[-1]._model_var = var  # 取值用

        # ── 2) 子 Agent 分工 ──
        card2 = RoundedCard(body, radius=R_CARD, fill=C["surface"],
                            outline=C["border_hi"], padx=16, pady=14, bg=C["bg"])
        card2.pack(fill=tk.X, padx=20, pady=(0, 12))
        c2 = card2.content
        head2 = tk.Frame(c2, bg=C["surface"])
        head2.pack(fill=tk.X)
        i18n.Label(head2, text=tr("子 Agent 分工"), bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        description2 = i18n.Label(c2, text=tr("按不同角色协作，交给主模型汇总各自结果"),
                                bg=C["surface"], fg=C["muted"], font=FONT_SMALL,
                                anchor="w", justify=tk.LEFT)
        description2.pack(fill=tk.X, pady=(4, 0))
        description2.bind("<Configure>", lambda e: description2.configure(wraplength=max(120, e.width)))
        self._team_subs_var = tk.BooleanVar(
            value=bool(cfg["sub_agents"].get("enabled")))
        i18n.Checkbutton(head2, text=tr("启用"), variable=self._team_subs_var,
                       command=lambda: self._team_select_mode("subs"),
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
        i18n.Label(mem_row, text=tr("记忆模式"), bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        self._team_mem_var = tk.StringVar(
            value=cfg.get("memory_mode") or "isolated")
        for val, label, tip in (("isolated", "独立上下文", "子任务只接收本轮任务，减少上下文开销"),
                                ("unified", "共享近期对话", "额外传入最近六条对话的截取内容")):
            i18n.Radiobutton(mem_row, text=label, variable=self._team_mem_var,
                           value=val, bg=C["surface"], fg=C["text"],
                           selectcolor=C["sel"], activebackground=C["surface"],
                           activeforeground=C["text"],
                           font=FONT_SMALL).pack(side=tk.LEFT, padx=(10, 0))
            attach_tooltip(mem_row.winfo_children()[-1], tip)

        communication = cfg.get('communication') or {}
        self._team_communication_var = tk.BooleanVar(value=communication.get('enabled') is True)
        self._team_communication_rounds = tk.IntVar(value=max(1,min(3,int(communication.get('rounds') or 1))))
        comm = tk.Frame(body, bg=C['surface'], padx=16, pady=12,
                        highlightthickness=1, highlightbackground=C['border_hi'])
        comm.pack(fill=tk.X, padx=20, pady=(0,12))
        i18n.Checkbutton(comm, text=tr('允许 Agent 相互交流'), variable=self._team_communication_var,
                         bg=C['surface'], fg=C['text'], selectcolor=C['sel'], font=FONT_SMALL).pack(anchor='w')
        i18n.Label(comm, text=tr('默认关闭；开启后交换实际结果，最多三轮，会增加模型调用费用。'),
                   bg=C['surface'], fg=C['muted'], font=FONT_SMALL, wraplength=600).pack(anchor='w', pady=6)
        ttk.Spinbox(comm, from_=1, to=3, textvariable=self._team_communication_rounds, width=4).pack(anchor='w')
        self._team_communication_var.trace_add('write', self._mark_team_dirty)
        self._team_communication_rounds.trace_add('write', self._mark_team_dirty)
        # ── 4) 保存 ──
        foot = tk.Frame(parent, bg=C["bg"])
        foot.pack(side=tk.BOTTOM, fill=tk.X, padx=20, pady=12, before=holder)
        self._team_save_btn = pill_button(foot, tr("保存配置"), self._team_save, kind="primary",
                                         bg=C["bg"], font=FONT_UI)
        self._team_save_btn.pack(side=tk.RIGHT, padx=(12, 0))
        self._team_feedback_var = i18n.StringVar(self.root)
        self._team_feedback_label = i18n.Label(foot, textvariable=self._team_feedback_var,
                                            bg=C["bg"], fg=C["muted"], font=FONT_CAPTION,
                                            anchor="w", justify=tk.LEFT)
        self._team_feedback_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._team_feedback_label.bind("<Configure>", lambda event:
            self._team_feedback_label.configure(wraplength=max(120, event.width)))
        for variable in (self._team_cluster_var, self._team_cluster_count,
                         self._team_subs_var, self._team_mem_var,
                         *(combo._model_var for combo in self._team_lane_combo)):
            variable.trace_add("write", self._mark_team_dirty)
        self._team_cluster_count.trace_add("write", self._team_update_lanes)
        self._team_update_lanes()

    def _team_update_lanes(self, *_args):
        count = self._team_cluster_count.get()
        for index, combo in enumerate(self._team_lane_combo):
            if index < count:
                combo._lane_row.pack(fill=tk.X, pady=1)
            else:
                combo._lane_row.pack_forget()

    def _team_select_mode(self, mode):
        if mode == "cluster" and self._team_cluster_var.get():
            self._team_subs_var.set(False)
        elif mode == "subs" and self._team_subs_var.get():
            self._team_cluster_var.set(False)

    def _update_team_feedback(self):
        if not hasattr(self, "_team_feedback_var"):
            return
        dirty = self._team_dirty
        self._team_feedback_var.set("有未保存修改 · 保存后下一轮对话生效" if dirty
                                    else "已保存 · 下一轮对话使用此配置")
        self._team_feedback_label.configure(fg=C["warn"] if dirty else C["muted"])
        self._team_save_btn.configure(state=tk.NORMAL if dirty else tk.DISABLED,
                                      bg=C["accent"] if dirty else C["surface2"])

    def _mark_team_dirty(self, *_args):
        self._team_dirty = True
        self._update_team_feedback()

    def _team_add_preset(self, preset: dict):
        """分工预设一行：role + 模型下拉 + prompt + 删除。"""
        if sum(bool(row['row'].winfo_exists()) for row in self._team_preset_rows)>=team.MAX_SUB_AGENTS:
            self._set_status(tr('最多六个子 Agent'),'warn')
            return
        box = self._team_preset_box
        row = tk.Frame(box, bg=C["surface"], highlightthickness=1,
                       highlightbackground=C["border_hi"])
        row.pack(fill=tk.X, pady=2)
        model_ids = [self._team_inherit, *self._team_choices]

        top = tk.Frame(row, bg=C["surface"])
        top.pack(fill=tk.X, padx=8, pady=(6, 2))
        top.grid_columnconfigure(1, weight=1)
        i18n.Label(top, text=tr("角色"), bg=C["surface"], fg=C["muted"],
                 font=FONT_CAPTION).grid(row=0, column=0, sticky="w", padx=(0, 8))
        role_var = tk.StringVar(value=str(preset.get("role") or "worker"))
        tk.Entry(top, textvariable=role_var, width=14, bg=C["input_bg"],
                 fg=C["text"], font=FONT_SMALL, relief=tk.FLAT, bd=0,
                 insertbackground=C["accent"], highlightthickness=1,
                 highlightbackground=C["border_hi"],
                 highlightcolor=C["accent"]).grid(row=0, column=1, sticky="ew", ipady=3,
                                                  padx=(0, 8))
        i18n.Label(top, text=tr("模型"), bg=C["surface"], fg=C["muted"],
                 font=FONT_CAPTION).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        model_var = tk.StringVar(value=self._team_selection_label(preset))
        combo = ttk.Combobox(top, textvariable=model_var, values=model_ids,
                             state="readonly", width=24, font=FONT_SMALL)
        combo.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        enabled_var = tk.BooleanVar(value=bool(preset.get("enabled", True)))
        i18n.Checkbutton(top, text=tr("参与"), variable=enabled_var, bg=C["surface"],
            fg=C["text"], selectcolor=C["sel"], activebackground=C["surface"],
            font=FONT_SMALL).grid(row=0, column=2)
        def remove():
            row.destroy()
            self._mark_team_dirty()

        del_btn = glyph_button(top, "✕", remove,
                               bg=C["surface"], fg=C["muted"], size=10,
                               tooltip=tr("删除这条预设"))
        del_btn.grid(row=0, column=3, padx=(4, 0))

        prompt_var = tk.StringVar(value=str(preset.get("system_prompt") or ""))
        i18n.Label(row, text=tr("提示词"), bg=C["surface"], fg=C["muted"],
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
             "role_var": role_var, "model_var": model_var, 'model_combo':combo,
             "prompt_var": prompt_var, "enabled_var": enabled_var, "row": row})
        for variable in (role_var, model_var, prompt_var, enabled_var):
            variable.trace_add("write", self._mark_team_dirty)
        self._mark_team_dirty()

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
        try:
            from phase_client import model_catalog
            self._team_choices={label:pair for pair,label in model_catalog(self.user_rows)}
            for combo in self._team_lane_combo:
                self._team_selection_value(combo._model_var.get())
            for row in self._team_preset_rows:
                if row['row'].winfo_exists(): self._team_selection_value(row['model_var'].get())
            self._team_communication_rounds.get()
        except (ValueError,tk.TclError) as exc:
            self._set_status(str(exc),'error')
            return
        cfg = team.load_config()
        # 集群
        lanes = []
        for i, combo in enumerate(self._team_lane_combo):
            if i < self._team_cluster_count.get():
                lanes.append(self._team_selection_value(combo._model_var.get()))
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
                **self._team_selection_value(row["model_var"].get()),
                "enabled": bool(row["enabled_var"].get()),
            })
        cfg["sub_agents"] = {"enabled": bool(self._team_subs_var.get()),
                             "presets": presets}
        cfg["memory_mode"] = self._team_mem_var.get()
        cfg['communication'] = {'enabled':bool(self._team_communication_var.get()),
                                'rounds':max(1,min(3,self._team_communication_rounds.get()))}
        try:
            team.save_config(cfg)
        except OSError as exc:
            self._set_status(f"分工配置保存失败：{exc}", "error")
            self._team_feedback_var.set(tr("保存失败，修改已保留 · 请重试"))
            self._team_feedback_label.configure(fg=C["error"])
            return
        self._team_cfg = cfg
        self._team_dirty = False
        self._update_team_feedback()
        self._set_status(
            f"已保存：集群 {'开' if cfg['cluster']['enabled'] else '关'}"
            f"（{cfg['cluster']['count']} 路）· 分工 "
            f"{'开' if cfg['sub_agents']['enabled'] else '关'}"
            f"（{len(presets)} 预设）· 记忆 {cfg['memory_mode']}", "ok")

    def _team_selection_label(self, selection):
        matches=[label for label,pair in self._team_choices.items()
                 if pair[1] == selection.get('model') and (not selection.get('provider') or pair[0] == selection['provider'])]
        if len(matches)==1: return matches[0]
        if selection.get('model') or selection.get('provider'):
            return str(selection.get('provider') or '?')+' / '+str(selection.get('model') or '?')
        return self._team_inherit

    def _team_selection_value(self, label):
        if label == self._team_inherit: return {'provider':'','model':''}
        pair = self._team_choices.get(label)
        if pair is None: raise ValueError('Agent provider/model is no longer configured')
        return {'provider':pair[0], 'model':pair[1]}

    STUB_TEXT = {
        "agents": ("Agents", "forge 的 Agent 注册表与子 Agent 调度",
                   ("registry.py 里的成员定义", "subagent 派发与回执",
                    "多模型协作（MOA）编排"),
                   "Agent 面板会把 registry 里的成员、能力与最近一次调度画出来。"),
        "knowledge": (tr("知识库"), "长期记忆与策展（memory / curator）",
                      ("会话记忆切片", "策展器打分与淘汰", "分级检索接入"),
                      "知识库面板会列出现有记忆条目、来源与最近命中。"),
        "evolution": (tr("演化"), "自演化迭代账本（evolution / iteration-ledger）",
                      ("迭代记录与指标", "能力包升级", "回归对比"),
                      "演化面板会把 iteration-ledger.jsonl 画成时间线。"),
        "files": (tr("文件与项目"), "在右侧工作区里浏览与改动项目文件",
                  ("文件树 / 变更 / 代码 / diff / 预览 / 终端",),
                  "点上方按钮或调用「打开工作区」即可展开右栏。"),
    }


    # ── 工具集视图：市场（插件 + 工具）─────────────────────────

    def _switch_tools_tab(self, key: str):
        """在市场 / 功能开关之间切换（默认功能开关，改这里不影响默认值）。"""
        if key not in ("market", "features"):
            return
        self._tools_tab = key
        if key != "market":
            self._cancel_market_render()
            self._cancel_market_filter()
        for name, holder in (("market", self.market_holder),
                             ("features", self.feature_holder)):
            if name == key:
                holder.pack(fill=tk.BOTH, expand=True)
            else:
                holder.pack_forget()
        for name, btn in getattr(self, "_tools_tab_buttons", {}).items():
            btn.configure(bg=C["surface2"] if name == key else C["bg"],
                          fg=C["text"] if name == key else C["ter"])
        if key == "market":
            if not getattr(self, "_market_built", False):
                self._build_market_panel(self.market_holder)
                self._market_built = True
            if getattr(self, "_market_snapshot", None) is not None:
                self._render_market(self._market_snapshot)
                if time.monotonic() - getattr(self, "_market_loaded_at", 0) < 2:
                    return
            self._refresh_market()

    def _market_obj(self):
        """惰性拿市场单例；模块缺失时返回 None（视图退化为提示）。"""
        if Marketplace is None:
            return None
        obj = getattr(self, "_market_singleton", None)
        want_external = self._external_sources_enabled()
        if obj is None or bool(getattr(obj, "external_sources", False)) != want_external:
            try:
                obj = Marketplace(home=self.home, external_sources=want_external)
            except Exception as exc:  # pragma: no cover
                self._post_ui(self._set_status, f"市场初始化失败：{exc}", "error")
                return None
            obj.external_sources = want_external
            self._market_singleton = obj
        return obj

    def _external_sources_enabled(self) -> bool:
        """外部生态源（OpenClaw/Claude/DSH/Codex）总开关，存 desktop config。"""
        try:
            cfg = load_desktop_config()
        except Exception:
            return True
        return bool((cfg or {}).get("market_external_sources", True))

    def _toggle_external_sources(self) -> None:
        now = not self._external_sources_enabled()
        try:
            if not save_desktop_config(market_external_sources=now):
                raise OSError("无法保存桌面偏好")
        except Exception as exc:
            self._set_status(f"外部生态源设置保存失败：{exc}", "warn")
            return
        # 重建市场单例并刷新
        self._market_singleton = None
        label = "开（含外部生态）" if now else "关（仅内置）"
        self._set_status(f"外部生态源：{label}", "info")
        self._refresh_market(force=True)

    def _plugin_runtime_obj(self, *, workspace=None, session=None, dispatch=None):
        """Each turn pins its scope/client. Plugin Python is never loaded by the GUI."""
        if CapabilityRuntime is None:
            return None
        market = self._market_obj()
        if market is None:
            return None
        client = self.client
        ws = str(workspace or self._active_workspace())
        sid = str(session if session is not None else self._session_id)
        rt = getattr(self, "_plugin_runtime_singleton", None)
        scope = (ws, sid, dispatch is None)
        if rt is not None and getattr(self, "_plugin_runtime_scope", None) == scope and rt.market is market:
            return rt
        try:
            rt = CapabilityRuntime(market, ws, sid,
                dispatch or (lambda name, args, **context: client.call_tool(name, args, timeout=3.0, **context)))
        except Exception as exc:  # e.g. host forge package unavailable (frozen build)
            self._plugin_runtime_singleton = None
            self._plugin_runtime_scope = None
            self._post_ui(self._set_status, f"插件运行时不可用：{exc}", "warn")
            return None
        self._plugin_runtime_singleton = rt
        self._plugin_runtime_scope = scope
        return rt

    def _reload_plugin_tools(self, *, reserved_names=(), workspace=None, session=None, dispatch=None):
        """按 enabled 状态重载插件工具；返回 (schemas, runtime)。

        schemas 只暴露当前范围已授权的声明式工具；执行交给 Forge 工具桥。
        任何失败都返回 ([], None)，不阻断正常对话。
        """
        try:
            rt = self._plugin_runtime_obj(workspace=workspace, session=session, dispatch=dispatch)
            if rt is None:
                return [], None
            report = rt.reload(reserved_names=reserved_names,
                               available_targets=reserved_names if self.client.plugin_policy_version == 1 else ())
        except Exception as exc:
            self._post_ui(self._set_status, f"插件工具不可用：{exc}；对话继续", "warn")
            return [], None
        if report.errors:
            pid, err = report.errors[0]
            self._post_ui(self._set_status,
                          f"{len(report.errors)} 个插件加载失败：{pid} {err}", "warn")
        return rt.openai_schemas(), rt

    def _build_market_panel(self, parent):
        """市场面板骨架：统计行 + 筛选行 + 滚动列表（内容由 _refresh_market 填）。"""
        head = tk.Frame(parent, bg=C["bg"], padx=20)
        head.pack(fill=tk.X, pady=(10, 0))
        self.market_stat_var = i18n.StringVar(self.root, value=tr("市场未载入"))
        # Pack order = space priority in Tk: buttons first so a long stat text
        # (final count incl. external ecosystems) compresses the label at high
        # DPI instead of squeezing the buttons below their requested width.
        pill_button(head, tr("打开插件目录"), self._open_plugins_dir,
                    kind="quiet", bg=C["bg"]).pack(side=tk.RIGHT)
        pill_button(head, tr("刷新"), lambda: self._refresh_market(force=True),
                    kind="ghost", bg=C["bg"]).pack(side=tk.RIGHT, padx=(0, 8))
        pill_button(head, tr("同步远程源"), self._sync_remote_sources,
                    kind="ghost", bg=C["bg"]).pack(side=tk.RIGHT, padx=(0, 8))
        i18n.Label(head, textvariable=self.market_stat_var, bg=C["bg"], fg=C["subtext"],
                 font=FONT_SMALL).pack(side=tk.LEFT)

        market_actions = tk.Frame(parent, bg=C["bg"], padx=20)
        market_actions.pack(fill=tk.X, pady=(6, 0))
        pill_button(market_actions, tr("安装本地插件"), self._install_local_plugin,
                    kind="ghost", bg=C["bg"]).pack(side=tk.LEFT)
        pill_button(market_actions, tr("查看审计"), self._show_plugin_audit,
                    kind="quiet", bg=C["bg"]).pack(side=tk.LEFT, padx=(8, 0))
        theme.flow_controls(market_actions)
        lifecycle = i18n.Label(parent, text=tr("安装 → 文件检查 → 知悉 → 启用 → 逐项授权 → Policy 判定 → 执行 → 审计 → 撤销"),
                 bg=C["bg"], fg=C["muted"], font=FONT_MICRO, anchor="w",
                 justify=tk.LEFT, wraplength=1)
        lifecycle.pack(fill=tk.X, padx=20, pady=(6, 0))
        theme.bind_wrap(lifecycle)

        filt = tk.Frame(parent, bg=C["bg"], padx=20)
        filt.pack(fill=tk.X, pady=(8, 0))
        self.market_query_var = tk.StringVar(value="")
        self.market_query_var.trace_add(
            "write", lambda *_: self._schedule_market_filter())
        entry = tk.Entry(filt, textvariable=self.market_query_var, bg=C["surface2"],
                         fg=C["text"], insertbackground=C["accent"], relief=tk.FLAT,
                         font=FONT_SMALL, width=22)
        entry.pack(side=tk.LEFT, ipady=3)
        i18n.Label(filt, text=tr("搜索插件"), bg=C["bg"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(6, 12))
        self.market_kind_var = tk.StringVar(value=tr("全部"))
        # 挂 trace 而不是靠在按钮 lambda 里刷新：任何途径改这个变量（按钮、
        # 快捷键、代码）都会触发合并筛选，不会出现「筛选变了列表没变」。
        self.market_kind_var.trace_add("write", lambda *_: self._schedule_market_filter())
        kinds = [tr("全部")] + [tr(KIND_LABELS.get(k, k)) for k in ("tool", "theme", "panel", "integration")]
        kind_row = tk.Frame(parent, bg=C["bg"], padx=20)
        kind_row.pack(fill=tk.X, pady=(6, 0))
        for label in kinds:
            pill_button(kind_row, label, lambda v=label: self.market_kind_var.set(v),
                        kind="ghost", bg=C["bg"]).pack(side=tk.LEFT, padx=(0, 6))
        pill_button(kind_row, tr("生态源"), self._toggle_external_sources,
                    kind="quiet", bg=C["bg"]).pack(side=tk.RIGHT)
        theme.flow_controls(kind_row)

        # 生态筛选（OpenClaw / Claude Code / DSH / Codex）：只影响展示过滤
        eco_row = tk.Frame(parent, bg=C["bg"], padx=20)
        eco_row.pack(fill=tk.X, pady=(4, 0))
        i18n.Label(eco_row, text=tr("生态："), bg=C["bg"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT)
        self.market_eco_var = tk.StringVar(value=tr("全部"))
        self.market_eco_var.trace_add("write", lambda *_: self._schedule_market_filter())
        for eco_label in [tr("全部"), "🦞 OpenClaw", "🎭 Claude Code", "🐳 DSH", "🤖 Codex"]:
            pill_button(eco_row, eco_label, lambda v=eco_label: self.market_eco_var.set(v),
                        kind="ghost", bg=C["bg"]).pack(side=tk.LEFT, padx=(0, 6))
        theme.flow_controls(eco_row)

        viewport = tk.Frame(parent, bg=C["bg"])
        viewport.pack(fill=tk.BOTH, expand=True, padx=20, pady=(10, 14))
        canvas = tk.Canvas(viewport, bg=C["bg"], highlightthickness=0, width=1, height=1)
        scrollbar = ttk.Scrollbar(viewport, orient=tk.VERTICAL, command=canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.market_list = tk.Frame(canvas, bg=C["bg"])
        self._market_window = canvas.create_window(0, 0, window=self.market_list, anchor="nw")
        self.market_list.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            self._market_window, width=e.width))
        theme.bind_scoped_wheel(canvas, self.market_list)
        self.market_canvas = canvas

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
        """0.3s 内能建 TCP 连接就算在听——避免半死网关拖住界面。"""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _sync_remote_sources(self, *, automatic=False):
        """手动同步远程源（后台线程跑网络；完成刷新列表并如实报结果）。"""
        if getattr(self, "_market_sync_busy", False):
            if not automatic:
                self._set_status(tr("正在同步远程源…"), "info")
            return
        self._market_sync_busy = True
        self._set_status(tr("正在同步远程源…"), "info")

        def work():
            market = self._market_obj()
            if market is None:
                return {"_error": "市场模块不可用"}
            try:
                return market.sync_sources(timeout=10.0)
            except Exception as exc:
                return {"_error": str(exc)[:200]}

        def done(results):
            self._market_sync_busy = False
            if isinstance(results, dict) and results and "_error" in results:
                self._set_status(f"远程同步异常：{results['_error']}", "warn")
            elif isinstance(results, dict) and results:
                ok = [r for r in results.values() if isinstance(r, dict) and r.get("ok")]
                fail = [r for r in results.values() if isinstance(r, dict) and not r.get("ok")]
                if fail and not ok:
                    self._set_status(
                        tr("远程源同步失败：{error}",
                           error=str(fail[0].get("error", ""))[:120]), "warn")
                elif fail:
                    self._set_status(
                        tr("远程源同步完成：{ok} 成功 / {fail} 失败",
                           ok=len(ok), fail=len(fail)), "warn")
                else:
                    total = sum(int(r.get("count") or 0) for r in ok)
                    self._set_status(tr("远程源已同步：{count} 条", count=total), "ok")
            self._refresh_market(force=True)

        self._submit_background("market-sync", work, done)

    def _refresh_market(self, force: bool = False):
        """重建市场列表：插件分区 + 工具分区。"""
        if not getattr(self, "_market_built", False):
            return
        if not hasattr(self, "market_list"):
            return
        self.market_stat_var.set(tr("正在读取市场…"))
        self._submit_background("market", self._read_market_snapshot, self._apply_market_snapshot)

    def _apply_market_snapshot(self, snapshot):
        self._market_snapshot = snapshot
        if snapshot is not None:
            self._market_loaded_at = time.monotonic()
        if self._active_view == "tools" and self._tools_tab == "market":
            self._render_market(snapshot)
        elif snapshot is not None:
            self.market_stat_var.set(f"共 {snapshot[1]['total']} 条")
        if snapshot is not None and snapshot[4].get("needs_sync"):
            self._sync_remote_sources(automatic=True)

    def _schedule_market_filter(self):
        token = getattr(self, "_market_filter_job", None)
        if token is not None:
            self.root.after_cancel(token)
        def apply_filter():
            self._market_filter_job = None
            if (self._active_view == "tools" and self._tools_tab == "market"
                    and hasattr(self, "_market_snapshot")):
                self._market_page = 0
                self._market_tool_page = 0
                self._render_market(self._market_snapshot)
        self._market_filter_job = self.root.after(120, apply_filter)

    def _cancel_market_filter(self):
        token = getattr(self, "_market_filter_job", None)
        if token is not None:
            self.root.after_cancel(token)
            self._market_filter_job = None

    def _read_market_snapshot(self):
        """Disk locks, fingerprints and HTTP requests never run inside Tk."""
        market = self._market_obj()
        if market is None:
            return None
        items = market.catalog()
        # catalog refreshes shared state before checking the sync timestamp.
        needs_sync = market.needs_remote_sync() is True
        summary = market.summary(items=items)
        names, err = self._gateway_tool_names()
        runtime_rows = []
        rt = self._plugin_runtime_obj()
        if rt is not None:
            supported = getattr(self.client, "plugin_policy_version", 0) == 1
            report = rt.reload(reserved_names=names or (), available_targets=(names or ()) if supported else (), plugins=items)
            if names and not supported:
                err = "当前网关不支持插件 Policy 上下文；请更新并重启。内置工具仍按其原有 Policy 执行。"
            reasons = {}
            for pid, reason in report.errors + report.skipped:
                reasons.setdefault(pid, []).append(reason)
            for plugin in items:
                plugin.runtime_status = " · ".join(reasons.get(plugin.id, ()))
            for name in rt.names(catalog=items):
                info = rt.tools.get(name)
                if info is not None:
                    runtime_rows.append((name, info["plugin_id"], info["plugin_id"]))
        entries = (build_tool_catalog(names, runtime_tools=runtime_rows, plugins=items)
                   if build_tool_catalog else [])
        remote_meta = {"count": sum(1 for p in items if p.source == "remote"),
                       "sync": market.last_sync_info(),
                       "needs_sync": needs_sync}
        return items, summary, entries, err, remote_meta

    def _render_market(self, snapshot):
        self._market_snapshot = snapshot
        self._cancel_market_filter()
        self._cancel_market_render()
        try:
            self._render_market_inner(snapshot)
        except Exception as exc:
            # 渲染中途炸掉不能把状态行永久留在「正在读取市场…」
            try:
                self.market_stat_var.set(tr("市场读取失败，可点击刷新重试"))
                self._set_status(f"渲染市场失败：{exc}", "warn")
            except Exception:
                pass

    def _render_market_inner(self, snapshot):
        for child in self.market_list.winfo_children():
            child.destroy()
        if snapshot is None:
            i18n.Label(self.market_list,
                     text=f"市场模块不可用：{_MARKET_IMPORT_ERROR or '未知原因'}",
                     bg=C["bg"], fg=C["warn"], font=FONT_SMALL).pack(anchor=tk.W)
            self.market_stat_var.set(tr("市场不可用"))
            return

        items, summary, entries, err, remote = snapshot
        self._market_active_ids = {t.plugin_id for t in entries if t.active and t.plugin_id}

        external_n = sum(1 for p in items
                         if p.id.startswith(("openclaw-", "claude-", "dsh-", "codex-")))
        remote_n = int(remote.get("count") or 0)
        stat = i18n.join("", [f"已启用 {summary['enabled']} · 已安装 {summary['installed']} · "
                             f"共 {summary['total']} 条（外部生态 {external_n} · ",
                             tr("远程 {count} 条", count=remote_n), f"）· 源 {summary['region']}"])
        sync_errors = [v for v in (remote.get("sync") or {}).values()
                       if isinstance(v, dict) and v.get("error")]
        if sync_errors:
            detail = f"{sync_errors[0].get('name', '')} {sync_errors[0]['error']}".strip()
            stat = i18n.join(" · ", [stat, tr("远程同步失败：{detail}", detail=detail[:60])])
        self.market_stat_var.set(stat)
        synced = remote.get("synced")
        if isinstance(synced, dict) and synced:
            if "_error" in synced:
                self._set_status(f"远程同步异常：{synced['_error']}", "warn")
            else:
                ok_n = sum(1 for r in synced.values()
                           if isinstance(r, dict) and r.get("ok"))
                fail_n = len(synced) - ok_n
                if fail_n:
                    self._set_status(
                        tr("远程源同步完成：{ok} 成功 / {fail} 失败",
                           ok=ok_n, fail=fail_n), "warn")
                else:
                    total = sum(int(r.get("count") or 0) for r in synced.values()
                                if isinstance(r, dict))
                    self._set_status(tr("远程源已同步：{count} 条", count=total), "ok")

        query = (self.market_query_var.get() if hasattr(self, "market_query_var") else "").strip().lower()
        want = self.market_kind_var.get() if hasattr(self, "market_kind_var") else tr("全部")

        eco_prefixes = {"🦞 OpenClaw": ("openclaw-",), "🎭 Claude Code": ("claude-",),
                        "🐳 DSH": ("dsh-",), "🤖 Codex": ("codex-", "codex-mcp-")}
        eco_want = (self.market_eco_var.get()
                    if hasattr(self, "market_eco_var") else tr("全部"))

        def match(plugin):
            if want != tr("全部") and plugin.kind_label != want:
                return False
            if eco_want != tr("全部"):
                prefixes = eco_prefixes.get(eco_want, ())
                if prefixes and not plugin.id.startswith(prefixes):
                    return False
            if not query:
                return True
            blob = " ".join([plugin.id, plugin.name, plugin.summary,
                             plugin.author, " ".join(plugin.tags)]).lower()
            return query in blob

        shown = [p for p in items if match(p)]
        i18n.Label(self.market_list, text=tr("插件"), bg=C["bg"], fg=C["ter"],
                 font=FONT_UI_BOLD).pack(anchor=tk.W, pady=(0, 6))
        if not shown:
            i18n.Label(self.market_list, text=tr("没有匹配的插件——换个关键词，或清掉类型筛选。"),
                     bg=C["bg"], fg=C["muted"], font=FONT_SMALL).pack(anchor=tk.W)
        # Bound widget count and yield between cards so input keeps dispatching.
        page_size = 12
        pages = max(1, (len(shown) + page_size - 1) // page_size)
        page = min(getattr(self, "_market_page", 0), pages - 1)
        self._market_page = page
        self._market_page_items = shown[page * page_size:(page + 1) * page_size]
        self._market_pager(len(shown), page, pages, tools=False)
        generation = self._market_render_generation
        pending = iter(self._market_page_items)

        def render_next():
            self._market_render_job = None
            if self._closing or generation != self._market_render_generation:
                return
            try:
                plugin = next(pending)
            except StopIteration:
                tool_size = 40
                tool_pages = max(1, (len(entries) + tool_size - 1) // tool_size)
                tool_page = min(getattr(self, "_market_tool_page", 0), tool_pages - 1)
                self._market_tool_page = tool_page
                try:
                    self._market_pager(len(entries), tool_page, tool_pages, tools=True)
                    self._market_tool_section(entries[tool_page * tool_size:(tool_page + 1) * tool_size], err)
                except Exception as exc:
                    self.market_stat_var.set(tr("市场读取失败，可点击刷新重试"))
                    self._set_status(f"渲染工具失败：{exc}", "warn")
                return
            try:
                self._market_plugin_card(plugin)
            except Exception as exc:
                self._cancel_market_render()
                self.market_stat_var.set(tr("市场读取失败，可点击刷新重试"))
                self._set_status(f"渲染插件失败：{plugin.id} {exc}", "warn")
                return
            self._market_render_job = self.root.after(8, render_next)

        self._market_render_job = self.root.after(1, render_next)

    def _cancel_market_render(self):
        self._market_render_generation = getattr(self, "_market_render_generation", 0) + 1
        token = getattr(self, "_market_render_job", None)
        if token is not None:
            self.root.after_cancel(token)
            self._market_render_job = None

    def _market_pager(self, count, page, pages, *, tools):
        if pages <= 1:
            return
        row = tk.Frame(self.market_list, bg=C["bg"])
        row.pack(fill=tk.X, pady=(4, 8))
        def select(offset):
            setattr(self, "_market_tool_page" if tools else "_market_page", page + offset)
            self._render_market(self._market_snapshot)
            self.market_canvas.yview_moveto(0)
        pill_button(row, "‹", lambda: select(-1), state=tk.NORMAL if page else tk.DISABLED,
                    bg=C["bg"]).pack(side=tk.LEFT)
        i18n.Label(row, text=tr("{label} {page}/{pages} · {count}",
                   label=tr("工具" if tools else "插件"), page=page+1, pages=pages, count=count),
                   bg=C["bg"], fg=C["subtext"], font=FONT_SMALL).pack(side=tk.LEFT, padx=8)
        pill_button(row, "›", lambda: select(1), state=tk.NORMAL if page + 1 < pages else tk.DISABLED,
                    bg=C["bg"]).pack(side=tk.LEFT)

    def _market_plugin_card(self, plugin):
        card = RoundedCard(self.market_list, radius=R_PANEL, fill=C["surface"],
                           outline=C["border_hi"], padx=16, pady=12, bg=C["bg"])
        card.pack(fill=tk.X, pady=(0, 8))
        body = card.content

        top = tk.Frame(body, bg=C["surface"])
        top.pack(fill=tk.X)
        i18n.Label(top, text=plugin.icon, bg=C["surface"], fg=C["text"],
                 font=FONT_TITLE).pack(side=tk.LEFT, padx=(0, 8))
        name = i18n.Label(top, text=plugin.name, bg=C["surface"], fg=C["text"],
                          font=FONT_UI_BOLD, anchor="w", justify=tk.LEFT, wraplength=1)
        name.pack(side=tk.LEFT, fill=tk.X, expand=True)
        theme.bind_wrap(name)
        metadata = tk.Frame(body, bg=C["surface"])
        metadata.pack(fill=tk.X, pady=(4, 0))
        version = i18n.Label(body, text=f"v{plugin.version}", bg=C["surface"], fg=C["muted"],
                            font=FONT_MICRO, anchor="w", justify=tk.LEFT, wraplength=1)
        version.pack(fill=tk.X)
        theme.bind_wrap(version)
        badge(metadata, tr(plugin.kind_label), tone="muted", bg=C["surface"]).pack(side=tk.LEFT)
        if plugin.source == "builtin":
            badge(metadata, tr("内置"), tone="muted", bg=C["surface"]).pack(side=tk.LEFT, padx=(4, 0))
        eco_badge = {"openclaw-": "🦞 OpenClaw", "claude-": "🎭 Claude Code",
                     "dsh-": "🐳 DSH", "codex-": "🤖 Codex"}
        for prefix, label in eco_badge.items():
            if plugin.id.startswith(prefix):
                badge(metadata, label, tone="accent_soft", bg=C["surface"]).pack(side=tk.LEFT, padx=(4, 0))
                break
        theme.flow_controls(metadata)
        states = tk.Frame(body, bg=C["surface"])
        states.pack(fill=tk.X, pady=(6, 0))
        granted = plugin.granted_capabilities(self._active_workspace(), self._session_id)
        active = plugin.id in getattr(self, "_market_active_ids", set())
        for label, yes in ((tr("已安装"), plugin.installed), (tr("已知悉"), plugin.acked and not plugin.needs_ack),
                           (tr("已启用"), plugin.enabled), (tr("已授权"), bool(granted)), (tr("可调用"), active)):
            badge(states, label if yes else {tr("已安装"): tr("未安装"), tr("已知悉"): tr("待知悉"), tr("已启用"): tr("未启用"),
                  tr("已授权"): tr("未授权"), tr("可调用"): tr("不可调用")}[label],
                  tone="ok" if yes else "muted", bg=C["surface"]).pack(side=tk.LEFT, padx=(0, 4))
        theme.flow_controls(states)

        def detail(text, *, fg=C["muted"], font=FONT_MICRO, pady=0):
            label = i18n.Label(body, text=text, bg=C["surface"], fg=fg, font=font,
                               anchor="w", justify=tk.LEFT, wraplength=1)
            label.pack(fill=tk.X, pady=pady)
            return theme.bind_wrap(label)

        if plugin.summary:
            detail(plugin.summary, fg=C["body"], font=FONT_SMALL, pady=(6, 2))
        meta = " · ".join(x for x in (plugin.author, plugin.homepage) if x)
        if meta:
            detail(meta)
        if plugin.permissions or plugin.capabilities:
            detail(tr("权限：{permissions}", permissions=i18n.join(" / ", map(tr, plugin.permission_labels()))),
                   fg=C["warn"] if plugin.executes_code else C["subtext"], pady=(2, 0))
        if plugin.error:
            detail(f"清单有问题：{plugin.error}", fg=C["error"])
        if getattr(plugin, "runtime_status", ""):
            detail(f"执行状态：{plugin.runtime_status}", fg=C["warn"], pady=(2, 0))
        contributed = " · ".join(f"{key} {len(value)}" for key, value in plugin.contributions.items() if value)
        if contributed:
            detail(tr("能力声明：{contributions}", contributions=contributed), pady=(2, 0))
        if plugin.executes_code:
            boundary = tr("Python 插件尚无系统沙箱：知悉与启用不会允许 Agent 执行其代码。")
        elif not plugin.contributions.get("tools"):
            boundary = "声明条目：尚未接入受控工具执行；安装或启用不等于功能已生效。"
        else:
            boundary = tr("文件指纹检查不等于签名验证；授权不能覆盖 Forge Policy 的拒绝或审批要求。")
        detail(boundary, pady=(4, 0))

        actions = tk.Frame(body, bg=C["surface"])
        actions.pack(fill=tk.X, pady=(10, 0))
        pid = plugin.id
        if plugin.installed and (plugin.needs_ack or not plugin.acked):
            pill_button(actions, tr("知悉确认"), lambda: self._market_action(pid, "ack"),
                        kind="primary", bg=C["surface"]).pack(side=tk.LEFT)
        if not plugin.installed:
            pill_button(actions, tr("安装"), lambda: self._market_action(pid, "install"),
                        kind="primary", bg=C["surface"]).pack(side=tk.LEFT)
        elif not plugin.enabled:
            pill_button(actions, tr("启用"), lambda: self._market_action(pid, "enable"),
                        kind="ok" if not plugin.needs_ack else "ghost",
                        bg=C["surface"]).pack(side=tk.LEFT)
        else:
            pill_button(actions, tr("禁用"), lambda: self._market_action(pid, "disable"),
                        kind="ghost", bg=C["surface"]).pack(side=tk.LEFT)
        if plugin.installed:
            pill_button(actions, tr("卸载"), lambda: self._market_action(pid, "uninstall"),
                        kind="danger", bg=C["surface"]).pack(side=tk.LEFT, padx=(8, 0))
        theme.flow_controls(actions)
        detail_actions = tk.Frame(body, bg=C["surface"])
        detail_actions.pack(fill=tk.X, pady=(6, 0))
        pill_button(detail_actions, tr("检查清单"), lambda: self._inspect_plugin(pid),
                    kind="quiet", bg=C["surface"]).pack(side=tk.LEFT)
        if plugin.installed and plugin.enabled:
            pill_button(detail_actions, tr("能力授权…"), lambda: self._market_action(pid, "grant"),
                        kind="ghost", bg=C["surface"]).pack(side=tk.LEFT, padx=(8, 0))
        if plugin.grants:
            pill_button(detail_actions, tr("撤销授权"), lambda: self._market_action(pid, "revoke"),
                        kind="danger", bg=C["surface"]).pack(side=tk.LEFT, padx=(8, 0))
        theme.flow_controls(detail_actions)

    def _market_tool_section(self, entries, err=""):
        """工具市场：网关工具桥暴露的工具 + 已启用插件声明的工具。"""
        divider(self.market_list, color=C["border_hi"], bg=C["bg"]).pack(
            fill=tk.X, pady=(10, 10))
        i18n.Label(self.market_list, text=tr("工具（{count}）", count=len(entries)), bg=C["bg"],
                 fg=C["ter"], font=FONT_UI_BOLD).pack(anchor=tk.W)
        if err:
            warning = i18n.Label(self.market_list, text=f"工具桥未就绪：{err}", bg=C["bg"],
                     fg=C["warn"], font=FONT_MICRO, anchor=tk.W,
                     justify=tk.LEFT, wraplength=1)
            warning.pack(fill=tk.X, pady=(2, 4))
            theme.bind_wrap(warning)
        if not entries:
            empty = i18n.Label(self.market_list,
                     text=tr("还没有可用工具。请启动带 --tools 的 Forge 网关；目录中的工具桥条目不能自动启动它。"),
                     bg=C["bg"], fg=C["muted"], font=FONT_SMALL,
                     anchor="w", justify=tk.LEFT, wraplength=1)
            empty.pack(fill=tk.X)
            theme.bind_wrap(empty)
            return
        grid = tk.Frame(self.market_list, bg=C["bg"])
        grid.pack(fill=tk.X, pady=(6, 0))
        grid.grid_columnconfigure(0, weight=2, uniform="tool-text")
        grid.grid_columnconfigure(1, weight=3, uniform="tool-text")
        for row, tool in enumerate(entries):
            name_lbl = i18n.Label(grid, text=tool.name, bg=C["bg"], fg=C["text"],
                                font=FONT_MONO or FONT_SMALL, anchor=tk.W,
                                justify=tk.LEFT, wraplength=1)
            name_lbl.grid(row=row, column=0, sticky="new", padx=(0, 12), pady=1)
            theme.bind_wrap(name_lbl)
            desc = ("可调用 · " if tool.active else "仅声明 · ") + (tool.summary or (
                "插件提供" if tool.source == "plugin" else "网关工具"))
            tone = C["warn"] if tool.danger else C["subtext"]
            mark = "⚠ " if tool.danger else ""
            description = i18n.Label(grid, text=f"{mark}{desc}", bg=C["bg"], fg=tone,
                                    font=FONT_MICRO, anchor=tk.W, justify=tk.LEFT, wraplength=1)
            description.grid(row=row, column=1, sticky="new", pady=1)
            theme.bind_wrap(description)

    def _gateway_tool_names(self):
        """从网关工具桥取工具名。返回 (names|None, 错误说明)。

        仅从后台调用。端口在监听并不代表 HTTP 正常；快检只用于离线提示。
        """
        client = getattr(self, "client", None)
        if client is None or not hasattr(client, "list_tools"):
            return None, "客户端不支持工具桥"
        base = getattr(client, "base_url", "") or ""
        m = re.search(r"://([^/:]+)(?::(\d+))?", base)
        host = m.group(1) if m else "127.0.0.1"
        port = int(m.group(2)) if (m and m.group(2)) else 8799
        if not self._port_open(host, port):
            return None, f"网关未在 {host}:{port} 上监听"
        try:
            raw = client.list_tools(timeout=3.0)
        except Exception as exc:
            return None, str(exc)[:160]
        names = []
        for item in raw if isinstance(raw, list) else []:
            fn = item.get("function") if isinstance(item, dict) else None
            name = (fn or item).get("name") if isinstance(fn or item, dict) else None
            if name:
                names.append(str(name))
        return names, ""

    def _market_action(self, pid: str, action: str):
        """安装 / 启用 / 禁用 / 卸载 / 确认信任。"""
        if getattr(self, "_market_action_busy", False):
            self._set_status(tr("正在处理插件操作，请稍候"), "info")
            return
        if action not in ("install", "ack", "enable", "disable", "uninstall", "grant", "revoke"):
            return
        if action == "uninstall" and not messagebox.askyesno(
                "卸载插件", f"卸载「{pid}」？\n\n"
                "插件目录会移到 marketplace/trash 下，可手动恢复。", parent=self.root):
            return
        self._market_action_busy = True
        self._set_status(f"正在处理 {pid}：{action}…", "info")

        def work():
            market = self._market_obj()
            if market is None:
                raise ValueError("市场模块不可用")
            if action in ("ack", "grant"):
                plugin = market.find(pid)
                if plugin is None or not plugin.installed or plugin.error:
                    raise ValueError("插件尚未安装或文件无效")
                return market, plugin, plugin.ack_of()
            methods = {"install": market.install_from_catalog, "enable": market.enable,
                       "disable": market.disable, "uninstall": market.uninstall,
                       "revoke": market.revoke_grants}
            methods[action](pid)

        def done(result):
            if action == "grant":
                _market, plugin, reviewed_fingerprint = result
                self._market_action_busy = False
                self._show_grant_dialog(plugin, reviewed_fingerprint)
            elif action == "ack":
                market, plugin, reviewed_fingerprint = result
                if not messagebox.askyesno(
                        "知悉插件声明",
                        f"你将确认已了解「{pid}」的当前文件和能力声明。\n\n"
                        f"版本：{plugin.version}\n权限：{'、'.join(plugin.permission_labels()) or '未声明'}\n\n"
                        "知悉不会授予权限，也不会自动启用。\n"
                        "Python 插件没有系统沙箱，不能由 Agent 执行。\n"
                        "文件、清单、权限或工具声明变化后需重新确认。\n\n"
                        "确认已知悉？", parent=self.root):
                    self._market_action_busy = False
                    self._set_status("已取消知悉确认", "info")
                    return
                self._submit_background("market-action", lambda: market.ack(
                    pid, expected_fingerprint=reviewed_fingerprint), finish)
            else:
                finish(result)

        def finish(_result):
            self._market_action_busy = False
            label = {"install": tr("已安装"), "ack": tr("已知悉"), "enable": tr("已启用"),
                     "disable": "已禁用并撤销授权", "uninstall": "已卸载", "revoke": "已撤销全部授权"}[action]
            self._set_status(f"{label} {pid}" + ("（未启用）" if action == "install" else ""),
                             "info" if action in ("disable", "uninstall") else "ok")
            self._refresh_market()

        self._submit_background("market-action", work, done)

    def _open_plugin_market(self):
        self._show_view("tools")
        self._switch_tools_tab("market")

    def _install_local_plugin(self):
        directory = filedialog.askdirectory(title=tr("选择含 forge-plugin.json 的插件目录"), parent=self.root)
        if not directory or getattr(self, "_market_action_busy", False):
            return
        self._market_action_busy = True
        def work():
            market = self._market_obj()
            if market is None:
                raise RuntimeError("插件市场不可用")
            return market.install_from_dir(directory, enable=False)
        def done(plugin):
            self._market_action_busy = False
            self._set_status(f"已安装 {plugin.id}；请检查清单并逐步确认与授权", "ok")
            self._refresh_market()
        self._submit_background("market-action", work, done)

    def _inspect_plugin(self, pid):
        def work():
            market = self._market_obj()
            return market.find(pid) if market else None
        def done(plugin):
            if plugin is None:
                self._set_status("插件已不存在", "warn")
                return
            text = json.dumps({"id": plugin.id, "version": plugin.version,
                "capabilities": sorted(plugin.declared_capabilities), "contributions": plugin.contributions,
                "provides": plugin.provides, "executes_code": plugin.executes_code,
                "path": plugin.path, "file_fingerprint": plugin.content_fingerprint,
                "signature": "未提供签名验证", "error": plugin.error}, ensure_ascii=False, indent=2)
            dialog = i18n.Toplevel(self.root)
            dialog.title(f"检查插件 · {plugin.name}")
            dialog.bind("<Escape>", lambda _event: dialog.destroy() or "break")
            dialog.geometry("700x520")
            dialog.configure(bg=C["bg"])
            content = scrolledtext.ScrolledText(dialog, bg=C["surface"], fg=C["text"],
                font=FONT_MONO, wrap=tk.WORD, relief=tk.FLAT)
            content.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
            content.insert("1.0", text)
            content.configure(state=tk.DISABLED)
            pill_button(dialog, tr("关闭"), dialog.destroy, bg=C["bg"]).pack(pady=(0, 12))
            try:
                dialog.grab_set()
            except tk.TclError:
                pass
        self._submit_background("plugin-inspect", work, done)

    def _show_grant_dialog(self, plugin, fingerprint):
        old = getattr(self, "_grant_dialog", None)
        if old is not None and old.winfo_exists():
            old.destroy()
        dialog = self._grant_dialog = i18n.Toplevel(self.root)
        dialog.title(tr("能力授权 · {name}", name=plugin.name))
        dialog.bind("<Escape>", lambda _event: dialog.destroy() or "break")
        dialog.transient(self.root)
        dialog.configure(bg=C["bg"])
        dialog.resizable(False, False)
        workspace, session_id = self._active_workspace(), self._session_id
        i18n.Label(dialog, text=tr("只选择你允许的能力"), bg=C["bg"], fg=C["text"],
                 font=FONT_SECTION).pack(anchor="w", padx=20, pady=(18, 8))
        i18n.Label(dialog, text=tr("工作区：{workspace}\n授权不会覆盖 Forge Policy；需要审批时仍由 Policy 决定。", workspace=workspace),
                 bg=C["bg"], fg=C["muted"], font=FONT_SMALL, justify=tk.LEFT,
                 wraplength=520).pack(anchor="w", padx=20, pady=(0, 10))
        dialog.capability_vars = {}
        for cap in sorted(plugin.declared_capabilities):
            supported = cap in GRANTABLE_CAPABILITIES and not plugin.executes_code
            var = tk.BooleanVar(master=dialog, value=False)
            checkbox = i18n.Checkbutton(dialog, text=tr("{base}{detail}", base=tr(PERMISSION_LABELS.get(cap, cap)),
                detail="" if supported else tr(" · 暂不可授权")), variable=var, bg=C["bg"], fg=C["text"],
                selectcolor=C["surface2"], activebackground=C["bg"], activeforeground=C["text"],
                font=FONT_SMALL, state=tk.NORMAL if supported else tk.DISABLED)
            checkbox.pack(anchor="w", padx=20, pady=3)
            if supported:
                dialog.capability_vars[cap] = var
        dialog.scope_var = tk.StringVar(master=dialog, value="session")
        def refresh_scope(*_):
            selected_session = session_id if dialog.scope_var.get() == "session" else ""
            current = {cap for g in plugin.grants if g.get("workspace") == workspace_key(workspace)
                       and g.get("session", "") == selected_session for cap in g.get("capabilities", [])}
            for cap, var in dialog.capability_vars.items():
                var.set(cap in current)
        dialog.scope_var.trace_add("write", refresh_scope)
        refresh_scope()
        for value, label in (("session", tr("仅当前对话")), ("workspace", tr("此工作区的所有对话"))):
            i18n.Radiobutton(dialog, text=label, variable=dialog.scope_var, value=value,
                bg=C["bg"], fg=C["text"], selectcolor=C["surface2"], activebackground=C["bg"],
                activeforeground=C["text"], font=FONT_SMALL).pack(anchor="w", padx=20, pady=3)
        broader = {cap for g in plugin.grants if g.get("workspace") == workspace_key(workspace)
                   and not g.get("session") for cap in g.get("capabilities", [])}
        if broader:
            i18n.Label(dialog, text="工作区已有授权：" + "、".join(PERMISSION_LABELS.get(c, c) for c in sorted(broader)) +
                     "\n当前对话会继承工作区授权。收窄权限时，请先撤销市场卡片上的授权。",
                     bg=C["bg"], fg=C["warn"], font=FONT_SMALL, wraplength=520,
                     justify=tk.LEFT).pack(anchor="w", padx=20, pady=(8, 0))
        actions = tk.Frame(dialog, bg=C["bg"])
        actions.pack(fill=tk.X, padx=20, pady=16)
        def grant():
            selected = [cap for cap, var in dialog.capability_vars.items() if var.get()]
            if not selected:
                self._set_status("请选择至少一项可授权能力；撤销请使用市场卡片上的按钮", "warn")
                return
            self._apply_market_grant(plugin.id, selected, dialog.scope_var.get(), fingerprint,
                                     workspace=workspace, session_id=session_id)
            dialog.destroy()
        dialog.grant_btn = pill_button(actions, tr("授权所选能力"), grant, kind="primary", bg=C["bg"])
        dialog.grant_btn.pack(side=tk.LEFT)
        if not dialog.capability_vars:
            dialog.grant_btn.configure(state=tk.DISABLED)
        pill_button(actions, tr("取消"), dialog.destroy, bg=C["bg"]).pack(side=tk.RIGHT)
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _apply_market_grant(self, pid, capabilities, scope, fingerprint, *, workspace=None, session_id=None):
        if getattr(self, "_market_action_busy", False):
            self._set_status("插件操作进行中，请稍后重试", "info")
            return
        workspace = workspace or self._active_workspace()
        session = (session_id if session_id is not None else self._session_id) if scope == "session" else ""
        self._market_action_busy = True
        def work():
            market = self._market_obj()
            if market is None:
                raise RuntimeError("插件市场不可用")
            return market.grant(pid, capabilities, workspace, session=session, expected_fingerprint=fingerprint)
        def done(_result):
            self._market_action_busy = False
            self._set_status(f"已授予 {pid} 所选能力；执行仍受 Forge Policy 约束", "ok")
            self._refresh_market()
        self._submit_background("market-action", work, done)

    def _show_plugin_audit(self):
        def work():
            market = self._market_obj()
            return market.audit_tail() if market else []
        def done(rows):
            messagebox.showinfo("插件调用审计 · 最近 30 条",
                "\n".join(f"{r['at']} · {r['plugin']} · {r['tool']} · {r['phase']} / {r['outcome']}" for r in rows)
                or "尚无插件调用记录。", parent=self.root)
        self._submit_background("plugin-audit", work, done)

    def _open_plugins_dir(self):
        market = self._market_obj()
        if market is None:
            return
        try:
            market.plugins_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(market.plugins_dir))  # noqa: S606 - Windows 桌面端
        except Exception as exc:
            self._set_status(f"打开插件目录失败：{exc}", "error")

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
        i18n.Label(head, text=title, bg=C["surface"], fg=C["text"], font=FONT_TITLE).pack(side=tk.LEFT)
        badge(head, "规划中", tone="muted", bg=C["surface"]).pack(side=tk.LEFT, padx=(10, 0))
        i18n.Label(card.content, text=subtitle, bg=C["surface"], fg=C["ter"],
                 font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=680).pack(fill=tk.X, pady=(6, 10))
        for item in bullets:
            row = tk.Frame(card.content, bg=C["surface"])
            row.pack(fill=tk.X, pady=2)
            i18n.Label(row, text="•", bg=C["surface"], fg=C["accent2"],
                     font=FONT_UI_BOLD, width=2).pack(side=tk.LEFT)
            i18n.Label(row, text=item, bg=C["surface"], fg=C["body"],
                     font=FONT_SMALL).pack(side=tk.LEFT)
        i18n.Label(card.content, text=note, bg=C["surface"], fg=C["muted"],
                 font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT,
                 wraplength=680).pack(fill=tk.X, pady=(10, 0))
        actions = tk.Frame(card.content, bg=C["surface"])
        actions.pack(fill=tk.X, pady=(14, 0))
        if key == "files":
            pill_button(actions, tr("打开工作区"), lambda: self._open_workspace("file_tree"),
                        kind="primary", bg=C["surface"]).pack(side=tk.LEFT)
        else:
            pill_button(actions, "回到对话", lambda: self._show_view("chat"),
                        kind="primary", bg=C["surface"]).pack(side=tk.LEFT)
            pill_button(actions, tr("打开工作区"), lambda: self._open_workspace("file_tree"),
                        kind="ghost", bg=C["surface"]).pack(side=tk.LEFT, padx=(8, 0))

    def _nav_click(self, key: str):
        if key == "connectors":
            self.desktop_features.open(1)
            return
        if key == "tools":
            self._open_plugin_market()
            return
        if key == "files":
            self._show_view("chat")
            self._open_workspace("file_tree")
            self._set_nav_active("files")
            self._show_sidebar_for("files")
            self._sync_view_navigation()
            return
        self._show_view(key)

    def _show_view(self, key: str, *, record_history=True):
        if key != "tools":
            self._cancel_market_render()
            self._cancel_market_filter()
        if key not in self._views:
            key = "chat"
        if key in self._lazy_views:
            self._build_stub_view(self._views[key], key)
            self._lazy_views.remove(key)
        if record_history and key != self._active_view and self._active_view in self._views:
            self._view_history.append(self._active_view)
            self._view_history = self._view_history[-32:]
        for other, frame in self._views.items():
            if other != key:
                frame.pack_forget()
        self._views[key].pack(fill=tk.BOTH, expand=True)
        was_chat = self._active_view == "chat"
        if was_chat and key != "chat":
            self._chat_sidebar_force_open = self._sidebar_force_open
        self._active_view = key
        if key != "chat":
            pass  # 侧边栏在所有模式常驻（2026-10-11）：不再因切页丢弃展开状态
        elif not was_chat:
            self._sidebar_force_open = getattr(self, "_chat_sidebar_force_open", False)
        self._set_nav_active(key)
        self._show_sidebar_for(key)
        self._sync_view_navigation()
        self._apply_responsive_layout()
        if (key == "tools" and self._tools_tab == "market"
                and getattr(self, "_market_snapshot", None) is not None):
            self._render_market(self._market_snapshot)
        if key == "config":
            self._update_status_label()

    def _set_nav_active(self, key: str):
        self._active_nav = key
        for nav_key, button in getattr(self, "sidebar_shortcuts", {}).items():
            button.configure(bg=C["sel"] if nav_key == key else C["surface2"],
                             fg=C["text"] if nav_key == key else C["ter"])
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

    def _active_workspace(self) -> Path:
        return getattr(self,'_project_workspace',None) or self._repo_root()

    def _build_workspace(self):
        self.workspace = None
        self.ws_holder = tk.Frame(self.split, bg=C["bg"])
        if WorkspacePanel is None:
            self._ws_error = _WS_IMPORT_ERROR or "workspace 模块未安装"
            return
        self._ws_error = ""
        try:
            self.workspace = WorkspacePanel(self.ws_holder, app=self,
                                            repo_root=self._active_workspace(),
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
                self.workspace._set_preview_sub(tr("预览"))
            except Exception:
                pass
        else:
            try:
                self.workspace.open_file_tree()
            except Exception:
                pass
        try:
            self.workspace.refresh_async()
        except Exception:
            pass
        self.ws_toggle_btn.configure(text=tr("▤ 收起工作区"))
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
            self.ws_toggle_btn.configure(text=tr("▤ 工作区"))
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
        """会话持久化走后台线程：主线程不再被 copy2+json.dump+fsync 堵住。

        数据先深拷贝快照（调用方后续改 list 不影响落盘内容）；失败只在
        状态栏提示，不弹窗打断。
        """
        import copy as _copy
        snapshot = _copy.deepcopy(sessions)
        def io():
            try:
                self._write_sessions_io(snapshot)
            except OSError as exc:
                self._post_ui(self._set_status,
                              f"会话记录保存失败：{exc}", "warn")
        threading.Thread(target=io, daemon=True, name="Forge-sessions-io").start()

    def _write_sessions_io(self, sessions: list[dict]):
        temp_path = None
        try:
            payload = redact({"sessions": sessions[:40]})
            path = self._sessions_path()
            # 写前给现有文件留 .bak（上次快照），防止误覆盖丢历史
            try:
                if path.is_file() and path.stat().st_size > 0:
                    path.with_suffix(".json.bak").write_text(redact(path.read_text(encoding='utf-8')), encoding='utf-8')
            except OSError:
                pass
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".sessions-", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except OSError as exc:
            self._post_ui(self._set_status, f"会话记录保存失败：{exc}", "warn")
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
        return tr("新对话")

    def _archive_current_session(self):
        history = list(getattr(self, "_chat_history", []) or [])
        if not history:
            return
        sid = getattr(self, "_session_id", None) or "s" + uuid.uuid4().hex
        sessions = self._load_sessions()
        entry = {
            "id": sid,
            "title": self._session_title(),
            "updated": time.time(),
            "messages": [m.to_dict() for m in history],
        }
        sessions = [s for s in sessions if s.get("id") != sid]
        sessions.insert(0, entry)
        self._write_sessions(sessions)

    def _cancel_history_render(self, event=None):
        if event is not None and event.widget is not getattr(self, "chat_area", None):
            return
        self._history_render_generation = getattr(self, "_history_render_generation", 0) + 1
        token = getattr(self, "_history_render_job", None)
        if token is not None:
            self.root.after_cancel(token)
            self._history_render_job = None

    def _render_history_messages(self, messages):
        """历史会话逐条渲染：每条之间 after(1) 让出主线程，长会话不卡界面。"""
        self._cancel_history_render()
        generation = self._history_render_generation
        session = getattr(self, "_session_id", None)
        area = self.chat_area
        if getattr(self, "_history_render_area", None) is not area:
            area.bind("<Destroy>", self._cancel_history_render, add="+")
            self._history_render_area = area
        state = {"i": 0, "prompt": None}

        def step():
            if generation != self._history_render_generation:
                return
            self._history_render_job = None
            if (getattr(self, "_closing", False) or not area.winfo_exists()
                    or session != getattr(self, "_session_id", None)):
                return
            i = state["i"]
            if i >= len(messages):
                self._refresh_history()
                return
            msg = messages[i]
            state["i"] = i + 1
            try:
                if msg.role == "user":
                    state["prompt"] = msg.content
                    self.chat_area.add_user(msg.content)
                elif msg.content.strip():
                    agent = self.chat_area.add_agent(app=self)
                    agent._retry_session = session
                    agent._retry_prompt = state["prompt"]
                    agent._retry_attachments = ()  # Saved prompt already contains its file snapshots.
                    agent._retry_from_history = True
                    agent.render_markdown(msg.content)
            except tk.TclError:
                return
            self._history_render_job = self.root.after(1, step)

        self._history_render_job = self.root.after(1, step)

    def _refresh_history(self):
        self._submit_background("history", self._load_sessions, self._render_history)

    def _render_history(self, sessions):
        box = getattr(self, "history_box", None)
        if box is None:
            return
        history_bg = box.cget("bg")
        for child in box.winfo_children():
            child.destroy()
        query = (self.session_search_var.get() if hasattr(self, "session_search_var")
                 else "").strip().lower()
        if query:
            sessions = [s for s in sessions if query in str(s.get("title", "")).lower()]
        if not sessions:
            i18n.Label(box, text="还没有历史对话" if not query else "没有匹配的对话",
                     bg=history_bg, fg=C["muted"], font=FONT_MICRO,
                     anchor=tk.W, padx=10, pady=8).pack(fill=tk.X)
            return
        active_id = getattr(self, "_session_id", None)
        last_group = None
        now = time.time()
        for session in sessions:
            import datetime as _dt
            _today = _dt.date.today()
            try:
                updated = float(session.get("updated", 0))
                _d = _dt.datetime.fromtimestamp(updated).date() if updated else None
                age = max(0, now - updated)
            except (ValueError, TypeError, OverflowError, OSError):
                age, _d = float("inf"), None
            if _d == _today:
                group = "今天"
            elif _d == _today - _dt.timedelta(days=1):
                group = "昨天"
            elif age < 604800:
                group = "最近 7 天"
            else:
                group = "更早"
            if group != last_group and not query:
                i18n.Label(box, text=group, bg=history_bg, fg=C["muted"],
                         font=FONT_MICRO, anchor="w", padx=12, pady=7).pack(fill=tk.X)
                last_group = group
            sid = str(session.get("id", ""))
            active = sid == active_id
            row = tk.Frame(box, bg=C["sel"] if active else history_bg,
                           cursor="hand2",
                           highlightthickness=0,
                           highlightbackground=C["sel_border"] if active else history_bg)
            row._history_base = history_bg
            row.pack(fill=tk.X, pady=1, padx=2)
            marker = tk.Frame(row, bg=C["accent"] if active else row["bg"], width=2)
            marker.pack(side=tk.LEFT, fill=tk.Y)
            row._active_marker = marker
            title = str(session.get("title", "未命名对话"))
            title_label = i18n.Label(row, text=title, bg=row["bg"],
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
        bg = C["sel"] if active else C["hover"] if hover else getattr(row, "_history_base", C["sidebar_history"])
        row.configure(bg=bg)
        for child in row.winfo_children():
            child.configure(bg=bg)
        marker = getattr(row, "_active_marker", None)
        if marker is not None:
            marker.configure(bg=C["accent"] if active else bg)

    def _new_session(self):
        if self._sending:
            self._set_status(tr("正在生成回复，完成后可新建对话"), "info")
            return
        self._archive_current_session()
        self._chat_history.clear()
        self._attachments.clear()
        self._session_custom_title = ""
        self._include_history = True
        self.send_var.set("")
        self._update_context_summary()
        self._session_id = "s" + uuid.uuid4().hex
        if callable(getattr(getattr(self, 'client', None), 'reset_secret_session', None)):
            self.client.reset_secret_session()
        if hasattr(self, "chat_area"):
            self._show_chat_start()
        try:
            self.chat_title_var.set(tr("新对话"))
            self.chat_sub_var.set("")
        except AttributeError:
            pass
        self._refresh_history()
        self._set_status(tr("已新建对话"), "info")
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
                                          str(m.get("content", "")),
                                          reasoning_content=str(m.get("reasoning_content") or ""))
                              for m in session.get("messages", []) if isinstance(m, dict)
                              and m.get("role") in ("user", "assistant")
                              and isinstance(m.get("content"), str)]
        self._session_id = sid
        if callable(getattr(getattr(self, 'client', None), 'reset_secret_session', None)):
            self.client.reset_secret_session()
        self._session_custom_title = str(session.get("title", tr("对话")))
        self._attachments.clear()
        self.send_var.set("")
        self._include_history = True
        self._update_context_summary()
        if hasattr(self, "chat_area"):
            self.chat_area.clear()
            self._render_history_messages(list(self._chat_history))
        try:
            self.chat_title_var.set(str(session.get("title", tr("对话"))))
            self.chat_sub_var.set("")
        except AttributeError:
            pass
        self._refresh_history()
        self._set_status(f"已载入对话：{session.get('title', '')}", "info")
        self.input_card.focus_entry()

    # ── 任务视图（forge run）───────────────────────────────
    def _show_chat_start(self):
        """空态 = 上下文感知的工作简报：仓库、变更规模、可做的事。

        只有一轮对话时中间大半是深灰空区——把它利用起来；产生对话后自动消失。
        """
        self._cancel_history_render()
        lines = []
        lines.append(tr("你可以让我审查更改、运行测试、解释代码，或描述一个新目标。"))
        self.chat_area.show_empty(
            tr("今天想完成什么？"),
            tuple(lines),
            actions=(
                (tr("开始对话"), tr("提问、讨论方案或梳理需求"),
                 self.input_card.focus_entry),
                (tr("交给 Forge 一个任务"), tr("运行工具并在时间线里跟踪进度"),
                 lambda: self._show_view("task")),
                (tr("查看项目工作区"), tr("浏览文件、变更、Diff 与预览"),
                 lambda: self._open_workspace("file_tree")),
            ),
        )
        self._refresh_work_context()

    def _context_suggestions(self):
        def draft(text):
            if self._sending:
                self._set_status("正在生成回复，结束后可准备下一条消息", "info")
                return
            if self.send_var.get().strip():
                self.input_card.focus_entry()
                self._set_status(tr("已有未发送草稿，请先编辑或发送当前内容"), "info")
                return
            self.send_var.set(text)
            self.input_card.focus_entry()
        return ((tr("审查更改"), lambda: draft("请审查当前工作区的实际更改，并说明问题和建议。")),
                (tr("运行测试"), lambda: draft("请运行当前项目已有的测试，并报告真实结果。")),
                (tr("解释代码"), lambda: draft("请先查看当前项目的 README，再解释项目结构和主要代码。")))

    def _refresh_work_context(self):
        repo = self._active_workspace() if self.run_py or self._project_workspace is not None else None
        generation = self._work_context_generation = getattr(self, "_work_context_generation", 0) + 1
        self.chat_area.set_work_context(repo.name if repo else None, actions=self._context_suggestions())
        if repo is None or WorkspacePanel is None:
            return

        def worker():
            from workspace import _git_is_repo, _git_status_map
            try:
                changed = len(_git_status_map(repo)) if _git_is_repo(repo) else None
            except (OSError, ValueError):
                changed = None
            self._post_ui(apply, changed)

        def apply(changed):
            if generation == self._work_context_generation and self._active_workspace() == repo:
                self.chat_area.set_work_context(repo.name, changed, actions=self._context_suggestions())
        threading.Thread(target=worker, daemon=True, name="Forge-context-snapshot").start()

    def _on_workspace_snapshot(self, repo, changed):
        if self._active_workspace() == repo:
            self._work_context_generation = getattr(self, "_work_context_generation", 0) + 1
            self.chat_area.set_work_context(repo.name, changed, actions=self._context_suggestions())

    def _build_task_view(self, parent):
        head = tk.Frame(parent, bg=C["chat"])
        head.pack(fill=tk.X, padx=20, pady=(16, 10))
        i18n.Label(head, text=tr("任务"), bg=C["chat"], fg=C["text"],
                 font=FONT_TITLE).pack(side=tk.LEFT)
        IconButton(head, text=tr("＋ 新建任务"), command=self._clear_task_view,
                  bg=C["chat"], fg=C["ter"], activebackground=C["hover"],
                  activeforeground=C["text"], font=FONT_SMALL, relief=tk.FLAT, bd=0,
                  padx=10, pady=3, cursor="hand2",
                  highlightthickness=1, highlightbackground=C["border_hi"]
                  ).pack(side=tk.RIGHT)
        i18n.Label(parent, text=tr("交代目标与验收标准，Forge 会执行并汇报过程；产出可在工作区查看。"),
                 bg=C["chat"], fg=C["ter"], font=FONT_SMALL, anchor=tk.W,
                 justify=tk.LEFT, wraplength=760).pack(fill=tk.X, padx=20)

        ctl = tk.Frame(parent, bg=C["chat"])
        ctl.pack(side=tk.BOTTOM, fill=tk.X, padx=20, pady=(10, 12))
        i18n.Label(ctl, text=tr("交给 Forge 的任务"), bg=C["chat"], fg=C["subtext"],
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
        i18n.Label(strategy_row, text=tr("策略"), bg=C["chat"], fg=C["muted"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=(0, 6))
        self._strategy_row = tk.Frame(strategy_row, bg=C["chat"])
        self._strategy_row.pack(side=tk.LEFT)
        self._task_strategy = "medium"
        self._render_strategy_chips()
        planning_row = tk.Frame(ctl, bg=C["chat"])
        planning_row.pack(fill=tk.X, pady=(6, 0))
        self._task_planning_row = planning_row
        self._render_planning_chips()
        pill_button(ctl, tr("规划模型与复审"), self._open_phase_settings,
                    kind="ghost", bg=C["chat"]).pack(anchor="w", pady=(5, 0))
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

    def _read_planning_level(self):
        from forge.planning import PLANNING_LEVELS
        for row in self.user_rows:
            if str(row.get("id")) == "planning":
                value = (row.get("config") or {}).get("level", "none")
                return value if value in PLANNING_LEVELS else "none"
        return "none"

    def _render_planning_chips(self):
        row = getattr(self, "_task_planning_row", None)
        if row is None or not row.winfo_exists():
            return
        for child in row.winfo_children():
            child.destroy()
        current = self._read_planning_level()
        for value, label in PLANNING_LABELS.items():
            theme.chip(row, label, selected=value == current,
                command=lambda v=value: self._set_planning_level(v)).pack(side=tk.LEFT, padx=(0, 5))

    def _open_planning_menu(self, anchor=None):
        current = self._read_planning_level()
        return show_popover_menu(anchor or self.session_menu_btn, [
            {"label": label, "selected": value == current,
             "command": lambda v=value: self._set_planning_level(v)}
            for value, label in PLANNING_LABELS.items()] + [
            {"separator": True}, {"label": tr("规划模型与复审"), "command": self._open_phase_settings}],
            title=tr("事前规划"), width=280)

    def _phase_setting(self, row_id, key, default=None):
        row = next((r for r in self.user_rows if str(r.get('id')) == row_id), {})
        if row.get('disabled'): return default
        return (row.get('config') or {}).get(key, default)

    def _save_phase_settings(self, planning_model, review_model, review_enabled):
        from phase_client import model_catalog
        from forge.phase_models import model_pair
        if self._sending or getattr(self, '_task_running', False) or self._feature_dirty:
            self._set_status(tr('请在执行结束并保存功能开关后调整规划'), 'info')
            return False
        latest = load_user_layer(self.home)
        choices = {pair for pair, _label in model_catalog(latest)}
        planning_model, review_model = model_pair(planning_model), model_pair(review_model)
        if any(pair is not None and pair not in choices for pair in (planning_model, review_model)):
            raise ValueError('Phase model is no longer enabled/configured')
        if type(review_enabled) is not bool: raise ValueError('Review enabled must be boolean')
        rows = copy.deepcopy(latest)
        for rid, values in [('planning', {'model': planning_model}),
                            ('review', {'model': review_model, 'enabled': review_enabled})]:
            row = next((r for r in rows if str(r.get('id')) == rid), None)
            if row is None:
                row = {'id': rid, 'name': rid + ':settings', 'config': {}}
                rows.append(row)
            row.setdefault('config', {}).update(values)
        save_user_layer(self.home, rows, expected_rows=latest)
        self.user_rows = rows
        self._rebuild_feature_toggles(force=True)
        self._sync_composer_metadata()
        for attr, task in [('_agent_msg', False), ('_task_msg', True)]:
            msg = getattr(self, attr, None)
            if msg is not None and msg.winfo_exists() and getattr(msg, '_review_source', None):
                self._attach_review_action(msg, msg._review_source, task=task)
        return True

    def _open_phase_settings(self):
        from phase_client import model_catalog
        if self._sending or getattr(self, '_task_running', False):
            self._set_status(tr('执行结束后可修改模型与复审设置'), 'info')
            return
        previous = getattr(self, '_phase_dialog', None)
        if previous is not None and previous.winfo_exists():
            previous.lift()
            return
        choices = [(None, i18n.resolve(tr('沿用执行模型'), self.root))] + model_catalog(self.user_rows)
        dialog = i18n.Toplevel(self.root)
        self._phase_dialog = dialog
        dialog.title(tr('规划模型与复审'))
        dialog.configure(bg=C['surface'])
        dialog.transient(self.root)
        body = tk.Frame(dialog, bg=C['surface'], padx=18, pady=16)
        body.pack(fill=tk.BOTH, expand=True)
        i18n.Label(body, text=tr('独立模型使用已配置 Provider 的凭据；审核只在点击后运行。'),
                    bg=C['surface'], fg=C['muted'], wraplength=360, justify='left').pack(fill=tk.X)
        selections = []
        for title, rid in [(tr('规划模型'), 'planning'), (tr('复审模型'), 'review')]:
            i18n.Label(body, text=title, bg=C['surface'], fg=C['text']).pack(anchor='w', pady=(14, 5))
            picker = ttk.Combobox(body, state='readonly', values=[label for _, label in choices], width=40)
            current = self._phase_setting(rid, 'model')
            index = next((i for i, (pair, _) in enumerate(choices) if pair == tuple(current or ())), 0)
            if current and index == 0:
                i18n.Label(body, text=tr('原模型已不可用，请重新选择。'),
                            bg=C['surface'], fg=C['warn']).pack(anchor='w')
            picker.current(index); picker.pack(fill=tk.X)
            selections.append(picker)
        enabled = tk.BooleanVar(value=self._phase_setting('review', 'enabled', False) is True)
        i18n.Checkbutton(body, text=tr('启用手动复审'), variable=enabled,
                         bg=C['surface'], fg=C['text'], selectcolor=C['input_bg']).pack(anchor='w', pady=14)
        def save():
            try:
                if self._save_phase_settings(choices[selections[0].current()][0],
                        choices[selections[1].current()][0], enabled.get()):
                    dialog.destroy()
            except (ValueError, OSError) as exc:
                self._set_status(str(exc), 'error')
        footer = tk.Frame(body, bg=C['surface']); footer.pack(fill=tk.X)
        pill_button(footer, tr('保存'), save, kind='primary', bg=C['surface']).pack(side=tk.RIGHT)
        self._phase_settings_close_btn = pill_button(footer, tr('取消'), dialog.destroy,
                                                     kind='ghost', bg=C['surface'])
        self._phase_settings_close_btn.pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind('<Escape>', lambda _event: dialog.destroy() or 'break')
        dialog.grab_set()

    def _set_planning_level(self, value):
        from forge.planning import planning_level
        planning_level(value)
        if self._sending or getattr(self, "_task_running", False) or self._feature_dirty:
            self._set_status("请在执行结束并保存功能开关后调整规划", "info")
            return False
        try:
            latest = load_user_layer(self.home)
            rows = copy.deepcopy(latest)
            row = next((r for r in rows if str(r.get("id")) == "planning"), None)
            if row is None:
                rows.append({"id": "planning", "name": "planning:level", "config": {"level": value}})
            else:
                row.setdefault("config", {})["level"] = value
            save_user_layer(self.home, rows, expected_rows=latest)
        except (OSError, ValueError) as exc:
            self._set_status(str(exc), "error")
            return False
        self.user_rows = rows
        pill = getattr(getattr(self, "input_card", None), "planning_pill", None)
        if pill is not None:
            pill.configure(text=PLANNING_LABELS[value])
        self._render_planning_chips()
        self._rebuild_feature_toggles(force=True)
        self._set_status(PLANNING_LABELS[value], "ok")
        return True

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
                                                  subtitle=f"策略：{label} · 工作区：{self._active_workspace()}")
        self._task_msg.stream_text("正在执行 forge run；实际工具记录将在任务返回后显示。")
        self._task_msg.set_status("运行中…")
        self._task_running = True
        self._task_cancel_event = threading.Event()
        self._running_strategy = self._task_strategy
        self._task_started = time.time()
        self.task_run_btn.configure(state=tk.DISABLED)
        self._set_status(f"任务已下发：{task[:40]}", "info")

        cmd = task_command(_python_exe(), self.run_py, self.home, task, self._task_strategy,
                           self._read_planning_level(), self._phase_setting('planning', 'model'),
                           workspace=self._active_workspace(),
                           profile='balanced' if getattr(getattr(self,'desktop_features',None),'sync',False) else 'conservative')
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
        threading.Thread(target=worker, daemon=False, name="Forge-task-launch").start()

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
            plan = data.get("planning")
            if isinstance(plan, dict):
                from forge.planning import TaskPlan
                parsed_plan = TaskPlan.parse(json.dumps({k: v for k, v in plan.items() if k != "level"}), plan["level"])
                msg.add_note(parsed_plan.display(), tone="muted")
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
                msg.add_tool_card(rows, title=tr("执行步骤"))
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
            self.workspace.refresh_async()
        changed = self._changed_file_count()
        actions = []
        if changed is None:
            actions.append({"label": "📑 查看仓库变更", "kind": "primary",
                            "command": lambda: self._open_workspace("changes")})
        elif changed > 0:
            actions.append({"label": f"📑 仓库变更 ({changed})", "kind": "primary",
                            "command": lambda: self._open_workspace("changes")})
        actions.append({"label": tr("打开工作区"), "command": lambda: self._open_workspace("file_tree")})
        if isinstance(data, dict):
            actions.extend(self._file_actions(str(data.get("text") or "")))
        if changed:
            actions.append({"label": tr("预览选中文件"), "command": lambda: self._open_workspace("preview")})
        msg.add_actions(actions)
        if isinstance(data, dict) and data.get('stopped') == 'final':
            self._attach_review_action(msg, str(data.get('text') or ''), task=True)

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
        language_row = tk.Frame(parent, bg=C["bg"])
        language_row.pack(fill=tk.X, pady=(8, 0))
        i18n.Label(language_row, text=tr("界面语言"), bg=C["bg"], fg=C["text"],
                   font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 10))
        self.language_var = tk.StringVar(value=i18n.LANGUAGES[self.root._forge_locale.language])
        self.language_picker = ttk.Combobox(language_row, textvariable=self.language_var,
            values=list(i18n.LANGUAGES.values()), state="readonly", width=15, font=FONT_SMALL)
        self.language_picker.pack(side=tk.LEFT)
        self.language_picker.bind("<<ComboboxSelected>>", lambda _e: self._change_language(
            next(code for code, label in i18n.LANGUAGES.items() if label == self.language_var.get())))
        language_hint = i18n.Label(language_row, text=tr("立即应用；保留草稿、对话和布局。"),
                   bg=C["bg"], fg=C["muted"], font=FONT_CAPTION, justify=tk.LEFT)
        language_hint.pack(side=tk.LEFT, padx=12, fill=tk.X, expand=True)
        theme.bind_wrap(language_hint)
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
        theme.bind_scoped_wheel(editor_canvas, right)
        self.editor_canvas = editor_canvas
        split.add(left, minsize=240, width=310)
        split.add(right_host, minsize=480)

        # ── 左栏三区：head（固定）→ list（撑满独立滚动）→ actions（固定底部）──
        # 之前 Provider 列表拿 35px、底部按钮被挤出首屏（实测「打开用户层目录」
        # 被压成 1x1），核心入口不能随配置数量增长消失。
        head = tk.Frame(left, bg=C["surface"])
        head.pack(fill=tk.X)
        i18n.Label(head, text=tr("用户配置"), bg=C["surface"], fg=C["text"],
                 font=FONT_SECTION).pack(anchor=tk.W)
        count_row = tk.Frame(head, bg=C["surface"])
        count_row.pack(fill=tk.X, pady=(2, 8))
        self.provider_count_var = i18n.StringVar(self.root, value=tr("0 条用户配置"))
        i18n.Label(count_row, textvariable=self.provider_count_var, bg=C["surface"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT)
        i18n.Label(count_row, text=tr("选择条目载入右侧编辑"), bg=C["surface"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.RIGHT)
        # 搜索 / 筛选（对显示名与真实 id 都匹配）
        search_row = tk.Frame(head, bg=C["surface"])
        search_row.pack(fill=tk.X, pady=(0, 10))
        self._search_active = False   # 占位符状态不算过滤（否则初始列表全被滤空）
        self.provider_search_var = tk.StringVar()
        self.provider_search_var.trace_add("write", lambda *_: self._filter_provider_list())
        search_entry = tk.Entry(search_row, textvariable=self.provider_search_var,
                                bg=C["input_bg"], fg=C["text"],
                                insertbackground=C["accent"], font=FONT_SMALL,
                                relief=tk.FLAT, highlightthickness=1,
                                highlightbackground=C["border"],
                                highlightcolor=C["accent"])
        search_entry.pack(fill=tk.X, ipady=4)
        self._provider_search_placeholder = tr("🔍 搜索名称 / 模型 / ID")
        self._provider_search_entry = search_entry
        search_entry.insert(0, self._provider_search_placeholder)
        search_entry.configure(fg=C["placeholder"])
        search_entry.bind("<FocusIn>", self._provider_search_focus_in)
        search_entry.bind("<FocusOut>", self._provider_search_focus_out)
        # pill_button 用 placeholder 色画不出图标，直接普通按钮；占位在 FocusIn 清掉
        pill_button(head, "＋ 新建 Provider", self._new_provider,
                    kind="ghost", bg=C["surface"]).pack(fill=tk.X, pady=(0, 0))

        # list 区 + 底部固定区：用垂直 PanedWindow 分割。
        # 纯 pack expand 有个坑：Listbox reqh 声明大了会把 bottom 全挤出可视区，
        # 声明小了又只分到几十像素。PanedWindow 两全：list 撑满剩余、bottom 恒在。
        body_split = tk.PanedWindow(left, orient=tk.VERTICAL, bg=C["border"],
                                    sashwidth=5, sashrelief=tk.FLAT, bd=0)
        body_split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        list_holder = tk.Frame(body_split, bg=C["surface"])
        self.provider_list = tk.Listbox(
            list_holder, bg=C["input_bg"], fg=C["text"],
            selectbackground=C["surface2"], selectforeground=C["accent"],
            font=FONT_UI, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["border"], highlightcolor=C["accent"],
            activestyle="none", selectborderwidth=0, height=1, exportselection=False,
        )
        self.provider_list.pack(fill=tk.BOTH, expand=True)
        self.provider_list.bind("<<ListboxSelect>>", self._on_provider_select)
        self._provider_rows_all = []   # (原始 index, 显示文本)，搜索过滤用

        actions = tk.Frame(body_split, bg=C["surface"])
        body_split.add(list_holder, minsize=120, height=420, stretch="always")
        body_split.add(actions, minsize=140, stretch="never")
        pill_button(actions, "API 密钥", self._open_api_keys, kind="accent_soft",
                    bg=C["surface"], icon="\U0001F511").pack(
            fill=tk.X)

        # ── 模型快捷编辑面板 + 一键温度：迁到右侧主区顶部（清单 #2 左挤右空）──
        # 这两块是「编辑功能」，右侧才是查看/编辑/操作的地方；左栏只留选择与导航。
        # 右侧 JSON 流水线原样保留（编辑器基线契约不破坏），只是上方多一条工具条。
        self.model_edit_frame = tk.Frame(right, bg=C["surface"], highlightthickness=1,
                                         highlightbackground=C["border"])
        # 插在标题之前：side=TOP 首个 pack 的在最上
        # 此刻 right 还没有别的子控件，side=TOP 即为最上；标题/流水线在其后 pack
        self.model_edit_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 10), ipadx=10, ipady=8)
        i18n.Label(self.model_edit_frame, text=tr("模型快捷编辑"), bg=C["input_bg"], fg=C["text"],
                 font=FONT_UI_BOLD).pack(anchor=tk.W)
        self.model_edit_target = i18n.Label(self.model_edit_frame, text=tr("（先在列表选中 provider）"),
                                          bg=C["input_bg"], fg=C["muted"], font=FONT_SMALL,
                                          wraplength=240, justify=tk.LEFT)
        self.model_edit_target.pack(anchor=tk.W, pady=(2, 6))
        entry_row = tk.Frame(self.model_edit_frame, bg=C["input_bg"])
        entry_row.pack(fill=tk.X)
        entry_row.grid_columnconfigure(0, weight=1)
        self.model_edit_var = tk.StringVar()
        self.model_edit_entry = tk.Entry(entry_row, textvariable=self.model_edit_var,
                                         bg=C["bg"], fg=C["text"], insertbackground=C["accent"],
                                         font=FONT_MONO, relief=tk.FLAT,
                                         highlightthickness=1, highlightbackground=C["border"],
                                         highlightcolor=C["accent"])
        self.model_edit_entry.grid(row=0, column=0, sticky="ew", ipady=4)
        # 按钮 grid 自然宽：之前 pack 在扩张 Entry 后面被挤成 8px 紫条
        self.model_apply_btn = i18n.Button(entry_row, text=tr("应用"), bg=C["accent"], fg="#ffffff",
                                         activebackground=C["accent_hover"], activeforeground="#ffffff",
                                         font=FONT_UI_BOLD, relief=tk.FLAT, padx=10, pady=3,
                                         command=self._apply_model_edit, cursor="hand2",
                                         state=tk.DISABLED)
        self.model_apply_btn.grid(row=0, column=1, padx=(6, 0))
        # 常用模型 chips（按 baseURL 域名给建议）
        self.model_chips_frame = tk.Frame(self.model_edit_frame, bg=C["input_bg"])
        self.model_chips_frame.pack(fill=tk.X, pady=(6, 0))
        self.model_edit_entry.bind("<Return>", lambda _e: self._apply_model_edit())
        # 探活按钮
        probe_row = tk.Frame(self.model_edit_frame, bg=C["input_bg"])
        probe_row.pack(fill=tk.X, pady=(6, 0))
        self.model_probe_btn = i18n.Button(probe_row, text=tr("测试此 provider"), bg=C["surface2"], fg=C["link"],
                                         activebackground=C["link_soft"], activeforeground=C["link"],
                                         font=FONT_UI, relief=tk.FLAT, padx=8, pady=2,
                                         command=self._probe_selected_provider, cursor="hand2",
                                         state=tk.DISABLED)
        self.model_probe_btn.pack(side=tk.LEFT)
        self.model_probe_var = tk.StringVar(value="")
        i18n.Label(probe_row, textvariable=self.model_probe_var, bg=C["input_bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT, padx=(8, 0))
        # 两条路径：① 一键配置（选温度）；② 手写配置文件。放底部 actions 区上方。
        one_click = tk.Frame(right, bg=C["surface"], highlightthickness=1,
                             highlightbackground=C["border"])
        one_click.pack(side=tk.TOP, fill=tk.X, pady=(0, 10), ipadx=10, ipady=8)
        # 标题行可点折叠：默认收起只占一行，列表永远有空间（底部固定区之前吃掉
        # 318px 把 Listbox 压回 35px——折叠后 ~90px）。
        oc_head = tk.Frame(one_click, bg=C["input_bg"])
        oc_head.pack(fill=tk.X)
        self._temp_expanded = False
        self.temp_toggle_var = i18n.StringVar(self.root, value=tr("▸ 一键配置 · 采样温度"))
        self.temp_summary_var = tk.StringVar(value="")
        i18n.Button(oc_head, textvariable=self.temp_toggle_var,
                  bg=C["input_bg"], fg=C["text"], font=FONT_UI_BOLD,
                  relief=tk.FLAT, bd=0, padx=0, pady=2, anchor=tk.W,
                  command=self._toggle_temp_panel, cursor="hand2",
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        i18n.Label(oc_head, textvariable=self.temp_summary_var, bg=C["input_bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.RIGHT)
        oc_body = tk.Frame(one_click, bg=C["input_bg"])
        i18n.Label(oc_body,
                 text=tr("Agent 场景 0.7 更稳；只接受默认温度的模型会自动跳过；"
                      "Claude 协议不发任何采样参数。"),
                 bg=C["input_bg"], fg=C["muted"], font=FONT_SMALL,
                 wraplength=330, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 6))
        temp_row = tk.Frame(oc_body, bg=C["input_bg"])
        temp_row.pack(fill=tk.X, pady=(2, 0))
        temp_row.grid_columnconfigure(0, weight=1, uniform="temp")
        temp_row.grid_columnconfigure(1, weight=1, uniform="temp")
        temp_row.grid_columnconfigure(2, weight=1, uniform="temp")
        self.temperature_var = tk.StringVar(value=self._current_temperature_preset())
        self._temperature_buttons = {}
        for col, (value, label) in enumerate((("0.7", "0.7 · 均衡"),
                                              ("1.0", "1.0 · 保守"),
                                              ("", "不设置"))):
            btn = i18n.Button(
                temp_row, text=label, bg=C["surface2"], fg=C["text"],
                activebackground=C["accent_soft"], activeforeground=C["accent"],
                font=FONT_UI, relief=tk.FLAT, padx=8, pady=4, cursor="hand2",
                command=lambda v=value: self._apply_temperature_preset(v))
            # grid + sticky=ew：三颗等宽，永远不会被挤成细条（清单 #4/#11）
            btn.grid(row=0, column=col, sticky="ew", padx=(0, 6) if col < 2 else 0)
            self._temperature_buttons[value] = btn
        self._paint_temperature_buttons()
        IconButton(oc_body, text=tr("手写配置文件（所有参数自己控）"), icon="external",
                  bg=C["surface2"], fg=C["link"],
                  activebackground=C["link_soft"], activeforeground=C["link"],
                  font=FONT_UI, relief=tk.FLAT, padx=10, pady=4, cursor="hand2",
                  command=self._open_user_layer_file).pack(anchor=tk.W, pady=(8, 0))
        self._temp_body = oc_body   # 默认不 pack = 折叠
        IconButton(actions, text=tr("打开用户层目录"), icon="external", bg=C["surface2"], fg=C["text"],
                  activebackground=C["border"], activeforeground=C["text"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=lambda: self._open_path(self.home), cursor="hand2"
                  ).pack(fill=tk.X, pady=(8, 0))

        i18n.Label(right, text=tr("添加或编辑配置"), bg=C["bg"], fg=C["text"],
                 font=FONT_SECTION).pack(anchor=tk.W)
        i18n.Label(right, text=tr("粘贴 Provider JSON 或地址与密钥，整理后确认预览再保存。"),
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
        self.organize_btn = i18n.Button(
            btn_bar, text=tr("整理并预览"), bg=C["accent"], fg="#ffffff",
            activebackground=C["accent_hover"], activeforeground="#ffffff",
            font=FONT_UI_BOLD, relief=tk.FLAT, padx=16, pady=6,
            command=self._do_organize, cursor="hand2",
        )
        self.organize_btn.pack(side=tk.LEFT)
        i18n.Button(btn_bar, text=tr("粘贴"), bg=C["surface2"], fg=C["text"],
                  activebackground=C["border"], activeforeground=C["text"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._paste_clipboard, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        # 破坏性操作（清空输入区）用红色文字区分，不和普通操作混一层级（清单 #6）
        i18n.Button(btn_bar, text=tr("清空"), bg=C["surface2"], fg=C["error"],
                  activebackground=C["error_soft"], activeforeground=C["error"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._clear_input, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        # 从 AutoClaw 用户层导入 provider（一次把 18 个 provider 写进 forge 用户层 + 密钥库）
        i18n.Button(btn_bar, text=tr("从 AutoClaw 导入"), bg=C["surface2"], fg=C["accent"],
                  activebackground=C["accent_soft"], activeforeground=C["accent"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._import_from_autoclaw, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        # 供应商目录：19 家常见厂商 + 各自的套餐/订阅端点，点一下填模板
        i18n.Button(btn_bar, text=tr("供应商目录"), bg=C["surface2"], fg=C["link"],
                  activebackground=C["link_soft"], activeforeground=C["link"],
                  font=FONT_UI, relief=tk.FLAT, padx=12, pady=6,
                  command=self._open_provider_catalog, cursor="hand2"
                  ).pack(side=tk.LEFT, padx=(8, 0))
        i18n.Label(btn_bar, text=tr("Ctrl + Enter 整理"), bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.RIGHT)

        preview_head = tk.Frame(right, bg=C["bg"])
        preview_head.pack(fill=tk.X, pady=(0, 7))
        i18n.Label(preview_head, text=tr("预览"), bg=C["bg"], fg=C["text"],
                 font=FONT_SECTION).pack(side=tk.LEFT)
        i18n.Label(preview_head, text=tr("整理后的 patch JSON"), bg=C["bg"],
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
        i18n.Label(warn_head, text=tr("提示与环境变量"), bg=C["bg"],
                 fg=C["subtext"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        self.warning_count_var = i18n.StringVar(self.root, value=tr("无提示"))
        i18n.Label(warn_head, textvariable=self.warning_count_var,
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
        i18n.Label(footer, text=tr("预览后保存"), bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT)
        self.save_btn = i18n.Button(
            footer, text=tr("保存到用户层"), bg=C["ok"], fg="#ffffff",
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
        self.feature_title_var = i18n.StringVar(self.root, value=tr("▸ 功能开关"))
        self.feature_toggle_btn = i18n.Button(
            head, textvariable=self.feature_title_var, bg=C["surface"],
            fg=C["text"], activebackground=C["surface2"],
            activeforeground=C["accent"], font=FONT_UI_BOLD,
            relief=tk.FLAT, bd=0, padx=0, pady=2, anchor=tk.W,
            command=self._toggle_feature_panel, cursor="hand2",
        )
        self.feature_toggle_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.feature_summary_var = i18n.StringVar(self.root, value=tr("读取中"))
        i18n.Label(head, textvariable=self.feature_summary_var, bg=C["surface"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.RIGHT)

        self.feature_body = tk.Frame(self.feature_card, bg=C["surface"])
        self.feature_feedback_var = i18n.StringVar(self.root, value=tr("勾选仅修改草稿。保存后，重启正在运行的 Forge / 通道服务以应用配置。"))
        i18n.Label(self.feature_body, textvariable=self.feature_feedback_var,
                 bg=C["surface"], fg=C["subtext"], font=FONT_SMALL,
                 anchor=tk.W, justify=tk.LEFT, wraplength=800
                 ).pack(fill=tk.X, pady=(10, 8))
        actions = tk.Frame(self.feature_body, bg=C["surface"])
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        self.feature_reset_btn = i18n.Button(
            actions, text=tr("还原未保存修改"), bg=C["surface2"], fg=C["subtext"],
            activebackground=C["border"], activeforeground=C["text"],
            font=FONT_SMALL, relief=tk.FLAT, padx=10, pady=5,
            command=self._discard_feature_changes, cursor="hand2", state=tk.DISABLED,
        )
        self.feature_reset_btn.pack(side=tk.LEFT)
        i18n.Button(actions, text=tr("刷新已保存配置"), command=self._reload_configuration,
                  bg=C["surface2"], fg=C["text"], activebackground=C["border"],
                  font=FONT_SMALL, relief=tk.FLAT, padx=10, pady=5,
                  cursor="hand2").pack(side=tk.LEFT, padx=8)
        i18n.Button(actions, text=tr("选择 Forge 目录"), command=self._choose_forge_repo,
                  bg=C["surface2"], fg=C["text"], activebackground=C["border"],
                  font=FONT_SMALL, relief=tk.FLAT, padx=10, pady=5,
                  cursor="hand2").pack(side=tk.LEFT)
        self.feature_save_btn = i18n.Button(
            actions, text=tr("保存功能开关"), bg=C["accent"], fg="#ffffff",
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
        theme.bind_scoped_wheel(self.feature_canvas, self.feature_list, self._scroll_features)
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
            self.feature_summary_var.set(tr("暂无可切换项目"))
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
        toggle = i18n.Checkbutton(
            card, text=entry["label"], variable=var, command=self._mark_features_dirty,
            bg=C["input_bg"], fg=C["text"], activebackground=C["input_bg"],
            activeforeground=C["accent"], selectcolor=C["surface2"],
            font=FONT_UI_BOLD, anchor=tk.W, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["input_bg"], highlightcolor=C["accent"],
            cursor="hand2",
        )
        state_var = tk.StringVar(value="")
        entry["state_var"] = state_var
        i18n.Label(card, textvariable=state_var, bg=C["input_bg"], fg=C["accent"],
                 font=FONT_SMALL).pack(side=tk.RIGHT, padx=8)
        toggle.pack(anchor=tk.W)
        description = i18n.Label(card, text=entry["description"], bg=C["input_bg"], fg=C["muted"],
                               font=FONT_SMALL, anchor=tk.W, justify=tk.LEFT)
        description.pack(anchor=tk.W, padx=(23, 0), pady=(2, 0))
        card.bind("<Configure>", lambda event: (
            toggle.configure(wraplength=max(160, event.width - 210)),
            description.configure(wraplength=max(160, event.width - 210))))
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
            i18n.Label(self.feature_list, text=tr("当前用户层没有可切换的布尔功能。添加 Provider 或通道配置后会自动出现在这里。"),
                     bg=C["surface"], fg=C["muted"], font=FONT_SMALL, anchor=tk.W,
                     justify=tk.LEFT, wraplength=620).pack(fill=tk.X, pady=4)
        self._mark_features_dirty()

    def _mark_features_dirty(self):
        changes = 0
        for entry in self._feature_entries:
            changed = bool(entry["var"].get()) != entry["value"]
            changes += changed
            positive_switch = entry["kind"] == "provider" or entry["path"][-1] in ("enabled", "moa")
            state = ((tr("开启") if entry["var"].get() else tr("关闭")) if positive_switch
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
        self.feature_feedback_var.set(tr("已刷新磁盘上的配置。保存后的设置由下次启动的服务读取。"))
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
        self.feature_feedback_var.set(tr("已保存。请重启正在运行的 Forge / 通道服务，让新设置生效。"))
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
        self.chat_title_var = i18n.StringVar(self.root, value=tr("新对话"))
        i18n.Label(title_row, textvariable=self.chat_title_var, bg=C["chat"],
                 fg=C["text"], font=FONT_TITLE).pack(side=tk.LEFT)
        self.chat_sub_var = tk.StringVar(value="")
        subtitle = i18n.Label(parent, textvariable=self.chat_sub_var, bg=C["chat"], fg=C["muted"],
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
        # 新建对话固定在当前对话右上角。
        right = tk.Frame(head, bg=C["chat"])
        right.pack(side=tk.RIGHT, anchor=tk.N, before=left)
        self.temp_var = tk.StringVar(value="0.7")
        self.session_menu_btn = glyph_button(right, "⋯", self._popup_session_menu, bg=C["chat"],
                     fg=C["ter"], size=12,
                     tooltip=tr("对话操作与设置"))
        self.session_menu_btn.pack(side=tk.RIGHT)
        self.clear_chat_btn = pill_button(right, tr("新建对话"), self._new_session,
                                         icon="＋", kind="ghost", bg=C["chat"], padx=8)
        self.clear_chat_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self.market_entry_btn = pill_button(right, tr("插件市场"), self._open_plugin_market,
                                            kind="quiet", bg=C["chat"])
        # Marketplace is reached from the sidebar shortcut.
        self.model_chip = None  # 模型选择统一放在 Composer。


        self.chat_area = cw.MessageArea(parent, bg=C["chat"])
        self._chat_empty = True

        # 模型选择器（在输入卡底栏里，由 _make_model_picker 建）
        self.model_combo = None
        self._team_mode = self._load_team_mode()

        self.input_card = cw.InputCard(
            parent, bg=C["chat"],
            placeholder=tr("让 Forge 构建、修复或调查……"),
            on_send=self._do_send,
            on_stop=self._stop_send,
            on_paste=self._paste_into_input,
            on_model=self._open_model_menu,
            model_var=self.model_var,
            on_thinking=self._open_thinking_menu,
            thinking_text=self._thinking_label(),
            thinking_var=self.reasoning_var,
            thinking_choices=REASONING_CHOICES,
            on_reasoning_select=self._set_reasoning_effort,
            on_reasoning_label=lambda effort: tr("模式：{label}", label=REASONING_LABELS.get(effort, effort)),
            on_planning=lambda: self._open_planning_menu(self.input_card.planning_pill),
            planning_text=PLANNING_LABELS[self._read_planning_level()],
            footer_left=tr("就绪"),
            model_widget=self._make_model_picker,
            on_attach=self._attach_files,
            on_context=self._open_context,
            on_commands=self._open_commands,
            on_project=lambda: self.desktop_features._choose_project(),
            on_project_files=lambda: self._nav_click("files"),
            on_team_settings=lambda: self._show_view("agents"),
            on_team_change=self._on_team_mode_changed,
            team_mode=self._team_mode,
        )
        self.input_card.pack(fill=tk.X, side=tk.BOTTOM, padx=20, pady=(0, 6))
        self.context_summary = tk.StringVar(value="历史上下文：开启 · 附件：0")
        self.context_summary_label = i18n.Label(
            parent, textvariable=self.context_summary, bg=C["chat"], fg=C["ter"],
            font=FONT_MICRO, anchor="w", padx=20)
        self.chat_area.pack(fill=tk.BOTH, expand=True)
        self.send_entry = self.input_card.entry
        self.send_var = self.input_card.send_var
        self.think_pill = self.input_card.think_pill
        self.think_pill.pack_forget()  # 任务专属控制仍可从会话菜单进入。
        self.model_var.trace_add("write", lambda *_: self._sync_composer_metadata())
        self.reasoning_var.trace_add("write", lambda *_: self._sync_composer_metadata())
        self._sync_composer_metadata()
        self.request_status_var = i18n.StringVar(self.root, value=tr("空闲"))
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
            show_thinking=False,
        )
        self.model_combo.pack(side=tk.LEFT, padx=(0, 8))
        attach_tooltip(self.model_combo, "选择模型（来自已启用的 Provider）")
        return self.model_combo

    def _sync_composer_metadata(self):
        if not hasattr(self, "input_card"):
            return
        model = self.model_var.get()
        provider = select_provider(self.user_rows, model)
        brand = brand_marks.detect(model=model, provider=provider) if provider else None
        row = next((row for row in self.user_rows if row.get("config") == provider), None)
        name = brand.label if brand else str(row.get("id")) if row else tr("未配置")
        effort = self.reasoning_var.get()
        mode = tr("标准") if effort == "off" else REASONING_LABELS.get(effort, effort)
        self.input_card.set_metadata(provider=name, mode=mode)
        pill = getattr(self.input_card, "planning_pill", None)
        if pill is not None:
            pill.configure(text=PLANNING_LABELS[self._read_planning_level()])

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
        dialog = i18n.Toplevel(self.root)
        dialog.title("供应商目录")
        dialog.bind("<Escape>", lambda _event: dialog.destroy() or "break")
        dialog.geometry("820x640")
        dialog.transient(self.root)
        dialog.configure(bg=C["bg"])
        dialog.minsize(620, 460)

        head = tk.Frame(dialog, bg=C["bg"], padx=20, pady=16)
        head.pack(fill=tk.X)
        i18n.Label(head, text=tr("供应商目录"), bg=C["bg"], fg=C["text"],
                 font=FONT_TITLE).pack(anchor=tk.W)
        i18n.Label(head, text=tr("每个供应商单独一条；标准 API 与订阅套餐分开列。"
                            "点一行即把 baseURL / wire 填进下面的输入框，你再补 apiKey。"),
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
        i18n.Label(search_row, text=tr("搜名称 / 别名 / 域名"), bg=C["bg"],
                 fg=C["muted"], font=FONT_SMALL).pack(side=tk.LEFT, padx=(8, 0))

        listing = cw.ScrollArea(dialog, bg=C["surface"], padx=0, pady=0)
        listing.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 12))

        footer = tk.Frame(dialog, bg=C["bg"], padx=20, pady=10)
        footer.pack(fill=tk.X)
        i18n.Label(footer, text=tr("「待确认」= 该地址我没核到一手来源，若报错请以官网控制台为准"),
                 bg=C["bg"], fg=C["muted"], font=FONT_CAPTION).pack(side=tk.LEFT)
        i18n.Button(footer, text=tr("关闭"), bg=C["surface2"], fg=C["text"],
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
                i18n.Label(listing.inner, text=tr("没有匹配的供应商"), bg=C["surface"],
                         fg=C["muted"], font=FONT_UI, pady=24).pack()
                return
            for preset in hits:
                card = tk.Frame(listing.inner, bg=C["surface"],
                                highlightthickness=1,
                                highlightbackground=C["border"])
                card.pack(fill=tk.X, pady=(0, 8))
                top = tk.Frame(card, bg=C["surface"], padx=12, pady=8)
                top.pack(fill=tk.X)
                icon, keep = brand_marks.mark_icon(preset.brand or None, 16, master=top)
                if icon is not None:
                    holder = i18n.Label(top, image=icon, bg=C["surface"])
                    holder.image = icon
                    holder.pack(side=tk.LEFT, padx=(0, 7))
                i18n.Label(top, text=preset.name, bg=C["surface"], fg=C["text"],
                         font=FONT_UI_BOLD).pack(side=tk.LEFT)
                tone = {catalog.SOURCE_CONFIRMED: C["ok"],
                        catalog.SOURCE_USER: C["ok"],
                        catalog.SOURCE_DOCS: C["muted"]}.get(
                    preset.source, C["warn"])
                i18n.Label(top, text=f"· {preset.source_label}", bg=C["surface"],
                         fg=tone, font=FONT_MICRO).pack(side=tk.LEFT, padx=(7, 0))
                if preset.docs:
                    i18n.Label(top, text=preset.docs, bg=C["surface"], fg=C["muted"],
                             font=FONT_CAPTION).pack(side=tk.RIGHT)
                plans_row = tk.Frame(card, bg=C["surface"], padx=12)
                plans_row.pack(fill=tk.X, pady=(0, 9))
                for plan in preset.plans:
                    # usable=False：本框架用不了（协议不支持）——置灰且不可点，
                    # 而不是给一份装不上的模板。
                    usable = getattr(plan, "usable", True)
                    chip = i18n.Button(
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
        entry.bind("<Escape>", lambda _e: dialog.destroy() or "break")
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

    def _toggle_temp_panel(self):
        self._temp_expanded = not getattr(self, "_temp_expanded", False)
        arrow = "▾" if self._temp_expanded else "▸"
        self.temp_toggle_var.set(f"{arrow} 一键配置 · 采样温度")
        if self._temp_expanded:
            self._temp_body.pack(fill=tk.X, pady=(4, 0))
        else:
            self._temp_body.pack_forget()
        self._update_temp_summary()

    def _update_temp_summary(self):
        current = self.temperature_var.get()
        label = {"0.7": "0.7 均衡", "1.0": "1.0 保守"}.get(current, "不设置")
        self.temp_summary_var.set(f"当前 {label}")

    def _paint_temperature_buttons(self) -> None:
        current = self.temperature_var.get()
        for value, btn in getattr(self, "_temperature_buttons", {}).items():
            selected = value == current
            btn.configure(bg=C["accent_soft"] if selected else C["surface2"],
                          fg=C["accent"] if selected else C["text"],
                          font=FONT_UI_BOLD if selected else FONT_UI)
        if getattr(self, "temp_summary_var", None) is not None:
            self._update_temp_summary()

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
        """Fixed mask reveals neither a prefix/suffix nor the credential length."""
        return "••••••••••••  Protected" if key else ""

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
        dialog = i18n.Toplevel(self.root)
        dialog.title("API 密钥")
        dialog.bind("<Escape>", lambda _event: dialog.destroy() or "break")
        dialog.geometry("760x600")
        dialog.transient(self.root)
        dialog.configure(bg=C["bg"])
        dialog.minsize(560, 420)

        head = tk.Frame(dialog, bg=C["bg"], padx=20, pady=16)
        head.pack(fill=tk.X)
        IconCanvas(head, "key", size=22, bg=C["bg"], fg=C["accent2"]).pack(
            side=tk.LEFT, padx=(0, 8))
        i18n.Label(head, text=tr("API 密钥"), bg=C["bg"], fg=C["text"],
                 font=FONT_TITLE).pack(side=tk.LEFT)
        self.key_status_var = i18n.StringVar(self.root)
        i18n.Label(head, textvariable=self.key_status_var, bg=C["bg"], fg=C["muted"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(12, 0))
        i18n.Label(head, text=tr("密钥只写入 ~/.forge/secrets.json（不入日志、不进命令行）"),
                 bg=C["bg"], fg=C["muted"], font=FONT_CAPTION).pack(side=tk.RIGHT)

        rows_host = cw.ScrollArea(dialog, bg=C["surface"], padx=0, pady=0)
        rows_host.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 12))

        footer = tk.Frame(dialog, bg=C["bg"], padx=20, pady=12)
        footer.pack(fill=tk.X)
        pill_button(footer, tr("完成"), dialog.destroy, kind="primary",
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
                    icon, keep = brand_marks.mark_icon(brand, 16, master=line)
                    if icon is not None:
                        holder = i18n.Label(line, image=icon, bg=C["surface"])
                        holder.image = icon
                        holder.pack(side=tk.LEFT, padx=(0, 6))
                i18n.Label(line, text=str(target["label"]), bg=C["surface"],
                         fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
                if target["disabled"]:
                    i18n.Label(line, text=tr("已停用"), bg=C["surface"], fg=C["warn"],
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
                i18n.Label(left, text=meta, bg=C["surface"], fg=C["muted"],
                         font=FONT_CAPTION, anchor="w").pack(fill=tk.X, pady=(3, 0))
                i18n.Label(top, text=state_text, bg=C["surface"], fg=state_fg,
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

                pill_button(entry_row, tr("保存"), save_key, kind="primary",
                            bg=C["surface"]).pack(side=tk.LEFT, padx=(6, 0))
                pill_button(entry_row, "清除", clear_key, kind="quiet",
                            bg=C["surface"]).pack(side=tk.LEFT, padx=(6, 0))
                entry.bind("<Return>", lambda _e, f=save_key: f())
            if not targets:
                i18n.Label(rows_host.inner, text=tr("用户层里还没有 provider 配置"),
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
        if not save_desktop_config(model_favorites=self._model_favorites):
            self._set_status("收藏已在当前窗口更新，但未能保存到磁盘", "warn")

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
            f"历史上下文：{tr('开启') if self._include_history else tr('关闭')} · "
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
        paths = filedialog.askopenfilenames(parent=self.root, title=tr("添加 UTF-8 文本附件"),
                                            initialdir=str(self._repo_root()))
        for path in paths:
            try:
                item = read_attachment(path, secret_scope=self._input_secret_scope())
                pending = [a for a in self._attachments if a["path"] != item["path"]] + [item]
                compose_prompt(self.send_var.get(), pending, secret_scope=self._input_secret_scope())
                self._attachments = pending
            except (OSError, ValueError) as exc:
                self._set_status(f"{Path(path).name}：{exc}", "warn")
                break
        self._update_context_summary()

    def _open_context(self):
        dialog = i18n.Toplevel(self.root)
        dialog.title("本轮上下文 · 发送前可检查")
        dialog.bind("<Escape>", lambda _event: dialog.destroy() or "break")
        dialog.geometry("720x540")
        dialog.transient(self.root)
        dialog.configure(bg=C["chat"])
        footer = tk.Frame(dialog, bg=C["chat"])
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=10)
        self.context_close_btn = pill_button(footer, tr("关闭"), dialog.destroy, kind="quiet", bg=C["chat"])
        self.context_close_btn.pack(side=tk.RIGHT)
        use_history = tk.BooleanVar(value=self._include_history)
        preview = scrolledtext.ScrolledText(dialog, wrap="word", bg=C["input_bg"],
                                            fg=C["text"], font=FONT_MONO_SM)
        def refresh():
            self._include_history = use_history.get()
            self._update_context_summary()
            selected = self._chat_history if self._include_history else []
            try:
                prompt = compose_prompt(self.send_var.get(), self._attachments, secret_scope=self._input_secret_scope())
            except ValueError as exc:
                prompt = str(exc)
            payload = [{"role": m.role, "content": m.content} for m in selected]
            payload.append({"role": "user", "content": prompt})
            preview.configure(state=tk.NORMAL)
            preview.delete("1.0", tk.END)
            preview.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
            preview.configure(state=tk.DISABLED)
        i18n.Checkbutton(dialog, text=tr("发送当前会话的历史消息"), variable=use_history,
                       command=refresh, bg=C["chat"], fg=C["text"], selectcolor=C["surface2"],
                       state=tk.DISABLED if self._sending else tk.NORMAL).pack(anchor="w", padx=12, pady=8)
        i18n.Label(dialog, text=tr("附件以添加时的文本快照发送。下方展示消息角色及实际内容。"),
                 bg=C["chat"], fg=C["ter"]).pack(anchor="w", padx=12)
        items = tk.Frame(dialog, bg=C["chat"])
        items.pack(fill=tk.X, padx=12, pady=6)
        for attachment in list(self._attachments):
            row = tk.Frame(items, bg=C["chat"])
            row.pack(fill=tk.X)
            i18n.Label(row, text=Path(attachment["path"]).name, bg=C["chat"],
                     fg=C["text"]).pack(side=tk.LEFT)
            def remove(item=attachment, widget=row):
                if item in self._attachments:
                    self._attachments.remove(item)
                widget.destroy()
                refresh()
            i18n.Button(row, text=tr("移除"), command=remove,
                      state=tk.DISABLED if self._sending else tk.NORMAL).pack(side=tk.RIGHT)
        preview.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        refresh()

    def _open_commands(self):
        items = (
            {"label": tr("新建对话"), "detail": "/new", "command": self._new_session},
            {"label": "任务执行", "detail": "/task · 使用 Forge Router",
             "command": lambda: self._show_view("task")},
            {"separator": True},
            {"label": "展开 / 收起工作区", "detail": "/workspace",
             "command": self._toggle_workspace},
            {"label": "查看仓库变更", "detail": "/changes",
             "command": lambda: self._open_workspace("changes")},
            {"label": "检查发送上下文", "detail": "/context",
             "command": self._open_context},
            {"label": tr("功能开关"), "detail": "/tools",
             "command": lambda: self._show_view("tools")},
        )
        return show_popover_menu(self.input_card, items, title=tr("工具与命令"),
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
        root = self._active_workspace().resolve()
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
        return f"任务沉思 · {THINKING_LABELS.get(self._thinking_mode, tr('关闭'))}"

    def _open_thinking_menu(self, anchor=None):
        items = [
            {"label": label, "detail": hint,
             "selected": self._thinking_mode == mode,
             "command": lambda m=mode: self._set_thinking_mode(m)}
            for mode, label, hint in THINKING_CHOICES
        ]
        return show_popover_menu(anchor or self.session_menu_btn, items, title=tr("任务沉思"),
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
        saved = save_desktop_config(reasoning_effort=effort)
        label = REASONING_LABELS[effort]
        if effort == "off":
            detail = "当前对话不发送额外推理强度"
        elif effort == "contemplate":
            detail = "对话使用高强度推理，Forge 任务启用沉思"
        else:
            detail = "支持思考强度的模型将在下一条消息生效"
        self._set_status(f"思考强度已设为「{label}」；{detail}" +
                         ("；桌面偏好保存失败，本次窗口仍可使用" if not saved else ""),
                         "ok" if saved else "warn")
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
        models = data.get("models") if isinstance(data, dict) else None
        providers = models.get("providers") if isinstance(models, dict) else None
        if not isinstance(providers, dict) or not providers:
            self._set_status("AutoClaw 配置里没有 provider", "warn")
            return
        rows = copy.deepcopy(self.user_rows)
        by_id = {r.get("id"): r for r in rows if r.get("id")}
        env_pairs = []
        skipped_placeholder = 0
        for pid, pconf in providers.items():
            if not isinstance(pconf, dict):
                skipped_placeholder += 1
                continue
            bk = pconf.get("baseUrl") or pconf.get("baseURL") or ""
            ak = pconf.get("apiKey") or ""
            if not isinstance(bk, str) or not isinstance(ak, str):
                skipped_placeholder += 1
                continue
            bk = bk.rstrip("/")
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
            save_user_layer(self.home, rows, expected_rows=self.user_rows)
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
            self.user_rows = rows
            self._editor_baseline = copy.deepcopy(rows)
            self._refresh_provider_list()
            self._set_status(f"配置已写入，但密钥库保存失败：{exc}；请修复密钥后再启动", "error")
            return
        self.user_rows = rows
        self._editor_baseline = copy.deepcopy(rows)
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
        msg = redact(msg)
        color = {
            "info": C["subtext"],
            "ok": C["ok"],
            "warn": C["warn"],
            "error": C["error"],
        }.get(level, C["subtext"])
        self.status_var.set(msg)
        self.status_lbl.configure(fg=color)

    # ── Provider 列表 ──
    # ── Provider 搜索过滤 + 新建（配置层改造 P1）──────────────

    def _provider_search_focus_in(self, _event=None):
        if self._provider_search_entry.get() == self._provider_search_placeholder:
            self._provider_search_entry.delete(0, tk.END)
            self._provider_search_entry.configure(fg=C["text"])
        self._search_active = True

    def _provider_search_focus_out(self, _event=None):
        if not self._provider_search_entry.get():
            self._search_active = False
            self._provider_search_entry.insert(0, self._provider_search_placeholder)
            self._provider_search_entry.configure(fg=C["placeholder"])
            self._filter_provider_list()

    def _filter_provider_list(self):
        """按搜索词过滤 Provider 列表（匹配显示名 / 真实 id / 模型名）。"""
        if not hasattr(self, "provider_list"):
            return
        query = (self.provider_search_var.get().strip().lower()
                 if getattr(self, "_search_active", False) else "")
        self.provider_list.delete(0, tk.END)
        self._provider_visible_indices = []
        for idx, text in getattr(self, "_provider_rows_all", []):
            if query and query not in text.lower():
                continue
            self.provider_list.insert(tk.END, text)
            self._provider_visible_indices.append(idx)
        if not self._provider_visible_indices:
            suffix = "（无匹配）" if query else "（空）"
            self.provider_list.insert(tk.END, f"暂无用户配置 {suffix}")
        # 保持选中项在过滤后仍可见
        try:
            cur = self._last_selected_provider_index
        except AttributeError:
            return
        if cur is not None:
            for vis_i, orig_i in enumerate(self._provider_visible_indices):
                if orig_i == cur:
                    self.provider_list.selection_clear(0, tk.END)
                    self.provider_list.selection_set(vis_i)
                    break

    def _new_provider(self):
        """新建 Provider：在右侧编辑区放一个最小模板，走原有整理→保存流水线。"""
        template = json.dumps({
            "id": f"custom__{time.strftime('%Y%m%d%H%M%S')}",
            "config": {
                "baseURL": "https://api.example.com/v1",
                "apiKey": "sk-…",
                "model": "model-name",
                "modelLabel": "显示名",
            },
        }, ensure_ascii=False, indent=2)
        if self._editor_is_dirty() and not messagebox.askyesno(
                "编辑内容尚未保存", "新建会替换当前编辑内容。要放弃未保存的编辑吗？",
                parent=self.root):
            return
        self.input_text.delete("1.0", tk.END)
        self.input_text.insert("1.0", template)
        self.input_text.configure(fg=C["text"])
        self._placeholder_visible = False
        self._pending_rows = []
        self._organized_input = ""
        self.save_btn.configure(state=tk.DISABLED)
        self.provider_list.selection_clear(0, tk.END)
        self._last_selected_provider_index = None
        self._sync_model_editor(None)
        self._set_status("已生成模板：改好 baseURL / apiKey / model 后点「整理并预览」再保存",
                         "info")
        self._set_preview("")
        self._set_warnings([])

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
                state = tr("关闭") if r.get("disabled") else tr("开启")
                # 有友好名时顺带显示真实 id，方便排查「上游不认模型」这类问题
                shown = (f"{label}  ({model})" if label and model and label != model
                         else (label or "未指定模型"))
                display.append(f"[{state}] {rid}  ·  {shown}")
            else:
                # 非 provider 行
                keys = ", ".join(list(conf.keys())[:3])
                display.append(f"{rid}  ·  {keys or '元数据'}")
        # 维护全量行（(原始 index, 文本)），供搜索过滤用；再按当前词过滤展示
        self._provider_rows_all = list(enumerate(display))
        self._filter_provider_list()
        self.provider_count_var.set(tr("{count} 条用户配置", count=len(rows)))
        # 模型下拉
        models = []
        for r in rows:
            if "baseURL" in r.get("config", {}) and not r.get("disabled"):
                m = r["config"].get("modelLabel") or r["config"].get("model") or r.get("id")
                if m and m not in models:
                    models.append(m)
        models = [m for m in models if m and m != "default"]
        self.model_combo.configure(values=models)
        self._sync_model_chip()
        self._sync_composer_metadata()
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
        visible = self._provider_visible_indices
        if sel[0] >= len(visible):
            return
        idx = visible[sel[0]]
        if idx >= len(self.user_rows):
            return
        if self._editor_is_dirty() and not messagebox.askyesno(
                "编辑内容尚未保存", "切换条目会替换当前编辑内容。要放弃未保存的编辑吗？", parent=self.root):
            return
        row = self.user_rows[idx]
        self._last_selected_provider_index = idx
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
                i18n.Label(chip_row, text=tr("常用:"), bg=C["input_bg"], fg=C["muted"],
                         font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 4))
                for m in suggestions:
                    i18n.Button(chip_row, text=m, bg=C["surface2"], fg=C["link"],
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
        rows = copy.deepcopy(self.user_rows)
        updated = next(r for r in rows if r["id"] == rid)
        old_model = updated["config"].get("model")
        updated["config"]["model"] = new_model
        if not updated["config"].get("smallModel") or updated["config"]["smallModel"] == old_model:
            updated["config"]["smallModel"] = new_model
        try:
            save_user_layer(self.home, rows, expected_rows=self.user_rows)
        except (OSError, ValueError) as exc:
            self._set_status(f"保存失败：{exc}", "error")
            return
        self.user_rows = rows
        self._editor_baseline = copy.deepcopy(rows)
        self._refresh_provider_list()
        self._sync_model_editor(updated)
        self._set_status(f"{rid} 的模型已改为 {new_model}", "ok")

    def _probe_selected_provider(self):
        """用密钥库里的 key 真实打一发 /chat/completions，结果写在面板上。"""
        rid = getattr(self, "_model_edit_row_id", None)
        if not rid:
            return
        row = next((r for r in self.user_rows if r.get("id") == rid), None)
        if not row:
            return
        conf = copy.deepcopy(row.get("config") or {})
        try:
            url, key, model = gateway_settings(conf, {**os.environ, **env_for()})
        except ValueError as exc:
            self.model_probe_var.set(str(exc))
            return
        generation = self._provider_probe_generation = getattr(self, "_provider_probe_generation", 0) + 1
        self.model_probe_var.set(tr("请求中…"))
        self.model_probe_btn.configure(state=tk.DISABLED)

        def worker():
            import urllib.request
            import urllib.error
            endpoint = url.rstrip("/") + "/chat/completions"
            body = json.dumps({"model": model, "messages": [{"role": "user", "content": "1+1=?"}], "max_tokens": 30}).encode()
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    j = json.loads(resp.read().decode())
                    reply = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    done(f"HTTP {resp.status} · {reply[:30]!r}", True)
            except urllib.error.HTTPError as e:
                from forge_client import http_error_detail
                detail = http_error_detail(e, 120).replace("\n", " ")
                done(f"HTTP {e.code} · {detail}", False)
            except Exception as e:
                done(f"{type(e).__name__}: {e}", False)

        def done(text, ok):
            def apply():
                if self._closing or generation != self._provider_probe_generation:
                    return
                self.model_probe_btn.configure(state=tk.NORMAL)
                current = next((r for r in self.user_rows if r.get("id") == rid), None)
                if getattr(self, "_model_edit_row_id", None) == rid and current and current.get("config") == conf:
                    self.model_probe_var.set(text)
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
        self.warning_count_var.set(f"{len(lines)} 项提示" if lines else tr("无提示"))
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
        folder = filedialog.askdirectory(title=tr("选择包含 run.py 的 Forge 目录"), parent=self.root)
        if not folder:
            return
        candidate = Path(folder) / "run.py"
        if not candidate.is_file():
            self._set_status("所选目录不包含 run.py，请选择 Forge 根目录", "error")
            return
        self.run_py = candidate
        self._refresh_work_context()
        if self.workspace is not None:
            self.workspace.set_repo_root(self._active_workspace())
        saved = save_desktop_config(forge_repo=str(candidate.parent))
        self.path_lbl.configure(text=tr("forge 目录已就绪"))
        self._set_status(("已记住 Forge 目录：" if saved else "Forge 目录已切换，但无法保存桌面偏好：") + folder,
                         "ok" if saved else "warn")

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
            return
        self._schedule_autostart_retry(self.status_var.get() or "启动失败")

    def _autostart_log(self, message: str) -> None:
        message = redact(message)
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
            self.gw_status_var.set(tr("● 离线"))
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

    def _set_read_scope(self, scope: str) -> None:
        """切换文件**读取**范围（不影响写沙箱），持久化后需重启 gateway。

        写权限仍由 --profile 决定：balanced 永远写不出工作区。
        这个开关只回答“能不能读工作区之外的东西”。
        """
        if scope not in ("workspace", "all"):
            return
        try:
            if not save_desktop_config(read_scope=scope):
                self._set_status(tr("读取范围保存失败，请重试。"), "error")
                return
        except Exception as exc:  # noqa: BLE001 - 保存失败不该把 UI 弄崩
            self._set_status(tr("读取范围保存失败：{err}").format(err=exc), "error")
            return
        try:
            self._read_scope_var.set(scope)
        except Exception:  # noqa: BLE001
            pass
        label = tr("全部磁盘") if scope == "all" else tr("仅工作区")
        self._set_status(tr("读取范围：{x}（重启 gateway 后生效）").format(x=label), "info")

    def _stop_gateway_from_menu(self):
        """用户主动停止：之后不再自动拉起，直到再次启动。"""
        self._gateway_user_stopped = True
        self._gateway_launch_cancel.set()
        self._cancel_gateway_launch_timer()
        if self._gateway_starting:
            self._gateway_starting = False
            self._gateway_down("已取消启动")
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
        """受理 gateway 启动请求；进程创建和失败回调在后台完成。"""
        if self._closing:
            # 关窗过程中可能有延迟定时器刚到点；别在退出路上又拉一个进程出来。
            return False
        if autostart and self._gateway_user_stopped:
            # 防住「重试回调已进入、用户刚好点击停止」的竞态。
            return False
        if self._gateway_starting:
            return True
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
        if not autostart:
            self._cancel_autostart_timer()
            self._autostart_attempts = 1
            self._gateway_autostarted = False
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
               "--port", str(port), "--home", str(self.home),
               # 工具桥：不带 --tools 时 gateway 的 /v1/tools 404、
               # /v1/tools/call 503，模型只能回复「无法访问你的电脑」。
               # workspace 沙箱 = 仓库根；balanced = 可读写工作区（全盘
               # 访问要 --profile aggressive + ack，不默认开）。
               "--tools", "--workspace", str(self._active_workspace()),
               "--profile", "balanced"]
        # 读范围独立于写沙箱：只追传 --read-scope，不动 --profile。
        # 默认 workspace 与 balanced 预设一致，行为零变化。
        _read_scope = str(load_desktop_config().get("read_scope", "workspace"))
        if _read_scope in ("workspace", "all"):
            cmd.extend(["--read-scope", _read_scope])
        if model_map:
            cmd.extend(["--model-map", model_map])
        provider_conf = upstream.get("config") or upstream
        adaptation = {key: provider_conf[key] for key in
                      ("vendor", "cacheControl", "expectedCalls", "callGapSeconds", "adapt", "thinkBudget",
                       "flex", "defer", "maxDeferSeconds")
                      if key in provider_conf}
        if adaptation:
            cmd.extend(["--provider-options", json.dumps(adaptation, ensure_ascii=False)])

        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace", cwd=str(self.run_py.parent))
        if IS_WINDOWS:
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        # Both port checks and CreateProcess can stall (including antivirus
        # and frozen executable extraction). Always launch off Tk.
        self._gateway_starting = True
        cancel = self._gateway_launch_cancel = threading.Event()
        self.gw_status_var.set(tr("● 检查端口中"))
        self.gw_btn.configure(state=tk.DISABLED, text=tr("检查端口…"))
        self.port_spin.configure(state=tk.DISABLED)
        self._gateway_launch_after_id = self.root.after(
            GATEWAY_LAUNCH_TIMEOUT_MS,
            lambda: self._gateway_launch_expired(cancel, autostart))

        def launch():
            nonlocal port  # 被占降级分支会重绑 port（OS 分配的可用端口）
            try:
                if port_in_use(port):
                    self._clear_stale_gateway_on_port(port)
                if cancel.is_set() or self._closing:
                    self._post_ui(self._finish_deferred_gateway, None, upstream, cancel, "已取消启动", autostart)
                    return
                if port_in_use(port):
                    # 清理后仍被占（占用者不是我们的孤儿，或清理失败）：
                    # 自动降级到 OS 分配的可用端口，保证「打开就能用」；
                    # 不再原地退避重试同一个死端口。端口三件套（命令行/
                    # port_var/client）同步由 _gateway_launch_stage 统一处理。
                    port = bindable_gateway_port(0)
                actual_port = bindable_gateway_port(port)
                if cancel.is_set() or self._closing:
                    return
                cmd[cmd.index("--port") + 1] = str(actual_port)
                self._post_ui(self._gateway_launch_stage, cancel, port, actual_port)
                proc = subprocess.Popen(cmd, env=env, **kwargs)
                with self._gateway_launch_lock:
                    dispose = cancel.is_set() or self._closing
                    if not dispose:
                        # Closing collects this child even before Tk accepts it.
                        self._gateway_pending_procs.append(proc)
                        self._post_ui(self._finish_deferred_gateway, proc, upstream, cancel, "", autostart)
                if dispose:
                    kill_process_tree(proc)
            except Exception as exc:
                self._post_ui(self._finish_deferred_gateway, None, upstream, cancel, str(exc), autostart)

        threading.Thread(target=launch, daemon=True, name="Forge-gateway-launch").start()
        return True

    def _cancel_gateway_launch_timer(self):
        timer = self._gateway_launch_after_id
        self._gateway_launch_after_id = None
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except (tk.TclError, ValueError):
                pass

    def _gateway_launch_expired(self, cancel, autostart):
        if cancel is not self._gateway_launch_cancel or not self._gateway_starting:
            return
        cancel.set()
        self._gateway_launch_after_id = None
        self._gateway_starting = False
        reason = "启动准备超时（30 秒）；请检查 Python / 端口，或点击启动重试"
        self._gateway_down(reason)
        self._autostart_log(reason)
        # A hung CreateProcess/probe may still be in flight: do not spawn more
        # overlapping attempts. A late child is disposed by its original worker.

    def _gateway_launch_stage(self, cancel, requested_port, actual_port):
        if cancel is not self._gateway_launch_cancel or cancel.is_set() or self._closing:
            return
        self.gateway_port = actual_port
        self.gateway_url = f"http://127.0.0.1:{actual_port}"
        self.client.base_url = self.gateway_url
        self.port_var.set(str(actual_port))
        if requested_port != actual_port:
            reason = f"端口 {requested_port} 被 Windows 禁止绑定，改用 {actual_port}"
            self._autostart_log(reason)
            self._push_terminal(f"[gateway] {reason}")
            self._set_status(reason, "info")
        self.gw_status_var.set(tr("● 启动中"))
        self.gw_btn.configure(text=tr("启动中…"))

    def _finish_deferred_gateway(self, proc, upstream, cancel, error, autostart):
        with self._gateway_launch_lock:
            self._gateway_pending_procs = [p for p in self._gateway_pending_procs if p is not proc]
        if cancel is not self._gateway_launch_cancel or cancel.is_set() or self._closing:
            if proc is not None and proc.poll() is None:
                threading.Thread(target=kill_process_tree, args=(proc,), daemon=False).start()
            return
        self._cancel_gateway_launch_timer()
        self._gateway_starting = False
        if proc is None:
            self._gateway_down(error or "启动失败")
            self._autostart_log(f"启动准备失败：{error}")
            if autostart:
                self._schedule_autostart_retry(error)
            return
        if autostart:
            self._autostart_log("进程已创建，等待网关就绪")
        self._gateway_started(proc, upstream)

    def _gateway_started(self, proc, upstream):
        self.gateway_proc = proc
        self._gateway_ready_proc = None
        self._gateway_provider = copy.deepcopy(upstream)
        self.gw_status_var.set(tr("● 启动中"))
        self.gw_btn.configure(state=tk.DISABLED, text=tr("启动中…"))
        self.port_spin.configure(state=tk.DISABLED)
        probe = ForgeGatewayClient(self.gateway_url)
        threading.Thread(target=self._drain_gateway_log, args=(proc,), daemon=True).start()
        threading.Thread(target=self._gateway_watchdog, args=(proc, probe), daemon=True).start()

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
                 f"$forgeProcess = Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" -ErrorAction SilentlyContinue; "
                 "if ($forgeProcess) { [pscustomobject]@{ CommandLine=$forgeProcess.CommandLine; "
                 "ParentAlive=[bool](Get-Process -Id $forgeProcess.ParentProcessId -ErrorAction SilentlyContinue) } | ConvertTo-Json -Compress }"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        try:
            process_info = json.loads(info)
            argv = [token.strip('"') for token in shlex.split(process_info.get("CommandLine") or "", posix=False)]
            home_arg = argv[argv.index("--home") + 1]
            owned = ("gateway" in argv and self.run_py is not None
                     and any(Path(token).resolve() == self.run_py.resolve() for token in argv if token.lower().endswith("run.py"))
                     and Path(home_arg).resolve() == Path(self.home).resolve()
                     and not process_info.get("ParentAlive", True))
            if not owned:
                # 放宽一步：同类 forge gateway（同 home）即使父进程判定异常
                # （WMI 查不到父进程信息）也允许清理——它占的就是我们的端口。
                try:
                    same_home = Path(home_arg).resolve() == Path(self.home).resolve()
                except (OSError, ValueError):
                    same_home = False
                owned = ("gateway" in argv and same_home
                         and self.run_py is not None
                         and any(token.lower().endswith("run.py") for token in argv))
        except (ValueError, IndexError, TypeError, AttributeError, OSError):
            owned = False
        if not owned:
            # 不是我们的 gateway，别动（可能是用户自己的服务）
            self._post_ui(self._set_status, f"端口 {port} 被正在运行的程序占用（PID {pid}）", "warn")
            return
        try:
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                self._post_ui(self._push_terminal, f"[gateway] 已清理占用端口 {port} 的旧进程（PID {pid}）")
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
            {"label": tr("新建对话"), "detail": "Ctrl+N", "command": self._new_session},
            {"separator": True},
            {"label": "上下文与附件", "command": self._open_context},
            {"label": f"回复温度 · {self.temp_var.get()}",
             "command": self._open_temperature_menu},
            {"label": "打开配置", "command": lambda: self._show_view("config")},
        ], title=tr("对话操作"), width=320)

    def _open_temperature_menu(self):
        show_popover_menu(self.session_menu_btn, [
            {"label": value, "selected": self.temp_var.get() == value,
             "command": lambda v=value: self._set_temperature(v)}
            for value in ("0.2", "0.5", "0.7", "1.0", "1.5")
        ], title=tr("回复温度"), width=240)

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
        was_ready = self._gateway_ready_proc is proc
        self._gateway_down(f"进程已退出（代码 {code}）")
        self._autostart_log(f"网关退出：代码 {code}，就绪={was_ready}")
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
        if not was_ready and self._gateway_autostarted:
            self._schedule_autostart_retry(f"就绪前退出（代码 {code}）；详情见工作区终端")
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
            self.gw_btn.configure(state=tk.NORMAL, text=tr("⟳ 重启"))
            self.gw_status_var.set(tr("● 停止失败"))
            self._set_status("未能停止 gateway，请重试", "error")

    def _gateway_timeout(self, proc):
        if self.gateway_proc is proc and proc.poll() is None:
            self.gw_status_var.set(tr("● 未就绪"))
            self.gw_status_lbl.configure(fg=C["warn"])
            self.gw_detail_status_lbl.configure(fg=C["warn"])
            self.gw_btn.configure(text=tr("⟳ 重启"), bg=C["accent"],
                                  state=tk.DISABLED if self._sending else tk.NORMAL)
            self._set_status("gateway 启动未就绪；可点「重启」重试，或检查端口/配置", "warn")
            # 起来但一直不健康，多半是端口/配置问题：收掉这个进程再退避重试，
            # 否则它会一直挂着，而用户看到的就是「打开了但没起来」。
            if self._gateway_autostarted and not self._gateway_user_stopped:
                self._autostart_log("启动后 30s 未就绪，收掉进程并重试")
                self._autostart_attempts = max(1, self._autostart_attempts)
                self._stop_gateway()

    def _gateway_up(self, proc):
        if self.gateway_proc is not proc or proc.poll() is not None:
            return
        self._project_switch_pending = False
        self._gateway_ready_proc = proc
        self._autostart_attempts = 0
        self._autostart_log(f"网关已就绪：端口 {self.gateway_port}")
        save_desktop_config(gateway_port=self.gateway_port)
        self.gw_status_var.set(f"● 在线 ({self.gateway_port})")
        self.gw_status_lbl.configure(fg=C["ok"])
        self.gw_detail_status_lbl.configure(fg=C["ok"])
        self.gw_btn.configure(text=tr("⟳ 重启"), bg=C["surface2"], fg=C["body"],
                              state=tk.DISABLED if self._sending else tk.NORMAL)
        self.port_spin.configure(state=tk.DISABLED if self._sending else tk.NORMAL)
        self._set_status(f"gateway 在线：{self.gateway_url}", "ok")

    def _gateway_down(self, reason: str = "已停止"):
        self.gw_status_var.set(tr("● 离线"))
        self.gw_status_lbl.configure(fg=C["muted"])
        self.gw_detail_status_lbl.configure(fg=C["muted"])
        self.gw_btn.configure(text=tr("▶ 启动"), bg=C["accent"], fg="#FFFFFF",
                              state=tk.DISABLED if self._sending else tk.NORMAL)
        self.port_spin.configure(state=tk.DISABLED if self._sending else tk.NORMAL)
        self._set_status(f"gateway {reason}", "warn")
        self.gateway_proc = None
        self._gateway_ready_proc = None

    def _stop_gateway(self):
        if not self.gateway_proc:
            return
        proc = self.gateway_proc
        self.gw_btn.configure(state=tk.DISABLED, text=tr("停止中…"))
        self._stopping_proc = proc
        self.gw_status_var.set(tr("● 停止中"))
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
            self._show_chat_start()

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
            return area.add_notice(text, tone="error", title=tr("错误"))
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

    # ── 底栏三态开关（关 / AI 决断 / 开）──────────────────────

    def _load_team_mode(self) -> str:
        try:
            cfg = load_desktop_config()
        except Exception:
            return "off"
        mode = (cfg or {}).get("team_mode", "off")
        return mode if mode in ("off", "auto", "on") else "off"

    def _on_team_mode_changed(self, mode: str) -> None:
        if mode not in ("off", "auto", "on"):
            return
        self._team_mode = mode
        try:
            saved = save_desktop_config(team_mode=mode)
        except Exception:
            saved = False
        label = {"off": "关（单模型直答）", "auto": "AI 决断（本地规则判断）",
                 "on": "开（按集群/分工配置并行）"}.get(mode, mode)
        self._set_status(f"Agent 协作模式：{label}" + ("；偏好未保存，本次仍生效" if not saved else ""),
                         "info" if saved else "warn")

    #: AI 决断的本地启发式（零成本、零额外请求）：命中「需要多视角」的语言特征
    #: 或足够长的复合问题才并行。宁可漏并行，不可滥并行（额度纪律）。
    _AUTO_HINTS = ("对比", "评测", "评估", "调研", "全面", "多角度", "多方案",
                   "审查", "评审", "利弊", "优劣", "几种", "哪些方案", "设计一下",
                   "架构", "选型", "compile", "review", "compare", "trade-off",
                   "调研报告", "深度")

    def _auto_should_parallel(self, prompt: str) -> bool:
        if len(prompt) >= 120:
            return True
        return any(h in prompt for h in self._AUTO_HINTS)

    def _plan_sidecars(self, cfg: dict, prompt: str) -> dict | None:
        """决定本轮要不要跑 sidecar；返回计划或 None。

        语义照抄 route.ts：
          - 子 Agent 分工开启且有预设 → 走分工
          - 否则集群开启 → 走集群（1-4 路同构方案）
          - 两者都开时分工优先（互斥，防双份注入）

        三态开关在原语义之前裁决：
          off  → 恒 None（单模型直答，配置再开也不跑）
          auto → 本地启发式判断该不该并行（不额外调模型，零成本）
          on   → 按集群/分工配置强制走
        """
        mode = getattr(self, "_team_mode", "off")
        if mode == "off":
            return None
        if mode == "auto" and not self._auto_should_parallel(prompt):
            return None
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
        env = plan["environment"]
        provider_rows = plan["provider_rows"]
        default_provider = plan["default_provider"]
        try:
            default_model = str((default_provider or {}).get("model") or "")
        except Exception:
            default_model = ""
        kind = plan["kind"]
        context = plan.get("context")

        if kind == "sub":
            agents = team.build_sub_agents(plan["presets"],
                                           plan["prompt"],
                                           context_text=context)
            if not agents:
                return ""
            # 模型反查 provider：preset 指定的模型可能属于另一家 provider
            for a, preset in zip(agents, plan['presets']):
                a.provider, a.model_name = team.resolve_selection(provider_rows, preset, default_provider)
            self._post_ui(self._set_request_status,
                          f"子任务并行 {len(agents)} 路…")
            results = team.run_sub_agents(agents, env,
                                          default_provider=default_provider,
                                          default_model=default_model, cancel_event=cancel_event,
                                          communication=cfg.get('communication',{}).get('enabled') is True,
                                          communication_rounds=cfg.get('communication',{}).get('rounds',1))
            injection = team.sub_agent_injection(results)
        else:
            lanes = []
            for lane in plan.get("lanes") or []:
                provider, lane_model = team.resolve_selection(provider_rows, lane, default_provider)
                lanes.append({"provider": provider, "model": lane_model})
            self._post_ui(self._set_request_status,
                          f"Agent 集群 {plan['count']} 路并行…")
            results = team.run_cluster(
                plan["prompt"], plan["count"], lanes, env,
                default_provider=default_provider,
                default_model=default_model, context_text=context, cancel_event=cancel_event,
                communication=cfg.get('communication',{}).get('enabled') is True,
                communication_rounds=cfg.get('communication',{}).get('rounds',1))
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

    def _post_tool_round(self, rows: list[dict], round_no: int):
        """模型发起的工具调用：每轮一张折叠卡片挂在本轮消息上。"""
        msg = getattr(self, "_agent_msg", None)
        if msg is None or not rows:
            return
        try:
            msg.add_tool_card(rows, title=f"工具调用 · 第 {round_no} 轮",
                              expanded=False)
        except Exception:
            pass

    def _post_note_to_msg(self, text: str):
        msg = getattr(self, "_agent_msg", None)
        if msg is None:
            return
        try:
            msg.add_note(text, tone="warn")
        except Exception:
            pass

    def _post_sidecar_card(self, title: str, rows: list[dict]):
        msg = getattr(self, "_agent_msg", None)
        if msg is None or not rows:
            return
        try:
            msg.add_tool_card(rows, title=title, expanded=False)
        except Exception:
            pass

    def _retry_last_agent(self, msg=None):
        """Prepare the clicked turn for editing; only Send may start another run."""
        if getattr(self, "_sending", False):
            self._set_status("正在生成回复，稍后再试", "info")
            return
        prompt = getattr(msg, "_retry_prompt", None)
        if (not prompt or getattr(msg, "_retry_session", None) != self._session_id
                or not msg.winfo_exists()):
            self._set_status("没有可重试的消息", "warn")
            return
        if self.send_var.get().strip() or self._attachments:
            self.input_card.focus_entry()
            self._set_status(tr("已有草稿或附件，已保留；清空后可编辑重试原消息。"), "info")
            return
        self._attachments = copy.deepcopy(list(getattr(msg, "_retry_attachments", ())))
        self.send_var.set(prompt)
        self._update_context_summary()
        self.input_card.focus_entry()
        self._set_status(
            tr("历史消息已作为文字快照放入输入框，请检查后发送。")
            if getattr(msg, "_retry_from_history", False) else
            tr("原消息已放入输入框；确认后发送，工具仍需经过权限检查。"), "info")

    def _input_secret_scope(self):
        scope = getattr(self.client, 'secret_scope', None)
        if scope is not None:
            return scope
        # Trusted embedded/test clients still get one scope per conversation.
        previous = getattr(self, '_fallback_secret_scope', None)
        sid = getattr(self, '_session_id', '')
        if previous is None or previous[0] != sid:
            if previous is not None:
                previous[1].close()
            from forge.secrets import SecretScope
            previous = self._fallback_secret_scope = (sid, SecretScope())
        return previous[1]

    def _do_send(self):
        if self._sending:
            return
        text = self.send_var.get().strip()
        if not text:
            return
        if self._run_local_command(text):
            return
        if self._gateway_starting or self._restart_pending or getattr(self, '_project_switch_pending', False):
            self._set_status(tr('正在切换或启动工作区，请等待 Gateway 就绪后发送'),'warn')
            return
        try:
            prompt = compose_prompt(text, self._attachments, secret_scope=self._input_secret_scope())
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
        try:
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
            previous_review = getattr(getattr(self, '_agent_msg', None), '_review_action', None)
            if previous_review is not None and previous_review.winfo_exists():
                previous_review.master.destroy()
            self._agent_msg = self.chat_area.add_agent(app=self)
            self._agent_msg._retry_session = self._session_id
            self._agent_msg._retry_prompt = self._input_secret_scope().protect_text(text)
            self._agent_msg._retry_attachments = copy.deepcopy(self._attachments)
            self._agent_msg.set_status("生成中…")
            self._agent_msg.stream_text("")
            # Explicit Send reveals the new turn; incoming deltas alone never
            # take scroll ownership back from someone reading earlier messages.
            self.chat_area.scroll.scroll_to_end()
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
            if sidecar_plan is not None:
                sidecar_plan.update(environment={**os.environ, **env_for()},
                                    provider_rows=copy.deepcopy(self.user_rows), prompt=prompt,
                                    default_provider=copy.deepcopy(select_provider(self.user_rows, model)))
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
            plugin_workspace, plugin_session = self._active_workspace(), self._session_id
            reasoning_effort = ("high" if self._reasoning_effort == "contemplate"
                                else None if self._reasoning_effort == "off" else self._reasoning_effort)
            planning = self._read_planning_level()  # capture all Tk/config state on the UI thread
            planning_model = self._phase_setting('planning', 'model')
            phase_rows = copy.deepcopy(self.user_rows)
            phase_env = {**os.environ, **env_for()}
            phase_scope = self._input_secret_scope()
            desktop = getattr(self,'desktop_features',None)
            selected_workspace = (desktop.workspace() if desktop is not None else self._active_workspace())
            if desktop is not None:
                desktop.current_workspace = selected_workspace
                with desktop.lock: desktop.rows = copy.deepcopy(self.user_rows)

            def worker():
                try:
                    ok, msg = client.health(cancel_event=cancel_event)
                    if not ok:
                        raise GatewayError(f"gateway 未连接：{msg}。请先启动 gateway 后重试。")
                    if cancel_event.is_set():
                        raise GenerationCancelled("已停止生成")
                    if planning != "none":
                        self._post_ui(self._set_request_status, tr("事前规划"))
                        planner = client
                        if planning_model:
                            from phase_client import PhaseClient
                            planner = PhaseClient(phase_rows, planning_model, phase_env, phase_scope, cancel_event)
                        plan, plan_usage = planner.plan_task(messages, planning, model=model,
                                                           cancel_event=cancel_event)
                        if cancel_event.is_set():
                            raise GenerationCancelled("已停止生成")
                        messages[-1] = ChatMessage("user", messages[-1].content + "\n\n" + plan.context())
                        self._post_ui(self._post_note_to_msg, plan.display())
                        if isinstance(plan_usage, dict):
                            self._post_ui(self._post_note_to_msg,
                                "Planning usage: " + json.dumps(plan_usage, ensure_ascii=False))
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
                                            ChatMessage("user", injection))
                        if cancel_event.is_set():
                            raise GenerationCancelled("已停止生成")
                    # ── 工具桥 + 插件工具：合并成一份 tools 给模型 ──
                    # 网关工具（bridge）：/v1/tools 拉取，走 gateway 执行
                    tools = None
                    try:
                        tools = client.list_tools() or None
                    except Exception:
                        # 老 gateway / 未开 --tools：按无工具模式对话（保持旧行为）
                        tools = None
                    # Plugin aliases are declarative, scoped and granted. Never import third-party Python.
                    bridge_names = {str((item.get("function") or {}).get("name", ""))
                                    for item in (tools or []) if isinstance(item, dict)}
                    def mediated_dispatch(name,args,**context):
                        if desktop is not None:
                            args=desktop.review_change(name,args,selected_workspace,phase_scope,cancel_event)
                        response=client.call_tool(name,args,timeout=3.0,**context)
                        if desktop is not None and isinstance(response,dict) and response.get('ok') and '_expected_revisions' in args:
                            self._post_ui(desktop.refresh_workspace)
                        return response
                    from forge.desktop_services import OPERATIONS as connector_operations
                    plugin_schemas, plugin_rt = self._reload_plugin_tools(reserved_names=bridge_names | set(connector_operations),
                        workspace=plugin_workspace, session=plugin_session,
                        dispatch=mediated_dispatch)
                    if plugin_schemas:
                        tools = list(tools or []) + plugin_schemas
                    connector_schemas = desktop.schemas() if desktop is not None else []
                    occupied = bridge_names | set(getattr(plugin_rt,'tools',{}))
                    connector_schemas = [item for item in connector_schemas
                                         if item['function']['name'] not in occupied]
                    connector_names = {item['function']['name'] for item in connector_schemas}
                    tools = list(tools or []) + connector_schemas
                    if cancel_event.is_set():
                        raise GenerationCancelled("已停止生成")
                    n_bridge = len(bridge_names)
                    parts = []
                    if n_bridge > 0:
                        parts.append(f"网关 {n_bridge}")
                    if plugin_schemas:
                        parts.append(f"插件 {len(plugin_schemas)}")
                    if connector_schemas:
                        parts.append(tr('连接器')+' '+str(len(connector_schemas)))
                    if parts:
                        self._post_ui(self._set_request_status,
                                      f"已加载 {' + '.join(parts)} 工具，正在生成…")
                    else:
                        self._post_ui(self._set_request_status, "正在生成…")
                    acc: list[str] = []

                    def on_chunk(piece: str):
                        if cancel_event.is_set():
                            raise GenerationCancelled("已停止生成")
                        acc.append(piece)
                        self._post_ui(self._append_stream_delta, piece)

                    TOOL_ROUNDS = 8   # 防失控：模型最多连续调 8 轮工具
                    from forge.execution_guard import ExecutionGuard
                    execution_guard = ExecutionGuard()
                    rounds = 0
                    while True:
                        round_start = len(acc)
                        result = client.stream_chat(
                            messages, model=model,
                            temperature=temp,
                            reasoning_effort=reasoning_effort,
                            on_chunk=on_chunk, cancel_event=cancel_event,
                            tools=tools,
                        )
                        # 防御：stream_chat 理论必返回 CompletionResult，
                        # 但测试 stub/异常实现可能返回 None —— 不能因此炸掉整轮
                        turn_text = "".join(acc[round_start:]) or (
                            result.text if result is not None else "")
                        calls = (list(getattr(result, "tool_calls", None) or [])
                                 if result is not None else [])
                        if not calls:
                            break   # 纯文本 → 本轮对话完成
                        # 模型发起了工具调用：按 OpenAI 协议回填 assistant(tool_calls)
                        # + 每个 call 一条 tool 结果，然后再问一轮
                        messages.append(ChatMessage(
                            role="assistant", content=turn_text, tool_calls=calls,
                            reasoning_content=str(getattr(result, "reasoning_content", "") or "")))
                        rows = []
                        for call in calls:
                            if cancel_event.is_set():
                                raise GenerationCancelled("已停止生成")
                            fn = call.get("function") or {}
                            name = str(fn.get("name") or "")
                            raw_args = fn.get("arguments") or "{}"
                            try:
                                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                            except ValueError:
                                args = {"_raw": str(raw_args)}
                            if not isinstance(args, dict):
                                args = {"value": args}
                            try:
                                # The gateway owns its names even if a stale runtime claims them.
                                if name in bridge_names:
                                    if desktop is not None:
                                        args = desktop.review_change(name,args,selected_workspace,phase_scope,cancel_event)
                                    resp = client.call_tool(name, args)
                                    if desktop is not None and isinstance(resp,dict) and resp.get('ok') and '_expected_revisions' in args:
                                        self._post_ui(desktop.refresh_workspace)
                                elif name in connector_names:
                                    resp = desktop.call(name,args,cancel_event)
                                elif plugin_rt is not None and name in plugin_rt.tools:
                                    resp = plugin_rt.call(name, args)
                                else:
                                    resp = client.call_tool(name, args)
                                ok_flag = bool(resp.get("ok")) if isinstance(resp, dict) else True
                                body_text = json.dumps(resp, ensure_ascii=False)
                            except Exception as tool_exc:
                                resp = {"ok": False,
                                        "error": f"{type(tool_exc).__name__}: {tool_exc}"}
                                ok_flag = False
                                body_text = str(tool_exc)
                            messages.append(ChatMessage(
                                role="tool", content=body_text,
                                tool_call_id=str(call.get("id") or "")))
                            if execution_guard.observe(name, args, ok_flag, body_text):
                                raise GatewayError("重复工具调用没有进展，已停止；请检查错误或调整任务。")
                            rows.append({
                                "name": name,
                                "desc": json.dumps(args, ensure_ascii=False)[:70],
                                "elapsed": f"第{rounds + 1}轮",
                                "ok": ok_flag,
                            })
                        rounds += 1
                        if rows:
                            self._post_ui(self._post_tool_round, rows, rounds)
                            self._post_ui(self._set_request_status,
                                          f"正在执行工具（第 {rounds} 轮）…")
                        if rounds >= TOOL_ROUNDS:
                            self._post_ui(self._post_note_to_msg,
                                          f"已达工具调用轮数上限（{TOOL_ROUNDS} 轮），停止继续调用")
                            break

                    full = "".join(acc)
                    if cancel_event.is_set():
                        raise GenerationCancelled("已停止生成")
                    self._post_ui(self._chat_succeeded, retained_history, full, text,
                                  str(getattr(result, "reasoning_content", "") or ""))
                except GenerationCancelled:
                    self._post_ui(self._chat_cancelled, text)
                except Exception as e:
                    error_text = f"{type(e).__name__}: {e}"
                    self._post_ui(self._chat_failed, error_text, text)
                finally:
                    self._post_ui(self._send_finished)

        except Exception as exc:
            self._send_finished()
            self._set_status(f"发送失败：{exc}", "error")
            return

        threading.Thread(target=worker, daemon=True).start()

    def _chat_succeeded(self, messages, full, original_prompt=None, reasoning_content=""):
        if self._abort_requested:
            self._chat_cancelled(original_prompt if original_prompt is not None else messages[-1].content)
            return
        self._chat_history = messages + [ChatMessage("assistant", full, reasoning_content=reasoning_content)]
        self._attachments.clear()
        self._update_context_summary()
        self.chat_title_var.set(self._session_title())
        msg = getattr(self, "_agent_msg", None)
        if msg is not None:
            msg.set_status("")
            msg.render_markdown(full or "（空回复）")
            actions = [{"label": tr("打开工作区"),
                        "command": lambda: self._open_workspace("file_tree")}]
            actions.extend(self._file_actions(full))
            msg.add_actions(actions)
            self._attach_review_action(msg, full)
        self._archive_current_session()
        self._refresh_history()
        self._set_status("回复完成", "ok")

    def _attach_review_action(self, msg, code, *, task=False):
        if not msg.winfo_exists(): return
        previous = getattr(msg, '_review_action', None)
        if previous is not None and previous.winfo_exists(): previous.master.destroy()
        msg._review_source = code
        if not getattr(msg, '_forge_message_id', None): msg._forge_message_id = uuid.uuid4().hex
        if code.strip() and self._phase_setting('review', 'enabled', False) is True:
            msg._review_action = msg.add_actions([{'label': tr('复审'),
                'command': lambda: self._review_response(msg, code, task=task)}])

    def _review_response(self, msg, code, *, task=False):
        if (self._sending or getattr(self, '_task_running', False)
                or self._phase_setting('review', 'enabled', False) is not True):
            return
        current = getattr(self, '_task_msg' if task else '_agent_msg', None)
        if msg is not current or not msg.winfo_exists() or not code.strip(): return
        rows = copy.deepcopy(self.user_rows)
        selection = self._phase_setting('review', 'model')
        environment = {**os.environ, **env_for()}
        scope = self._input_secret_scope()
        client, model = self.client, self.model_var.get()
        if self.gateway_proc is not None:
            provider = select_provider(rows, model)
            if provider is not None: model = provider['model']
        sid, identifier = self._session_id, uuid.uuid4().hex
        message_id = getattr(msg, '_forge_message_id', identifier)
        msg._forge_review_id = identifier
        self._sending = True
        self._abort_requested = False
        self._cancel_event = cancel = threading.Event()
        self.input_card.set_busy(True)
        self._set_request_status(tr('复审中…'))
        for button in getattr(getattr(msg, '_review_action', None), '_buttons', ()):
            button.configure(state=tk.DISABLED, text=tr('复审中…'))
        def finish(result=None, error=None):
            current = getattr(self, '_task_msg' if task else '_agent_msg', None)
            if (not self._closing and self._session_id == sid and current is msg
                    and getattr(msg, '_forge_review_id', None) == identifier and msg.winfo_exists()):
                if cancel.is_set():
                    feedback = i18n.resolve(tr('复审已停止。'), msg)
                elif error:
                    feedback = i18n.resolve(tr('复审失败'), msg) + ': ' + redact(error)
                else:
                    prefix = i18n.resolve(tr('审核意见'), msg)
                    if result.truncated: prefix += '\n' + i18n.resolve(tr('仅审核前 20000 字符，未覆盖全部内容。'), msg)
                    feedback = prefix + '\n' + result.text + '\nReview usage: ' + json.dumps(result.usage, ensure_ascii=False)
                previous = getattr(msg, '_review_feedback', None)
                if previous is not None and previous.winfo_exists(): previous.destroy()
                msg._review_feedback = msg.add_note(feedback, tone='muted')
            for button in getattr(getattr(msg, '_review_action', None), '_buttons', ()):
                if button.winfo_exists(): button.configure(state=tk.NORMAL, text=tr('复审'))
            self._send_finished()
        def worker():
            try:
                reviewer = client
                if selection:
                    from phase_client import PhaseClient
                    reviewer = PhaseClient(rows, selection, environment, scope, cancel)
                result = reviewer.review_code(code, model=model, cancel_event=cancel)
                if cancel.is_set(): raise GenerationCancelled('stopped')
                from forge.session import Session
                with Session(self.home / 'sessions' / 'reviews' / (identifier + '.jsonl')) as log:
                    log.append('review_result', message_id=message_id, source_session=sid,
                               review=result.to_dict())
                self._post_ui(finish, result, None)
            except Exception as exc:
                self._post_ui(finish, None, str(exc))
        threading.Thread(target=worker, daemon=True, name='Forge-code-review').start()

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
            area.add_notice(message, tone="error", title=tr("gateway 请求失败"))
        if not self.send_var.get():
            self.send_var.set(prompt)
        self._set_status("请求失败，消息已保留；检查 gateway 后可重试", "error")

    def _stop_send(self):
        """请求中止当前回复，即时反馈并等待工作线程结束连接。"""
        if not self._sending:
            return
        self._abort_requested = True
        self._cancel_event.set()
        self.input_card.set_stopping()
        self._set_request_status("正在停止…")
        self._set_status("已请求停止，正在结束本轮连接；输入的草稿会保留", "warn")
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
        self._set_request_status(tr("就绪"))
        self._refresh_send_circle()
    # ── 关闭 ──
    def _on_close(self):
        if (self._feature_dirty or self._team_dirty or self._editor_is_dirty() or self._pending_rows) and not messagebox.askyesno(
                "有未保存的修改", "功能开关、分工预设或编辑内容尚未保存。要放弃这些修改并退出吗？", parent=self.root):
            return
        self._closing = True
        desktop = getattr(self,'desktop_features',None)
        if desktop is not None: desktop.close()
        self._cancel_history_render()
        self._cancel_market_render()
        self._cancel_market_filter()
        self._gateway_launch_cancel.set()
        self._cancel_gateway_launch_timer()
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
        monitor = self._sysmon
        processes = []
        with self._gateway_launch_lock:
            pending_procs = self._gateway_pending_procs
            self._gateway_pending_procs = []
        for proc in (getattr(self, "_task_proc", None), self.gateway_proc, *pending_procs):
            try:
                alive = proc is not None and proc.poll() is None
            except Exception:
                alive = False
            if alive and all(existing is not proc for existing in processes):
                processes.append(proc)
        if not processes and monitor is None:
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
                if monitor is not None:
                    try:
                        monitor.stop()
                    except Exception:
                        pass
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
    if "--forge-plugin-worker" in sys.argv:
        from plugin_worker import worker_main
        index = sys.argv.index("--forge-plugin-worker")
        raise SystemExit(worker_main(sys.argv[index + 1:]))
    if "--diagnose" in sys.argv:
        raise SystemExit(_diagnose())
    _setup_dpi()
    root = tk.Tk()
    app = ForgeGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
