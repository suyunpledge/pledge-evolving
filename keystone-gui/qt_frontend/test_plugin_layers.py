"""一切皆插件：插件补丁层合成、宿主授权行拒收、单插件降级、运行席位开关、插件页。"""
import json
from pathlib import Path
import tempfile
import unittest

from config_model import load_user_layer, save_user_layer
from keystone.config import load_config, plugin_patch_problems
from keystone.extensions import disabled_seats, mount_contrib_extensions
from plugin_market import Marketplace, parse_manifest
from .gateway_service import chat_model_routes, model_rows
from . import plugin_page

BUNDLE = Path(__file__).resolve().parents[2] / 'keystone' / 'bundles' / 'base.json'
PROVIDER = {'id': 'plug-model', 'name': 'provider:plug-model', 'config': {
    'wire': 'openai', 'baseURL': 'https://llm.example.test/v1', 'model': 'plug-1', 'modelLabel': '插件模型'}}


def write_plugin(root, pid, patch, **extra):
    folder = Path(root) / pid
    folder.mkdir(parents=True)
    manifest = {'id': pid, 'name': pid, 'version': '1.0.0', 'kind': 'integration',
                'contributions': {'patch': patch}, **extra}
    (folder / 'keystone-plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
    return folder


class PluginLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / 'home'
        self.src = Path(self.temp.name) / 'src'
        self.home.mkdir()
        self.market = Marketplace(self.home, builtin_catalog=Path(self.temp.name) / 'none.json')

    def tearDown(self):
        self.temp.cleanup()

    def install(self, pid, patch):
        write_plugin(self.src, pid, patch)
        self.market.install_from_dir(self.src / pid)
        plugin = self.market.find(pid)
        self.market.ack(pid, expected_fingerprint=plugin.ack_of())
        return self.market.enable(pid)

    def test_protected_rows_are_refused_at_install(self):
        self.assertTrue(plugin_patch_problems([{'id': 'policy', 'config': {'allow': ['*']}}]))
        self.assertTrue(plugin_patch_problems([{'id': 'x', 'name': 'tools:expose'}]))
        self.assertTrue(plugin_patch_problems([{'remove': 'model'}]))
        self.assertFalse(plugin_patch_problems([PROVIDER]))
        with self.assertRaises(ValueError):
            parse_manifest({'id': 'evil', 'name': 'evil', 'contributions': {'patch': [{'id': 'sandbox'}]}})

    def test_layer_is_live_only_while_installed_enabled_and_acked(self):
        self.install('plug', [PROVIDER])
        layer = self.home / 'plugin-layers' / 'plug.json'
        self.assertTrue(layer.is_file())
        cfg = load_config(self.home, bundles=[BUNDLE])
        self.assertIn('plugin:plug', cfg.history)
        self.assertEqual(cfg.row('plug-model').source, 'plugin:plug')
        # The user layer still wins by id.
        save_user_layer(self.home, [{**PROVIDER, 'config': {**PROVIDER['config'], 'model': 'mine'}}])
        cfg = load_config(self.home, bundles=[BUNDLE])
        self.assertEqual(cfg.row('plug-model').config['model'], 'mine')
        # Recovery path skips plugins with the user layer.
        self.assertNotIn('plugin:plug', load_config(self.home, bundles=[BUNDLE], include_user_layer=False).history)
        self.market.disable('plug')
        self.assertFalse(layer.exists())
        self.market.enable('plug')
        self.assertTrue(layer.exists())
        self.market.uninstall('plug')
        self.assertFalse(layer.exists())

    def test_a_broken_or_tampered_layer_only_degrades_itself(self):
        self.install('good', [PROVIDER])
        folder = self.home / 'plugin-layers'
        (folder / 'bad.json').write_text('[{"id": "policy", "config": {}}]', encoding='utf-8')
        (folder / 'worse.json').write_text('{not json', encoding='utf-8')
        cfg = load_config(self.home, bundles=[BUNDLE])
        self.assertIn('plugin:good', cfg.history)
        self.assertEqual(cfg.row('policy').source, 'base.json')
        self.assertEqual(len(cfg.warnings), 2)

    def test_provider_plugins_reach_the_model_picker(self):
        self.install('plug', [PROVIDER])
        routes = chat_model_routes(model_rows(self.home))
        self.assertIn('插件模型', routes)
        self.assertEqual(routes['插件模型'][1]['model'], 'plug-1')

    def test_seats_are_plugins_that_can_be_switched_off(self):
        self.assertIn('thinking', mount_contrib_extensions(home=self.home))
        plugin_page.set_seat(self.home, 'thinking', False)
        self.assertEqual(disabled_seats(self.home), {'thinking'})
        slots = mount_contrib_extensions(home=self.home)
        self.assertNotIn('thinking', slots)
        self.assertIn('compactor', slots)
        plugin_page.set_seat(self.home, 'thinking', True)
        self.assertEqual(disabled_seats(self.home), set())
        self.assertEqual([r['id'] for r in load_user_layer(self.home)], ['seats'])

    def test_page_groups_seats_then_plugins_by_state(self):
        self.install('plug', [PROVIDER])
        write_plugin(self.src, 'pending', [PROVIDER | {'id': 'other'}])
        self.market.install_from_dir(self.src / 'pending')
        data = plugin_page.view(self.home, self.market, self.home)
        ids = [r['id'] for r in data['rows']]
        self.assertTrue(all(i.startswith('seat:') for i in ids[:8]))
        self.assertEqual(ids[8:], ['pending', 'plug'])
        pending = data['rows'][8]
        self.assertIn('需确认信任', pending['subtitle'])
        self.assertIn('补丁层 1', pending['subtitle'])
        self.assertEqual([a['key'] for a in pending['actions']], ['inspect', 'ack', 'uninstall'])
        self.assertIn('1 个补丁层', data['hint'])
        filtered = plugin_page.view(self.home, self.market, self.home, query='pending')
        self.assertEqual([r['id'] for r in filtered['rows']], ['pending'])
        result = plugin_page.act(self.home, self.market, self.home, 'seatOff', {'id': 'seat:replay'}, lambda *_: True)
        self.assertFalse(result['enabled'])
        provenance = plugin_page.act(self.home, self.market, self.home, 'provenance', {}, lambda *_: True)
        self.assertIn('plugin:plug', provenance['layers'])


if __name__ == '__main__':
    unittest.main()
