"""P0-1 修复验证：victim 在 workspace 外，five assertions"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, '.')
from revon.policy import Policy, Mode, Sandbox
from revon.tools import ToolContext, build_builtin_registry

with tempfile.TemporaryDirectory() as sb, tempfile.TemporaryDirectory() as outside:
    sb_path = Path(sb)
    outside_path = Path(outside)
    victim = outside_path / "victim.txt"
    victim.write_text("SAFE")
    ctx = ToolContext(
        policy=Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE,
                      workspace=sb_path, non_interactive=True),
        workspace=sb_path
    )
    registry = build_builtin_registry()

    # ① abs path oot in patches[].path
    r = registry.invoke("apply_patch", {"patches": [{"path": str(victim), "old": "SAFE", "new": "PWNED"}]}, ctx)
    label = "PASS" if not r.ok and victim.read_text() == "SAFE" else "FAIL - VULN"
    print(f"abs oot: ok={r.ok} content={victim.read_text()}  -> {label}")

    # ② ../ traversal
    escape = sb_path / ".." / "escape.txt"
    escape.write_text("SAFE2")
    r2 = registry.invoke("apply_patch", {"patches": [{"path": "../escape.txt", "old": "SAFE2", "new": "PWNED2"}]}, ctx)
    label2 = "PASS" if not r2.ok and escape.read_text() == "SAFE2" else "FAIL - VULN"
    print(f"rel traversal: ok={r2.ok} content={escape.read_text()}  -> {label2}")

    # ③ decoy
    decoy = sb_path / "decoy.txt"
    decoy.write_text("DECOY")
    r3 = registry.invoke("apply_patch", {"path": "decoy.txt",
        "patches": [{"path": str(victim), "old": "SAFE", "new": "PWNED3"}]}, ctx)
    label3 = "PASS" if not r3.ok and victim.read_text() == "SAFE" else "FAIL - VULN"
    print(f"decoy: ok={r3.ok} content={victim.read_text()}  -> {label3}")

    # ④ edits alias
    r4 = registry.invoke("apply_patch", {"edits": [{"path": str(victim), "old": "SAFE", "new": "PWNED4"}]}, ctx)
    label4 = "PASS" if not r4.ok and victim.read_text() == "SAFE" else "FAIL - VULN"
    print(f"edits alias: ok={r4.ok} content={victim.read_text()}  -> {label4}")

    # ⑤ read-only sandbox
    ro_ctx = ToolContext(
        policy=Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.READ_ONLY,
                      workspace=sb_path, non_interactive=True),
        workspace=sb_path
    )
    r5 = registry.invoke("apply_patch", {"patches": [{"path": str(victim), "old": "SAFE", "new": "PWNED-RO"}]}, ro_ctx)
    label5 = "PASS" if not r5.ok and victim.read_text() == "SAFE" else "FAIL - VULN"
    print(f"readonly: ok={r5.ok} content={victim.read_text()}  -> {label5}")

    all_pass = all([
        not r.ok and victim.read_text() == "SAFE",
        not r2.ok and escape.read_text() == "SAFE2",
        not r3.ok and victim.read_text() == "SAFE",
        not r4.ok and victim.read_text() == "SAFE",
        not r5.ok and victim.read_text() == "SAFE",
    ])
    print(f"\nResult: {'ALL 5 PASSED' if all_pass else 'SOME FAILED - see above'}")
    sys.exit(0 if all_pass else 1)
