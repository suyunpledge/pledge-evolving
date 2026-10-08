"""Trusted helper for the onboarding panel: save one credential, atomically.

Why a separate script (not an RPC on the bridge):
    The wizard's step 2 can run *before* a working bridge exists — that is the
    whole point of onboarding. Routing the save through the bridge would make
    "paste your API key" depend on "the engine already connects", which is the
    chicken-and-egg the wizard exists to break.

Why the value comes over stdin:
    A secret in ``argv`` is visible to every process on the machine (task
    manager, ``ps``, WMI). stdin is not. The extension writes the value to the
    child's stdin and closes it; nothing else sees it.

The write mirrors ``forge.secrets._store_set_secret``: same path protections,
same 1 MiB cap, same atomic replace, same name grammar. It deliberately does
NOT touch the environment — the bridge re-reads the store at ``initialize``,
so an env write here would be a second source of truth that can disagree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import NoReturn

MAX_STORE = 1024 * 1024
NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def fail(message: str) -> NoReturn:
    sys.stdout.write(json.dumps({"ok": False, "error": message}) + "\n")
    raise SystemExit(1)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default="", help="Forge config dir; empty = ~/.forge")
    parser.add_argument("--name", required=True, help="credential name (secrets.json key)")
    parser.add_argument("--engine-root", default="", help="Forge source root for path guards")
    options = parser.parse_args()

    name = options.name.strip()
    if not NAME_RE.fullmatch(name):
        fail("Invalid credential name: only letters, digits, - and _ (max 128)")

    value = sys.stdin.read()
    if not value.strip():
        fail("Empty credential value")
    if len(value.encode("utf-8")) > 64 * 1024:
        fail("Credential value exceeds the 64 KiB limit")

    home = Path(options.home or (Path.home() / ".forge")).expanduser().resolve()

    # Reuse the engine's own path protections when we can import it; otherwise
    # fall back to an equivalent local check rather than skipping the guard.
    if options.engine_root:
        root = Path(options.engine_root).expanduser().resolve()
        if (root / "forge" / "secrets.py").is_file():
            sys.path.insert(0, str(root))
    try:
        from forge.secrets import protect_store_path
    except ImportError:
        def protect_store_path(path: Path) -> None:
            resolved = path.resolve()
            if resolved.is_symlink():
                raise PermissionError("Secret store path is a symlink")
            if resolved.is_dir():
                raise PermissionError("Secret store path is a directory")

    path = home / "secrets.json"
    try:
        protect_store_path(path)
    except PermissionError as exc:
        fail(str(exc))

    try:
        if path.is_file():
            with path.open("rb") as stream:
                raw = stream.read(MAX_STORE + 1)
            if len(raw) > MAX_STORE:
                fail("Credential store exceeds the 1 MiB size limit")
            existing = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
        else:
            existing = {}
    except (OSError, ValueError) as exc:
        fail(f"Cannot read the credential store: {type(exc).__name__}")

    if not isinstance(existing, dict):
        fail("Credential store is not a JSON object")
    for key, item in existing.items():
        if not isinstance(key, str) or not NAME_RE.fullmatch(key) or not isinstance(item, str):
            fail("Credential store contains an invalid entry")

    existing[name] = value
    home.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        fail(f"Cannot write the credential store: {type(exc).__name__}")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    sys.stdout.write(json.dumps({
        "ok": True, "name": name, "store": str(path),
        "configured": sorted(existing),
    }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())