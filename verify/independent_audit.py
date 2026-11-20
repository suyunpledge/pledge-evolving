#!/usr/bin/env python3
"""keystone 独立验证器 —— 第三方验证，不修改被验证的框架代码。

由 deepseek 实例（辅助迭代方）提供。它不重复作者的自检，只回答作者自检
结构上回答不了的三个问题：

  1. 数字对不对：README / PLAN 里写死的模块数、断言数、自检数，与实测是否一致；
  2. 证据实不实：164 条断言里有多少条是真跑出来的行，有多少条是闸门在"模块自检
     没有任何输出"时自己补出来的通过行；
  3. 接缝通不通：各 contrib 模块自己的自检都绿，但它们之间的数据契约是否对得上
     （mcp_bridge 产出的工具名，toolhost 收不收；toolhost 判定权限依据什么）。

用法（在 agent-keystone 根目录执行）：
    python verify/independent_audit.py
    python verify/independent_audit.py --json

退出码：0 = 全通过；1 = 有 FAIL；2 = 只有 WARN。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_HOME = Path.home() / ".keystone"
WORKSPACE = str(Path.cwd())

RESULTS: list[dict] = []


def record(section: str, rid: str, title: str, verdict: str, evidence: str) -> None:
    RESULTS.append({"section": section, "id": rid, "title": title,
                    "verdict": verdict, "evidence": evidence})


def build_registry():
    from keystone.registry import ContribAPI, ModuleRegistry

    api = ContribAPI(home=DEFAULT_HOME, workspace=Path(WORKSPACE))
    reg = ModuleRegistry(api, DEFAULT_HOME / "contrib")
    try:
        reg.discover()
    except Exception as exc:  # noqa: BLE001
        record("0", "setup:home-contrib", "扫描用户 contrib 目录", "WARN", f"{type(exc).__name__}: {exc}")
    reg.discover(ROOT / "keystone" / "contrib")
    return reg


def run_modules_validate(root: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "run.py", "modules", "validate"],
        cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def summary_of(text: str) -> dict:
    m = re.search(r"contributed=(\d+)\s+healthy=(\d+)\s+assertions=(\d+)", text)
    if not m:
        return {"contributed": None, "healthy": None, "assertions": None}
    return {"contributed": int(m.group(1)), "healthy": int(m.group(2)), "assertions": int(m.group(3))}


def module_rows(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[ok") or ln.strip().startswith("[FAIL")]


# ---------------------------------------------------------------------------
# 1. 声明数字 vs 实测
# ---------------------------------------------------------------------------

def section_claims() -> None:
    readme = (ROOT / "readme.md").read_text(encoding="utf-8", errors="replace") if (ROOT / "readme.md").exists() else ""
    plan = (ROOT / "plan.md").read_text(encoding="utf-8", errors="replace") if (ROOT / "plan.md").exists() else ""

    claimed_checks = None
    m = re.search(r"离线跑\s*\*\*(\d+)\s*项", readme)
    if m:
        claimed_checks = int(m.group(1))

    live = run_modules_validate(ROOT)
    live_sum = summary_of(live)

    proc = subprocess.run([sys.executable, "run.py", "selftest"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    text = (proc.stdout or "") + (proc.stderr or "")
    # 输出里可能嵌入子进程自己的 "N/N checks passed"，取最后一处（即总账）
    hits = re.findall(r"(\d+)/(\d+)\s+checks passed", text)
    real_checks = int(hits[-1][1]) if hits else None
    nested = [h[0] for h in hits[:-1]]

    record("1", "claims:selftest", "自检项数（README 声明 vs 实测）",
           "PASS" if (claimed_checks is None or claimed_checks == real_checks) else "FAIL",
           f"README={claimed_checks} 实测={real_checks} 退出码={proc.returncode}"
           + (f"（另有嵌套自检行 {nested}）" if nested else ""))

    # 契约要求的模块数：brief 里点名 10 个 legacy 模块 + 参考实现
    claimed_mods = len(re.findall(r"^##\s*给", (ROOT / "briefs-round3.md").read_text(encoding="utf-8", errors="replace"), re.M)) \
        if (ROOT / "briefs-round3.md").exists() else None
    record("1", "claims:modules", "贡献模块数（实测）",
           "PASS" if live_sum["contributed"] else "FAIL",
           f"contributed={live_sum['contributed']} healthy={live_sum['healthy']} assertions={live_sum['assertions']} "
           f"(round3 任务书点名 {claimed_mods} 份)")

    # 每条任务书都要求「自检至少 10 条」
    if live_sum["assertions"] is not None and live_sum["contributed"]:
        avg = live_sum["assertions"] / live_sum["contributed"]
        record("1", "claims:assertion-floor", "单模块断言均值 vs 任务书下限 10",
               "WARN" if avg < 10 else "PASS",
               f"均值={avg:.1f}（{live_sum['assertions']}/{live_sum['contributed']}）；任务书要求每份 ≥10 条")


# ---------------------------------------------------------------------------
# 2. 证据质量：有没有"零证据通过"
# ---------------------------------------------------------------------------

EVIDENCE_FREE_MARKERS = ("no output", "produced no output", "no verdict", "no rows")


def section_evidence(reg) -> None:
    from keystone.registry import ADAPTER_MIN_ASSERTIONS

    healthy = reg.healthy()
    total_rows = 0
    empty_rows: list[tuple[str, str]] = []
    legacy_mods: list[str] = []

    for contrib in healthy:
        rows = list(contrib.assertions or [])
        total_rows += len(rows)
        if getattr(contrib, "adapter", "") == "legacy-script":
            legacy_mods.append(contrib.name)
        for name, ok, detail in rows:
            blob = f"{name} {detail}".lower()
            if any(marker in blob for marker in EVIDENCE_FREE_MARKERS):
                empty_rows.append((contrib.name, str(name)))

    record("2", "evidence:total-rows", "健康模块的断言总行数",
           "PASS", f"{total_rows} 行，来自 {len(healthy)} 份模块；legacy 适配 {len(legacy_mods)} 份"
                   f"（{', '.join(sorted(legacy_mods))}）")

    if empty_rows:
        detail = "; ".join(f"{mod}::{name}" for mod, name in empty_rows)
        record("2", "evidence:zero-output-pass",
               "存在「无输出 ⇒ 判 pass」的合成通过行",
               "FAIL",
               f"{len(empty_rows)}/{total_rows} 行没有任何可核验内容：{detail}")
    else:
        record("2", "evidence:zero-output-pass", "存在「无输出 ⇒ 判 pass」的合成通过行",
               "PASS", f"0/{total_rows}")

    # 有效断言密度：扣掉证据真空行之后，是否仍有 ≥10 条/模块
    if healthy:
        effective = (total_rows - len(empty_rows)) / len(healthy)
        record("2", "evidence:effective-density", "扣除真空行后的单模块断言密度",
               "WARN" if effective < 10 else "PASS",
               f"有效行/模块={effective:.1f}")
    record("2", "evidence:adapter-floor", "legacy 适配的最低断言门槛",
           "WARN" if ADAPTER_MIN_ASSERTIONS < 8 else "PASS",
           f"ADAPTER_MIN_ASSERTIONS={ADAPTER_MIN_ASSERTIONS}（契约模块门槛 MIN_ASSERTIONS=8）")


# ---------------------------------------------------------------------------
# 3. 闸门对抗：作者交空测试 / 交坏实现，闸门分不分得出来
# ---------------------------------------------------------------------------

EXPLOIT_SUFFIX = '''

# --- verifier injection: 模拟"作者交了空自检" ---
def selftest() -> List[str]:
    """verifier: 一个什么都不检查的空测试。"""
    return []
'''

BROKEN_SUFFIX = '''

# --- verifier injection: 破坏一处真实行为（只读启发恒为 True）---
def _guess_read_only(name: str) -> bool:
    return True
'''


def _mutated_run(tmp_root: Path, module_file: str, suffix: str, label: str) -> dict:
    dst = tmp_root / label
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".openclaw_tmp_out.txt"))
    target = dst / "keystone" / "contrib" / module_file
    target.write_text(target.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    text = run_modules_validate(dst)
    return {"summary": summary_of(text), "rows": module_rows(text), "text": text}


def section_gate() -> None:
    baseline = summary_of(run_modules_validate(ROOT))
    with tempfile.TemporaryDirectory(prefix="keystone-gate-") as tmp:
        tmp_root = Path(tmp)
        try:
            empty = _mutated_run(tmp_root, "toolhost.py", EXPLOIT_SUFFIX, "empty-test")
            broken = _mutated_run(tmp_root, "toolhost.py", BROKEN_SUFFIX, "broken-impl")
        except Exception as exc:  # noqa: BLE001
            record("3", "gate:setup", "闸门对抗环境", "WARN", f"{type(exc).__name__}: {exc}")
            return

    e = empty["summary"]
    toolhost_admitted = any("toolhost" in ln for ln in empty["rows"])
    record("3", "gate:empty-selftest",
           "把作者自检换成空实现后，闸门是否仍然放行",
           "FAIL" if (toolhost_admitted and e["healthy"] == baseline["healthy"]) else "PASS",
           f"基线 healthy={baseline['healthy']} assertions={baseline['assertions']}；"
           f"空测试 healthy={e['healthy']} assertions={e['assertions']}；toolhost 仍入库={toolhost_admitted}")

    b = broken["summary"]
    broke_detected = b["healthy"] < baseline["healthy"] or any("FAIL" in ln and "toolhost" in ln for ln in broken["rows"])
    fail_rows = [ln for ln in broken["rows"] if ln.strip().startswith("[FAIL")]
    record("3", "gate:broken-impl",
           "破坏一处真实行为后，闸门是否拦得住（正对照）",
           "PASS" if broke_detected else "FAIL",
           f"破坏后 healthy={b['healthy']}（基线 {baseline['healthy']}）；"
           f"隔离行={' / '.join(fail_rows)[:300] or '(无)'}")


# ---------------------------------------------------------------------------
# 4. 接缝：mcp_bridge 的产物，toolhost 收不收
# ---------------------------------------------------------------------------

def section_seam(reg) -> None:
    mods = {c.name: c.module for c in reg.healthy()}
    mc, th = mods.get("mcp_bridge"), mods.get("toolhost")
    if mc is None or th is None:
        record("4", "seam:setup", "接缝探针依赖 mcp_bridge + toolhost", "WARN",
               f"缺失：{'mcp_bridge ' if mc is None else ''}{'toolhost' if th is None else ''}")
        return

    raw_tool = {"name": "read_file", "description": "reads",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "readOnlyHint": True}
    disc = mc.discover({"name": "fs"}, [raw_tool])
    produced = [t["name"] for t in disc.get("tools", [])]
    as_read_only = [t.get("read_only") for t in disc.get("tools", [])]
    record("4", "seam:mcp-name-shape", "mcp_bridge.discover 产出的工具名形态",
           "PASS" if produced else "FAIL", f"name={produced} read_only={as_read_only}")

    reg_out = th.register_tools([dict(raw_tool, name=produced[0]) if produced else raw_tool])
    kept = [t["name"] for t in reg_out.get("tools", [])]
    dropped = reg_out.get("dropped", [])
    record("4", "seam:toolhost-accepts-mcp",
           "toolhost.register_tools 是否接受 mcp_bridge 的产物",
           "FAIL" if (produced and not kept) else "PASS",
           f"输入={produced} → 保留={kept} 丢弃={[d.get('reason') for d in dropped]}")

    # 只读语义是否被前缀破坏
    plain = th.register_tools([{"name": "read_file"}])
    pref = th.register_tools([{"name": produced[0]}]) if produced else {"tools": []}
    p_read = plain["tools"][0]["read_only"] if plain.get("tools") else None
    f_read = pref["tools"][0]["read_only"] if pref.get("tools") else None
    flipped = (p_read is True and f_read is False)
    record("4", "seam:readonly-flip",
           "加 server 前缀后只读分类是否翻转",
           "FAIL" if flipped else ("WARN" if f_read is None else "PASS"),
           f"read_file -> read_only={p_read}；{produced[0] if produced else '(无)'} -> read_only={f_read}")


# ---------------------------------------------------------------------------
# 5. 权限判定依据：名字还是声明
# ---------------------------------------------------------------------------

def section_permission(reg) -> None:
    mods = {c.name: c.module for c in reg.healthy()}
    th = mods.get("toolhost")
    if th is None:
        record("5", "perm:setup", "权限探针依赖 toolhost", "WARN", "toolhost 未入库")
        return

    cases = [
        ("write tool named read_*", "read_secrets", "default"),
        ("write tool named list_*", "list_and_wipe", "default"),
        ("honest write tool", "write_file", "default"),
        ("prefixed read tool", "fs/read_file", "default"),
    ]
    lines = []
    for label, tool, mode in cases:
        try:
            out = th.authorize(tool, mode, {}, None, WORKSPACE)
            lines.append(f"{label}: {tool} -> {out.get('decision')}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{label}: {tool} -> {type(exc).__name__}")

    escape = [ln for ln in lines if ln.startswith(("write tool named", "prefixed read tool"))
              and "-> allow" in ln]
    record("5", "perm:name-decides",
           "权限判定是否只看名字（声明 readOnlyHint 是否被采信）",
           "FAIL" if escape else "WARN",
           "; ".join(lines))

    hinted = th.register_tools([{"name": "read_everything", "readOnlyHint": False}])
    hint_read = hinted["tools"][0]["read_only"] if hinted.get("tools") else None
    record("5", "perm:hint-honoured",
           "register_tools 是否采信 readOnlyHint=False",
           "PASS" if hint_read is False else "WARN",
           f"read_everything + readOnlyHint=False -> read_only={hint_read}")

    # 归一结果里带了 read_only，authorize 却拿不到这份信息
    try:
        import inspect
        params = list(inspect.signature(th.authorize).parameters)
    except Exception:  # noqa: BLE001
        params = []
    record("5", "perm:signature-gap",
           "authorize 是否可能消费归一后的 read_only 字段",
           "WARN" if "spec" not in params and "read_only" not in params else "PASS",
           f"authorize 形参={params}（归一产物里的 read_only/readOnlyHint 无处传入）")


# ---------------------------------------------------------------------------
# 6. 钩子可达性 / 能力漂移（跨模块）
# ---------------------------------------------------------------------------

def section_hooks(reg) -> None:
    broken = []
    total = 0
    for contrib in reg.healthy():
        for canon in contrib.hooks:
            total += 1
            if contrib.implementation(canon) is None:
                broken.append(f"{contrib.name}.{canon}")
    record("6", "hooks:reachability", "声明钩子的可达性",
           "PASS" if not broken else "FAIL",
           f"{total} 个钩子，不可达 {len(broken)} 个" + (f"：{', '.join(broken)}" if broken else ""))

    caps: dict[str, list[str]] = {}
    for contrib in reg.healthy():
        for cap in contrib.capabilities:
            caps.setdefault(cap, []).append(contrib.name)
    dup = {c: v for c, v in caps.items() if len(v) > 1}
    record("6", "hooks:capability-overlap", "能力标签重合（多实现并存）",
           "PASS" if dup else "WARN",
           f"{len(caps)} 个标签；重合 {len(dup)} 个" + (f"：{dup}" if dup else ""))


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    reg = build_registry()
    section_claims()
    section_evidence(reg)
    section_gate()
    section_seam(reg)
    section_permission(reg)
    section_hooks(reg)

    fails = [r for r in RESULTS if r["verdict"] == "FAIL"]
    warns = [r for r in RESULTS if r["verdict"] == "WARN"]

    if args.json:
        print(json.dumps({"results": RESULTS, "fail": len(fails), "warn": len(warns)},
                         ensure_ascii=False, indent=2))
    else:
        current = None
        for r in RESULTS:
            if r["section"] != current:
                current = r["section"]
                print(f"\n## 第 {current} 组")
            print(f"  [{r['verdict']:<4}] {r['id']:<28} {r['title']}")
            print(f"         {r['evidence']}")
        print(f"\n合计：{len(RESULTS)} 项检查，FAIL {len(fails)}，WARN {len(warns)}")

    return 1 if fails else (2 if warns else 0)


if __name__ == "__main__":
    raise SystemExit(main())
