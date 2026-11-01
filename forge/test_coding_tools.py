"""Coding 工具集：grep（不因 JSONC 崩溃 / 新参数 / 预筛不漏 / 不泄露密钥）、find_files、review_changes。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from forge import toolwire
from forge.policy import Mode, Policy, Sandbox
from forge.tools import BUILTIN_TOOL_NAMES, ToolContext, build_builtin_registry

SECRET = "sk-abcdef0123456789abcdef0123456789"


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        files = {
            "src/app.py": "class AppTests:\n    pass\n\ndef greet(name):\n    # TODO: validate\n    return name\n",
            "src/util.py": "def helper():\n    return 'Greet'\n",
            "tests/test_app.py": "from src.app import greet\n\ndef test_greet():\n    assert greet('x') == 'x'\n",
            "tsconfig.json": "{\n  // comments make this invalid strict JSON\n  \"compilerOptions\": {\"greet\": true}\n}\n",
            "config.py": f"api_key = \"{SECRET}\"\n",
            "node_modules/pkg/index.js": "function greet() {}\n",
            ".venv/lib/greet.py": "def greet(): pass\n",
        }
        for name, text in files.items():
            path = self.ws / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.ws / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0greet")
        self.reg = build_builtin_registry()

    def ctx(self, reg=None):
        reg = reg or self.reg
        policy = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=self.ws,
                        non_interactive=True)
        return ToolContext(policy=policy, workspace=self.ws, extras={"registry": reg})

    def call(self, name, reg=None, **args):
        return (reg or self.reg).invoke(name, args, self.ctx(reg))


class GrepTests(_Workspace):
    def test_jsonc_file_is_skipped_with_a_note_instead_of_failing_the_search(self):
        result = self.call("grep", pattern="greet")
        self.assertTrue(result.ok, result.error)
        self.assertIn("src/app.py:4: def greet(name):", result.content)
        self.assertIn("skipped 1 file(s)", result.content)
        self.assertIn("tsconfig.json", result.content)
        self.assertNotIn("node_modules", result.content)
        self.assertNotIn(".venv", result.content)
        self.assertNotIn("logo.png", result.content)

    def test_glob_ignore_case_and_context(self):
        only_py = self.call("grep", pattern="greet", glob="test_*.py").content
        self.assertTrue(all(line.startswith("tests/test_app.py") for line in only_py.splitlines()))
        self.assertIn("src/util.py:2:", self.call("grep", pattern="greet", ignore_case=True).content)
        self.assertNotIn("src/util.py", self.call("grep", pattern="greet").content)
        around = self.call("grep", pattern="TODO", context=1).content.splitlines()
        self.assertEqual(around, ["src/app.py-4- def greet(name):", "src/app.py:5: # TODO: validate",
                                  "src/app.py-6- return name"])

    def test_line_anchors_survive_the_whole_file_prefilter(self):
        result = self.call("grep", pattern="^class \\w+Tests")
        self.assertIn("src/app.py:1: class AppTests:", result.content)
        self.assertIn("src/app.py:3:", self.call("grep", pattern="^$").content)

    def test_secrets_never_appear_in_results(self):
        result = self.call("grep", pattern="sk-abcdef")
        self.assertNotIn(SECRET, result.content)
        self.assertEqual(result.meta["matches"], 0)
        masked = self.call("grep", pattern="api_key").content
        self.assertIn("config.py:1:", masked)
        self.assertNotIn(SECRET, masked)

    def test_invalid_regex_and_match_limit_are_explained(self):
        bad = self.call("grep", pattern="[unclosed")
        self.assertFalse(bad.ok)
        self.assertIn("invalid regular expression", bad.error)
        (self.ws / "many.txt").write_text("hit\n" * 150, encoding="utf-8")
        many = self.call("grep", pattern="^hit$")
        self.assertEqual(many.meta["matches"], 100)
        self.assertIn("stopped at 100 matches", many.content)


class FindFilesTests(_Workspace):
    def test_name_and_path_patterns_skip_dependency_folders(self):
        self.assertEqual(self.call("find_files", pattern="test_*.py").content, "tests/test_app.py")
        by_path = self.call("find_files", pattern="src/**/*.py").content.splitlines()
        self.assertEqual(by_path, ["src/app.py", "src/util.py"])
        everything = self.call("find_files", pattern="*.py").content
        self.assertNotIn("node_modules", everything)
        self.assertNotIn(".venv", everything)
        self.assertEqual(self.call("find_files", pattern="*.rs").content, "(no files matched)")


class ReviewChangesTests(_Workspace):
    def test_cumulative_session_diff_covers_edits_new_files_deletions_and_subagents(self):
        self.assertEqual(self.call("review_changes").content, "(no changes in this session)")
        self.assertTrue(self.call("edit_file", path="src/util.py", old="'Greet'", new="'Hello'").ok)
        self.assertTrue(self.call("write_file", path="NOTES.md", content="todo\n").ok)
        self.assertTrue(self.call("delete_file", path="tests/test_app.py").ok)
        child = self.reg.clone()
        self.assertTrue(self.call("edit_file", reg=child, path="src/util.py", old="'Hello'", new="'Hi'").ok)
        review = self.call("review_changes")
        self.assertEqual(review.meta["files"], 3)
        self.assertIn("-    return 'Greet'\n+    return 'Hi'", review.content)
        self.assertIn("--- /dev/null\n+++ b/NOTES.md", review.content)
        self.assertIn("tests/test_app.py: deleted", review.content)
        one = self.call("review_changes", path="src/util.py")
        self.assertEqual(one.meta["files"], 1)
        self.assertNotIn("NOTES.md", one.content)

    def test_reverting_a_change_by_hand_clears_it(self):
        self.call("edit_file", path="src/util.py", old="'Greet'", new="'X'")
        self.call("edit_file", path="src/util.py", old="'X'", new="'Greet'")
        self.assertEqual(self.call("review_changes").content, "(no changes in this session)")


class DeclarationTests(unittest.TestCase):
    def test_new_tools_are_builtin_and_every_core_parameter_is_described(self):
        self.assertTrue({"find_files", "review_changes"} <= BUILTIN_TOOL_NAMES)
        declared = {d["function"]["name"]: d["function"]
                    for d in toolwire.tool_declarations(build_builtin_registry().visible(), "openai")}
        for name in ("read_file", "read_range", "file_outline", "list_dir", "grep", "find_files",
                     "review_changes", "write_file", "edit_file", "apply_patch"):
            props = declared[name]["parameters"]["properties"]
            for arg, shape in props.items():
                self.assertGreater(len(shape.get("description", "")), len(arg) + 5, f"{name}.{arg}")
        items = declared["apply_patch"]["parameters"]["properties"]["patches"]["items"]
        self.assertEqual(items["required"], ["path", "old", "new"])
        json.dumps(declared)


if __name__ == "__main__":
    unittest.main()
