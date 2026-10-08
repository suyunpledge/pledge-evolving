"""Background console and interrupted tool-call regressions; no external API."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from forge.checkpoint import CheckpointStore
from forge.federation import subprocess_runner
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry, ToolContext
from forge.tool_adapter import parse_tool_call_tags, parse_text_protocol_calls


class RuntimeIntegrityTests(unittest.TestCase):
    def test_interrupted_wrappers_never_turn_into_executable_calls(self):
        for parse in (parse_tool_call_tags, parse_text_protocol_calls):
            for tag in ("tools", "tool_call"):
                interrupted = f'<{tag}>{{"name":"write_file","arguments":{{"path":"x","content":"x"}}}}'
                self.assertEqual(parse(interrupted), [])
                self.assertEqual(len(parse(interrupted + f"</{tag}>")), 1)
                complete = '{"name":"read_file","arguments":{"path":"real"}}'
                calls = parse(complete + "\n" + interrupted)
                self.assertEqual([call["name"] for call in calls], ["read_file"])

    def test_tags_inside_json_strings_remain_literal_arguments(self):
        call = '{"name":"write_file","arguments":{"content":"<tools>literal</tools>"}}'
        for parse in (parse_tool_call_tags, parse_text_protocol_calls):
            self.assertEqual(parse(call)[0]["arguments"]["content"], "<tools>literal</tools>")

    def test_checkpoint_operations_all_run_without_a_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work = base / "work"
            work.mkdir()
            (work / "a.txt").write_text("real checkpoint data", encoding="utf-8")
            store = CheckpointStore(base / "home", work)
            if not store.enabled:
                self.skipTest("Git unavailable")
            original = subprocess.run
            with patch("forge.checkpoint.subprocess.run", wraps=original) as run:
                point = store.snapshot("runtime regression")
            self.assertIsNotNone(point)
            self.assertGreaterEqual(run.call_count, 5)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_real_shell_and_federation_children_have_no_console(self):
        script = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"
        argv = [sys.executable, "-c", script]
        code, text, timed_out = subprocess_runner(argv, "", {}, 5)
        self.assertEqual((code, text.strip(), timed_out), (0, "0", False))
        with tempfile.TemporaryDirectory() as tmp:
            # isolated=False 是宿主自己的控制台回归测试在跑真实 shell ——
            # 这是一个显式的信任声明（ToolContext 默认已翻转为受保护）。
            context = ToolContext(policy=Policy(mode=Mode.BYPASS, workspace=Path(tmp), non_interactive=True), workspace=Path(tmp), isolated=False)
            result = build_builtin_registry().invoke("shell_exec", {"command": subprocess.list2cmdline(argv)}, context)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.content.strip(), "0")


if __name__ == "__main__":
    unittest.main()
