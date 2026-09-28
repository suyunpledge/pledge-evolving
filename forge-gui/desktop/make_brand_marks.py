# -*- coding: utf-8 -*-
"""构建期生成模型品牌标志位图（assets/brands/）。

输入：``assets/brands-src/<icon>.png`` —— LobeHub icons 的 dark 变体
（白色字形 + 透明底，640×640），随仓库一起提交，保证离线可重建。

输出：``assets/brands/<brand-key>-{16,20,24,32}.png``
用品牌色给字形上色 + 统一视觉尺寸（按 alpha 包围盒归一化到画布 84%）。

运行时**不 import Pillow**：GUI 只做 ``tk.PhotoImage`` + ``subsample``。

用法::

    python desktop/make_brand_marks.py [--check]

``--check`` 只比对输出是否已是最新，不写文件（构建脚本自检用）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 Pillow：pip install pillow\n")
    raise SystemExit(2)

GUI_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GUI_DIR))

from brand_marks import BRANDS, GENERATED_SIZES  # noqa: E402

SRC_DIR = GUI_DIR / "assets" / "brands-src"
OUT_DIR = GUI_DIR / "assets" / "brands"
GLYPH_RATIO = 0.84        # 字形最长边占画布比例（视觉重量统一）


def _hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _render(src: Path, color: str, size: int) -> Image.Image:
    img = Image.open(src).convert("RGBA")
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        raise ValueError("空图（全透明）：%s" % src)
    glyph = alpha.crop(bbox)
    longest = max(glyph.size)
    target = max(1, int(round(size * GLYPH_RATIO)))
    scale = target / longest
    new_size = (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale)))
    glyph = glyph.resize(new_size, Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    mask.paste(glyph, ((size - new_size[0]) // 2, (size - new_size[1]) // 2))

    out = Image.new("RGBA", (size, size), _hex_rgb(color) + (0,))
    out.putalpha(mask)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    if not SRC_DIR.is_dir():
        sys.stderr.write("缺少源目录：%s\n" % SRC_DIR)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    written = skipped = missing = 0
    rows = []
    for brand in BRANDS:
        src = SRC_DIR / f"{brand.icon}.png"
        if not src.is_file():
            missing += 1
            rows.append((brand.key, brand.icon, "缺源图"))
            continue
        for size in GENERATED_SIZES:
            dst = OUT_DIR / f"{brand.key}-{size}.png"
            try:
                img = _render(src, brand.color, size)
            except Exception as exc:
                rows.append((brand.key, brand.icon, "渲染失败：%s" % exc))
                missing += 1
                break
            if args.check:
                if dst.is_file():
                    skipped += 1
                else:
                    written += 1
                continue
            img.save(dst, format="PNG", optimize=True)
            written += 1
        else:
            rows.append((brand.key, brand.icon, brand.color))

    for key, icon, note in rows:
        print("  %-10s src=%-12s %s" % (key, icon, note))
    print("\n%s：生成 %d 个文件，跳过 %d，缺源 %d"
          % ("校验" if args.check else "完成", written, skipped, missing))
    if args.check and written:
        sys.stderr.write("有 %d 个标志未生成，先运行 make_brand_marks.py\n" % written)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
