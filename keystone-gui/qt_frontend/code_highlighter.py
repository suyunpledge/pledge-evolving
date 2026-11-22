"""Small native QTextDocument highlighter (VS Code Dark+ palette); formatting never changes editor text."""
import re
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat


class CodeHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.rules = []
        specs = [
            # Types / PascalCase identifiers (VS Code teal)
            (r'\b[A-Z][A-Za-z0-9_]*\b', '#4ec9b0', False),
            # Function / method calls (VS Code warm yellow)
            (r'\b[A-Za-z_]\w*(?=\s*\()', '#dcdcaa', False),
            # Decorators / annotations
            (r'@[A-Za-z_]\w*', '#dcdcaa', False),
            # Declarations, storage & literals (VS Code blue)
            (r'\b(?:class|def|async|const|let|var|function|new|property|readonly|signal|required|true|false|null|undefined|None|True|False|self|this|super|void|int|real|bool|string|float|vec2|vec3|vec4|mat4|uniform|layout|in|out)\b', '#569cd6', False),
            # Control flow & imports (VS Code magenta-lavender)
            (r'\b(?:await|return|yield|if|else|elif|for|while|try|except|finally|raise|with|as|import|from|pass|break|continue|in|is|not|and|or|lambda|export|default|switch|case|throw|catch|pragma)\b', '#c586c0', False),
            # Numeric literals (VS Code soft sage)
            (r'\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b', '#b5cea8', False),
            # Strings (VS Code terracotta-peach)
            (r'"(?:[^"\\]|\\.)*"|\x27(?:[^\x27\\]|\\.)*\x27', '#ce9178', False),
            # Line comments (VS Code green, italic)
            (r'(?://|#).*$', '#6a9955', True),
        ]
        for pattern, color, italic in specs:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if italic:
                fmt.setFontItalic(True)
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
