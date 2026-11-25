"""Editable Office documents built from structured content supplied in the conversation."""
import re
import unicodedata

from .math_tools import number


def text(value, limit=4000, *, allow_blank=False):
    if not isinstance(value, str) or (not allow_blank and not value.strip()) or len(value) > limit:
        raise ValueError(f'请提供 1 至 {limit} 字符的文本。')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError('文本含无效控制字符。')
    return value


def items(value, maximum, label, minimum=1):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f'{label}应包含 {minimum} 至 {maximum} 项。')
    return value


def write_spreadsheet(args, dest):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    book.remove(book.active)
    seen = set()
    for sheet in items(args['sheets'], 12, '工作表'):
        name = text(sheet['name'], 31)
        if re.search(r'[\\/*?:\[\]]', name) or name.startswith("'") or name.endswith("'") or name.casefold() in seen:
            raise ValueError('工作表名必须有效且唯一。')
        seen.add(name.casefold())
        ws = book.create_sheet(name)
        headers = [text(h, 160) for h in items(sheet['headers'], 50, '表头')]
        rows = items(sheet['rows'], 2000, '数据行', 0)
        widths = [0] * len(headers)
        for r, values in enumerate([headers, *rows], 1):
            if not isinstance(values, list) or len(values) != len(headers):
                raise ValueError(f'工作表「{name}」第 {r} 行的列数与表头不一致。')
            for c, value in enumerate(values, 1):
                cell = ws.cell(r, c)
                if isinstance(value, str):
                    cell.value = text(value, allow_blank=True)
                    cell.data_type = 's'  # Supplied text stays literal, including leading "=".
                elif value is None or isinstance(value, bool):
                    cell.value = value
                else:
                    number(value)
                    cell.value = value
                width = sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in str(value or '')[:64])
                widths[c - 1] = max(widths[c - 1], width)
                if r == 1:
                    cell.font = Font(name='Calibri', color='FFFFFF', bold=True)
                    cell.fill = PatternFill('solid', fgColor='265578')
                    cell.alignment = Alignment(vertical='center')
        for c, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = min(56, max(12, width + 3))
        ws.row_dimensions[1].height = 24
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
    book.save(dest)


def write_document(args, dest):
    from docx import Document

    document = Document()
    document.core_properties.title = text(args['title'], 120)
    document.add_heading(args['title'], 0)
    for section in items(args['sections'], 60, '章节'):
        if not isinstance(section, dict):
            raise ValueError('章节需要包含 paragraphs 或 bullets 字段。')
        if 'heading' in section:
            document.add_heading(text(section['heading'], 180), 1)
        paragraphs = items(section.get('paragraphs', []), 100, '正文段落', 0)
        bullets = items(section.get('bullets', []), 50, '项目符号', 0)
        if not paragraphs and not bullets:
            raise ValueError('每个章节至少需要一段正文或一个列表项。')
        for paragraph in paragraphs:
            document.add_paragraph(text(paragraph))
        for bullet in bullets:
            document.add_paragraph(text(bullet), style='List Bullet')
    document.save(dest)


def write_slides(args, dest):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_AUTO_SIZE
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    deck.core_properties.title = text(args['title'], 120)
    cover = deck.slides.add_slide(deck.slide_layouts[0])
    cover.shapes.title.width = Inches(11.8)
    cover.shapes.title.text = args['title']
    cover.shapes.title.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    if 'subtitle' in args:
        subtitle = cover.placeholders[1]
        subtitle.width = Inches(11.8)
        subtitle.text = text(args['subtitle'], 240)
    for row in items(args['slides'], 60, '内容页'):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        heading = slide.shapes.title
        heading.width = Inches(11.8)
        heading.text = text(row['title'], 120)
        heading.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        heading.text_frame.paragraphs[0].font.color.rgb = RGBColor.from_string('265578')
        body = slide.placeholders[1]
        body.width, body.height = Inches(11.8), Inches(5.4)
        frame = body.text_frame
        frame.word_wrap = True
        frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        for i, bullet in enumerate(items(row['bullets'], 8, '每页要点')):
            p = frame.add_paragraph() if i else frame.paragraphs[0]
            p.text = text(bullet, 220)
            p.font.size, p.space_after = Pt(23), Pt(12)
    deck.save(dest)
