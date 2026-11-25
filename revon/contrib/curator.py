"""revon.contrib.curator — 知识代谢：sidecar 状态机 + pin 豁免 + 账本

状态机单向迁移：active -> stale -> archived
  - pinned=True 永不迁移
  - created_by 命中 protectCreatedBy 永不迁移
  - archived 永远不会被拉回 active（只归档，不删除）

全部纯函数：不读系统时钟（now 由调用方传入）、不读写文件、不起进程、不联网。
"""

from __future__ import annotations

import copy
from typing import Any

MODULE_API_VERSION = 1

# ---------------------------------------------------------------------------
# 常量与保守默认值
# ---------------------------------------------------------------------------

DEFAULT_STALE_AFTER = 2_592_000.0      # 30 天
DEFAULT_ARCHIVE_AFTER = 7_776_000.0    # 90 天
DEFAULT_PROTECTED_CREATORS = ("user", "installed")

KNOWN_CREATORS = ("agent", "user", "installed")
KNOWN_STATES = ("active", "stale", "archived")

LEDGER_ACTORS = ("curator", "agent", "user")

_MISSING = object()


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nonneg_int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, float) and v >= 0:
        return int(v)
    return 0


def _normalize_policy(policy: Any, warnings: list[str]) -> dict:
    """校验 policy，非法项回退默认并记 warning（绝不静默）。"""
    stale_after = DEFAULT_STALE_AFTER
    archive_after = DEFAULT_ARCHIVE_AFTER
    protected = list(DEFAULT_PROTECTED_CREATORS)

    if policy is None:
        return {
            "staleAfterSeconds": stale_after,
            "archiveAfterSeconds": archive_after,
            "protectCreatedBy": protected,
        }
    if not isinstance(policy, dict):
        warnings.append("policy 非 dict，全部使用保守默认值")
        return {
            "staleAfterSeconds": stale_after,
            "archiveAfterSeconds": archive_after,
            "protectCreatedBy": protected,
        }

    raw_stale = policy.get("staleAfterSeconds", _MISSING)
    if raw_stale is _MISSING:
        pass
    elif _is_number(raw_stale) and raw_stale > 0:
        stale_after = float(raw_stale)
    else:
        warnings.append("staleAfterSeconds 非法（需为正数），回退默认值")

    raw_archive = policy.get("archiveAfterSeconds", _MISSING)
    if raw_archive is _MISSING:
        pass
    elif _is_number(raw_archive) and raw_archive > 0:
        archive_after = float(raw_archive)
    else:
        warnings.append("archiveAfterSeconds 非法（需为正数），回退默认值")

    if archive_after < stale_after:
        warnings.append("archiveAfterSeconds 小于 staleAfterSeconds，状态机仍按各自阈值单向推进")

    raw_protect = policy.get("protectCreatedBy", _MISSING)
    if raw_protect is _MISSING:
        pass
    elif isinstance(raw_protect, (list, tuple)):
        protected = [str(x) for x in raw_protect]
    else:
        warnings.append("protectCreatedBy 非列表，回退默认值 [user, installed]")

    return {
        "staleAfterSeconds": stale_after,
        "archiveAfterSeconds": archive_after,
        "protectCreatedBy": protected,
    }


def _normalize_entry(raw: Any, index: int, now: float, warnings: list[str]) -> dict | None:
    """深拷贝并补齐保守默认值。畸形条目返回 None。

    时间戳缺省策略：created_at / last_used_at 缺失一律视为 now（新鲜），
    因此在拿不到年龄证据时绝不会发生迁移——这是保守方向。
    """
    if not isinstance(raw, dict):
        warnings.append(f"第 {index} 项不是 dict，已跳过")
        return None

    entry = copy.deepcopy(raw)

    eid = entry.get("id", _MISSING)
    if eid is _MISSING or eid is None or (isinstance(eid, str) and eid == ""):
        warnings.append(f"第 {index} 项缺少 id，使用序号占位")
        entry["id"] = f"<#{index}>"
    else:
        entry["id"] = str(eid)

    created_by = entry.get("created_by", _MISSING)
    if created_by is _MISSING or created_by is None:
        created_by = "agent"
    elif not isinstance(created_by, str) or created_by not in KNOWN_CREATORS:
        warnings.append(f"条目 {entry['id']} 的 created_by={created_by!r} 非法，按 agent 处理")
        created_by = "agent"
    entry["created_by"] = created_by

    state = entry.get("state", _MISSING)
    if state is _MISSING or state is None:
        state = "active"
    elif not isinstance(state, str) or state not in KNOWN_STATES:
        warnings.append(f"条目 {entry['id']} 的 state={state!r} 非法，保持不动")
        entry["state"] = str(state) if isinstance(state, str) else "active"
        # 非法状态不参与迁移，但保留在输出里
        entry["_invalid_state"] = True
        state = entry["state"]
    entry["state"] = state

    entry["pinned"] = entry.get("pinned") is True
    entry["use_count"] = _nonneg_int(entry.get("use_count", 0))
    entry["view_count"] = _nonneg_int(entry.get("view_count", 0))
    entry["patch_count"] = _nonneg_int(entry.get("patch_count", 0))

    created_at = entry.get("created_at", _MISSING)
    if not _is_number(created_at):
        created_at = now
    entry["created_at"] = float(created_at)

    last_used = entry.get("last_used_at", _MISSING)
    if not _is_number(last_used):
        # 缺最后使用时间：退回到创建时间；创建时间也缺时两者皆为 now
        last_used = entry["created_at"]
    entry["last_used_at"] = float(last_used)

    return entry


# ---------------------------------------------------------------------------
# Hook: curate —— sidecar 状态机（只归档，不删除）
# ---------------------------------------------------------------------------

def curate(entries: list[dict], now: float, policy: dict | None = None) -> dict:
    """对条目做一次单向状态推进。

    返回 {
        "entries":    [更新后的副本，输入绝不被修改],
        "transitions": {"active->stale": n, "stale->archived": n, "kept": n},
        "warnings":   [...],
    }
    """
    warnings: list[str] = []

    if not _is_number(now):
        warnings.append("now 非法，按 0 处理")
        now = 0.0
    else:
        now = float(now)

    pol = _normalize_policy(policy, warnings)
    stale_after = pol["staleAfterSeconds"]
    archive_after = pol["archiveAfterSeconds"]
    protected_creators = set(pol["protectCreatedBy"])

    transitions = {"active->stale": 0, "stale->archived": 0, "kept": 0}
    out: list[dict] = []

    for index, raw in enumerate(entries or []):
        entry = _normalize_entry(raw, index, now, warnings)
        if entry is None:
            continue

        invalid_state = entry.pop("_invalid_state", False)
        if invalid_state:
            transitions["kept"] += 1
            out.append(entry)
            continue

        state = entry["state"]
        pinned = entry["pinned"]
        created_by = entry["created_by"]
        idle = now - entry["last_used_at"]

        # archived 是终态：任何证据都不许把它拉回 active/stale
        if state == "archived":
            transitions["kept"] += 1
            out.append(entry)
            continue

        exempt = pinned or created_by in protected_creators

        # 每次调用最多推进一格：active 只能变 stale，stale 才能变 archived
        if state == "active":
            if not exempt and idle >= stale_after:
                entry["state"] = "stale"
                transitions["active->stale"] += 1
            else:
                transitions["kept"] += 1
        elif state == "stale":
            if not exempt and idle >= archive_after:
                entry["state"] = "archived"
                transitions["stale->archived"] += 1
            else:
                transitions["kept"] += 1
        else:
            transitions["kept"] += 1

        out.append(entry)

    return {"entries": out, "transitions": transitions, "warnings": warnings}


# ---------------------------------------------------------------------------
# Hook: review —— 纯启发式候选打分（不调用任何模型）
# ---------------------------------------------------------------------------

def _review_score(entry: dict, now: float) -> tuple[int, list[str]]:
    """分越高越值得归档。只依赖使用频次、闲置时长、创建者三个事实。"""
    score = 0
    reasons: list[str] = []

    idle_seconds = now - entry["last_used_at"]
    idle_days = idle_seconds / 86400.0

    if entry["created_by"] == "agent":
        score += 40
        reasons.append("agent 自建")

    if idle_days >= 365:
        score += 40
        reasons.append(f"已 {int(idle_days)} 天未使用")
    elif idle_days >= 180:
        score += 30
        reasons.append(f"已 {int(idle_days)} 天未使用")
    elif idle_days >= 90:
        score += 20
        reasons.append(f"已 {int(idle_days)} 天未使用")
    elif idle_days >= 30:
        score += 10
        reasons.append(f"已 {int(idle_days)} 天未使用")

    usage_total = entry["use_count"] + entry["view_count"] + entry["patch_count"]
    if usage_total == 0:
        score += 20
        reasons.append("零使用记录")
    elif usage_total <= 3:
        score += 10
        reasons.append(f"使用记录仅 {usage_total} 次")

    if entry["state"] == "stale":
        score += 10
        reasons.append("已被标记 stale")

    return min(score, 100), reasons


def review(entries: list[dict], now: float) -> dict:
    """返回归档候选（按分数降序）、受保护 id 列表、未触碰条目数。"""
    if not _is_number(now):
        now = 0.0
    else:
        now = float(now)

    candidates: list[dict] = []
    protected: list[str] = []
    untouched = 0

    for index, raw in enumerate(entries or []):
        if not isinstance(raw, dict):
            untouched += 1
            continue

        warns: list[str] = []
        entry = _normalize_entry(raw, index, now, warns)
        if entry is None:
            untouched += 1
            continue
        entry.pop("_invalid_state", None)

        eid = entry["id"]

        if entry["pinned"] or entry["created_by"] in DEFAULT_PROTECTED_CREATORS:
            protected.append(eid)
            continue

        # archived 是终态，审查也不再动它
        if entry["state"] == "archived":
            untouched += 1
            continue

        score, reasons = _review_score(entry, now)
        if score <= 0:
            untouched += 1
            continue

        candidates.append({
            "id": eid,
            "reason": "；".join(reasons),
            "score": score,
        })

    candidates.sort(key=lambda c: (-c["score"], c["id"]))
    return {"candidates": candidates, "protected": protected, "untouched": untouched}


# ---------------------------------------------------------------------------
# Hook: ledger_entry —— 账本行（actor 白名单，非法即报错）
# ---------------------------------------------------------------------------

def ledger_entry(action: str, entry: dict, actor: str, now: float) -> dict:
    """构造一行账本：{ts, actor, action, target, before, after}。

    actor 只允许 curator / agent / user，其它值直接 ValueError——
    账本的意义就是可追责，来源不明的记录不许混进来。

    action 形如 "active->stale" 时，before/after 从箭头两侧解析；
    其它动作（pin / unpin / note…）before/after 均取条目当前状态。
    """
    if actor not in LEDGER_ACTORS:
        raise ValueError(
            f"非法 actor={actor!r}，只允许 { '/'.join(LEDGER_ACTORS) }"
        )
    if not isinstance(action, str) or action == "":
        raise ValueError("action 必须是非空字符串")
    if not isinstance(entry, dict):
        raise ValueError("entry 必须是 dict")

    current_state = entry.get("state")
    before_state: Any = current_state
    after_state: Any = current_state
    if "->" in action:
        left, _, right = action.partition("->")
        before_state = left.strip()
        after_state = right.strip()

    return {
        "ts": now,
        "actor": actor,
        "action": action,
        "target": entry.get("id"),
        "before": {"state": before_state},
        "after": {"state": after_state},
    }


# ---------------------------------------------------------------------------
# register / selftest
# ---------------------------------------------------------------------------

def register(api) -> dict[str, Any]:
    return {
        "name": "curator",
        "version": "1.0.0",
        "capabilities": [
            "knowledge.metabolize",
            "knowledge.review",
            "knowledge.ledger",
        ],
        "hooks": {
            "curate": curate,
            "review": review,
            "ledger_entry": ledger_entry,
        },
        "author": "Hermes",
        "summary": "Sidecar state machine active/stale/archived with pin exemptions and an append-only ledger.",
    }


def selftest() -> list[tuple[str, bool, str]]:
    now = 1_000_000_000.0
    DAY = 86_400.0

    old_far = now - (200 * DAY)      # 远超 archive 阈值
    old_stale = now - (30 * DAY + 1_000.0)
    old_archive = now - (100 * DAY)
    recent = now - 100.0

    entries = [
        {"id": "pinned",    "created_by": "agent",     "state": "active",
         "pinned": True,  "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
        {"id": "by-user",   "created_by": "user",      "state": "active",
         "pinned": False, "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
        {"id": "by-inst",   "created_by": "installed", "state": "active",
         "pinned": False, "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
        {"id": "go-stale",  "created_by": "agent", "state": "active", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_stale, "last_used_at": old_stale},
        {"id": "go-arch",   "created_by": "agent", "state": "stale", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_archive, "last_used_at": old_archive},
        {"id": "fresh",     "created_by": "agent", "state": "active", "pinned": False,
         "use_count": 9, "view_count": 3, "patch_count": 1,
         "created_at": recent, "last_used_at": recent},
        {"id": "frozen",    "created_by": "agent", "state": "archived", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": recent},
        {"id": "stale-young", "created_by": "agent", "state": "stale", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_stale, "last_used_at": old_stale},
        {},  # 全缺字段
        {"id": "one-hop",  "created_by": "agent", "state": "active", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
    ]
    snapshot = copy.deepcopy(entries)

    res = curate(entries, now)
    out = {e["id"]: e for e in res["entries"]}
    tr = res["transitions"]

    # 自定义 policy：关闭创建者豁免
    res_no_protect = curate(
        [{"id": "u", "created_by": "user", "state": "active", "pinned": False,
          "use_count": 0, "view_count": 0, "patch_count": 0,
          "created_at": old_far, "last_used_at": old_far}],
        now, {"staleAfterSeconds": 2_592_000, "archiveAfterSeconds": 7_776_000,
              "protectCreatedBy": []},
    )
    # 非法 policy 阈值回退
    res_bad_pol = curate(
        [{"id": "g", "created_by": "agent", "state": "active", "pinned": False,
          "use_count": 0, "view_count": 0, "patch_count": 0,
          "created_at": old_stale, "last_used_at": old_stale}],
        now, {"staleAfterSeconds": -1, "archiveAfterSeconds": "x"},
    )
    res_malformed = curate([None, "junk", 123], now)
    res_empty = curate([], now)
    res_none = curate(None, now)  # type: ignore[arg-type]

    # ---- review ----
    review_entries = [
        {"id": "old", "created_by": "agent", "state": "active", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": now - 400 * DAY, "last_used_at": now - 400 * DAY},
        {"id": "mid", "created_by": "agent", "state": "active", "pinned": False,
         "use_count": 1, "view_count": 4, "patch_count": 0,
         "created_at": now - 100 * DAY, "last_used_at": now - 100 * DAY},
        {"id": "new", "created_by": "agent", "state": "active", "pinned": False,
         "use_count": 9, "view_count": 9, "patch_count": 9,
         "created_at": recent, "last_used_at": recent},
        {"id": "keep-user", "created_by": "user", "state": "active", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
        {"id": "keep-pin", "created_by": "agent", "state": "active", "pinned": True,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
        {"id": "dead", "created_by": "agent", "state": "archived", "pinned": False,
         "use_count": 0, "view_count": 0, "patch_count": 0,
         "created_at": old_far, "last_used_at": old_far},
    ]
    rv = review(review_entries, now)
    rv_empty = review([], now)

    # ---- ledger ----
    row = ledger_entry("active->stale", {"id": "x", "state": "stale"}, "curator", now)
    actor_rejected = False
    try:
        ledger_entry("pin", {"id": "x"}, "system", now)
    except ValueError:
        actor_rejected = True
    actors_ok = all(
        ledger_entry("note", {"id": "x", "state": "active"}, a, now)["actor"] == a
        for a in ("curator", "agent", "user")
    )

    manifest = register(None)

    return [
        # 豁免 —— 3 条
        ("pinned-exempt",
         out["pinned"]["state"] == "active",
         f"state={out['pinned']['state']}"),
        ("user-created-exempt",
         out["by-user"]["state"] == "active",
         f"state={out['by-user']['state']}"),
        ("installed-created-exempt",
         out["by-inst"]["state"] == "active",
         f"state={out['by-inst']['state']}"),
        # 迁移 —— 2 条
        ("active-to-stale",
         out["go-stale"]["state"] == "stale",
         f"state={out['go-stale']['state']}"),
        ("stale-to-archived",
         out["go-arch"]["state"] == "archived",
         f"state={out['go-arch']['state']}"),
        # 未到期 / 单向性 —— 3 条
        ("not-due-kept-active",
         out["fresh"]["state"] == "active",
         f"state={out['fresh']['state']}"),
        ("stale-not-due-kept-stale",
         out["stale-young"]["state"] == "stale",
         f"state={out['stale-young']['state']}"),
        ("archived-never-revived",
         out["frozen"]["state"] == "archived",
         f"state={out['frozen']['state']}"),
        # 状态机每次只推进一格
        ("one-hop-per-curate",
         out["one-hop"]["state"] == "stale",
         f"state={out['one-hop']['state']}（active 不得直接跳 archived）"),
        # 缺字段 / 副本 —— 2 条
        ("missing-fields-conservative-default",
         out["<#8>"]["state"] == "active" and out["<#8>"]["pinned"] is False,
         f"entry={out['<#8>']}"),
        ("returns-deep-copy-input-unchanged",
         entries == snapshot and res["entries"][0] is not entries[0],
         "输入被修改或返回了同一对象"),
        # 计数 —— 1 条
        ("transition-counts",
         tr == {"active->stale": 2, "stale->archived": 1, "kept": 7},
         f"transitions={tr}"),
        # policy —— 2 条
        ("custom-protect-override",
         res_no_protect["entries"][0]["state"] == "stale",
         f"state={res_no_protect['entries'][0]['state']}"),
        ("invalid-policy-fallback-warns",
         res_bad_pol["entries"][0]["state"] == "stale"
         and len(res_bad_pol["warnings"]) >= 1,
         f"warnings={res_bad_pol['warnings']}"),
        # 畸形 / 空输入 —— 2 条
        ("malformed-entries-skipped-with-warning",
         res_malformed["entries"] == [] and len(res_malformed["warnings"]) == 3,
         f"res={res_malformed}"),
        ("empty-and-none-input-safe",
         res_empty == {"entries": [], "transitions":
                       {"active->stale": 0, "stale->archived": 0, "kept": 0},
                       "warnings": []}
         and res_none["entries"] == [],
         f"empty={res_empty}, none={res_none}"),
        # review —— 3 条
        ("review-score-order",
         [c["id"] for c in rv["candidates"]] == ["old", "mid", "new"]
         and rv["candidates"][0]["score"] == 100,
         f"candidates={rv['candidates']}"),
        ("review-protected-and-untouched",
         set(rv["protected"]) == {"keep-user", "keep-pin"} and rv["untouched"] == 1,
         f"protected={rv['protected']}, untouched={rv['untouched']}"),
        ("review-empty-safe",
         rv_empty == {"candidates": [], "protected": [], "untouched": 0},
         f"rv={rv_empty}"),
        # ledger —— 3 条
        ("ledger-row-shape-and-arrow-parse",
         row == {"ts": now, "actor": "curator", "action": "active->stale",
                 "target": "x", "before": {"state": "active"},
                 "after": {"state": "stale"}},
         f"row={row}"),
        ("ledger-actor-whitelist-rejects",
         actor_rejected, "非法 actor 未被拒绝"),
        ("ledger-all-valid-actors",
         actors_ok, "合法 actor 未被接受"),
        # manifest —— 2 条
        ("manifest-name-ok",
         manifest["name"] == "curator", f"name={manifest.get('name')}"),
        ("manifest-hooks-callable",
         all(callable(manifest["hooks"][h]) for h in ("curate", "review", "ledger_entry")),
         f"hooks={list(manifest['hooks'])}"),
    ]


__all__ = ["MODULE_API_VERSION", "register", "selftest"]
