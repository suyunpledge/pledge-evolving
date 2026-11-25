from __future__ import annotations

MODULE_API_VERSION = 1

_DEFAULT_BUDGET = {"maxChars": 24000, "maxMessages": 200, "keepTail": 6}


def _as_message_list(messages):
    if isinstance(messages, list):
        return messages
    return []


def _content_len(message):
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    try:
        return len(str(content))
    except Exception:
        return 0


def _copy_message(message):
    if isinstance(message, dict):
        return dict(message)
    return message


def _norm_budget(budget):
    result = dict(_DEFAULT_BUDGET)
    if not isinstance(budget, dict):
        return result
    for key in ("maxChars", "maxMessages", "keepTail"):
        if key not in budget:
            continue
        value = budget[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = int(value)
        if value < 0:
            value = 0
        result[key] = value
    return result


def should_compact(messages, budget=None):
    msgs = _as_message_list(messages)
    conf = _norm_budget(budget)

    size = sum(_content_len(m) for m in msgs)
    reasons = []
    compact = False

    max_chars = conf["maxChars"]
    if max_chars > 0 and size > max_chars:
        compact = True
        reasons.append("total chars %d > maxChars %d" % (size, max_chars))

    max_messages = conf["maxMessages"]
    if max_messages > 0 and len(msgs) > max_messages:
        compact = True
        reasons.append("messages %d > maxMessages %d" % (len(msgs), max_messages))

    if max_chars > 0:
        half = max_chars / 2.0
        biggest = max((_content_len(m) for m in msgs), default=0)
        if biggest > half:
            compact = True
            reasons.append("single message %d > half budget %g" % (biggest, half))

    reason = "; ".join(reasons) if reasons else "within budget"
    return {"compact": compact, "size": size, "reason": reason}


def plan(messages, budget=None):
    msgs = _as_message_list(messages)
    conf = _norm_budget(budget)
    keep = conf["keepTail"]

    pinned = []
    non_pinned = []
    for message in msgs:
        if isinstance(message, dict) and message.get("pinned") is True:
            pinned.append(_copy_message(message))
        else:
            non_pinned.append(message)

    if keep <= 0:
        head_src = non_pinned
        tail_src = []
    elif len(non_pinned) <= keep:
        head_src = []
        tail_src = non_pinned
    else:
        head_src = non_pinned[:-keep]
        tail_src = non_pinned[-keep:]

    head = [_copy_message(m) for m in head_src]
    tail = [_copy_message(m) for m in tail_src]
    dropped_chars = sum(_content_len(m) for m in head_src)
    summary_placeholder = "[compacted %d messages]" % len(head)

    return {
        "head": head,
        "tail": tail,
        "pinned": pinned,
        "dropped_chars": dropped_chars,
        "summary_placeholder": summary_placeholder,
    }


def merge_summary(messages, summary):
    msgs = _as_message_list(messages)
    if isinstance(summary, str):
        text = summary
    elif summary is None:
        text = ""
    else:
        text = str(summary)

    head_message = {
        "role": "user",
        "content": "[compacted %d messages] " % len(msgs) + text,
    }
    return [head_message] + [_copy_message(m) for m in msgs]


def register(api):
    return {
        "name": "compactor",
        "version": "1.0.0",
        "capabilities": [
            "context.should_compact",
            "context.plan",
            "context.merge_summary",
        ],
        "hooks": {
            "should_compact": should_compact,
            "plan": plan,
            "merge_summary": merge_summary,
        },
        "author": "OpenCode",
        "summary": "上下文压缩阈值判定与保留区选择（纯函数）",
    }


def selftest():
    results = []

    def check(name, condition, detail=""):
        results.append((name, bool(condition), "" if condition else (detail or "assertion failed")))

    big = [{"role": "user", "content": "a" * 100}]
    r = should_compact(big, {"maxChars": 50, "maxMessages": 100, "keepTail": 2})
    check("chars-over-triggers", r["compact"] is True and r["size"] == 100, repr(r))

    many = [{"role": "user", "content": "x"} for _ in range(5)]
    r = should_compact(many, {"maxChars": 1000, "maxMessages": 3, "keepTail": 2})
    check("messages-over-triggers", r["compact"] is True and r["size"] == 5, repr(r))

    single = [{"role": "user", "content": "a" * 11}, {"role": "user", "content": "b"}]
    r = should_compact(single, {"maxChars": 20, "maxMessages": 100, "keepTail": 2})
    check("single-over-triggers", r["compact"] is True and r["size"] == 12, repr(r))

    small = [{"role": "user", "content": "hello"}]
    r = should_compact(small, {"maxChars": 1000, "maxMessages": 10, "keepTail": 2})
    check("within-budget-no-trigger", r["compact"] is False and r["reason"] == "within budget", repr(r))

    msgs = [
        {"id": "m%d" % i, "role": "user", "content": "x" * 10, "pinned": (i == 3)}
        for i in range(1, 9)
    ]
    snapshot = [dict(m) for m in msgs]
    p = plan(msgs, {"maxChars": 1000, "maxMessages": 100, "keepTail": 2})

    tail_ids = [m["id"] for m in p["tail"]]
    check("tail-keeps-keepTail", tail_ids == ["m7", "m8"], repr(tail_ids))

    pinned_ids = [m["id"] for m in p["pinned"]]
    check("pinned-exempt-from-keepTail", pinned_ids == ["m3"], repr(pinned_ids))

    head_ids = [m["id"] for m in p["head"]]
    check(
        "head-tail-disjoint",
        set(head_ids).isdisjoint(set(tail_ids)) and "m3" not in head_ids and "m3" not in tail_ids,
        repr((head_ids, tail_ids, pinned_ids)),
    )

    check("dropped-chars-computed", p["dropped_chars"] == 50, repr(p["dropped_chars"]))

    check(
        "input-not-mutated",
        msgs == snapshot and p["head"][0] is not msgs[0] and p["tail"][0] is not msgs[6],
        "input changed or shared references",
    )

    tail = [{"role": "assistant", "content": "keep"}]
    merged = merge_summary(tail, "SUM")
    check(
        "merge-summary-structure",
        isinstance(merged, list)
        and len(merged) == 2
        and merged[0]["role"] == "user"
        and merged[0]["content"].startswith("[compacted ")
        and merged[0]["content"].endswith("SUM")
        and merged[1] == tail[0]
        and merged[1] is not tail[0],
        repr(merged),
    )

    e = should_compact([], None)
    e_none = should_compact(None, None)
    pe = plan([], None)
    pe_none = plan(None, None)
    me = merge_summary([], "s")
    check(
        "empty-input-safe",
        e["compact"] is False
        and e["size"] == 0
        and e_none["compact"] is False
        and pe["head"] == []
        and pe["tail"] == []
        and pe["pinned"] == []
        and pe["dropped_chars"] == 0
        and pe_none["head"] == []
        and len(me) == 1,
        "empty input handling failed",
    )

    return results
