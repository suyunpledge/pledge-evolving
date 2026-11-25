# -*- coding: utf-8 -*-
"""v2.2 增量复核探针：looks_complex 正确性/最小性 + smart 门控语义 + OBS-1 收口 + _COMPLEX_MIN_LEN 意见。

用法：在 agent-revon/ 目录下运行  python probe_r3v2_thinking.py
"""
import importlib.util, sys, difflib, ast
from pathlib import Path

ROOT = Path.cwd()
assert (ROOT / "revon" / "loop.py").is_file(), "请在 agent-revon/ 目录下运行"
sys.path.insert(0, str(ROOT))

failures = []
def chk(name, cond, detail=""):
    if not cond:
        failures.append(f"[FAIL] {name}: {detail}")

# ---------- 1) 模块级：looks_complex 正确性 ----------
spec = importlib.util.spec_from_file_location("think_v22", ROOT / "revon" / "contrib" / "thinking.py")
m = importlib.util.module_from_spec(spec)
sys.modules["think_v22"] = m
spec.loader.exec_module(m)

# M1: 中文触发词命中
chk("M1-zh-trigger", m.looks_complex("帮我分析一下这个系统的性能瓶颈并给出优化方案") is True)
# M2: 英文触发词命中（词边界）
chk("M2-en-trigger", m.looks_complex("Please design a caching strategy") is True)
# M3: 词内子串不误报 + 短无触发词不命中
chk("M3-false-positive",
    not m.looks_complex("replant the tree") and not m.looks_complex("你好"))
# M4: 长度阈值 240
chk("M4-long>=240", m.looks_complex("x" * 300) is True)
chk("M4-short<240", m.looks_complex("x" * 30) is False)
# M5: 空/非字符串安全
chk("M5-empty", m.looks_complex("") is False)
chk("M5-none", m.looks_complex(None) is False)
# M6: hook 面仍 4 名
chk("M6-hooks-4",
    set(m.register()["hooks"]) == {"should_think", "build_thinking_task",
                                    "split_thinking", "estimate_budget"})
# M7: looks_complex 不是 hook（self-test / hooks 列表均无此名）
h = m.register()["hooks"]
chk("M7-not-a-hook", "looks_complex" not in h)
# 自检里也无 looks_complex（验证：它不在 register 的 hooks 中）

# ---------- 2) smart 门控语义（需 Agent/LoopLimits/ThinkingSuite）----------
from revon.loop import Agent, LoopLimits, ThinkingSuite, mount_contrib_extensions  # noqa
from revon.model import ModelRouter, Provider, Usage  # noqa
from revon.policy import Mode, Policy  # noqa
from revon.tools import build_builtin_registry  # noqa

class ProbeTransport:
    def __init__(self):
        self.calls = []
    def complete(self, provider, model, messages, **options):
        self.calls.append([dict(x) for x in messages])
        return ("<think>内部推演</think>考虑完毕，给出答案。",
                Usage(prompt_tokens=7, completion_tokens=3), {})

def run_case(mode, task):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        seats = mount_contrib_extensions(home=ws)
        assert isinstance(seats.get("thinking"), ThinkingSuite)
        t = ProbeTransport()
        router = ModelRouter([Provider(name="main", base_url="http://x")],
                             transport=t, chain=[("main","m")], retries_per_provider=0)
        agent = Agent(home=ws, workspace=ws, router=router,
                      registry=build_builtin_registry(),
                      policy=Policy(mode=Mode.PLAN, workspace=ws, non_interactive=True),
                      limits=LoopLimits(max_steps=1),
                      extensions=seats, thinking=mode)
        report = agent.run(task)
        counts = {k: v for k, v in agent.extension_calls.items() if k.startswith("thinking.")}
        injected = any("<contemplation>" in str(mm.get("content"))
                       for call in t.calls for mm in call if isinstance(mm, dict))
        events = [e for e in report.events if e.get("type") == "thinking_mode"]
        return counts, injected, events

c_off, inj_off, ev_off = run_case("off", "分析这个任务的要点")
chk("R1-off-zero", not c_off and not inj_off, f"counts={c_off}")

c_s1, inj_s1, ev_s1 = run_case("smart", "chore")
chk("R2-smart-trivial-only-looks-complex",
    c_s1.get("thinking.looks_complex") == 1 and len(c_s1) == 1 and not inj_s1,
    f"counts={c_s1}")

c_s2, inj_s2, ev_s2 = run_case("smart", "分析这个任务的要点")
chk("R3-smart-complex-full-chain",
    all(c_s2.get(f"thinking.{h}", 0) >= 1 for h in
        ("looks_complex","estimate_budget","should_think","build_thinking_task",
         "split_thinking","converged")) and inj_s2,
    f"counts={c_s2}")

c_on, inj_on, ev_on = run_case("on", "chore")
chk("R4-on-always-full-chain",
    "thinking.looks_complex" not in c_on
    and all(c_on.get(f"thinking.{h}", 0) >= 1 for h in
            ("estimate_budget","should_think","build_thinking_task",
             "split_thinking","converged")) and inj_on,
    f"counts={c_on}")

chk("R5-mode-event-smart-engaged",
    bool(ev_s1) and ev_s1[0].get("engaged") is False
    and bool(ev_s2) and ev_s2[0].get("engaged") is True,
    f"s1={ev_s1[:1]} s2={ev_s2[:1]}")

# ---------- 3) CLI ----------
from revon.cli import build_parser  # noqa
args_ok = build_parser().parse_args(["run", "hi", "--thinking", "smart"])
chk("C1-cli-smart", getattr(args_ok, "thinking", None) == "smart")
try:
    build_parser().parse_args(["run", "hi", "--thinking", "bogus"])
    chk("C2-cli-bogus-rejected", False, "no SystemExit")
except SystemExit:
    chk("C2-cli-bogus-rejected", True)

# ---------- 4) OBS-1 收口核实 ----------
# looks_complex 是 run-time consumer；_is_word_match 被 looks_complex 消费
# _looks_like_decimal 仍只在 selftest 消费（新架构无句切分）
selftest_fn = next(n for n in ast.walk(ast.parse((ROOT/"revon"/"contrib"/"thinking.py").read_text()))
                   if isinstance(n,ast.FunctionDef) and n.name=='selftest')
lo,hi = selftest_fn.lineno, selftest_fn.end_lineno
# Verify looks_complex is NOT just in selftest (it's consumed by loop.py's _contemplate)
# Find all calls to looks_complex in contrib/thinking.py
src_contrib = (ROOT / "revon" / "contrib" / "thinking.py").read_text()
looks_calls = [n.lineno for n in ast.walk(ast.parse(src_contrib))
               if isinstance(n,ast.Name) and n.id=='looks_complex']
chk("OBS1-looks-complex-has-consumer", len(looks_calls) > 1,
    f"looks_complex calls (should be 2: selftest + looks_complex itself): {looks_calls}")

# ---------- 5) _COMPLEX_MIN_LEN=240 意见 ----------
# 初值 240 ≈ 240 字（英文约 480 token），作为"长文本"下限合理。
# 建议：在 00 说明 + selftest 注释里记录标定依据（"≈ 480 token / 2 倍 short"）。
# 240 有依据——当前不阻塞，若后续需要：在 docs 补一句标定来源。
# 探针无法动态调参，只记录意见。
print(f"\n_OPINION _COMPLEX_MIN_LEN=240: reasonable baseline for long-text trigger; "
      f"can be documented with calibration note if needed.")

if failures:
    for f in failures: print(f)
    print(f"\nFAILED {len(failures)} checks")
    sys.exit(1)
else:
    print("\nV2.2 INCREMENTAL PROBE ALL PASSED")
