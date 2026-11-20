#!/usr/bin/env python3
"""接缝与权限探针 —— 把 independent_audit.py 的第 4、5 组单独拎出来，可逐条复跑。

这个文件存在的意义：把"结论"变成"可当场重跑的命令"。任何人对下面任何一条有异议，
跑一遍就能看到同样的输出；不接受"我看过代码所以没问题"这种证据。

用法：cd agent-keystone && python verify/probe_seam.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from keystone.registry import ContribAPI, ModuleRegistry  # noqa: E402


def main() -> int:
    home = Path.home() / ".keystone"
    api = ContribAPI(home=home, workspace=Path.cwd())
    reg = ModuleRegistry(api, home / "contrib")
    reg.discover()
    reg.discover(ROOT / "keystone" / "contrib")
    mods = {c.name: c.module for c in reg.healthy()}
    mc, th = mods.get("mcp_bridge"), mods.get("toolhost")
    if mc is None or th is None:
        print("mcp_bridge / toolhost 未入库，探针无法进行")
        return 2

    print("A) mcp_bridge 产出的名字，过不过它自己的校验器？")
    disc = mc.discover({"name": "fs"}, [{"name": "read_file", "readOnlyHint": True}])
    produced = disc["tools"][0]["name"]
    print(f"   discover 产出        : {produced!r}  (read_only={disc['tools'][0]['read_only']})")
    print(f"   mcp_bridge 自己校验   : {mc._is_valid_name(produced)}   <- 产出与自家语法互斥")

    print("\nB) 交给 toolhost.register_tools：")
    out = th.register_tools([{"name": produced, "readOnlyHint": True}])
    print(f"   保留={[t['name'] for t in out['tools']]}  丢弃={[d['reason'] for d in out['dropped']]}")

    print("\nC) toolhost.authorize 在 read-only 模式下的裁决（名字决定沙箱是否生效）：")
    for tool in ["read_secrets", "list_and_wipe", "delete_file", "write_file", produced]:
        verdict = th.authorize(tool, "read-only", {}, None, str(Path.cwd()))
        print(f"   {tool:<16} -> {verdict['decision']:<5} {verdict['reason']}")

    print("\nD) 权威信号在接缝处被丢弃：")
    norm = th.register_tools([{"name": "read_secrets", "readOnlyHint": False}])
    print(f"   register_tools 依据 readOnlyHint=False 判 read_only={norm['tools'][0]['read_only']}")
    print(f"   同一次调用 authorize  -> {th.authorize('read_secrets', 'read-only', {}, None, str(Path.cwd()))['decision']}")

    print("\nE) 归一产物里的 read_only 没有入口传给 authorize：")
    import inspect
    print(f"   authorize 形参 = {list(inspect.signature(th.authorize).parameters)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
