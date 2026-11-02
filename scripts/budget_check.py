"""Check Forge's per-file source line ceilings.

This is an engineering ratchet, not a test-coverage claim.  Every Python file
under ``forge/`` must be listed in the manifest; deliberate growth requires a
visible ``--update`` step in the same change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return sum(1 for _ in handle)


def _discover(root: Path) -> dict[str, int]:
    base = root / "forge"
    rows: dict[str, int] = {}
    if not base.is_dir():
        return rows
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts or any(part.startswith("_bak-") for part in path.parts):
            continue
        rows[path.relative_to(root).as_posix()] = _line_count(path)
    return rows


def audit(root: Path, manifest_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"manifest unreadable: {exc}"], "files": []}
    entries = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {"ok": False, "errors": ["manifest.files must be an object"], "files": []}

    current = _discover(root)
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    for rel, ceiling_raw in sorted(entries.items()):
        path = root / rel
        if not path.is_file():
            errors.append(f"missing manifest file: {rel}")
            continue
        try:
            ceiling = int(ceiling_raw)
        except (TypeError, ValueError):
            errors.append(f"invalid ceiling for {rel}: {ceiling_raw!r}")
            continue
        used = current.get(rel, _line_count(path))
        row = {"path": rel, "lines": used, "ceiling": ceiling,
               "over": max(0, used - ceiling)}
        rows.append(row)
        if used > ceiling:
            errors.append(f"{rel}: {used} lines exceeds ceiling {ceiling}")

    for rel in sorted(set(current) - set(entries)):
        errors.append(f"unlisted forge file: {rel}")

    return {"ok": not errors, "errors": errors, "files": rows,
            "count": len(rows), "total_lines": sum(row["lines"] for row in rows)}


def update(root: Path, manifest_path: Path) -> dict[str, Any]:
    rows = _discover(root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"version": 1, "files": rows},
                                        ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return {"ok": True, "count": len(rows), "total_lines": sum(rows.values()),
            "manifest": str(manifest_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--update", action="store_true", help="rewrite ceilings to current line counts")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest = (args.manifest or (root / "forge-budgets.json")).resolve()
    result = update(root, manifest) if args.update else audit(root, manifest)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.update:
        print(f"updated {manifest} ({result['count']} files, {result['total_lines']} lines)")
    else:
        for error in result["errors"]:
            print(f"[FAIL] {error}")
        print(f"{'PASS' if result['ok'] else 'FAIL'}: {result['count']} files, {result['total_lines']} lines")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
