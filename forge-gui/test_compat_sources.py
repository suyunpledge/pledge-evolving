"""test_compat_sources.py — 外部生态适配层回归。

全程使用隔离的假 HOME 目录结构，不碰真实 ~/.forge / ~/.claude / ~/.dsh / ~/.codex。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compat_sources as cs


def _mk_tree(root: Path, spec: dict) -> None:
    for rel, content in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class TestOpenClawScan(unittest.TestCase):
    def test_skill_with_frontmatter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills"
            _mk_tree(root, {
                "file-io/SKILL.md": (
                    "---\nname: file-io\ndescription: >\n  Upload local files\n  to filebin.\n---\n\n# body\n"),
                "no-fm/SKILL.md": "# Just a title\n\nplain body line",
                "notaskill/nested.txt": "x",
            })
            got = cs.scan_openclaw_skills(roots=[root])
            ids = [e.manifest["id"] for e in got]
            self.assertIn("openclaw-file-io", ids)
            self.assertIn("openclaw-no-fm", ids)
            self.assertNotIn("openclaw-notaskill", ids)
            e = next(e for e in got if e.manifest["id"] == "openclaw-file-io")
            self.assertEqual(e.source_eco, "openclaw")
            self.assertEqual(e.install_dir, root / "file-io")
            self.assertFalse(e.manifest["executes_code"])
            self.assertEqual(e.manifest["capabilities"], ["repo.read"])
            self.assertIn("Upload local files to filebin", e.manifest["summary"])
            nofm = next(e for e in got if e.manifest["id"] == "openclaw-no-fm")
            self.assertEqual(nofm.manifest["summary"], "Just a title")

    def test_missing_root_returns_empty(self):
        self.assertEqual(cs.scan_openclaw_skills(roots=[Path("Z:/nope")]), [])

    def test_duplicate_slug_dedupe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills"
            _mk_tree(root, {
                "alpha/SKILL.md": "---\nname: alpha\ndescription: one\n---\n",
                "alpha copy/SKILL.md": "---\nname: alpha\ndescription: two\n---\n",
            })
            got = cs.scan_openclaw_skills(roots=[root])
            ids = [e.manifest["id"] for e in got]
            self.assertEqual(len(ids), len(set(ids)))

    def test_manifest_passes_parse(self):
        # 契约测试：产出的清单必须能过 Forge parse_manifest
        sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving\forge-gui")
        from plugin_market import parse_manifest
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "skills"
            _mk_tree(root, {"s/SKILL.md": "---\nname: s\ndescription: d\n---\n"})
            e = cs.scan_openclaw_skills(roots=[root])[0]
            p = parse_manifest(e.manifest, source="builtin")
            self.assertEqual(p.id, "openclaw-s")
            self.assertFalse(p.executes_code)
            # capabilities 声明触发 needs_ack 是预期行为（知悉门槛），只需解析成功


class TestClaudeScan(unittest.TestCase):
    MKT = {
        "official/.claude-plugin/marketplace.json": json.dumps({
            "name": "official", "owner": {"name": "Anthropic"},
            "plugins": [
                {"name": "code-review", "description": "Review code quality",
                 "author": {"name": "Anthropic"}, "source": "./plugins/code-review"},
                {"name": "remote-thing", "description": "From git",
                 "source": {"source": "git-subdir", "url": "https://github.com/x/y.git", "path": "plugins/thing"}},
                {"name": "", "description": "bad"},
                "not-a-dict",
            ]}),
        "official/plugins/code-review/.claude-plugin/plugin.json": json.dumps({"name": "code-review"}),
        "official/plugins/code-review/commands/code-review.md": "---\ndescription: Review changes\n---\nbody",
    }

    def test_scan_and_dedupe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "marketplaces"
            _mk_tree(root, self.MKT)
            got = cs.scan_claude_marketplace(root=root)
            ids = [e.manifest["id"] for e in got]
            self.assertIn("claude-code-review", ids)
            self.assertIn("claude-remote-thing", ids)
            cr = next(e for e in got if e.manifest["id"] == "claude-code-review")
            self.assertEqual(cr.source_eco, "claude")
            self.assertEqual(cr.install_dir, root / "official" / "plugins" / "code-review")
            self.assertEqual(cr.manifest["author"], "Anthropic")
            self.assertFalse(cr.manifest["executes_code"])

    def test_command_summary_fallback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "marketplaces"
            no_desc = dict(self.MKT)
            no_desc["official/.claude-plugin/marketplace.json"] = json.dumps({
                "plugins": [{"name": "code-review", "source": "./plugins/code-review"}]})
            _mk_tree(root, no_desc)
            got = cs.scan_claude_marketplace(root=root)
            cr = next(e for e in got if e.manifest["id"] == "claude-code-review")
            self.assertEqual(cr.manifest["summary"], "Review changes")

    def test_bad_marketplace_json_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "marketplaces"
            _mk_tree(root, {".claude-plugin/marketplace.json": "{broken json"})
            self.assertEqual(cs.scan_claude_marketplace(root=root), [])

    def test_manifest_passes_parse(self):
        sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving\forge-gui")
        from plugin_market import parse_manifest
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "marketplaces"
            _mk_tree(root, self.MKT)
            for e in cs.scan_claude_marketplace(root=root):
                p = parse_manifest(e.manifest, source="builtin")
                self.assertTrue(p.id.startswith("claude-"))
                self.assertFalse(p.executes_code)


class TestDshScan(unittest.TestCase):
    def test_dependencies_and_bundle_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "profiles"
            _mk_tree(root, {
                "web/package.json": json.dumps({
                    "dependencies": {
                        "dsh-tavern": "^2.5.0",
                        "seekmaid-pet": "file:C:/Users/x/SeekMaid/",
                        "@dsh-external/skin": "github:a/b",
                    },
                    "dsh": {"profile": {"bundles": ["dsh-tavern"]}},
                }),
                "headless/package.json": json.dumps({"dependencies": {"dsh-tavern": "^2.5.0"}}),
                "notdir.txt": "x",
            })
            got = cs.scan_dsh_bundles(root=root)
            ids = {e.manifest["id"] for e in got}
            self.assertIn("dsh-dsh-tavern", ids)
            self.assertIn("dsh-seekmaid-pet", ids)
            self.assertIn("dsh-dsh-external-skin", ids)
            tav = [e for e in got if e.manifest["id"] == "dsh-dsh-tavern"]
            self.assertEqual(len(tav), 1)  # 跨 profile 去重
            self.assertIn("生效中", tav[0].manifest["summary"])  # web 激活态优先保留
            skin = next(e for e in got if e.manifest["id"] == "dsh-dsh-external-skin")
            self.assertIn("未激活", skin.manifest["summary"])

    def test_manifest_passes_parse(self):
        sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving\forge-gui")
        from plugin_market import parse_manifest
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "profiles"
            _mk_tree(root, {"web/package.json": json.dumps({
                "dependencies": {"dsh-tavern": "^2.5.0"},
                "dsh": {"profile": {"bundles": ["dsh-tavern"]}}})})
            for e in cs.scan_dsh_bundles(root=root):
                parse_manifest(e.manifest, source="builtin")


class TestCodexScan(unittest.TestCase):
    CFG = (
        '# top comment\n'
        'model = "gpt-5"\n'
        '[plugins."codex-app-tools@openai-bundled"]\n'
        'enabled = true\n'
        '[plugins."disabled-one@openai-bundled"]\n'
        'enabled = false\n'
        '[mcp_servers.node_repl]\n'
        'args = []\n'
        "command = 'C:/bin/node_repl.exe'\n"
        '[mcp_servers.node_repl.env]\n'
        'X = "1"\n'
    )

    def test_plugins_and_mcp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text(self.CFG, encoding="utf-8")
            got = cs.scan_codex_plugins(home=home)
            ids = [e.manifest["id"] for e in got]
            self.assertIn("codex-codex-app-tools", ids)
            self.assertIn("codex-mcp-node_repl", ids)
            self.assertNotIn("codex-disabled-one", ids)
            mcp = next(e for e in got if e.manifest["id"] == "codex-mcp-node_repl")
            self.assertIn("node_repl.exe", mcp.manifest["summary"])

    def test_empty_config(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(cs.scan_codex_plugins(home=Path(td)), [])

    def test_toml_section_env_not_leaked_into_mcp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.toml").write_text(self.CFG, encoding="utf-8")
            got = cs.scan_codex_plugins(home=home)
            mcp = next(e for e in got if e.manifest["id"] == "codex-mcp-node_repl")
            # env 段的键不能混进 mcp 主段
            self.assertNotIn("X", mcp.manifest["summary"])


class TestAggregation(unittest.TestCase):
    def test_compat_entries_aggregates_and_caches(self):
        calls = {"n": 0}

        def fake_scan():
            calls["n"] += 1
            return [cs.CompatEntry(source_eco="openclaw", origin="x", manifest={"id": "openclaw-x"})]

        orig = cs.scan_openclaw_skills
        cs.scan_openclaw_skills = fake_scan
        try:
            cs._SCAN_CACHE.clear()
            cs._SCAN_CACHE_AT.clear()
            got1 = cs.compat_entries()
            got2 = cs.compat_entries()
            self.assertEqual(len(got1), len(got2))
            self.assertEqual(calls["n"], 1)  # 第二次走缓存
            got3 = cs.compat_entries(refresh=True)
            self.assertEqual(calls["n"], 2)
        finally:
            cs.scan_openclaw_skills = orig

    def test_single_source_failure_does_not_break_all(self):
        def boom():
            raise RuntimeError("scan failed")

        orig = cs.scan_claude_marketplace
        cs.scan_claude_marketplace = boom
        try:
            cs._SCAN_CACHE.clear()
            cs._SCAN_CACHE_AT.clear()
            got = cs.compat_entries(refresh=True)
            self.assertIsInstance(got, list)  # 不炸，其他源照常
        finally:
            cs.scan_claude_marketplace = orig

    def test_eco_labels(self):
        labels = cs.eco_labels()
        self.assertEqual(len(labels), 4)
        self.assertIn("claude", labels)


if __name__ == "__main__":
    unittest.main()
