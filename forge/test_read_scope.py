"""Read-scope contract: reading is an independent axis from writing.

The property under test, stated once: **read scope and write scope move
independently.** Historically they were welded together through ``Sandbox``, so
"read anywhere" forced "write anywhere". These assertions pin the decoupling so
a future refactor cannot weld them back.

Run directly::

    python -m forge.test_read_scope
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .policy import Decision, Mode, Policy, ReadScope, Sandbox

_PASS: list[str] = []
_FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (_PASS if ok else _FAIL).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> int:
    print("forge read-scope assertions")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws = root / "ws"
        outside = root / "outside"
        extra = root / "extra"
        for d in (ws, outside, extra):
            d.mkdir()
        inside_file = ws / "a.txt"
        outside_file = outside / "b.txt"
        extra_file = extra / "c.txt"
        for f in (inside_file, outside_file, extra_file):
            f.write_text("x", encoding="utf-8")

        # -- 1. AUTO reproduces the historical behaviour exactly ------------
        print("\n-- AUTO = 旧行为（兼容）--")
        auto_closed = Policy(sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws)
        check("auto + workspace-write: reads inside", auto_closed.allows_read(inside_file))
        check("auto + workspace-write: blocks outside", not auto_closed.allows_read(outside_file))
        auto_open = Policy(sandbox=Sandbox.FULL_ACCESS, workspace=ws)
        check("auto + full-access: reads anywhere", auto_open.allows_read(outside_file))

        # -- 2. the decoupling itself ---------------------------------------
        print("\n-- 解耦：写受限、读自由 --")
        decoupled = Policy(sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws,
                           read_scope=ReadScope.ALL)
        check("write still confined to workspace", decoupled.allows_write(outside_file) is False)
        check("read allowed outside workspace", decoupled.allows_read(outside_file) is True)
        check("read allowed inside too", decoupled.allows_read(inside_file))

        # the mirror case: sandbox open, reads deliberately reined in
        print("\n-- 反向：写开放、读收窄 --")
        narrowed = Policy(sandbox=Sandbox.FULL_ACCESS, workspace=ws,
                          read_scope=ReadScope.WORKSPACE)
        check("write anywhere still allowed", narrowed.allows_write(outside_file) is True)
        check("read confined by explicit scope", narrowed.allows_read(outside_file) is False)

        # -- 3. explicit read roots -----------------------------------------
        print("\n-- 指定文件范围 --")
        scoped = Policy(sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws,
                        read_scope=ReadScope.WORKSPACE, read_roots=(extra,))
        check("workspace still readable", scoped.allows_read(inside_file))
        check("extra root readable", scoped.allows_read(extra_file))
        check("unlisted path still blocked", not scoped.allows_read(outside_file))
        check("granted_read_roots lists both", len(scoped.granted_read_roots()) == 2)

        # -- 4. evaluate() wires the same gate ------------------------------
        print("\n-- evaluate() 走同一个门 --")
        ev = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws,
                    read_scope=ReadScope.ALL)
        check("read_file outside allowed under scope=all",
              ev.evaluate("read_file", args={"path": str(outside_file)}) is Decision.ALLOW,
              str(ev.evaluate("read_file", args={"path": str(outside_file)})))
        check("write_file outside still denied",
              ev.evaluate("write_file", args={"path": str(outside_file)}) is Decision.DENY)
        # and the confined case still denies
        ev2 = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws)
        check("read_file outside denied when confined",
              ev2.evaluate("read_file", args={"path": str(outside_file)}) is Decision.DENY)
        check("grep root is gated the same way",
              ev.evaluate("grep", args={"root": str(outside), "pattern": "x"}) is Decision.ALLOW)

        # -- 5. subagents inherit, and cannot widen -------------------------
        print("\n-- 子代理继承 --")
        parent = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws,
                        read_scope=ReadScope.ALL, read_roots=(extra,))
        child = parent.child()
        check("child inherits read scope", child.read_scope is ReadScope.ALL)
        check("child inherits read roots", child.read_roots == (extra,))
        confined_parent = Policy(workspace=ws, read_scope=ReadScope.WORKSPACE)
        check("child of confined parent stays confined",
              confined_parent.child().read_scope is ReadScope.WORKSPACE)

        # -- 6. config round-trip -------------------------------------------
        print("\n-- 配置往返 --")
        from .config import Config

        cfg = Config()
        cfg.apply_patch([{"id": "policy", "name": "policy:core", "config": {
            "mode": "acceptEdits", "sandbox": "workspace-write",
            "readScope": "all", "readRoots": [str(extra)]}}])
        built = Policy.from_config(cfg, workspace=ws)
        check("readScope read from config", built.read_scope is ReadScope.ALL)
        check("readRoots read from config", built.read_roots == (extra,))
        check("config-built policy reads outside", built.allows_read(outside_file))

        cfg2 = Config()
        cfg2.apply_patch([{"id": "policy", "name": "policy:core", "config": {
            "mode": "acceptEdits", "sandbox": "workspace-write"}}])
        check("absent readScope defaults to AUTO",
              Policy.from_config(cfg2, workspace=ws).read_scope is ReadScope.AUTO)

        # -- 7. --profile must not wipe an explicit read scope --------------
        print("\n-- profile 不覆盖读范围 --")
        from .cli import PROFILE_PRESETS, _apply_profile

        for name, preset in PROFILE_PRESETS.items():
            check(f"preset {name} states a readScope", "readScope" in preset)

        class _Args:
            profile = "balanced"
            i_know = True
            read_scope = None
            read_root = None

        cfg3 = Config()
        cfg3.apply_patch([{"id": "policy", "name": "policy:core",
                           "config": {"readScope": "all", "readRoots": [str(extra)]}}])
        _apply_profile(_Args(), cfg3)
        merged = dict(cfg3.row("policy").config)
        check("--profile balanced keeps readScope=all", merged.get("readScope") == "all",
              str(merged.get("readScope")))
        check("--profile balanced keeps readRoots", merged.get("readRoots") == [str(extra)])
        check("--profile balanced still applies sandbox",
              merged.get("sandbox") == "workspace-write")

        class _Args2(_Args):
            read_scope = "all"

        cfg4 = Config()
        _apply_profile(_Args2(), cfg4)
        check("--read-scope overrides the preset",
              dict(cfg4.row("policy").config).get("readScope") == "all")

        class _Args3(_Args):
            read_scope = None
            read_root = [str(extra)]

        cfg5 = Config()
        _apply_profile(_Args3(), cfg5)
        check("--read-root appended",
              dict(cfg5.row("policy").config).get("readRoots") == [str(extra)])

    total = len(_PASS) + len(_FAIL)
    print(f"\n{len(_PASS)}/{total} passed")
    if _FAIL:
        print("failed: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
