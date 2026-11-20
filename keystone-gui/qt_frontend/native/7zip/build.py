"""Build the bundled decoder with GCC or Clang (CC can select the compiler)."""
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
SOURCES = ('Bcj2', 'Bra', 'Bra86', 'BraIA64', 'CpuArch', 'Delta', 'Lzma2Dec', 'LzmaDec',
           'Ppmd7', 'Ppmd7Dec', '7zCrc', '7zCrcOpt', '7zArcIn', '7zBuf', '7zBuf2',
           '7zDec', '7zFile', '7zStream')


def main():
    compiler = os.environ.get('CC', 'gcc')
    machine = platform.machine().lower()
    arch = 'x64' if machine in {'amd64', 'x86_64'} else machine
    if sys.platform == 'win32':
        flags = ['-shared', '-static-libgcc', '-Wl,--no-insert-timestamp', '-s']
        extension = 'dll'
    elif sys.platform == 'darwin':
        flags, extension = ['-dynamiclib'], 'dylib'
    else:
        flags, extension = ['-shared'], 'so'
    if sys.platform != 'win32':
        flags += ['-fPIC']
    target = ROOT / f'forge7z-{arch}.{extension}'
    with tempfile.TemporaryDirectory(prefix='.build-', dir=ROOT) as directory:
        output = Path(directory) / target.name
        subprocess.run([compiler, '-std=c11', '-O2', '-fvisibility=hidden',
                        '-D_FILE_OFFSET_BITS=64', '-DZ7_PPMD_SUPPORT', '-DZ7_EXTRACT_ONLY', *flags,
                        '-I', str(ROOT / 'sdk'), str(ROOT / 'forge7z.c'),
                        *(str(ROOT / 'sdk' / (name + '.c')) for name in SOURCES),
                        '-o', str(output)], check=True)
        output.replace(target)
    print(target)


if __name__ == '__main__':
    main()
