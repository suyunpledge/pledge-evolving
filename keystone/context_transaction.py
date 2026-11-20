"""Durable source archives and guarded, synchronous history replacement.

Archives contain redacted source messages. Checkpoints describe provenance,
not verified facts. No transaction IDs or diagnostics enter model prompts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

from .secrets import redact


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def archive_source(directory, transaction_id, messages):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    safe = redact(messages)
    archive_id = "ctx-" + transaction_id
    path = root / (archive_id + ".json")
    pending = path.with_suffix(".pending")
    # Incomplete writes retain a .pending extension, never a valid archive ID.
    with pending.open("x", encoding="utf-8") as stream:
        json.dump({"version": 1, "archive_id": archive_id, "messages": safe,
                   "archive_digest": digest(safe)}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    return {"archive_id": archive_id, "path": str(path), "archive_digest": digest(safe),
            "message_count": len(safe)}


def valid_tool_pairs(messages):
    """Require complete OpenAI or Anthropic call groups; never repair silently."""
    pending = set()
    for message in messages:
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        calls = [c.get("id") for c in message.get("tool_calls") or []]
        calls += [b.get("id") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = ([message.get("tool_call_id")] if message.get("role") == "tool" else [])
        results += [b.get("tool_use_id") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if pending and not results:
            return False
        for call in calls:
            if not isinstance(call, str) or not call or call in pending:
                return False
            pending.add(call)
        for result in results:
            if result not in pending:
                return False
            pending.remove(result)
    return not pending


def compact_transaction(messages, compactor, *, directory, max_chars, pinned_contents=(),
                        validate=None, cancelled=None, state=None, archiver=None, prefix_messages=()):
    """Return (original-or-committed-history, audit). All callbacks are host-owned."""
    transaction_id = uuid.uuid4().hex
    source = copy.deepcopy(messages)
    before = compactor.size(source)
    source_digest = digest(source)
    state_digest = digest(state()) if state else ""
    audit = {"transaction_id": transaction_id, "before": before, "after": before,
             "source_digest": source_digest, "shrunk": False, "committed": False}

    def unchanged():
        return (digest(messages) == source_digest
                and (not state or digest(state()) == state_digest))

    def abort(reason):
        audit["reason"] = reason
        return messages, audit

    if cancelled and cancelled():
        return abort("cancelled")
    try:
        archive = (archiver or archive_source)(directory, transaction_id, source)
        if not isinstance(archive, dict) or not archive.get("archive_id"):
            return abort("archive_invalid")
        audit["archive"] = archive
        if not unchanged():
            return abort("source_or_policy_changed")
        options = {'max_chars': max_chars, 'pinned_contents': pinned_contents}
        if prefix_messages:
            options['prefix_messages'] = copy.deepcopy(prefix_messages)
        candidate = compactor.compact(copy.deepcopy(source), **options)
        if cancelled and cancelled():
            return abort("cancelled")
        if not unchanged():
            return abort("source_or_policy_changed")
        if compactor.size(candidate) >= before:
            return abort("no_reclaim")
        if candidate[:len(prefix_messages)] != list(prefix_messages):
            return abort('stable_prefix_changed')
        protected = [m for m in source if m.get("role") in {"system", "developer"} or
                     (isinstance(m.get("content"), str) and m["content"] in pinned_contents and m["content"])]
        cursor = 0
        for message in protected:
            while cursor < len(candidate) and candidate[cursor] != message:
                cursor += 1
            if cursor == len(candidate):
                return abort("protected_message_changed")
            cursor += 1
        if not valid_tool_pairs(candidate):
            return abort("invalid_tool_pairs")
        if validate and not validate(candidate):
            return abort("candidate_does_not_fit")
        if (cancelled and cancelled()) or not unchanged():
            return abort("cancelled_or_state_changed")
        audit.update(committed=True, shrunk=True, after=compactor.size(candidate),
                     candidate_digest=digest(candidate), reason="committed",
                     checkpoint={"version": 1, "archive_id": archive["archive_id"],
                                 "coverage": {"start": 0, "end_exclusive": len(source)},
                                 "summary_status": "unverified_excerpt"})
        return candidate, audit
    except Exception as exc:
        # Do not include callback text (which might contain source material).
        return abort("transaction_failed:" + type(exc).__name__)
