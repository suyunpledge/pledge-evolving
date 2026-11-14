"""Read existing Forge history; keep Qt writes in per-session atomic files.

The existing Tk application may still be open. It rewrites sessions.json as a
whole, so the Qt frontend must not race it or lose new conversations on its exit.
"""
import json
import os
from pathlib import Path
import tempfile

from forge.secrets import redact


class SessionStore:
    def __init__(self, home):
        self.home = Path(home)
        self.folder = self.home / "gui" / "qt"

    @staticmethod
    def read(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def atomic(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".qt-", delete=False) as out:
                temporary = Path(out.name)
                json.dump(redact(data), out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def sessions(self):
        legacy = self.read(self.home / "gui" / "sessions.json", {})
        if not legacy:
            legacy = self.read(self.home / "gui-sessions.json", {})
        rows = legacy.get("sessions", []) if isinstance(legacy, dict) else legacy
        merged = {}
        for row in (rows if isinstance(rows, list) else []):
            if isinstance(row, dict) and isinstance(row.get("messages"), list) and row.get("id"):
                merged[str(row["id"])] = row
        for path in (self.folder / "sessions").glob("*.json"):
            row = self.read(path, {})
            if isinstance(row, dict) and row.get("id") and isinstance(row.get("messages"), list):
                merged[str(row["id"])] = row
        return sorted(merged.values(), key=lambda s: float(s.get("updated", 0)), reverse=True)

    @staticmethod
    def safe_id(sid):
        # Never turn a legacy session's arbitrary identifier into a path.
        import hashlib
        return hashlib.sha256(str(sid).encode()).hexdigest()

    def save_session(self, entry):
        self.atomic(self.folder / "sessions" / (self.safe_id(entry["id"]) + ".json"), entry)

    def draft(self, sid):
        data = self.read(self.folder / "drafts" / (self.safe_id(sid) + ".json"), {})
        return str(data.get("text", "")) if isinstance(data, dict) else ""

    def save_draft(self, sid, text):
        self.atomic(self.folder / "drafts" / (self.safe_id(sid) + ".json"), {"text": text})

    def preferences(self):
        data = self.read(self.folder / "preferences.json", {})
        return data if isinstance(data, dict) else {}

    def save_preferences(self, data):
        self.atomic(self.folder / "preferences.json", data)
