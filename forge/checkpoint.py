"""Shadow-git checkpoints.

Hermes snapshots the workspace into a shadow repository before every write so
``/rollback`` is always available. Same contract here, but the shadow repo
lives under the product home (never inside the user's own tree) and is addressed
with explicit ``--git-dir`` / ``--work-tree`` so it can never be confused with
the user's real repository.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Checkpoint:
    commit: str
    label: str
    ts: float = field(default_factory=time.time)

    def to_raw(self) -> dict:
        return {"commit": self.commit, "label": self.label, "ts": self.ts}


class CheckpointStore:
    def __init__(self, home: Path, workspace: Path) -> None:
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.git_dir = self.home / "checkpoints" / "shadow.git"
        self.enabled = shutil.which("git") is not None
        self._history: list[Checkpoint] = []

    # -- plumbing --------------------------------------------------------
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.workspace}", *args],
            capture_output=True,
            text=True,
            check=check,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _sync_env(self) -> dict[str, str]:
        import os

        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = env.get("GIT_AUTHOR_NAME", "forge")
        env["GIT_AUTHOR_EMAIL"] = env.get("GIT_AUTHOR_EMAIL", "forge@localhost")
        env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
        return env

    def init(self) -> bool:
        if not self.enabled:
            return False
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        if not self.git_dir.exists():
            subprocess.run(["git", "init", "--bare", "--quiet", str(self.git_dir)],
                           capture_output=True, text=True, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._git("config", "core.worktree", str(self.workspace), check=False)
        return True

    def snapshot(self, label: str) -> Checkpoint | None:
        """Commit the current workspace state into the shadow repo."""
        if not self.enabled or not self.init():
            return None
        env = self._sync_env()
        subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.workspace}", "add", "-A"],
            capture_output=True, text=True, env=env, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        commit = subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.workspace}",
             "commit", "--allow-empty", "-m", f"{label} @ {time.strftime('%H:%M:%S')}"],
            capture_output=True, text=True, env=env, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rev = subprocess.run(
            ["git", f"--git-dir={self.git_dir}", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sha = rev.stdout.strip()
        if not sha:
            return None
        point = Checkpoint(commit=sha, label=label)
        if not self._history or self._history[-1].commit != sha:
            self._history.append(point)
        return point

    def history(self, limit: int = 20) -> list[Checkpoint]:
        if not self._history and self.enabled and self.git_dir.exists():
            log = self._git("log", "--pretty=%H\t%s", f"-{limit}", check=False)
            for line in log.stdout.splitlines():
                sha, _, subject = line.partition("\t")
                label = subject.split(" @ ")[0]
                self._history.append(Checkpoint(commit=sha.strip(), label=label))
        return self._history[-limit:]

    def rollback(self, commit: str) -> bool:
        """Restore every tracked file to the snapshot state."""
        if not self.enabled or not self.init():
            return False
        result = self._git("checkout", commit, "--", ".", check=False)
        return result.returncode == 0

    def rollback_last(self) -> bool:
        history = self.history()
        if len(history) < 2:
            return False
        return self.rollback(history[-2].commit)

    def prune(self, keep: int = 30) -> dict[str, int]:
        """Trim the shadow history without touching the workspace."""
        history = self.history(limit=10_000)
        if len(history) <= keep:
            return {"pruned": 0, "kept": len(history)}
        self._git("gc", "--prune=now", "--quiet", check=False)
        return {"pruned": 0, "kept": len(history), "note": "gc run; history retained for safety"}


__all__ = ["Checkpoint", "CheckpointStore"]
