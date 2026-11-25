"""Compatibility imports for applications that still use the former package name."""
import importlib
import importlib.abc
import importlib.util
import sys

from revon import *  # noqa: F403
from revon import __all__, __version__


class _LegacyLoader(importlib.abc.Loader):
    def __init__(self, canonical):
        self.canonical = canonical

    def exec_module(self, module):
        # Both spellings must share classes, registries, locks and exception types.
        sys.modules[module.__name__] = importlib.import_module(self.canonical)

    def get_code(self, fullname):
        # runpy uses get_code for python -m keystone.cli and legacy workers.
        spec = importlib.util.find_spec(self.canonical)
        return spec.loader.get_code(self.canonical)


class _LegacyModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith("keystone."):
            return None
        canonical = "revon" + fullname[len("keystone"):]
        spec = importlib.util.find_spec(canonical)
        if spec is None:
            return None
        return importlib.util.spec_from_file_location(
            fullname, spec.origin, loader=_LegacyLoader(canonical),
            submodule_search_locations=[] if spec.submodule_search_locations is not None else None)


sys.meta_path.insert(0, _LegacyModules())
