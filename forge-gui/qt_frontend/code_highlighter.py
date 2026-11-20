"""Small native QTextDocument highlighter; formatting never changes editor text."""
import re
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat


class CodeHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.rules = []
        for pattern, color in [
            (r'\b(?:class|def|async|await|return|yield|if|else|elif|for|while|try|except|finally|raise|with|as|import|from|pass|break|continue|in|is|not|and|or|lambda|const|let|var|function|new|export|default|true|false|null|None|True|False)\b', '#c7a4e8'),
            (r'\b\d+(?:\.\d+)?\b', '#e4c48b'),
            (r'\b[A-Za-z_]\w*(?=\s*\()', '#8ac8eb'),
            (r'"(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27', '#9fd499'),
            (r'(?://|#).*$', '#7a8c84'),
        ]:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.rules.append((re.compile(pattern), fmt))

    def highlightBlock(self, text):
        if len(text) > 16000:
            return
        # Qt offsets are UTF-16; non-BMP characters occupy two code units.
        for pattern, fmt in self.rules:
            for match in pattern.finditer(text):
                start = len(text[:match.start()].encode('utf-16-le')) // 2
                length = len(match.group().encode('utf-16-le')) // 2
                self.setFormat(start, length, fmt)
