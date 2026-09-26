"""从品牌原图生成图标与界面标志（可重复执行）。

输入：desktop/forge-logo.png（品牌原图，正方形，黑底）
输出：
    desktop/forge.ico                     打包用应用图标（黑底圆角磁贴 + 居中标志，多尺寸）
    ../assets/forge-logo-{52,78,104}.png  界面用标志（黑底抠透明，供 tk.PhotoImage）

若原图不存在，回退到程序化绘制的 Indigo「F」图标（保持老行为）。

用法：
    python make_icon.py                 # 用 desktop/forge-logo.png（若存在）
    python make_icon.py <原图路径>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
DEFAULT_SOURCE = HERE / "forge-logo.png"

ICO_SIZES = (256, 128, 64, 48, 32, 24, 16)
UI_SIZES = (52, 78, 104)

ACCENT = (79, 70, 229, 255)          # #4F46E5（回退图标）
ACCENT_LIGHT = (109, 99, 240, 255)   # #6D63F0
TILE_BG = (0, 0, 0, 255)             # 原图底色：纯黑

# 亮度抠图阈值：<=BG_KEEP 视为背景（全透明），>=BG_CLEAR 视为实体（全不透明）
BG_KEEP = 8
BG_CLEAR = 38


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


def content_box(im: Image.Image, thr: int = 20):
    gray = im.convert("L")
    return gray.point(lambda v: 255 if v > thr else 0).getbbox()


def square_crop(im: Image.Image, pad_ratio: float = 0.04) -> Image.Image:
    """按内容包围盒裁成正方形（居中），四周留 pad_ratio 边距。"""
    box = content_box(im)
    if box is None:
        return im
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    side = max(x1 - x0, y1 - y0) * (1 + pad_ratio * 2)
    half = side / 2
    left, top = int(round(cx - half)), int(round(cy - half))
    right, bottom = int(round(cx + half)), int(round(cy + half))
    out = Image.new("RGBA", (int(round(side)), int(round(side))), (0, 0, 0, 0))
    out.paste(im.crop((left, top, right, bottom)), (0, 0))
    return out


def key_out_background(im: Image.Image) -> Image.Image:
    """把纯黑底抠成透明（亮度越低越透明；深灰金属受影响很小，且界面底色也近黑）。"""
    out = im.convert("RGBA")
    gray = out.convert("L")
    span = max(1, BG_CLEAR - BG_KEEP)

    def alpha(v: int) -> int:
        if v <= BG_KEEP:
            return 0
        if v >= BG_CLEAR:
            return 255
        return int((v - BG_KEEP) * 255 / span)

    alpha_ch = gray.point(alpha)
    out.putalpha(alpha_ch)
    return out


def make_icon(master: Image.Image, size: int) -> Image.Image:
    """黑底圆角磁贴 + 居中标志（保留原图设计里的黑底观感）。"""
    scale = 4
    s = size * scale
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, s - 1, s - 1),
                                           radius=int(s * 0.22), fill=TILE_BG)
    inner = int(s * 0.80)
    logo = master.resize((inner, inner), Image.LANCZOS)
    off = (s - inner) // 2
    tile.alpha_composite(logo, (off, off))
    return tile.resize((size, size), Image.LANCZOS)


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    use_brand = src.is_file()

    if use_brand:
        raw = Image.open(src).convert("RGBA")
        master = square_crop(raw)
        print(f"source : {src}  ({raw.size[0]}x{raw.size[1]})")
        print(f"cropped: {master.size[0]}x{master.size[1]}  -> {DEFAULT_SOURCE.name}")
        if src.resolve() != DEFAULT_SOURCE.resolve():
            master.save(DEFAULT_SOURCE)
            print(f"  saved master: {DEFAULT_SOURCE}")

        out_ico = HERE / "forge.ico"
        frames = [make_icon(master, n) for n in ICO_SIZES]
        frames[0].save(out_ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                       append_images=frames[1:])
        print(f"icon   : {out_ico}  ({out_ico.stat().st_size} bytes, "
              f"{len(ICO_SIZES)} sizes)")

        ASSETS.mkdir(parents=True, exist_ok=True)
        keyed = key_out_background(master)
        for n in UI_SIZES:
            out = ASSETS / f"forge-logo-{n}.png"
            keyed.resize((n, n), Image.LANCZOS).save(out)
            print(f"ui     : {out}  ({out.stat().st_size} bytes)")
    else:
        print(f"no brand source at {src}; 回退到程序化 Indigo「F」")
        out_ico = HERE / "forge.ico"
        frames = [render_fallback(n) for n in ICO_SIZES]
        frames[0].save(out_ico, format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                       append_images=frames[1:])
        print(f"icon   : {out_ico}  ({out_ico.stat().st_size} bytes)")

    preview = HERE / "forge.png"
    (frames[0] if use_brand else render_fallback(256)).save(preview)
    print(f"preview: {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
