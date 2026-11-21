"""3D modelling before/after: the same tasks with and without the model3d tools.

Default (offline): a scripted model drives the real Agent loop through the
"40x40x10 mm plate with a 20 mm round hole" task — first spec puts the cutter
at the corner, the tool's own checks flag it (through_holes want 1, got 0), the
second call fixes it. Proves install → tool surface → Policy → files → checks.

``--live``: real model, two arms per task, fresh workspace per run:
  before — today's surface minus model3d_build/model3d_inspect and the skill
           (the model has write_file and must author STL/OBJ text itself);
  after  — default surface (model3d tools + bundled model3d skill).
Every produced file is graded independently by re-parsing it from disk.
Credentials come from the user's existing MiMo provider row (as in
verify_mimo_live.py); they are never printed or copied.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keystone import model as kmodel                       # noqa: E402
from keystone.capability import CapabilityLibrary, default_roots  # noqa: E402
from keystone.loop import Agent                             # noqa: E402
from keystone.model import Usage                            # noqa: E402
from keystone.model3d import analyze, decode, format_for   # noqa: E402
from keystone.policy import Mode, Policy, Sandbox          # noqa: E402
from keystone.runtime_types import LoopLimits              # noqa: E402
from keystone.session import Session                       # noqa: E402
from keystone.tools import build_builtin_registry          # noqa: E402

TASKS = {
    "plate": {
        "text": "做一个用于 3D 打印的零件：40×40×10 mm 的方形板，正中间开一个直径 20 mm 的圆形通孔。"
                "导出为 STL 文件保存到 models/plate.stl。",
        "file": "models/plate.stl",
        "expect": {"size": [40, 40, 10], "through_holes": 1, "tolerance": 0.05},
    },
    "gear": {
        "text": "Make a 3D-printable spur gear: 12 teeth, module 2 (so outer diameter 28 mm), 8 mm thick, "
                "with a 5 mm round bore through the centre. Save it as models/gear.stl.",
        "file": "models/gear.stl",
        "expect": {"size": [28, 28, 8], "through_holes": 1, "tolerance": 0.6},
    },
    "stand": {
        "text": "Design a simple phone stand I can 3D print: a 70 mm wide stand with a flat base plate, "
                "a back support leaning at about 60-70 degrees, and a front lip so the phone does not slide. "
                "It must be one solid piece. Save it as models/phone_stand.stl.",
        "file": "models/phone_stand.stl",
        "expect": {"shells": 1},
        "size_hint": {"x_or_y": 70},
    },
}


def grade(ws: Path, task: dict) -> dict:
    path = ws / task["file"]
    if not path.is_file():
        # a model may pick another name/format: grade the newest mesh it wrote
        found = sorted((p for p in ws.rglob("*") if p.suffix.lower() in (".stl", ".obj", ".glb", ".gltf")),
                       key=lambda p: p.stat().st_mtime)
        if not found:
            return {"file": None, "ok": False, "why": "no mesh file written"}
        path = found[-1]
    try:
        mesh = decode(path.read_bytes(), format_for(path))
    except Exception as exc:  # noqa: BLE001 — grading must never crash the run
        return {"file": path.name, "ok": False, "why": f"unparseable: {exc}"[:200]}
    report = analyze(mesh, expect=task["expect"])
    failed = [f"{r['check']}: want {r['want']} got {r['got']}" for r in report.get("expect", []) if not r["ok"]]
    if not report.get("watertight"):
        failed.append(f"not watertight ({report.get('boundary_edges')} open, "
                      f"{report.get('nonmanifold_edges')} non-manifold edges)")
    if report.get("inverted"):
        failed.append("inverted normals")
    if report.get("orientation_conflicts"):
        failed.append(f"{report['orientation_conflicts']} orientation conflicts")
    hint = task.get("size_hint", {}).get("x_or_y")
    if hint and report.get("bbox"):
        sx, sy, _ = report["bbox"]["size"]
        if min(abs(sx - hint), abs(sy - hint)) > 1.0:
            failed.append(f"width: want {hint} on x or y, got {sx} x {sy}")
    return {"file": path.relative_to(ws).as_posix(), "ok": not failed, "why": "; ".join(failed),
            "triangles": report.get("triangles"), "size": report.get("bbox", {}).get("size"),
            "through_holes": report.get("through_holes"), "watertight": report.get("watertight"),
            "shells": report.get("shells")}


def make_agent(arm: str, ws: Path, home: Path, router, max_steps: int) -> Agent:
    registry = build_builtin_registry()
    if arm == "before":
        for name in ("model3d_build", "model3d_inspect"):
            registry.unregister(name)
        caps = CapabilityLibrary([home / "capabilities"])
    else:
        caps = CapabilityLibrary(default_roots(home))
    caps.scan()
    policy = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws, non_interactive=True)
    session = Session(home / "sessions" / f"{arm}-{ws.name}.jsonl", meta={"cwd": str(ws), "kind": "model3d-ab"})
    return Agent(home=home, workspace=ws, router=router, registry=registry, policy=policy, session=session,
                 capabilities=caps, limits=LoopLimits(max_steps=max_steps), wire_hint="openai")


def run_once(arm: str, task_name: str, router, root: Path, max_steps: int) -> dict:
    task = TASKS[task_name]
    ws = root / f"{task_name}-{arm}-{int(time.time() * 1000) % 10**8}"
    ws.mkdir(parents=True)
    home = root / "home"
    agent = make_agent(arm, ws, home, router, max_steps)
    started = time.time()
    with agent.session:
        report = agent.run(task["text"])
    tools = [s.tool for s in report.steps if s.tool]
    usage = report.usage or {}
    return {"arm": arm, "task": task_name, "stopped": report.stopped, "seconds": round(time.time() - started, 1),
            "tool_calls": len(tools), "tools": tools,
            "failed_tool_calls": sum(1 for s in report.steps if s.tool and s.decision not in ("ok", "allow")),
            "model_requests": len(usage["calls"]) if isinstance(usage.get("calls"), list) else usage.get("requests"),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "grade": grade(ws, task), "answer": (report.text or "")[:300]}


# -- offline scripted demo -----------------------------------------------------

class ScriptedModeller:
    """Plays a model that misplaces the hole first, then follows the tool's feedback."""

    def __init__(self) -> None:
        self.turns = 0
        self.seen: list[str] = []

    def complete(self, provider, model, messages, **options):
        self.turns += 1
        results = [m for m in messages if m.get("role") == "tool"]
        if results:
            self.seen.append(str(results[-1].get("content", "")))
        hole = {"op": "cylinder", "d": 20, "h": 12, "translate": [0, 0, -1]}       # wrong: at the corner
        if len(results) == 1 and "through_holes: want 1, got 0 [FAIL]" in self.seen[-1]:
            hole = {"op": "cylinder", "d": 20, "h": 12, "translate": [20, 20, -1]}  # fixed from feedback
        elif len(results) >= 2:
            return ("已生成 models/plate.stl：40×40×10 mm，中心 Ø20 mm 通孔，水密校验通过。",
                    Usage(prompt_tokens=900, completion_tokens=40), {})
        args = {"path": "models/plate.stl", "expect": {"size": [40, 40, 10], "through_holes": 1},
                "spec": {"units": "mm", "model": {"op": "difference", "children": [
                    {"op": "box", "size": [40, 40, 10]}, hole]}}}
        call = {"id": f"call_{self.turns}", "name": "model3d_build", "args": args}
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": call["id"], "type": "function",
            "function": {"name": "model3d_build", "arguments": json.dumps(args)}}]}
        return "", Usage(prompt_tokens=800, completion_tokens=120), {
            "tool_calls": [call], "wire": "openai", "assistant_message": message}


def scripted_demo(root: Path) -> dict:
    transport = ScriptedModeller()
    provider = kmodel.Provider("scripted", "http://127.0.0.1:9", "unused", "openai",
                               ("scripted",), "scripted", "scripted")
    router = kmodel.ModelRouter([provider], transport=transport, chain=[("scripted", "scripted")])
    row = run_once("after", "plate", router, root, 8)
    row["feedback_seen"] = [text.splitlines()[0] for text in transport.seen]
    return row


def live_router(model_name: str, home: Path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_mimo_live import provider_for
    provider = provider_for(model_name, home)
    return kmodel.ModelRouter([provider], transport=kmodel.HttpTransport(timeout=180),
                              chain=[(provider.name, model_name)], retries_per_provider=1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="call the real model (MiMo Token Plan)")
    parser.add_argument("--model", default="mimo-v2.6-flash")
    parser.add_argument("--home", default=str(Path.home() / ".forge"),
                        help="profile holding the provider rows + secrets (read only)")
    parser.add_argument("--tasks", default="plate,gear")
    parser.add_argument("--arms", default="before,after")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="model3d-ab-"))
    rows = []
    if not args.live:
        row = scripted_demo(root)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=1))
    else:
        router = live_router(args.model, Path(args.home))
        for task in args.tasks.split(","):
            for run in range(args.runs):
                for arm in args.arms.split(","):
                    row = run_once(arm, task, router, root, args.max_steps)
                    row["run"] = run
                    rows.append(row)
                    g = row["grade"]
                    print(f"{task:<6} {arm:<6} run{run} stopped={row['stopped']:<9} tools={row['tool_calls']:<3} "
                          f"{row['seconds']:>6}s grade={'PASS' if g['ok'] else 'FAIL'} {g.get('why', '')[:90]}",
                          flush=True)
    summary = {}
    for row in rows:
        key = f"{row['task']}/{row['arm']}"
        s = summary.setdefault(key, {"runs": 0, "pass": 0, "tool_calls": 0, "seconds": 0.0, "final": 0})
        s["runs"] += 1
        s["pass"] += int(row["grade"]["ok"])
        s["final"] += int(row["stopped"] == "final")
        s["tool_calls"] += row["tool_calls"]
        s["seconds"] += row["seconds"]
    for s in summary.values():
        s["avg_tool_calls"] = round(s.pop("tool_calls") / s["runs"], 1)
        s["avg_seconds"] = round(s.pop("seconds") / s["runs"], 1)
    out = {"date": datetime.now().isoformat(timespec="seconds"), "live": args.live,
           "model": args.model if args.live else "scripted", "summary": summary, "runs": rows}
    target = Path(args.out) if args.out else ROOT / "reports" / (
        f"model3d-ab-{'live' if args.live else 'scripted'}-{datetime.now():%Y%m%d-%H%M}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"report: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
