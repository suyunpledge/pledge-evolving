"""实际启动 GUI，分别在 Work / Coding 下截屏。

按用户硬性要求：禁止仅靠测试断言验收，必须提供实际运行截图。
本脚本构造完整的 ForgeGuiApp（host 类），分别切换 Work / Coding
两次，对每次调用 _request_workspace 后截屏，验证顶栏 / 左侧 nav 视觉一致。

截图策略：用 win32gui.PrintWindow 直接画窗口到位图（不依赖屏幕显示
内容，alpha=0 / withdraw 也能正常抓）。
"""
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 同 test_layout_regression 的 headless 配置
import os
os.environ.setdefault("FORGE_INLINE_IME", "0")
os.environ.setdefault("FORGE_NO_AUTOSTART", "1")
os.environ.setdefault("FORGE_NO_TELEMETRY", "1")

import tkinter as tk
import ctypes
import ctypes.wintypes as wt
from PIL import Image
import win32gui
import win32ui
import win32con


def grab_window(hwnd, path):
    """用 win32 PrintWindow 把窗口画到内存位图，存 PNG。

    不依赖屏幕刷新——alpha=0 / 隐藏 / 屏幕外都能拿到窗口内容。
    """
    # 拿客户区尺寸（去掉 chrome 边）
    rect = win32gui.GetClientRect(hwnd)
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    if w < 100 or h < 100:
        # 退到窗口外框
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
    print(f"  hwnd={hwnd} size=({w},{h})")

    # 创建设备无关位图（DIB）
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)

    # PrintWindow 旗标：PW_RENDERFULLCONTENT = 2（即使窗口遮挡也画全）
    result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    print(f"  PrintWindow result={result}")

    # 位图 → PIL Image
    bmp_info = bmp.GetInfo()
    bmp_bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer('RGB',
                            (bmp_info['bmWidth'], bmp_info['bmHeight']),
                            bmp_bits, 'raw', 'BGRX', 0, 1)
    img.save(path)
    print(f"  saved {path} ({img.size[0]}x{img.size[1]})")

    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)


def main():
    # 构造 host 类
    from forge_gui_v2 import ForgeGuiApp

    root = tk.Tk()
    root.title('Forge GUI Nav Audit')
    # 不设 alpha=0 也不 withdraw——PrintWindow 用 PW_RENDERFULLCONTENT
    # 即使窗口遮挡或 alpha=0 也能抓全。但我们想让窗口被画到屏幕（以防
    # draw 异常），让 ForgeGuiApp 自己设 geometry 与 alpha（测试一致）。

    # 调小窗口到 1280x720，让 grab 更快、PNG 更小
    root.geometry('1280x720+50+50')

    # ForgeGuiApp 在 __init__ 里建好整套：_build_topbar、_build_sidebar、
    # _build_workspace（WorkspaceDeck：Work / Coding 两面）。app 自身继承自
    # ttk.Frame，但 layout 由 __init__ 内部的 body 自行 pack 满。
    app = ForgeGuiApp(root)
    # 给 Tk 时间完成初次布局
    for _ in range(8):
        root.update_idletasks()
        root.update()
        time.sleep(0.1)

    # 取 hwnd（PrintWindow 走 win32）
    hwnd = int(root.frame(), 16)
    if not hwnd:
        hwnd = int(root.winfo_id())
    print(f"hwnd = {hwnd}")

    # Step 1: Work 模式（默认）
    print("=== Work 模式 ===")
    grab_window(hwnd, 'nav_work.png')

    # Step 2: 切到 Coding
    print("=== 切到 Coding 模式 ===")
    app._request_workspace(app._workspace_kind.CODING)
    for _ in range(8):
        root.update_idletasks()
        root.update()
        time.sleep(0.1)
    grab_window(hwnd, 'nav_coding.png')

    # Step 3: 切回 Work（验证回切后顶栏/侧栏视觉稳定）
    print("=== 切回 Work 模式 ===")
    app._request_workspace(app._workspace_kind.WORK)
    for _ in range(8):
        root.update_idletasks()
        root.update()
        time.sleep(0.1)
    grab_window(hwnd, 'nav_work_return.png')

    # 关闭
    try:
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        print("ERROR:", e)
