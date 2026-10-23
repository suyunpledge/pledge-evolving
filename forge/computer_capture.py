"""Capture only an explicitly selected HWND. Runs in a bounded worker process.

PrintWindow failure never falls back to capturing the user's whole desktop.
"""
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib


def _chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def _capture(window, process_id):
    from .computer_win32 import WindowsControlDriver
    driver = WindowsControlDriver(window, process_id)
    u, g = driver.u, c.WinDLL('gdi32', use_last_error=True)
    u.IsIconic.argtypes = [w.HWND]
    if u.IsIconic(window):
        raise ValueError('Restore the selected window before capture')
    u.SetThreadDpiAwarenessContext.argtypes = [w.HANDLE]
    u.SetThreadDpiAwarenessContext(w.HANDLE(-4))
    geometry = driver.geometry()
    rect = w.RECT()
    if not u.GetWindowRect(window, c.byref(rect)): raise ValueError('Window geometry unavailable')
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0 or width * height > 16000000:
        raise ValueError('Window is empty or exceeds 16 megapixels')
    u.GetWindowDC.argtypes = [w.HWND]; u.GetWindowDC.restype = w.HDC
    u.ReleaseDC.argtypes = [w.HWND, w.HDC]
    u.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
    g.CreateCompatibleDC.argtypes = [w.HDC]; g.CreateCompatibleDC.restype = w.HDC
    g.SelectObject.argtypes = [w.HDC, w.HANDLE]; g.SelectObject.restype = w.HANDLE
    g.DeleteObject.argtypes = [w.HANDLE]; g.DeleteDC.argtypes = [w.HDC]
    class Header(c.Structure):
        _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                    ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                    ('image_size', w.DWORD), ('x', w.LONG), ('y', w.LONG),
                    ('used', w.DWORD), ('important', w.DWORD)]
    g.CreateDIBSection.argtypes = [w.HDC, c.POINTER(Header), w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD]
    g.CreateDIBSection.restype = w.HBITMAP
    source = u.GetWindowDC(window)
    dc = g.CreateCompatibleDC(source)
    bits = c.c_void_p()
    header = Header(c.sizeof(Header), width, -height, 1, 32, 0, width*height*4, 0,0,0,0)
    bitmap = g.CreateDIBSection(dc, c.byref(header), 0, c.byref(bits), None, 0)
    old = None
    try:
        if not source or not dc or not bitmap or not bits.value: raise OSError('Screenshot allocation failed')
        old = g.SelectObject(dc, bitmap)
        if not old or old == c.c_void_p(-1).value:
            old = None
            raise OSError('Screenshot bitmap selection failed')
        if not u.PrintWindow(window, dc, 2): raise OSError('This window does not support PrintWindow capture')
        if not g.GdiFlush(): raise OSError('Screenshot drawing did not flush')
        driver._validate()
        after = w.RECT(); u.GetWindowRect(window, c.byref(after))
        if bytes(after) != bytes(rect) or driver.geometry() != geometry:
            raise ValueError('Window moved during capture; retry preview')
        raw = c.string_at(bits, width*height*4)
        rgba = bytearray(len(raw))
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = raw[2::4], raw[1::4], raw[0::4], b'\xff'*(width*height)
        scan = b''.join(b'\0'+rgba[y*width*4:(y+1)*width*4] for y in range(height))
        png = b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', struct.pack('>IIBBBBB',width,height,8,6,0,0,0))
        png += _chunk(b'IDAT', zlib.compress(scan, 6)) + _chunk(b'IEND', b'')
        if len(png) > 8*1024*1024: raise ValueError('Screenshot exceeds 8 MiB')
        cl,ct,cr,cb = geometry['client_rect']
        left,top = cl-rect.left,ct-rect.top
        if not (0 <= left < cr-rect.left <= width and 0 <= top < cb-rect.top <= height):
            raise ValueError('Client geometry is outside the captured image')
        client_hash = hashlib.sha256()
        for row in range(top,cb-rect.top):
            client_hash.update(rgba[(row*width+left)*4:(row*width+cr-rect.left)*4])
        return {'png':png, 'width':width, 'height':height, **geometry,
                'client_sha256':client_hash.hexdigest()}
    finally:
        if old: g.SelectObject(dc, old)
        if bitmap: g.DeleteObject(bitmap)
        if dc: g.DeleteDC(dc)
        if source: u.ReleaseDC(window, source)


def capture_window(window, process_id, *, cancelled=None, timeout=6):
    """No credentials or user config enter the capture worker."""
    if cancelled is not None and cancelled.is_set(): raise RuntimeError('Capture cancelled')
    env = {k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP'}}
    root = Path(__file__).resolve().parents[1]
    env['PYTHONPATH'] = str(root)
    env['PYTHONIOENCODING'] = 'utf-8'
    process = subprocess.Popen([sys.executable, '-B', '-m', 'forge.computer_capture', str(int(window)), str(int(process_id))],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    import time
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled is not None and cancelled.is_set(): raise RuntimeError('Capture cancelled')
            if time.monotonic() >= deadline: raise TimeoutError('Window capture exceeded time budget')
            try:
                data, error = process.communicate(timeout=.1)
                break
            except subprocess.TimeoutExpired: pass
        if process.returncode != 0:
            raise RuntimeError(error.decode('utf-8', 'replace')[-500:])
        if len(data) < 4: raise ValueError('Incomplete capture metadata')
        size = struct.unpack('>I',data[:4])[0]
        if not 0 < size <= 4096: raise ValueError('Invalid capture metadata size')
        metadata = json.loads(data[4:4+size])
        data = data[4+size:]
        if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 8*1024*1024:
            raise ValueError('Capture returned an invalid PNG')
        width, height = struct.unpack('>II', data[16:24])
        if metadata['width'] != width or metadata['height'] != height:
            raise ValueError('Capture metadata and PNG dimensions differ')
        return {**metadata, 'png': data, 'sha256': hashlib.sha256(data).hexdigest(),
                'target': f'win32:{int(process_id)}:{int(window)}'}
    finally:
        if process.poll() is None: process.kill()
        process.communicate()


if __name__ == '__main__':
    try:
        frame = _capture(int(sys.argv[1]), int(sys.argv[2]))
        png = frame.pop('png')
        metadata = json.dumps(frame).encode('utf-8')
        sys.stdout.buffer.write(struct.pack('>I',len(metadata))+metadata+png)
    except Exception as exc:
        sys.stderr.write(str(exc)); raise SystemExit(1)
