"""Small, deterministic task-contract state machine for Forge runs."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from xml.sax.saxutils import escape as _xml_escape


class ContractStatus(str, Enum):
    EXPLORING = "exploring"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    READY_TO_DELIVER = "ready_to_deliver"


_RANK = {
    ContractStatus.EXPLORING.value: 0,
    ContractStatus.PLANNING.value: 1,
    ContractStatus.EXECUTING.value: 2,
    ContractStatus.VERIFYING.value: 3,
    ContractStatus.READY_TO_DELIVER.value: 4,
    ContractStatus.BLOCKED.value: 0,
}
_TASK_WORDS = re.compile(r"\b(fix|add|change|implement|build|update|refactor|write|create|修复|新增|修改|实现|编写|创建)\b", re.I)
_FOLLOW_WORDS = re.compile(r"\b(continue|also|then|follow[- ]?up|继续|另外|然后|再)\b", re.I)
_CONSTRAINT_WORDS = re.compile(
    r"(?:不要|不能|禁止|必须)\s*([^。.!?\n]{2,80})"
    r"|(?:never|must|do not|don't|avoid)\s+([^。.!?\n]{2,80})", re.I)
_FILE_PATTERN = re.compile(r"(?<![\w./-])([\w.-]+(?:/[\w.-]+)*\.(?:py|ts|js|json|md|toml|yaml|yml|txt))(?![\w.-])")


@dataclass(frozen=True)
class TaskContract:
    objective: str
    status: str = ContractStatus.EXPLORING.value
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    mentioned_files: tuple[str, ...] = ()
    turn: int = 0
    version: int = 1

    def to_raw(self) -> dict:
        raw = asdict(self)
        for key in ("constraints", "success_criteria", "scope", "mentioned_files"):
            raw[key] = list(raw[key])
        return raw

    @classmethod
    def from_raw(cls, raw: dict) -> "TaskContract":
        if not isinstance(raw, dict):
            raise ValueError("contract must be an object")
        status = str(raw.get("status", ContractStatus.EXPLORING.value))
        if status not in _RANK:
            raise ValueError(f"unknown contract status: {status}")
        return cls(
            objective=str(raw.get("objective", "")).strip(), status=status,
            constraints=tuple(str(x) for x in raw.get("constraints") or ()),
            success_criteria=tuple(str(x) for x in raw.get("success_criteria") or ()),
            scope=tuple(str(x) for x in raw.get("scope") or ()),
            mentioned_files=tuple(str(x) for x in raw.get("mentioned_files") or ()),
            turn=max(0, int(raw.get("turn", 0))), version=int(raw.get("version", 1)),
        )

    def render_projection(self, *, max_constraints: int = 3, max_criteria: int = 2,
                          include_objective: bool = True) -> str:
        """Stable boundary metadata; omit objective only beside the full task."""
        esc = lambda value: _xml_escape(str(value), {'"': "&quot;"})
        rows = [f'<task-contract status="{esc(self.status)}" authoritative="true">']
        if include_objective:
            rows.append(f"<objective>{esc(self.objective)}</objective>")
        for label, values, limit in (
                ("constraint", self.constraints, max_constraints),
                ("success-criterion", self.success_criteria, max_criteria)):
            for value in values[:limit]:
                rows.append(f"<{label}>{esc(value)}</{label}>")
        if self.mentioned_files:
            rows.append("<scope>" + ",".join(esc(x) for x in self.mentioned_files) + "</scope>")
        rows.append("</task-contract>")
        return "".join(rows)

    def render_anchor(self) -> str:
        return "<task-anchor authoritative=\"true\">" + self.render_projection() + "</task-anchor>"


def extract_task_contract(text: str, *, turn: int = 0) -> TaskContract:
    text = str(text or "").strip()
    constraints = tuple(dict.fromkeys((m.group(1) or m.group(2)).strip()
                                      for m in _CONSTRAINT_WORDS.finditer(text)))
    files = tuple(dict.fromkeys(_FILE_PATTERN.findall(text)))
    criteria = tuple(f"完成并验证：{path}" for path in files[:2])
    scope = files[:8]
    return TaskContract(objective=text[:500], constraints=constraints[:8],
                        success_criteria=criteria, scope=scope,
                        mentioned_files=files[:16], turn=max(0, int(turn)))


def classify_turn_mode(text: str, existing: TaskContract | None = None) -> str:
    value = str(text or "").strip()
    if not value:
        return "chat"
    if existing is not None and _FOLLOW_WORDS.search(value) and not _TASK_WORDS.search(value):
        return "followUp"
    if _TASK_WORDS.search(value) or _FILE_PATTERN.search(value):
        return "task"
    return "followUp" if existing is not None else "chat"


def advance_status(contract: TaskContract, next_status: str | ContractStatus) -> TaskContract:
    target = str(next_status.value if isinstance(next_status, ContractStatus) else next_status)
    if target not in _RANK:
        raise ValueError(f"unknown contract status: {target}")
    current = contract.status
    if current == ContractStatus.BLOCKED.value or target == ContractStatus.BLOCKED.value:
        return TaskContract(**{**contract.to_raw(), "constraints": tuple(contract.constraints),
                               "success_criteria": tuple(contract.success_criteria),
                               "scope": tuple(contract.scope),
                               "mentioned_files": tuple(contract.mentioned_files),
                               "status": target})
    if target == ContractStatus.EXECUTING.value and current in {
            ContractStatus.READY_TO_DELIVER.value, ContractStatus.VERIFYING.value}:
        return TaskContract(**{**contract.to_raw(), "constraints": tuple(contract.constraints),
                               "success_criteria": tuple(contract.success_criteria),
                               "scope": tuple(contract.scope),
                               "mentioned_files": tuple(contract.mentioned_files),
                               "status": target})
    if _RANK[target] < _RANK[current]:
        return contract
    return TaskContract(**{**contract.to_raw(), "constraints": tuple(contract.constraints),
                           "success_criteria": tuple(contract.success_criteria),
                           "scope": tuple(contract.scope),
                           "mentioned_files": tuple(contract.mentioned_files),
                           "status": target})


__all__ = ["ContractStatus", "TaskContract", "advance_status", "classify_turn_mode",
           "extract_task_contract"]
