"""Problem matchers: turn compiler/runtime output into file:line diagnostics.

Modelled on VS Code's ``problemMatcher`` contributions (``$gcc``, ``$msCompile``,
``$tsc``, ``$tsc-watch``, the Java/Kotlin/Maven matchers of the language
extensions) plus multi-line matchers for Python and Node.js tracebacks, which
VS Code only linkifies in the terminal. Each matcher is a regular expression
over one line, or a small state machine for tracebacks. Output is parsed after
the process finishes; nothing is executed here.
"""
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

LIMIT = 500
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
_SEVERITY = {'error': 'error', 'fatal error': 'error', 'e': 'error', 'err': 'error', '错误': 'error',
             'warning': 'warning', 'w': 'warning', 'warn': 'warning', '警告': 'warning',
             'info': 'info', 'note': 'info', 'i': 'info', '注意': 'info', '注': 'info'}
# Windows drive paths contain ``:``; ``(?:[A-Za-z]:)?`` keeps it in the file group.
_FILE = r'(?P<file>(?:[A-Za-z]:)?[^:\n\t"<>|]*?[^:\s\n\t"<>|])'

LINE_MATCHERS = {
    # $gcc / clang: "path:line:col: error: message" (column optional)
    'gcc': [re.compile(r'^' + _FILE + r':(?P<line>\d+):(?:(?P<col>\d+):)?\s+'
                       r'(?P<sev>fatal error|error|warning|note|错误|警告|附注)\s*[:：]\s*(?P<msg>.*)$')],
    # $msCompile: "path(line[,col]): error C1234: message", optional MSBuild "12>" prefix
    'msvc': [re.compile(r'^\s*(?:\d+>)?(?P<file>(?:[A-Za-z]:)?[^():\n][^()\n]*?)\((?P<line>\d+)(?:,(?P<col>\d+))?(?:,\d+,\d+)?\)'
                        r'\s*:\s*(?:fatal\s+)?(?P<sev>error|warning|info)\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<msg>.*)$')],
    # javac (English and Chinese locales): "Main.java:3: error: message"
    'javac': [re.compile(r'^(?P<file>(?:[A-Za-z]:)?[^:\n]*?\.java):(?P<line>\d+):\s*(?P<sev>error|warning|错误|警告)\s*[:：]\s*(?P<msg>.*)$')],
    # Maven compiler plugin: "[ERROR] /path/Main.java:[12,5] message"
    'maven': [re.compile(r'^\[(?P<sev>ERROR|WARNING|INFO)\]\s+(?P<file>(?:[A-Za-z]:)?[^\[\n]*?\.(?:java|kt|scala))'
                         r':\[(?P<line>\d+),(?P<col>\d+)\]\s*(?P<msg>.*)$')],
    # kotlinc 1.9+/2.x: "e: file:///C:/x/Main.kt:3:5 message"; older: "e: Main.kt: (3, 5): message"
    'kotlin': [re.compile(r'^(?P<sev>e|w|i|error|warning|info):\s+(?P<file>(?:file:/+)?(?:[A-Za-z]:)?[^\n]*?\.kts?)'
                          r':(?P<line>\d+):(?P<col>\d+)\s+(?P<msg>.*)$'),
               re.compile(r'^(?P<sev>e|w|i|error|warning|info):\s+(?P<file>(?:[A-Za-z]:)?[^\n]*?\.kts?):\s*'
                          r'\((?P<line>\d+),\s*(?P<col>\d+)\):\s*(?P<msg>.*)$'),
               re.compile(r'^(?P<file>(?:[A-Za-z]:)?[^:\n]*?\.kts?):(?P<line>\d+):(?P<col>\d+):\s*'
                          r'(?P<sev>error|warning|info)\s*:\s*(?P<msg>.*)$')],
    # $tsc ("file(l,c): error TS1: m") and $tsc pretty/watch ("file:l:c - error TS1: m")
    'tsc': [re.compile(r'^(?P<file>[^\s(][^()\n]*?)\((?P<line>\d+),(?P<col>\d+)\):\s+(?P<sev>error|warning|info)\s+'
                       r'(?P<code>TS\d+)\s*:\s*(?P<msg>.*)$'),
            re.compile(r'^' + _FILE + r':(?P<line>\d+):(?P<col>\d+)\s+-\s+(?P<sev>error|warning|info)\s+'
                       r'(?P<code>TS\d+)\s*:\s*(?P<msg>.*)$')],
    # pytest short summary / --tb=short frames: "tests/test_x.py:12: AssertionError"
    'pytest': [re.compile(r'^(?P<file>(?:[A-Za-z]:)?[^:\s][^:\n]*?\.py):(?P<line>\d+):\s+'
                          r'(?P<msg>[A-Za-z_][\w.]*(?:Error|Exception|Failed|Warning)\b.*)$')],
}
_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.*))?$')
_PY_EXCEPTION = re.compile(r'^(?P<name>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure)|'
                           r'[A-Za-z_][\w.]*Error)(?::\s*(?P<msg>.*))?$')
_PY_CARET = re.compile(r'^\s*\^+\s*$')
_NODE_FRAME = re.compile(r'^\s+at\s+(?:.*?\()?(?P<file>(?:file:/+)?(?:[A-Za-z]:[\\/]|/|\.{1,2}[\\/])[^()\n]*?)'
                         r':(?P<line>\d+):(?P<col>\d+)\)?\s*$')
_NODE_HEADER = re.compile(r'^(?P<file>(?:file:/+)?(?:[A-Za-z]:[\\/]|/)[^\n]*?\.(?:[cm]?js|[cm]?ts|jsx|tsx)):(?P<line>\d+)$')
_NODE_ERROR = re.compile(r'^(?:Uncaught\s+)?(?P<name>[A-Z][\w]*(?:Error|Exception))(?:\s*\[\w+\])?:\s*(?P<msg>.*)$')
_JAVA_EXCEPTION = re.compile(r'^(?:Exception in thread "[^"]*"\s+)?(?P<name>(?:[a-z_][\w$]*\.)+[A-Z][\w$]*'
                             r'(?:Exception|Error))(?::\s*(?P<msg>.*))?$')
_JAVA_FRAME = re.compile(r'^\s+at\s+[\w$.<>/]+\((?P<file>[^():]+\.(?:java|kt)):(?P<line>\d+)\)$')


def _path(raw, cwd):
    raw = raw.strip().strip('"')
    if raw.startswith('file:'):
        parsed = urlparse(raw)
        raw = unquote(parsed.path)
        if re.match(r'^/[A-Za-z]:', raw):
            raw = raw[1:]
    path = Path(raw)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def _inside(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


class _Collector:
    def __init__(self, cwd, workspace):
        self.cwd = Path(cwd)
        self.workspace = Path(workspace or cwd).resolve()
        self.rows, self.seen = [], set()

    def add(self, file, line, column, severity, message, source, code=''):
        if len(self.rows) >= LIMIT:
            return
        path = _path(file, self.cwd)
        line = max(1, int(line))
        column = max(1, int(column or 1))
        severity = _SEVERITY.get(str(severity or 'error').strip().lower(), 'error')
        message = str(message or '').strip() or code or severity
        key = (str(path).lower(), line, column, message)
        if key in self.seen:
            return
        self.seen.add(key)
        self.rows.append({'path': str(path), 'line': line, 'column': column, 'severity': severity,
                          'message': message[:1000], 'code': code, 'source': source,
                          'inWorkspace': _inside(path, self.workspace),
                          'relative': self._relative(path)})

    def _relative(self, path):
        try:
            return Path(path).relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)

    def best_frame(self, frames):
        inside = [f for f in frames if _inside(_path(f[0], self.cwd), self.workspace)]
        return (inside or frames)[-1] if frames else None


def _python(lines, out):
    frames, in_trace, syntax = [], False, None
    for index, line in enumerate(lines):
        if line.startswith('Traceback (most recent call last)'):
            frames, in_trace, syntax = [], True, None
            continue
        frame = _PY_FRAME.match(line)
        if frame:
            # SyntaxError reports appear without "Traceback" when compiling.
            if not in_trace:
                frames, in_trace = [], True
            frames.append((frame['file'], frame['line'], None))
            syntax = None
            continue
        if not in_trace:
            continue
        if _PY_CARET.match(line) and frames:
            caret = line.index('^')
            source = lines[index - 1] if index else ''
            syntax = max(1, caret - (len(source) - len(source.lstrip())) + 1)
            continue
        exception = _PY_EXCEPTION.match(line.strip()) if not line.startswith(' ') else None
        if exception and frames:
            file, number, _ = out.best_frame(frames)
            message = exception['name'] + (': ' + exception['msg'] if exception['msg'] else '')
            column = syntax if exception['name'].endswith(('SyntaxError', 'IndentationError', 'TabError')) else 1
            out.add(file, number, column, 'error', message, 'python')
            frames, in_trace, syntax = [], False, None


def _node(lines, out):
    message, frames, header = None, [], None
    def flush():
        nonlocal message, frames, header
        if message and (frames or header):
            file, number, column = header if header else out.best_frame(frames)
            out.add(file, number, column, 'error', message, 'node')
        message, frames, header = None, [], None
    for line in lines:
        head = _NODE_HEADER.match(line)
        if head:
            flush()
            header = (head['file'], head['line'], 1)
            continue
        error = _NODE_ERROR.match(line.strip()) if not line.startswith(' ') else None
        if error:
            if message:
                flush()
            message = error['name'] + ': ' + error['msg']
            continue
        frame = _NODE_FRAME.match(line)
        if frame and message:
            frames.append((frame['file'], frame['line'], frame['col']))
            continue
        if message and frames and not line.strip():
            flush()
    flush()


def _java(lines, out):
    message, frames = None, []
    def flush():
        nonlocal message, frames
        if message and frames:
            # Stack frames carry a bare file name; resolve it under the workspace.
            name, number = frames[0]
            hits = [p for p in out.workspace.rglob(name)
                    if not any(part in {'.git', 'node_modules', 'build', '.keystone-build'} for part in p.parts)][:1] \
                if len(out.rows) < LIMIT else []
            out.add(str(hits[0]) if hits else name, number, 1, 'error', message, 'java')
        message, frames = None, []
    for line in lines:
        exception = _JAVA_EXCEPTION.match(line.strip()) if not line.startswith(('\t', ' ')) else None
        if exception:
            flush()
            message = exception['name'] + (': ' + exception['msg'] if exception['msg'] else '')
            continue
        frame = _JAVA_FRAME.match(line)
        if frame and message:
            frames.append((frame['file'], frame['line']))
        elif message and frames and not line.startswith(('\t', ' ')):
            flush()
    flush()


MULTILINE = {'python': _python, 'node': _node, 'java': _java}
MATCHERS = tuple(LINE_MATCHERS) + tuple(MULTILINE)


def parse(output, matchers, *, cwd, workspace=None):
    """Return problems for ``output`` using the named matchers, in output order."""
    out = _Collector(cwd, workspace)
    lines = ANSI.sub('', str(output or '')).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    names = [m for m in dict.fromkeys(matchers) if m in MATCHERS]
    for line in lines:
        for name in names:
            for pattern in LINE_MATCHERS.get(name, ()):
                found = pattern.match(line)
                if found:
                    groups = found.groupdict()
                    out.add(groups['file'], groups['line'], groups.get('col'), groups.get('sev') or 'error',
                            groups.get('msg'), name, groups.get('code') or '')
                    break
            else:
                continue
            break
    for name in names:
        if name in MULTILINE:
            MULTILINE[name](lines, out)
    return out.rows


def summary(problems):
    errors = sum(1 for p in problems if p['severity'] == 'error')
    warnings = sum(1 for p in problems if p['severity'] == 'warning')
    return {'errors': errors, 'warnings': warnings, 'total': len(problems)}
