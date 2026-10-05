"""test_remote_sources.py — 远程插件源同步（Claude 官方市场 + ClawHub）。

全程临时 home + mock 网络，不碰真实 ~/.forge；其中一条用例显式跑真网络
（标记 REAL_NET，默认也跑——两个源都只有 ~200KB，失败只记账不阻塞）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_market as pm
import compat_sources

CLAUDE_PAYLOAD = {"plugins": [
    {"name": "code-review", "description": "Review changes", "author": {"name": "Anthropic"},
     "source": "./plugins/code-review", "homepage": "https://example.com"},
    {"name": "", "description": "invalid, should be skipped"},
    "not-a-dict",
]}
CLAWHUB_PAYLOAD = {"items": [
    {"ownerHandle": "awspace", "slug": "pdf", "displayName": "Pdf",
     "summary": "PDF toolkit", "stats": {"downloads": 50018},
     "canonicalUrl": "/awspace/skills/pdf"},
    {"slug": "", "displayName": "no-slug-skipped"},
]}


class RemoteCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="forge-remote-")
        self.addCleanup(self._td.cleanup)
        self.market = pm.Marketplace(home=self._td.name,
                                     builtin_catalog=Path(self._td.name) / "none.json")

    @staticmethod
    def fake_ok(url, timeout=10.0):
        return CLAUDE_PAYLOAD if "claude" in url else CLAWHUB_PAYLOAD


class TestConverters(RemoteCase):
    def test_claude_payload_to_manifest(self):
        entries = compat_sources.convert_remote_payload("claude-marketplace", CLAUDE_PAYLOAD)
        self.assertEqual(len(entries), 1)  # 空 name 与非 dict 被跳过
        p = pm.parse_manifest(entries[0], source="remote")
        self.assertEqual(p.id, "claude-code-review")
        self.assertFalse(p.executes_code)
        self.assertEqual(p.source, "remote")
        self.assertIn("repo.read", p.capabilities)

    def test_clawhub_payload_to_manifest(self):
        entries = compat_sources.convert_remote_payload("clawhub", CLAWHUB_PAYLOAD)
        self.assertEqual(len(entries), 1)
        p = pm.parse_manifest(entries[0], source="remote")
        self.assertEqual(p.id, "openclaw-awspace-pdf")
        self.assertIn("https://clawhub.ai/awspace/skills/pdf", p.homepage)
        self.assertFalse(p.executes_code)

    def test_bad_schema_raises(self):
        with self.assertRaises(ValueError):
            compat_sources.convert_remote_payload("claude-marketplace", {"nope": 1})
        with self.assertRaises(ValueError):
            compat_sources.convert_remote_payload("clawhub", [1, 2, 3])
        with self.assertRaises(ValueError):
            compat_sources.convert_remote_payload("unknown-kind", {})
        with self.assertRaises(ValueError):
            compat_sources.convert_remote_payload("clawhub", {"items": []})  # 0 条也拒


class TestSync(RemoteCase):
    def test_default_sources_present_and_not_state_seeded(self):
        # state["sources"] 保持 []（既有测试契约），默认源走 DEFAULT_SOURCES
        self.assertEqual(self.market.state().get("sources"), [])
        self.assertEqual(len(self.market.remote_sources()), 2)

    def test_sync_success_writes_cache_and_entries(self):
        with patch.object(pm, "_http_get_json", side_effect=self.fake_ok):
            res = self.market.sync_sources(timeout=1)
        self.assertTrue(all(v["ok"] for v in res.values()), res)
        entries = self.market.remote_entries()
        ids = {e.id for e in entries}
        self.assertIn("claude-code-review", ids)
        self.assertIn("openclaw-awspace-pdf", ids)
        # 真实缓存文件存在
        cache_files = list(self.market.market_dir.joinpath("cache").glob("*.json"))
        self.assertEqual(len(cache_files), 2)
        # last_sync 落盘（重新加载还在——default_state 键）
        m2 = pm.Marketplace(home=self._td.name,
                            builtin_catalog=Path(self._td.name) / "none.json")
        self.assertEqual(len(m2.last_sync_info()), 2)
        self.assertEqual(len(m2.remote_entries()), 2)

    def test_catalog_merges_remote(self):
        with patch.object(pm, "_http_get_json", side_effect=self.fake_ok):
            self.market.sync_sources(timeout=1)
        cat = self.market.catalog()
        ids = {p.id for p in cat}
        self.assertIn("claude-code-review", ids)
        self.assertIn("openclaw-awspace-pdf", ids)

    def test_failure_keeps_old_cache_and_throttles(self):
        with patch.object(pm, "_http_get_json", side_effect=self.fake_ok):
            self.market.sync_sources(timeout=1)
        with patch.object(pm, "_http_get_json", side_effect=OSError("no network")):
            res = self.market.sync_sources(timeout=1)
        self.assertTrue(all(not v["ok"] for v in res.values()))
        # 旧缓存还在
        self.assertEqual(len(self.market.remote_entries()), 2)
        # error 记账
        self.assertTrue(all(v.get("error") for v in self.market.last_sync_info().values()))
        # 失败 1 小时内不再自动同步（离线不打爆）
        self.assertFalse(self.market.needs_remote_sync())

    def test_needs_sync_lifecycle(self):
        self.assertTrue(self.market.needs_remote_sync())          # 首次无缓存
        with patch.object(pm, "_http_get_json", side_effect=self.fake_ok):
            self.market.sync_sources(timeout=1)
        self.assertFalse(self.market.needs_remote_sync())         # 新鲜
        # 拨回很久以前 → 过期
        with pm.market_lock(self.market.market_dir / "state.lock"):
            self.market._state = self.market._load_state()
            for url in self.market._state["last_sync"]:
                self.market._state["last_sync"][url]["at_epoch"] = 0
            self.market.save()
        m2 = pm.Marketplace(home=self._td.name,
                            builtin_catalog=Path(self._td.name) / "none.json")
        self.assertTrue(m2.needs_remote_sync())

    def test_remote_disabled_hides_everything(self):
        with patch.object(pm, "_http_get_json", side_effect=self.fake_ok):
            self.market.sync_sources(timeout=1)
        self.market._state["remote_disabled"] = True
        self.market.save()
        m2 = pm.Marketplace(home=self._td.name,
                            builtin_catalog=Path(self._td.name) / "none.json")
        self.assertEqual(m2.remote_sources(), [])
        self.assertEqual(m2.remote_entries(), [])
        self.assertFalse(m2.needs_remote_sync())
        self.assertEqual(m2.state()["sources"], [])  # 契约不破

    def test_custom_source_appends(self):
        self.market._state["sources"] = [
            "https://example.com/a.json",
            {"kind": "clawhub", "url": "https://example.com/b", "name": "自定义"}]
        self.market.save()
        m2 = pm.Marketplace(home=self._td.name,
                            builtin_catalog=Path(self._td.name) / "none.json")
        self.assertEqual(len(m2.remote_sources()), 4)

    def test_size_cap_rejects_oversized(self):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"x" * (pm.REMOTE_MAX_BYTES + 1)

        import urllib.request
        with patch.object(urllib.request, "urlopen", return_value=Resp()):
            with self.assertRaises(ValueError):
                pm._http_get_json("https://example.com/big.json", timeout=1)

    def test_real_network_end_to_end(self):
        """真网络冒烟（~200KB）：只断言拉到了条目，不抦地址细节。"""
        m = pm.Marketplace(home=tempfile.mkdtemp(prefix="forge-remote-real-"))
        res = m.sync_sources(timeout=20)
        ok = [v for v in res.values() if v.get("ok")]
        self.assertTrue(ok, res)
        self.assertGreater(sum(int(v.get("count") or 0) for v in ok), 0)


if __name__ == "__main__":
    unittest.main()
