from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from forge.policy import Mode, Policy, Sandbox
from forge.tools import ToolContext, build_builtin_registry


class UtilityToolTests(unittest.TestCase):
    def test_new_tools_execute_under_read_only_policy(self):
        registry = build_builtin_registry()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            context = ToolContext(Policy(workspace=root, mode=Mode.READ_ONLY, sandbox=Sandbox.READ_ONLY), root)
            def transform(action, text, **extra):
                result = registry.invoke('text_transform', {'action': action, 'text': text, **extra}, context)
                self.assertTrue(result.ok, result.error)
                return result.content
            encoded = transform('base64_encode', '示例 🚀')
            self.assertEqual(transform('base64_decode', encoded), '示例 🚀')
            self.assertEqual(transform('json_query', '{"items":[{"name":"中文"}]}', path='items[0].name'), '"中文"')
            self.assertEqual(transform('count', '', mode='lines'), '0')
            for value, source, target, expected in [(68, 'F', 'C', 20), (1, 'KB', 'B', 1000), (1, 'KiB', 'B', 1024)]:
                result = registry.invoke('unit_convert', {'value': value, 'from': source, 'to': target}, context)
                self.assertTrue(result.ok, result.error)
                self.assertAlmostEqual(result.meta['value'], expected)
            self.assertFalse(registry.invoke('unit_convert', {'value': 2, 'from': 'kg', 'to': 'm'}, context).ok)
            self.assertFalse(registry.invoke('text_transform', {'action': 'url_decode', 'text': '%XX'}, context).ok)
            self.assertFalse(registry.invoke('text_transform', {'action': 'json_format', 'text': 'x' * 65537}, context).ok)
            self.assertFalse(list(root.iterdir()))
