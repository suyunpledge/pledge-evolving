"""test_ime_inline.py — IME 行内组合回归（Windows 子类化 + preedit 渲染 + 发送隔离）。"""
import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_widgets as cw
import ime_inline
from ime_inline import InlineIME


class PreeditRenderCase(unittest.TestCase):
    """preedit 纯渲染逻辑（不需要真 IME）。"""

    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.text = tk.Text(self.root)
        self.text.pack()
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_apply_and_replace_preedit(self):
        self.text.insert("1.0", "已有文本")
        self.text.mark_set("insert", "1.3")
        ime = InlineIME.__new__(InlineIME)   # 不装 wndproc，只测渲染
        ime._w = self.text
        ime.composing = False
        ime._preedit = ""
        ime._start_index = None
        ime._proc_ref = None
        ime._tag_ready = False
        ime._supported = True
        ime._anchor_ime_ui = lambda: None

        ime._apply_preedit("na'gan")
        self.assertTrue(ime.composing)
        self.assertEqual(self.text.get("1.3", "1.3+6c"), "na'gan")
        self.assertIn("ime-preedit", self.text.tag_names("1.4"))
        self.assertEqual(self.text.index("insert"), "1.9")
        self.assertFalse(self.text.edit_modified())   # 不进 modified → 不进 send_var

        # 整体替换不累积
        ime._apply_preedit("na'gan'ni")
        self.assertEqual(self.text.get("1.3", "1.3+9c"), "na'gan'ni")
        self.assertEqual(self.text.get("1.3+9c", "1.3+10c"), "本")  # 原插入点之后的正文

        ime._clear_preedit()
        self.assertEqual(self.text.get("1.0", "end-1c"), "已有文本")
        self.assertEqual(self.text.index("insert"), "1.3")
        self.assertFalse(ime.composing)

    def test_cancel_if_composing_drops_preedit(self):
        ime = InlineIME.__new__(InlineIME)
        ime._w = self.text
        ime.composing = False
        ime._preedit = ""
        ime._start_index = None
        ime._proc_ref = None
        ime._tag_ready = False
        ime._supported = True
        ime._anchor_ime_ui = lambda: None
        ime._apply_preedit("pin'yin")
        ime.cancel_if_composing()
        self.assertEqual(self.text.get("1.0", "end-1c"), "")
        self.assertFalse(ime.composing)


@unittest.skipUnless(sys.platform.startswith("win32"), "Windows only")
class SubclassCase(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.text = tk.Text(self.root)
        self.text.pack()
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_install_subclasses_hwnd_and_detach_restores(self):
        ime = InlineIME(self.text)
        self.assertTrue(ime._supported)
        self.assertTrue(ime._orig_proc)
        # 当前 wndproc 应是我们的回调
        user32 = ctypes.WinDLL("user32")
        get_ptr = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        get_ptr.restype = ctypes.c_ssize_t
        cur = get_ptr(ime._hwnd, ime_inline.GWL_WNDPROC)
        self.assertEqual(cur, ctypes.cast(ime._proc_ref, ctypes.c_void_p).value)
        orig = ime._orig_proc
        ime.detach()
        cur2 = get_ptr(ime._hwnd, ime_inline.GWL_WNDPROC)
        self.assertEqual(cur2, orig)
        ime.detach()   # 幂等

    def test_wndproc_dispatch_paths(self):
        ime = InlineIME(self.text)
        self.addCleanup(ime.detach)
        calls = []
        ime._on_compstr = lambda hwnd: calls.append("compstr")
        ime._call_orig = lambda *a: calls.append("orig") or 0
        # COMPSTR → 自处理，返回 1，不进 orig
        r = ime._wndproc(ime._hwnd, ime_inline.WM_IME_COMPOSITION, 0,
                         ime_inline.GCS_COMPSTR)
        self.assertEqual(r, 1)
        self.assertEqual(calls, ["compstr"])
        # RESULTSTR → 清 preedit 后进 orig
        calls.clear()
        ime._wndproc(ime._hwnd, ime_inline.WM_IME_COMPOSITION, 0,
                     ime_inline.GCS_RESULTSTR)
        self.assertIn("orig", calls)
        # STARTCOMPOSITION → 自处理返回 1
        calls.clear()
        r = ime._wndproc(ime._hwnd, ime_inline.WM_IME_STARTCOMPOSITION, 0, 0)
        self.assertEqual(r, 1)
        self.assertTrue(ime.composing)
        # ENDCOMPOSITION → 清后进 orig
        r = ime._wndproc(ime._hwnd, ime_inline.WM_IME_ENDCOMPOSITION, 0, 0)
        self.assertFalse(ime.composing)
        # SETCONTEXT → 剥组合 UI 位、留候选位
        with patch.object(ime._user32, "DefWindowProcW",
                          side_effect=lambda h, m, w, l: l) as d:
            flags = (ime_inline.ISC_SHOWUICOMPOSITIONWINDOW | 0x00000001 | 0x00000004)
            out = ime._wndproc(ime._hwnd, ime_inline.WM_IME_SETCONTEXT, 1, flags)
            self.assertFalse(out & ime_inline.ISC_SHOWUICOMPOSITIONWINDOW)
            self.assertTrue(out & 0x00000001)   # 候选窗口位保留
            self.assertTrue(out & 0x00000004)

    def test_callback_exception_never_breaks_message_flow(self):
        ime = InlineIME(self.text)
        self.addCleanup(ime.detach)
        ime._on_compstr = Mock(side_effect=RuntimeError("boom"))
        orig_calls = []
        ime._call_orig = lambda *a: (orig_calls.append(a), 0)[1]
        # 异常被吞、消息落回 Tk 原窗口过程（比中断消息流安全），不崩
        r = ime._wndproc(ime._hwnd, ime_inline.WM_IME_COMPOSITION, 0,
                         ime_inline.GCS_COMPSTR)
        self.assertEqual(r, 0)
        self.assertEqual(len(orig_calls), 1)

    def test_non_windows_is_noop(self):
        with patch.object(ime_inline.sys, "platform", "linux"):
            ime = InlineIME(self.text)
        self.assertFalse(ime._supported)
        self.assertFalse(ime.composing)
        ime._apply_preedit("x")     # no-op 不炸
        ime.detach()


class InputCardGuardCase(unittest.TestCase):
    """InputCard 集成：组合中 send_var 隔离 + 回车拦截。"""

    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.card = cw.InputCard(self.root, on_send=None)
        self.card.pack()
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_composing_blocks_send_var_sync(self):
        self.assertIsNotNone(self.card._inline_ime if sys.platform.startswith("win32") else True)
        ime = self.card._inline_ime
        if ime is None:      # 非 Windows：用假对象验证守卫逻辑
            ime = Mock(composing=True)
            self.card._inline_ime = ime
        ime.composing = True
        self.card.send_var.set("")
        self.card.entry.insert("insert", "拼音进行中")
        self.root.update()
        self.assertEqual(self.card.send_var.get(), "")   # 未同步
        ime.composing = False
        self.card.entry.edit_modified(True)
        self.card._text_changed()
        # 组合结束后同步路径恢复正常
        self.assertIn("拼音", self.card.send_var.get())

    def test_enter_during_composition_does_not_send(self):
        sent = []
        card = cw.InputCard(self.root, on_send=lambda: sent.append(1))
        card.pack()
        self.root.update()
        ime = card._inline_ime or Mock(composing=True)
        if card._inline_ime is None:
            card._inline_ime = ime
        ime.composing = True
        card.send_var.set("正文")
        ev = Mock()
        ev.state = 0
        r = card._enter(ev)
        self.assertEqual(r, "break")
        self.assertEqual(sent, [])
        self.assertFalse(ime.composing)      # cancel_if_composing 生效

    def test_fire_send_guard(self):
        sent = []
        card = cw.InputCard(self.root, on_send=lambda: sent.append(1))
        card.pack()
        self.root.update()
        card.send_var.set("正文")
        ime = card._inline_ime or Mock(composing=True)
        if card._inline_ime is None:
            card._inline_ime = ime
        ime.composing = True
        card._fire_send()
        self.assertEqual(sent, [])
        ime.composing = False
        card._fire_send()
        self.assertEqual(sent, [1])


if __name__ == "__main__":
    unittest.main()
