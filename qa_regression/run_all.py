"""Independent QA regression runner for Forge.

Run every probe in this directory and aggregate pass/fail.

    python qa_regression/run_all.py

Two probes are intentionally *diagnostic* rather than pass/fail suites
(probe_symlink.py and probe_redaction_leak.py report what they observe; some
of their FAIL lines are known product/test-harness findings rather than
regressions). Everything else must be green on a healthy main.

The runner never mutates product source. Probes write only to temp dirs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (script, kind) -- "gate" means a FAIL is a regression; "info" means the
# output documents known findings and is not used to fail the build.
PROBES = [
    ("probe_defects.py", "gate"),      # checkpoint / evolution / memory
    ("probe_defects2.py", "gate"),     # session + budget concurrency
    ("probe_sanitise.py", "gate"),     # child-output injection coverage
    ("probe_snapshot.py", "gate"),     # TaskSnapshot isolation
    ("probe_tools.py", "gate"),        # tool registry invariants
    ("probe_symlink.py", "info"),      # documents the host symlink limitation
    ("probe_symlink_nature.py", "info"),
    ("probe_pollution.py", "info"),
    ("probe_redaction_leak.py", "info"),
]


def main() -> int:
    gates, info = [], []
    for name, kind in PROBES:
        path = HERE / name
        if not path.exists():
            print(f"[skip] {name} (missing)")
            continue
        print(f"\n-------- {name} [{kind}] --------")
        proc = subprocess.run([sys.executable, str(path)],
                              cwd=str(HERE), capture_output=True, text=True)
        print(proc.stdout.strip() or "(no stdout)")
        if proc.stderr.strip():
            print("stderr:", proc.stderr.strip()[:500])
        (gates if kind == "gate" else info).append((name, proc.returncode))

    print("\n================ QA regression summary ================")
    failed = [(n, rc) for n, rc in gates if rc != 0]
    for name, rc in gates:
        print(f"  {'GATE PASS' if rc == 0 else 'GATE FAIL'}  {name}")
    for name, rc in info:
        print(f"  {'info ok  ' if rc == 0 else 'info FAIL(documented)'}  {name}")
    print(f"\ngates: {len(gates) - len(failed)}/{len(gates)} passed")
    if failed:
        print("Failing gates (these are regressions to fix):")
        for name, _ in failed:
            print(f"  - {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
