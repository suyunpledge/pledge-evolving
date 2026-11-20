"""Compatibility with installs made before the Forge -> Pledge Keystone rename.

Nothing is moved or rewritten on disk. Old names are only *read* when the new
ones do not exist yet, so an existing ``~/.forge`` home, ``forge.patch.json``,
``forge-desktop.json``, installed ``forge-plugin.json`` plugins and ``FORGE_*``
environment variables keep working unchanged. New installs use the new names.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

NEW, OLD = "KEYSTONE_", "FORGE_"
HOME_NAME, LEGACY_HOME_NAME = ".keystone", ".forge"
USER_LAYER, LEGACY_USER_LAYER = "keystone.patch.json", "forge.patch.json"
DESKTOP_CONFIG, LEGACY_DESKTOP_CONFIG = "keystone-desktop.json", "forge-desktop.json"
PLUGIN_MANIFEST, LEGACY_PLUGIN_MANIFEST = "keystone-plugin.json", "forge-plugin.json"
CAPABILITY_MANIFEST, LEGACY_CAPABILITY_MANIFEST = "keystone.capability.json", "forge.capability.json"


def mirror_env(env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Make ``FORGE_X`` and ``KEYSTONE_X`` interchangeable; an explicit value always wins."""
    env = os.environ if env is None else env
    for key, value in list(env.items()):
        if key.startswith(OLD):
            env.setdefault(NEW + key[len(OLD):], value)
        elif key.startswith(NEW):
            env.setdefault(OLD + key[len(NEW):], value)
    return env


def prefer(new: Path, old: Path) -> Path:
    """``new`` unless only the legacy ``old`` exists."""
    new, old = Path(new), Path(old)
    return old if not new.exists() and old.exists() else new


def default_home() -> Path:
    """``$KEYSTONE_HOME`` (or legacy ``$FORGE_HOME``), else ``~/.keystone``, else an existing ``~/.forge``."""
    explicit = os.environ.get(NEW + "HOME") or os.environ.get(OLD + "HOME")
    if explicit:
        return Path(explicit).expanduser()
    return prefer(Path.home() / HOME_NAME, Path.home() / LEGACY_HOME_NAME)


def home_roots() -> set[Path]:
    """Every location a home may live in — for protection checks, never for choosing one."""
    roots = {Path.home() / HOME_NAME, Path.home() / LEGACY_HOME_NAME, default_home()}
    return {Path(r) for r in roots}


def user_layer(home) -> Path:
    return prefer(Path(home) / USER_LAYER, Path(home) / LEGACY_USER_LAYER)


def desktop_config(home) -> Path:
    return prefer(Path(home) / DESKTOP_CONFIG, Path(home) / LEGACY_DESKTOP_CONFIG)


def plugin_manifest(directory) -> Path:
    return prefer(Path(directory) / PLUGIN_MANIFEST, Path(directory) / LEGACY_PLUGIN_MANIFEST)


def capability_manifest(directory) -> Path:
    return prefer(Path(directory) / CAPABILITY_MANIFEST, Path(directory) / LEGACY_CAPABILITY_MANIFEST)


def workspace_dirs(workspace) -> list[Path]:
    """Project-level control directories: ``.keystone`` first, then a legacy ``.forge``."""
    return [Path(workspace) / HOME_NAME, Path(workspace) / LEGACY_HOME_NAME]
