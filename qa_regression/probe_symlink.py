"""Probe: does ProjectBoundary actually stop a symlink that points outside?

Question under test is narrow and security-relevant: if a project directory
contains a symlink whose target lives outside the project, does opening that
path through the editor leak outside content, or is it rejected?

Writes nothing outside a fresh temp dir.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _bootstrap import check, summary  # noqa: E402  (sys.path bootstrap first)

from forge.development.editor import Document, ProjectBoundary  # noqa: E402


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="qa-symlink-"))
    proj = base / "proj"
    proj.mkdir()
    outside = base / "private.txt"
    outside.write_text("OUTSIDE-SECRET")

    link = proj / "linked.txt"
    made = True
    try:
        link.symlink_to(outside)
    except OSError as exc:
        made = False
        print(f"NOTE: symlink creation unavailable on this host: {exc}")
    print(f"symlink created: {made}; is_symlink={link.is_symlink() if made else None}")
    if made:
        print(f"link.resolve() -> {link.resolve()}")

    b = ProjectBoundary(proj)

    def _boundary_blocks():
        if not made:
            return True, "symlink unsupported on host; cannot exercise"
        try:
            p = b.path(link)
        except PermissionError as exc:
            return True, f"blocked: {exc}"
        return False, f"ALLOWED -> {p}; content={p.read_text()!r}"

    check("boundary rejects symlink escape", _boundary_blocks)

    def _document_open_blocks():
        if not made:
            return True, "symlink unsupported on host; cannot exercise"
        try:
            d = Document.open(b, link)
        except PermissionError as exc:
            return True, f"blocked: {exc}"
        return False, f"ALLOWED; leaked text={d.text!r}"

    check("Document.open rejects symlink escape", _document_open_blocks)

    # Control: a plain in-project file must still be allowed, otherwise the
    # probe above could "pass" simply because everything is rejected.
    inside = proj / "inside.txt"
    inside.write_text("INSIDE-OK")

    def _control_inside_allowed():
        try:
            d = Document.open(b, inside)
        except Exception as exc:  # noqa: BLE001
            return False, f"in-project file wrongly rejected: {exc}"
        return d.text == "INSIDE-OK", f"text={d.text!r}"

    check("control: in-project file still opens", _control_inside_allowed)

    # Control: a direct absolute outside path must be rejected.
    def _control_abs_outside_blocked():
        try:
            b.path(outside)
        except PermissionError as exc:
            return True, f"blocked: {exc}"
        return False, "absolute outside path ALLOWED"

    check("control: absolute outside path rejected", _control_abs_outside_blocked)

    return summary("symlink boundary probe")


if __name__ == "__main__":
    raise SystemExit(main())
