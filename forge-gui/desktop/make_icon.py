"""生成 forge 桌面应用图标（多尺寸 .ico）。

与应用内品牌一致：Indigo #4F46E5 圆角方块 + 左上更亮的高光 + 白色 F。
零额外依赖：只用 Pillow（构建期需要，运行期不需要）。

用法：
    python make_icon.py [输出路径]
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (79, 70, 229, 255)          # #4F46E5
ACCENT_LIGHT = (109, 99, 240, 255)   # #6D63F0
SIZES = (256, 128, 64, 48, 32, 24, 16)


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


def render(size: int) -> Image.Image:
    """在 4 倍画布上绘制再缩回目标尺寸，边缘更干净。"""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = int(s * 0.22)

    # 底色圆角方块
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=ACCENT)

    # 左上高光：一块更亮的圆角三角（模拟应用里的渐变）
    hl = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hl).polygon([(0, 0), (int(s * 0.72), 0), (0, int(s * 0.72))],
                               fill=ACCENT_LIGHT)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
    img.paste(Image.alpha_composite(img, hl), (0, 0), mask)

    # 白色 F
    font = _font(int(s * 0.60))
    box = d.textbbox((0, 0), "F", font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((s - tw) / 2 - box[0], (s - th) / 2 - box[1]), "F", font=font,
           fill=(255, 255, 255, 255))

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "forge.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = [render(n) for n in SIZES]
    frames[0].save(out, format="ICO",
                   sizes=[(n, n) for n in SIZES],
                   append_images=frames[1:])
    print(f"icon written: {out} ({out.stat().st_size} bytes)")
    png = out.with_suffix(".png")
    frames[0].save(png, format="PNG")
    print(f"preview written: {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
