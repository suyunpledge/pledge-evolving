"""Shared bootstrap for the independent QA regression suite.

Puts the repository root on sys.path so ``forge`` imports the same way
``run.py`` does, and exposes a tiny check-runner that mirrors the
project's own ``selftest`` reporting style.

This file is QA scaffolding only. It never imports anything for the purpose
of mutating product state, and it never edits product source.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Always state WHICH tree is under test. Other agents commit and edit this
# repo concurrently, so a result without a tree identity is not reproducible.
print(f"[qa] probing tree: {ROOT}")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:
    """Run one check, record pass/fail, never raise."""
    try:
        ok, detail = fn()
    except Exception:  # noqa: BLE001 - QA harness must report, not crash
        ok, detail = False, "EXC: " + traceback.format_exc(limit=4).splitlines()[-1]
    RESULTS.append((name, bool(ok), str(detail)))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def summary(title: str) -> int:
    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"=== {title}: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ===")
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0
