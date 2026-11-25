"""Read-only desktop observations for model tools, with bounded local rendering."""
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from keystone.route_capabilities import effective_capabilities
from keystone.secrets import read_public_bytes, redact
from keystone.visual_input import image_data, MAX_BYTES, supports_vision
from .local_tool_runtime import ToolFailure, _new_output, workspace_path
from .office_workspace import OfficeWorkspace
from .productivity_tools import attachment_path, obj

REFERENCE = re.compile(r'[a-f0-9]{32}/[^/\\]+')
PAGE = {'type': 'integer', 'minimum': 1, 'description': 'Page or slide number, starting at 1.'}
PATH = {'type': 'string', 'description': 'Workspace-relative path, or the attachment reference from a tool receipt.'}


def tools(provider, *, chat=False):
    model = provider.get('model', '')
    try:
        caps = effective_capabilities(provider.get('routeCapabilities', {}),
                                      provider.get('modelCapabilities', {}).get(model, {}))
        vision = supports_vision(caps, model, provider.get('modelKinds', {}).get(model) or provider.get('modelKindId', ''))
    except (AttributeError, ValueError):
        vision = False
    catalog = {}
    if vision:
        desc = 'View a PNG or JPEG from this conversation.' if chat else 'Inspect a workspace PNG/JPEG or a PNG conversation attachment.'
        catalog['view_image'] = desc, obj({'path': PATH}, 'path')
        if not chat:
            catalog['office_render'] = ('Render a saved native Office document page or slide to a PNG attachment and inspect it.',
                obj({'id': {'type': 'string', 'description': 'Document or presentation ID from office_list.'}, 'page': PAGE}, 'id'))
    if not chat:
        catalog['read_pdf'] = ('Read embedded text of one workspace PDF page; returns page count.',
                               obj({'path': PATH, 'page': PAGE}, 'path'))
    return {'keystone_' + key: ({'type': 'function', 'function': {'name': 'keystone_' + key,
            'description': desc, 'parameters': schema}}, ('visual', key)) for key, (desc, schema) in catalog.items()}


def render(state, number, cancel):
    payload = json.dumps([state, number], ensure_ascii=True).encode('ascii')
    if len(payload) > 6 * 1024 * 1024:
        raise ValueError('单次渲染的办公内容过大。')
    worker = Path(__file__).with_name('office_preview_process.py')
    with subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        deadline = time.monotonic() + 22
        try:
            while not cancel.is_set() and time.monotonic() < deadline:
                try:
                    raw, _ = process.communicate(payload, timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            else:
                raise ValueError('渲染已取消或超时。')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    result = json.loads(raw)
    if process.returncode or 'error' in result:
        raise ValueError(result.get('error', '渲染失败。'))
    return base64.b64decode(result['image'], validate=True), result['pages']


def pdf_text(path, number, workspace):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtPdf import QPdfDocument
    if path.suffix.lower() != '.pdf':
        raise ValueError('请提供 PDF 文件。')
    device = QBuffer()
    device.setData(read_public_bytes(path, limit=16 * 1024 * 1024, root=workspace))
    device.open(QIODevice.OpenModeFlag.ReadOnly)
    pdf = QPdfDocument()
    pdf.load(device)
    count = pdf.pageCount()
    if not 1 <= number <= count:
        raise ValueError('PDF 未能打开或页码超出范围。')
    content = pdf.getAllText(number - 1).text() or '这页没有可提取的文本。'
    return {'ok': True, 'content': redact(content[:16000]), 'meta': {'page': number, 'pages': count, 'truncated': len(content) > 16000}}


def call(target, args, workspace, folder, cancel, *, chat=False):
    key = target[1]
    try:
        if cancel.is_set():
            raise ValueError('操作已取消。')
        page = args.get('page', 1)
        if type(page) is not int or page < 1:
            raise ValueError('页码从 1 开始。')
        if key == 'view_image':
            reference = args.get('path', '')
            if not isinstance(reference, str):
                raise ValueError('请提供图片路径。')
            if REFERENCE.fullmatch(reference):
                path = attachment_path(folder, reference)
                root = folder
            elif chat:
                raise ValueError('Chat 只能查看本对话的图片附件。')
            else:
                path = workspace_path(workspace, reference)
                root = workspace
            image = image_data(path, root=root)
            return {'ok': True, 'content': '图片已提供给模型。', '_visual_observation': image}
        if chat:
            raise ValueError('请进入 Work 后读取办公文件。')
        if key == 'read_pdf':
            return pdf_text(workspace_path(workspace, args.get('path')), page, workspace)
        if key != 'office_render':
            raise ValueError('未知的视觉工具。')
        from .office_controller import editor_state
        doc = OfficeWorkspace(workspace).read_for_tool(args.get('id'))
        if doc['kind'] not in ('document', 'presentation'):
            raise ValueError('支持文档或演示；表格图表请使用 office_read。')
        state = {k: v for k, v in editor_state(doc).items() if k in ('fields', 'kind', 'slideOrder', 'slideDesign')}
        state['fields'] = redact({k: v for k, v in state['fields'].items() if not k.endswith('.notes')})
        bitmap, total = render(state, page, cancel)
        if len(bitmap) > MAX_BYTES:
            raise ValueError('渲染结果超过图片大小限制。')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        exports = workspace_path(folder, 'exports')
        exports.mkdir(exist_ok=True)
        identifier = uuid.uuid4().hex
        name = f'page-{page}.png'
        with _new_output(exports, identifier) as (stage, dest):
            (stage / name).write_bytes(bitmap)
            image = image_data(stage / name, root=exports)
            if cancel.is_set():
                raise ValueError('操作已取消。')
        return {'ok': True, 'content': '已渲染并提供页面图片。', '_visual_observation': image,
                'meta': {'attachment': identifier + '/' + name, 'version': doc['version'], 'page': page, 'pages': total}}
    except (OSError, ValueError, KeyError, TypeError, ToolFailure) as exc:
        return {'ok': False, 'error': str(exc), 'meta': {'execution_state': 'not_started'}}
