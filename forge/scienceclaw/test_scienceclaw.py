"""Tests for the ScienceClaw capability port (forge/scienceclaw/).

Run:  python forge/scienceclaw/test_scienceclaw.py
Three groups matching the three ported capabilities.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forge.scienceclaw import (
    OffloadPolicy,
    PlanStep,
    ToolResultOffloader,
    extract_text,
    extract_thinking,
    make_summary,
    normalize_plan_steps,
    strip_think_tags,
)

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ── 1. offload ───────────────────────────────────────────────

def test_extract_text_shapes():
    check("str 直取", extract_text("hello world, " + "x" * 200) is not None)
    check("dict.content", extract_text({"content": "y" * 150}) == "y" * 150)
    check("短 dict 不触发", extract_text({"content": "short"}) is None)


def test_policy_thresholds():
    p = OffloadPolicy()
    check("aggressive 工具低阈值", p.threshold_for("web_search") == 3000)
    check("relaxed 工具高阈值", p.threshold_for("read_file") == 30000)
    check("未知工具双倍阈值", p.threshold_for("mystery") == 6000)
    check("应卸载：超阈值搜索结果", p.should_offload("web_search", "z" * 3001))
    check("不卸载：未超阈值", not p.should_offload("web_search", "z" * 3000))
    check("不卸载：文件工具 5k", not p.should_offload("read_file", "z" * 5000))


def test_offloader_wraps_and_writes():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        off = ToolResultOffloader(Path(td))
        big = "A" * 5000
        # web_search 超 3000 → 落盘 + 摘要
        out = off.wrap("web_search", "call-1", lambda: big)
        check("落盘触发", off.offload_count == 1, f"count={off.offload_count}")
        check("返回摘要含路径引用", "research_data" in out and "read_file" in out)
        check("摘要保留前缀预览", out.startswith("[完整结果已落盘"))
        target = Path(off.last_path)
        check("文件真实写入", target.exists() and target.read_text(encoding='utf-8') == big)
        # 小结果不动
        small = off.wrap("web_search", "call-2", lambda: "ok")
        check("小结果不卸载", small == "ok" and off.offload_count == 1)


def test_offloader_preserves_dict_shape():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        off = ToolResultOffloader(Path(td))
        payload = {"content": "B" * 4000, "meta": "keep-me"}
        out = off.wrap("web_search", "call-3", lambda: payload)
        check("dict 形状保留", isinstance(out, dict) and out.get("meta") == "keep-me")
        check("content 换成摘要", "[完整结果已落盘" in out["content"])


# ── 2. plan ──────────────────────────────────────────────────

def test_plan_normalisation():
    raw = [
        {"content": "第一步"},
        {"id": "s2", "content": "第二步", "status": "completed",
         "tools": ["grep"], "priority": "high"},
    ]
    steps = normalize_plan_steps(raw)
    check("缺省 id 补位", steps[0]["id"] == "step-1")
    check("缺省 status=pending", steps[0]["status"] == "pending")
    check("缺省 priority=medium", steps[0]["priority"] == "medium")
    check("显式字段保留", steps[1]["id"] == "s2" and steps[1]["status"] == "completed")
    check("tools/files 补空表", steps[0]["tools"] == [] and steps[0]["files"] == [])
    check("created_at 是 int", isinstance(steps[0]["created_at"], int))


def test_plan_step_typeddict_roundtrip():
    steps = normalize_plan_steps([{"content": "x"}])
    again = normalize_plan_steps(steps)  # idempotent on its own output
    check("归一化幂等", again == steps)


# ── 3. thinking ──────────────────────────────────────────────

def test_thinking_deepseek_reasoning_content():
    msg = {"content": "正文回答", "additional_kwargs": {"reasoning_content": "让我想想…"}}
    thinking, clean = extract_thinking(msg)
    check("DeepSeek 思考取回", thinking == "让我想想…")
    check("正文干净", clean == "正文回答")


def test_thinking_claude_blocks():
    msg = {"content": [
        {"type": "thinking", "thinking": "先分析需求"},
        {"type": "text", "text": "这是答案"},
    ]}
    thinking, clean = extract_thinking(msg)
    check("Claude 思考取回", thinking == "先分析需求")
    check("Claude 正文干净", clean == "这是答案")


def test_thinking_think_tags():
    thinking, clean = extract_thinking({"content": "<think>推理过程</think>结论在此"})
    check("think 标签思考取回", thinking == "推理过程")
    check("think 标签剥除", clean == "结论在此")
    check("strip 助手", strip_think_tags("<think>a</think>b") == "b")


def test_thinking_plain_text_passthrough():
    thinking, clean = extract_thinking({"content": "普通回答"})
    check("无思考格式直通", thinking == "" and clean == "普通回答")


def test_thinking_attribute_message():
    class Msg:
        content = "<think>attr 思考</think>attr 正文"
        additional_kwargs = {}
    thinking, clean = extract_thinking(Msg())
    check("对象消息属性访问", thinking == "attr 思考" and clean == "attr 正文")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed")
    sys.exit(1 if FAIL else 0)
