"""Small, in-memory image observations for routes with explicit vision support."""
import base64
from pathlib import Path
import struct
import zlib

from .model_kinds import resolve_model_kind
from .secrets import read_public_bytes

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 16_000_000


def supports_vision(caps, model, kind_id=''):
    if 'vision' in caps:
        return caps['vision'] is True
    kind, _ = resolve_model_kind(model, kind_id)
    return bool(kind and 'vision' in kind.traits)


def router_supports(router, wire):
    if not hasattr(router, '_order') or not hasattr(router, 'providers') or getattr(router, 'moa', False):
        return False
    routes = router._order(None)
    for key, model in routes:
        provider = router.providers.get(key)
        if getattr(provider, 'wire', None) != wire or not callable(getattr(provider, 'capabilities_for_model', None)):
            return False
        caps = provider.capabilities_for_model(model)
        kind_id = provider.model_kind_ids.get(model) or (provider.model_kind_id if model == provider.default_model else '')
        if not supports_vision(caps, model, kind_id):
            return False
    return bool(routes)


def image_path(root, reference):
    if not isinstance(reference, str) or not reference:
        raise ValueError('请提供工作区图片路径。')
    root = Path(root).resolve()
    path = (root / reference).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError('只能查看当前工作区内的图片。')
    return path


def image_data(path):
    """Strip PNG ancillary text and JPEG application metadata before transport."""
    raw = read_public_bytes(path, limit=MAX_BYTES)
    if path.suffix.lower() == '.png' and raw.startswith(b'\x89PNG\r\n\x1a\n'):
        data = bytearray(raw[:8])
        offset = 8
        width = height = 0
        pixels = False
        while offset + 12 <= len(raw):
            size, tag = struct.unpack_from('>I4s', raw, offset)
            end = offset + 12 + size
            if end > len(raw) or zlib.crc32(raw[offset+4:end-4]) != int.from_bytes(raw[end-4:end], 'big'):
                raise ValueError('PNG 数据损坏。')
            if offset == 8:
                if tag != b'IHDR' or size != 13:
                    raise ValueError('PNG 缺少有效尺寸。')
                width, height = struct.unpack_from('>II', raw, offset + 8)
            if tag == b'IDAT':
                pixels = True
            if tag in {b'IHDR', b'PLTE', b'tRNS', b'IDAT', b'IEND'}:
                data.extend(raw[offset:end])
            offset = end
            if tag == b'IEND' and not size and pixels:
                break
        else:
            raise ValueError('PNG 缺少完整图像。')
        mime = 'image/png'
    elif path.suffix.lower() in ('.jpg', '.jpeg') and raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9'):
        # Retain the encoded image, omitting metadata before its first scan.
        data = bytearray(raw[:2])
        width = height = 0
        offset = 2
        while offset + 4 <= len(raw) and raw[offset] == 255:
            marker = raw[offset+1]
            length = int.from_bytes(raw[offset+2:offset+4], 'big')
            end = offset + 2 + length
            if length < 2 or end > len(raw):
                raise ValueError('JPEG 数据损坏。')
            if marker in (0xc0, 0xc1, 0xc2) and length >= 7:
                height, width = struct.unpack_from('>HH', raw, offset + 5)
            if marker == 0xda:
                data.extend(raw[offset:])
                break
            if marker != 0xfe and not 0xe0 <= marker <= 0xef:
                data.extend(raw[offset:end])
            offset = end
        else:
            raise ValueError('JPEG 缺少图像数据。')
        mime = 'image/jpeg'
    else:
        raise ValueError('支持 PNG 或 JPEG 图片，最大 2 MiB。')
    if not width or not height or width * height > MAX_PIXELS:
        raise ValueError('图片尺寸超出限制。')
    return mime, base64.b64encode(data).decode('ascii')
