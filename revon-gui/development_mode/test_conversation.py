"""Coding Mode 对话区的呈现：只建一个 Conversation 控件，不启动整个工作台。"""
import time
import tkinter as tk
import unittest

import gui_theme as theme
from development_mode.agent_panel import Conversation, reference_spans


def make_root(attempts=5):
    """本机 Tk 启动偶发读不到自带的 .tcl 脚本（与被测代码无关）；重试几次再放弃。"""
    for attempt in range(attempts):
        try:
            return tk.Tk()
        except tk.TclError:
            if attempt == attempts - 1:
                raise
            time.sleep(.2)


class ReferenceSpanTests(unittest.TestCase):
    def links(self, text):
        return [(shown, kind, target) for shown, kind, target in reference_spans(text) if kind]

    def test_dotted_identifiers_are_not_files(self):
        self.assertEqual(self.links('按 delta.tool_calls 累积，item.get 取值，版本 v0.3，耗时 1.5 秒'), [])

    def test_paths_and_known_file_names_are_files(self):
        self.assertEqual(self.links('看 revon-gui/revon_client.py:330、README.md 与 [入口](one.py)'), [
            ('revon-gui/revon_client.py:330', 'file', 'revon-gui/revon_client.py:330'),
            ('README.md', 'file', 'README.md'), ('入口', 'file', 'one.py')])

    def test_url_is_one_link_and_never_split_into_a_fake_path(self):
        self.assertEqual(self.links('更多见 https://platform.openai.com/docs。'),
                         [('https://platform.openai.com/docs', 'url', 'https://platform.openai.com/docs')])
        self.assertEqual(self.links('[文档](https://a.b/c)'), [('文档', 'url', 'https://a.b/c')])

    def test_text_is_preserved_around_links(self):
        text = '更多见 https://a.b/c。然后改 src/x.py（两处）'
        self.assertEqual(''.join(shown for shown, _kind, _target in reference_spans(text)), text)


class ConversationRenderTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.root.geometry('700x600')
        theme.apply_ttk_theme(self.root)
        self.opened, self.urls = [], []
        self.chat = Conversation(self.root, self.opened.append)
        self.chat.open_url = self.urls.append
        self.chat.pack(fill='both', expand=True)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def body(self):
        return self.chat.get('1.0', 'end-1c')

    def test_role_and_time_share_one_line(self):
        self.chat.message('你', 'hello', timestamp=1791882420)
        head = self.body().split('\n')[0]
        self.assertTrue(head.startswith('你'))
        self.assertRegex(head, r'\d\d:\d\d$')
        self.assertTrue(self.chat.tag_ranges('timestamp'))
        self.assertTrue(self.chat.tag_ranges('user_heading'))

    def test_restored_roles_use_readable_names(self):
        self.chat.message('user', 'old question', historical=True)
        self.chat.message('assistant', 'old answer', historical=True)
        lines = self.body().split('\n')
        self.assertTrue(lines[0].startswith('你'))
        self.assertTrue(any(line.startswith('Coding Agent') for line in lines))
        self.assertIn('时间未记录', self.body())

    def test_user_text_keeps_its_own_line_breaks(self):
        self.chat.message('你', 'Traceback:\n  File "a.py", line 3\nValueError')
        self.assertIn('Traceback:\n  File "a.py", line 3\nValueError', self.body())
        self.assertTrue(self.chat.tag_ranges('user_body'))

    def test_table_is_aligned_columns_not_raw_pipes(self):
        self.chat.message('Coding Agent', '| 线路 | 受影响 |\n| --- | --- |\n| OpenAI | 是 |\n| Anthropic | 否 |')
        body = self.body()
        self.assertNotIn('|', body)
        self.assertNotIn('---', body)
        self.assertIn('线路\t受影响', body)
        self.assertIn('Anthropic\t否', body)
        self.assertTrue(self.chat.tag_ranges('table_head'))

    def test_code_block_has_caption_highlight_and_copy(self):
        code = 'def f():\n    return "x"  # note'
        self.chat.message('Coding Agent', 'before\n\n```python\n' + code + '\n```\n\nafter')
        body = self.body()
        self.assertIn('python\t复制\n' + code + '\n', body)
        for tag in ('codeblock', 'code_caption', 'kw', 'st', 'cm'):
            self.assertTrue(self.chat.tag_ranges(tag), tag)
        # 高亮必须落在代码块自己的区间里，而不是从文档开头数偏移。
        first, last = (str(index) for index in self.chat.tag_ranges('kw')[:2])
        self.assertEqual(self.chat.get(first, last), 'def')
        copy = next(name for name in self.chat.tag_names() if name.startswith('copy') and name != 'copy')
        self.chat._copy_code(copy, code)
        self.assertEqual(self.root.clipboard_get(), code)
        self.assertIn('已复制', self.body())
        self.chat._relabel(copy, '复制')
        self.assertNotIn('已复制', self.body())
        self.assertEqual(self.chat.cget('state'), 'disabled')

    def test_links_route_files_and_urls_separately(self):
        self.chat.message('Coding Agent', '改了 src/a.py:3，参考 https://example.com/doc 。`delta.tool_calls` 不是文件')
        names = self.chat.tag_names()
        files = [name for name in names if name.startswith('file')]
        urls = [name for name in names if name.startswith('url')]
        self.assertEqual((len(files), len(urls)), (1, 1))
        self.assertEqual(self.chat.get(*self.chat.tag_ranges(files[0])), 'src/a.py:3')
        self.assertEqual(self.chat.get(*self.chat.tag_ranges(urls[0])), 'https://example.com/doc')
        self.root.update()
        for name in (files[0], urls[0]):
            x, y, _w, _h = self.chat.bbox(str(self.chat.tag_ranges(name)[0]) + '+2c')
            self.chat.event_generate('<Motion>', x=x + 1, y=y + 1)
            self.chat.event_generate('<Button-1>', x=x + 1, y=y + 1)
            self.chat.event_generate('<ButtonRelease-1>', x=x + 1, y=y + 1)
            self.root.update()
        self.assertEqual(self.opened, ['src/a.py:3'])
        self.assertEqual(self.urls, ['https://example.com/doc'])

    def test_status_roles_are_toned(self):
        self.chat.message('执行失败', 'HTTP 400')
        self.chat.message('任务结果', '状态：final')
        self.assertTrue(self.chat.tag_ranges('error_heading'))
        self.assertTrue(self.chat.tag_ranges('error_body'))
        self.assertTrue(self.chat.tag_ranges('note_heading'))
        self.assertFalse(self.chat.tag_ranges('agent_heading'))

    def test_new_agent_output_does_not_drag_a_reader_back_down(self):
        for index in range(40):
            self.chat.message('Coding Agent', f'line {index}\n\nmore')
        self.root.update()
        self.assertEqual(self.chat.yview()[1], 1.0, '停在底部时跟随新消息')
        self.chat.yview_moveto(0)
        self.chat._update_follow()          # 等同于用户滚动后的重新判定
        self.assertFalse(self.chat._follow)
        self.chat.message('Coding Agent', 'while reading earlier output')
        self.root.update()
        self.assertEqual(self.chat.yview()[0], 0.0, '向上翻看时不被新消息拽走')
        self.chat.message('你', 'my own message')
        self.root.update()
        self.assertTrue(self.chat._follow)
        self.assertEqual(self.chat.yview()[1], 1.0, '自己发的消息一定露出来')

    def test_clear_releases_per_block_tags(self):
        self.chat.message('Coding Agent', 'a.py\n\n```\nx\n```\n\n| a | b |\n| --- | --- |\n| 1 | 2 |')
        self.chat.clear()
        leftovers = [name for name in self.chat.tag_names()
                     if name.startswith(('file', 'url', 'copy', 'table')) and name[-1].isdigit()]
        self.assertEqual(leftovers, [])


if __name__ == '__main__':
    unittest.main()
