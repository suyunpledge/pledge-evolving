"""Secret detection must not rewrite source-code expressions.

Regression for an agent review (2026-11-25): reading ``keystone/tools.py`` showed
``token = args.get(...)``-style lines as ``{{SECRET_REF:...}}``, and the write
guard then refused to edit any such file because the protected view differed
from the raw bytes.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from keystone.policy import Mode, Policy, Sandbox
from keystone.secrets import REF, SecretScope
from keystone.tools import ToolContext, build_builtin_registry

SOURCE = (
    "def load(args, cfg):\n"
    "    token = args.get('token')\n"
    "    password = cfg.password\n"
    "    secret = os.environ['APP_SECRET']\n"
    "    api_key = getpass()\n"
    "    token = token_value\n"
    "    return token\n"
)


class CodeAwareDetection(unittest.TestCase):
    def test_source_expressions_survive_and_literals_do_not(self):
        scope = SecretScope()
        self.assertEqual(scope.protect_text(SOURCE, code=True), SOURCE)
        literal = 'API_KEY = "sk-live-hardcoded-value-123456"\n'
        self.assertRegex(scope.protect_text(literal, code=True), REF)

    def test_generic_text_keeps_protecting_data_lines(self):
        scope = SecretScope()
        for line in ("PASSWORD=hunter2", "API_KEY=abc.def", "password: hunter2", "token=abc.def"):
            with self.subTest(line=line):
                self.assertRegex(scope.protect_text(line), REF)
        for line in ("token = args.get('token')", "secret = os.environ['X']", "api_key = getpass()",
                     "password = cfg.password"):
            with self.subTest(line=line):
                self.assertEqual(scope.protect_text(line), line)


class CodeAwareTools(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        (self.ws / "app.py").write_text(SOURCE, encoding="utf-8")
        self.reg = build_builtin_registry()
        policy = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=self.ws,
                        non_interactive=True)
        self.ctx = ToolContext(policy=policy, workspace=self.ws, extras={"registry": self.reg})

    def test_read_tools_show_source_verbatim(self):
        for name, args in (("read_file", {"path": "app.py"}),
                           ("read_range", {"path": "app.py", "start": 1, "end": 7}),
                           ("grep", {"pattern": "token", "path": "app.py"})):
            with self.subTest(tool=name):
                result = self.reg.invoke(name, args, self.ctx)
                self.assertTrue(result.ok, result.error)
                self.assertNotIn("SECRET_REF", result.content)

    def test_source_with_credential_named_variables_is_editable(self):
        result = self.reg.invoke("edit_file", {"path": "app.py", "old": "return token",
                                               "new": "return token or None"}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertIn("return token or None", (self.ws / "app.py").read_text(encoding="utf-8"))

    def test_source_with_a_literal_secret_stays_protected(self):
        (self.ws / "keys.py").write_text('API_KEY = "sk-live-hardcoded-value-123456"\n', encoding="utf-8")
        read = self.reg.invoke("read_file", {"path": "keys.py"}, self.ctx)
        self.assertNotIn("sk-live", read.content)
        edit = self.reg.invoke("edit_file", {"path": "keys.py", "old": "API_KEY", "new": "KEY"}, self.ctx)
        self.assertFalse(edit.ok)


if __name__ == "__main__":
    unittest.main()
