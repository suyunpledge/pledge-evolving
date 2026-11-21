"""Local Chat/Work tools. File tools publish new, user-openable conversation attachments."""
import json
import os
from pathlib import Path, PureWindowsPath
import re
import uuid

from . import math_tools
from .chart_files import write_chart
from .local_tool_runtime import ToolFailure, _new_output, workspace_path
from .office_files import text, write_document, write_slides, write_spreadsheet


def obj(properties, *required):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def array(item):
    return {'type': 'array', 'items': item}


STRING = {'type': 'string'}
NUMBER = {'type': 'number'}
CELL = {'anyOf': [STRING, NUMBER, {'type': 'boolean'}, {'type': 'null'}]}
SHEET = obj({'name': STRING, 'headers': array(STRING), 'rows': array(array(CELL))}, 'name', 'headers', 'rows')
SLIDE = obj({'title': STRING, 'bullets': array(STRING)}, 'title', 'bullets')
SECTION = obj({'heading': STRING, 'paragraphs': array(STRING), 'bullets': array(STRING)})
MATH = ('Use + - * / % ^ **, parentheses, pi/e/tau and sin, cos, tan, asin, acos, atan, '
        'sqrt, log, log10, exp, abs, round, floor, ceil. Trigonometry uses radians.')
CATALOG = {
    'evaluate_math': ('函数与表达式求值', 'Evaluate a numeric expression or f(x) for supplied x_values. ' + MATH,
                      obj({'expression': STRING, 'x_values': array(NUMBER)}, 'expression')),
    'summarize_numbers': ('数值统计', 'Compute count, sum, mean, median, minimum, maximum and population standard deviation.',
                          obj({'values': array(NUMBER)}, 'values')),
    'plot_chart': ('绘制图表', 'Create a PNG attachment. For function give expression and optional x_min/x_max. '
                   'For line/bar/scatter give y and optional x (numbers or category labels). ' + MATH,
                   obj({'kind': {'type': 'string', 'enum': ['function', 'line', 'bar', 'scatter']},
                        'title': STRING, 'expression': STRING, 'x_min': NUMBER, 'x_max': NUMBER,
                        'x': array({'anyOf': [NUMBER, STRING]}), 'y': array(NUMBER),
                        'x_label': STRING, 'y_label': STRING}, 'kind', 'title')),
    'create_spreadsheet': ('创建 Excel 表格', 'Create an editable XLSX attachment with named sheets, column headers and typed rows. '
                           'Rows must match the header width. Strings are literal text.',
                           obj({'title': STRING, 'sheets': array(SHEET)}, 'title', 'sheets')),
    'create_slides': ('创建 PowerPoint 幻灯片', 'Create an editable widescreen PPTX attachment with a title slide and content slides. '
                      'Provide 1–8 bullets per content slide.',
                      obj({'title': STRING, 'subtitle': STRING, 'slides': array(SLIDE)}, 'title', 'slides')),
    'create_document': ('创建 Word 文档', 'Create an editable DOCX attachment. Each section contains paragraphs and/or bullet points.',
                        obj({'title': STRING, 'sections': array(SECTION)}, 'title', 'sections')),
}
FILES = {'plot_chart': ('.png', write_chart), 'create_spreadsheet': ('.xlsx', write_spreadsheet),
         'create_slides': ('.pptx', write_slides), 'create_document': ('.docx', write_document)}
NAMES = frozenset('keystone_' + key for key in CATALOG)


def attachment_path(folder, reference):
    if not isinstance(reference, str) or not re.fullmatch(r'[a-f0-9]{32}/[^/\\]+', reference):
        raise ValueError('附件标识无效。')
    path = workspace_path(folder, 'exports/' + reference)
    if path.suffix not in {suffix for suffix, _ in FILES.values()} or not path.is_file():
        raise ValueError('附件已移动或删除。')
    return path


def office_fallback(suffix):
    """Use machine-registered Office when the selected Windows file handler was uninstalled."""
    executable = {'.xlsx': 'EXCEL.EXE', '.docx': 'WINWORD.EXE', '.pptx': 'POWERPNT.EXE'}.get(suffix)
    if os.name != 'nt' or not executable:
        return None
    import winreg
    choice = rf'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{suffix}\UserChoice'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, choice) as key:
            handler = winreg.QueryValueEx(key, 'ProgId')[0]
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, handler):
                return None
        except FileNotFoundError:
            pass
        app = rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}'
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, app) as key:
            path = Path(os.path.expandvars(winreg.QueryValue(key, None).strip('"')))
        if path.is_absolute() and path.name.upper() == executable and path.is_file():
            return path
    except OSError:
        pass
    return None


def call(key, args, folder, cancel):
    try:
        schema = CATALOG[key][2]
        if not isinstance(args, dict) or args.keys() - schema['properties'].keys():
            raise ValueError('工具参数包含未定义的字段。')
        missing = set(schema['required']) - args.keys()
        if missing:
            raise ValueError('缺少参数 ' + ', '.join(sorted(missing)))
        if cancel.is_set():
            raise ValueError('操作已取消。')
        if len(json.dumps(args, ensure_ascii=False, allow_nan=False)) > 512_000:
            raise ValueError('单次输入过大，请拆分数据。')
        if key not in FILES:
            values = math_tools.evaluate(args) if key == 'evaluate_math' else math_tools.summarize(args)
            return {'ok': True, 'content': json.dumps(values, ensure_ascii=False, allow_nan=False)}
        suffix, write = FILES[key]
        stem = re.sub(r'[^\w .()-]', '_', text(args['title'], 120)).strip(' .')[:64].rstrip(' .')
        if not stem or PureWindowsPath(stem).is_reserved():
            stem = 'Keystone'
        filename = stem + suffix
        identifier = uuid.uuid4().hex
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        exports = workspace_path(folder, 'exports')
        exports.mkdir(exist_ok=True)
        with _new_output(exports, identifier) as (stage, destination):
            write(args, stage / filename)
            if cancel.is_set():
                raise ValueError('操作已取消。')
        return {'ok': True, 'content': '已创建 ' + filename, 'path': str(destination / filename),
                'meta': {'attachment': identifier + '/' + filename, 'execution_state': 'completed'}}
    except (ArithmeticError, ImportError, KeyError, OSError, TypeError, ValueError, ToolFailure) as exc:
        return {'ok': False, 'error': str(exc), 'meta': {'execution_state': 'not_started'}}


def with_productivity(connection, folder):
    existing, dispatch = connection
    tools = dict(existing)
    for key, (_, description, schema) in CATALOG.items():
        name = 'keystone_' + key
        tools[name] = ({'type': 'function', 'function': {
            'name': name, 'description': description, 'parameters': schema}}, ('productivity', key))

    def route(target, args, cancel):
        if isinstance(target, tuple) and len(target) == 2 and target[0] == 'productivity':
            return call(target[1], args, folder, cancel)
        return dispatch(target, args, cancel)
    return tools, route
