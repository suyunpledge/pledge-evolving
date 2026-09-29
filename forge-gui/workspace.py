"""forge 桌面端 · 右侧「工作区」面板（v2：三段堆叠布局）。

对照参考稿的版式（关键区别：三个区域**同时可见、上下堆叠**，不是标签页互斥切换）：

    ┌────────── 顶栏（图标 + 工作区 + Beta + 面包屑 + ✕）──────────┐
    │ 标签条: 文件树 | 变更(N) | <当前文件> | diff | 预览 | 终端      │
    ├────────────┬─────────────────────────┬────────┤
    │ 文件树列    │ Tab 条 / Home / 代码+minimap │ ← 上区 ~55%
    ├────────────┴─────────────────────────┴────────┤
    │ 变更(N) 文件列表+chips │ Diff 对比视图（并排）  │  ← 中区 ~25%
    ├──────────────────────────────────────────────┤
    │ 预览 | 控制台 | 终端 | 图像 | Markdown（子标签） │  ← 下区 ~20%
    └──────────────────────────────────────────────┘

v3 增量（本轮新增，不推翻既有版式）：
    - 代码列顶部多文件 Tab 条（每个 Tab 尾随 × 关闭按钮）
    - 无 activeFile 时显示 Workspace Home（仓库名 / 改动统计 / 最近改动文件）
    - 点文件树/变更列表 → 真正打开 Tab 并切换内容
    - 新接口 reveal_file(path, line=...) / workspace_summary()

对外 API（主程序按以下签名调用，名字必须一致）：
    - ``show()`` / ``hide()`` / ``toggle()`` / ``is_visible`` (property, bool)
    - ``open_file(path, *, tab=None)``：代码区载入该文件（tab 兼容旧调用，忽略内容）
    - ``show_diff(path=None)``：中区 diff 渲染该文件（None=全部）
    - ``open_changes()`` / ``open_file_tree()``：聚焦对应区域
    - ``refresh()``：重新扫描文件树 + git 状态
    - ``changes_count()``：当前变更文件数
    - ``push_terminal(text)``：往下区终端/控制台追加一行
    - ``set_repo_root(path)``：切换仓库根
    - ``reveal_file(path, *, line=None)``：主程序用于「聊天里点击文件引用」
    - ``workspace_summary()``：给主程序/Home 用，返回 dict

零第三方依赖（标准库 + tkinter + gui_theme）；所有 git/文件 IO 异常不外抛。
"""
from __future__ import annotations

import os
import math
import subprocess
import time
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import Any, Callable
from chat_widgets import ScrollArea

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import decor
from gui_theme import (  # noqa: E402
    C,
    FONT_MICRO,
    FONT_MONO_SM,
    FONT_MONO_XS,
    FONT_SMALL,
    FONT_TITLE,
    FONT_UI_BOLD,
    PAD_M,
    PAD_S,
    PAD_XS,
    attach_tooltip,
    divider,
    glyph_button,
    gutter_lines,
    highlight_python,
    human_size,
    pill_button,
    round_rect,
    setup_code_tags,
    style_scrollbar,
)

_MAX_FILE_BYTES = 400 * 1024
_MAX_FILE_LINES = 4000
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist",
              "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".openclaw"}
_SKIP_SUFFIXES = (".pyc", ".pyo")
_BAND_WEIGHTS = (11, 5, 4)          # 上 / 中 / 下 三区高度权重（≈55/25/20）
_TREE_WIDTH = 168                   # 文件树列宽
_CHANGES_WIDTH = 216                # 变更列表列宽
_MINIMAP_W = 64                     # minimap 宽
_HIGHLIGHT_TAG = "_active_line_hl"
_FILE_TAB_MAX_LEN = 18              # 文件 Tab 文字上限（截断用）


# ─── git / fs 工具 ─────────────────────────────────────────


def _run_git(repo: Path, *args: str, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _git_is_repo(repo: Path) -> bool:
    return bool(_run_git(repo, "rev-parse", "--show-toplevel", timeout=2.0).strip())


def _resolve_path(repo: Path | None, p: str | os.PathLike) -> Path | None:
    if p is None:
        return None
    try:
        path = Path(p)
    except (TypeError, ValueError):
        return None
    if not path.is_absolute() and repo is not None:
        path = (repo / path)
    try:
        return path.resolve()
    except OSError:
        return path


def _scan_tree(root: Path, *, max_depth: int = 8, expanded=None,
               max_nodes: int = 2000) -> list[dict[str, Any]]:
    """目录优先、名字次之的扁平节点列表。"""
    nodes: list[dict[str, Any]] = []

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            return
        # 目录优先 / 名字次之（is_dir 不带 follow_symlinks：3.12 及以下没有该参数）
        def _sort_key(entry: Path):
            try:
                return (not entry.is_dir(), entry.name.lower())
            except OSError:
                return (True, entry.name.lower())
        entries.sort(key=_sort_key)
        for entry in entries:
            if len(nodes) >= max_nodes:
                break
            try:
                name = entry.name
                is_dir = entry.is_dir()
                if is_dir:
                    if name in _SKIP_DIRS:
                        continue
                    nodes.append({"path": entry, "name": name,
                                  "is_dir": True, "depth": depth})
                    def _is_link(e: Path) -> bool:
                        try:
                            return e.is_symlink()
                        except OSError:
                            return True
                    if (not _is_link(entry) and
                            (expanded is None or str(entry) in expanded)):
                        _walk(entry, depth + 1)
                else:
                    if name.endswith(_SKIP_SUFFIXES):
                        continue
                    nodes.append({"path": entry, "name": name,
                                  "is_dir": False, "depth": depth})
            except OSError:
                continue

    nodes.append({"path": root, "name": root.name or str(root),
                  "is_dir": True, "depth": 0})
    _walk(root, 1)
    return nodes


def _git_status_map(repo: Path) -> dict[str, str]:
    out = _run_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    result: dict[str, str] = {}
    records = iter(out.split("\0"))
    for line in records:
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:]
        if "R" in code or "C" in code:
            next(records, None)  # -z reports destination first, then source.
        result[path.replace("\\", "/")] = code.strip() or "?"
    return result


def _count_lines(path: Path, limit: int = 20000) -> int:
    try:
        with open(path, "rb") as f:
            n = 0
            for _ in f:
                n += 1
                if n >= limit:
                    break
            return n
    except OSError:
        return 0


def _git_diff(repo: Path, *options: str, path=None) -> str:
    tail = ["--", str(path)] if path is not None else ["--"]
    base = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", *options]
    if _run_git(repo, "rev-parse", "--verify", "HEAD").strip():
        return _run_git(repo, *base, "HEAD", *tail)
    return (_run_git(repo, *base, "--cached", *tail)
            + _run_git(repo, *base, *tail))


def _git_diff_numstat(repo: Path) -> dict[str, tuple[int, int]]:
    out = _git_diff(repo, "--numstat", "-z")
    result: dict[str, tuple[int, int]] = {}
    for line in out.split("\0"):
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        a, d, path = parts[0], parts[1], parts[2]
        try:
            added = int(a) if a != "-" else 0
            removed = int(d) if d != "-" else 0
        except ValueError:
            added = removed = 0
        key = path.replace("\\", "/")
        previous = result.get(key, (0, 0))
        result[key] = (previous[0] + added, previous[1] + removed)
    # 未跟踪文件按「整文件新增」计
    for path, code in _git_status_map(repo).items():
        if code.startswith("?") and path not in result:
            full = repo / path
            if full.is_file():
                result[path] = (_count_lines(full), 0)
    return result


def _shorten_path(path: str, max_len: int = 26) -> str:
    """长路径中间截断：保头保尾，例如 a/very/long/path.py → a/…/path.py。"""
    if len(path) <= max_len:
        return path
    parts = path.split("/")
    if len(parts) >= 2:
        head, tail = parts[0], parts[-1]
        if len(head) + len(tail) + 4 <= max_len:
            return f"{head}/…/{tail}"
    return path[:max_len - 1] + "…"


def _strip_md(text: str) -> str:
    """极简 Markdown 去记号（预览用）。"""
    out = []
    for line in text.splitlines():
        s = line
        if s.lstrip().startswith("#"):
            s = s.lstrip("#").strip()
        s = s.replace("**", "").replace("`", "")
        if s.lstrip().startswith("> "):
            s = "│ " + s.lstrip()[2:]
        out.append(s)
    return "\n".join(out)


# ─── 内部小组件 ────────────────────────────────────────────


class _Tab(tk.Label):
    """标签条上的一枚标签（选中态：#1E1E2A 底 + #34344A 描边）。"""

    def __init__(self, parent, text: str, *, on_click: Callable[["_Tab"], None]):
        super().__init__(parent, text=text, bg=C["bg"], fg=C["ter"],
                         font=FONT_SMALL, padx=10, pady=5, cursor="hand2",
                         highlightthickness=1, highlightbackground=C["bg"])
        self._on_click = on_click
        self._selected = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _click(self, _e=None):
        self._on_click(self)

    def _on_enter(self, _e=None):
        if not self._selected:
            self.configure(bg=C["hover"], fg=C["text"])

    def _on_leave(self, _e=None):
        if not self._selected:
            self.configure(bg=C["bg"], fg=C["ter"])

    def set_selected(self, selected: bool):
        self._selected = selected
        self.configure(bg=C["sel"] if selected else C["bg"],
                       fg=C["text"] if selected else C["ter"],
                       highlightbackground=C["sel_border"] if selected else C["bg"])

    def set_text(self, text: str):
        self.configure(text=text)


class _FileTab(tk.Frame):
    """代码列顶部的多文件 Tab：文件名 + 关闭 ×；选中态高亮。"""

    def __init__(self, parent, *, name: str, on_select: Callable[["_FileTab"], None],
                 on_close: Callable[["_FileTab"], None], tooltip: str | None = None):
        super().__init__(parent, bg=C["bg"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self._on_select = on_select
        self._on_close = on_close
        self._selected = False
        self._base = C["bg"]
        self._name_lbl = tk.Label(self, text=name, bg=self._base, fg=C["ter"],
                                  font=FONT_SMALL, padx=8, pady=4)
        self._name_lbl.pack(side=tk.LEFT)
        self._close_btn = tk.Label(self, text="×", bg=self._base, fg=C["muted"],
                                   font=FONT_UI_BOLD, padx=4, pady=1,
                                   cursor="hand2")
        self._close_btn.pack(side=tk.LEFT)
        for w in (self, self._name_lbl):
            w.bind("<Button-1>", self._handle_select)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self._close_btn.bind("<Button-1>", self._handle_close)
        self._close_btn.bind("<Enter>", self._on_close_enter)
        self._close_btn.bind("<Leave>", self._on_close_leave)
        if tooltip:
            attach_tooltip(self, tooltip)

    def _handle_select(self, _e=None):
        self._on_select(self)

    def _handle_close(self, event=None):
        # 阻止冒泡触发 select
        if event is not None:
            try:
                return "break"
            except Exception:
                pass
        try:
            self._on_close(self)
        except Exception:
            pass

    def _paint(self, bg: str, fg: str):
        self.configure(bg=bg)
        self._name_lbl.configure(bg=bg, fg=fg)
        self._close_btn.configure(bg=bg)

    def _on_enter(self, _e=None):
        if not self._selected:
            self._paint(C["hover"], C["text"])

    def _on_leave(self, _e=None):
        if not self._selected:
            self._paint(self._base, C["ter"])

    def _on_close_enter(self, _e=None):
        self._close_btn.configure(fg=C["error"])
        self._paint(C["hover"], C["text"])

    def _on_close_leave(self, _e=None):
        self._close_btn.configure(fg=C["muted"])
        if not self._selected:
            self._paint(self._base, C["ter"])

    def set_selected(self, selected: bool):
        self._selected = selected
        if selected:
            self._paint(C["sel"], C["text"])
            self.configure(highlightthickness=1,
                           highlightbackground=C["sel_border"])
        else:
            self._paint(self._base, C["ter"])
            self.configure(highlightthickness=0)

    def set_name(self, name: str):
        self._name_lbl.configure(text=name)


class _FileRow(tk.Frame):
    """文件树 / 变更列表共用的一行：缩进 + 图标 + 名字 + 状态徽章 (+X −Y)。"""

    _STATUS_COLORS = {"M": C["git_m"], "A": C["git_a"], "?": C["git_a"],
                      "D": C["git_d"], "R": C["git_u"], "U": C["git_u"]}

    def __init__(self, parent, *, depth: int = 0, indent: int = 14,
                 icon: str, name: str, status: str = "",
                 added: int | None = None, removed: int | None = None,
                 expandable: bool = False, expanded: bool = False,
                 on_click: Callable | None = None, bg: str | None = None):
        base = bg or C["bg"]
        super().__init__(parent, bg=base, cursor="hand2",
                         highlightthickness=0)
        self._base = base
        self._on_click = on_click
        self._depth = depth
        inner = tk.Frame(self, bg=base)
        inner.pack(fill=tk.X, padx=(6 + depth * indent, 6), pady=1)

        arrow = "▾" if expanded else ("▸" if expandable else " ")
        self._arrow_lbl = tk.Label(inner, text=arrow, bg=base, fg=C["muted"],
                                   font=FONT_MICRO, width=2)
        self._arrow_lbl.pack(side=tk.LEFT)
        self._icon_lbl = tk.Label(inner, text=icon, bg=base, fg=C["subtext"],
                                  font=FONT_MICRO)
        self._icon_lbl.pack(side=tk.LEFT, padx=(0, 5))
        # pack 顺序：徽章/统计（RIGHT 侧）必须在 name 之前，
        # 否则会被 name 的 expand=True 挤掉。所有子 widget 都放 inner 里。
        if added is not None or removed is not None:
            stats = tk.Frame(inner, bg=base)
            stats.pack(side=tk.RIGHT, padx=(4, 0))
            tk.Label(stats, text=f"+{added or 0}", bg=base, fg=C["diff_add"],
                     font=FONT_MONO_XS).pack(side=tk.LEFT)
            tk.Label(stats, text=f"−{removed or 0}", bg=base, fg=C["diff_del"],
                     font=FONT_MONO_XS).pack(side=tk.LEFT, padx=(4, 0))
        if status:
            color = self._STATUS_COLORS.get(status[:1].upper(), C["muted"])
            self._status_lbl = tk.Label(inner, text=status[:1].upper(), bg=base,
                                        fg=color, font=FONT_MICRO, width=2)
            self._status_lbl.pack(side=tk.RIGHT, padx=(2, 0))
        else:
            self._status_lbl = None
        self._name_lbl = tk.Label(inner, text=name, bg=base, fg=C["body"],
                                  font=FONT_SMALL, anchor="w")
        self._name_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for w in (self, inner, self._arrow_lbl, self._icon_lbl, self._name_lbl):
            w.bind("<Button-1>", self._handle_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)

    def _handle_click(self, _e=None):
        if self._on_click is not None:
            self._on_click(self)

    def _paint(self, bg: str, fg: str):
        # 染色外层 + inner + inner 内所有子 widget（含 stats / status 徽章）
        self.configure(bg=bg)
        for child in self.winfo_children():
            try:
                child.configure(bg=bg)
                for sub in child.winfo_children():
                    try:
                        sub.configure(bg=bg)
                    except Exception:
                        pass
            except Exception:
                pass
        self._name_lbl.configure(fg=fg)

    def _on_enter(self, _e=None):
        self._paint(C["hover"], C["text"])

    def _on_leave(self, _e=None):
        self._paint(self._base, C["body"])

    def set_highlight(self, on: bool):
        if on:
            self._paint(C["sel"], C["text"])
            self.configure(highlightthickness=1,
                           highlightbackground=C["sel_border"])
        else:
            self._paint(self._base, C["body"])
            self.configure(highlightthickness=0)


class _Minimap(tk.Canvas):
    """代码缩略图：每行一根 1-2px 彩条 + 可视区域框；点击/拖动跳转。"""

    LINE_H = 2
    MAX_LINES = 400

    def __init__(self, parent, *, target: tk.Text, width: int = _MINIMAP_W):
        super().__init__(parent, width=width, bg=C["sidebar"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self._target = target
        self._mw = width
        self._dragging = False
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_press)
        self.bind("<Configure>", lambda _e: self.redraw())

    # -- 数据 --
    def redraw(self):
        self.delete("all")
        h = self.winfo_height()
        if h < 10:
            return
        try:
            content = self._target.get("1.0", "end-1c")
        except tk.TclError:
            return
        lines = content.splitlines()
        if not lines:
            return
        step = max(1, len(lines) // self.MAX_LINES)
        view = self._target.yview()
        scale = h / max(1, len(lines))
        y = 1
        for i in range(0, len(lines), step):
            color = self._line_color(lines[i])
            indent = len(lines[i]) - len(lines[i].lstrip())
            x1 = 6 + min(indent, 24)
            x2 = self._mw - 6
            if lines[i].strip():
                # 长度感：按内容长度收缩右端，更像 minimap
                frac = min(1.0, len(lines[i].strip()) / 90.0)
                x2 = max(x1 + 4, int(x1 + (self._mw - 12 - x1) * (0.35 + 0.65 * frac)))
                self.create_line(x1, y, x2, y, fill=color, width=self.LINE_H)
            y += self.LINE_H + 1
            if y > h - 4:
                break
        # 可视区域框
        top = max(0, int(view[0] * h))
        bottom = max(top + 8, int(view[1] * h))
        self.create_rectangle(2, top, self._mw - 2, bottom,
                              outline=C["sel_border"], fill="#FFFFFF",
                              stipple="gray12")

    @staticmethod
    def _line_color(line: str) -> str:
        s = line.strip()
        if s.startswith("#"):
            return C["code_comment"]
        if s.startswith(("def ", "class ", "async def")):
            return C["code_fn"]
        if s.startswith(("import ", "from ", "@")):
            return C["code_kw"]
        if '"' in s or "'" in s:
            return C["code_str"]
        return "#3A3A48"

    # -- 交互 --
    def _on_press(self, event):
        h = max(1, self.winfo_height())
        frac = min(1.0, max(0.0, (event.y - 4) / h))
        try:
            self._target.yview_moveto(frac)
            gutter = getattr(self._target, "_sync_gutter", None)
            if callable(gutter):
                gutter()
        except tk.TclError:
            pass
        self.redraw()


# ─── 主面板 ────────────────────────────────────────────────


class WorkspacePanel(tk.Frame):
    """右侧工作区（三段堆叠：代码区 / 变更+diff / 预览）。"""

    _TAB_NAMES = ("文件树", "变更", "代码", "diff", "预览", "终端")

    def __init__(self, parent, app: Any = None, *,
                 repo_root: str | os.PathLike | None = None,
                 on_close: Callable[[], None] | None = None, **kw: Any):
        super().__init__(parent, bg=C["bg"], highlightthickness=0, bd=0, **kw)
        self._app = app
        self._on_close = on_close

        default_root = _HERE.parent
        try:
            self._repo_root: Path = (Path(repo_root).resolve() if repo_root
                                     else default_root)
        except (OSError, ValueError):
            self._repo_root = default_root

        self._current_tab = "文件树"
        self._current_file: Path | None = None       # 兼容旧字段
        self._current_diff_file: Path | None = None
        self._active_file: Path | None = None        # 当前 Tab 文件
        # 多文件 Tab：path(key=str) → {"path":..., "tab": _FileTab, "line": int|None}
        self._open_tabs: dict[str, dict[str, Any]] = {}
        self._tab_order: list[str] = []              # Tab 显示顺序
        self._expanded_dirs: set[str] = {str(self._repo_root)}
        self._file_tree_rows: list[tuple[_FileRow, dict]] = []
        self._tabs: dict[str, _Tab] = {}
        self._breadcrumb_var = tk.StringVar(value=self._repo_root.name)
        self._preview_subtab_var = tk.StringVar(value="预览")
        self._filter_var = tk.StringVar(value="全部文件")
        self._is_git_repo = False
        self._git_status: dict[str, str] = {}
        self._git_numstat: dict[str, tuple[int, int]] = {}
        self._hidden = True
        self._terminal_buffer = ""
        self._tree_nav_visible = True
        self._changes_nav_visible = True
        self._tree_auto_collapsed = False
        self._changes_auto_collapsed = False
        self._sash_placed = False
        self._sash_retries = 0
        self._sash_after_ids: set[str] = set()
        self._transient_after_ids: set[str] = set()
        self._highlight_after_id: str | None = None

        self._build_topbar()
        self._build_tabbar()
        self._build_bands()
        # Diff 保持为中段主体，Changed Files 是按需展开的辅助导航。
        self._set_changes_nav_visible(False)

        self.pack_propagate(False)
        self.configure(width=640)
        self.pack_forget()

        try:
            self.refresh()
        except Exception:
            pass
        self.bind("<Configure>", self._on_workspace_configure)
        self._vp.bind("<Configure>", lambda _e: self._place_sashes_once())
        self.bind("<Destroy>", self._cancel_sash_retries, add="+")

    # ─── 顶栏 ──────────────────────────────────────────────

    def _build_topbar(self):
        top = tk.Frame(self, bg=C["bg"], height=36)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)
        left = tk.Frame(top, bg=C["bg"])
        left.pack(side=tk.LEFT, padx=(PAD_M, PAD_S))
        tk.Label(left, text="▤", bg=C["bg"], fg=C["accent2"],
                 font=FONT_TITLE).pack(side=tk.LEFT, padx=(0, PAD_XS))
        tk.Label(left, text="工作区", bg=C["bg"], fg=C["text"],
                 font=(FONT_TITLE[0], 12, "bold")).pack(side=tk.LEFT)
        crumb = tk.Frame(top, bg=C["bg"])
        crumb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=PAD_S)
        self._crumb_lbl = tk.Label(crumb, textvariable=self._breadcrumb_var,
                                   bg=C["bg"], fg=C["ter"], font=FONT_SMALL,
                                   anchor="w")
        self._crumb_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        close_btn = glyph_button(top, "✕", self._request_close, tooltip="收起工作区")
        close_btn.pack(side=tk.RIGHT, padx=PAD_S)
        self._changes_nav_btn = glyph_button(
            top, "⑂", self._toggle_changes_nav, size=11,
            tooltip="显示 / 隐藏变更列表")
        self._changes_nav_btn.pack(side=tk.RIGHT, padx=(0, 2))
        self._tree_nav_btn = glyph_button(
            top, "☷", self._toggle_tree_nav, size=11,
            tooltip="显示 / 隐藏文件树")
        self._tree_nav_btn.pack(side=tk.RIGHT, padx=(0, 2))
        divider(self).pack(side=tk.TOP, fill=tk.X)

    def _on_workspace_configure(self, _event=None):
        self._place_sashes_once()
        width = self.winfo_width()
        if width < 700:
            if self._tree_nav_visible:
                self._tree_auto_collapsed = True
                self._set_tree_nav_visible(False)
            if self._changes_nav_visible:
                self._changes_auto_collapsed = True
                self._set_changes_nav_visible(False)
        elif width >= 760:
            if self._tree_auto_collapsed:
                self._tree_auto_collapsed = False
                self._set_tree_nav_visible(True)
            if self._changes_auto_collapsed:
                self._changes_auto_collapsed = False
                self._set_changes_nav_visible(True)

    def _request_close(self):
        if self._on_close is not None:
            try:
                self._on_close()
                return
            except Exception:
                pass
        self.hide()

    # ─── 标签条 ────────────────────────────────────────────

    def _build_tabbar(self):
        bar = tk.Frame(self, bg=C["bg"], height=34)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)
        self._tabbar = bar

        def _click(t: _Tab):
            name = next((k for k, v in self._tabs.items() if v is t), None)
            if name:
                self._focus_band_for_tab(name)

        for name in self._TAB_NAMES:
            t = _Tab(bar, name, on_click=_click)
            t.pack(side=tk.LEFT, padx=(PAD_XS, 0), pady=3)
            self._tabs[name] = t
        self._tabs["文件树"].set_selected(True)
        self._current_tab = "文件树"
        divider(self).pack(side=tk.TOP, fill=tk.X)

    # ─── 三段主体 ──────────────────────────────────────────

    def _build_bands(self):
        self._vp = tk.PanedWindow(self, orient=tk.VERTICAL, bg=C["border"],
                                  sashwidth=5, sashrelief=tk.FLAT, bd=0,
                                  opaqueresize=True)
        self._vp.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self._band_top = tk.Frame(self._vp, bg=C["bg"],
                                  highlightthickness=1,
                                  highlightbackground=C["bg"])
        self._band_mid = tk.Frame(self._vp, bg=C["bg"],
                                  highlightthickness=1,
                                  highlightbackground=C["bg"])
        self._band_bottom = tk.Frame(self._vp, bg=C["bg"],
                                     highlightthickness=1,
                                     highlightbackground=C["bg"])
        self._vp.add(self._band_top, minsize=140, stretch="always")
        self._vp.add(self._band_mid, minsize=110, stretch="always")
        self._vp.add(self._band_bottom, minsize=90, stretch="always")

        self._build_top_band(self._band_top)
        self._build_mid_band(self._band_mid)
        self._build_bottom_band(self._band_bottom)

    def _place_sashes_once(self, _event=None):
        # 按 11:5:4 比例摆两根 sash。
        # 注意两个坑：(1) 首帧高度连续变化（1 → 中间值 → 最终值）；
        # (2) 在 Configure 事件流里 sash_place 可能被后续布局 pass 覆盖，
        #     需要延时“校验-重放”一到两拍才能落定。
        h = self._vp.winfo_height()
        if h < 200:
            retries = getattr(self, "_sash_retries", 0)
            if retries < 60:
                self._sash_retries = retries + 1
                self._schedule_sash_retry(80)
            return
        expected = (int(h * _BAND_WEIGHTS[0] / sum(_BAND_WEIGHTS)),
                    int(h * (_BAND_WEIGHTS[0] + _BAND_WEIGHTS[1]) / sum(_BAND_WEIGHTS)))
        try:
            current = (self._vp.sash_coord(0)[1], self._vp.sash_coord(1)[1])
        except tk.TclError:
            current = expected
        settled = (abs(current[0] - expected[0]) <= 4
                   and abs(current[1] - expected[1]) <= 4)
        settle_until = getattr(self, "_sash_settle_until", 0)
        if settled and getattr(self, "_ratio_applied_h", -1) == h:
            if time.monotonic() < settle_until:
                self._schedule_sash_retry(150)
            return
        self._ratio_applied_h = h
        try:
            self._vp.sash_place(0, 0, expected[0])
            self._vp.sash_place(1, 0, expected[1])
        except tk.TclError:
            pass
        self._sash_placed = True
        if time.monotonic() < settle_until:
            self._schedule_sash_retry(150)

    def _schedule_sash_retry(self, delay: int):
        """跟踪延时校验，避免 Workspace 销毁后 Tcl 继续调用旧命令。"""
        if not self.winfo_exists():
            return
        if self._sash_after_ids:
            return
        token = None

        def run():
            if token is not None:
                self._sash_after_ids.discard(token)
            if self.winfo_exists():
                self._place_sashes_once()

        token = self.after(delay, run)
        self._sash_after_ids.add(token)

    def _cancel_sash_retries(self, event=None):
        if event is not None and event.widget is not self:
            return
        for token in tuple(self._sash_after_ids):
            try:
                self.after_cancel(token)
            except tk.TclError:
                pass
        self._sash_after_ids.clear()
        for token in tuple(self._transient_after_ids):
            try:
                self.after_cancel(token)
            except tk.TclError:
                pass
        self._transient_after_ids.clear()

    # ── 上区：文件树 | 代码 | minimap ──
    def _build_top_band(self, band: tk.Frame):
        pane = tk.PanedWindow(band, orient=tk.HORIZONTAL, bg=C["border"],
                              sashwidth=5, sashrelief=tk.FLAT, bd=0,
                              opaqueresize=True)
        pane.pack(fill=tk.BOTH, expand=True)

        # 文件树列
        tree_col = tk.Frame(pane, bg=C["bg"])
        tree_head = tk.Frame(tree_col, bg=C["bg"], height=28)
        tree_head.pack(fill=tk.X)
        tree_head.pack_propagate(False)
        tk.Label(tree_head, text="文件树", bg=C["bg"], fg=C["ter"],
                 font=FONT_MICRO).pack(side=tk.LEFT, padx=PAD_S)
        glyph_button(tree_head, "⟳", self.refresh, size=10,
                     tooltip="重新扫描").pack(side=tk.RIGHT, padx=2)
        tree_host = tk.Frame(tree_col, bg=C["bg"])
        tree_host.pack(fill=tk.BOTH, expand=True)
        self._tree_canvas = tk.Canvas(tree_host, bg=C["bg"],
                                      highlightthickness=0, bd=0)
        tree_vbar = tk.Scrollbar(tree_host, orient=tk.VERTICAL,
                                 command=self._tree_canvas.yview,
                                 bg=C["surface2"], troughcolor=C["bg"],
                                 activebackground=C["scroll"], relief=tk.FLAT,
                                 bd=0, highlightthickness=0, width=8)
        self._tree_canvas.configure(yscrollcommand=tree_vbar.set)
        tree_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._tree_body = tk.Frame(self._tree_canvas, bg=C["bg"])
        self._tree_win = self._tree_canvas.create_window(
            0, 0, window=self._tree_body, anchor="nw")
        self._tree_body.bind(
            "<Configure>",
            lambda _e: self._tree_canvas.configure(
                scrollregion=self._tree_canvas.bbox("all")))
        self._tree_canvas.bind(
            "<Configure>",
            lambda e: self._tree_canvas.itemconfigure(self._tree_win,
                                                      width=e.width))
        self._tree_canvas.bind("<MouseWheel>", self._tree_wheel)
        self._tree_body.bind("<MouseWheel>", self._tree_wheel)
        pane.add(tree_col, width=_TREE_WIDTH, minsize=120, stretch="never")
        self._tree_col = tree_col

        # 代码区（meta 行 + Tab 条 + 行号槽 + 主 Text + minimap）
        code_col = tk.Frame(pane, bg=C["bg"])
        meta = tk.Frame(code_col, bg=C["bg"], height=28)
        meta.pack(fill=tk.X)
        meta.pack_propagate(False)
        self._code_meta_var = tk.StringVar(value="未打开文件")
        tk.Label(meta, textvariable=self._code_meta_var, bg=C["bg"],
                 fg=C["subtext"], font=FONT_MICRO, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=PAD_S)
        glyph_button(meta, "⟳", self._reload_code, size=10,
                     tooltip="重新载入当前文件").pack(side=tk.RIGHT, padx=2)

        # 文件 Tab 条（多文件 Tab）—— 默认隐藏，无 activeFile 时不占位
        self._file_tab_strip = tk.Frame(code_col, bg=C["bg"], height=28)
        # 不立刻 pack；在 _show_code() 里再 pack
        self._file_tab_strip.pack_propagate(False)
        self._file_tab_canvas = tk.Canvas(self._file_tab_strip, bg=C["bg"],
                                          highlightthickness=0, bd=0,
                                          height=28)
        self._file_tab_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._file_tab_inner = tk.Frame(self._file_tab_canvas, bg=C["bg"])
        self._file_tab_inner_id = self._file_tab_canvas.create_window(
            0, 0, window=self._file_tab_inner, anchor="nw")
        self._file_tab_inner.bind(
            "<Configure>",
            lambda _e: self._file_tab_canvas.configure(
                scrollregion=self._file_tab_canvas.bbox("all")))
        self._file_tab_canvas.bind(
            "<Configure>",
            lambda e: self._file_tab_canvas.itemconfigure(
                self._file_tab_inner_id,
                width=max(e.width, self._file_tab_inner.winfo_reqwidth())))
        self._file_tab_canvas.bind("<MouseWheel>",
                                   lambda e: self._file_tab_canvas.xview_scroll(
                                       -1 if e.delta > 0 else 1, "units"))
        self._file_tab_inner.bind(
            "<MouseWheel>",
            lambda e: self._file_tab_canvas.xview_scroll(
                -1 if e.delta > 0 else 1, "units"))

        # Home 视图（无 activeFile 时显示）—— 与代码主体互斥占位
        self._home_frame = tk.Frame(code_col, bg=C["bg"])
        self._build_home_view(self._home_frame)

        # 代码主体（行号槽 + Text + minimap）
        code_body = tk.Frame(code_col, bg=C["code_bg"])
        self._code_body = code_body

        self._gutter = tk.Text(code_body, width=4, bg=C["code_bg"],
                               fg=C["muted"], font=FONT_MONO_XS, padx=4,
                               pady=3, relief=tk.FLAT, highlightthickness=0,
                               bd=0, takefocus=0, wrap="none",
                               state=tk.DISABLED, cursor="arrow",
                               exportselection=False)

        self._code_text = tk.Text(code_body, bg=C["code_bg"],
                                  fg=C["code_plain"], font=FONT_MONO_SM,
                                  wrap="none", relief=tk.FLAT,
                                  highlightthickness=0, bd=0, takefocus=0,
                                  cursor="arrow", exportselection=False,
                                  padx=8, pady=3, spacing1=1, spacing3=1)
        code_vbar = tk.Scrollbar(code_body, orient=tk.VERTICAL,
                                 command=self._code_vscroll,
                                 bg=C["surface2"], troughcolor=C["code_bg"],
                                 activebackground=C["scroll"], relief=tk.FLAT,
                                 bd=0, highlightthickness=0, width=8)
        code_hbar = tk.Scrollbar(code_col, orient=tk.HORIZONTAL,
                                 command=self._code_text.xview,
                                 bg=C["surface2"], troughcolor=C["code_bg"],
                                 activebackground=C["scroll"], relief=tk.FLAT,
                                 bd=0, highlightthickness=0, width=8)
        self._code_text.configure(yscrollcommand=self._code_yscroll,
                                  xscrollcommand=code_hbar.set)
        self._code_vbar = code_vbar
        self._code_hbar = code_hbar
        setup_code_tags(self._code_text)
        # 临时高亮行用的 tag
        self._code_text.tag_configure(
            _HIGHLIGHT_TAG, background=C["sel"], foreground=C["text"])
        style_scrollbar(self._code_text)
        self._code_text._sync_gutter = self._sync_gutter  # minimap 拖动后回调

        # minimap 与滚动条先占位，code_text 最后吃剩余空间
        self._minimap = _Minimap(code_body, target=self._code_text)
        self._minimap.pack(side=tk.RIGHT, fill=tk.Y)
        code_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._gutter.pack(side=tk.LEFT, fill=tk.Y)
        self._code_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        code_hbar.pack(side=tk.BOTTOM, fill=tk.X)
        code_body.pack(fill=tk.BOTH, expand=True)

        for w in (self._code_text, self._gutter):
            w.bind("<MouseWheel>", self._code_wheel)
        self._gutter.bind("<Configure>", lambda _e: None)
        pane.add(code_col, minsize=260, stretch="always")
        self._code_col = code_col
        self._top_pane = pane

        # 默认初始：Home 可见，代码主体 / Tab 条隐藏
        self._show_home()

    def _set_tree_nav_visible(self, visible: bool):
        if visible == self._tree_nav_visible:
            return
        try:
            if visible:
                self._top_pane.add(self._tree_col, before=self._code_col,
                                   width=_TREE_WIDTH, minsize=120, stretch="never")
                self._tree_nav_btn.configure(fg=C["ter"])
            else:
                self._top_pane.forget(self._tree_col)
                self._tree_nav_btn.configure(fg=C["muted"])
            self._tree_nav_visible = visible
        except tk.TclError:
            pass

    def _toggle_tree_nav(self):
        self._tree_auto_collapsed = False
        self._set_tree_nav_visible(not self._tree_nav_visible)

    def _tree_wheel(self, event):
        self._tree_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _code_wheel(self, event):
        self._code_text.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self._sync_gutter()
        return "break"

    def _code_vscroll(self, *args):
        self._code_text.yview(*args)
        self._sync_gutter()

    def _code_yscroll(self, *args):
        try:
            self._code_vbar.set(*args)
        except tk.TclError:
            pass
        self._sync_gutter()

    def _sync_gutter(self):
        try:
            frac = self._code_text.yview()[0]
            self._gutter.configure(state=tk.NORMAL)
            self._gutter.yview_moveto(frac)
            self._gutter.configure(state=tk.DISABLED)
            self._minimap.redraw()
        except (tk.TclError, AttributeError):
            pass

    # ── 中区：变更列表 | diff ──
    def _build_mid_band(self, band: tk.Frame):
        pane = tk.PanedWindow(band, orient=tk.HORIZONTAL, bg=C["border"],
                              sashwidth=5, sashrelief=tk.FLAT, bd=0,
                              opaqueresize=True)
        pane.pack(fill=tk.BOTH, expand=True)

        # 左：变更列表
        left = tk.Frame(pane, bg=C["bg"])
        head = tk.Frame(left, bg=C["bg"], height=30)
        head.pack(fill=tk.X)
        head.pack_propagate(False)
        tk.Label(head, text="⑂", bg=C["bg"], fg=C["accent2"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(PAD_S, 4))
        self._changes_title_var = tk.StringVar(value="变更 (0)")
        tk.Label(head, textvariable=self._changes_title_var, bg=C["bg"],
                 fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        self._changes_total_var = tk.StringVar(value="+0 −0")
        self._total_lbl = tk.Label(head, textvariable=self._changes_total_var,
                                   bg=C["bg"], fg=C["subtext"],
                                   font=FONT_MONO_XS)
        self._total_lbl.pack(side=tk.LEFT, padx=(PAD_S, 0))

        chips = tk.Frame(left, bg=C["bg"])
        chips.pack(fill=tk.X, padx=PAD_S, pady=(2, 4))
        chips.grid_columnconfigure(0, weight=1)
        chips.grid_columnconfigure(1, weight=1)
        self._filter_chips: dict[str, tk.Label] = {}
        for i, name in enumerate(("全部文件", "已修改", "新增", "已删除")):
            c = self._make_filter_chip(chips, name)
            c.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, PAD_XS),
                   pady=1)
            self._filter_chips[name] = c

        list_host = tk.Frame(left, bg=C["bg"])
        list_host.pack(fill=tk.BOTH, expand=True)
        self._changes_canvas = tk.Canvas(list_host, bg=C["bg"],
                                         highlightthickness=0, bd=0)
        ch_vbar = tk.Scrollbar(list_host, orient=tk.VERTICAL,
                               command=self._changes_canvas.yview,
                               bg=C["surface2"], troughcolor=C["bg"],
                               activebackground=C["scroll"], relief=tk.FLAT,
                               bd=0, highlightthickness=0, width=8)
        self._changes_canvas.configure(yscrollcommand=ch_vbar.set)
        ch_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._changes_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._changes_body = tk.Frame(self._changes_canvas, bg=C["bg"])
        win = self._changes_canvas.create_window(
            0, 0, window=self._changes_body, anchor="nw")
        self._changes_body.bind(
            "<Configure>",
            lambda _e: self._changes_canvas.configure(
                scrollregion=self._changes_canvas.bbox("all")))
        self._changes_canvas.bind(
            "<Configure>",
            lambda e: self._changes_canvas.itemconfigure(win, width=e.width))
        self._changes_canvas.bind("<MouseWheel>", self._changes_wheel)
        self._changes_body.bind("<MouseWheel>", self._changes_wheel)
        pane.add(left, width=_CHANGES_WIDTH, minsize=150, stretch="never")
        self._changes_col = left

        # 右：diff 视图
        right = tk.Frame(pane, bg=C["bg"])
        dhead = tk.Frame(right, bg=C["bg"], height=30)
        dhead.pack(fill=tk.X)
        dhead.pack_propagate(False)
        self._diff_title_var = tk.StringVar(value="diff")
        tk.Label(dhead, textvariable=self._diff_title_var, bg=C["bg"],
                 fg=C["text"], font=FONT_UI_BOLD, anchor="w").pack(
            side=tk.LEFT, padx=PAD_S)
        self._diff_total_var = tk.StringVar(value="+0 −0")
        tk.Label(dhead, textvariable=self._diff_total_var, bg=C["bg"],
                 fg=C["subtext"], font=FONT_MONO_XS).pack(side=tk.LEFT,
                                                           padx=(PAD_S, 0))
        tk.Label(dhead, text="统一 Diff · 含暂存", bg=C["bg"], fg=C["ter"],
                 font=FONT_MICRO).pack(side=tk.RIGHT, padx=PAD_S)

        diff_body = tk.Frame(right, bg=C["code_bg"])
        diff_body.pack(fill=tk.BOTH, expand=True)
        self._diff_text = tk.Text(diff_body, bg=C["code_bg"],
                                  fg=C["code_plain"], font=FONT_MONO_SM,
                                  wrap="none", relief=tk.FLAT,
                                  highlightthickness=0, bd=0, takefocus=0,
                                  cursor="arrow", padx=6, pady=3,
                                  spacing1=1, spacing3=1)
        diff_vbar = tk.Scrollbar(diff_body, orient=tk.VERTICAL,
                                 command=self._diff_text.yview,
                                 bg=C["surface2"], troughcolor=C["code_bg"],
                                 activebackground=C["scroll"], relief=tk.FLAT,
                                 bd=0, highlightthickness=0, width=8)
        self._diff_text.configure(yscrollcommand=diff_vbar.set)
        diff_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._diff_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        setup_code_tags(self._diff_text)
        style_scrollbar(self._diff_text)
        self._diff_text.configure(state=tk.DISABLED)
        pane.add(right, minsize=240, stretch="always")
        self._diff_col = right
        self._mid_pane = pane

    def _set_changes_nav_visible(self, visible: bool):
        if visible == self._changes_nav_visible:
            return
        try:
            if visible:
                self._mid_pane.add(self._changes_col, before=self._diff_col,
                                   width=_CHANGES_WIDTH, minsize=150,
                                   stretch="never")
                self._changes_nav_btn.configure(fg=C["ter"])
            else:
                self._mid_pane.forget(self._changes_col)
                self._changes_nav_btn.configure(fg=C["muted"])
            self._changes_nav_visible = visible
        except tk.TclError:
            pass

    def _toggle_changes_nav(self):
        self._changes_auto_collapsed = False
        self._set_changes_nav_visible(not self._changes_nav_visible)

    def _changes_wheel(self, event):
        self._changes_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _make_filter_chip(self, parent, name: str):
        selected = name == self._filter_var.get()
        c = tk.Label(parent, text=name, padx=7, pady=2, font=FONT_MICRO,
                     cursor="hand2", highlightthickness=1,
                     bg=C["accent_soft"] if selected else C["surface2"],
                     fg=C["accent_text"] if selected else C["ter"],
                     highlightbackground=C["accent"] if selected
                     else C["border_hi"])
        c.bind("<Button-1>", lambda _e, n=name: self._set_filter(n))
        return c

    # ── 下区：预览 / 控制台 / 终端 / 图像 / Markdown ──
    def _build_bottom_band(self, band: tk.Frame):
        top = tk.Frame(band, bg=C["bg"], height=30)
        top.pack(fill=tk.X)
        top.pack_propagate(False)
        sub_holder = tk.Frame(top, bg=C["bg"])
        sub_holder.pack(side=tk.LEFT, padx=PAD_S)
        self._preview_subs: dict[str, tk.Label] = {}
        for name in ("预览", "控制台", "终端", "图像", "Markdown"):
            lbl = tk.Label(sub_holder, text=name, bg=C["bg"],
                           fg=C["text"] if name == "预览" else C["ter"],
                           font=FONT_SMALL, padx=8, pady=4, cursor="hand2")
            lbl.pack(side=tk.LEFT)
            lbl.bind("<Button-1>", lambda _e, n=name: self._set_preview_sub(n))
            self._preview_subs[name] = lbl
        self._sub_underline = tk.Frame(band, bg=C["accent"], height=2)

        actions = tk.Frame(top, bg=C["bg"])
        actions.pack(side=tk.RIGHT, padx=PAD_S)
        glyph_button(actions, "⟳", self._reload_preview, size=10,
                     tooltip="刷新预览").pack(side=tk.RIGHT, padx=(PAD_XS, 0))
        glyph_button(actions, "↗", self._open_current_external, size=10,
                     tooltip="在新窗口打开").pack(side=tk.RIGHT)

        self._preview_host = tk.Frame(band, bg=C["sidebar"])
        self._preview_host.pack(fill=tk.BOTH, expand=True, padx=PAD_S,
                                pady=(2, PAD_S))
        self._preview_text = tk.Text(self._preview_host, bg=C["code_bg"],
                                     fg=C["code_plain"], font=FONT_MONO_SM,
                                     wrap="word", relief=tk.FLAT,
                                     highlightthickness=0, bd=0, takefocus=0,
                                     cursor="arrow", padx=8, pady=6)
        prev_vbar = tk.Scrollbar(self._preview_host, orient=tk.VERTICAL,
                                 command=self._preview_text.yview,
                                 bg=C["surface2"], troughcolor=C["code_bg"],
                                 activebackground=C["scroll"], relief=tk.FLAT,
                                 bd=0, highlightthickness=0, width=8)
        self._preview_text.configure(yscrollcommand=prev_vbar.set)
        self._prev_vbar = prev_vbar
        prev_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        setup_code_tags(self._preview_text)
        style_scrollbar(self._preview_text)
        self._preview_text.configure(state=tk.DISABLED)
        self._preview_canvas: tk.Canvas | None = None
        self._show_preview_sub("预览")

    # ─── Home 视图（无 activeFile 时显示） ────────────────────

    def _build_home_view(self, parent: tk.Frame):
        """Workspace Home：大标题 + 统计 + 最近改动文件 + 入口按钮。"""
        outer = parent  # parent 即 code_col 内的 _home_frame
        outer.configure(bg=C["bg"])
        self._home_scroll = ScrollArea(outer, bg=C["bg"])
        self._home_scroll.pack(fill=tk.BOTH, expand=True)
        host = self._home_scroll.inner

        # 内部用 RoundedCard 风格的纯 Frame（沿用主题色）
        card = tk.Frame(host, bg=C["bg"], highlightthickness=0, padx=12, pady=12)
        card.pack(side=tk.TOP, fill=tk.X, padx=PAD_M, pady=(PAD_M, PAD_S))

        # 头部
        head = tk.Frame(card, bg=C["bg"])
        head.pack(fill=tk.X)
        # 仓库图标：柔光球代替原来的单色 ▤ 字符
        try:
            repo_icon = tk.Canvas(head, width=34, height=34, bg=C["bg"],
                                  highlightthickness=0, bd=0)
            decor.soft_orb(repo_icon, 17, 17, 15, bg=C["bg"],
                           fg=C["accent2"], layers=7, core=C["accent2"])
            decor.dot_grid(repo_icon, 4, 4, 30, 30, bg=C["bg"], fg=C["accent2"],
                           step=13, r=0.7)
            repo_icon.pack(side=tk.LEFT, padx=(0, 8))
        except tk.TclError:
            pass
        self._home_title_var = tk.StringVar(value=self._repo_root.name)
        tk.Label(head, textvariable=self._home_title_var, bg=C["bg"],
                 fg=C["text"], font=FONT_TITLE,
                 anchor="w").pack(side=tk.LEFT)

        # 仓库根路径（小字）
        self._home_repo_var = tk.StringVar(value=str(self._repo_root))
        tk.Label(card, textvariable=self._home_repo_var, bg=C["bg"],
                 fg=C["muted"], font=FONT_MICRO, anchor="w").pack(
            fill=tk.X, pady=(2, 10))

        # 统计行
        stats = tk.Frame(card, bg=C["bg"])
        stats.pack(fill=tk.X)
        self._home_summary_var = tk.StringVar(value="")
        tk.Label(stats, textvariable=self._home_summary_var, bg=C["bg"],
                 fg=C["body"], font=FONT_SMALL, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        # 最近改动文件列表
        tk.Label(card, text="最近修改文件", bg=C["bg"], fg=C["ter"],
                 font=FONT_MICRO, anchor="w").pack(
            fill=tk.X, pady=(12, 4))
        self._home_recent_body = tk.Frame(card, bg=C["bg"])
        self._home_recent_body.pack(fill=tk.X)

        # 按钮行
        btns = tk.Frame(card, bg=C["bg"])
        btns.pack(fill=tk.X, pady=(10, 8), before=self._home_recent_body)
        pill_button(btns, "查看全部变更", self.open_changes, kind="accent_soft",
                    font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, PAD_S))
        pill_button(btns, "打开文件…", self._home_pick_file, kind="ghost",
                    font=FONT_SMALL).pack(side=tk.LEFT)

        # 提示文案（非 git / 无改动时显示）
        self._home_empty_var = tk.StringVar(value="")
        self._home_empty_lbl = tk.Label(card, textvariable=self._home_empty_var,
                                        bg=C["bg"], fg=C["muted"],
                                        font=FONT_SMALL, anchor="w",
                                        justify="left", wraplength=420)
        self._home_empty_lbl.pack(fill=tk.X, pady=(10, 0))

    def _refresh_home(self):
        if not hasattr(self, "_home_summary_var"):
            return
        self._home_title_var.set(self._repo_root.name)
        self._home_repo_var.set(str(self._repo_root))
        summary = self.workspace_summary()
        if summary.get("is_git"):
            self._home_summary_var.set(
                f"{summary['changed']} files changed · "
                f"+{summary['added']} / −{summary['removed']}"
            )
            if summary["changed"] == 0:
                self._home_empty_var.set(
                    "工作区干净：没有未提交改动。\n"
                    "可以从左侧文件树打开任意文件，或点「打开文件…」选择。"
                )
            else:
                self._home_empty_var.set("")
        else:
            self._home_summary_var.set("（不是 git 仓库）")
            self._home_empty_var.set(
                "此目录未被 git 跟踪。仍然可以从左侧文件树浏览、打开文件，"
                "或点「打开文件…」选择。"
            )
        # 重渲染最近改动文件列表
        body = self._home_recent_body
        for child in list(body.winfo_children()):
            child.destroy()
        recent = (self.workspace_summary().get("recent") or [])[:5]
        if not recent:
            tk.Label(body, text="（无）", bg=C["bg"], fg=C["muted"],
                     font=FONT_SMALL, anchor="w").pack(fill=tk.X, pady=2)
        for rel in recent:
            self._make_home_file_row(body, rel)

    def _make_home_file_row(self, parent, rel: str):
        row = tk.Frame(parent, bg=C["bg"], cursor="hand2")
        row.pack(fill=tk.X, pady=1)
        tk.Label(row, text="•", bg=C["bg"], fg=C["accent2"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(row, text=rel, bg=C["bg"], fg=C["body"],
                 font=FONT_SMALL, anchor="w").pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        for w in (row, *row.winfo_children()):
            w.bind("<Button-1>", lambda _e, p=rel: self._open_path_or_warn(p))
            w.bind("<Enter>", lambda _e, r=row: self._recolor_home_row(r, True))
            w.bind("<Leave>", lambda _e, r=row: self._recolor_home_row(r, False))

    @staticmethod
    def _recolor_home_row(row: tk.Frame, hover: bool):
        bg = C["hover"] if hover else C["bg"]
        try:
            row.configure(bg=bg)
            for child in row.winfo_children():
                child.configure(bg=bg)
        except tk.TclError:
            pass

    def _home_pick_file(self):
        try:
            from tkinter import filedialog
            initial = str(self._repo_root) if self._repo_root.exists() else None
            picked = filedialog.askopenfilename(initialdir=initial,
                                                title="打开文件")
            if picked:
                self._open_path_or_warn(picked)
        except Exception as exc:
            self._set_status(f"打开文件对话框失败：{exc}", "warn")

    # ─── Tab / Home 切换 ──────────────────────────────────────

    def _show_home(self):
        """显示 Home，隐藏代码主体 / Tab 条。"""
        try:
            self._file_tab_strip.pack_forget()
        except (tk.TclError, AttributeError):
            pass
        # 代码主体隐藏（code_body 不直接 pack_forget，因为它本身没 pack，
        # 而是用 _gutter/_code_text/_minimap/_code_vbar/_code_hbar
        # 占位 code_col。这里我们让 code_body 仍然 pack 占住 code_col，
        # 但内部所有子件全部 pack_forget。)
        for w in (self._gutter, self._code_text, self._code_vbar,
                  self._code_hbar, self._minimap):
            try:
                w.pack_forget()
            except (tk.TclError, AttributeError):
                pass
        self._code_body.pack_forget()
        # Home 显示
        if not self._home_frame.winfo_ismapped():
            self._home_frame.pack(fill=tk.BOTH, expand=True)
        self._refresh_home()

    def _show_code(self):
        """隐藏 Home，显示代码主体 + Tab 条。"""
        try:
            self._home_frame.pack_forget()
            self._code_body.pack_forget()
            self._code_hbar.pack_forget()
        except (tk.TclError, AttributeError):
            pass
        # Tab 条：放在 meta 行下面、Home 占位之前
        try:
            if not self._file_tab_strip.winfo_ismapped():
                # code_col 内的当前 pack 顺序是 [meta, home]，
                # 目标是 [meta, file_tab_strip, code_body, ...]。Home 已 forget。
                # 直接 pack 到 code_col 上，逻辑顺序靠 pack 记录保持。
                self._file_tab_strip.pack(fill=tk.X)
        except (tk.TclError, AttributeError):
            pass
        # 代码主体重建 pack 顺序（与构建时一致）
        try:
            self._minimap.pack(side=tk.RIGHT, fill=tk.Y)
            self._code_vbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._gutter.pack(side=tk.LEFT, fill=tk.Y)
            self._code_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._code_hbar.pack(side=tk.BOTTOM, fill=tk.X)
            self._code_body.pack(fill=tk.BOTH, expand=True)
        except (tk.TclError, AttributeError):
            pass

    # ─── 可见性 / 对外 API ─────────────────────────────────

    @property
    def is_visible(self) -> bool:
        return not self._hidden

    def show(self):
        if self._hidden:
            self.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
            self._hidden = False
            self._sash_placed = False
            self._sash_retries = 0
            self._ratio_applied_h = -1
            # 首次显示后 2 秒内做“放置 + 校验”，抵抗 Tk 布局 pass 的覆盖
            self._sash_settle_until = time.monotonic() + 2.0
            self._schedule_sash_retry(40)

    def hide(self):
        if not self._hidden:
            self.pack_forget()
            self._hidden = True

    def toggle(self):
        self.hide() if self.is_visible else self.show()

    def set_repo_root(self, path):
        try:
            self._repo_root = Path(path).resolve()
        except (OSError, ValueError):
            return
        self._expanded_dirs = {str(self._repo_root)}
        self._current_file = None
        self._active_file = None
        self._current_diff_file = None
        # 关闭所有 Tab
        for key in list(self._open_tabs.keys()):
            self._close_tab(key)
        self._tab_order.clear()
        self.refresh()

    def changes_count(self) -> int:
        try:
            return len(self._git_status)
        except Exception:
            return 0

    def open_file_tree(self):
        self._tree_auto_collapsed = False
        self._set_tree_nav_visible(True)
        self._focus_band_for_tab("文件树")

    def open_changes(self):
        self._changes_auto_collapsed = False
        self._set_changes_nav_visible(True)
        self._focus_band_for_tab("变更")

    def open_file(self, path, *, tab: str | None = None):
        """公开 API：打开文件为 Tab（已有则激活），并加载代码/预览/diff。"""
        target = _resolve_path(self._repo_root, path)
        if target is None or not target.is_file():
            self._set_status(f"无法打开：{path}", "warn")
            return False
        try:
            key = str(target.resolve())
        except OSError:
            key = str(target)
        if key not in self._open_tabs:
            self._add_tab(target)
        self._activate_tab(key)
        # 加载代码区
        self._current_file = target
        self._active_file = target
        self._load_code_view(target)
        self._render_preview(target)
        # 中区 diff：优先显示该文件 diff
        self._current_diff_file = target
        self._render_diff(target)
        self._focus_band_for_tab(tab or "代码")
        # 高亮/minimap 需要一拍布局后再画（大文件 tag_add 0.7s，放 idle 里不卡打开）
        self.after_idle(lambda: (self._sync_gutter(),
                                 self._code_text.yview_moveto(0)))
        return True

    def show_diff(self, path=None):
        # Render diff only; never touch the open code view unless path 指向一个
        # 尚未打开的文件 → 这种情况下也开成 Tab 以便 Code/Diff 对应同一文件。
        if path is not None:
            resolved = _resolve_path(self._repo_root, path)
            if resolved is not None:
                self._current_diff_file = resolved
            else:
                self._set_status("invalid file path", "warn")
                return
            # 如果该文件不在 Tab 里且是文件，自动加一个（任务 A 要求 5）
            try:
                key = str(resolved.resolve())
            except OSError:
                key = str(resolved)
            if key not in self._open_tabs and resolved.is_file():
                self._add_tab(resolved)
                self._activate_tab(key)
                self._current_file = resolved
                self._active_file = resolved
                self._load_code_view(resolved)
                self._render_preview(resolved)
        else:
            self._current_diff_file = None
        self._render_diff(self._current_diff_file)
        self._focus_band_for_tab("diff")

    def reveal_file(self, path, *, line: int | None = None) -> None:
        """主程序用于「聊天里点击文件引用」：打开该文件为 Tab 并定位到 line（可选，1-based）。

        支持：相对路径（相对 repo_root）、绝对路径、带 `:120` 行号的字符串、
        不存在的路径（静默失败 + 可读提示，不抛异常）。
        """
        if path is None:
            self._set_status("reveal_file：缺少路径", "warn")
            return
        # 解析 "path:line" 形式
        text = str(path)
        parsed_line: int | None = line
        if parsed_line is None and (":" in text):
            head, _, tail = text.rpartition(":")
            # 仅当 tail 是纯数字且 head 像路径时使用，避免误伤 Windows 盘符 `C:\`
            if tail.isdigit() and head and not head[-1].isspace():
                parsed_line = int(tail)
                text = head
        target = _resolve_path(self._repo_root, text)
        if target is None:
            self._set_status(f"reveal_file：路径无效 {path}", "warn")
            return
        if not target.exists():
            self._set_status(f"文件不存在：{target}", "warn")
            return
        if not target.is_file():
            self._set_status(f"不是文件：{target}", "warn")
            return
        # 真正打开
        ok = self.open_file(target)
        if not ok:
            return
        # 定位到行
        if parsed_line is not None and parsed_line > 0:
            try:
                self._scroll_to_line(parsed_line)
            except (tk.TclError, AttributeError):
                pass

    def workspace_summary(self) -> dict:
        """给主程序/Home 用：{"repo", "changed", "added", "removed",
        "recent": [相对路径, ...], "is_git": bool}。所有 IO/git 异常都不外抛。"""
        try:
            changed = len(self._git_status)
            added = 0
            removed = 0
            for _k, (a, d) in self._git_numstat.items():
                added += a
                removed += d
            recent: list[str] = []
            for p in self._git_status.keys():
                recent.append(p)
            return {
                "repo": str(self._repo_root),
                "changed": changed,
                "added": added,
                "removed": removed,
                "recent": recent,
                "is_git": bool(self._is_git_repo),
            }
        except Exception:
            return {
                "repo": str(self._repo_root),
                "changed": 0,
                "added": 0,
                "removed": 0,
                "recent": [],
                "is_git": False,
            }

    def push_terminal(self, text: str):
        self._terminal_buffer = (self._terminal_buffer + text.rstrip("\n") + "\n")[-100000:]
        widget = getattr(self, "_term_text", None)
        if widget is None or not widget.winfo_exists():
            return
        try:
            widget.configure(state=tk.NORMAL, fg=C["body"])
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, self._terminal_buffer)
            widget.see(tk.END)
            widget.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def refresh(self):
        repo = self._repo_root
        self._is_git_repo = False
        try:
            self._is_git_repo = _git_is_repo(repo)
        except Exception:
            pass
        if self._is_git_repo:
            try:
                self._git_status = _git_status_map(repo)
            except Exception:
                self._git_status = {}
            try:
                self._git_numstat = _git_diff_numstat(repo)
            except Exception:
                self._git_numstat = {}
        else:
            self._git_status = {}
            self._git_numstat = {}
        self._refresh_file_tree()
        self._refresh_changes()
        self._update_tab_counts()
        self._update_tabs_file()
        self._update_breadcrumb()
        # refresh 只扫描文件树 + git；当前打开文件的重渲染由调用方按需触发。
        if self._active_file is not None:
            self._update_tabs_file()
        # 重新计算 Home（如果当前是 Home 状态）
        if self._active_file is None:
            self._refresh_home()

    # ─── 聚焦（标签条 → 区域高亮） ──────────────────────────

    def _focus_band_for_tab(self, name: str):
        if name == "文件树":
            self._set_tree_nav_visible(True)
        elif name == "变更":
            self._set_changes_nav_visible(True)
        self._current_tab = name
        for key, tab in self._tabs.items():
            tab.set_selected(key == name)
        band = {"文件树": self._band_top, "代码": self._band_top,
                "变更": self._band_mid, "diff": self._band_mid}.get(name)
        if name == "预览":
            self._set_preview_sub("预览")
            band = self._band_bottom
        elif name == "终端":
            self._set_preview_sub("终端")
            band = self._band_bottom
        if band is not None:
            self._flash_band(band)
        if name == "代码" and self._active_file is not None:
            self._update_breadcrumb(file=self._active_file)
        elif name == "diff":
            f = self._current_diff_file
            if f is not None:
                self._update_breadcrumb(file=f, diff=True)
            else:
                self._update_breadcrumb()
            self._render_diff(self._current_diff_file)
        elif name == "变更":
            self._update_breadcrumb()

    def _flash_band(self, band: tk.Frame):
        try:
            band.configure(highlightbackground=C["accent"])
            token = None

            def clear():
                if token is not None:
                    self._transient_after_ids.discard(token)
                if band.winfo_exists():
                    band.configure(highlightbackground=C["bg"])

            token = band.after(900, clear)
            self._transient_after_ids.add(token)
        except tk.TclError:
            pass

    def _update_tab_counts(self):
        tab = self._tabs.get("变更")
        if tab is not None:
            tab.set_text(f"变更 ({len(self._git_status)})")

    def _update_tabs_file(self):
        tab = self._tabs.get("代码")
        if tab is not None and self._active_file is not None:
            tab.set_text(self._active_file.name)

    # ─── 文件 Tab 多文件管理 ─────────────────────────────────

    @staticmethod
    def _tab_label(path: Path) -> str:
        name = path.name
        if len(name) <= _FILE_TAB_MAX_LEN:
            return name
        return name[: _FILE_TAB_MAX_LEN - 1] + "…"

    def _add_tab(self, path: Path) -> str:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in self._open_tabs:
            return key
        rel_label = self._rel_label(path)
        tab = _FileTab(
            self._file_tab_inner,
            name=self._tab_label(path),
            on_select=self._on_file_tab_selected,
            on_close=self._on_file_tab_closed,
            tooltip=rel_label,
        )
        tab.pack(side=tk.LEFT, padx=(2, 0), pady=3)
        self._open_tabs[key] = {"path": path, "tab": tab, "line": None}
        self._tab_order.append(key)
        return key

    def _on_file_tab_selected(self, tab_widget: _FileTab):
        # 找到对应 key
        for key, info in self._open_tabs.items():
            if info["tab"] is tab_widget:
                self._activate_tab(key)
                # 加载
                path = info["path"]
                self._current_file = path
                self._active_file = path
                self._load_code_view(path)
                self._render_preview(path)
                self._current_diff_file = path
                self._render_diff(path)
                self._focus_band_for_tab("代码")
                return

    def _on_file_tab_closed(self, tab_widget: _FileTab):
        for key, info in list(self._open_tabs.items()):
            if info["tab"] is tab_widget:
                # 当时是不是 active？记下来，关完再决定切到哪个
                was_active = (self._active_file is not None and
                              self._tab_key_of(self._active_file) == key)
                self._close_tab(key)
                if was_active:
                    self._activate_adjacent(key)
                return

    def _activate_tab(self, key: str):
        # 先把所有 Tab 取消高亮
        for k, info in self._open_tabs.items():
            info["tab"].set_selected(k == key)
        info = self._open_tabs.get(key)
        if info is None:
            return
        path = info["path"]
        self._active_file = path
        self._current_file = path
        self._update_tabs_file()
        self._update_breadcrumb(file=path)
        # 显示代码主体
        self._show_code()

    def _activate_adjacent(self, closed_key: str):
        """关闭 closed_key 后，把激活态切到相邻 Tab（没有则回 Home）。"""
        if closed_key in self._tab_order:
            idx = self._tab_order.index(closed_key)
            self._tab_order.pop(idx)
        else:
            idx = 0
        # 优先选右边；没有就左边
        new_key = None
        if self._tab_order:
            if idx < len(self._tab_order):
                new_key = self._tab_order[idx]
            else:
                new_key = self._tab_order[-1]
        if new_key is not None and new_key in self._open_tabs:
            self._activate_tab(new_key)
            path = self._open_tabs[new_key]["path"]
            self._current_file = path
            self._active_file = path
            self._load_code_view(path)
            self._render_preview(path)
            self._current_diff_file = path
            self._render_diff(path)
        else:
            # 全关了 → 回 Home
            self._active_file = None
            self._current_file = None
            self._current_diff_file = None
            self._show_home()
            self._update_tabs_file()
            self._update_breadcrumb()

    def _close_tab(self, key: str):
        info = self._open_tabs.pop(key, None)
        if info is None:
            return
        try:
            info["tab"].destroy()
        except tk.TclError:
            pass
        if key in self._tab_order:
            self._tab_order.remove(key)

    def _tab_key_of(self, path: Path) -> str | None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        return key if key in self._open_tabs else None

    def _open_path_or_warn(self, path_like):
        """Home 入口用：接受相对或绝对路径。"""
        self.reveal_file(path_like, line=None)

    def _scroll_to_line(self, line: int):
        """滚动到指定行（1-based），并临时高亮该行。"""
        # 取消上一处高亮
        if self._highlight_after_id is not None:
            try:
                self.after_cancel(self._highlight_after_id)
            except Exception:
                pass
            self._highlight_after_id = None
        try:
            self._code_text.tag_remove(_HIGHLIGHT_TAG, "1.0", "end")
        except tk.TclError:
            pass
        if line <= 0:
            return
        idx = f"{line}.0"
        try:
            self._code_text.see(f"{max(1, line - 3)}.0")
            self._code_text.see(idx)
            end = f"{line}.end"
            self._code_text.tag_add(_HIGHLIGHT_TAG, idx, end)
            # 2.5s 后清掉
            self._highlight_after_id = self.after(2500, self._clear_highlight)
        except tk.TclError:
            pass

    def _clear_highlight(self):
        try:
            self._code_text.tag_remove(_HIGHLIGHT_TAG, "1.0", "end")
        except tk.TclError:
            pass
        self._highlight_after_id = None

    # ─── 文件树 ────────────────────────────────────────────

    def _refresh_file_tree(self):
        body = self._tree_body
        for child in list(body.winfo_children()):
            child.destroy()
        self._file_tree_rows.clear()
        try:
            nodes = _scan_tree(self._repo_root, expanded=self._expanded_dirs)
        except Exception as exc:
            tk.Label(body, text=f"扫描失败：{exc}", bg=C["bg"],
                     fg=C["muted"], font=FONT_SMALL).pack(anchor="w",
                                                          padx=PAD_S,
                                                          pady=PAD_S)
            return
        repo = self._repo_root
        for node in nodes[1:]:
            depth = node["depth"] - 1
            path: Path = node["path"]
            name = node["name"]
            is_dir = node["is_dir"]
            status = ""
            if not is_dir:
                try:
                    rel = str(path.resolve().relative_to(repo.resolve())) \
                        .replace("\\", "/")
                    status = self._git_status.get(rel, "")
                except (ValueError, OSError):
                    status = ""
            icon = ("📁" if is_dir else "🐍" if name.endswith(".py")
                    else "📄")
            row = _FileRow(
                body, depth=max(0, depth), icon=icon, name=name,
                status=status, expandable=is_dir,
                expanded=str(path) in self._expanded_dirs,
                on_click=(lambda _r=None, p=path, d=is_dir:
                          self._toggle_dir(p) if d else self.open_file(p)),
            )
            row.pack(fill=tk.X)
            self._file_tree_rows.append((row, node))
        if not self._file_tree_rows:
            tk.Label(body, text="（空目录）", bg=C["bg"], fg=C["muted"],
                     font=FONT_SMALL).pack(anchor="w", padx=PAD_S, pady=PAD_S)

    def _toggle_dir(self, path: Path):
        key = str(path)
        if key in self._expanded_dirs:
            self._expanded_dirs.discard(key)
        else:
            self._expanded_dirs.add(key)
        try:
            self._refresh_file_tree()
        except Exception:
            pass

    # ─── 变更列表 ──────────────────────────────────────────

    def _refresh_changes(self):
        body = self._changes_body
        for child in list(body.winfo_children()):
            child.destroy()
        if not self._is_git_repo:
            tk.Label(body, text="不是 git 仓库", bg=C["bg"], fg=C["muted"],
                     font=FONT_SMALL).pack(anchor="w", padx=PAD_S, pady=PAD_S)
            self._changes_title_var.set("变更 (0)")
            self._changes_total_var.set("+0 −0")
            return
        cur = self._filter_var.get()
        rows: list[tuple[str, str, int, int]] = []
        for path, code in self._git_status.items():
            primary = (code or "?")[:1].upper()
            if cur == "已修改" and primary != "M":
                continue
            if cur == "新增" and primary not in ("A", "?"):
                continue
            if cur == "已删除" and primary != "D":
                continue
            added, removed = self._git_numstat.get(path, (0, 0))
            rows.append((primary, path, added, removed))
        self._changes_title_var.set(f"变更 ({len(rows)})")
        total_add = sum(r[2] for r in rows)
        total_del = sum(r[3] for r in rows)
        self._changes_total_var.set(f"+{total_add} −{total_del}")
        if not rows:
            tk.Label(body, text="无变更", bg=C["bg"], fg=C["muted"],
                     font=FONT_SMALL).pack(anchor="w", padx=PAD_S, pady=PAD_S)
            return
        for code, path, added, removed in sorted(rows, key=lambda r: r[1]):
            display = Path(path).name
            row = _FileRow(body, depth=0, icon="📄", name=display, status=code,
                           added=added, removed=removed,
                           on_click=lambda _r=None, p=path: self.show_diff(p))
            attach_tooltip(row, path)
            row.pack(fill=tk.X)

    def _set_filter(self, name: str):
        self._filter_var.set(name)
        for n, c in self._filter_chips.items():
            selected = n == name
            c.configure(bg=C["accent_soft"] if selected else C["surface2"],
                        fg=C["accent_text"] if selected else C["ter"],
                        highlightbackground=C["accent"] if selected
                        else C["border_hi"])
        self._refresh_changes()

    # ─── 代码 ──────────────────────────────────────────────

    def _reload_code(self):
        if self._active_file is not None:
            self._load_code_view(self._active_file)

    def _load_code_view(self, path: Path):
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._code_meta_var.set(f"无法读取：{exc}")
            self._set_code_text(f"无法读取文件：{exc}")
            return
        size = len(data)
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                text = data.decode("gbk")
                encoding = "gbk"
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
                encoding = "utf-8(replace)"
        lines = text.splitlines()
        truncated = False
        if size > _MAX_FILE_BYTES or len(lines) > _MAX_FILE_LINES:
            truncated = True
            lines = lines[:_MAX_FILE_LINES]
        note = "（已截断）" if truncated else ""
        self._code_meta_var.set(
            f"{self._rel_label(path)} · {human_size(size)} · {encoding}{note}")
        self._set_code_text("\n".join(lines), truncate_hint=truncated)
        if path.suffix.lower() == ".py":
            try:
                highlight_python(self._code_text)
            except Exception:
                pass
        # 重置高亮 tag
        try:
            self._code_text.tag_remove(_HIGHLIGHT_TAG, "1.0", "end")
        except tk.TclError:
            pass
        self._update_gutter()
        self._update_tabs_file()
        self._update_breadcrumb(file=path)

    def _set_code_text(self, content: str, *, truncate_hint: bool = False):
        text = self._code_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert("1.0", content)
        if truncate_hint:
            text.insert(tk.END, f"\n\n…（文件过大，仅显示前 {_MAX_FILE_LINES} 行）")
        text.configure(state=tk.DISABLED)

    def _update_gutter(self):
        try:
            n = int(self._code_text.index("end-1c").split(".")[0])
            width = max(3, len(str(n)) + 1)
        except tk.TclError:
            width = 4
        self._gutter.configure(width=width, state=tk.NORMAL)
        self._gutter.delete("1.0", tk.END)
        self._gutter.insert("1.0", gutter_lines(self._code_text,
                                                gutter_width=width - 1))
        self._gutter.configure(state=tk.DISABLED)
        self._sync_gutter()

    def _rel_label(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._repo_root.resolve())) \
                .replace("\\", "/")
        except (ValueError, OSError):
            return path.name

    def _update_breadcrumb(self, *, file: Path | None = None,
                           diff: bool = False):
        parts = [self._repo_root.name]
        if file is not None:
            try:
                rel = file.resolve().relative_to(self._repo_root.resolve())
                parts.extend(rel.parts)
            except (ValueError, OSError):
                parts.append(file.name)
        if diff:
            parts.append("diff")
        self._breadcrumb_var.set(" › ".join(parts))

    # ─── diff ──────────────────────────────────────────────

    def _render_diff(self, path: Path | None):
        text = self._diff_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        if not self._is_git_repo:
            text.insert("1.0", "（不是 git 仓库）")
            text.configure(state=tk.DISABLED)
            self._diff_title_var.set("diff")
            return
        if path is not None:
            self._diff_title_var.set(self._rel_label(path))
            rel = self._rel_label(path)
            out = _git_diff(self._repo_root, path=path)
            if not out:
                # 未跟踪的新文件：整文件按新增渲染
                try:
                    code = self._git_status.get(rel, "")
                    if not code.startswith("?"):
                        raw = ""
                    else:
                        with path.open("rb") as stream:
                            raw = stream.read(_MAX_FILE_BYTES).decode("utf-8", errors="replace")
                except OSError:
                    raw = ""
                if raw:
                    text.insert(tk.END,
                                f"新增文件（未跟踪）：{rel}\n",
                                ("meta",))
                    for line in raw.splitlines():
                        text.insert(tk.END, line + "\n", ("add",))
                    text.configure(state=tk.DISABLED)
                    self._diff_total_var.set(f"+{len(raw.splitlines())} −0")
                    return
                # 有改动文件但 diff 为空（HEAD 同步但工作区无变）→ 给可读提示
                text.insert("1.0", f"{rel}\n无未提交改动。\n")
                text.configure(state=tk.DISABLED)
                self._diff_total_var.set("+0 −0")
                return
            first_hunk = None
            idx = 0
            for line in out[:_MAX_FILE_BYTES].splitlines()[:_MAX_FILE_LINES]:
                idx += 1
                if line.startswith(("+++", "---", "diff --git", "index ")):
                    tag = "meta"
                elif line.startswith("@@"):
                    tag = "hunk"
                    if first_hunk is None:
                        first_hunk = max(1, idx - 3)
                elif line.startswith("+"):
                    tag = "add"
                elif line.startswith("-"):
                    tag = "del"
                else:
                    tag = ""
                text.insert(tk.END, line + "\n", (tag,) if tag else ())
            text.configure(state=tk.DISABLED)
            if first_hunk is not None:
                try:
                    text.see(f"{first_hunk}.0")
                except tk.TclError:
                    pass
            ns = self._git_numstat.get(rel)
            if ns is None and rel in self._git_status and \
                    self._git_status[rel].startswith("?"):
                ns = (_count_lines(path), 0)
            ns = ns or (0, 0)
            self._diff_total_var.set(f"+{ns[0]} −{ns[1]}")
            return
        else:
            self._diff_title_var.set("diff（全部）")
            out = _git_diff(self._repo_root)
            if not out:
                text.insert("1.0",
                            "工作区没有已跟踪文件的改动\n"
                            "（未跟踪的新文件请从左侧列表点开）\n")
                text.configure(state=tk.DISABLED)
                self._refresh_diff_total()
                return
        first_hunk = None
        idx = 0
        for line in out[:_MAX_FILE_BYTES].splitlines()[:_MAX_FILE_LINES]:
            idx += 1
            if line.startswith(("+++", "---", "diff --git", "index ")):
                tag = "meta"
            elif line.startswith("@@"):
                tag = "hunk"
                if first_hunk is None:
                    first_hunk = max(1, idx - 3)
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            else:
                tag = ""
            text.insert(tk.END, line + "\n", (tag,) if tag else ())
        text.configure(state=tk.DISABLED)
        if first_hunk is not None:
            try:
                text.see(f"{first_hunk}.0")
            except tk.TclError:
                pass
        self._refresh_diff_total()

    def _refresh_diff_total(self):
        total_add = sum(v[0] for v in self._git_numstat.values())
        total_del = sum(v[1] for v in self._git_numstat.values())
        self._diff_total_var.set(f"+{total_add} −{total_del}")

    # ─── 预览 ──────────────────────────────────────────────

    def _set_preview_sub(self, name: str):
        self._preview_subtab_var.set(name)
        for n, lbl in self._preview_subs.items():
            sel = n == name
            lbl.configure(fg=C["text"] if sel else C["ter"])
        self._show_preview_sub(name)
        if name in ("预览", "终端"):
            self._current_tab = name
            for key, tab in self._tabs.items():
                tab.set_selected(key == name)

    def _show_preview_sub(self, name: str):
        """终端/控制台切到终端 Text；其余切到预览内容宿主。"""
        if name in ("终端", "控制台"):
            if getattr(self, "_term_text", None) is None:
                self._term_text = tk.Text(self._preview_host, bg=C["code_bg"],
                                          fg=C["muted"], font=FONT_MONO_SM,
                                          wrap="none", relief=tk.FLAT,
                                          highlightthickness=0, bd=0,
                                          takefocus=0, cursor="arrow",
                                          padx=8, pady=6)
                self._term_vbar = tk.Scrollbar(
                    self._preview_host, orient=tk.VERTICAL,
                    command=self._term_text.yview,
                    bg=C["surface2"], troughcolor=C["code_bg"],
                    activebackground=C["scroll"], relief=tk.FLAT,
                    bd=0, highlightthickness=0, width=8)
                self._term_text.configure(yscrollcommand=self._term_vbar.set)
                self._term_text.insert("1.0", self._terminal_buffer or
                                       "任务 / gateway 输出日志（只读）\n")
                self._term_text.configure(state=tk.DISABLED)
            self._preview_text.pack_forget()
            self._prev_vbar.pack_forget()
            if self._preview_canvas is not None:
                self._preview_canvas.pack_forget()
            self._term_vbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._term_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self._update_preview_underline()
            return
        if getattr(self, "_term_text", None) is not None:
            self._term_text.pack_forget()
            self._term_vbar.pack_forget()
        if name == "图像":
            self._preview_text.pack_forget()
            self._prev_vbar.pack_forget()
            self._show_image_preview()
        else:
            if self._preview_canvas is not None:
                self._preview_canvas.pack_forget()
                self._preview_canvas = None
            self._prev_vbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            if self._active_file is not None:
                self._render_preview(self._active_file)
            else:
                self._preview_write("打开一个文件后，这里会显示预览。\n\n"
                                    "支持：Markdown / HTML 源码 / 图片 / 文本。")
        self._update_preview_underline()

    def _update_preview_underline(self):
        try:
            self._sub_underline.place_forget()
            name = self._preview_subtab_var.get()
            lbl = self._preview_subs.get(name)
            if lbl is None:
                return
            lbl.update_idletasks()
            x = lbl.winfo_rootx() - self.winfo_rootx()
            w = lbl.winfo_width()
            y = lbl.winfo_rooty() - self.winfo_rooty() + lbl.winfo_height()
            self._sub_underline.place(in_=self, x=x, y=y - 2,
                                      width=max(8, w), height=2)
            self._sub_underline.lift()
        except tk.TclError:
            pass

    def _preview_write(self, text: str):
        if self._preview_canvas is not None:
            self._preview_canvas.pack_forget()
        self._prev_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._preview_text.configure(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)
        self._preview_text.insert("1.0", text)
        self._preview_text.configure(state=tk.DISABLED)

    def _reload_preview(self):
        if self._active_file is not None:
            self._render_preview(self._active_file)
        self._set_status("预览已刷新", "info")

    def _open_current_external(self):
        path = self._active_file
        if path is None:
            self._set_status("没有可打开的文件", "warn")
            return
        # A code preview must not execute a .py/.bat/.ps1 file via file association.
        if os.name == "nt" and path.suffix.lower() not in {
                ".html", ".htm", ".svg", ".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp", ".pdf"}:
            try:
                subprocess.Popen(["notepad.exe", str(path)])
            except OSError as exc:
                self._set_status(f"无法打开文本预览：{exc}", "error")
            return
        opener = getattr(self._app, "_open_path", None)
        if callable(opener):
            try:
                opener(path)
                return
            except Exception:
                pass
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:
            try:
                os.startfile(str(path))  # type: ignore[attr-defined]
            except Exception as exc:
                self._set_status(f"无法打开：{exc}", "error")

    def _render_preview(self, path: Path):
        sub = self._preview_subtab_var.get()
        if sub in ("终端", "控制台"):
            return
        suffix = path.suffix.lower()
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        if suffix in image_exts and sub in ("预览", "图像"):
            self._show_image_preview(path)
            return
        # 文本类
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                self._preview_write("文件超过 400 KiB，内嵌预览已跳过；可在新窗口打开。")
                return
            with path.open("rb") as stream:
                data = stream.read(_MAX_FILE_BYTES)
        except OSError as exc:
            self._preview_write(f"无法读取：{exc}")
            return
        if suffix in {".html", ".htm", ".svg"}:
            txt = data.decode("utf-8", errors="replace")
            self._preview_write(txt + "\n\n（HTML/SVG 源码 · 可点「在新窗口打开」）")
            return
        if suffix == ".md" and sub in ("预览", "Markdown"):
            self._preview_write(_strip_md(data.decode("utf-8", errors="replace")))
            return
        if suffix == ".py":
            self._preview_write(data.decode("utf-8", errors="replace"))
            return
        try:
            txt = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                txt = data.decode("gbk")
            except UnicodeDecodeError:
                self._preview_write(
                    f"二进制文件 · {human_size(len(data))} · 可在新窗口打开")
                return
        self._preview_write(txt)

    def _show_image_preview(self, path: Path | None = None) -> bool:
        target = path or self._active_file
        if target is None:
            return False
        try:
            if target.stat().st_size > 20 * 1024 * 1024:
                self._preview_write("图片超过 20 MiB，请在新窗口打开。")
                return False
            img = tk.PhotoImage(file=str(target))
        except (tk.TclError, OSError):
            if path is None:
                self._preview_write("当前文件不是可显示的图片。\n"
                                    "（PhotoImage 支持 PNG/GIF/PPM；"
                                    "JPG/WebP 请点「在新窗口打开」）")
            else:
                self._preview_write("该格式无法内嵌显示，请点「在新窗口打开」。")
            return path is None
        w, h = img.width(), img.height()
        max_w, max_h = 620, 320
        factor = max(1, math.ceil(max(w / max_w, h / max_h)))
        try:
            if factor > 1:
                img = img.subsample(factor)
        except tk.TclError:
            pass
        self._preview_text.pack_forget()
        self._prev_vbar.pack_forget()
        if self._preview_canvas is None:
            self._preview_canvas = tk.Canvas(self._preview_host,
                                             bg=C["sidebar"],
                                             highlightthickness=0, bd=0)
        self._preview_canvas.delete("all")
        self._preview_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._preview_canvas.create_image(10, 10, image=img, anchor="nw")
        self._preview_canvas.image = img  # 防 GC
        return True

    # ─── 状态 ──────────────────────────────────────────────

    def _set_status(self, msg: str, level: str = "info"):
        setter = getattr(self._app, "_set_status", None) if self._app else None
        if callable(setter):
            try:
                setter(msg, level)
            except Exception:
                pass


# ─── 入口（无头冒烟） ──────────────────────────────────────


def _selftest(repo: str | None = None):
    root = tk.Tk()
    root.withdraw()
    try:
        rr = repo or str(Path(__file__).resolve().parent.parent)
        panel = WorkspacePanel(root, repo_root=rr)
        panel.show()
        for fn in (panel.open_file_tree, panel.open_changes, panel.refresh,
                   lambda: panel.show_diff(), panel.hide):
            fn()
            root.update()
        sample = None
        repo_path = Path(rr)
        if repo_path.exists():
            for p in repo_path.rglob("*.py"):
                if any(part in _SKIP_DIRS for part in p.parts):
                    continue
                if p.name == "__init__.py":
                    continue
                sample = p
                break
        if sample is not None:
            panel.open_file(sample)
            root.update()
        panel.push_terminal("test")
        panel._set_preview_sub("终端")
        panel._set_preview_sub("图像")
        panel._set_preview_sub("预览")
        root.update()
        print("selftest ok; changes:", panel.changes_count())
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _selftest_non_git():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ws_nongit_")
    root = tk.Tk()
    root.withdraw()
    try:
        panel = WorkspacePanel(root, repo_root=tmp)
        panel.show()
        for fn in (panel.open_file_tree, panel.open_changes, panel.refresh,
                   panel.hide):
            fn()
            root.update()
        print("non-git ok")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--selftest-non-git" in sys.argv:
        _selftest_non_git()
    else:
        r = tk.Tk()
        r.title("WorkspacePanel 预览")
        r.configure(bg=C["bg"])
        r.geometry("680x860+50+50")
        ws = WorkspacePanel(r, repo_root=str(Path(__file__).resolve().parent.parent))
        ws.show()
        r.mainloop()
