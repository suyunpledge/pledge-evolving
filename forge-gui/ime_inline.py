"""Windows 下的 IME 行内组合（inline composition）支持。

问题根因（tkWinX.c，Tk 8.6）：Tk 的 `HandleIMEComposition` 只处理
`GCS_RESULTSTR`（已确认文本），对 `GCS_COMPSTR`（组合中的拼音）直接
return 0 → 走 DefWindowProc → IMM 弹出**独立的默认组合浮层**，与输入框
视觉脱离。Tk 从设计上就不渲染 preedit，也不给组合串定位。

本模块用 ctypes 子类化 Text 控件的窗口过程（SetWindowLongPtrW）：
- `WM_IME_STARTCOMPOSITION`：自行处理（返回已处理，不进 DefWindowProc，
  从源头抑制默认组合浮层的创建）。
- `WM_IME_COMPOSITION` + `GCS_COMPSTR`：读组合串，用下划线 tag 行内
  渲染在光标处；返回已处理。
- `WM_IME_COMPOSITION` + `GCS_RESULTSTR`：先清掉行内 preedit，再交还
  Tk 原窗口过程（确认文本的插入仍走 Tk 既有路径，行为不变）。
- `WM_IME_ENDCOMPOSITION`：清 preedit 后交还原过程。
- `WM_IME_SETCONTEXT`：剥掉 ISC_SHOWUICOMPOSITIONWINDOW 位再进
  DefWindowProc——默认组合 UI 不显示，**候选词窗口保持系统原样**
  （ISC_SHOWUICANDIDATEWINDOW 位不动，WM_IME_NOTIFY 全部透传）。
- 其余消息：原样 CallWindowProcW 给 Tk，零逻辑。

约束与兜底：
- 非 Windows / ctypes 失败 / 子类化失败 → 整体 no-op，绝不破坏输入。
- 回调全程 try/except；detached 或控件销毁后回退 DefWindowProcW。
- preedit 是纯显示层：写入 Text 用 tag 标记，send_var 同步被
  composing 标志挡住，**未确认拼音永远进不了发送内容**。
"""
from __future__ import annotations

import ctypes
import sys

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None

# ── Win32 常量 ──────────────────────────────────────────────────
WM_IME_STARTCOMPOSITION = 0x0107
WM_IME_ENDCOMPOSITION = 0x0108
WM_IME_COMPOSITION = 0x010E
WM_IME_SETCONTEXT = 0x0281
GCS_COMPSTR = 0x0008
GCS_RESULTSTR = 0x0800
ISC_SHOWUICOMPOSITIONWINDOW = 0x00000002
GWL_WNDPROC = -4
CFS_POINT = 0x0002

_TAG = "ime-preedit"


def _diag_path():
    """诊断日志路径：FORGE_IME_LOG 优先；否则 ~/.forge/gui/ime-diag.log。"""
    import os
    env = os.environ.get("FORGE_IME_LOG")
    if env:
        return env
    try:
        base = os.path.join(os.path.expanduser("~"), ".forge", "gui")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "ime-diag.log")
    except OSError:
        return None


def _dbg(line: str):
    """常驻诊断：只记消息类型/标志，绝不记文本内容；文件封顶 64KB。"""
    path = _diag_path()
    if not path:
        return
    try:
        import os
        if os.path.exists(path) and os.path.getsize(path) > 65536:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("(truncated)\n")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class InlineIME:
    """把一个 Tk Text 控件的 IME 组合串改为行内显示。

    用法：`InlineIME(text_widget)`；读 `.composing` 判断是否在组合中
    （回车/发送前检查，避免把组合中的拼音当正文发出去）。
    """

    def __init__(self, text_widget):
        self._w = text_widget
        self.composing = False
        self._preedit = ""
        self._start_index = None
        self._proc_ref = None      # 强引用：回调被 GC 会直接崩进程
        self._orig_proc = 0
        self._hwnd = None
        self._detached = False
        self._supported = sys.platform.startswith("win32") and tk is not None
        self._top_hwnd = None
        self._top_orig = 0
        self._top_proc = None
        if not self._supported:
            return
        try:
            self._install()
            self._install_toplevel()
        except Exception:
            self._supported = False

    # ── 安装 / 卸载 ─────────────────────────────────────────────

    def _install(self):
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        imm32 = ctypes.WinDLL("imm32", use_last_error=True)
        self._user32 = user32
        self._imm32 = imm32

        set_ptr = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        set_ptr.restype = ctypes.c_ssize_t
        set_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_size_t,
                                           ctypes.c_ssize_t]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_size_t, ctypes.c_ssize_t]
        imm32.ImmGetContext.restype = ctypes.c_void_p
        imm32.ImmGetContext.argtypes = [ctypes.c_void_p]
        imm32.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        imm32.ImmGetCompositionStringW.restype = ctypes.c_long
        imm32.ImmGetCompositionStringW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                   ctypes.c_void_p, ctypes.c_uint]
        imm32.ImmSetCompositionWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        imm32.ImmSetCandidateWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._set_ptr = set_ptr

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class COMPOSITIONFORM(ctypes.Structure):
            _fields_ = [("dwStyle", wintypes.DWORD), ("ptCurrentPos", POINT),
                        ("rcArea", ctypes.c_long * 4)]

        class CANDIDATEFORM(ctypes.Structure):
            _fields_ = [("dwIndex", wintypes.DWORD), ("dwStyle", wintypes.DWORD),
                        ("ptCurrentPos", POINT), ("rcArea", ctypes.c_long * 4)]

        self._COMPOSITIONFORM = COMPOSITIONFORM
        self._CANDIDATEFORM = CANDIDATEFORM

        wndproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_size_t,
                                       ctypes.c_ssize_t)
        proc = wndproc_t(self._wndproc)
        hwnd = ctypes.c_void_p(int(self._w.winfo_id()))
        orig = set_ptr(hwnd, GWL_WNDPROC,
                       ctypes.cast(proc, ctypes.c_void_p).value)
        if not orig:
            raise OSError("SetWindowLongPtrW(GWL_WNDPROC) failed")
        self._wndproc_type = wndproc_t
        self._proc_ref = proc
        self._orig_proc = orig
        self._hwnd = hwnd
        self._tag_ready = False
        self._w.bind("<Destroy>", self._on_destroy, add="+")

    def _install_toplevel(self):
        """顶层窗口也挂一份钩子：部分 IME 把 WM_IME_SETCONTEXT 发给顶层窗口；
        这里只抑制组合 UI 位，其余消息原样透传（不抢 Tk 的窗口管理）。"""
        try:
            top = self._w.winfo_toplevel()
            hwnd = ctypes.c_void_p(int(top.winfo_id()))
            proc = self._wndproc_type(self._top_wndproc)
            orig = self._set_ptr(hwnd, GWL_WNDPROC,
                                 ctypes.cast(proc, ctypes.c_void_p).value)
            if not orig:
                return
            self._top_hwnd = hwnd
            self._top_orig = orig
            self._top_proc = proc
        except Exception:
            self._top_hwnd = None

    def _top_wndproc(self, hwnd, msg, wParam, lParam):
        try:
            if msg == WM_IME_SETCONTEXT:
                return self._user32.DefWindowProcW(
                    hwnd, msg, wParam,
                    lParam & ~ISC_SHOWUICOMPOSITIONWINDOW)
        except Exception:
            pass
        if self._top_orig:
            return self._user32.CallWindowProcW(self._top_orig, hwnd, msg,
                                                wParam, lParam)
        return self._user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    def detach(self):
        """还原窗口过程（幂等；销毁/退出前调用）。"""
        if self._detached or not self._supported or not self._orig_proc:
            return
        try:
            self._set_ptr(self._hwnd, GWL_WNDPROC, self._orig_proc)
        except Exception:
            pass
        if self._top_hwnd is not None and self._top_orig:
            try:
                self._set_ptr(self._top_hwnd, GWL_WNDPROC, self._top_orig)
            except Exception:
                pass
            self._top_hwnd = None
            self._top_orig = 0
        self._orig_proc = 0
        self._detached = True
        self._clear_preedit()

    def _on_destroy(self, event=None):
        if event is not None and event.widget is not self._w:
            return
        self.detach()

    def _call_orig(self, hwnd, msg, wParam, lParam):
        if self._orig_proc:
            return self._user32.CallWindowProcW(self._orig_proc, hwnd, msg,
                                                wParam, lParam)
        return self._user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    # ── 窗口过程 ────────────────────────────────────────────────

    def _wndproc(self, hwnd, msg, wParam, lParam):
        try:
            if msg in (0x0107, 0x0108, 0x010E, 0x0281, 0x0100, 0x0101, 0x0102, 0x0109, 0x0286):
                _dbg(f"msg=0x{msg:04X} wParam={wParam} lParam=0x{lParam & 0xFFFFFFFF:X}")
        except Exception:
            pass
        try:
            if msg == WM_IME_COMPOSITION:
                if lParam & GCS_COMPSTR:
                    self._on_compstr(hwnd)
                    return 1                     # 已处理：不进 DefWindowProc
                if lParam & GCS_RESULTSTR:
                    # 确认文本：先撤 preedit，再交还 Tk（插入走 Tk 原路径）
                    self._clear_preedit()
                    return self._call_orig(hwnd, msg, wParam, lParam)
                return self._call_orig(hwnd, msg, wParam, lParam)
            if msg == WM_IME_STARTCOMPOSITION:
                self._begin()
                return 1                         # 抑制默认组合浮层
            if msg == WM_IME_ENDCOMPOSITION:
                self._clear_preedit()
                return self._call_orig(hwnd, msg, wParam, lParam)
            if msg == WM_IME_SETCONTEXT:
                # 候选窗口 UI 保留，默认组合 UI 位剥掉
                return self._user32.DefWindowProcW(
                    hwnd, msg, wParam,
                    lParam & ~ISC_SHOWUICOMPOSITIONWINDOW)
        except Exception:
            pass
        return self._call_orig(hwnd, msg, wParam, lParam)

    def _on_compstr(self, hwnd):
        s = ""
        try:
            hIMC = self._imm32.ImmGetContext(hwnd)
            if hIMC:
                try:
                    n = self._imm32.ImmGetCompositionStringW(
                        hIMC, GCS_COMPSTR, None, 0)
                    if n and n > 0:
                        buf = ctypes.create_unicode_buffer(n // 2 + 1)
                        got = self._imm32.ImmGetCompositionStringW(
                            hIMC, GCS_COMPSTR, buf, n)
                        if got and got > 0:
                            s = buf.value[: got // 2]
                finally:
                    self._imm32.ImmReleaseContext(hwnd, hIMC)
        except Exception:
            s = ""
        self._apply_preedit(s)

    # ── preedit 渲染（纯 Tk，可独立测试）────────────────────────

    def _begin(self):
        if not self.composing:
            try:
                self._start_index = self._w.index("insert")
            except Exception:
                self._start_index = None
            self.composing = True

    def _ensure_tag(self):
        if not self._tag_ready:
            try:
                self._w.tag_configure(_TAG, underline=True)
                self._tag_ready = True
            except Exception:
                pass

    def _apply_preedit(self, s: str):
        """把组合串画在起始光标处；重复调用整体替换（不累积）。"""
        s = str(s or "")
        if not self.composing:
            self._begin()          # 有些 IME 不发 STARTCOMPOSITION
        if self._start_index is None:
            return
        try:
            if self._preedit:
                self._w.delete(self._start_index,
                               f"{self._start_index}+{len(self._preedit)}c")
            if s:
                self._ensure_tag()
                self._w.insert(self._start_index, s, _TAG)
            self._preedit = s
            self._w.mark_set("insert", f"{self._start_index}+{len(s)}c")
            self._w.edit_modified(False)   # preedit 不进 send_var
            self._w.see("insert")
        except Exception:
            return
        self._anchor_ime_ui()

    def _clear_preedit(self):
        if not self.composing and not self._preedit:
            self._start_index = None
            return
        try:
            if self._preedit and self._start_index is not None:
                self._w.delete(self._start_index,
                               f"{self._start_index}+{len(self._preedit)}c")
                self._w.mark_set("insert", self._start_index)
                self._w.edit_modified(False)
        except Exception:
            pass
        self._preedit = ""
        self.composing = False
        self._start_index = None

    def _anchor_ime_ui(self):
        """把组合/候选窗口的参考点钉在 preedit 起点（屏幕坐标）。"""
        try:
            dline = self._w.dlineinfo(self._start_index)
            if not dline:
                return
            ix, iy, iw, ih, base = dline
            x = self._w.winfo_rootx() + ix
            y = self._w.winfo_rooty() + iy + ih
            hIMC = self._imm32.ImmGetContext(self._hwnd)
            if not hIMC:
                return
            try:
                cf = self._COMPOSITIONFORM()
                cf.dwStyle = CFS_POINT
                cf.ptCurrentPos.x, cf.ptCurrentPos.y = x, y
                self._imm32.ImmSetCompositionWindow(hIMC, ctypes.byref(cf))
                cand = self._CANDIDATEFORM()
                cand.dwIndex = 0
                cand.dwStyle = CFS_POINT
                cand.ptCurrentPos.x, cand.ptCurrentPos.y = x, y
                self._imm32.ImmSetCandidateWindow(hIMC, ctypes.byref(cand))
            finally:
                self._imm32.ImmReleaseContext(self._hwnd, hIMC)
        except Exception:
            pass

    # ── 对外辅助 ────────────────────────────────────────────────

    def cancel_if_composing(self):
        """发送/清空输入框前调用：组合中则丢弃 preedit（不发送拼音）。"""
        if self.composing or self._preedit:
            self._clear_preedit()
