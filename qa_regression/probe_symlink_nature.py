"""Determine what kind of filesystem object symlink_to() actually produced.

Before calling the escaping failure a product defect we must know whether the
symlink is real. If Windows produced a non-functional placeholder, the
"escape" never happened and the failing assertion is an environment artifact.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="qa-nature-"))
    proj = base / "proj"
    proj.mkdir()
    outside = base / "private.txt"
    outside.write_text("OUTSIDE-SECRET")
    link = proj / "linked.txt"

    try:
        link.symlink_to(outside)
    except OSError as exc:
        print(f"symlink_to raised OSError: {exc}")
        return

    st = os.lstat(str(link))
    print("exists(link):", link.exists())
    print("is_symlink:", link.is_symlink())
    print("lstat.st_mode  :", oct(st.st_mode))
    print("S_ISLNK(lstat) :", stat.S_ISLNK(st.st_mode))
    print("size on disk   :", st.st_size)
    attrs = getattr(st, "st_file_attributes", None)
    print("st_file_attributes:", attrs)
    if attrs is not None:
        REPARSE = 0x0400
        print("  FILE_ATTRIBUTE_REPARSE_POINT set:", bool(attrs & REPARSE))
    try:
        print("os.readlink    :", os.readlink(str(link)))
    except OSError as exc:
        print("os.readlink    : FAILED", exc)
    print("read_text      :", repr(link.read_text()))
    print("resolve(strict):", link.resolve())

    # Cross-check against a directory symlink (junctions often behave
    # differently from file symlinks on Windows).
    odir = base / "outsidedir"
    odir.mkdir()
    (odir / "f.txt").write_text("DIR-SECRET")
    dlink = proj / "linkdir"
    try:
        dlink.symlink_to(odir, target_is_directory=True)
        print("\ndir symlink is_symlink:", dlink.is_symlink())
        print("dir symlink resolve    :", dlink.resolve())
        print("dir listing works      :", (dlink / "f.txt").exists())
    except OSError as exc:
        print("\ndir symlink FAILED:", exc)


if __name__ == "__main__":
    main()
