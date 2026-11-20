"""Read archive entries as byte streams without letting a decoder place files."""
from contextlib import contextmanager
import ctypes as c
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import platform
import stat
import sys
import tarfile
import threading
import zipfile


SEVEN_MAGIC = b"7z\xbc\xaf\x27\x1c"
NATIVE = Path(__file__).resolve().parent / 'native' / '7zip'
_RUNNING = c.CFUNCTYPE(c.c_int)
_load_lock = threading.Lock()
_CHUNK = 1024 * 1024


class ArchiveError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    key: object
    name: str
    size: int
    folder: bool


def library_path():
    platform_name = platform.machine().lower()
    arch = 'x64' if platform_name in {'x86_64', 'amd64'} else platform_name
    ext = {'win32': 'dll', 'darwin': 'dylib'}.get(sys.platform, 'so')
    return NATIVE / f'forge7z-{arch}.{ext}'


@lru_cache(maxsize=1)
def _load():
    path = library_path()
    try:
        lib = c.CDLL(str(path))
    except OSError:
        raise ArchiveError('无法加载 Keystone 内置 7-Zip 解码核心，请检查当前系统版本。') from None
    lib.forge7z_crc_init.argtypes = []
    lib.forge7z_crc_init.restype = None
    lib.forge7z_open.argtypes = [c.c_wchar_p if os.name == 'nt' else c.c_char_p,
                                c.c_size_t, _RUNNING, c.POINTER(c.c_int)]
    lib.forge7z_open.restype = c.c_void_p
    lib.forge7z_count.argtypes = [c.c_void_p]
    lib.forge7z_count.restype = c.c_uint32
    lib.forge7z_entry.argtypes = [c.c_void_p, c.c_uint32, c.POINTER(c.c_uint16), c.c_size_t,
                                 c.POINTER(c.c_uint64), c.POINTER(c.c_uint32)]
    lib.forge7z_entry.restype = c.c_size_t
    lib.forge7z_bytes.argtypes = [c.c_void_p, c.c_uint32, c.POINTER(c.c_void_p), c.POINTER(c.c_size_t)]
    lib.forge7z_bytes.restype = c.c_int
    lib.forge7z_close.argtypes = [c.c_void_p]
    lib.forge7z_close.restype = None
    lib.forge7z_crc_init()
    return lib


def _seven_error(status):
    if status == 2:
        return ArchiveError('7z 数据块超出解码内存上限。')
    if status == 4:
        return ArchiveError('压缩算法或加密方式超出当前内置 7-Zip 核心支持范围。')
    if status == 10:
        return ArchiveError('解压已取消或超时。')
    return ArchiveError(f'无法解码 7z 归档，可能已损坏（错误 {status}）。')


class SevenArchive:
    def __init__(self, path, running, memory):
        with _load_lock:
            self.lib = _load()
        self.running = _RUNNING(running)
        raw = str(path) if os.name == 'nt' else os.fsencode(path)
        status = c.c_int()
        self.handle = self.lib.forge7z_open(raw, memory, self.running, c.byref(status))
        if not self.handle:
            raise _seven_error(status.value)

    def entries(self, max_entries):
        count = self.lib.forge7z_count(self.handle)
        if count > max_entries:
            raise ArchiveError('归档条目数量超出单次解压上限。')
        for index in range(count):
            length_args = (c.c_uint64(), c.c_uint32())
            size, flags = length_args
            length = self.lib.forge7z_entry(self.handle, index, None, 0, c.byref(size), c.byref(flags))
            if length < 2 or length > 4096 or flags.value & 2:
                raise ArchiveError('归档包含链接、特殊条目或无法使用的文件名。')
            name = (c.c_uint16 * length)()
            self.lib.forge7z_entry(self.handle, index, name, length, c.byref(size), c.byref(flags))
            text = bytes(name)[:-2].decode('utf-16-le')
            yield Entry(index, text, size.value, bool(flags.value & 1))

    def chunks(self, entry):
        address, size = c.c_void_p(), c.c_size_t()
        status = self.lib.forge7z_bytes(self.handle, entry.key, c.byref(address), c.byref(size))
        if status:
            raise _seven_error(status)
        if size.value != entry.size or (size.value and address.value is None):
            raise ArchiveError('7z 解码结果与归档目录不一致。')
        for offset in range(0, size.value, _CHUNK):
            yield c.string_at(address.value + offset, min(_CHUNK, size.value - offset))

    def close(self):
        self.lib.forge7z_close(self.handle)


class StandardArchive:
    def __init__(self, archive):
        self.archive = archive

    def entries(self, max_entries):
        if isinstance(self.archive, zipfile.ZipFile):
            values = self.archive.infolist()
            if len(values) > max_entries:
                raise ArchiveError('归档条目数量超出单次解压上限。')
            for item in values:
                mode = (item.external_attr >> 16) & 0xFFFF if item.create_system == 3 else 0
                if (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or item.flag_bits & 1):
                    raise ArchiveError('ZIP 包含链接、特殊条目或加密内容。')
                yield Entry(item, item.orig_filename, item.file_size, item.is_dir())
        else:
            for index, item in enumerate(self.archive):
                if index >= max_entries:
                    raise ArchiveError('归档条目数量超出单次解压上限。')
                if not (item.isdir() or item.isfile()):
                    raise ArchiveError('TAR 包含链接或特殊条目。')
                yield Entry(item, item.name, item.size, item.isdir())

    def chunks(self, entry):
        if isinstance(self.archive, zipfile.ZipFile):
            stream = self.archive.open(entry.key)
        else:
            stream = self.archive.extractfile(entry.key)
        if stream is None:
            raise ArchiveError('无法读取归档条目。')
        with stream:
            while chunk := stream.read(_CHUNK):
                yield chunk


@contextmanager
def open_archive(path, running, memory):
    with path.open('rb') as stream:
        prefix = stream.read(len(SEVEN_MAGIC))
    if prefix == SEVEN_MAGIC:
        archive = SevenArchive(path, running, memory)
        try:
            yield archive
        finally:
            archive.close()
        return
    try:
        if zipfile.is_zipfile(path):
            archive = zipfile.ZipFile(path)
        else:
            archive = tarfile.open(path, 'r:*')
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        raise ArchiveError('支持 .7z、ZIP 和 TAR（含压缩 TAR），请选择可用的归档文件。') from None
    try:
        with archive:
            yield StandardArchive(archive)
    except (NotImplementedError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise ArchiveError('无法解压此归档，请检查文件完整性和压缩方法。') from exc
