"""
pledge-evolving Windows GUI — 跨平台桌面交互界面
零依赖，纯 tkinter（Python 3.10+ 自带），双击即用。
支持 Windows / macOS / Linux 统一体验。

用法：
    python forge_gui.py          # 直接运行
    pythonw forge_gui.py        # 无控制台窗口（Windows）

功能：
    - 输入任务，一键运行 forge run
    - 选择策略（economy / balanced / premium）
    - 实时输出日志
    - selftest / doctor 快捷按钮
    - 跨平台：Windows / macOS / Linux 自适应字体与 DPI
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
from pathlib import Path

# ─── 常量 ────────────────────────────────────────────────
WINDOW_SIZE = "820x600"
MIN_SIZE = (640, 440)
MAX_LOG_LINES = 2000
FLUSH_THRESHOLD = 16

# ─── 跨平台检测 ─────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ─── 跨平台字体 ─────────────────────────────────────────
if IS_MACOS:
    FONT_FAMILY = ("SF Mono", "Menlo", "Monaco", "Courier New")
    FONT_FAMILY_UI = ("SF Pro Text", "Helvetica Neue", "Helvetica", "Arial")
    FONT_SIZE = 12
    FONT_SIZE_UI = 13
    FONT_SIZE_BTN = 13
    PAD_X = 16
    PAD_Y_TOP = 14
    PAD_Y_MID = 6
    ENTRY_PADY = 6
elif IS_LINUX:
    FONT_FAMILY = ("JetBrains Mono", "Fira Code", "DejaVu Sans Mono", "Liberation Mono")
    FONT_FAMILY_UI = ("Cantarell", "Noto Sans", "DejaVu Sans", "Liberation Sans")
    FONT_SIZE = 11
    FONT_SIZE_UI = 11
    FONT_SIZE_BTN = 11
    PAD_X = 14
    PAD_Y_TOP = 12
    PAD_Y_MID = 5
    ENTRY_PADY = 5
else:  # Windows
    FONT_FAMILY = ("Cascadia Code", "Consolas", "Lucida Console", "Courier New")
    FONT_FAMILY_UI = "Segoe UI"
    FONT_SIZE = 11
    FONT_SIZE_UI = 10
    FONT_SIZE_BTN = 10
    PAD_X = 12
    PAD_Y_TOP = 10
    PAD_Y_MID = 4
    ENTRY_PADY = 4

# ─── 调色板 ──────────────────────────────────────────────
# Catppuccin Mocha-inspired palette
C = {
    "bg":       "#1e1e2e",   # 窗口背景
    "surface":  "#313244",   # 卡片/按钮背景
    "overlay":  "#45475a",   # hover 状态
    "text":     "#cdd6f4",   # 主文字
    "subtext":  "#a6adc8",   # 次要文字
    "muted":    "#6c7086",   # 禁用/占位
    "accent":   "#89b4fa",   # 主题色（蓝）
    "accent2":  "#a6e3a1",   # 成功（绿）
    "error":    "#f38ba8",   # 错误（红）
    "warn":     "#f9e2af",   # 警告（黄）
    "log_bg":   "#11111b",   # 日志背景
    "log_fg":   "#bac2de",   # 日志文字
    "input_bg": "#181825",   # 输入框背景
    "border":   "#585b70",   # 边框

}


# ─── 路径探测 ────────────────────────────────────────────
def _find_run_py() -> Path | None:
    """多策略定位 run.py：同目录 → 环境变量 → 向上查找 3 层。"""
    here = Path(__file__).resolve().parent
    candidate = here / "run.py"
    if candidate.is_file():
        return candidate
    forge_repo = os.environ.get("FORGE_REPO", "").strip()
    if forge_repo:
        env_repo = Path(forge_repo)
        if env_repo.is_dir():
            candidate = env_repo / "run.py"
            if candidate.is_file():
                return candidate
    # 不做盲目向上查找——避免在临时目录等意外位置绑定错仓库
    return None


RUN_PY = _find_run_py()
if RUN_PY is None:
    raise RuntimeError(
        "找不到 run.py：请设置 FORGE_REPO 环境变量，"
        "或将 forge_gui.py 放在 run.py 同目录"
    )
FORGE_REPO = RUN_PY.parent


# ─── 跨平台 DPI 适配 ────────────────────────────────────
def _setup_dpi():
    """高 DPI 适配：Windows 和 macOS 各自处理。"""
    if IS_WINDOWS:
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    # macOS 和 Linux 的 tkinter 在高 DPI 下默认行为较好，无需额外处理


class ForgeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("forge")
        self.root.geometry(WINDOW_SIZE)
        self.root.configure(bg=C["bg"])
        self.root.minsize(*MIN_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 跨平台图标（不依赖外部文件）
        self._set_icon()

        self._running = False
        self._proc: subprocess.Popen | None = None

        self._build_ui()

    def _set_icon(self):
        """设置窗口图标（跨平台兼容）。"""
        try:
            # 用 tkinter 内置的 photo image 设置小图标
            icon_size = 16
            icon = tk.PhotoImage(width=icon_size, height=icon_size)
            # 画一个简单的 F 字母作为图标
            for y in range(icon_size):
                for x in range(icon_size):
                    # 简单的几何图形
                    if (2 <= x <= 13 and 2 <= y <= 4) or \
                       (2 <= x <= 4 and 2 <= y <= 13) or \
                       (2 <= x <= 10 and 7 <= y <= 9):
                        icon.put(C["accent"], (x, y))
            self.root.iconphoto(True, icon)
        except Exception:
            pass  # 某些窗口管理器不支持

    # ── UI 构建 ──────────────────────────────────────────
    def _add_hover(self, widget, normal_bg, hover_bg, normal_fg=None, hover_fg=None):
        """为按钮添加 hover 效果。"""
        fg = normal_fg or widget.cget("fg")
        hfg = hover_fg or fg
        def on_enter(e):
            widget.configure(bg=hover_bg, fg=hfg)
        def on_leave(e):
            widget.configure(bg=normal_bg, fg=fg)
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)

    def _build_ui(self):
        # 主容器（留内边距）
        main = tk.Frame(self.root, bg=C["bg"], padx=PAD_X, pady=PAD_Y_TOP)
        main.pack(fill=tk.BOTH, expand=True)

        # ── 标题栏 ──
        header = tk.Frame(main, bg=C["bg"])
        header.pack(fill=tk.X, pady=(0, 12))

        tk.Label(
            header, text="forge", bg=C["bg"], fg=C["text"],
            font=(FONT_FAMILY, 16, "bold"),
        ).pack(side=tk.LEFT)

        tk.Label(
            header, text=" pledge-evolving", bg=C["bg"], fg=C["muted"],
            font=(FONT_FAMILY_UI, 11),
        ).pack(side=tk.LEFT, padx=(4, 0), pady=(2, 0))

        # ── 策略选择卡片 ──
        strategy_card = tk.Frame(main, bg=C["surface"], padx=14, pady=10)
        strategy_card.pack(fill=tk.X, pady=(0, 8))

        tk.Label(
            strategy_card, text="策略", bg=C["surface"], fg=C["subtext"],
            font=(FONT_FAMILY_UI, FONT_SIZE_UI),
        ).pack(side=tk.LEFT, padx=(0, 12))

        self.strategy_var = tk.StringVar(value="balanced")
        strategies = [
            ("economy", "经济", "按成本升序，失败才升级"),
            ("balanced", "均衡", "中端主力，失败升级（推荐）"),
            ("premium", "高端", "中端草稿 → 高端裁决"),
        ]
        for val, label, tip in strategies:
            frame = tk.Frame(strategy_card, bg=C["surface"])
            frame.pack(side=tk.LEFT, padx=(0, 16))
            tk.Radiobutton(
                frame, text=label, variable=self.strategy_var, value=val,
                bg=C["surface"], fg=C["text"], selectcolor=C["overlay"],
                activebackground=C["surface"], activeforeground=C["accent"],
                font=(FONT_FAMILY_UI, FONT_SIZE_UI),
                indicatoron=True,
            ).pack(side=tk.LEFT)
            tk.Label(
                frame, text=tip, bg=C["surface"], fg=C["muted"],
                font=(FONT_FAMILY_UI, 9),
            ).pack(side=tk.LEFT, padx=(4, 0))

        # ── 任务输入 ──
        input_card = tk.Frame(main, bg=C["surface"], padx=14, pady=10)
        input_card.pack(fill=tk.X, pady=(0, 8))

        tk.Label(
            input_card, text="任务", bg=C["surface"], fg=C["subtext"],
            font=(FONT_FAMILY_UI, FONT_SIZE_UI),
        ).pack(side=tk.LEFT, padx=(0, 12))

        self.task_entry = tk.Entry(
            input_card, bg=C["input_bg"], fg=C["text"],
            insertbackground=C["accent"],
            font=(FONT_FAMILY, FONT_SIZE),
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C["border"], highlightcolor=C["accent"],
        )
        self.task_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=ENTRY_PADY)
        self.task_entry.bind("<Return>", lambda e: self._run_task())
        self._placeholder = "输入任务，如：帮我总结 README..."
        self._set_placeholder()
        self.task_entry.bind("<FocusIn>", self._on_focus_in)
        self.task_entry.bind("<FocusOut>", self._on_focus_out)
        self.task_entry.focus_set()
        self.root.bind("<Escape>", lambda e: self._on_close())
        self.root.bind("<Up>", lambda e: self._nav_history(-1))
        self.root.bind("<Down>", lambda e: self._nav_history(1))
        self.root.bind("<Control-l>", lambda e: self._clear_log())

        # ── 按钮栏 ──
        btn_bar = tk.Frame(main, bg=C["bg"])
        btn_bar.pack(fill=tk.X, pady=(0, 10))

        # 主按钮：运行
        self.run_btn = tk.Button(
            btn_bar, text="▶  运行", bg=C["accent"], fg=C["bg"],
            font=(FONT_FAMILY_UI, FONT_SIZE_BTN, "bold"),
            relief=tk.FLAT, padx=20, pady=4,
            activebackground=C["overlay"], activeforeground=C["text"],
            command=self._run_task, cursor="hand2",
        )
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._add_hover(self.run_btn, C["accent"], C["overlay"], C["bg"], C["text"])

        # 停止按钮
        self.stop_btn = tk.Button(
            btn_bar, text="■ 停止", bg=C["error"], fg=C["bg"],
            font=(FONT_FAMILY_UI, FONT_SIZE_BTN, "bold"),
            relief=tk.FLAT, padx=14, pady=4,
            activebackground=C["overlay"], activeforeground=C["text"],
            command=self._stop, state=tk.DISABLED, cursor="hand2",
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 20))
        self._add_hover(self.stop_btn, C["error"], C["overlay"], C["bg"], C["text"])

        # 辅助按钮
        for label, cmd in [("selftest", "selftest"), ("doctor", "doctor")]:
            tk.Button(
                btn_bar, text=label, bg=C["surface"], fg=C["subtext"],
                font=(FONT_FAMILY_UI, FONT_SIZE_BTN - 1),
                relief=tk.FLAT, padx=10, pady=3,
                activebackground=C["overlay"], activeforeground=C["text"],
                command=lambda c=cmd: self._run_cmd([c]), cursor="hand2",
            ).pack(side=tk.LEFT, padx=(0, 6))

        # 清空按钮（右侧）
        tk.Button(
            btn_bar, text="清空", bg=C["surface"], fg=C["muted"],
            font=(FONT_FAMILY_UI, FONT_SIZE_BTN - 1),
            relief=tk.FLAT, padx=8, pady=3,
            activebackground=C["overlay"], activeforeground=C["text"],
            command=self._clear_log, cursor="hand2",
        ).pack(side=tk.RIGHT)

        # ── 日志输出 ──
        log_frame = tk.Frame(main, bg=C["border"], padx=1, pady=1)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.log = scrolledtext.ScrolledText(
            log_frame, bg=C["log_bg"], fg=C["log_fg"],
            insertbackground=C["accent"],
            font=(FONT_FAMILY, FONT_SIZE),
            relief=tk.FLAT, wrap=tk.WORD,
            state=tk.DISABLED,
            padx=10, pady=8,
            highlightthickness=0,
        )
        self.log.pack(fill=tk.BOTH, expand=True)

        # 日志文本标签样式
        self.log.tag_configure("error", foreground=C["error"])
        self.log.tag_configure("success", foreground=C["accent2"])
        self.log.tag_configure("muted", foreground=C["muted"])
        self.log.tag_configure("accent", foreground=C["accent"])

        # ── 状态栏 ──
        status_bar = tk.Frame(main, bg=C["bg"])
        status_bar.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="就绪")
        self.status_label = tk.Label(
            status_bar, textvariable=self.status_var, bg=C["bg"],
            fg=C["muted"], font=(FONT_FAMILY_UI, 9), anchor=tk.W,
        )
        self.status_label.pack(side=tk.LEFT)

        # 版本标签 + 性能面板切换
        right_frame = tk.Frame(status_bar, bg=C["bg"])
        right_frame.pack(side=tk.RIGHT)

        self._perf_visible = False
        self.perf_btn = tk.Label(
            right_frame, text="📊 性能", bg=C["bg"], fg=C["muted"],
            font=(FONT_FAMILY_UI, 9), cursor="hand2",
        )
        self.perf_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.perf_btn.bind("<Button-1>", lambda e: self._toggle_perf())

        try:
            from forge import __version__
            ver = __version__
        except Exception:
            ver = "dev"
        tk.Label(
            right_frame, text=f"v{ver}", bg=C["bg"], fg=C["muted"],
            font=(FONT_FAMILY_UI, 9), anchor=tk.E,
        ).pack(side=tk.LEFT)

        # ── 性能面板（默认隐藏）──
        self.perf_frame = tk.Frame(main, bg=C["surface"], padx=14, pady=8)

        # 运行历史统计
        self._task_history: list[str] = []
        self._history_idx = -1
        self._run_times: list[float] = []
        self._run_tokens: list[int] = []
        self._run_costs: list[float] = []

        perf_grid = tk.Frame(self.perf_frame, bg=C["surface"])
        perf_grid.pack(fill=tk.X)

        self.perf_labels = {}
        metrics = [
            ("total_runs", "总运行", "0"),
            ("avg_time", "平均耗时", "—"),
            ("std_time", "耗时标准差", "—"),
            ("p95_time", "P95 耗时", "—"),
            ("success_rate", "成功率", "—"),
            ("total_cost", "总费用", "¥0.00"),
        ]
        for i, (key, label, default) in enumerate(metrics):
            col = i % 3
            row = i // 3
            cell = tk.Frame(perf_grid, bg=C["surface"], padx=8, pady=4)
            cell.grid(row=row, column=col, sticky="w", padx=(0, 16))
            tk.Label(cell, text=label, bg=C["surface"], fg=C["muted"],
                     font=(FONT_FAMILY_UI, 9)).pack(anchor="w")
            lbl = tk.Label(cell, text=default, bg=C["surface"], fg=C["text"],
                          font=(FONT_FAMILY, 12, "bold"))
            lbl.pack(anchor="w")
            self.perf_labels[key] = lbl

    # ── 日志 ─────────────────────────────────────────────
    def _append(self, text: str, tag: str = ""):
        self.log.configure(state=tk.NORMAL)
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log.delete("1.0", f"{line_count - MAX_LOG_LINES + 1}.0")
        self.log.insert(tk.END, text, tag)
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _clear_log(self):
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    # ── 执行 ─────────────────────────────────────────────
    def _run_task(self):
        task = self.task_entry.get().strip()
        if not task:
            messagebox.showwarning("提示", "请输入任务内容")
            return
        strategy = self.strategy_var.get()
        cmd = [sys.executable, str(RUN_PY), "run", task, "--strategy", strategy]
        self._run_cmd(cmd, full_command=True)

    def _run_cmd(self, args: list[str], full_command: bool = False):
        if self._running:
            return
        if not RUN_PY.is_file():
            messagebox.showerror("错误", f"找不到 run.py\n{RUN_PY}")
            return

        if full_command:
            cmd = args
        else:
            cmd = [sys.executable, str(RUN_PY)] + args

        self._running = True
        self.run_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._pulse_running()
        run_py_idx = next((i for i, v in enumerate(cmd) if v == str(RUN_PY)), -1)
        status_text = " ".join(cmd[run_py_idx + 1:]) if run_py_idx >= 0 else " ".join(cmd)
        self.status_var.set(f"运行中: {status_text}")

        self._clear_log()
        self._append(f"$ {' '.join(cmd)}\n\n", "muted")

        threading.Thread(target=self._exec, args=(cmd,), daemon=True).start()

    def _exec(self, cmd: list[str]):
        import time
        start = time.monotonic()
        try:
            kwargs: dict = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8-sig" if IS_WINDOWS else "utf-8",
                cwd=str(FORGE_REPO),
                bufsize=1,
            )
            if IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            self._proc = subprocess.Popen(cmd, **kwargs)

            buffer: list[str] = []
            for line in self._proc.stdout:
                buffer.append(line)
                if len(buffer) >= FLUSH_THRESHOLD:
                    self.root.after(0, self._append, "".join(buffer))
                    buffer.clear()
            if buffer:
                self.root.after(0, self._append, "".join(buffer))

            rc = self._proc.wait()
            elapsed = time.monotonic() - start
            self.root.after(0, self._record_run, elapsed, rc == 0)
            if rc == 0:
                self.root.after(0, self._append, f"\n✓ 完成 (exit {rc}, {elapsed:.1f}s)\n", "success")
            else:
                self.root.after(0, self._append, f"\n✗ 退出码: {rc} ({elapsed:.1f}s)\n", "error")
            self.root.after(0, self.status_var.set,
                           f"{'完成' if rc == 0 else '失败'} (exit {rc})")
        except Exception as e:
            self.root.after(0, self._append, f"\n✗ 错误: {e}\n", "error")
            self.root.after(0, self.status_var.set, "出错")
        finally:
            self._running = False
            self._proc = None
            self.root.after(0, lambda: self.run_btn.configure(state=tk.NORMAL))
            self.root.after(0, lambda: self.stop_btn.configure(state=tk.DISABLED))

    def _stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3)
            except ProcessLookupError:
                pass  # 进程已自行退出
            except subprocess.TimeoutExpired:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
            self._append("\n■ 已终止\n", "error")
            self.status_var.set("已停止")


    # ── Placeholder ──────────────────────────────────────
    def _set_placeholder(self):
        if not self.task_entry.get():
            self.task_entry.insert(0, self._placeholder)
            self.task_entry.configure(fg=C["muted"])

    def _on_focus_in(self, event):
        if self.task_entry.get() == self._placeholder:
            self.task_entry.delete(0, tk.END)
            self.task_entry.configure(fg=C["text"])

    def _on_focus_out(self, event):
        self._set_placeholder()

    # ── 历史记录导航 ─────────────────────────────────────
    def _nav_history(self, direction: int):
        if not self._task_history:
            return
        current = self.task_entry.get()
        if current != self._placeholder and current:
            if not self._task_history or self._task_history[-1] != current:
                self._task_history.append(current)
        if direction == -1:
            if self._history_idx < len(self._task_history) - 1:
                self._history_idx += 1
        else:
            if self._history_idx > 0:
                self._history_idx -= 1
            else:
                self._history_idx = -1
                self.task_entry.delete(0, tk.END)
                self._set_placeholder()
                return
        idx = len(self._task_history) - 1 - self._history_idx
        self.task_entry.delete(0, tk.END)
        self.task_entry.insert(0, self._task_history[idx])
        self.task_entry.configure(fg=C["text"])

    # ── 运行状态脉冲 ─────────────────────────────────────
    def _pulse_running(self):
        if not self._running:
            return
        current = self.status_var.get()
        if current.endswith(" ●"):
            self.status_var.set(current[:-2])
        else:
            self.status_var.set(current + " ●")
        self.root.after(800, self._pulse_running)

    def _on_close(self):
        """关闭窗口时停止子进程，避免孤儿。"""
        if self._running and self._proc:
            self._stop()
        self.root.destroy()


def main():
    _setup_dpi()
    root = tk.Tk()
    app = ForgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
