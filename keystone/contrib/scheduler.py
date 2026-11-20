"""keystone.contrib.scheduler — 周期任务调度器

暴露三个 hooks：
  on_due_check   → due_jobs 到期判定
  on_lease       → acquire_lease 租约互斥
  on_gate        → gate 全局暂停闸门

全部纯函数，无 side-effect。
"""

from __future__ import annotations

import copy
import time
from typing import Any

MODULE_API_VERSION = 1


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

_FIELD = object()


def _get(d: dict | None, key: str) -> Any:
    if not isinstance(d, dict):
        return _FIELD
    return d.get(key, _FIELD)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _copy_job(job: dict) -> dict:
    return copy.deepcopy(job)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def due_jobs(jobs: list[dict], now: float) -> list[dict]:
    """返回到期任务副本。规则：never-run / 间隔到 / paused / 全局闸门跳过。"""
    gate_state = _gate_raw({"paused": False, "pausedUntil": None}, now)
    if gate_state["blocked"]:
        return []

    result: list[dict] = []
    for job in jobs or []:
        every = _get(job, "everySeconds")
        last = _get(job, "lastRunAt")
        paused = _get(job, "paused")
        if not _is_number(every) or every <= 0:
            continue
        if paused is True:
            continue
        if (last is _FIELD) or (not _is_number(last)) or (last <= 0):
            result.append(_copy_job(job))
            continue
        if (now - last) >= every:
            result.append(_copy_job(job))
    return result


def acquire_lease(job: dict, owner: str, now: float, ttl_seconds: float) -> tuple[bool, dict]:
    """租约互斥。返回 (got_lease, updated_job)。"""
    if not isinstance(owner, str) or owner == "":
        return False, _copy_job(job)
    if not _is_number(ttl_seconds) or ttl_seconds <= 0:
        return False, _copy_job(job)

    updated = _copy_job(job)
    lease_owner = _get(job, "leaseOwner")
    lease_expires = _get(job, "leaseExpiresAt")

    has_lease = (
        isinstance(lease_owner, str)
        and lease_owner != ""
        and _is_number(lease_expires)
        and lease_expires > 0
    )

    if has_lease and lease_expires > now and lease_owner != owner:
        return False, updated

    updated["leaseOwner"] = owner
    updated["leaseExpiresAt"] = now + ttl_seconds
    return True, updated


def _gate_raw(state: dict, now: float) -> dict:
    paused = _get(state, "paused")
    paused_until = _get(state, "pausedUntil")
    if paused is not True:
        return {"blocked": False, "reason": ""}
    if paused_until is _FIELD or paused_until is None or not _is_number(paused_until):
        return {"blocked": True, "reason": "全局暂停（永久）"}
    if paused_until <= now:
        return {"blocked": False, "reason": ""}
    remaining = int(paused_until - now)
    return {"blocked": True, "reason": f"全局暂停中，还剩 {remaining} 秒"}


def gate(state: dict, now: float) -> dict:
    """全局暂停闸门。返回 {"blocked": bool, "reason": str}。"""
    return _gate_raw(state, now)


# ---------------------------------------------------------------------------
# register / selftest
# ---------------------------------------------------------------------------

def register(api) -> dict[str, Any]:
    return {
        "name": "scheduler",
        "version": "1.0.0",
        "capabilities": [
            "schedule.due-check",
            "schedule.periodic",
            "scheduler.lease",
            "scheduler.gate",
        ],
        "hooks": {
            "on_due_check": due_jobs,
            "on_lease": acquire_lease,
            "on_gate": gate,
        },
        "author": "keystone contrib (stepclaw)",
        "summary": "Periodic job scheduling with lease mutual-exclusion and global pause gate.",
    }


def selftest() -> list[tuple[str, bool, str]]:
    now = 1_000_000.0
    jobs_base = [
        {"id": "fresh",   "everySeconds": 60,  "lastRunAt": 0,       "paused": False},
        {"id": "due",     "everySeconds": 60,  "lastRunAt": 999_300, "paused": False},
        {"id": "not-due", "everySeconds": 60,  "lastRunAt": 999_990, "paused": False},
        {"id": "broken",  "everySeconds": 0,   "lastRunAt": 0,       "paused": False},
        {"id": "paused",  "everySeconds": 60,  "lastRunAt": 0,       "paused": True},
    ]

    fired = due_jobs(jobs_base, now)
    ids = [j["id"] for j in fired]

    # lease tests
    job_no_lease  = {"id": "f", "everySeconds": 60}
    got1, upd1    = acquire_lease(job_no_lease, "node-1", now, 30.0)
    job_expired   = {"id": "g", "everySeconds": 60, "leaseOwner": "node-2", "leaseExpiresAt": now - 10}
    got2, upd2    = acquire_lease(job_expired, "node-1", now, 30.0)
    job_active    = {"id": "h", "everySeconds": 60, "leaseOwner": "node-2", "leaseExpiresAt": now + 100}
    got3, _       = acquire_lease(job_active, "node-1", now, 30.0)
    got4, _       = acquire_lease(job_no_lease, "", now, 30.0)

    # gate tests
    s_no_pause   = gate({"paused": False, "pausedUntil": None}, now)
    s_forever    = gate({"paused": True,  "pausedUntil": None}, now)
    s_expired    = gate({"paused": True,  "pausedUntil": now - 1}, now)
    s_active     = gate({"paused": True,  "pausedUntil": now + 3600}, now)

    manifest = register(None)

    # immutability
    original = [{"id": "imm", "everySeconds": 60, "lastRunAt": 0, "paused": False}]
    ret      = due_jobs(original, now)
    original_copy = [{"id": "imm", "everySeconds": 60, "lastRunAt": 0, "paused": False}]
    ret_none = due_jobs(None, now)  # type: ignore[arg-type]

    return [
        # due_jobs — 5 条
        ("never-run-is-due",          "fresh"  in ids,    str(ids)),
        ("interval-met-is-due",       "due"    in ids,    str(ids)),
        ("within-interval-not-due",   "not-due" not in ids, str(ids)),
        ("zero-interval-skipped",     "broken" not in ids, str(ids)),
        ("paused-job-skipped",        "paused" not in ids, str(ids)),
        # lease — 4 条
        ("no-lease-acquired",         got1 is True,       ""),
        ("expired-lease-reclaimed",   got2 is True and upd2["leaseOwner"] == "node-1", ""),
        ("active-lease-denied",       got3 is False,      ""),
        ("empty-owner-rejected",      got4 is False,      ""),
        # gate — 4 条
        ("not-paused-not-blocked",    s_no_pause["blocked"] is False, ""),
        ("forever-paused-blocked",    s_forever["blocked"] is True,  ""),
        ("expired-pause-released",    s_expired["blocked"] is False, ""),
        ("active-pause-blocks",       s_active["blocked"] is True,   ""),
        # 不变性 — 3 条
        ("returns-copy",              ret and ret[0] is not original[0], ""),
        ("original-unchanged",        original == original_copy,       ""),
        ("none-input-safe",           ret_none == [],                  ""),
        # manifest — 2 条
        ("manifest-name-ok",          manifest["name"] == "scheduler",  ""),
        ("manifest-has-capabilities", len(manifest["capabilities"]) >= 4, ""),
    ]


__all__ = ["MODULE_API_VERSION", "register", "selftest"]
