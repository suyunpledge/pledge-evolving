from __future__ import annotations

"""低速空转监督席位。

This is a deliberately small transplant of the useful part of Tianshu's
convergence detector.  It does not pretend to be a general cognitive judge:
it scores only observable tool history, keeps all state in the caller, and
returns an advisory or a bounded abort recommendation.  ``ExecutionGuard``
continues to own exact repeated-action detection.
"""

import math
from collections import Counter
from xml.sax.saxutils import escape as _xml_escape


MODULE_API_VERSION = 1

_VERSION = "1.0.0"
_SIGNAL_WINDOW = 6
_NO_TOOL_ABORT = 5
_DEFAULT_TIERS = {"low": 8, "mid": 14, "high": 20}
_LARGE_TIERS = {"low": 25, "mid": 34, "high": 42}
_WRITE_TOOLS = {"write_file", "edit_file", "apply_patch", "delete_file", "edit_config"}
_ADVISORY_CHANNELS = {"bus", "system-reminder", "status"}
_ADVISORY_TIERS = {"constitutional", "operational", "informational"}
_MAX_ADVISORIES_PER_TURN = 3
_MAX_PER_CATEGORY = 2
_ADVISORY_COOLDOWN = 2


def _turn_number(value, fallback):
    if isinstance(value, bool):
        return fallback
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(fallback, value)


def _target(row):
    if not isinstance(row, dict):
        return ""
    target = row.get("target")
    if isinstance(target, str) and target:
        return target
    args = row.get("args")
    if isinstance(args, dict):
        for key in ("path", "file", "target", "root"):
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
        blocks = args.get("patches") or args.get("edits")
        if isinstance(blocks, list):
            paths = [b.get("path") for b in blocks
                     if isinstance(b, dict) and isinstance(b.get("path"), str)]
            if paths:
                return "|".join(sorted(set(paths)))
    meta = row.get("meta")
    if isinstance(meta, dict):
        committed = meta.get("committed")
        if isinstance(committed, list):
            paths = [p for p in committed if isinstance(p, str) and p]
            if paths:
                return "|".join(sorted(set(paths)))
    return ""


def _known_bool(value):
    return value if isinstance(value, bool) else None


def _signals(turns):
    """Return the four observable v1 signals and which ones had data."""
    recent = turns[-_SIGNAL_WINDOW:] if isinstance(turns, list) else []
    rows = []
    for turn in recent:
        if not isinstance(turn, dict):
            continue
        tools = turn.get("tools")
        if not isinstance(tools, list):
            continue
        rows.extend(row for row in tools if isinstance(row, dict))

    signals = {}
    known = {}

    writes = [row for row in rows if str(row.get("tool", "")) in _WRITE_TOOLS]
    write_outcomes = [_known_bool(row.get("changed")) for row in writes]
    write_outcomes = [value for value in write_outcomes if value is not None]
    if write_outcomes:
        signals["editRatio"] = sum(write_outcomes) / len(write_outcomes)
        known["editRatio"] = True

    targets = [_target(row) for row in rows]
    targets = [target for target in targets if target]
    if len(targets) >= 2:
        signals["targetNovelty"] = (len(set(targets)) - 1) / (len(targets) - 1)
        known["targetNovelty"] = True

    tools = [str(row.get("tool", "")) for row in rows if row.get("tool")]
    if len(tools) >= 2:
        counts = Counter(tools)
        entropy = -sum((count / len(tools)) * math.log(count / len(tools))
                       for count in counts.values())
        maximum = math.log(len(counts)) if len(counts) > 1 else 1.0
        signals["toolEntropy"] = entropy / maximum if maximum else 0.0
        known["toolEntropy"] = True

    outcomes = [_known_bool(row.get("ok")) for row in rows]
    outcomes = [value for value in outcomes if value is not None]
    if outcomes:
        signals["errorPenalty"] = sum(outcomes) / len(outcomes)
        known["errorPenalty"] = True

    return signals, known, recent, rows


def _productive_text(text):
    if not isinstance(text, str):
        return False
    body = " ".join(text.split())
    if len(body) < 200:
        return False
    half = max(1, len(body) // 2)
    return body[:half] != body[half:half * 2]


def infer_phase(turns, workflow_phase="executing", *, acceptance=False):
    """Infer the useful scoring phase from observable tool names.

    Forge has an explicit ``workflow_state`` for planning/executing but no
    separate verify/deliver events.  Test/diagnostic calls without writes are
    therefore treated as ``verify``; a planned task with no new edits and an
    acceptance marker can be treated as ``deliver`` by the host.
    """
    if str(workflow_phase) == "planning":
        return "plan"
    if str(workflow_phase) == "ready":
        return "explore"
    recent = turns[-3:] if isinstance(turns, list) else []
    rows = [row for turn in recent if isinstance(turn, dict)
            for row in (turn.get("tools") or []) if isinstance(row, dict)]
    names = [str(row.get("tool") or "").lower() for row in rows]
    has_write = any(name in _WRITE_TOOLS for name in names)
    verify_call = any(name.startswith(("run", "test", "check", "verify"))
                      or "pytest" in name for name in names)
    if verify_call and not has_write:
        return "verify"
    if acceptance and not has_write:
        return "deliver"
    return "execute"


def _weights_for_phase(phase):
    return {
        "explore": {"editRatio": 0.10, "targetNovelty": 0.35,
                    "toolEntropy": 0.30, "errorPenalty": 0.25},
        "plan": {"editRatio": 0.05, "targetNovelty": 0.25,
                 "toolEntropy": 0.35, "errorPenalty": 0.35},
        "execute": {"editRatio": 0.40, "targetNovelty": 0.25,
                    "toolEntropy": 0.15, "errorPenalty": 0.20},
        "verify": {"editRatio": 0.05, "targetNovelty": 0.25,
                   "toolEntropy": 0.35, "errorPenalty": 0.35},
        "deliver": {"editRatio": 0.10, "targetNovelty": 0.25,
                     "toolEntropy": 0.25, "errorPenalty": 0.40},
    }.get(str(phase), {"editRatio": 0.35, "targetNovelty": 0.25,
                       "toolEntropy": 0.20, "errorPenalty": 0.20})


def route_advisories(entries, turn, state=None):
    """Budget and route advisory entries using a caller-owned state dict.

    Constitutional entries are retained ahead of the operational/informational
    budget. Other entries are capped at three per turn and two per category.
    A monotonic render turn supplies deterministic cooldown; wall-clock time is
    deliberately absent so replay and tests produce identical results.
    """
    current = _turn_number(turn, 0)
    previous = state if isinstance(state, dict) else {}
    last_render = dict(previous.get("last_render") or {})
    selected = []
    dropped = []
    seen = set()

    clean = []
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict):
            dropped.append({"reason": "malformed"})
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            dropped.append({"reason": "empty"})
            continue
        key = str(raw.get("key") or raw.get("id") or text[:80])
        tier = str(raw.get("tier") or "operational")
        channel = str(raw.get("channel") or "bus")
        if tier not in _ADVISORY_TIERS:
            tier = "operational"
        if channel not in _ADVISORY_CHANNELS:
            channel = "bus"
        if key in seen:
            dropped.append({"key": key, "reason": "duplicate"})
            continue
        seen.add(key)
        prior = last_render.get(key)
        if isinstance(prior, dict) and current - int(prior.get("turn", -10**9)) < _ADVISORY_COOLDOWN:
            dropped.append({"key": key, "reason": "cooldown"})
            continue
        try:
            priority = float(raw.get("priority", 0.0))
        except (TypeError, ValueError):
            priority = 0.0
        clean.append({"id": str(raw.get("id") or key), "key": key,
                      "text": text, "tier": tier, "channel": channel,
                      "category": str(raw.get("category") or "general"),
                      "priority": priority})

    constitutional = sorted((e for e in clean if e["tier"] == "constitutional"),
                            key=lambda e: (-e["priority"], e["key"]))
    normal = sorted((e for e in clean if e["tier"] != "constitutional"),
                    key=lambda e: (0 if e["tier"] == "operational" else 1,
                                   -e["priority"], e["key"]))
    selected.extend(constitutional)
    category_counts = {}
    for entry in normal:
        category = entry["category"]
        if category_counts.get(category, 0) >= _MAX_PER_CATEGORY:
            dropped.append({"key": entry["key"], "reason": "category_budget"})
            continue
        if len(selected) - len(constitutional) >= _MAX_ADVISORIES_PER_TURN:
            dropped.append({"key": entry["key"], "reason": "turn_budget"})
            continue
        selected.append(entry)
        category_counts[category] = category_counts.get(category, 0) + 1

    next_last = dict(last_render)
    for entry in selected:
        next_last[entry["key"]] = {"turn": current, "text": entry["text"]}
    return {"selected": selected, "dropped": dropped,
            "state": {"last_render": next_last}}


def render_advisories(entries):
    """Render prompt-visible bus entries without leaking markup from content."""
    rows = [entry for entry in entries if isinstance(entry, dict)
            and entry.get("channel") in {"bus", "status"}
            and str(entry.get("text") or "").strip()]
    if not rows:
        return ""
    body = "\n".join(
        "- [%s] %s" % (_xml_escape(str(entry.get("category") or "general")),
                       _xml_escape(str(entry.get("text") or "")))
        for entry in rows)
    return "<forge-advisory>\n" + body + "\n</forge-advisory>"


def supervise(turns, turn=None, *, phase="execute", context_chars=0,
              warned=False, text="", bus_state=None):
    """Score a run's recent tool history.

    ``turns`` is a list of ``{"turn": int, "tools": list[dict]}`` rows.  A
    tool row uses ``tool``, ``args``/``target``, ``ok`` and optional boolean
    ``changed``.  Unknown write outcomes are excluded from the edit ratio,
    matching the source detector's conservative evidence rule.
    """
    turns = turns if isinstance(turns, list) else []
    current = _turn_number(turn, len(turns))
    signals, known, recent, rows = _signals(turns)
    tiers = _LARGE_TIERS if int(context_chars or 0) > 1_000_000 else _DEFAULT_TIERS

    # The source detector treats edit progress as meaningful only when it is
    # attached to a novel target. Missing signals are reweighted away.
    weights = _weights_for_phase(phase)
    if "editRatio" in signals:
        novelty = signals.get("targetNovelty", 1.0)
        signals["editRatio"] *= max(novelty, 0.1)
    if phase == "execute" and not any(
            row.get("changed") is True for row in rows if isinstance(row, dict)):
        if "editRatio" in signals:
            signals["editRatio"] *= 0.5

    available = [name for name in weights if name in known]
    if available:
        total_weight = sum(weights[name] for name in available)
        score = sum(signals[name] * weights[name] for name in available) / total_weight
    else:
        score = 1.0

    no_tool_turns = 0
    for row in reversed(recent):
        if row.get("tools"):
            break
        no_tool_turns += 1
    no_tool_abort = no_tool_turns >= _NO_TOOL_ABORT
    productive = _productive_text(text)
    level = 0
    if current >= tiers["high"] and score <= 0.2:
        level = 3
    elif current >= tiers["mid"] and score <= 0.4:
        level = 2
    elif current >= tiers["low"] and score <= 0.6:
        level = 1
    if no_tool_abort:
        level = max(level, 3)
    if productive and no_tool_abort:
        level = min(level, 2)

    previous_scores = []
    for row in turns[:-1]:
        if isinstance(row, dict) and isinstance(row.get("score"), (int, float)):
            previous_scores.append(float(row["score"]))
    score_declining = bool(previous_scores) and score < previous_scores[-1]
    score_abort = (level >= 3 and score < 0.05 and score_declining and bool(warned)
                   and not productive)
    # A no-tool abort gets one warning-only grace turn.  The caller carries
    # ``warned`` across turns, matching the source host's priorWarning latch.
    should_abort = bool(no_tool_abort and bool(warned) and not productive) or score_abort
    if should_abort:
        reason = "no_tool_turns" if no_tool_abort and not productive else "score_abort"
    elif level:
        reason = "low_convergence_score"
    else:
        reason = "within_budget"
    advisory = ""
    if level:
        advisory = ("收敛监督 L%d：近期轨迹进展不足（score=%.2f）。"
                    "请停止重复探索，改为验证、总结或明确说明阻塞原因。"
                    % (level, score))
    bus = route_advisories([
        {"id": "convergence", "key": "convergence:L%d:%s" % (level, reason),
         "text": advisory, "tier": "operational", "channel":
         "system-reminder" if level >= 2 else "bus", "category": "convergence",
         "priority": float(level)}
    ] if advisory else [], current, bus_state)
    advisories = bus["selected"]
    return {
        "level": level,
        "score": round(max(0.0, min(1.0, score)), 6),
        "signals": {name: round(float(value), 6) for name, value in signals.items()},
        "known_signals": sorted(known),
        "no_tool_turns": no_tool_turns,
        "should_abort": should_abort,
        "score_abort": score_abort,
        "reason": reason,
        "advisory": advisories[0]["text"] if advisories else "",
        "advisories": advisories,
        "bus_block": render_advisories(advisories),
        "advisory_dropped": bus["dropped"],
        "bus_state": bus["state"],
        "tiers": dict(tiers),
    }


def register(api):
    return {
        "name": "supervisor",
        "version": _VERSION,
        "capabilities": ["convergence", "advisory"],
        "hooks": {"supervise": supervise},
        "author": "Forge runtime",
        "summary": "基于工具轨迹的低速空转收敛评分与有界提醒",
    }


def selftest():
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), "" if condition else (detail or "assertion failed")))

    base = [{"turn": i, "tools": [{"tool": "read_file", "args": {"path": "a.py"},
                                      "ok": True, "changed": False}]} for i in range(1, 9)]
    low = supervise(base, 8)
    check("low-score-level-one", low["level"] == 1 and low["score"] <= 0.6, repr(low))

    high = supervise(base + [{"turn": i, "tools": [{"tool": "read_file",
        "args": {"path": "a.py"}, "ok": False, "changed": False}]} for i in range(9, 21)], 20)
    check("low-score-level-three", high["level"] == 3, repr(high))

    healthy = supervise([{"turn": i, "tools": [{"tool": "write_file",
        "args": {"path": "f%d.py" % i}, "ok": True, "changed": True},
        {"tool": "run_tests", "args": {"path": "f%d.py" % i}, "ok": True}]} for i in range(1, 21)], 20)
    check("productive-score-is-healthy", healthy["score"] > 0.2 and not healthy["should_abort"], repr(healthy))

    no_tools = supervise([{"turn": i, "tools": []} for i in range(1, 6)], 5)
    check("no-tool-grace-turn", not no_tools["should_abort"] and no_tools["level"] == 3, repr(no_tools))
    no_tools_after_warning = supervise([{"turn": i, "tools": []} for i in range(1, 6)], 5,
                                       warned=True)
    check("no-tool-hard-path", no_tools_after_warning["should_abort"]
          and no_tools_after_warning["reason"] == "no_tool_turns", repr(no_tools_after_warning))

    report = "x" * 180 + " y" * 20
    protected = supervise([{"turn": i, "tools": []} for i in range(1, 6)], 5, text=report)
    check("productive-report-protected", not protected["should_abort"] and protected["level"] <= 2, repr(protected))

    missing = supervise([{"turn": 1, "tools": [{"tool": "read_file", "ok": True}]}], 1)
    check("missing-signals-safe", missing["known_signals"] == ["errorPenalty"], repr(missing))

    unknown = supervise([{"turn": 1, "tools": [{"tool": "write_file", "ok": True,
        "changed": None}]}], 1)
    check("unknown-write-not-counted", "editRatio" not in unknown["known_signals"], repr(unknown))

    large = supervise(base, 8, context_chars=1_100_000)
    check("large-context-tier", large["tiers"]["low"] == 25 and large["level"] == 0, repr(large))

    declining = [{"turn": i, "score": max(0.01, 0.3 - i / 100.0),
                  "tools": [{"tool": "read_file", "args": {"path": "a.py"},
                              "ok": False, "changed": False}]}
                 for i in range(1, 20)]
    last = {"turn": 20, "score": 0.01,
            "tools": [{"tool": "read_file", "args": {"path": "a.py"},
                       "ok": False, "changed": False}]}
    verdict = supervise(declining + [last], 20, warned=True)
    check("score-abort-needs-guard", verdict["score_abort"], repr(verdict))

    routed = route_advisories([
        {"id": "a", "text": "a", "tier": "operational", "category": "x", "priority": 2},
        {"id": "b", "text": "b", "tier": "informational", "category": "x", "priority": 1},
        {"id": "c", "text": "c", "tier": "informational", "category": "x", "priority": 0},
        {"id": "d", "text": "d", "tier": "informational", "category": "x", "priority": -1},
    ], 1)
    check("advisory-category-budget", len(routed["selected"]) == 2
          and any(item["reason"] == "category_budget" for item in routed["dropped"]), repr(routed))

    constitutional = route_advisories([
        {"id": "c", "text": "must", "tier": "constitutional", "category": "safety"},
        *[{"id": str(i), "text": str(i), "tier": "operational", "category": str(i)} for i in range(5)],
    ], 1)
    check("constitutional-bypasses-normal-budget",
          constitutional["selected"][0]["tier"] == "constitutional"
          and len(constitutional["selected"]) == 4, repr(constitutional))

    first = route_advisories([{"id": "same", "text": "same", "category": "x"}], 2)
    second = route_advisories([{"id": "same", "text": "same", "category": "x"}], 3, first["state"])
    check("advisory-cooldown-is-deterministic",
          len(first["selected"]) == 1 and not second["selected"]
          and second["dropped"][0]["reason"] == "cooldown", repr(second))

    rendered = render_advisories([{"channel": "bus", "category": "x",
                                   "text": "<unsafe>"}])
    check("advisory-render-escapes-content",
          "&lt;unsafe&gt;" in rendered and rendered.startswith("<forge-advisory>"), rendered)

    verify_turns = [{"turn": i, "tools": [{"tool": ("run_tests" if i % 2 else "check_types"),
        "args": {"path": "f%d.py" % i}, "ok": True, "changed": False}]} for i in range(1, 9)]
    verify = supervise(verify_turns, 8, phase=infer_phase(verify_turns))
    check("verify-phase-does-not-penalise-no-edit",
          infer_phase(verify_turns) == "verify" and verify["score"] > 0.6
          and verify["level"] == 0, repr(verify))

    return checks


__all__ = ["infer_phase", "register", "render_advisories", "route_advisories", "selftest", "supervise"]
