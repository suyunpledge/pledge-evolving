import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from revon import compat


class RenameCompatibilityTests(unittest.TestCase):
    def test_existing_profiles_are_selected_in_place(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'home', return_value=Path(tmp)), patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            for name in ('.forge', '.keystone', '.revon'):
                profile = root / name
                profile.mkdir()
                self.assertEqual(compat.default_home(), profile)
                self.assertIn(profile, compat.home_roots())
            for accessor, old, current in (
                (compat.user_layer, 'keystone.patch.json', 'revon.patch.json'),
                (compat.desktop_config, 'keystone-desktop.json', 'revon-desktop.json'),
                (compat.plugin_manifest, 'keystone-plugin.json', 'revon-plugin.json'),
                (compat.capability_manifest, 'keystone.capability.json', 'revon.capability.json'),
            ):
                (profile / old).touch()
                self.assertEqual(accessor(profile), profile / old)
                (profile / current).touch()
                self.assertEqual(accessor(profile), profile / current)

    def test_explicit_environment_and_old_config_expressions(self):
        from revon.config import _boot_ctx
        env = {'KEYSTONE_EXAMPLE': 'former', 'REVON_EXAMPLE': 'current'}
        compat.mirror_env(env)
        self.assertEqual(env, {'KEYSTONE_EXAMPLE': 'former', 'REVON_EXAMPLE': 'current', 'FORGE_EXAMPLE': 'current'})
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_boot_ctx(None)['env']['KEYSTONE_EXAMPLE'], 'former')

    def test_legacy_imports_share_runtime_and_entry_point(self):
        from keystone.policy import Policy
        from revon.policy import Policy as Current
        self.assertIs(Policy, Current)
        root = Path(__file__).resolve().parents[1]
        for module in ('revon.cli', 'keystone.cli'):
            proc = subprocess.run([sys.executable, '-m', module, '--help'], cwd=root,
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('revon', proc.stdout)
