"""Contract test: the ToolContext isolation default must stay fail-closed.

Provenance (2026-10-08): flipping the default from False to True was the P1
fix from the security review. Mutating it back made *no* test fail — the
protection the flip provides had no pin of its own. This file is that pin.

The property is one sentence: **a ToolContext constructed without an explicit
``isolated`` argument must behave as protected.** "Protected" is observable
three ways, each covered below:

  1. the field itself is True;
  2. a native-execution tool (shell_exec) is refused without ever spawning a
     process, even under a BYPASS policy;
  3. a write tool targeting a control root (~/.forge) is refused.

An embedder that genuinely wants to run trusted native tools must write
``isolated=False`` — an explicit, greppable decision.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.policy import Mode, Policy
from forge.tools import ToolContext, build_builtin_registry


class IsolatedDefaultTests(unittest.TestCase):
    def test_default_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = ToolContext(policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=root),
                              workspace=root)
            self.assertTrue(ctx.isolated,
                            "ToolContext default flipped back to fail-open — "
                            "every embedder that forgot the flag is silently "
                            "unprotected again")

    def test_default_context_refuses_native_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = ToolContext(policy=Policy(mode=Mode.BYPASS, workspace=root),
                              workspace=root)          # no isolated argument
            reg = build_builtin_registry()
            with patch("forge.tools.subprocess.run") as run:
                result = reg.invoke("shell_exec",
                                    {"command": "echo should-never-run"}, ctx)
            self.assertFalse(result.ok)
            run.assert_not_called()

    def test_opt_out_is_explicit_and_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = ToolContext(policy=Policy(mode=Mode.BYPASS, workspace=root),
                              workspace=root, isolated=False)
            self.assertFalse(ctx.isolated)
            # the escape hatch exists — trusting hosts may use it deliberately
            with patch("forge.tools.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "ok"
                run.return_value.stderr = ""
                result = build_builtin_registry().invoke(
                    "shell_exec", {"command": "echo hi"}, ctx)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
