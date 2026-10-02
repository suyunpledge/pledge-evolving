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


def icon_tile(shapes, size, color):
    scale = 4
    factor = size * scale / 24
    tile = Image.new("RGBA", (size * scale, size * scale))
    draw = ImageDraw.Draw(tile)
    width = max(1, round(1.7 * factor))
    for kind, coords in shapes:
        points = [v * factor for v in coords]
        if kind in ("line", "poly"):
            pairs = list(zip(points[::2], points[1::2]))
            if kind == "poly":
                pairs.append(pairs[0])
            draw.line(pairs, fill=color, width=width, joint="curve")
            for x, y in pairs:
                radius = width / 2
                draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color)
        elif kind == "circle":
            draw.ellipse(points, outline=color, width=width)
        else:
            draw.rounded_rectangle(points, radius=2 * factor, outline=color, width=width)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def atlas(tiles, size, path):
    output = Image.new("RGBA", (32 * size, math.ceil(len(tiles) / 32) * size))
    for index, tile in enumerate(tiles):
        output.alpha_composite(tile, ((index % 32) * size, (index // 32) * size))
    output.save(path, optimize=True)


def generate():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"icons": {name: index for index, name in enumerate(ICONS)}, "emoji": {}}
    for size in (20, 24):
        for key, color in PALETTE.items():
            atlas([icon_tile(shapes, size, color) for shapes in ICONS.values()], size,
                  OUT / f"icons-{key}-{size}.png")
    font_path = Path(os.environ.get("FORGE_EMOJI_FONT", "C:/Windows/Fonts/seguiemj.ttf"))
    if not font_path.is_file():
        if (OUT / "manifest.json").is_file():
            existing = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
            manifest["emoji"] = existing.get("emoji", {})
            (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            print("System emoji font unavailable; keeping existing emoji atlas.")
            return
        raise SystemExit("Set FORGE_EMOJI_FONT to a color emoji font, or use the checked-in UI assets.")
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
    for size in (24, 32):
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
