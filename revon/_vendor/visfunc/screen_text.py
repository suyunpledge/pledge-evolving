"""Read the text on an application screen, for agents that need to find and click UI elements.

Unlike the subtitle reader, this makes no assumptions about where text sits or how it is laid out:
every line found is returned once, with its box and centre in source pixels. Pass `regions` (for
example the changed regions from `motionspec settle`) to read only part of the screen, which is
much faster because text detection dominates the cost.

The OCR engine loads once per reader; keep one reader alive (the HTTP service does) instead of
constructing one per screenshot.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Sequence
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

Box = tuple[int, int, int, int]
MAX_REGIONS = 64
LOW_CONFIDENCE = 0.8
WORD_GAP = 0.3  # A gap wider than this share of the line height starts a new word.


def _area(b: Box) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _overlap(a: Sequence[int], b: Sequence[int]) -> int:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def clip_regions(
    regions: Sequence[Sequence[int]], width: int, height: int, *, pad: int = 6, min_height: int = 32
) -> list[Box]:
    """Pad, clip and merge regions so each crop has enough context for text detection."""
    if len(regions) > MAX_REGIONS:
        raise ValueError(f"at most {MAX_REGIONS} regions")
    boxes: list[Box] = []
    for region in regions:
        if len(region) != 4:
            raise ValueError("regions must be [left, top, right, bottom]")
        left, t, r, b = (int(v) for v in region)
        if left >= r or t >= b:
            raise ValueError("regions must have positive width and height")
        left, t, r, b = left - pad, t - pad, r + pad, b + pad
        if b - t < min_height:
            grow = (min_height - (b - t) + 1) // 2
            t, b = t - grow, b + grow
        box = (max(0, left), max(0, t), min(width, r), min(height, b))
        if box[0] < box[2] and box[1] < box[3]:
            boxes.append(box)
    if not boxes:
        raise ValueError("no region lies within the image")
    merged = True
    while merged:  # Overlapping crops would read the same text twice.
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, c = boxes[i], boxes[j]
                if _overlap(a, c) > 0:
                    boxes[i] = (min(a[0], c[0]), min(a[1], c[1]), max(a[2], c[2]), max(a[3], c[3]))
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(boxes, key=lambda b: (b[1], b[0]))


def dedupe(lines: list[dict], *, contained: float = 0.6) -> list[dict]:
    """Drop lines mostly covered by a higher-scoring one: one reading per spot on screen."""
    kept: list[dict] = []
    for line in sorted(lines, key=lambda row: -row["score"]):
        box = line["bbox_px"]
        if all(
            _overlap(box, k["bbox_px"]) < contained * min(_area(box), _area(k["bbox_px"]))
            for k in kept
        ):
            kept.append(line)
    return reading_order(kept)


def reading_order(lines: list[dict]) -> list[dict]:
    """Top to bottom by visual row, then left to right within a row."""
    rows: list[list[dict]] = []
    for line in sorted(lines, key=lambda row: (row["bbox_px"][1] + row["bbox_px"][3]) / 2):
        top, bottom = line["bbox_px"][1], line["bbox_px"][3]
        centre = (top + bottom) / 2
        if rows:
            anchor = rows[-1][0]["bbox_px"]
            if abs(centre - (anchor[1] + anchor[3]) / 2) <= 0.5 * min(
                bottom - top, anchor[3] - anchor[1]
            ):
                rows[-1].append(line)
                continue
        rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda r: r["bbox_px"][0])]


def load_bgr(image: bytes | str | Path) -> Any:
    """Decode like the raster functions do: EXIF orientation applied, alpha over white."""
    from io import BytesIO

    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(BytesIO(image) if isinstance(image, bytes) else image) as source:
        if getattr(source, "n_frames", 1) != 1:
            raise ValueError("screen text needs a single-frame image")
        with ImageOps.exif_transpose(source) as oriented, oriented.convert("RGBA") as rgba:
            pixels = np.asarray(rgba)
    rgb = pixels[:, :, :3].astype(np.uint16)
    alpha = pixels[:, :, 3:4].astype(np.uint16)
    if (alpha < 255).any():
        rgb = (rgb * alpha + 255 * (255 - alpha) + 127) // 255
    return cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2BGR)


def split_words(
    chars: Sequence[str] | None, quads: Sequence | None, box: Sequence[int], x0: int
) -> list[dict]:
    """Split a line into click targets: at spaces, and at wide gaps between characters.

    OCR often reads a label and the field next to it, or two table cells, as one line. Character
    positions come from the recogniser and are approximate (a few pixels), which is enough to put
    each word's centre on the right element. Returns nothing when the line is a single word.
    """
    if not chars or not quads or len(chars) != len(quads):
        return []
    left, top, right, bottom = box
    gap = WORD_GAP * (bottom - top)
    groups: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = []
    for char, quad in zip(chars, quads, strict=True):
        xs = [float(point[0]) for point in quad]
        start, end = x0 + min(xs), x0 + max(xs)
        if char.isspace():
            if current:
                groups.append(current)
            current = []
            continue
        if current and start - current[-1][2] > gap:
            groups.append(current)
            current = []
        current.append((char, start, end))
    if current:
        groups.append(current)
    words = []
    for group in groups:
        a = max(left, round(min(c[1] for c in group)))
        b = min(right, round(max(c[2] for c in group)))
        if b > a:
            words.append(
                {
                    "text": "".join(c[0] for c in group),
                    "bbox_px": [a, top, b, bottom],
                    "center_px": [(a + b) // 2, (top + bottom) // 2],
                }
            )
    return words if len(words) > 1 else []


def _plain(text: str) -> str:
    return "".join(text.split()).casefold()


def find_text(result: dict, query: str, *, exact: bool = False) -> list[dict]:
    """Find where to click for `query` in a screen-text result, best match first.

    Matches whole lines and runs of consecutive words, ignoring spaces and case. Ranking: equal
    text before containing text, a whole line before a few words inside a longer line, the same
    letter case before a different one, then shorter and higher-scoring readings.
    """
    want = _plain(query)
    if not want:
        raise ValueError("query must contain text")
    cased = "".join(query.split())
    hits = []
    for line in result["lines"]:
        candidates = [(line["text"], line["bbox_px"], True)]
        words = line.get("words", [])
        for i in range(len(words)):
            for j in range(i + 1, len(words) + 1):
                if (i, j) != (0, len(words)):
                    span = words[i:j]
                    box = [
                        span[0]["bbox_px"][0],
                        line["bbox_px"][1],
                        span[-1]["bbox_px"][2],
                        line["bbox_px"][3],
                    ]
                    candidates.append((" ".join(w["text"] for w in span), box, False))
        for text, box, whole in candidates:
            got = _plain(text)
            if got == want or (not exact and want in got):
                hits.append(
                    {
                        "text": text,
                        "bbox_px": list(box),
                        "center_px": [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2],
                        "exact": got == want,
                        "whole_line": whole,
                        "score": line["score"],
                        "low_confidence": line["low_confidence"],
                        "_cased": "".join(text.split()) == cased,
                    }
                )
    hits.sort(
        key=lambda h: (
            not h["exact"],
            not h["whole_line"],
            not h["_cased"],
            len(_plain(h["text"])),
            -h["score"],
        )
    )
    for h in hits:
        del h["_cased"]
    return hits


def widen_regions(
    regions: Sequence[Sequence[int]], width: int, *, factor: float = 6.0, minimum: int = 120
) -> list[list[int]]:
    """Stretch changed regions sideways so a whole line of text is read, not just the part
    that changed (a status label whose value changed still starts with its unchanged name)."""
    out = []
    for left, top, right, bottom in regions:
        grow = max(minimum, round(factor * (bottom - top)))
        out.append([max(0, left - grow), top, min(width, right + grow), bottom])
    return out


def lines_touching(result: dict, regions: Sequence[Sequence[int]]) -> list[dict]:
    """Lines that overlap one of `regions` or sit on the same row as one.

    A toggled checkbox or a changed icon has its label beside it, not on it; a line counts as on
    the same row when it shares at least half its height with the region. Use it on a result
    read from `widen_regions`, which bounds how far sideways a row reaches.
    """

    def same_row(box, region):
        shared = min(box[3], region[3]) - max(box[1], region[1])
        return shared >= 0.5 * min(box[3] - box[1], region[3] - region[1])

    return [
        line
        for line in result["lines"]
        if any(
            _overlap(line["bbox_px"], r) > 0 or same_row(line["bbox_px"], r)
            for r in regions
        )
    ]


def pixel_sha256(bgr: Any) -> str:
    """Hash of the opaque RGBA bytes, equal to `RasterFunction.pixel_sha256` for the same image."""
    import cv2

    return sha256(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA).tobytes()).hexdigest()


class ScreenTextReader:
    def __init__(self, *, threads: int | None = None) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self.threads = threads or min(8, os.cpu_count() or 4)
        self.engine = RapidOCR(
            intra_op_num_threads=self.threads,
            inter_op_num_threads=1,
            det_limit_type="max",
            det_limit_side_len=1920,
        )
        self._lock = threading.Lock()

    def read(
        self, image: Any, regions: Sequence[Sequence[int]] | None = None, *, min_score: float = 0.5
    ) -> dict:
        """Read text from a BGR array or an image file. Boxes are [left, top, right, bottom]."""
        import numpy as np

        if isinstance(image, (bytes, str, Path)):
            image = load_bgr(image)
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.ndim != 3
            or image.shape[2] < 3
        ):
            raise ValueError("image must be a BGR uint8 array or an image file")
        image = np.ascontiguousarray(image[:, :, :3])
        height, width = image.shape[:2]
        crops = [(0, 0, width, height)] if not regions else clip_regions(regions, width, height)
        if sum(_area(b) for b in crops) > 0.6 * width * height:
            crops = [(0, 0, width, height)]
        start = time.perf_counter()
        lines: list[dict] = []
        with self._lock:
            for x, y, r, b in crops:
                rows, _ = self.engine(image[y:b, x:r], use_cls=False, return_word_box=True)
                for row in rows or []:
                    polygon, text, score = row[:3]
                    chars = row[4] if len(row) > 4 else None
                    quads = row[3] if len(row) > 3 else None
                    text = text.strip()
                    if not text or not (score >= min_score) or not any(c.isalnum() for c in text):
                        continue
                    xs = [p[0] for p in polygon]
                    ys = [p[1] for p in polygon]
                    box = (
                        max(x, x + int(min(xs))),
                        max(y, y + int(min(ys))),
                        min(r, x + int(max(xs)) + 1),
                        min(b, y + int(max(ys)) + 1),
                    )
                    if box[0] >= box[2] or box[1] >= box[3]:
                        continue
                    line = {
                        "text": text,
                        "bbox_px": list(box),
                        "center_px": [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2],
                        "score": round(float(score), 4),
                        "low_confidence": float(score) < LOW_CONFIDENCE,
                    }
                    words = split_words(chars, quads, box, x)
                    if words:
                        line["words"] = words
                    lines.append(line)
        return {
            "version": "svsg.screen-text/1",
            "coordinate_space": "source_pixels_xyxy",
            "size_px": [width, height],
            "pixel_sha256": pixel_sha256(image),
            "regions_px": [list(c) for c in crops],
            "lines": dedupe(lines),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            "engine": {
                "package": "rapidocr-onnxruntime",
                "version": version("rapidocr-onnxruntime"),
                "threads": self.threads,
            },
        }
