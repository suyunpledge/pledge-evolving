"""Tool-result feedback that saves the model a turn.

Three cheap, host-side enrichments applied after a tool ran (never instead of it):

* ``write_checks``  — after a successful write, parse the touched .py/.json/.toml
  files (and scan any text file for leftover merge-conflict markers) and report
  the first problem with its line, so a broken edit is caught in the same turn
  instead of after a separate read/run step.
* ``path_suggestions`` — when a call failed because its path does not exist,
  offer the closest workspace paths ("did you mean"), so a guessed path costs
  one failed call instead of a find/list detour.
* ``ToolBlocker`` — per-run circuit breaker for tools the host refuses as a
  whole (protected-agent native execution, missing capability) or keeps
  denying; later calls are answered at once with one explanation and
  alternatives instead of another round-trip through policy.

All helpers are best effort: they never raise and never flip a result's ``ok``.
"""
from __future__ import annotations

import ast
import difflib
import json
from pathlib import Path
from typing import Any, Iterable

CHECK_LIMIT = 512 * 1024
WALK_LIMIT = 20000
_MISSING_HINTS = ("not a file", "no such file", "not found", "does not exist", "cannot find")
_CONFLICT = ("<<<<<<< ", ">>>>>>> ")


def _targets(args: dict[str, Any] | None) -> list[str]:
    args = args or {}
    rows = [args.get("path")]
    blocks = args.get("patches") or args.get("edits") or []
    if isinstance(blocks, list):
        rows += [b.get("path") for b in blocks if isinstance(b, dict)]
    seen, out = set(), []
    for row in rows:
        if isinstance(row, str) and row and row not in seen:
            seen.add(row)
            out.append(row)
    return out


def _shown(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


def _check_text(name: str, text: str) -> dict[str, Any] | None:
    """Return a problem dict for the first issue found, or None when clean/unchecked."""
    suffix = Path(name).suffix.lower()
    try:
        if suffix in {".py", ".pyw"}:
            ast.parse(text, filename=name)
        elif suffix == ".json":
            json.loads(text)
        elif suffix == ".toml":
            import tomllib
            tomllib.loads(text)
    except SyntaxError as exc:
        return {"kind": "SyntaxError", "line": exc.lineno or 0, "column": exc.offset or 0, "message": exc.msg}
    except json.JSONDecodeError as exc:
        return {"kind": "JSONDecodeError", "line": exc.lineno, "column": exc.colno, "message": exc.msg}
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        return {"kind": type(exc).__name__, "line": 0, "column": 0, "message": str(exc)[:200]}
    lines = text.splitlines()
    if any(l.startswith(_CONFLICT[1]) for l in lines):
        for number, line in enumerate(lines, 1):
            if line.startswith(_CONFLICT[0]):
                return {"kind": "ConflictMarker", "line": number, "column": 1,
                        "message": "unresolved merge-conflict marker"}
    return None


def checked_kind(name: str) -> bool:
    return Path(name).suffix.lower() in {".py", ".pyw", ".json", ".toml"}


def write_checks(args: dict[str, Any] | None, workspace: Path, resolve) -> tuple[str, list[dict[str, Any]]]:
    """Check files a successful write touched. Returns (note for the model, meta rows)."""
    rows: list[dict[str, Any]] = []
    for raw in _targets(args)[:20]:
        try:
            path = resolve(raw)
            if not path.is_file() or path.stat().st_size > CHECK_LIMIT:
                continue
            text = path.read_bytes().decode("utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        shown = _shown(path, workspace)
        problem = _check_text(shown, text)
        if problem is None and not checked_kind(shown):
            continue
        row = {"path": shown, "ok": problem is None}
        if problem:
            row.update(problem)
            lines = text.splitlines()
            if 0 < problem["line"] <= len(lines):
                row["source"] = lines[problem["line"] - 1][:160]
        rows.append(row)
    if not rows:
        return "", rows
    good = [r["path"] for r in rows if r["ok"]]
    parts = []
    if good:
        parts.append("syntax OK: " + ", ".join(good[:6]) + (" …" if len(good) > 6 else ""))
    for r in (r for r in rows if not r["ok"]):
        where = f"{r['path']}:{r['line']}:{r['column']}" if r["line"] else r["path"]
        parts.append(f"PROBLEM {where} {r['kind']}: {r['message']}"
                     + (f"\n    {r['source']}" if r.get("source") else "")
                     + "\n    The file was written as given; fix it before relying on it.")
    return "\n[post-write check] " + "\n[post-write check] ".join(parts), rows


def looks_missing(error: str) -> bool:
    lowered = (error or "").lower()
    return any(hint in lowered for hint in _MISSING_HINTS)


def path_suggestions(raw: str, workspace: Path, files: Iterable[Path], limit: int = 3,
                     public=None) -> list[str]:
    """Closest existing workspace paths for a missing ``raw`` path."""
    wanted = raw.replace("\\", "/").strip().lstrip("./")
    if not wanted:
        return []
    base = wanted.rsplit("/", 1)[-1].lower()
    candidates: list[str] = []
    for count, path in enumerate(files):
        if count >= WALK_LIMIT:
            break
        if public is not None:
            try:
                public(path)
            except Exception:
                continue
        candidates.append(_shown(path, workspace))
    if not candidates:
        return []
    exact = [c for c in candidates if c.rsplit("/", 1)[-1].lower() == base]
    exact.sort(key=lambda c: (-difflib.SequenceMatcher(None, c, wanted).ratio(), c))
    out = exact[:limit]
    if len(out) < limit:
        names: dict[str, list[str]] = {}
        for c in candidates:
            names.setdefault(c.rsplit("/", 1)[-1].lower(), []).append(c)
        for name in difflib.get_close_matches(base, list(names), n=limit, cutoff=0.6):
            out += [c for c in names[name] if c not in out]
        out += [c for c in difflib.get_close_matches(wanted, candidates, n=limit, cutoff=0.6) if c not in out]
    return out[:limit]


# -- per-run circuit breaker ------------------------------------------------
# Refusals that do not depend on the arguments: the tool cannot work in this run at all.
TOOL_WIDE = {"secret.isolation", "missing_capability"}
REPEATED_DENIALS = 3
ALTERNATIVES = {
    "shell_exec": ("git_status", "git_add", "git_commit", "git_log", "file_stats", "file_export",
                   "grep", "find_files", "read_file", "calculator"),
    "notebook_edit": ("edit_file", "apply_patch", "write_file"),
}


class ToolBlocker:
    def __init__(self):
        self.reset()

    def reset(self):
        self.blocked: dict[str, str] = {}
        self._denials: dict[str, int] = {}

    def refusal(self, name: str):
        return self.blocked.get(name)

    def observe(self, name: str, result, available: Iterable[str] = ()) -> str:
        """Record a result; return an explanation when this call just blocked the tool."""
        meta = result.meta if isinstance(result.meta, dict) else {}
        if result.ok:
            self._denials.pop(name, None)
            return ""
        if name in self.blocked:
            return ""
        wide = meta.get("boundary") in TOOL_WIDE or meta.get("error_code") in TOOL_WIDE
        if meta.get("authorization") == "deny" or wide:
            self._denials[name] = self._denials.get(name, 0) + 1
        if not wide and self._denials.get(name, 0) < REPEATED_DENIALS:
            return ""
        why = (result.error or "refused by the host").split("\n", 1)[0][:200]
        available = set(available)
        options = [t for t in ALTERNATIVES.get(name, ()) if t in available]
        note = (f"Tool '{name}' is unavailable for the rest of this run ({why}). "
                "Further calls will be refused without running.")
        if options:
            note += " Use instead: " + ", ".join(options) + "."
        else:
            note += " Choose a different approach or explain the limitation to the user."
        self.blocked[name] = note
        return note
