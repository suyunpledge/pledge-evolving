"""Coding 工具集：grep（不因 JSONC 崩溃 / 新参数 / 预筛不漏 / 不泄露密钥）、find_files、review_changes。"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from revon import toolwire
from revon.policy import Mode, Policy, Sandbox
from revon.tools import BUILTIN_TOOL_NAMES, ToolContext, build_builtin_registry

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
    def test_pages_count_matching_lines_and_stop_at_exact_boundary(self):
        path = self.ws / "中文.txt"
        path.write_text("".join(f"item {n}\n" for n in range(1, 201)), encoding="utf-8")
        args = dict(root=path.name, pattern=r"^item ", context=1)
        first = self.call("grep", **args)
        self.assertEqual(first.meta["next_offset"], 100)
        second = self.call("grep", offset=first.meta["next_offset"], **args)
        self.assertFalse(second.meta["truncated"])
        self.assertNotIn("next_offset", second.meta)
        self.assertEqual(second.meta["matches"], 100)
        self.assertIn("中文.txt:101: item 101", second.content)
        self.assertNotIn("中文.txt:100:", second.content)
        self.assertEqual(self.call("grep", offset=200, **args).meta["matches"], 0)
        self.assertFalse(self.call("grep", offset=-1, **args).ok)

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_git_ignored_files_hidden_except_tracked_and_reincluded(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.ws, check=True, capture_output=True)
        git("init", "-q")
        (self.ws / ".gitignore").write_text(".openclaw/\nbuild/\n", encoding="utf8")
        files = ("build/lib/copy.txt", "build/tracked.txt", ".openclaw/tmp/noise.txt",
                 "中文/keep.txt", "中文/skip.txt")
        for name in files:
            path = self.ws / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unique_marker\n", encoding="utf8")
        (self.ws / "中文/.gitignore").write_text("*.txt\n!keep.txt\n", encoding="utf8")
        git("add", "-f", "build/tracked.txt")
        for tool, args in (("grep", {"pattern": "unique_marker"}), ("find_files", {"pattern": "*.txt"})):
            result = self.call(tool, **args)
            for name in files:
                self.assertEqual(name in result.content, name in ("build/tracked.txt", "中文/keep.txt"), name)
            nested = self.call(tool, root="中文", **args)
            self.assertIn("中文/keep.txt", nested.content)
            self.assertNotIn("skip.txt", nested.content)
            ignored_root = self.call(tool, root=".openclaw", **args)
            self.assertNotIn("noise.txt", ignored_root.content)

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


class EditReceiptTests(_Workspace):
    def test_patch_conflict_reports_whether_a_previous_file_was_committed(self):
        mkstemp = tempfile.mkstemp

        for conflict_index in (0, 1):
            with self.subTest(conflict_index=conflict_index):
                paths = [self.ws / 'one.txt', self.ws / 'two.txt']
                for path in paths:
                    path.write_bytes(b'original')
                edits = [dict(path=path.name, old='original', new='patched') for path in paths]
                target = paths[conflict_index]

                def start_write(**kwargs):
                    if kwargs['prefix'] == f'.{target.name}.':
                        target.write_bytes(b'user edit')
                    return mkstemp(**kwargs)

                with patch('tempfile.mkstemp', side_effect=start_write):
                    result = self.call('apply_patch', patches=edits)
                self.assertFalse(result.ok)
                self.assertEqual(result.meta['error_code'], 'revision_conflict')
                self.assertEqual(result.meta['execution_state'], 'unknown' if conflict_index else 'not_started')
                self.assertEqual(result.meta['committed'], [str(path.resolve()) for path in paths[:conflict_index]])
                self.assertEqual(target.read_bytes(), b'user edit')
                other = paths[1 - conflict_index]
                self.assertEqual(other.read_bytes(), b'patched' if conflict_index else b'original')


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
