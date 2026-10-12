"""Probe: is TaskSnapshot truly frozen against later ModeState mutations?

This is the exact scenario from tests/test_development_mode.py::
test_all_role_models_are_part_of_task_snapshot, instrumented so we can see
what the snapshot actually holds instead of only seeing the assertion fail.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _bootstrap import check, summary  # noqa: E402

from forge.development.state import ModeState  # noqa: E402


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="qa-snapshot-"))
    home = base / "ide-home"

    s = ModeState(home)
    s.configure_role("review", ("reviewer", "old"))
    frozen = s.task_snapshot()

    print("frozen.roles            :", frozen.roles)
    print("type of roles value     :", type(dict(frozen.roles).get("review")))
    print("raw _data['roles']      :", s._data["roles"])

    s.configure_role("review", ("reviewer", "new"))
    after = dict(frozen.roles).get("review")
    print("after mutation, frozen  :", after)

    def _value_frozen():
        got = dict(frozen.roles).get("review")
        expected = ("reviewer", "old")
        # Compare element-wise so a list/tuple difference is reported clearly.
        same = tuple(got) == expected if got is not None else False
        return same, f"got={got!r} ({type(got).__name__}) expected={expected!r}"

    check("snapshot role value is frozen at ('reviewer','old')", _value_frozen)

    def _type_is_tuple():
        got = dict(frozen.roles).get("review")
        return isinstance(got, tuple), f"got type={type(got).__name__}, value={got!r}"

    check("snapshot role value is a tuple (test asserts == tuple)", _type_is_tuple)

    def _live_state_updated():
        got = s._data["roles"]["review"]
        return tuple(got) == ("reviewer", "new"), f"live={got!r}"

    check("control: live state did take the new role", _live_state_updated)

    # Also verify agent_models behaves the same way.
    def _agents_frozen():
        s.configure_agent_model("worker", ("p", "m1"))
        snap = s.task_snapshot()
        s.configure_agent_model("worker", ("p", "m2"))
        got = dict(snap.agent_models).get("worker")
        return tuple(got) == ("p", "m1"), f"got={got!r}"

    check("snapshot agent_models frozen", _agents_frozen)

    return summary("snapshot isolation probe")


if __name__ == "__main__":
    raise SystemExit(main())
