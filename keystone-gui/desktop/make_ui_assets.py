"""Generate UI/emoji atlases at build time. Runtime needs only Tk PhotoImage."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

GUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUI))
from ui_icons import ICONS, PALETTE

OUT = GUI / "assets" / "ui"


def _grad_color(color, y_frac):
    """垂直渐变：上部提亮 12%，下部压暗 10%（金属拉丝质感）。

    color 兼容 "#RRGGBB" 字符串或 (r,g,b) 元组。
    """
    if isinstance(color, str):
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    else:
        r, g, b = color[0], color[1], color[2]
    # 2026-11-08：渐变幅度减半（±6% / 5%）。20px 小图标上强渐变会让笔画发灰发脏。
    if y_frac < 0.5:
        k = 1.0 + 0.06 * (1.0 - y_frac * 2)
    else:
        k = 1.0 - 0.05 * (y_frac * 2 - 1)
    return (min(255, int(r * k)), min(255, int(g * k)), min(255, int(b * k)))


def icon_tile(shapes, size, color):
    """8x 超采样 + 逐段垂直渐变描边 + 圆头接点 + LANCZOS 缩回。

    对比旧版（4x + 平色）：边缘更平滑，笔画有上亮下暗的金属光泽。
    """
    scale = 8
    factor = size * scale / 24
    tile = Image.new("RGBA", (size * scale, size * scale))
    draw = ImageDraw.Draw(tile)
    width = max(1, round(1.75 * factor))
    total_h = size * scale
    for kind, coords in shapes:
        points = [v * factor for v in coords]
        if kind in ("line", "poly"):
            pairs = list(zip(points[::2], points[1::2]))
            if kind == "poly":
                pairs.append(pairs[0])
            for seg in range(len(pairs) - 1):
                (x0, y0), (x1, y1) = pairs[seg], pairs[seg + 1]
                mid_y = (y0 + y1) / 2 / max(1, total_h)
                seg_color = _grad_color(color, mid_y)
                draw.line([(x0, y0), (x1, y1)], fill=seg_color, width=width)
                for x, y in ((x0, y0), (x1, y1)):
                    radius = width / 2
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                                 fill=seg_color)
        elif kind == "circle":
            ys = points[1] / max(1, total_h)
            draw.ellipse(points, outline=_grad_color(color, ys), width=width)
        elif kind == "fill":
            pairs = list(zip(points[::2], points[1::2]))
            draw.polygon(pairs, fill=_grad_color(color, points[1] / max(1, total_h)))
        elif kind == "dot":
            # 实心点（「更多」三点、警告/信息的点）：此前落入 else 被画成空心圆角框。
            ys = points[1] / max(1, total_h)
            draw.ellipse(points, fill=_grad_color(color, ys))
        else:
            ys = points[1] / max(1, total_h)
            draw.rounded_rectangle(points, radius=2 * factor,
                                   outline=_grad_color(color, ys), width=width)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def brand_mark(size, *, glow=0):
    """Ember 品牌块：圆角方 + 熔铁斜向渐变 + 顶部高光 + 深色铁砧火花。

    glow > 0 时四周留出 glow 像素的暖色光晕（首页 hero 用）。Tk 无渐变/
    模糊能力，所以在构建期烘焙成 PNG，运行时只读 PhotoImage。
    """
    from PIL import ImageFilter
    scale = 4
    full = (size + glow * 2) * scale
    out = Image.new("RGBA", (full, full))
    tile_px = size * scale
    origin = glow * scale
    if glow:
        halo = Image.new("RGBA", (full, full))
        ImageDraw.Draw(halo).rounded_rectangle(
            (origin + tile_px * .12, origin + tile_px * .22, origin + tile_px * .88, origin + tile_px * 1.04),
            radius=tile_px * .3, fill=(255, 100, 40, 235))
        out.alpha_composite(halo.filter(ImageFilter.GaussianBlur(glow * scale * .38)))
    top, bottom = (255, 164, 90), (232, 84, 31)
    grad = Image.new("RGBA", (tile_px, tile_px))
    gd = ImageDraw.Draw(grad)
    for y in range(tile_px):
        for x in range(0, tile_px, 4):
            t = min(1.0, max(0.0, (x * .45 + y) / (tile_px * 1.45)))
            colour = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
            gd.line([(x, y), (x + 3, y)], fill=colour)
    mask = Image.new("L", (tile_px, tile_px))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile_px - 1, tile_px - 1), radius=tile_px * .28, fill=255)
    tile = Image.new("RGBA", (tile_px, tile_px))
    tile.paste(grad, (0, 0), mask)
    shine = Image.new("RGBA", (tile_px, tile_px))
    ImageDraw.Draw(shine).rounded_rectangle((1, 1, tile_px - 2, tile_px - 2), radius=tile_px * .28,
                                           outline=(255, 214, 178, 120), width=max(1, scale))
    tile.alpha_composite(Image.composite(shine, Image.new("RGBA", shine.size), mask))
    inset = round(size * .2)
    glyph = icon_tile(ICONS["anvil"], (size - inset * 2) * scale, (42, 18, 6))
    tile.alpha_composite(glyph, (inset * scale, inset * scale))
    out.alpha_composite(tile, (origin, origin))
    return out.resize((size + glow * 2, size + glow * 2), Image.Resampling.LANCZOS)


def atlas(tiles, size, path):
    output = Image.new("RGBA", (32 * size, math.ceil(len(tiles) / 32) * size))
    for index, tile in enumerate(tiles):
        output.alpha_composite(tile, ((index % 32) * size, (index // 32) * size))
    output.save(path, optimize=True)
    if path.name.startswith('emoji-'):
        for page, top in enumerate(range(0, output.height, 8 * size)):
            output.crop((0, top, output.width, min(output.height, top + 8 * size))).save(
                path.with_name(path.stem + f'-page-{page}.png'), optimize=True)


def generate():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"icons": {name: index for index, name in enumerate(ICONS)}, "emoji": {}}
    for size in (20, 24, 28):
        for key, color in PALETTE.items():
            atlas([icon_tile(shapes, size, color) for shapes in ICONS.values()], size,
                  OUT / f"icons-{key}-{size}.png")
    for size in (20, 24, 26, 30, 36, 52, 72, 104):
        brand_mark(size).save(OUT / f"brand-mark-{size}.png", optimize=True)
    for size in (64, 80, 96):
        brand_mark(size, glow=size // 3).save(OUT / f"brand-hero-{size}.png", optimize=True)
    if os.environ.get("KEYSTONE_BRAND_ONLY"):
        return
    font_path = Path(os.environ.get("KEYSTONE_EMOJI_FONT", "C:/Windows/Fonts/seguiemj.ttf"))
    if not font_path.is_file():
        if (OUT / "manifest.json").is_file():
            existing = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
            manifest["emoji"] = existing.get("emoji", {})
            (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            print("System emoji font unavailable; keeping existing emoji atlas.")
            return
        raise SystemExit("Set KEYSTONE_EMOJI_FONT to a color emoji font, or use the checked-in UI assets.")
    font = ImageFont.truetype(str(font_path), 96)
    def signature(text):
        mask = font.getmask(text)
        return mask.size, hashlib.sha256(bytes(mask)).digest()
    missing = signature(chr(0x10FFFF))
    supported = []
    # Keep one code point per tile. Unsupported ZWJ/skin-tone sequences remain
    # their exact text at runtime, instead of substituting a different emoji.
    points = list(range(0x1F300, 0x1FB00)) + list(range(0x2600, 0x27C0)) + [0x231A, 0x231B, 0x23F0, 0x23F3]
    for point in points:
        char = chr(point)
        if signature(char) == missing or not font.getmask(char).getbbox():
            continue
        supported.append(char)
    for size in (20, 24, 32):
        tiles = []
        for char in supported:
            scratch = Image.new("RGBA", (180, 180))
            draw = ImageDraw.Draw(scratch)
            draw.text((8, 0), char, font=font, embedded_color=True)
            bounds = scratch.getbbox()
            tile = Image.new("RGBA", (size, size))
            if bounds:
                cropped = scratch.crop(bounds)
                cropped.thumbnail((size-2, size-2), Image.Resampling.LANCZOS)
                tile.alpha_composite(cropped, ((size-cropped.width)//2, (size-cropped.height)//2))
            tiles.append(tile)
        atlas(tiles, size, OUT / f"emoji-{size}.png")
    manifest["emoji"] = {char: index for index, char in enumerate(supported)}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    print(f"Generated {len(ICONS)} UI icons and {len(supported)} emoji; assets: {OUT}")


if __name__ == "__main__":
    generate()
