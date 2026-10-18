"""QA: 直接构造 Coding 工作台并截图，绕开主导航 deck。

用途：验证 development_mode 下欢迎页 / 底部面板 / Agent 对话区的空态
与动作卡渲染。不依赖 ForgeGuiApp 的 Work/Coding 切换（该路径由其他
Agent 并发修改中），因此本脚本的结论只覆盖 development_mode 自身。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# forge 包在仓库根（forge-gui 的上一级），补一条导入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("FORGE_INLINE_IME", "0")
os.environ.setdefault("FORGE_NO_AUTOSTART", "1")
os.environ.setdefault("FORGE_NO_TELEMETRY", "1")

import tkinter as tk
import ctypes
import win32gui
import win32ui
from PIL import Image


def grab(hwnd, path):
    rect = win32gui.GetClientRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']), bits,
                     'raw', 'BGRX', 0, 1).save(path)
    print(f"  saved {path} ({w}x{h})")
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)


def main():
    import tempfile
    from forge.development.state import ModeState, WorkMode
    from development_mode.app import DevelopmentWorkbench

    home = Path(tempfile.mkdtemp(prefix='qa-coding-')) / 'home'
    state = ModeState(home)
    state.switch(WorkMode.DEVELOPMENT)

    root = tk.Tk()
    root.title('QA Coding Workbench')
    root.geometry('1440x900+40+40')
    bench = DevelopmentWorkbench(root, state)
    bench.pack(fill='both', expand=True)

    for _ in range(10):
        root.update_idletasks()
        root.update()
        time.sleep(0.1)

    hwnd = int(root.frame(), 16)
    print("=== Coding 工作台（无项目 · 欢迎页） ===")
    grab(hwnd, 'qa_coding_welcome.png')

    # 探针：确认空态与动作卡真的建起来了
    print("\n-- 探针 --")
    cards = getattr(bench.welcome, 'action_cards', [])
    print(f"action cards: {len(cards)} -> {[c.action_key for c in cards]}")
    for c in cards:
        kids = [type(w).__name__ for w in c.winfo_children()]
        print(f"  card {c.action_key}: children={kids}")
        # inner 里的 Label 数（图标/标题/描述）
        for w in c.winfo_children():
            if isinstance(w, tk.Frame):
                labels = [type(g).__name__ for g in w.winfo_children()]
                print(f"    inner children={labels}")

    hint_tags = []
    for name, view in bench.output_views.items():
        if hasattr(view, 'tag_ranges'):
            hint_tags.append((name, bool(view.tag_ranges('empty_hint'))))
    print(f"bottom panel empty-hint per tab: {hint_tags}")

    chat_hint = bool(bench.chat.tag_ranges('empty_hint')) if hasattr(bench.chat, 'tag_ranges') else 'n/a'
    print(f"agent chat empty-hint present: {chat_hint}")

    root.destroy()


if __name__ == "__main__":
    main()
