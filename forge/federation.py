"""Heterogeneous worker federation.

The problem, stated plainly: seven agent CLIs on one machine are not seven
functions. Each one is a process with its own protocol, its own sandbox, its
own auth, its own idea of what stdout means. Treating them as callables fails
in at least six distinct ways, all of which were observed while building this:

1. **sandbox refusal** — opencode auto-rejects reads outside its workspace, and
   an optimising agent will burn its whole budget retrying the same denied read
2. **protocol mismatch** — Claude Code only speaks the Anthropic Messages wire;
   the cheap models speak OpenAI's
3. **vendor auth** — some CLIs gate non-default providers behind their own service
4. **stdout noise** — banners, box drawing, ANSI, tool logs, progress lines
5. **hang without failure** — a streaming proxy that never closes looks like a
   slow model, not a broken one
6. **silent degradation** — exit code 0 with an empty or truncated answer

So a worker here is a first-class descriptor, dispatch classifies failures
rather than just timing out, and output goes through a cleaner before anyone
reads it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# worker description
# ---------------------------------------------------------------------------

COST_TIERS = ("free", "cheap", "standard", "premium")
PERMISSION_TIERS = ("read-only", "workspace-write", "full-access")


@dataclass
class WorkerSpec:
    name: str
    argv: list[str]                     # "{task}" placeholder is substituted
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()  # tags used for matching
    cost: str = "standard"
    permission: str = "read-only"
    protocol: str = "text"              # text | json | anthropic | openai
    timeout_s: int = 900
    max_attempts: int = 1
    health_argv: list[str] = field(default_factory=list)
    notes: str = ""
    available: bool = True
    sandbox_note: str = ""

    def command(self, task: str) -> list[str]:
        return [part.replace("{task}", task) for part in self.argv]

    def to_raw(self) -> dict[str, Any]:
        return {
            "name": self.name, "cost": self.cost, "permission": self.permission,
            "protocol": self.protocol, "capabilities": list(self.capabilities),
            "available": self.available, "notes": self.notes,
        }


@dataclass
class WorkerResult:
    worker: str
    ok: bool
    text: str = ""
    raw: str = ""
    exit_code: int | None = None
    duration_s: float = 0.0
    failure: str = ""                   # timeout | sandbox | protocol | model | empty | refused | error
    attempts: int = 1
    cleaned_bytes: int = 0

    def to_raw(self) -> dict[str, Any]:
        return {
            "worker": self.worker, "ok": self.ok, "failure": self.failure,
            "exit_code": self.exit_code, "duration_s": round(self.duration_s, 2),
            "attempts": self.attempts, "chars": len(self.text),
        }


# ---------------------------------------------------------------------------
# output cleaning
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BOX_CHARS = set("─│╭╮╰╯━┃┏┓┗┛┆┊╌╎▌▐▖▗▘▝")
NOISE_MARKERS = (
    "NativeCommandError", "+ CategoryInfo", "+ FullyQualifiedErrorId",
    "process exited with code", "at line:", "> build ·",
)
ERROR_MARKERS = (
    "there's an issue with the selected model",
    "not found or method not allowed",
    "connection refused",
    "no module named",
    "permission denied",
)


def clean_output(raw: str) -> str:
    """Strip terminal noise while keeping the payload."""
    if not raw:
        return ""
    text = ANSI_RE.sub("", raw)
    out: list[str] = []
    for line in text.splitlines():
        stripped = "".join(ch for ch in line if ch not in BOX_CHARS).rstrip()
        if not stripped.strip():
            out.append("")
            continue
        if any(marker in stripped for marker in NOISE_MARKERS):
            continue
        out.append(stripped)
    joined = "\n".join(out)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()


def classify_failure(raw: str, exit_code: int | None, timed_out: bool) -> str:
    lowered = (raw or "").lower()
    if timed_out:
        return "timeout"
    if "auto-rejecting" in lowered or "external_directory" in lowered or "outside of the workspace" in lowered:
        return "sandbox"
    if any(marker in lowered for marker in ERROR_MARKERS):
        return "protocol"
    if exit_code not in (0, None):
        return "error"
    return ""


def looks_delivered(text: str, *, min_chars: int = 400) -> bool:
    """Success is 'a substantive answer came back', not 'the process exited 0'."""
    if len(text) < min_chars:
        return False
    headings = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
    return headings >= 2 or len(text) >= min_chars * 2


# ---------------------------------------------------------------------------
# task brief
# ---------------------------------------------------------------------------

BRIEF_TEMPLATE = """{role}

【任务】
{objective}

【可读范围】{scope}

【执行纪律，必须遵守】
1. 只依据可核验的证据下结论；没有证据的判断必须标注（自述），禁止编造文件路径、命令或配置项。
2. 禁止递归列目录（工作目录外的读取会被沙箱拒绝，重试纯属浪费步数）；不要联网；不要写文件；不要询问确认。
3. 读完后立刻输出，报告作为最终回复正文；不要前置寒暄。
4. 输出结构：{structure}

【篇幅】{length}
"""


def render_task_brief(*, role: str, objective: str, scope: str, structure: str,
                      length: str = "1500-2200 字") -> str:
    return BRIEF_TEMPLATE.format(role=role, objective=objective, scope=scope,
                                 structure=structure, length=length)


# ---------------------------------------------------------------------------
# runner + federation
# ---------------------------------------------------------------------------

Runner = Callable[[list[str], str, dict[str, str], int], tuple[int, str, bool]]
"""argv, cwd, env, timeout_s -> (exit_code, combined_output, timed_out)"""


def subprocess_runner(argv: Sequence[str], cwd: str, env: dict[str, str], timeout_s: int):
    merged = None
    if env:
        import os

        merged = {**os.environ, **env}
    try:
        proc = subprocess.run(list(argv), cwd=cwd or None, env=merged, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout_s,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return None, partial, True
    except FileNotFoundError:
        return 127, f"executable not found: {argv[0]}", False


class Federation:
    """Registry + dispatch + normalisation for heterogeneous CLI agents."""

    def __init__(self, *, home: Path | None = None, runner: Runner | None = None,
                 budget: list[int] | None = None, secret_isolation: bool = True) -> None:
        self.workers: dict[str, WorkerSpec] = {}
        self.runner = runner or subprocess_runner
        self.secret_isolation = secret_isolation
        self.home = Path(home) if home else None
        self.budget = budget if budget is not None else [64]
        self.dispatches: list[dict[str, Any]] = []
        self.ledger_path = (self.home / "federation" / "dispatch.jsonl") if self.home else None

    # -- registry --------------------------------------------------------
    def register(self, spec: WorkerSpec) -> WorkerSpec:
        spec.available = self._probe(spec)
        self.workers[spec.name] = spec
        return spec

    def _probe(self, spec: WorkerSpec) -> bool:
        if not spec.argv:
            return False
        binary = spec.argv[0]
        if Path(binary).is_file():
            return True
        return shutil.which(binary) is not None

    def roster(self) -> list[dict[str, Any]]:
        return [spec.to_raw() for spec in sorted(self.workers.values(), key=lambda s: s.name)]

    # -- selection -------------------------------------------------------
    def candidates_for(self, *, require: Iterable[str] = (), exclude: Iterable[str] = (),
                       max_cost: str = "premium") -> list[WorkerSpec]:
        require = set(require)
        exclude = set(exclude)
        ceiling = COST_TIERS.index(max_cost)
        out = []
        for spec in self.workers.values():
            if not spec.available or spec.name in exclude:
                continue
            if require and not require.issubset(set(spec.capabilities)):
                continue
            if COST_TIERS.index(spec.cost) > ceiling:
                continue
            out.append(spec)
        out.sort(key=lambda s: (COST_TIERS.index(s.cost), s.name))
        return out

    def choose(self, *, require: Iterable[str] = (), exclude: Iterable[str] = (),
               max_cost: str = "premium") -> WorkerSpec:
        pool = self.candidates_for(require=require, exclude=exclude, max_cost=max_cost)
        if not pool:
            raise LookupError(f"no worker matches require={list(require)} max_cost={max_cost}")
        return pool[0]

    # -- dispatch --------------------------------------------------------
    def dispatch(self, name: str, brief: str, *, timeout_s: int | None = None) -> WorkerResult:
        from .secrets import SecretScope, redact
        brief = SecretScope().protect_text(brief)
        spec = self.workers.get(name)
        if spec is None:
            raise KeyError(f"unknown worker {name!r}")
        if self.secret_isolation and self.runner is subprocess_runner:
            return WorkerResult(worker=name, ok=False, failure="sandbox",
                                text="External CLI agents lack a mediated Secret boundary; use Forge subagents")
        if self.budget[0] <= 0:
            return WorkerResult(worker=name, ok=False, failure="refused")

        timeout = timeout_s or spec.timeout_s
        attempts = 0
        result = WorkerResult(worker=name, ok=False)
        while attempts < max(1, spec.max_attempts):
            attempts += 1
            self.budget[0] -= 1
            started = time.time()
            code, raw, timed_out = self.runner(spec.command(brief), spec.cwd, spec.env, timeout)
            raw = redact(raw)
            result = WorkerResult(
                worker=name,
                ok=False,
                raw=raw or "",
                exit_code=code,
                duration_s=time.time() - started,
                attempts=attempts,
            )
            result.failure = classify_failure(raw or "", code, timed_out)
            cleaned = clean_output(raw or "")
            result.text = cleaned
            result.cleaned_bytes = len(cleaned)
            if result.failure in ("sandbox", "protocol", "refused"):
                break  # retrying cannot fix these, and it costs money
            if looks_delivered(cleaned):
                result.ok = True
                result.failure = ""
                break
            result.failure = result.failure or "empty"
            if result.failure == "empty":
                break
        self._record(result, brief)
        return result

    def dispatch_best(self, brief: str, *, require: Iterable[str] = (),
                      exclude: Iterable[str] = (), max_cost: str = "premium",
                      timeout_s: int | None = None) -> WorkerResult:
        """Try workers in cost order until one delivers."""
        for spec in self.candidates_for(require=require, exclude=exclude, max_cost=max_cost):
            result = self.dispatch(spec.name, brief, timeout_s=timeout_s)
            if result.ok:
                return result
        return WorkerResult(worker="(none)", ok=False, failure="exhausted")

    def dispatch_many(self, briefs: dict[str, str]) -> dict[str, WorkerResult]:
        """One worker per brief, in parallel (each worker is an isolated process)."""
        import threading

        results: dict[str, WorkerResult] = {}
        lock = threading.Lock()

        def work(name: str, brief: str) -> None:
            try:
                outcome = self.dispatch(name, brief)
            except Exception as exc:  # a dead worker must not kill the fan-out
                outcome = WorkerResult(worker=name, ok=False, failure=f"exception: {exc}")
            with lock:
                results[name] = outcome

        threads = [threading.Thread(target=work, args=(n, b)) for n, b in briefs.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    # -- bookkeeping -----------------------------------------------------
    def _record(self, result: WorkerResult, brief: str) -> None:
        entry = {**result.to_raw(), "ts": time.time(), "brief_head": brief[:160],
                 "budget_left": self.budget[0]}
        from .secrets import redact
        entry = redact(entry)
        self.dispatches.append(entry)
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def report(self) -> dict[str, Any]:
        delivered = [d for d in self.dispatches if d["ok"]]
        by_failure: dict[str, int] = {}
        for dispatch in self.dispatches:
            if dispatch["failure"]:
                by_failure[dispatch["failure"]] = by_failure.get(dispatch["failure"], 0) + 1
        return {
            "workers": len(self.workers),
            "dispatches": len(self.dispatches),
            "delivered": len(delivered),
            "by_failure": by_failure,
            "chars": sum(d["chars"] for d in delivered),
            "budget_left": self.budget[0],
        }


# ---------------------------------------------------------------------------
# default fleet (declared via <home>/federation.json)
# ---------------------------------------------------------------------------

def default_fleet(home: Path, *, workspace: str = "", claude_settings: dict[str, str] | None = None) -> Federation:
    """Build the worker fleet from the user's local roster file.

    Worker CLIs are machine-specific, so the framework ships no hardcoded
    roster: declare workers in ``<home>/federation.json`` (see
    ``templates/federation.example.json`` for the shape). Without that file
    the fleet is simply empty.
    """
    fed = Federation(home=home)
    roster_file = Path(home) / "federation.json"
    if not roster_file.is_file():
        return fed
    for entry in json.loads(roster_file.read_text(encoding="utf-8")):
        data = dict(entry)
        data.setdefault("cwd", workspace)
        if "capabilities" in data:
            data["capabilities"] = tuple(data["capabilities"])
        fed.register(WorkerSpec(**data))
    return fed


__all__ = [
    "COST_TIERS",
    "Federation",
    "PERMISSION_TIERS",
    "WorkerResult",
    "WorkerSpec",
    "classify_failure",
    "clean_output",
    "default_fleet",
    "looks_delivered",
    "render_task_brief",
    "subprocess_runner",
]
