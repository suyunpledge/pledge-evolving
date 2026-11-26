"""从品牌原图生成图标与界面标志（可重复执行）。

输入：品牌原图——已画好的圆角磁贴（Revon 极光狐），四周纯黑底。
      显式传入外部原图时，抠掉黑底、裁成正方形后存为 desktop/revon-logo.png（母版）。
输出：
    desktop/revon.ico                     打包用应用图标（磁贴本身，圆角外透明，多尺寸）
    desktop/revon.png                     256px 预览
    ../assets/revon-logo-{52,78,104}.png  界面用标志（同一磁贴，供 tk.PhotoImage / QML）
    ../../vscode-extension/media/revon-logo.png  VS Code 扩展图标（256px）

界面里的 brand-mark / brand-hero 由 make_ui_assets.py 从同一母版派生。

若原图不存在，回退到程序化绘制的 Indigo「F」图标（保持老行为）。

用法：
    python make_icon.py                 # 用 desktop/revon-logo.png（若存在）
    python make_icon.py <原图路径>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
DEFAULT_SOURCE = HERE / "revon-logo.png"

ICO_SIZES = (256, 128, 64, 48, 32, 24, 16)
UI_SIZES = (52, 78, 104)

ACCENT = (79, 70, 229, 255)          # #4F46E5（回退图标）
ACCENT_LIGHT = (109, 99, 240, 255)   # #6D63F0
VSCODE_ICON = HERE.parent.parent / "vscode-extension" / "media" / "revon-logo.png"

# 从四角泛洪抠黑底：亮度 <= BG_THRESHOLD 且与图边连通的像素视为背景。
# 只抠与边缘连通的黑，磁贴内部的深色夜空不受影响。
BG_THRESHOLD = 14


# ─── 回退：程序化 Indigo「F」 ─────────────────────────────


def _font(size: int):
    for name in ("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        for root in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"):
            path = Path(root) / name
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError:
                    continue
    return ImageFont.load_default()


def render_fallback(size: int) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(s * 0.22)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=ACCENT)
    hl = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hl).polygon([(0, 0), (int(s * 0.72), 0), (0, int(s * 0.72))],
                               fill=ACCENT_LIGHT)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius,
                                           fill=255)
    img.paste(Image.alpha_composite(img, hl), (0, 0), mask)
    font = _font(int(s * 0.60))
    box = d.textbbox((0, 0), "F", font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((s - tw) / 2 - box[0], (s - th) / 2 - box[1]), "F", font=font,
           fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


# ─── 品牌原图处理 ─────────────────────────────────────────


def tile_master(raw: Image.Image) -> Image.Image:
    """黑底上的圆角磁贴 → 圆角外透明的正方形母版。"""
    rgb = raw.convert("RGB")
    w, h = rgb.size
    # 泛洪标记：先把亮度图二值化（背景候选=0），再从四角把连通的 0 涂成 128。
    lum = rgb.convert("L").point(lambda v: 0 if v <= BG_THRESHOLD else 255)
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if lum.getpixel(corner) == 0:
            ImageDraw.floodfill(lum, corner, 128)
    alpha = lum.point(lambda v: 0 if v == 128 else 255)
    # 边缘羽化（约 0.1% 边长），避免缩小后锯齿
    alpha = alpha.filter(ImageFilter.GaussianBlur(max(1.0, w / 900)))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 16 else 0).getbbox()
    if box is None:
        raise ValueError("原图里找不到磁贴（全是黑底？）")
    out = out.crop(box)
    side = max(out.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(out, ((side - out.width) // 2, (side - out.height) // 2))
    return square


def scaled(master: Image.Image, size: int) -> Image.Image:
    """缩放到目标尺寸；小尺寸补一点锐化，免得插画糊成一团。"""
    out = master.resize((size, size), Image.LANCZOS)
    if size <= 64:
        out = out.filter(ImageFilter.UnsharpMask(radius=0.6, percent=60, threshold=2))
    return out


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    # 防降质：输入必须是原始位图，绝不能拿生成出来的 .ico 当源
    if src.suffix.lower() == ".ico":
        print(f"拒绝：{src.name} 是生成产物，不能当输入图。"
              f"请传原始品牌图，或省略参数使用 {DEFAULT_SOURCE.name}")
        return 2
    out_ico = HERE / "revon.ico"
    preview = HERE / "revon.png"

    if not src.is_file():
        print(f"no brand source at {src}; 回退到程序化 Indigo「F」")
        frames = [render_fallback(n) for n in ICO_SIZES]
        frames[0].save(out_ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                       append_images=frames[1:])
        frames[0].save(preview)
        print(f"icon   : {out_ico}  ({out_ico.stat().st_size} bytes)")
        return 0

    raw = Image.open(src)
    print(f"source : {src}  ({raw.size[0]}x{raw.size[1]})")
    if src.resolve() == DEFAULT_SOURCE.resolve():
        # 母版已是处理后的透明磁贴：直接用，不再二次抠图
        master = raw.convert("RGBA")
    else:
        master = tile_master(raw)
        master.save(DEFAULT_SOURCE, optimize=True)
        print(f"master : {DEFAULT_SOURCE}  ({master.size[0]}x{master.size[1]})")

    frames = [scaled(master, n) for n in ICO_SIZES]
    frames[0].save(out_ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                   append_images=frames[1:])
    print(f"icon   : {out_ico}  ({out_ico.stat().st_size} bytes, {len(ICO_SIZES)} sizes)")
    frames[0].save(preview)
    print(f"preview: {preview}")

    ASSETS.mkdir(parents=True, exist_ok=True)
    for n in UI_SIZES:
        out = ASSETS / f"revon-logo-{n}.png"
        scaled(master, n).save(out, optimize=True)
        print(f"ui     : {out}  ({out.stat().st_size} bytes)")
    if VSCODE_ICON.parent.is_dir():
        scaled(master, 256).save(VSCODE_ICON, optimize=True)
        print(f"vscode : {VSCODE_ICON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
