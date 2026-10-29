"""键位表（shortcuts）的格式化与一致性约束——纯逻辑，不建 Tk 窗口。"""
import unittest

from development_mode import shortcuts


class DisplayTests(unittest.TestCase):
    def test_windows_modifier_and_case(self):
        self.assertEqual(shortcuts.display('Mod+P', mac=False), 'Ctrl+P')
        self.assertEqual(shortcuts.display('Mod+Shift+S', mac=False), 'Ctrl+Shift+S')
        self.assertEqual(shortcuts.display('Control+Tab', mac=False), 'Ctrl+Tab')

    def test_windows_named_keys(self):
        self.assertEqual(shortcuts.display('Return', mac=False), 'Enter')
        self.assertEqual(shortcuts.display('Shift+Return', mac=False), 'Shift+Enter')
        self.assertEqual(shortcuts.display('F5', mac=False), 'F5')

    def test_mac_uses_symbols_without_plus(self):
        self.assertEqual(shortcuts.display('Mod+P', mac=True), '⌘P')
        self.assertEqual(shortcuts.display('Mod+Shift+P', mac=True), '⇧⌘P')


class SequenceTests(unittest.TestCase):
    def test_tk_sequence_windows(self):
        self.assertEqual(shortcuts.tk_sequence('Mod+P', mac=False), '<Control-P>')
        self.assertEqual(shortcuts.tk_sequence('Mod+Shift+E', mac=False),
                         '<Control-Shift-E>')
        self.assertEqual(shortcuts.tk_sequence('F5', mac=False), '<F5>')

    def test_tk_sequence_mac(self):
        self.assertEqual(shortcuts.tk_sequence('Mod+P', mac=True), '<Command-P>')

    def test_every_global_binding_is_unique(self):
        sequences = [sequence for sequence, _key in shortcuts.bindings()]
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_local_shortcuts_are_not_bound_globally(self):
        bound_keys = {key for _seq, key in shortcuts.bindings()}
        self.assertNotIn('send', bound_keys)
        self.assertNotIn('newline', bound_keys)
        # 但文案仍然可取——输入区的键帽提示靠它
        self.assertEqual(shortcuts.hint('send'), 'Enter')
        self.assertEqual(shortcuts.hint('send_force'), 'Ctrl+Enter')


class LookupTests(unittest.TestCase):
    def test_unknown_action_is_silent(self):
        self.assertIsNone(shortcuts.get('nope'))
        self.assertEqual(shortcuts.hint('nope', '—'), '—')
        self.assertEqual(shortcuts.tooltip('nope', '自定义'), '自定义')

    def test_tooltip_contains_binding(self):
        self.assertEqual(shortcuts.tooltip('save'), '保存当前文档（Ctrl+S）')
        self.assertEqual(shortcuts.tooltip('close_tab', '关闭当前标签页'),
                         '关闭当前标签页（Ctrl+W）')

    def test_groups_cover_every_shortcut(self):
        grouped = sum(len(items) for items in shortcuts.groups().values())
        self.assertEqual(grouped, len(shortcuts.SHORTCUTS))

    def test_expected_actions_exist(self):
        for key in ('quick_open', 'save', 'save_as', 'close_tab', 'next_tab',
                    'prev_tab', 'focus_explorer', 'focus_search', 'run_tests'):
            self.assertIsNotNone(shortcuts.get(key), key)


if __name__ == '__main__':
    unittest.main()
