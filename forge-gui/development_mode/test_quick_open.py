"""Ctrl+P 快速打开的排序与文本整形（纯函数，不创建 Tk 窗口）。"""
import unittest

from development_mode.quick_open import (LIMIT, _clip_px, _match_score, clip,
                                         fit_pair, rank, split_path)


class MatchScoreTests(unittest.TestCase):
    def test_no_match_returns_none(self):
        self.assertIsNone(_match_score('forge/cli.py', 'zzz'))

    def test_subsequence_matches_in_order_only(self):
        self.assertIsNotNone(_match_score('forge/development/state.py', 'fdst'))
        self.assertIsNone(_match_score('forge/development/state.py', 'dsf'))

    def test_case_insensitive(self):
        self.assertEqual(_match_score('Forge/CLI.py', 'cli'),
                         _match_score('Forge/CLI.py', 'CLI'))


class RankTests(unittest.TestCase):
    PATHS = [
        'forge/cli.py',
        'forge/app_store/schema.py',
        'forge-gui/development_mode/app.py',
        'forge-gui/development_mode/agent_panel.py',
        'docs/appendix.md',
        'README.md',
    ]

    def test_empty_query_keeps_scan_order_and_truncates(self):
        self.assertEqual(rank(self.PATHS, ''), self.PATHS[:LIMIT])
        self.assertEqual(rank(self.PATHS, '   '), self.PATHS[:LIMIT])
        self.assertEqual(rank(self.PATHS, '', limit=2), self.PATHS[:2])

    def test_filename_prefix_wins_over_path_substring(self):
        result = rank(self.PATHS, 'app')
        self.assertTrue(result)
        self.assertEqual(result[0], 'forge-gui/development_mode/app.py')
        # 只出现在目录名里的路径仍可命中，但排在文件名命中之后
        self.assertIn('forge/app_store/schema.py', result)
        self.assertLess(result.index('forge-gui/development_mode/app.py'),
                        result.index('forge/app_store/schema.py'))

    def test_directory_only_hit_still_returns_path(self):
        result = rank(self.PATHS, 'docs')
        self.assertEqual(result, ['docs/appendix.md'])

    def test_no_match_yields_empty_list(self):
        self.assertEqual(rank(self.PATHS, 'qqq'), [])

    def test_limit_is_respected(self):
        self.assertEqual(len(rank(self.PATHS, 'e', limit=3)), 3)

    def test_shorter_path_breaks_ties(self):
        paths = ['a/b/agent_panel.py', 'agent_panel.py']
        result = rank(paths, 'agent_panel')
        self.assertEqual(result[0], 'agent_panel.py')

    def test_result_is_deterministic(self):
        self.assertEqual(rank(self.PATHS, 'forge'), rank(self.PATHS, 'forge'))


class TextShapeTests(unittest.TestCase):
    def test_split_path_keeps_directory_separator(self):
        self.assertEqual(split_path('forge-gui/app.py'),
                         ('app.py', 'forge-gui/'))
        self.assertEqual(split_path('app.py'), ('app.py', ''))

    def test_clip_keeps_tail_when_asked(self):
        self.assertEqual(clip('forge/development_mode/', 12, from_left=True),
                         '…pment_mode/')
        self.assertEqual(clip('app.py', 10), 'app.py')

    def test_clip_truncates_from_right_by_default(self):
        self.assertEqual(clip('application_state.py', 10), 'applicati…')


class PixelClipTests(unittest.TestCase):
    """按像素预算裁字（量宽用等宽假字体：每字符 10 单位）。"""

    @staticmethod
    def measure(text):
        return len(text) * 10

    def test_fits_returns_text_untouched(self):
        self.assertEqual(_clip_px('app.py', 200, self.measure), 'app.py')

    def test_trims_until_ellipsis_fits(self):
        # 预算 50 = 4 字符 + 省略号；留头保后缀
        self.assertEqual(_clip_px('application.py', 50, self.measure),
                         'appl…')

    def test_from_left_keeps_tail(self):
        # 预算 50 = 省略号 + 4 字符，留下离文件最近的那截
        self.assertEqual(_clip_px('forge/dev/app.py', 50, self.measure,
                                  from_left=True), '…p.py')

    def test_room_too_small_for_ellipsis_yields_empty(self):
        self.assertEqual(_clip_px('app.py', 5, self.measure), '')

    def test_empty_text_is_empty(self):
        self.assertEqual(_clip_px('', 100, self.measure), '')


class FitPairTests(unittest.TestCase):
    """文件名 + 目录共用一行宽度时的分配。"""

    @staticmethod
    def measure(text):
        return len(text) * 10

    def test_both_fit_when_room_is_plenty(self):
        self.assertEqual(
            fit_pair('app.py', 'forge/', 1000, self.measure, self.measure),
            ('app.py', 'forge/'))

    def test_directory_keeps_its_head_when_name_is_long(self):
        # 名字 300、目录 100、gap 32；预算 400 时目录仍拿满，名字被裁
        name, directory = fit_pair(
            'app.py.bak-codingmode-20261013', 'forge-gui/', 400,
            self.measure, self.measure, gap=32)
        self.assertTrue(name.endswith('…'), name)
        self.assertEqual(directory, 'forge-gui/')

    def test_directory_is_clipped_from_left_when_too_long(self):
        name, directory = fit_pair(
            'a.py', 'very/deep/nested/directory/tree/', 400,
            self.measure, self.measure, gap=32)
        self.assertEqual(name, 'a.py')
        self.assertTrue(directory.startswith('…'), directory)
        self.assertTrue(directory.endswith('tree/'), directory)

    def test_no_directory_leaves_whole_budget_to_name(self):
        self.assertEqual(fit_pair('app.py', '', 600, self.measure,
                                  self.measure), ('app.py', ''))

    def test_result_never_exceeds_budget(self):
        name, directory = fit_pair(
            'app.py.bak-codingmode-20261013',
            'forge-gui/development_mode/', 586, self.measure, self.measure,
            gap=32)
        used = self.measure(name) + self.measure(directory) + 32
        self.assertLessEqual(used, 586, (name, directory))


if __name__ == '__main__':
    unittest.main()
