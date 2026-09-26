"""forge 桌面端 · 右侧「工作区」面板。

职责：
    - 仓库浏览（文件树 + git 状态徽章）
    - 变更总览（chip 筛选 + +X/-Y 统计）
    - 代码查看（行号槽 + Python 高亮 + 滚动同步）
    - diff 视图（git diff 行级着色）
    - 预览（图片缩略图 / Markdown 轻量渲染 / 文本 / HTML 源码）
    - 终端（gateway 输出回显）

设计口径：
    - 完全复用 ``gui_theme`` 的配色 ``C``、字体 ``FONT_*``、圆角 ``R_*`` 与绘制
      原语（``RoundedCard``、``pill_button``、``chip``、``badge``、``glyph_button``、
      ``setup_code_tags``、``highlight_python``、``gutter_lines``、``human_size``、
      ``style_scrollbar``），不另造一套 token。
    - 零第三方依赖（标准库 + tkinter + gui_theme）。
    - 所有 IO（git / 文件系统）必须 try/except，绝不让异常冒到 UI。
    - 导入期不执行 git/文件扫描，只在 ``__init__`` / ``refresh`` 时按需触发。

对外 API（主程序按以下签名调用，名字必须一致）：
    - ``show()`` / ``hide()`` / ``toggle()`` / ``is_visible`` (property, bool)
    - ``open_file(path, *, tab=None)``：切到代码标签并加载文件
    - ``show_diff(path=None)``：切到 diff 标签；给 path 就渲染该文件的 diff
    - ``open_changes()``：切到「变更」标签
    - ``open_file_tree()``：切到「文件树」标签
    - ``refresh()``：重新扫描文件树 + git 状态
    - ``push_terminal(text)``：往终端追加一行
    - ``set_repo_root(path)``：切换仓库根
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import scrolledtext
from typing import Any, Callable, Iterable

# 复用设计系统（绝对 import，避开相对路径陷阱）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from gui_theme import (  # noqa: E402
    C,
    FONT_GLYPH,
    FONT_MICRO,
    FONT_MONO,
    FONT_MONO_SM,
    FONT_MONO_XS,
    FONT_SMALL,
    FONT_TITLE,
    FONT_UI,
    FONT_UI_BOLD,
    PAD_L,
    PAD_M,
    PAD_S,
    PAD_XS,
    R_SM,
    attach_tooltip,
    badge,
    chip,
    divider,
    glyph_button,
    gutter_lines,
    highlight_python,
    human_size,
    pill_button,
    setup_code_tags,
    style_scrollbar,
)

# 截断阈值（超大文件不全读）
_MAX_FILE_BYTES = 400 * 1024
_MAX_FILE_LINES = 4000
# 不扫描的目录（与 ``git status`` 行为对齐）
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "dist", "build"}
_SKIP_SUFFIXES = (".pyc", ".pyo")


# ─── 工具 ───────────────────────────────────────────────────


def _bg_of(widget) -> str:
    """Best-effort 父背景取色（tkinter 默认灰底是深色主题大敌）。"""
    try:
        return widget.cget("bg")
    except Exception:
        return C["bg"]


def _humanize_command(text: str) -> str:
    """给 git 输出做极简高亮片段，返回 ``(prefix, rest)``。"""
    return text  # 占位；保留接口以便以后扩展


def _run_git(repo: Path, *args: str, timeout: float = 4.0) -> str:
    """同步跑 git 命令并返回 stdout；任何异常一律返回空串。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _resolve_path(repo: Path | None, p: str | os.PathLike) -> Path | None:
    """把 ``p``（绝对或相对 ``repo``）解析为绝对 Path，失败返回 None。"""
    if p is None:
        return None
    try:
        path = Path(p)
    except (TypeError, ValueError):
        return None
    if not path.is_absolute() and repo is not None:
        path = (repo / path).resolve()
    return path


def _scan_tree(root: Path, *, max_depth: int = 8) -> list[dict[str, Any]]:
    """递归扫目录树，返回排序后的节点列表（目录优先，名字其次）。

    节点结构：
        ``{"path": Path, "name": str, "is_dir": bool, "depth": int}``
    """
    nodes: list[dict[str, Any]] = []

    def _walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError, FileNotFoundError):
            return
        # 目录优先 / 名字次之
        entries.sort(key=lambda x: (not x.is_dir(follow_symlinks=False),
                                    x.name.lower()))
        for entry in entries:
            try:
                name = entry.name
                if entry.is_dir(follow_symlinks=False):
                    if name in _SKIP_DIRS:
                        continue
                    nodes.append({"path": entry, "name": name,
                                  "is_dir": True, "depth": depth})
                    _walk(entry, depth + 1)
                elif entry.is_file(follow_symlinks=False):
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
    """解析 ``git status --porcelain`` 为 ``{相对路径: 状态码}``。

    状态码来自 porcelain v1 第一列：``M/A/D/R/C/U/?/?`` 等；空表示未变更。
    """
    out = _run_git(repo, "status", "--porcelain")
    result: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        # 形如 "XY filename"；XY 中第二格是暂存，第一格是工作区
        # 对修改展示：M 工作区、MM 工作区+暂存、A/D 等
        path = line[3:].strip()
        # 处理 rename（``R  old -> new``）
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        result[path.replace("\\", "/")] = code
    return result


def _git_diff_numstat(repo: Path, *,
                       against: str = "HEAD") -> dict[str, tuple[int, int]]:
    """``git diff --numstat`` → ``{path: (added, removed)}``。

    失败回落到 ``git diff --numstat``（无 ``HEAD`` 的初始仓库也能跑）。
    未跟踪文件按 ``+lines -0`` 计。
    """
    out = _run_git(repo, "diff", "--numstat", against)
    if not out and against == "HEAD":
        out = _run_git(repo, "diff", "--numstat")
    result: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, path = parts[0], parts[1], parts[2]
        if a == "-" or d == "-":  # 二进制
            try:
                added, removed = int(a) if a != "-" else 0, int(d) if d != "-" else 0
            except ValueError:
                added = removed = 0
        else:
            try:
                added, removed = int(a), int(d)
            except ValueError:
                added = removed = 0
        result[path.replace("\\", "/")] = (added, removed)

    # 未跟踪文件（``??``）按行数计
    untracked = [k for k, v in _git_status_map(repo).items()
                 if v.strip().startswith("?")]
    for path in untracked:
        full = (repo / path)
        try:
            lines = sum(1 for _ in full.read_bytes().splitlines())
        except OSError:
            lines = 0
        result[path] = (lines, 0)
    return result


def _git_is_repo(repo: Path) -> bool:
    out = _run_git(repo, "rev-parse", "--show-toplevel", timeout=2.0)
    return bool(out.strip())


def _strip_md(text: str) -> str:
    """极简 Markdown 渲染：去掉 ``#``/``**``/``>`` 记号 + 链接转 ``text``。"""
    import re

    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("######"):
            lines.append(("  " + line.lstrip("#").strip()).upper())
        elif line.startswith("#####"):
            lines.append("  " + line.lstrip("#").strip())
        elif line.startswith("####"):
            lines.append(("  " + line.lstrip("#").strip()).upper())
        elif line.startswith("###"):
            lines.append(line.lstrip("#").strip().upper())
        elif line.startswith("##"):
            lines.append(line.lstrip("#").strip().upper())
        elif line.startswith("#"):
            lines.append(line.lstrip("#").strip().upper())
        elif line.startswith(">"):
            lines.append("│ " + line.lstrip(">").strip())
        elif line.startswith("- "):
            lines.append("• " + line[2:])
        elif re.match(r"^\d+\.\s", line):
            lines.append(line)
        else:
            # 链接 [text](url) → text
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
            # 强调符号剥掉
            line = line.replace("**", "").replace("__", "")
            lines.append(line)
    return "\n".join(lines)


# ─── 标签按钮 ─────────────────────────────────────────────


class _Tab(tk.Label):
    """顶部标签条上的可点击标签。"""

    def __init__(self, parent, text: str, *, on_click: Callable[["_Tab"], None]):
        super().__init__(
            parent, text=text, font=FONT_SMALL, padx=PAD_M, pady=6,
            bg=C["bg"], fg=C["ter"], cursor="hand2",
            highlightthickness=1, highlightbackground=C["border"],
        )
        self._on_click = on_click
        self._selected = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _click(self, _e=None):
        try:
            self._on_click(self)
        except Exception:
            pass

    def _on_enter(self, _e=None):
        if not self._selected:
            self.configure(bg=C["hover"], fg=C["text"])

    def _on_leave(self, _e=None):
        if not self._selected:
            self.configure(bg=C["bg"], fg=C["ter"])

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if selected:
            self.configure(bg=C["sel"], fg=C["text"],
                           highlightbackground=C["sel_border"])
        else:
            self.configure(bg=C["bg"], fg=C["ter"],
                           highlightbackground=C["border"])


class _FileRow(tk.Frame):
    """文件树 / 变更列表的一行（图标 + 名字 + git 徽章 + +X/-Y）。"""

    def __init__(self, parent, *, depth: int = 0, indent: int = 14,
                 icon: str = "📄", name: str = "", status: str = "",
                 added: int | None = None, removed: int | None = None,
                 expandable: bool = False, expanded: bool = False,
                 on_click: Callable[["_FileRow"], None] | None = None,
                 on_toggle: Callable[["_FileRow"], None] | None = None):
        super().__init__(parent, bg=C["bg"], highlightthickness=0, bd=0)
        self._on_click = on_click
        self._on_toggle = on_toggle
        self._expanded = expanded
        self._expandable = expandable
        self._status = status

        prefix = "    " * depth
        glyph = ("▾ " if expandable and expanded
                 else "▸ " if expandable else "  ")
        self._toggle_lbl = tk.Label(
            self, text=prefix + glyph + icon + " ", bg=C["bg"], fg=C["body"],
            font=FONT_MONO_SM, anchor="w", padx=0, pady=2,
        )
        self._toggle_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if on_click is not None:
            self._toggle_lbl.configure(cursor="hand2")
            self._toggle_lbl.bind("<Button-1>", self._handle_click)
        if on_toggle is not None and expandable:
            self._toggle_lbl.bind("<Double-Button-1>", lambda _e: on_toggle(self))
        self._toggle_lbl.bind("<Enter>", self._on_enter)
        self._toggle_lbl.bind("<Leave>", self._on_leave)

        self._name_lbl = tk.Label(self, text=name, bg=C["bg"], fg=C["body"],
                                  font=FONT_SMALL, anchor="w", padx=0, pady=2)
        self._name_lbl.pack(side=tk.LEFT)
        if on_click is not None:
            self._name_lbl.configure(cursor="hand2")
            self._name_lbl.bind("<Button-1>", self._handle_click)
            self._name_lbl.bind("<Enter>", self._on_enter)
            self._name_lbl.bind("<Leave>", self._on_leave)

        if status:
            self._badge = badge(self, status,
                                tone={"M": "warn",
                                      "A": "ok",
                                      "D": "error",
                                      "?": "ok",
                                      "U": "muted",
                                      "R": "info"}.get(status[0], "muted"))
            self._badge.pack(side=tk.LEFT, padx=(PAD_S, 0))
        else:
            self._badge = None

        if added is not None or removed is not None:
            num = f"+{added or 0} −{removed or 0}"
            self._stats_lbl = tk.Label(self, text=num, bg=C["bg"],
                                       fg=C["ok"] if (added and not removed)
                                       else C["error"] if (removed and not added)
                                       else C["subtext"],
                                       font=FONT_MONO_XS, padx=0, pady=2)
            self._stats_lbl.pack(side=tk.RIGHT, padx=(PAD_M, PAD_S))
        else:
            self._stats_lbl = None

    def _handle_click(self, _e=None):
        if self._on_click is not None:
            try:
                self._on_click(self)
            except Exception:
                pass

    def _on_enter(self, _e=None):
        for w in (self, self._toggle_lbl, self._name_lbl):
            try:
                w.configure(bg=C["hover"])
            except tk.TclError:
                pass

    def _on_leave(self, _e=None):
        for w in (self, self._toggle_lbl, self._name_lbl):
            try:
                w.configure(bg=C["bg"])
            except tk.TclError:
                pass

    def set_highlight(self, on: bool) -> None:
        for w in (self, self._toggle_lbl, self._name_lbl):
            try:
                w.configure(bg=C["sel"] if on else C["bg"])
            except tk.TclError:
                pass


# ─── 主面板 ────────────────────────────────────────────────


class WorkspacePanel(tk.Frame):
    """右侧工作区面板（深色三栏 IDE 风格）。"""

    _TAB_NAMES = ("文件树", "变更", "代码", "diff", "预览", "终端")

    def __init__(self, parent, app: Any = None, *, repo_root: str | os.PathLike
                 | None = None, on_close: Callable[[], None] | None = None,
                 **kw: Any):
        super().__init__(parent, bg=C["bg"], highlightthickness=0, bd=0, **kw)
        self._app = app
        self._on_close = on_close

        # ─── 仓库根（默认 = 本文件上层） ────────────────────
        default_root = _HERE.parent  # forge-gui/.. → forge-gui-work/
        try:
            self._repo_root: Path = Path(repo_root).resolve() if repo_root \
                else default_root
        except (OSError, ValueError):
            self._repo_root = default_root

        # ─── 内部状态 ───────────────────────────────────────
        self._current_tab: str = "文件树"
        self._current_file: Path | None = None
        self._current_diff_file: Path | None = None
        self._file_tree_rows: list[tuple[_FileRow, dict[str, Any]]] = []
        self._expanded_dirs: set[str] = set()  # 用绝对路径字符串
        self._expanded_dirs.add(str(self._repo_root))  # 默认展开根
        self._tabs: dict[str, _Tab] = {}
        self._breadcrumb_var = tk.StringVar(value=self._repo_root.name)
        self._preview_subtab_var = tk.StringVar(value="预览")
        self._diff_view_var = tk.StringVar(value="列表视图")
        self._filter_var = tk.StringVar(value="全部文件")
        self._is_git_repo: bool = False

        # ─── 顶栏 + 标签条 + 内容区 ──────────────────────────
        self._build_topbar()
        self._build_tabbar()
        self._build_content()
        self._build_preview_subtab()
        self._build_diff_header()

        self._hidden = True
        self.pack_propagate(False)
        self.configure(width=620)
        # 默认隐藏（主程序显式调用 ``show()`` 才出现）
        self.pack_forget()

        # 首屏扫描
        try:
            self.refresh()
        except Exception:
            pass

    # ─── 对外属性 / 切换 ────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        return not self._hidden

    def show(self) -> None:
        if self._hidden:
            self.pack(side=tk.RIGHT, fill=tk.Y, padx=(1, 0))
            self._hidden = False
            try:
                self.refresh()
            except Exception:
                pass

    def hide(self) -> None:
        if not self._hidden:
            self.pack_forget()
            self._hidden = True

    def toggle(self) -> None:
        self.hide() if self.is_visible else self.show()

    def set_repo_root(self, path: str | os.PathLike) -> None:
        try:
            self._repo_root = Path(path).resolve()
        except (OSError, ValueError):
            return
        self._expanded_dirs = {str(self._repo_root)}
        self._current_file = None
        self._current_diff_file = None
        try:
            self.refresh()
        except Exception:
            pass

    # ─── 对外 API ───────────────────────────────────────────

    def open_file_tree(self) -> None:
        self._switch_tab("文件树")

    def open_changes(self) -> None:
        self._switch_tab("变更")

    def open_file(self, path: str | os.PathLike, *, tab: str | None = None
                  ) -> None:
        target = _resolve_path(self._repo_root, path)
        if target is None:
            self._set_status("无效的文件路径", "warn")
            return
        self._current_file = target
        self._current_tab = tab or "代码"
        self._switch_tab(self._current_tab)
        self._load_code_view(target)

    def show_diff(self, path: str | os.PathLike | None = None) -> None:
        if path is not None:
            resolved = _resolve_path(self._repo_root, path)
            if resolved is not None:
                self._current_diff_file = resolved
        self._switch_tab("diff")
        self._render_diff(self._current_diff_file)

    def push_terminal(self, text: str) -> None:
        """追加一行到终端标签（gateway 输出会来这里）。"""
        widget = getattr(self, "_term_text", None)
        if widget is None or not widget.winfo_exists():
            return
        try:
            widget.insert(tk.END, text if text.endswith("\n") else text + "\n")
            widget.see(tk.END)
        except tk.TclError:
            pass

    def changes_count(self) -> int:
        """当前有改动的文件数（git status 条目数；非 git 仓返回 0）。"""
        try:
            return len(self._git_status)
        except Exception:
            return 0

    def refresh(self) -> None:
        """重新扫描文件树 + git 状态。"""
        repo = self._repo_root
        try:
            self._is_git_repo = _git_is_repo(repo)
        except Exception:
            self._is_git_repo = False

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
        self._refresh_diff_total()
        self._update_breadcrumb()

    # ─── 内部：构建 ─────────────────────────────────────────

    def _build_topbar(self) -> None:
        top = tk.Frame(self, bg=C["bg"], height=40, highlightthickness=0, bd=0)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)

        left = tk.Frame(top, bg=C["bg"], highlightthickness=0, bd=0)
        left.pack(side=tk.LEFT, padx=(PAD_M, PAD_S))

        tk.Label(left, text="▤", bg=C["bg"], fg=C["accent"], font=FONT_TITLE
                 ).pack(side=tk.LEFT, padx=(0, PAD_XS))
        tk.Label(left, text="工作区", bg=C["bg"], fg=C["text"],
                 font=(FONT_UI[0], 14, "bold")).pack(side=tk.LEFT)
        badge(left, "Beta", tone="accent").pack(side=tk.LEFT,
                                                padx=(PAD_S, 0))

        # 面包屑
        crumb = tk.Frame(top, bg=C["bg"], highlightthickness=0, bd=0)
        crumb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=PAD_S)
        self._crumb_lbl = tk.Label(
            crumb, textvariable=self._breadcrumb_var, bg=C["bg"], fg=C["ter"],
            font=FONT_SMALL, anchor="w")
        self._crumb_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 关闭按钮
        glyph_button(top, "✕", self.hide).pack(side=tk.RIGHT, padx=PAD_S)

        # 1px 下边界
        divider(top).pack(side=tk.BOTTOM, fill=tk.X)

    def _build_tabbar(self) -> None:
        bar = tk.Frame(self, bg=C["bg"], height=34, highlightthickness=0, bd=0)
        bar.pack(side=tk.TOP, fill=tk.X)
        bar.pack_propagate(False)

        def _click(t: _Tab) -> None:
            self._switch_tab(t.cget("text").split(" (")[0])

        for name in self._TAB_NAMES:
            t = _Tab(bar, name, on_click=_click)
            t.pack(side=tk.LEFT, padx=(PAD_XS, 0))
            self._tabs[name] = t
        divider(self).pack(side=tk.TOP, fill=tk.X)

    def _build_content(self) -> None:
        body = tk.Frame(self, bg=C["bg"], highlightthickness=0, bd=0)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._body = body

        # ─ 文件树 ────────────────────────────────────────────
        tree = tk.Frame(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._tab_frames = {"文件树": tree}

        tree_top = tk.Frame(tree, bg=C["bg"], highlightthickness=0, bd=0,
                            height=32)
        tree_top.pack(side=tk.TOP, fill=tk.X)
        tree_top.pack_propagate(False)
        tk.Label(tree_top, text="文件树", bg=C["bg"], fg=C["ter"],
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=PAD_M)
        pill_button(tree_top, "刷新", self.refresh, kind="quiet"
                    ).pack(side=tk.RIGHT, padx=PAD_S)

        self._tree_canvas_frame = tk.Frame(tree, bg=C["bg"],
                                           highlightthickness=0, bd=0)
        self._tree_canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._tree_empty_lbl = tk.Label(
            tree, text="不是 git 仓库或无文件", bg=C["bg"], fg=C["muted"],
            font=FONT_SMALL)
        self._tree_body = tk.Frame(self._tree_canvas_frame, bg=C["bg"],
                                   highlightthickness=0, bd=0)
        self._tree_body.pack(fill=tk.BOTH, expand=True)

        # ─ 变更 ──────────────────────────────────────────────
        changes = tk.Frame(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._tab_frames["变更"] = changes
        self._changes_total_lbl = tk.Label(
            changes, text="共 0 个变更", bg=C["bg"], fg=C["subtext"],
            font=FONT_SMALL, anchor="w", padx=PAD_M)
        self._changes_total_lbl.pack(side=tk.TOP, fill=tk.X, pady=(PAD_S, 0))

        chip_bar = tk.Frame(changes, bg=C["bg"], highlightthickness=0, bd=0)
        chip_bar.pack(side=tk.TOP, fill=tk.X, padx=PAD_M, pady=PAD_S)
        self._filter_chips: dict[str, tk.Label] = {}
        for name in ("全部文件", "已修改", "新增", "已删除"):
            c = chip(chip_bar, name, selected=(name == "全部文件"),
                     command=lambda n=name: self._set_filter(n))
            c.pack(side=tk.LEFT, padx=(0, PAD_XS))
            self._filter_chips[name] = c

        self._changes_body = tk.Frame(changes, bg=C["bg"],
                                      highlightthickness=0, bd=0)
        self._changes_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                                padx=PAD_M)

        # ─ 代码 ──────────────────────────────────────────────
        code = tk.Frame(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._tab_frames["代码"] = code
        self._code_meta_var = tk.StringVar(value="未打开文件")
        tk.Label(code, textvariable=self._code_meta_var, bg=C["bg"],
                 fg=C["subtext"], font=FONT_SMALL, anchor="w",
                 padx=PAD_M).pack(side=tk.TOP, fill=tk.X, pady=(PAD_S, 0))

        code_body = tk.Frame(code, bg=C["code_bg"], highlightthickness=0, bd=0)
        code_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                       padx=PAD_M, pady=(PAD_S, PAD_M))
        code_body.pack_propagate(False)

        # 行号槽（独立 Text，纵向滚动同步）
        gutter = tk.Text(
            code_body, width=4, bg=C["code_bg"], fg=C["muted"],
            font=FONT_MONO_XS, padx=4, pady=4,
            relief=tk.FLAT, highlightthickness=0, bd=0, takefocus=0,
            wrap="none", state=tk.DISABLED, cursor="arrow",
            exportselection=False)
        gutter.pack(side=tk.LEFT, fill=tk.Y)
        self._gutter = gutter

        # 主 Text
        main_text = tk.Text(
            code_body, bg=C["code_bg"], fg=C["code_plain"],
            font=FONT_MONO_SM, wrap="none",
            relief=tk.FLAT, highlightthickness=0, bd=0,
            takefocus=0, cursor="arrow",
            exportselection=False)
        main_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # 水平滚动条
        hbar = tk.Scrollbar(code_body, orient=tk.HORIZONTAL,
                            command=main_text.xview, bg=C["surface2"],
                            troughcolor=C["chat"], activebackground=C["scroll"],
                            relief=tk.FLAT, bd=0, highlightthickness=0)
        main_text.configure(xscrollcommand=hbar.set)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        # 垂直滚动条（ttk 主题）
        vbar = tk.Scrollbar(code_body, orient=tk.VERTICAL,
                            command=self._on_vscroll, bg=C["surface2"],
                            troughcolor=C["chat"], activebackground=C["scroll"],
                            relief=tk.FLAT, bd=0, highlightthickness=0)
        main_text.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        style_scrollbar(main_text)

        setup_code_tags(main_text)
        self._code_text = main_text

        # ─ diff ──────────────────────────────────────────────
        diff = tk.Frame(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._tab_frames["diff"] = diff
        diff_body = tk.Frame(diff, bg=C["bg"], highlightthickness=0, bd=0)
        diff_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                       padx=PAD_M, pady=(PAD_S, PAD_M))
        diff_text = tk.Text(
            diff_body, bg=C["code_bg"], fg=C["code_plain"],
            font=FONT_MONO_SM, wrap="none",
            relief=tk.FLAT, highlightthickness=0, bd=0,
            takefocus=0, cursor="arrow")
        diff_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        diff_vbar = tk.Scrollbar(diff_body, orient=tk.VERTICAL,
                                 command=diff_text.yview,
                                 bg=C["surface2"], troughcolor=C["chat"],
                                 activebackground=C["scroll"],
                                 relief=tk.FLAT, bd=0, highlightthickness=0)
        diff_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        style_scrollbar(diff_text)
        setup_code_tags(diff_text)
        diff_text.configure(state=tk.DISABLED)
        self._diff_text = diff_text
        self._diff_total_var = tk.StringVar(value="+0 −0")

        # ─ 预览 ──────────────────────────────────────────────
        preview = tk.Frame(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._tab_frames["预览"] = preview
        preview_body = tk.Frame(preview, bg=C["bg"], highlightthickness=0, bd=0)
        preview_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                           padx=PAD_M, pady=(PAD_S, PAD_M))
        preview_text = tk.Text(
            preview_body, bg=C["code_bg"], fg=C["code_plain"],
            font=FONT_MONO_SM, wrap="word",
            relief=tk.FLAT, highlightthickness=0, bd=0,
            takefocus=0, cursor="arrow")
        preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        preview_vbar = tk.Scrollbar(preview_body, orient=tk.VERTICAL,
                                    command=preview_text.yview,
                                    bg=C["surface2"], troughcolor=C["chat"],
                                    activebackground=C["scroll"],
                                    relief=tk.FLAT, bd=0, highlightthickness=0)
        preview_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        style_scrollbar(preview_text)
        setup_code_tags(preview_text)
        preview_text.configure(state=tk.DISABLED)
        self._preview_text = preview_text
        self._preview_canvas_holder: tk.Frame | None = None

        # ─ 终端 ──────────────────────────────────────────────
        term = tk.Frame(body, bg=C["bg"], highlightthickness=0, bd=0)
        self._tab_frames["终端"] = term
        term_body = tk.Frame(term, bg=C["code_bg"], highlightthickness=0, bd=0)
        term_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                       padx=PAD_M, pady=(PAD_S, PAD_M))
        term_text = tk.Text(
            term_body, bg=C["code_bg"], fg=C["body"],
            font=FONT_MONO_SM, wrap="none",
            relief=tk.FLAT, highlightthickness=0, bd=0,
            takefocus=0, cursor="arrow")
        term_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        term_vbar = tk.Scrollbar(term_body, orient=tk.VERTICAL,
                                 command=term_text.yview,
                                 bg=C["surface2"], troughcolor=C["chat"],
                                 activebackground=C["scroll"],
                                 relief=tk.FLAT, bd=0, highlightthickness=0)
        term_vbar.pack(side=tk.RIGHT, fill=tk.Y)
        style_scrollbar(term_text)
        term_text.insert("1.0", "gateway 输出会显示在这里\n")
        term_text.configure(fg=C["muted"])
        self._term_text = term_text

    def _build_preview_subtab(self) -> None:
        """预览标签内部的子标签条 + 右上角按钮。"""
        preview = self._tab_frames["预览"]
        # 顶部一行
        top = tk.Frame(preview, bg=C["bg"], height=34, highlightthickness=0,
                       bd=0)
        top.pack(side=tk.TOP, fill=tk.X, padx=PAD_M, pady=(PAD_S, 0))
        top.pack_propagate(False)

        sub = tk.Frame(top, bg=C["bg"], highlightthickness=0, bd=0)
        sub.pack(side=tk.LEFT)

        def _make_sub(name: str) -> tk.Label:
            lbl = tk.Label(sub, text=name, bg=C["bg"], fg=C["ter"],
                           font=FONT_SMALL, padx=PAD_S, pady=4, cursor="hand2")
            lbl.bind("<Button-1>", lambda _e, n=name: self._set_preview_sub(n))
            lbl.bind("<Enter>", lambda _e, w=lbl: w.configure(fg=C["text"]))
            lbl.bind("<Leave>",
                     lambda _e, w=lbl, n=name: w.configure(
                         fg=C["text"] if self._preview_subtab_var.get() == n
                         else C["ter"]))
            return lbl

        for name in ("预览", "控制台", "终端", "图像", "Markdown"):
            _make_sub(name).pack(side=tk.LEFT)

        # 右侧两个按钮
        actions = tk.Frame(top, bg=C["bg"], highlightthickness=0, bd=0)
        actions.pack(side=tk.RIGHT)
        self._open_external_btn = pill_button(
            actions, "在新窗口打开", self._open_current_external,
            kind="quiet")
        self._open_external_btn.pack(side=tk.RIGHT, padx=(PAD_XS, 0))
        self._refresh_btn = pill_button(
            actions, "刷新", self._reload_preview, kind="quiet")
        self._refresh_btn.pack(side=tk.RIGHT, padx=(PAD_XS, 0))

        # 子标签下划线（占位；颜色在 _set_preview_sub 切换）
        self._sub_underline = tk.Frame(self._tab_frames["预览"], bg=C["accent"],
                                       height=2)
        self._sub_underline.place_forget()  # 第一次画完再定位
        self._preview_sub_holder = top  # 用于下划线定位

    def _build_diff_header(self) -> None:
        """diff 标签的顶部（标题 + 视图切换）。"""
        diff = self._tab_frames["diff"]
        top = tk.Frame(diff, bg=C["bg"], height=32, highlightthickness=0, bd=0)
        top.pack(side=tk.TOP, fill=tk.X, padx=PAD_M, pady=(PAD_S, 0))
        top.pack_propagate(False)

        self._diff_title_var = tk.StringVar(value="变更")
        tk.Label(top, textvariable=self._diff_title_var, bg=C["bg"],
                 fg=C["text"], font=FONT_UI_BOLD, anchor="w"
                 ).pack(side=tk.LEFT)
        tk.Label(top, textvariable=self._diff_total_var, bg=C["bg"],
                 fg=C["subtext"], font=FONT_MONO_XS
                 ).pack(side=tk.LEFT, padx=(PAD_S, 0))

        # 右侧：列表视图 / 分栏视图（分栏 disabled + tooltip）
        right = tk.Frame(top, bg=C["bg"], highlightthickness=0, bd=0)
        right.pack(side=tk.RIGHT)
        self._list_view_btn = pill_button(
            right, "列表视图", lambda: None, kind="quiet")
        self._list_view_btn.configure(state=tk.DISABLED)
        attach_tooltip(self._list_view_btn, "列表视图（当前）")
        self._list_view_btn.pack(side=tk.RIGHT, padx=(PAD_XS, 0))
        self._split_view_btn = pill_button(
            right, "分栏视图", lambda: None, kind="quiet")
        self._split_view_btn.configure(state=tk.DISABLED)
        attach_tooltip(self._split_view_btn, "分栏视图暂未实现")
        self._split_view_btn.pack(side=tk.RIGHT, padx=(PAD_XS, 0))

    # ─── 内部：行为 ─────────────────────────────────────────

    def _on_vscroll(self, *args: Any) -> None:
        """主 Text 滚动 → 同步行号槽。"""
        try:
            self._code_text.yview_moveto(args[0])
            self._gutter.yview_moveto(args[0])
        except (tk.TclError, IndexError):
            pass

    def _set_status(self, msg: str, level: str = "info") -> None:
        setter = getattr(self._app, "_set_status", None) if self._app else None
        if callable(setter):
            try:
                setter(msg, level)
            except Exception:
                pass

    def _switch_tab(self, name: str) -> None:
        if name not in self._tab_frames:
            return
        self._current_tab = name
        for n, frame in self._tab_frames.items():
            try:
                if n == name:
                    frame.pack(fill=tk.BOTH, expand=True)
                else:
                    frame.pack_forget()
            except tk.TclError:
                pass
        for n, tab in self._tabs.items():
            tab.set_selected(n == name)
        # 同步面包屑 / 状态
        if name == "代码" and self._current_file is not None:
            self._update_breadcrumb(file=self._current_file)
        elif name == "diff" and self._current_diff_file is not None:
            self._update_breadcrumb(file=self._current_diff_file, diff=True)
        else:
            self._update_breadcrumb()

    def _set_filter(self, name: str) -> None:
        self._filter_var.set(name)
        for n, c in self._filter_chips.items():
            c.configure(bg=C["accent_soft"] if n == name else C["surface2"],
                        fg=C["accent_text"] if n == name else C["ter"],
                        highlightbackground=C["accent"] if n == name
                        else C["border_hi"])
        self._refresh_changes()

    def _set_preview_sub(self, name: str) -> None:
        self._preview_subtab_var.set(name)
        # 重新渲染当前文件
        if self._current_file is not None:
            self._render_preview(self._current_file)

    def _update_breadcrumb(self, *, file: Path | None = None,
                           diff: bool = False) -> None:
        repo = self._repo_root
        try:
            rel_root = repo.relative_to(repo.parent.parent) \
                if repo.parent.parent else repo
        except ValueError:
            rel_root = repo
        parts = [repo.name]
        if file is not None:
            try:
                rel = file.resolve().relative_to(repo.resolve())
                parts.extend(rel.parts)
            except ValueError:
                parts.append(file.name)
        text = " › ".join(parts)
        self._breadcrumb_var.set(text)

    # ─── 文件树 ─────────────────────────────────────────────

    def _refresh_file_tree(self) -> None:
        body = self._tree_body
        for child in list(body.winfo_children()):
            child.destroy()
        self._file_tree_rows.clear()

        repo = self._repo_root
        try:
            nodes = _scan_tree(repo)
        except Exception as exc:
            tk.Label(body, text=f"扫描失败：{exc}", bg=C["bg"],
                     fg=C["muted"], font=FONT_SMALL).pack(anchor="w",
                                                          padx=PAD_M,
                                                          pady=PAD_M)
            return

        if not nodes or len(nodes) <= 1:
            tk.Label(body, text="（无文件）", bg=C["bg"],
                     fg=C["muted"], font=FONT_SMALL).pack(anchor="w",
                                                          padx=PAD_M,
                                                          pady=PAD_M)
            return

        # 顶层根节点隐藏（已经在面包屑显示），从深度 1 开始
        rows_container = body
        for node in nodes[1:]:
            depth = node["depth"] - 1  # 把根目录的 1 层缩进吃掉
            path: Path = node["path"]
            name = node["name"]
            is_dir = node["is_dir"]

            # 跳过未展开的子树（但要保留根的直接子）
            if is_dir and depth > 0 and str(path) not in self._expanded_dirs:
                continue

            # 状态徽章（仅文件）
            status = ""
            if not is_dir:
                try:
                    rel = path.resolve().relative_to(repo.resolve())
                    rel_str = str(rel).replace("\\", "/")
                    status = self._git_status.get(rel_str, "")
                except (ValueError, OSError):
                    status = ""

            icon = ("📁" if is_dir
                    else "🐍" if name.endswith(".py")
                    else "📄" if name.endswith(".md")
                    else "📄")

            def _on_row(_r: _FileRow = None, p: Path = path,
                        d: bool = is_dir, n: str = name) -> None:
                if d:
                    self._toggle_dir(p)
                else:
                    self.open_file(p)

            def _on_dir_toggle(_r: _FileRow = None, p: Path = path) -> None:
                self._toggle_dir(p)

            row = _FileRow(
                rows_container,
                depth=max(0, depth),
                icon=icon, name=name,
                status=status,
                expandable=is_dir,
                expanded=str(path) in self._expanded_dirs,
                on_click=_on_row,
                on_toggle=_on_dir_toggle,
            )
            row.pack(fill=tk.X, anchor="w", padx=(PAD_S, PAD_S),
                     pady=max(0, (depth == 0) - 1))
            self._file_tree_rows.append((row, node))

    def _toggle_dir(self, path: Path) -> None:
        key = str(path)
        if key in self._expanded_dirs:
            self._expanded_dirs.discard(key)
        else:
            self._expanded_dirs.add(key)
        try:
            self._refresh_file_tree()
        except Exception:
            pass

    # ─── 变更 ───────────────────────────────────────────────

    def _refresh_changes(self) -> None:
        body = self._changes_body
        for child in list(body.winfo_children()):
            child.destroy()

        if not self._is_git_repo:
            tk.Label(body, text="不是 git 仓库", bg=C["bg"], fg=C["muted"],
                     font=FONT_SMALL).pack(anchor="w", pady=PAD_M)
            self._changes_total_lbl.configure(text="不是 git 仓库")
            return

        repo = self._repo_root
        status = self._git_status
        numstat = self._git_numstat

        # 按当前 filter 筛选
        cur = self._filter_var.get()
        rows: list[tuple[str, str, int, int]] = []
        for path, code in status.items():
            primary = code.strip()[:1] or "?"
            if cur == "已修改" and primary != "M":
                continue
            if cur == "新增" and primary not in ("A", "?"):
                continue
            if cur == "已删除" and primary != "D":
                continue
            added, removed = numstat.get(path, (0, 0))
            rows.append((code.strip() or "?", path, added, removed))

        # 顶部统计
        total_add = sum(r[2] for r in rows)
        total_del = sum(r[3] for r in rows)
        self._diff_total_var.set(f"+{total_add} −{total_del}")
        self._changes_total_lbl.configure(
            text=f"共 {len(rows)} 个变更 · +{total_add} −{total_del}"
        )

        if not rows:
            tk.Label(body, text="无变更", bg=C["bg"], fg=C["muted"],
                     font=FONT_SMALL).pack(anchor="w", pady=PAD_M)
            return

        for code, path, added, removed in rows:
            row = _FileRow(
                body,
                depth=0,
                icon="📄",
                name=path,
                status=code or "?",
                added=added, removed=removed,
                on_click=lambda _r=None, p=path: self.show_diff(p),
            )
            row.pack(fill=tk.X, anchor="w", pady=max(0, 0))

    def _refresh_diff_total(self) -> None:
        if self._is_git_repo and hasattr(self, "_diff_total_var"):
            numstat = getattr(self, "_git_numstat", {})
            total_add = sum(v[0] for v in numstat.values())
            total_del = sum(v[1] for v in numstat.values())
            self._diff_total_var.set(f"+{total_add} −{total_del}")

    # ─── 代码 ───────────────────────────────────────────────

    def _load_code_view(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except (OSError, FileNotFoundError) as exc:
            self._code_meta_var.set(f"无法读取：{exc}")
            self._set_code_text(f"无法读取文件：{exc}")
            self._update_gutter()
            return

        size = len(data)
        truncated = False
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                text = data.decode("gbk")
                encoding = "gbk"
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
                encoding = "utf-8 (含替换)"

        lines = text.splitlines()
        if size > _MAX_FILE_BYTES or len(lines) > _MAX_FILE_LINES:
            truncated = True
            lines = lines[:_MAX_FILE_LINES]

        # 行号 / meta
        rel_str = self._rel_label(path)
        note = "（已截断）" if truncated else ""
        self._code_meta_var.set(
            f"{rel_str} · {human_size(size)} · {encoding}{note}"
        )
        self._set_code_text("\n".join(lines), truncate_hint=truncated)
        if path.suffix.lower() == ".py":
            try:
                highlight_python(self._code_text)
            except Exception:
                pass
        self._update_gutter()
        self._update_breadcrumb(file=path)

    def _set_code_text(self, content: str, *, truncate_hint: bool = False) \
            -> None:
        text = self._code_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert("1.0", content)
        if truncate_hint:
            text.insert(tk.END, "\n\n…（文件过大，仅显示前 "
                             f"{_MAX_FILE_LINES} 行）")
        text.configure(state=tk.DISABLED)

    def _update_gutter(self) -> None:
        try:
            width = max(3, len(str(self._code_text.index('end-1c')
                                   .split('.')[0])) + 1)
        except tk.TclError:
            width = 4
        self._gutter.configure(width=width, state=tk.NORMAL)
        self._gutter.delete("1.0", tk.END)
        self._gutter.insert("1.0", gutter_lines(self._code_text,
                                                 gutter_width=width - 1))
        # 让 gutter 的滚动位置对齐主 Text
        try:
            self._gutter.yview_moveto(self._code_text.yview()[0])
        except (tk.TclError, IndexError):
            pass
        self._gutter.configure(state=tk.DISABLED)

    def _rel_label(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._repo_root.resolve())) \
                .replace("\\", "/")
        except ValueError:
            return path.name

    # ─── diff ───────────────────────────────────────────────

    def _render_diff(self, path: Path | None) -> None:
        text = self._diff_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)

        repo = self._repo_root
        if not self._is_git_repo:
            text.insert("1.0", "（不是 git 仓库）")
            text.configure(state=tk.DISABLED)
            self._diff_title_var.set("变更")
            return

        args = ["diff"]
        if path is not None:
            args.extend(["--", str(path)])
            self._diff_title_var.set(self._rel_label(path))
        else:
            self._diff_title_var.set("变更")

        out = _run_git(repo, *args)
        if not out and path is None:
            text.insert("1.0", "无改动\n")
            text.configure(state=tk.DISABLED)
            return
        if not out and path is not None:
            # 未跟踪的新文件：git diff 为空，按「整文件新增」渲染
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                raw = ""
            if raw:
                text.insert(tk.END, f"新增文件（未跟踪）：{self._rel_label(path)}\n", ("meta",))
                for line in raw.splitlines():
                    text.insert(tk.END, line + "\n", ("add",))
                text.configure(state=tk.DISABLED)
                self._diff_total_var.set(f"+{len(raw.splitlines())} −0")
                return
            text.insert("1.0", "无改动\n")
            text.configure(state=tk.DISABLED)
            return

        lines = out.splitlines()
        for i, line in enumerate(lines, start=1):
            tag = ""
            if line.startswith("+++") or line.startswith("---"):
                tag = "meta"
            elif line.startswith("@@"):
                tag = "hunk"
            elif line.startswith("diff --git") or line.startswith("index "):
                tag = "meta"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            start = f"{i}.0"
            end = f"{i}.end"
            if tag:
                text.insert(tk.END, line + "\n", (tag,))
            else:
                text.insert(tk.END, line + "\n")
        text.configure(state=tk.DISABLED)

        # 总计
        if path is None:
            self._refresh_diff_total()
        else:
            rel = self._rel_label(path)
            ns = self._git_numstat.get(rel, (0, 0))
            self._diff_total_var.set(f"+{ns[0]} −{ns[1]}")

    # ─── 预览 ───────────────────────────────────────────────

    def _reload_preview(self) -> None:
        if self._current_file is not None:
            self._render_preview(self._current_file)

    def _open_current_external(self) -> None:
        path = self._current_file
        if path is None:
            self._set_status("没有可打开的文件", "warn")
            return
        url = path.resolve().as_uri()
        # 优先 app._open_path；回落到 webbrowser.open / os.startfile
        opener = getattr(self._app, "_open_path", None)
        if callable(opener):
            try:
                opener(str(path))
                return
            except Exception:
                pass
        try:
            webbrowser.open(url)
        except Exception:
            try:
                os.startfile(str(path))  # type: ignore[attr-defined]
            except Exception as exc:
                self._set_status(f"无法打开：{exc}", "error")

    def _render_preview(self, path: Path) -> None:
        # 拆掉之前可能的图像 holder
        for child in list(self._tab_frames["预览"].winfo_children()):
            if child is self._preview_sub_holder:
                continue
        # 先清空 Text + 隐藏图像区
        self._preview_text.configure(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)
        # 删除旧的 canvas holder
        for child in list(self._tab_frames["预览"].winfo_children()):
            if getattr(child, "_aip_canvas_holder", False):
                child.destroy()

        suffix = path.suffix.lower()
        # 图像：渲染缩略图（PNG/GIF/BMP 走 PhotoImage，其它格式给出提示）
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        if suffix in image_exts and self._preview_subtab_var.get() in (
                "预览", "图像"):
            holder = tk.Frame(self._tab_frames["预览"], bg=C["code_bg"],
                              highlightthickness=0, bd=0)
            holder._aip_canvas_holder = True  # type: ignore[attr-defined]
            holder.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                        padx=PAD_M, pady=(0, PAD_M))
            canvas = tk.Canvas(holder, bg=C["code_bg"], highlightthickness=0,
                               bd=0)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            ok = self._draw_image_thumbnail(canvas, path)
            if ok:
                self._preview_text.configure(state=tk.DISABLED)
                return
            # 退化：提示 + 文件信息
            self._preview_text.insert("1.0",
                                      f"该格式需在新窗口打开\n\n"
                                      f"文件：{path}\n大小：{human_size(self._safe_size(path))}\n")
            self._preview_text.configure(state=tk.DISABLED)
            return

        # Markdown / HTML：读文本
        try:
            data = path.read_bytes()
            size = len(data)
        except OSError as exc:
            self._preview_text.insert("1.0", f"无法读取：{exc}")
            self._preview_text.configure(state=tk.DISABLED)
            return

        sub = self._preview_subtab_var.get()
        if suffix in {".html", ".htm", ".svg"}:
            try:
                txt = data.decode("utf-8")
            except UnicodeDecodeError:
                txt = data.decode("utf-8", errors="replace")
            self._preview_text.insert("1.0",
                                      f"{txt}\n\n（HTML/SVG 源码 · 可在新窗口打开）")
            self._preview_text.configure(state=tk.DISABLED)
            return

        if suffix == ".md" and sub in ("预览", "Markdown"):
            try:
                txt = data.decode("utf-8")
            except UnicodeDecodeError:
                txt = data.decode("utf-8", errors="replace")
            self._preview_text.insert("1.0", _strip_md(txt))
            self._preview_text.configure(state=tk.DISABLED)
            return

        # 其它文本
        try:
            txt = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                txt = data.decode("gbk")
            except UnicodeDecodeError:
                self._preview_text.insert(
                    "1.0", f"二进制文件 · {human_size(size)} · 可在新窗口打开")
                self._preview_text.configure(state=tk.DISABLED)
                return
        self._preview_text.insert("1.0", txt)
        self._preview_text.configure(state=tk.DISABLED)

    def _draw_image_thumbnail(self, canvas: tk.Canvas, path: Path) -> bool:
        """返回 True 表示成功在 Canvas 上画了缩略图。"""
        try:
            img = tk.PhotoImage(file=str(path))
        except (tk.TclError, OSError):
            return False
        w, h = img.width(), img.height()
        max_w, max_h = 640, 360
        scale = min(1.0, max_w / max(w, 1), max_h / max(h, 1))
        if scale < 1.0:
            # PhotoImage 只支持整数缩小倍数
            factor = max(1, int(round(1 / scale)))
            try:
                img = img.subsample(factor)
            except tk.TclError:
                pass
        canvas.delete("all")
        canvas.create_image(10, 10, image=img, anchor="nw")
        canvas.image = img  # type: ignore[attr-defined]  # 防 GC
        # 让 canvas 自适应
        canvas.configure(width=img.width() + 20, height=img.height() + 20)
        return True

    @staticmethod
    def _safe_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0


# ─── 入口（无头冒烟） ──────────────────────────────────────


def _selftest(repo: str | None = None) -> None:
    """无 tk 弹窗的冒烟：构造 → 各种 API → 销毁。"""
    root = tk.Tk()
    root.withdraw()
    try:
        # 没指定就用本仓库父目录下的 pledge-evolving
        rr = repo or r"C:\Users\匡溯昀\pledge-evolving"
        panel = WorkspacePanel(root, repo_root=rr)
        # 各 API 路径走一遍
        for fn in (panel.show,
                   panel.open_file_tree,
                   panel.open_changes,
                   panel.refresh,
                   lambda: panel.show_diff(),
                   panel.hide):
            try:
                fn()
                root.update()
            except Exception as exc:
                print(f"step {fn!r} failed: {exc}")

        # 找一个 .py 文件走 open_file
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
            try:
                panel.open_file(sample)
                root.update()
            except Exception as exc:
                print(f"open_file failed: {exc}")

        try:
            panel.push_terminal("test")
            root.update()
        except Exception as exc:
            print(f"push_terminal failed: {exc}")

        print("selftest ok")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _selftest_non_git() -> None:
    """把 repo_root 指到非 git 目录，验证不会崩。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ws_nongit_")
    root = tk.Tk()
    root.withdraw()
    try:
        panel = WorkspacePanel(root, repo_root=tmp)
        panel.show()
        for fn in (panel.open_file_tree, panel.open_changes,
                   panel.refresh, panel.hide):
            try:
                fn()
                root.update()
            except Exception as exc:
                print(f"non-git step {fn!r} failed: {exc}")
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
        # 简单可视化启动（一般不在这里跑，留给主程序调用）
        r = tk.Tk()
        r.title("WorkspacePanel 预览")
        r.configure(bg=C["bg"])
        r.geometry("640x720+50+50")
        ws = WorkspacePanel(r, repo_root=r"C:\Users\匡溯昀\pledge-evolving")
        ws.show()
        ws.open_file_tree()
        r.mainloop()