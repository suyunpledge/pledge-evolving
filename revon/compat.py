"""Resolve current and legacy installation paths and environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

NEW, OLD = "REVON_", "FORGE_"
HOME_NAME, LEGACY_HOME_NAME = ".revon", ".forge"
USER_LAYER, LEGACY_USER_LAYER = "revon.patch.json", "forge.patch.json"
DESKTOP_CONFIG, LEGACY_DESKTOP_CONFIG = "revon-desktop.json", "forge-desktop.json"
PLUGIN_MANIFEST, LEGACY_PLUGIN_MANIFEST = "revon-plugin.json", "forge-plugin.json"
CAPABILITY_MANIFEST, LEGACY_CAPABILITY_MANIFEST = "revon.capability.json", "forge.capability.json"


def mirror_env(env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Supply missing legacy and current environment aliases without replacing explicit values."""
    env = os.environ if env is None else env
    prefixes = (NEW, "KEYSTONE_", OLD)
    suffixes = {key[len(prefix):] for key in env for prefix in prefixes if key.startswith(prefix)}
    for suffix in suffixes:
        value = next(env[prefix + suffix] for prefix in prefixes if prefix + suffix in env)
        for prefix in prefixes:
            env.setdefault(prefix + suffix, value)
    return env


def prefer(new: Path, *older: Path) -> Path:
    candidates = (Path(new), *map(Path, older))
    return next((path for path in candidates if path.exists()), candidates[0])


def default_home() -> Path:
    for key in ("REVON_HOME", "KEYSTONE_HOME", "FORGE_HOME"):
        if os.environ.get(key):
            return Path(os.environ[key]).expanduser()
    return prefer(*workspace_dirs(Path.home()))


def home_roots() -> set[Path]:
    return {*workspace_dirs(Path.home()), default_home()}


def _existing(directory, current, legacy):
    directory = Path(directory)
    return prefer(directory / current, directory / current.replace("revon", "keystone", 1), directory / legacy)


def user_layer(home) -> Path:
    return _existing(home, USER_LAYER, LEGACY_USER_LAYER)


def desktop_config(home) -> Path:
    return _existing(home, DESKTOP_CONFIG, LEGACY_DESKTOP_CONFIG)


def plugin_manifest(directory) -> Path:
    return _existing(directory, PLUGIN_MANIFEST, LEGACY_PLUGIN_MANIFEST)


def capability_manifest(directory) -> Path:
    return _existing(directory, CAPABILITY_MANIFEST, LEGACY_CAPABILITY_MANIFEST)


def workspace_dirs(workspace) -> list[Path]:
    return [Path(workspace) / name for name in (HOME_NAME, ".keystone", LEGACY_HOME_NAME)]
