"""Probe: identify what makes the snapshot test order-dependent.

Hypothesis under test: importing a module from tests/test_development_loop.py
changes how ModeState.task_snapshot() materialises role pairs, which turns a
passing assertion into a failing one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _bootstrap import check, summary  # noqa: E402

import forge.phase_models as pm  # noqa: E402
from forge.development.state import ModeState  # noqa: E402


def scenario(label: str):
    """Run the exact snapshot scenario and report what came back."""
    base = Path(tempfile.mkdtemp(prefix="qa-poll-"))
    s = ModeState(base / "ide-home")
    s.configure_role("review", ("reviewer", "old"))
    frozen = s.task_snapshot()
    s.configure_role("review", ("reviewer", "new"))
    got = dict(frozen.roles).get("review")
    print(f"  [{label}] frozen role -> {got!r} ({type(got).__name__})")
    print(f"  [{label}] model_pair id={id(pm.model_pair)} "
          f"module={pm.model_pair.__module__}")
    return got


def main() -> int:
    print("model_pair before extra imports:", pm.model_pair)

    got_clean = scenario("clean import")
    check("clean: snapshot role == ('reviewer','old') as tuple",
          lambda: (got_clean == ("reviewer", "old"), f"{got_clean!r}"))

    # Now pull in the modules that tests/test_development_loop.py imports,
    # one at a time, and re-run the same scenario.
    import importlib

    suspects = [
        "forge.loop",
        "forge.policy",
        "forge.tools",
        "forge.test_task_planning",
        "forge.test_phase_models",
        "forge.model",
        "forge.session",
    ]
    changed = None
    for name in suspects:
        importlib.import_module(name)
        got = scenario(f"after {name}")
        if got != got_clean:
            changed = name
            print(f"  >>> behaviour changed after importing {name}")
            break

    def _no_pollution():
        return changed is None, f"first divergent import: {changed}"

    check("importing dev-loop modules does not change snapshot semantics", _no_pollution)

    print()
    print("model_pair after imports:", pm.model_pair)
    return summary("order-dependence probe")


if __name__ == "__main__":
    raise SystemExit(main())
