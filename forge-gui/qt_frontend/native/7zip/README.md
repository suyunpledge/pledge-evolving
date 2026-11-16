# 7-Zip decoder used by Forge

`sdk/` contains the public-domain ANSI-C decoder and its dependencies from
[ip7z/7zip tag 26.06](https://github.com/ip7z/7zip/tree/26.06/C), unmodified.
The upstream SDK notes are in [7zC.txt](7zC.txt). `forge7z.c` is Forge's
binding. It exposes archive metadata and decoded buffers. It does not resolve
archive entry paths or write extracted files.

Forge loads the bundled Windows x64 DLL from this directory. To rebuild with
GCC or Clang on your desktop platform, run `python build.py` in this directory.
Set `CC` to the compiler name or path if needed.
