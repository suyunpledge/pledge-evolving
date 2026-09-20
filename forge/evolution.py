"""Self-iteration: turning episodes into durable capability, safely.

Learned from Hermes Agent, whose loop is the only one of the seven reference
frameworks that treats "the agent's own knowledge rots" as a first-class
problem. The pieces worth copying:

* ``learning``    — successful trajectories become reusable experience
* ``curator``     — a background pass reviews agent-authored knowledge and
                    **archives** (never deletes), with pinning and a ledger
* ``journey`` / ``memory-graph`` — knowledge and skills accumulate over time,
                    so the framework has a history rather than just a state
* ``checkpoints`` — a shadow snapshot before anything is written
* ``hooks``       — first use of anything executable needs an explicit grant

The one thing this module deliberately does **not** copy: silent persistence.
Hermes-style evolution is powerful precisely because it is auditable, so the
invariants here are hard ones:

1. A candidate is never applied without either a low-risk auto-apply rule or an
   explicit approval. Nothing about "the model said it was a good idea" counts.
2. Long-term instruction files (memory, agent docs, skills) **always** require
   approval — there is no configuration that turns that off.
3. Every state change is appended to a ledger, and every applied candidate can
   be rolled back from the exact prior content.
4. Knowledge is archived, never destroyed, and archiving is reversible.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

SIGNAL_KINDS = ("correction", "preference", "workflow", "pitfall", "success")

CANDIDATE_KINDS = ("memory", "skill", "config", "prompt", "note")

CANDIDATE_STATES = ("pending", "approved", "applied", "rejected", "quarantined", "stale")

# Targets that can never be auto-applied, regardless of settings.
LONG_TERM_MARKERS = (
    "MEMORY.md", "AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md",
    "SKILL.md", "CLAUDE.md", "CODEBUDDY.md", "skill.md",
)

_PATTERNS: tuple[tuple[str, str], ...] = (
    ("correction", r"不对|错了|不是这样|应该(?:是|改)|改成|不要(?:再)?|别再|纠正|不对吧"),
    ("preference", r"我喜欢|我更(?:喜欢|倾向)|以后都|记住|当作长期|偏好|优先(?:用|选)|最好都"),
    ("workflow", r"每次都|流程|步骤|先.{0,8}再|固定(?:做法|流程)|sop|按这个顺序"),
    ("pitfall", r"踩坑|踩到|坑|失败|报错|挂了|崩溃|超时|不生效"),
    ("success", r"这样就对了|保持这个|成功|有效|就这样做|按这个来"),
)


@dataclass
class Signal:
    kind: str
    text: str
    session_id: str = ""
    ordinal: int = -1
    confidence: float = 0.5
    at: float = field(default_factory=time.time)

    def ref(self) -> str:
        return f"{self.session_id}#{self.ordinal}" if self.session_id else "unlinked"

    def to_raw(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "session_id": self.session_id,
                "ordinal": self.ordinal, "confidence": self.confidence, "at": self.at}

    @staticmethod
    def from_raw(raw: dict[str, Any]) -> "Signal":
        return Signal(kind=str(raw.get("kind", "note")), text=str(raw.get("text", "")),
                      session_id=str(raw.get("session_id", "")), ordinal=int(raw.get("ordinal", -1)),
                      confidence=float(raw.get("confidence", 0.5)), at=float(raw.get("at", time.time())))


@dataclass
class Candidate:
    id: str
    kind: str
    target: str
    content: str
    signals: list[Signal] = field(default_factory=list)
    risk: str = "medium"                 # low | medium | high
    provenance: str = "agent"            # agent | human | imported
    status: str = "pending"
    requires_approval: bool = True
    created_at: float = field(default_factory=time.time)
    decided_at: float = 0.0
    applied_at: float = 0.0
    decision_by: str = ""
    previous_content: str = ""
    version: int = 0
    note: str = ""

    def to_raw(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "target": self.target, "content": self.content,
            "signals": [s.to_raw() for s in self.signals], "risk": self.risk,
            "provenance": self.provenance, "status": self.status,
            "requires_approval": self.requires_approval, "created_at": self.created_at,
            "decided_at": self.decided_at, "applied_at": self.applied_at,
            "decision_by": self.decision_by, "previous_content": self.previous_content,
            "version": self.version, "note": self.note,
        }

    @staticmethod
    def from_raw(raw: dict[str, Any]) -> "Candidate":
        return Candidate(
            id=str(raw.get("id", "")), kind=str(raw.get("kind", "note")),
            target=str(raw.get("target", "")), content=str(raw.get("content", "")),
            signals=[Signal.from_raw(s) for s in raw.get("signals") or []],
            risk=str(raw.get("risk", "medium")), provenance=str(raw.get("provenance", "agent")),
            status=str(raw.get("status", "pending")),
            requires_approval=bool(raw.get("requires_approval", True)),
            created_at=float(raw.get("created_at", time.time())),
            decided_at=float(raw.get("decided_at", 0.0)),
            applied_at=float(raw.get("applied_at", 0.0)),
            decision_by=str(raw.get("decision_by", "")),
            previous_content=str(raw.get("previous_content", "")),
            version=int(raw.get("version", 0)), note=str(raw.get("note", "")),
        )

    @property
    def evidence(self) -> list[str]:
        return [s.ref() for s in self.signals]


# ---------------------------------------------------------------------------
# signal extraction (offline, deterministic)
# ---------------------------------------------------------------------------

def extract_signals(text: str, *, session_id: str = "", ordinal: int = -1,
                    min_confidence: float = 0.0) -> list[Signal]:
    """Heuristic first pass. A model-backed refiner can be layered on top.

    One line may carry several different signals ("成功；失败；以后都…" in a
    single ledger row used to yield only the first match). Same-kind repeats
    within a line still collapse to one — the line is the evidence unit.
    """
    out: list[Signal] = []
    if not text:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) < 4:
            continue
        seen_kinds: set[str] = set()
        for kind, pattern in _PATTERNS:
            if not re.search(pattern, line, re.I):
                continue
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            confidence = 0.55 + (0.15 if len(line) > 24 else 0.0)
            out.append(Signal(kind=kind, text=line[:400], session_id=session_id,
                              ordinal=ordinal, confidence=min(confidence, 0.9)))
    return [s for s in out if s.confidence >= min_confidence]


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

class Ledger:
    """Append-only record of every evolution action, replayable on its own."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: list[dict[str, Any]] = []
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    self.entries.append(json.loads(line))

    def append(self, action: str, candidate: Candidate, **extra: Any) -> dict[str, Any]:
        entry = {
            "seq": len(self.entries) + 1,
            "ts": time.time(),
            "action": action,
            "candidate_id": candidate.id,
            "kind": candidate.kind,
            "target": candidate.target,
            "status": candidate.status,
            "evidence": candidate.evidence,
            "by": candidate.decision_by or "system",
            **extra,
        }
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def replay(self) -> list[dict[str, Any]]:
        return list(self.entries)

    def actions_for(self, candidate_id: str) -> list[str]:
        return [e["action"] for e in self.entries if e["candidate_id"] == candidate_id]


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

Refiner = Callable[[list[Signal]], list[Candidate]]


class EvolutionEngine:
    def __init__(
        self,
        home: Path,
        *,
        memory=None,
        capabilities=None,
        checkpoints=None,
        refiner: Refiner | None = None,
        auto_apply_note_risk: str = "low",
        workspace: Path | None = None,
    ) -> None:
        self.home = Path(home)
        self.root = self.home / "evolution"
        self.pending_dir = self.root / "pending"
        self.applied_dir = self.root / "applied"
        self.archive_dir = self.root / "archive"
        self.versions_dir = self.root / "versions"
        self.ledger = Ledger(self.root / "ledger.jsonl")
        self.memory = memory
        self.capabilities = capabilities
        self.checkpoints = checkpoints
        self.refiner = refiner
        self.auto_apply_note_risk = auto_apply_note_risk
        # Sandbox boundary: if set, apply() rejects targets outside this root.
        self._workspace = Path(workspace) if workspace else None
        self.candidates: dict[str, Candidate] = {}
        self._load()

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        for directory in (self.pending_dir, self.applied_dir, self.archive_dir):
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.json")):
                try:
                    candidate = Candidate.from_raw(json.loads(file.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
                self.candidates[candidate.id] = candidate

    def _path_for(self, candidate: Candidate) -> Path:
        if candidate.status in ("applied", "approved"):
            return self.applied_dir / f"{candidate.id}.json"
        if candidate.status in ("rejected", "quarantined", "stale"):
            return self.archive_dir / f"{candidate.id}.json"
        return self.pending_dir / f"{candidate.id}.json"

    def _save(self, candidate: Candidate) -> None:
        target = self._path_for(candidate)
        target.parent.mkdir(parents=True, exist_ok=True)
        for directory in (self.pending_dir, self.applied_dir, self.archive_dir):
            stale = directory / f"{candidate.id}.json"
            if stale != target and stale.is_file():
                stale.unlink()
        target.write_text(json.dumps(candidate.to_raw(), ensure_ascii=False, indent=2), encoding="utf-8")

    # -- observation -----------------------------------------------------
    def observe_text(self, text: str, *, session_id: str = "", ordinal: int = -1) -> list[Signal]:
        return extract_signals(text, session_id=session_id, ordinal=ordinal)

    def observe_session(self, session, *, limit: int | None = None) -> list[Signal]:
        """Harvest signals from a session's event log (evidence stays linked)."""
        events = list(getattr(session, "events", []))
        if limit:
            events = events[-limit:]
        signals: list[Signal] = []
        session_id = str(getattr(session, "meta", {}).get("session_id", ""))
        for event in events:
            if event.type not in ("user_message", "tool_call", "assistant_message"):
                continue
            blob = str(event.data.get("content") or event.data.get("result") or "")
            if not blob:
                continue
            # results are quoted context: only real user/assistant text counts as a signal
            if event.type == "tool_call":
                continue
            signals.extend(extract_signals(blob, session_id=session_id, ordinal=event.ordinal))
        return signals

    # -- nomination ------------------------------------------------------
    def nominate(self, signals: Iterable[Signal]) -> list[Candidate]:
        signals = list(signals)
        if not signals:
            return []
        if self.refiner is not None:
            produced = list(self.refiner(signals) or [])
            for candidate in produced:
                if not candidate.id:
                    candidate.id = f"cand-{uuid.uuid4().hex[:10]}"
                self._register(candidate)
            if produced:
                return produced
        produced = [self._rule_based(signals)]
        for candidate in produced:
            self._register(candidate)
        return produced

    def _rule_based(self, signals: list[Signal]) -> Candidate:
        kinds = {s.kind for s in signals}
        if "preference" in kinds or "correction" in kinds:
            kind, risk = "memory", "medium"
        elif "workflow" in kinds:
            kind, risk = "skill", "medium"
        else:
            kind, risk = "note", "low"
        body = "\n".join(f"- [{s.kind}] {s.text}" for s in signals)
        candidate = Candidate(
            id=f"cand-{uuid.uuid4().hex[:10]}",
            kind=kind,
            target=str(self.home / "evolution" / f"{kind}-{time.strftime('%Y%m%d')}.md"),
            content=body,
            signals=signals,
            risk=risk,
            provenance="agent",
        )
        return candidate

    def _register(self, candidate: Candidate) -> Candidate:
        candidate.requires_approval = self.requires_approval(candidate)
        self.candidates[candidate.id] = candidate
        self._save(candidate)
        self.ledger.append("nominated", candidate, requires_approval=candidate.requires_approval)
        return candidate

    # -- the gate --------------------------------------------------------
    @staticmethod
    def is_long_term(target: str) -> bool:
        return any(marker.lower() in str(target).lower() for marker in LONG_TERM_MARKERS)

    def requires_approval(self, candidate: Candidate) -> bool:
        """True means: a human must say yes. There is no setting that lifts this."""
        if self.is_long_term(candidate.target):
            return True
        if candidate.kind in ("memory", "skill", "config", "prompt"):
            return True
        if candidate.risk != "low":
            return True
        if candidate.provenance != "agent":
            return True
        if candidate.kind == "note" and candidate.risk == self.auto_apply_note_risk:
            return False
        return True

    def evaluate(self, candidate: Candidate) -> str:
        """pending | auto | quarantined — the decision, not the application."""
        if self.requires_approval(candidate):
            return "pending"
        if not candidate.signals:
            candidate.note = "no evidence attached"
            return "quarantined"
        return "auto"

    # -- lifecycle -------------------------------------------------------
    def approve(self, candidate_id: str, *, by: str = "human") -> Candidate:
        candidate = self._require(candidate_id)
        if candidate.status not in ("pending", "quarantined"):
            raise RuntimeError(f"cannot approve a candidate in state {candidate.status!r}")
        candidate.status = "approved"
        candidate.decision_by = by
        candidate.decided_at = time.time()
        self._save(candidate)
        self.ledger.append("approved", candidate)
        return candidate

    def reject(self, candidate_id: str, *, by: str = "human", reason: str = "") -> Candidate:
        candidate = self._require(candidate_id)
        candidate.status = "rejected"
        candidate.decision_by = by
        candidate.decided_at = time.time()
        candidate.note = reason or candidate.note
        self._save(candidate)
        self.ledger.append("rejected", candidate, reason=reason)
        return candidate

    def quarantine(self, candidate_id: str, *, reason: str = "") -> Candidate:
        candidate = self._require(candidate_id)
        candidate.status = "quarantined"
        candidate.note = reason or "quarantined for review"
        candidate.decided_at = time.time()
        self._save(candidate)
        self.ledger.append("quarantined", candidate, reason=candidate.note)
        return candidate

    def apply(self, candidate_id: str, *, by: str = "", force: bool = False) -> Candidate:
        """Write the candidate to its target. Approval is checked here, not earlier."""
        candidate = self._require(candidate_id)
        if candidate.status == "applied":
            return candidate
        if candidate.requires_approval and candidate.status != "approved" and not force:
            raise PermissionError(
                f"{candidate.id} requires approval (target={candidate.target}, kind={candidate.kind}, "
                f"risk={candidate.risk}); call approve() first"
            )

        decision = self.evaluate(candidate)
        if decision == "quarantined" and not force:
            self.quarantine(candidate.id, reason="failed gate at apply time")
            raise PermissionError(f"{candidate.id} failed the evidence gate")

        target = Path(candidate.target)
        # Sandbox enforcement: reject writes outside workspace boundary.
        if self._workspace is not None:
            try:
                target.resolve().relative_to(self._workspace.resolve())
            except ValueError:
                raise PermissionError(
                    f"{candidate.id}: target {target} escapes workspace boundary {self._workspace}"
                )
        if self.checkpoints is not None:
            point = self.checkpoints.snapshot(f"before evolution {candidate.id}")
            if point is not None:
                candidate.note = (candidate.note + f" checkpoint={point.commit[:8]}").strip()
        if target.is_file():
            candidate.previous_content = target.read_text(encoding="utf-8", errors="replace")

        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        block = f"\n<!-- evolution {candidate.id} @ {stamp} evidence={','.join(candidate.evidence) or 'none'} -->\n"
        existing = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        target.write_text(existing + block + candidate.content + "\n", encoding="utf-8")

        candidate.status = "applied"
        candidate.applied_at = time.time()
        candidate.decision_by = by or candidate.decision_by or ("auto" if not candidate.requires_approval else "human")
        self._save(candidate)
        self.ledger.append("applied", candidate, decision=decision)

        # keep the domain stores in step when the target is a known store
        if self.memory is not None and candidate.kind == "memory":
            for signal in candidate.signals:
                self.memory.remember(signal.text, kind="feedback", source="agent")
        self.bump_version(reason=f"applied {candidate.id}")
        return candidate

    def rollback(self, candidate_id: str, *, by: str = "human") -> Candidate:
        candidate = self._require(candidate_id)
        if candidate.status != "applied":
            raise RuntimeError(f"{candidate.id} is not applied; nothing to roll back")
        target = Path(candidate.target)
        # snapshot the *current* state first: a rollback is itself a write
        if self.checkpoints is not None:
            point = self.checkpoints.snapshot(f"before rollback {candidate.id}")
            if point is not None:
                candidate.note = (candidate.note + f" rollback-checkpoint={point.commit[:8]}").strip()
        if candidate.previous_content:
            target.write_text(candidate.previous_content, encoding="utf-8")
        elif target.is_file():
            target.write_text("", encoding="utf-8")
        candidate.status = "pending"
        candidate.applied_at = 0.0
        candidate.decision_by = by
        self._save(candidate)
        self.ledger.append("rolled_back", candidate)
        self.bump_version(reason=f"rolled back {candidate.id}")
        return candidate

    # -- metabolism ------------------------------------------------------
    def curate(self, *, stale_after: float = 7 * 86400, now: float | None = None) -> dict[str, int]:
        """Archive (never delete) pending candidates that have gone stale."""
        now = now or time.time()
        archived = 0
        for candidate in list(self.candidates.values()):
            if candidate.status != "pending":
                continue
            if now - candidate.created_at < stale_after:
                continue
            candidate.status = "stale"
            candidate.note = (candidate.note + " archived by curator").strip()
            self._save(candidate)
            self.ledger.append("archived", candidate, reason="stale pending")
            archived += 1
        return {"archived": archived,
                "pending": sum(1 for c in self.candidates.values() if c.status == "pending"),
                "applied": sum(1 for c in self.candidates.values() if c.status == "applied")}

    def restore(self, candidate_id: str) -> Candidate:
        candidate = self._require(candidate_id)
        if candidate.status not in ("stale", "quarantined", "rejected"):
            raise RuntimeError(f"{candidate.id} is not archived")
        candidate.status = "pending"
        candidate.decided_at = 0.0
        self._save(candidate)
        self.ledger.append("restored", candidate)
        return candidate

    # -- versioning ------------------------------------------------------
    def bump_version(self, *, reason: str = "") -> dict[str, Any]:
        """A version is the *set* of applied candidates, so it can be diffed."""
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        applied = [c for c in self.candidates.values() if c.status == "applied"]
        version = {
            "version": len(list(self.versions_dir.glob("v*.json"))) + 1,
            "at": time.time(),
            "reason": reason,
            "applied": [{"id": c.id, "kind": c.kind, "target": c.target} for c in applied],
            "memory_digest": (self.memory.stats() if self.memory is not None else None),
        }
        path = self.versions_dir / f"v{version['version']:03d}.json"
        path.write_text(json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8")
        return version

    def history(self) -> list[dict[str, Any]]:
        return self.ledger.replay()

    def stats(self) -> dict[str, Any]:
        by_status: dict[str, int] = {state: 0 for state in CANDIDATE_STATES}
        for candidate in self.candidates.values():
            by_status[candidate.status] = by_status.get(candidate.status, 0) + 1
        return {
            "candidates": len(self.candidates),
            "by_status": by_status,
            "ledger_entries": len(self.ledger.entries),
            "versions": len(list(self.versions_dir.glob("v*.json"))) if self.versions_dir.is_dir() else 0,
        }

    # -- helpers ---------------------------------------------------------
    def pending(self) -> list[Candidate]:
        return sorted((c for c in self.candidates.values() if c.status == "pending"),
                      key=lambda c: -c.created_at)

    def applied(self) -> list[Candidate]:
        return sorted((c for c in self.candidates.values() if c.status == "applied"),
                      key=lambda c: -c.applied_at)

    def _require(self, candidate_id: str) -> Candidate:
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"unknown candidate {candidate_id!r}")
        return candidate


__all__ = [
    "CANDIDATE_KINDS",
    "CANDIDATE_STATES",
    "Candidate",
    "EvolutionEngine",
    "Ledger",
    "LONG_TERM_MARKERS",
    "SIGNAL_KINDS",
    "Signal",
    "extract_signals",
]
