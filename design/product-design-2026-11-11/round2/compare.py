"""Combine captured native windows at identical scales for visual QA."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
font = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 22)
before = Image.open(HERE / 'before.png').convert('RGB')
after = Image.open(HERE / 'after.png').convert('RGB')
assert before.size == after.size == (2162, 1398)


def pair(name, box=None, scale=1):
    images = [im.crop(box) if box else im for im in (before, after)]
    width, height = images[0].size
    width, height = round(width * scale), round(height * scale)
    canvas = Image.new('RGB', (width * 2 + 24, height + 58), '#101114')
    draw = ImageDraw.Draw(canvas)
    for index, (im, label) in enumerate(zip(images, ('Before this iteration', 'After: structure + icons'))):
        left = index * (width + 24)
        draw.text((left + 12, 14), label, font=font, fill='#e9eaed')
        canvas.paste(im.resize((width, height), Image.Resampling.LANCZOS), (left, 58))
    canvas.save(HERE / name)


pair('comparison.png', scale=.5)
pair('sidebar-comparison.png', (0, 122, 362, 1354))
pair('composer-comparison.png', (634, 975, 1584, 1354))
