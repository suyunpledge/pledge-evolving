"""PowerPoint interchange for the native, editable Keystone presentation layouts."""
import json
import math
import unicodedata

from .office_slides import DEFAULT_THEME, LAYOUTS, THEMES, geometry, parse_table, slide_order


def text_size(text, width, height, preferred):
    """Reserve a line for each paragraph; PowerPoint doesn't refit until a user edits."""
    widths = []
    for line in text.split("\n"):
        widths.append(sum(1 if unicodedata.east_asian_width(c) in ("W", "F")
                          else .35 if c.isspace() else .66 for c in line))
    size = preferred
    while size > 10:
        lines = sum(max(1, math.ceil(length * size / width)) for length in widths)
        if lines * size * 1.16 <= height:
            break
        size = max(10, size - 1)
    return size


def build_presentation(fields):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.oxml.xmlchemy import OxmlElement
    from pptx.util import Pt

    def rgb(value):
        return RGBColor.from_string(value[1:])

    deck = Presentation()
    deck.slide_width, deck.slide_height = Pt(960), Pt(540)
    colors = THEMES[fields.get("slideTheme", DEFAULT_THEME)]
    for key in slide_order(fields) or ["S1"]:
        layout = fields.get(key + ".layout", "content")
        rows = parse_table(fields[key + ".table"]) if key + ".table" in fields else []
        scene = geometry(layout, bool(fields.get(key + ".right")), bool(rows))
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        background = slide.background.fill
        background.solid()
        background.fore_color.rgb = rgb(colors["background"])
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *(Pt(v) for v in scene["accent"]))
        bar.name = "Keystone.accent." + layout
        bar.fill.solid()
        bar.fill.fore_color.rgb = rgb(colors["accent"])
        bar.line.fill.background()
        for role, (x, y, w, h, size) in scene["boxes"].items():
            if role == "title":
                shape = slide.shapes.title
                shape.left, shape.top, shape.width, shape.height = map(Pt, (x, y, w, h))
            else:
                shape = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
            shape.name = "Keystone." + role
            frame = shape.text_frame
            frame.text = fields.get(key if role == "title" else key + "." + role, "")
            size = text_size(frame.text, w, h, size)
            frame.word_wrap = True
            frame.vertical_anchor = MSO_ANCHOR.TOP
            frame.margin_top = frame.margin_bottom = frame.margin_left = frame.margin_right = 0
            frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
            for p in frame.paragraphs:
                p.space_before = p.space_after = Pt(0)
                p.alignment = PP_ALIGN.LEFT
                p.line_spacing = Pt(size * 1.12)
                p.font.name = "Microsoft YaHei UI"
                p.font.size = Pt(size)
                p.font.bold = role == "title"
                p.font.color.rgb = rgb(colors["ink"])
        if rows:
            shape = slide.shapes.add_table(len(rows), len(rows[0]), *(Pt(v) for v in scene["table"]))
            shape.name = "Keystone.table"
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    cell = shape.table.cell(i, j)
                    cell.text = value
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = rgb(colors["accent"] if i == 0 else colors["background"])
                    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                    cell.margin_left = cell.margin_right = Pt(8)
                    cell.margin_top = cell.margin_bottom = Pt(1)
                    cell.text_frame.word_wrap = True
                    cell.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
                    size = text_size(value, scene["table"][2] / len(row) - 16,
                                     scene["table"][3] / len(rows) - 2, 20)
                    for p in cell.text_frame.paragraphs:
                        p.alignment = PP_ALIGN.LEFT
                        p.space_before = p.space_after = Pt(0)
                        p.line_spacing = Pt(size * 1.12)
                        p.font.name, p.font.size, p.font.bold = "Microsoft YaHei UI", Pt(size), i == 0
                        p.font.color.rgb = rgb(colors["background"] if i == 0 else colors["ink"])
        notes = fields.get(key + ".notes")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
        if fields.get(key + ".hidden") == "true":
            slide._element.set("show", "0")
        if fields.get(key + ".transition") == "fade":
            transition = OxmlElement("p:transition")
            transition.set("spd", "fast")
            transition.append(OxmlElement("p:fade"))
            slide._element.insert_element_before(transition, "p:timing", "p:extLst")
    return deck


def read_presentation(path):
    from pptx import Presentation
    from pptx.enum.dml import MSO_COLOR_TYPE, MSO_FILL_TYPE
    from pptx.enum.shapes import PP_PLACEHOLDER

    deck = Presentation(path)
    if len(deck.slides) > 999:
        raise ValueError("演示超过 999 页。")
    fields = {}
    for i, slide in enumerate(deck.slides, 1):
        key = f"S{i}"
        title = slide.shapes.title
        fields[key] = title.text if title is not None else ""
        shapes = [s for s in slide.shapes if s.has_text_frame
                  and (title is None or s.shape_id != title.shape_id)]
        layout = next((s.name.removeprefix("Keystone.accent.") for s in shapes
                       if s.name.startswith("Keystone.accent.")
                       and s.name.removeprefix("Keystone.accent.") in LAYOUTS), None)
        text = [s for s in shapes if s.text and not s.name.startswith("Keystone.accent.")]
        if layout:
            if i == 1:
                fill = slide.background.fill
                if fill.type == MSO_FILL_TYPE.SOLID and fill.fore_color.type == MSO_COLOR_TYPE.RGB:
                    bg = "#" + str(fill.fore_color.rgb)
                    for theme, palette in THEMES.items():
                        if bg == palette["background"] and theme != DEFAULT_THEME:
                            fields["slideTheme"] = theme
            right = next((s for s in text if s.name == "Keystone.right"), None)
        else:
            right = None
            layout = "content"
            if any(s.is_placeholder and s.placeholder_format.type == PP_PLACEHOLDER.SUBTITLE for s in shapes):
                layout = "cover"
            elif len(text) == 2:
                left, other = sorted(text, key=lambda s: s.left)
                if left.left + left.width <= other.left and abs(left.top - other.top) < deck.slide_height / 8:
                    layout, right = "columns", other
        if layout != "content":
            fields[key + ".layout"] = layout
        if right is not None:
            fields[key + ".right"] = right.text
        fields[key + ".body"] = "\n".join(s.text for s in text if s is not right)
        for shape in slide.shapes:
            if not shape.has_table:
                continue
            rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
            raw = json.dumps(rows, ensure_ascii=False)
            if key + ".table" not in fields:
                try:
                    parse_table(raw)
                except ValueError:
                    pass
                else:
                    fields[key + ".table"] = raw
                    continue
            fields[key + ".body"] += "\n" + "\n".join(" · ".join(row) for row in rows)
        if slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            if frame is not None and frame.text:
                fields[key + ".notes"] = frame.text
        if slide._element.get("show") in ("0", "false"):
            fields[key + ".hidden"] = "true"
        if slide._element.xpath("./p:transition/p:fade"):
            fields[key + ".transition"] = "fade"
    return fields
