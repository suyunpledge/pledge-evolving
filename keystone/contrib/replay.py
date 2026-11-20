# -*- coding: utf-8 -*-
"""keystone.contrib.replay —— 会话回放：把 append-only 事件流变成可读时间线。

会话文件是一份 append-only JSONL，每行一个 JSON 事件，形如::

    {"ordinal": 3, "type": "tool_call", "ts": 1757..., "tool": "read_file",
     "args": {"path": "a.txt"}, "ok": true, "result": "..."}

首行约定为 ``{"ordinal": 0, "type": "session_meta", ...}``，``ordinal`` 严格递增。
常见 ``type``：user_message / assistant_message / tool_call / tool_decision /
subagent_spawn / subagent_result / compaction / checkpoint / loop_stop / model_error。

对外只暴露三个纯函数：

- :func:`replay`     事件流 → 人话时间线 + 统计；``until_ordinal`` 可截断回放。
- :func:`fork_plan`  计算「从尾部保留 N 条历史」的分叉计划；session_meta 永远剔除。
- :func:`diff_runs`  按 ``(type, tool)`` 序列对齐两次运行，给出首个分歧点与工具差异。

健壮性约定：畸形事件（非 dict、缺 type、缺 ordinal、ordinal 非法或乱序）一律
安全跳过并计入 ``malformed``，绝不因事件数据抛异常；``ts`` 等其余字段不参与摘要。

直接运行本文件可执行内置自检::

    python keystone/contrib/replay.py
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable

MODULE_API_VERSION = 1

__all__ = ["replay", "fork_plan", "diff_runs", "register", "selftest"]

_PREVIEW_LIMIT = 80

# 已知事件类型 → 人话标签；tool_call / tool_decision 需结合字段单独生成。
_LABELS = {
    "session_meta": "会话开始",
    "user_message": "用户提问",
    "assistant_message": "助手回复",
    "subagent_spawn": "分派子代理",
    "subagent_result": "子代理返回",
    "compaction": "压缩上下文",
    "checkpoint": "保存检查点",
    "loop_stop": "循环停止",
    "model_error": "模型错误",
}

# 各家事件流对 token 用量的字段命名不一，这里归一到统一键名再累加。
_USAGE_ALIASES = {
    "input_tokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "generated_tokens": "output_tokens",
    "cached_tokens": "cached_tokens",
    "cache_read_tokens": "cached_tokens",
    "cache_read_input_tokens": "cached_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "thinking_tokens": "reasoning_tokens",
    "total_tokens": "total_tokens",
}
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)

_APPROVE_WORDS = {"approve", "approved", "allow", "allowed",
                  "accept", "accepted", "yes", "grant", "granted"}
_REJECT_WORDS = {"reject", "rejected", "deny", "denied",
                 "refuse", "refused", "no", "block", "blocked"}


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _is_int(value) -> bool:
    """是 int 且不是 bool（Python 里 bool 是 int 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_event(ev, last_ordinal):
    """校验单个事件，返回 ``(是否有效, 新的 last_ordinal)``。

    乱序/重复的判定基准是「上一个有效事件」的 ordinal；ordinal 合法但缺 type
    的事件不计入基准（事件本身不可信）。
    """
    if not isinstance(ev, dict):
        return False, last_ordinal
    ordinal = ev.get("ordinal")
    etype = ev.get("type")
    if not _is_int(ordinal) or ordinal < 0:
        return False, last_ordinal
    if not isinstance(etype, str) or not etype:
        return False, last_ordinal
    if ordinal <= last_ordinal:
        return False, last_ordinal
    return True, ordinal


def _preview(text, limit: int = _PREVIEW_LIMIT) -> str:
    """压平所有空白并按长度截断，供 detail / reason 使用。"""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)] + "…"


def _tool_name(ev) -> str:
    """尽量给出可读的工具名；缺失返回空串，非字符串兜底转 str。"""
    tool = ev.get("tool")
    if isinstance(tool, str):
        return tool
    if tool is not None:
        return str(tool)
    return ""


def _summarize_args(args: dict, max_items: int = 4) -> str:
    parts = [f"{key}={_preview(str(value), 24)}"
             for key, value in list(args.items())[:max_items]]
    if len(args) > max_items:
        parts.append("…")
    return ", ".join(parts)


def _label_for(ev: dict) -> str:
    etype = ev["type"]
    if etype == "tool_call":
        name = _tool_name(ev)
        label = f"调用 {name}" if name else "调用工具"
        if ev.get("ok") is False:
            label += "（失败）"
        return label
    if etype == "tool_decision":
        decision = ev.get("decision")
        if decision is None and "approved" in ev:
            decision = "approve" if ev.get("approved") else "reject"
        if isinstance(decision, str):
            word = decision.strip().lower()
            if word in _APPROVE_WORDS:
                return "批准工具调用"
            if word in _REJECT_WORDS:
                return "拒绝工具调用"
        return "工具决策"
    return _LABELS.get(etype, f"未知事件（{etype}）")


def _detail_for(ev: dict) -> str:
    """按事件类型挑出最有信息量的内容，拼成一句人话摘要；没有就返回空串。"""
    etype = ev["type"]
    parts: list[str] = []

    if etype in ("user_message", "assistant_message"):
        text = ev.get("text")
        if text is None:
            text = ev.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(_preview(text))

    elif etype == "tool_call":
        args = ev.get("args")
        if isinstance(args, dict) and args:
            parts.append("参数 " + _preview(_summarize_args(args)))
        if ev.get("ok") is False:
            err = ev.get("error")
            if not (isinstance(err, str) and err.strip()):
                err = ev.get("result")
            if isinstance(err, str) and err.strip():
                parts.append("错误 " + _preview(err))

    elif etype == "tool_decision":
        if _tool_name(ev):
            parts.append("工具 " + _tool_name(ev))

    elif etype == "subagent_spawn":
        name = ev.get("agent") or ev.get("name") or ev.get("agent_type")
        if isinstance(name, str) and name.strip():
            parts.append("代理 " + name)
        task = ev.get("task") or ev.get("prompt")
        if isinstance(task, str) and task.strip():
            parts.append(_preview(task))

    elif etype == "subagent_result":
        result = ev.get("result")
        if isinstance(result, str) and result.strip():
            parts.append(_preview(result))

    elif etype == "compaction":
        before, after = ev.get("before_tokens"), ev.get("after_tokens")
        if _is_num(before) and _is_num(after):
            parts.append(f"token {before:g} → {after:g}")

    elif etype == "checkpoint":
        name = ev.get("name") or ev.get("label") or ev.get("id")
        if isinstance(name, str) and name.strip():
            parts.append("检查点 " + name)

    elif etype == "loop_stop":
        reason = ev.get("reason")
        if isinstance(reason, str) and reason.strip():
            parts.append("原因 " + reason)

    elif etype == "model_error":
        err = ev.get("error") or ev.get("message")
        if isinstance(err, str) and err.strip():
            parts.append(_preview(err))

    elif etype == "session_meta":
        for key in ("model", "version", "session_id", "cwd"):
            value = ev.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) \
                    and str(value).strip():
                parts.append(f"{key}={value}")

    return "；".join(parts)


def _merge_usage(usage: dict, raw: dict) -> None:
    """把一条事件里的 usage 字段归一后累加进 usage；非数值项忽略。"""
    for key, value in raw.items():
        canonical = _USAGE_ALIASES.get(key) if isinstance(key, str) else None
        if canonical is not None and _is_num(value):
            usage[canonical] += value


def _hashable_tool(ev):
    """diff 用的工具键：不可哈希的 tool 值退化为 repr，避免 Counter 抛异常。"""
    tool = ev.get("tool")
    if tool is None:
        return None
    return tool if isinstance(tool, Hashable) else repr(tool)


def _ordinal_or_index(ev: dict, index: int) -> int:
    ordinal = ev.get("ordinal")
    return ordinal if _is_int(ordinal) else index


def _clean_events(events) -> list:
    """diff 用的宽松清洗：只要求 dict 且 type 是非空字符串（不校验 ordinal）。"""
    return [ev for ev in events
            if isinstance(ev, dict) and isinstance(ev.get("type"), str) and ev["type"]]


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #

def replay(events: list[dict], until_ordinal: int | None = None) -> dict:
    """把事件流回放成人话时间线，并附带统计。

    返回::

        {"timeline":     [{"ordinal", "type", "label", "detail"}, ...],
         "turns":        int,          # user_message 条数（每条开启一个回合）
         "tool_calls":   int,          # tool_call 条数
         "tools_used":   [...],        # 工具名，按首次出现顺序去重
         "usage":        {...},        # token 用量累加（字段名归一）+ model_responses
         "stops":        [...],        # 每个 loop_stop 的 {"ordinal", "reason"}
         "malformed":    int,          # 畸形事件数（统计整条流，含截断点之后）
         "truncated_at": int | None}   # 因 until_ordinal 真的截掉了事件时 = until_ordinal

    - ``until_ordinal`` 给定时只回放 ordinal <= 该值的事件（含）；
      若没有任何事件被截掉，``truncated_at`` 为 None。
    - ``timeline`` 里的 ``label`` 是人话摘要（如「调用 read_file」「用户提问」），
      ``detail`` 是内容摘要（消息预览、参数、失败原因等），可为空串。
    - 畸形事件（非 dict、缺 type、缺 ordinal、ordinal 非法或乱序）安全跳过并计入
      ``malformed``，不抛异常。
    """
    if until_ordinal is not None and not _is_int(until_ordinal):
        raise TypeError(
            f"until_ordinal 必须是 int 或 None，得到 {type(until_ordinal).__name__}")

    timeline: list[dict] = []
    turns = 0
    tool_calls = 0
    tools_used: list[str] = []
    seen_tools: set[str] = set()
    usage = dict.fromkeys(_USAGE_KEYS, 0)
    usage["model_responses"] = 0
    stops: list[dict] = []
    malformed = 0
    cut = False
    last_ordinal = -1

    for ev in events:
        ok, last_ordinal = _valid_event(ev, last_ordinal)
        if not ok:
            malformed += 1
            continue

        ordinal = ev["ordinal"]
        if until_ordinal is not None and ordinal > until_ordinal:
            cut = True
            continue

        etype = ev["type"]
        timeline.append({
            "ordinal": ordinal,
            "type": etype,
            "label": _label_for(ev),
            "detail": _detail_for(ev),
        })

        if etype == "user_message":
            turns += 1
        elif etype == "tool_call":
            tool_calls += 1
            name = _tool_name(ev)
            if name and name not in seen_tools:
                seen_tools.add(name)
                tools_used.append(name)

        if isinstance(ev.get("usage"), dict):
            _merge_usage(usage, ev["usage"])
            usage["model_responses"] += 1

        if etype == "loop_stop":
            reason = ev.get("reason")
            if not (isinstance(reason, str) and reason):
                reason = ev.get("result") if isinstance(ev.get("result"), str) else ""
            stops.append({"ordinal": ordinal, "reason": _preview(reason)})

    return {
        "timeline": timeline,
        "turns": turns,
        "tool_calls": tool_calls,
        "tools_used": tools_used,
        "usage": usage,
        "stops": stops,
        "malformed": malformed,
        "truncated_at": until_ordinal if cut else None,
    }


def fork_plan(events: list[dict], keep_last: int) -> dict:
    """计算从事件流尾部保留 ``keep_last`` 条历史的分叉计划。

    返回::

        {"source_ordinals": [...],  # 保留事件的 ordinal（升序）
         "dropped": int,            # 被丢弃的事件数（含必丢的 session_meta）
         "reason": str}             # 人话说明

    边界：``keep_last <= 0`` 返回空计划；``keep_last`` 不小于历史事件总数时
    保留全部历史；session_meta 不是历史，无论何时都会被剔除。
    """
    if not _is_int(keep_last):
        raise TypeError(
            f"keep_last 必须是 int，得到 {type(keep_last).__name__}")

    valid: list[dict] = []
    last = -1
    for ev in events:
        ok, last = _valid_event(ev, last)
        if ok:
            valid.append(ev)

    total = len(valid)
    history = [ev for ev in valid if ev["type"] != "session_meta"]
    meta_dropped = total - len(history)

    if keep_last <= 0:
        return {
            "source_ordinals": [],
            "dropped": total,
            "reason": f"keep_last={keep_last} 非正数，返回空计划（全部 {total} 条事件均被丢弃）",
        }

    if keep_last >= len(history):
        kept = history
        reason = (f"keep_last={keep_last} 不小于历史事件总数，保留全部 {len(history)} 条历史"
                  if history else "流中没有可保留的历史事件")
    else:
        kept = history[-keep_last:]
        reason = f"保留最后 {keep_last} 条历史事件，丢弃更早的 {len(history) - keep_last} 条"

    if meta_dropped:
        reason += f"；按约定剔除 {meta_dropped} 条 session_meta"

    return {
        "source_ordinals": [ev["ordinal"] for ev in kept],
        "dropped": total - len(kept),
        "reason": reason,
    }


def diff_runs(a: list[dict], b: list[dict]) -> dict:
    """比较两次运行的事件流，按 ``(type, tool)`` 序列对齐（不比较 payload）。

    返回::

        {"only_in_a": [...],        # (type, tool) 在 a 侧出现次数多于 b 的清单
         "only_in_b": [...],        # 同上，b 侧
         "first_divergence": int | None,  # 首个分歧点
         "tool_delta": {...}}       # 按工具名的次数差（a − b），只列非零项

    - only_in_* 元素形如 ``{"type": ..., "tool": ..., "count": 出现次数差}``；
      两侧事件种类完全一致时为空表（顺序不同不会记入 only_in_*，由
      first_divergence 体现）。
    - first_divergence 取公共前缀首次断裂处的事件 ordinal：优先取 a 侧，
      a 在该位置已无事件时取 b 侧；事件缺 ordinal 时回退为序列下标；
      两条序列完全一致时为 None。
    - 畸形事件（非 dict、缺 type）直接跳过，不抛异常。
    """
    ea = _clean_events(a)
    eb = _clean_events(b)
    pa = [(ev["type"], _hashable_tool(ev)) for ev in ea]
    pb = [(ev["type"], _hashable_tool(ev)) for ev in eb]

    common = min(len(pa), len(pb))
    pos = common
    for i in range(common):
        if pa[i] != pb[i]:
            pos = i
            break

    if pos < len(ea):
        first_divergence = _ordinal_or_index(ea[pos], pos)
    elif pos < len(eb):
        first_divergence = _ordinal_or_index(eb[pos], pos)
    else:
        first_divergence = None

    def only(seq_self, seq_other):
        extra = Counter(seq_self) - Counter(seq_other)
        rows = [{"type": etype, "tool": tool, "count": n}
                for (etype, tool), n in extra.items()]
        rows.sort(key=lambda row: (-row["count"], row["type"], str(row["tool"])))
        return rows

    count_a = Counter(t for t in (_hashable_tool(ev) for ev in ea) if t is not None)
    count_b = Counter(t for t in (_hashable_tool(ev) for ev in eb) if t is not None)
    tool_delta: dict = {}
    for tool in sorted(set(count_a) | set(count_b), key=str):
        delta = count_a[tool] - count_b[tool]
        if delta:
            tool_delta[tool if isinstance(tool, str) else str(tool)] = delta

    return {
        "only_in_a": only(pa, pb),
        "only_in_b": only(pb, pa),
        "first_divergence": first_divergence,
        "tool_delta": tool_delta,
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def _selftest() -> None:
    import sys

    try:  # Windows 控制台可能是 GBK，统一按 UTF-8 输出
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    def mk(ordinal, etype, **kw):
        ev = {"ordinal": ordinal, "type": etype}
        ev.update(kw)
        return ev

    def sample():
        return [
            mk(0, "session_meta", model="glm-5", cwd="/tmp"),
            mk(1, "user_message", text="帮我读一下配置文件"),
            mk(2, "assistant_message", text="好的，我先读取文件。",
               usage={"input_tokens": 100, "output_tokens": 20}),
            mk(3, "tool_call", tool="read_file", args={"path": "a.txt"}, ok=True, result="ok"),
            mk(4, "tool_decision", decision="approve", tool="read_file"),
            mk(5, "assistant_message", text="读完了，内容如下。",
               usage={"input_tokens": 150, "output_tokens": 30}),
            mk(6, "subagent_spawn", agent="searcher", task="找出所有相关配置"),
            mk(7, "subagent_result", result="找到 3 条记录"),
            mk(8, "compaction", before_tokens=9000, after_tokens=2000),
            mk(9, "user_message", text="继续"),
            mk(10, "tool_call", tool="grep", args={"pattern": "foo"}, ok=False, error="no match"),
            mk(11, "checkpoint", name="before-refactor"),
            mk(12, "loop_stop", reason="task_complete"),
        ]

    checks: list[tuple[str, object]] = []

    def case(name):
        def wrap(fn):
            checks.append((name, fn))
            return fn
        return wrap

    @case("自检 1：时间线顺序正确、schema 完整、统计正确")
    def _():
        r = replay(sample())
        ordinals = [e["ordinal"] for e in r["timeline"]]
        assert ordinals == list(range(13)), ordinals
        for entry in r["timeline"]:
            assert set(entry) == {"ordinal", "type", "label", "detail"}, entry
            assert isinstance(entry["label"], str) and entry["label"]
            assert isinstance(entry["detail"], str)
        assert r["turns"] == 2, r["turns"]
        assert r["tool_calls"] == 2, r["tool_calls"]
        assert r["tools_used"] == ["read_file", "grep"], r["tools_used"]
        assert r["usage"]["input_tokens"] == 250, r["usage"]
        assert r["usage"]["output_tokens"] == 50, r["usage"]
        assert r["usage"]["model_responses"] == 2, r["usage"]
        assert r["stops"] == [{"ordinal": 12, "reason": "task_complete"}], r["stops"]
        assert r["malformed"] == 0 and r["truncated_at"] is None

    @case("自检 2：label 是人话摘要，不是原始 type 字符串")
    def _():
        r = replay(sample())
        by_ordinal = {e["ordinal"]: e for e in r["timeline"]}
        assert by_ordinal[1]["label"] == "用户提问"
        assert by_ordinal[2]["label"] == "助手回复"
        assert by_ordinal[3]["label"] == "调用 read_file"
        assert by_ordinal[4]["label"] == "批准工具调用"
        assert by_ordinal[6]["label"] == "分派子代理"
        assert by_ordinal[7]["label"] == "子代理返回"
        assert by_ordinal[8]["label"] == "压缩上下文"
        assert by_ordinal[11]["label"] == "保存检查点"
        assert by_ordinal[12]["label"] == "循环停止"
        assert by_ordinal[10]["label"] == "调用 grep（失败）"  # 失败的调用要标出来
        r2 = replay([mk(0, "session_meta"), mk(1, "mystery_event")])
        assert r2["timeline"][1]["label"].startswith("未知事件")
        assert "mystery_event" in r2["timeline"][1]["label"]

    @case("自检 3：detail 摘要（消息预览 / 参数 / 失败原因 / 压缩前后）")
    def _():
        by_ordinal = {e["ordinal"]: e for e in replay(sample())["timeline"]}
        assert by_ordinal[1]["detail"] == "帮我读一下配置文件"
        assert by_ordinal[3]["detail"] == "参数 path=a.txt"
        assert by_ordinal[4]["detail"] == "工具 read_file"
        assert by_ordinal[8]["detail"] == "token 9000 → 2000"
        assert by_ordinal[10]["detail"] == "参数 pattern=foo；错误 no match"
        assert by_ordinal[12]["detail"] == "原因 task_complete"
        assert by_ordinal[0]["detail"].startswith("model=glm-5")

    @case("自检 4：until_ordinal 截断回放（truncated_at 置位，统计只算窗口内）")
    def _():
        r = replay(sample(), until_ordinal=5)
        assert [e["ordinal"] for e in r["timeline"]] == [0, 1, 2, 3, 4, 5]
        assert r["truncated_at"] == 5
        assert r["turns"] == 1 and r["tool_calls"] == 1
        assert r["tools_used"] == ["read_file"]
        assert r["stops"] == [] and r["malformed"] == 0

    @case("自检 5：until_ordinal 未截掉任何事件时 truncated_at 为 None")
    def _():
        assert replay(sample(), until_ordinal=99)["truncated_at"] is None
        assert replay(sample(), until_ordinal=12)["truncated_at"] is None
        assert replay(sample())["truncated_at"] is None

    @case("自检 6：截断点之外的畸形事件仍被计数（malformed 统计整条流）")
    def _():
        stream = [mk(0, "session_meta"),
                  mk(1, "user_message", text="a"),
                  mk(2, "assistant_message", text="b"),
                  "垃圾",  # 截断点之后的畸形
                  mk(4, "user_message", text="c")]
        r = replay(stream, until_ordinal=2)
        assert [e["ordinal"] for e in r["timeline"]] == [0, 1, 2]
        assert r["truncated_at"] == 2
        assert r["malformed"] == 1

    @case("自检 7：usage 字段名归一（prompt/completion 等别名）与非 dict 忽略")
    def _():
        events = [mk(0, "session_meta"),
                  mk(1, "assistant_message",
                     usage={"prompt_tokens": 5, "completion_tokens": 7,
                            "cached_tokens": 2, "bogus": "x"}),
                  mk(2, "assistant_message", usage="oops")]
        u = replay(events)["usage"]
        assert u["input_tokens"] == 5 and u["output_tokens"] == 7, u
        assert u["cached_tokens"] == 2 and u["total_tokens"] == 0, u
        assert u["model_responses"] == 1, u

    @case("自检 8：fork 永远丢掉 session_meta；keep_last 超限时保留全部历史")
    def _():
        fp = fork_plan(sample(), 100)
        assert fp["source_ordinals"] == list(range(1, 13)), fp
        assert fp["dropped"] == 1  # 只丢了 session_meta
        assert "session_meta" in fp["reason"]

    @case("自检 9：fork keep_last<=0 返回空计划")
    def _():
        for n in (0, -2):
            fp = fork_plan(sample(), n)
            assert fp["source_ordinals"] == [], fp
            assert fp["dropped"] == 13, fp
            assert fp["reason"]

    @case("自检 10：fork keep_last 部分保留（取尾部 N 条历史）")
    def _():
        fp = fork_plan(sample(), 3)
        assert fp["source_ordinals"] == [10, 11, 12], fp
        assert fp["dropped"] == 10, fp  # 9 条更早历史 + 1 条 session_meta
        assert "最后 3 条" in fp["reason"]

    @case("自检 11：fork 空输入 / 仅 session_meta 的流都安全")
    def _():
        fp = fork_plan([], 5)
        assert fp == {"source_ordinals": [], "dropped": 0,
                      "reason": "流中没有可保留的历史事件"}
        fp2 = fork_plan([mk(0, "session_meta")], 5)
        assert fp2["source_ordinals"] == [] and fp2["dropped"] == 1

    @case("自检 12：diff 两次完全相同的运行 → 全空、无分歧")
    def _():
        d = diff_runs(sample(), sample())
        assert d["only_in_a"] == [] and d["only_in_b"] == []
        assert d["first_divergence"] is None
        assert d["tool_delta"] == {}

    @case("自检 13：diff 找出首个分歧点（中途换了工具）")
    def _():
        a = sample()
        b = sample()
        b[10]["tool"] = "sed"
        d = diff_runs(a, b)
        assert d["first_divergence"] == 10, d
        assert d["only_in_a"] == [{"type": "tool_call", "tool": "grep", "count": 1}], d
        assert d["only_in_b"] == [{"type": "tool_call", "tool": "sed", "count": 1}], d

    @case("自检 14：diff 尾部分歧 + only_in 双侧各报各的")
    def _():
        a = sample() + [mk(13, "tool_call", tool="browser", ok=True)]
        b = sample() + [mk(13, "tool_call", tool="shell", ok=True)]
        d = diff_runs(a, b)
        assert d["first_divergence"] == 13, d
        assert d["only_in_a"][0]["tool"] == "browser"
        assert d["only_in_b"][0]["tool"] == "shell"

    @case("自检 15：diff 一条流是另一条的前缀（扩展出一侧）")
    def _():
        d = diff_runs(sample()[:10], sample())
        assert d["first_divergence"] == 10, d
        assert d["only_in_a"] == []
        assert len(d["only_in_b"]) == 3  # grep 调用、checkpoint、loop_stop

    @case("自检 16：diff 工具差异 tool_delta（a − b，只列非零）")
    def _():
        a = sample()
        b = sample()
        b[10]["tool"] = "sed"
        assert diff_runs(a, b)["tool_delta"] == {"grep": 1, "sed": -1}
        a2 = sample() + [mk(13, "tool_call", tool="browser", ok=True)]
        b2 = sample() + [mk(13, "tool_call", tool="shell", ok=True)]
        assert diff_runs(a2, b2)["tool_delta"] == {"browser": 1, "shell": -1}

    @case("自检 17：畸形事件安全跳过并计数（非 dict / 缺字段 / 乱序 / 非法 ordinal）")
    def _():
        stream = [
            None,                                     # 非 dict
            "x", 42,                                  # 非 dict
            {},                                       # 缺 ordinal、缺 type
            {"ordinal": 1},                           # 缺 type
            {"type": "user_message"},                 # 缺 ordinal
            mk(2, "user_message", text="hi"),         # 有效
            mk(2, "tool_call", tool="dup"),           # ordinal 重复（乱序）
            mk(1, "tool_call"),                       # ordinal 倒退（乱序）
            mk(-1, "weird"),                          # 负 ordinal
            mk(3.5, "weird"),                         # float ordinal
            mk(True, "weird"),                        # bool ordinal
            mk(5, "user_message", text="ok"),         # 有效，流能继续
        ]
        r = replay(stream)
        assert r["malformed"] == 11, r["malformed"]
        assert [e["ordinal"] for e in r["timeline"]] == [2, 5]
        assert r["turns"] == 2
        # fork_plan 同样不抛异常
        fp = fork_plan(stream, 5)
        assert fp["source_ordinals"] == [2, 5]

    @case("自检 18：空输入安全（replay / fork_plan / diff_runs）")
    def _():
        r = replay([])
        assert r["timeline"] == [] and r["turns"] == 0 and r["tool_calls"] == 0
        assert r["tools_used"] == [] and r["stops"] == []
        assert r["malformed"] == 0 and r["truncated_at"] is None
        assert all(v == 0 for v in r["usage"].values())
        d = diff_runs([], [])
        assert d == {"only_in_a": [], "only_in_b": [],
                     "first_divergence": None, "tool_delta": {}}
        d2 = diff_runs([], [mk(0, "user_message", text="hi")])
        assert d2["first_divergence"] == 0
        assert d2["only_in_b"] == [{"type": "user_message", "tool": None, "count": 1}]

    for name, fn in checks:
        fn()
        print(f"PASS  {name}")
    print(f"自检完成：{len(checks)}/{len(checks)} 项通过")


if __name__ == "__main__":
    _selftest()


# ---------------------------------------------------------------------------
# register / selftest (for keystone module loader)
# ---------------------------------------------------------------------------

def register(api) -> dict:
    """模块注册入口。"""
    return {
        "name": "replay",
        "version": "1.0.0",
        "capabilities": [
            "replay.timeline",
            "replay.fork_plan",
            "replay.diff_runs",
        ],
        "hooks": {
            "on_replay": replay,
            "on_fork_plan": fork_plan,
            "on_diff_runs": diff_runs,
        },
        "author": "keystone contrib (stepclaw)",
        "summary": "Session replay: append-only event stream → human-readable timeline with statistics.",
    }


def selftest() -> list[tuple[str, bool, str]]:
    """返回 [(name, ok, detail), ...] 供 registry.validate 消费。"""
    import sys, io
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        _selftest()
    except Exception as exc:
        sys.stdout = old_stdout
        return [("selftest_internal", False, str(exc))]
    sys.stdout = old_stdout
    return [("replay_selftest", True, buf.getvalue().strip())]
