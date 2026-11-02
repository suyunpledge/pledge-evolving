"""Coding Agent views over TaskEvidence; no execution or global link handlers."""
import json
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from chat_widgets import parse_blocks, inline_segments
from .buttons import dev_button
from gui_theme import C, FONT_UI, FONT_UI_BOLD, FONT_SECTION, FONT_CAPTION, FONT_SMALL, FONT_MONO_SM, style_editor, button_feedback, attach_tooltip, ui_px
from ui_icons import IconButton
from forge.secrets import redact


# 新手任务模板（图标 / 标题 / 是否改文件 / 填入输入框的草稿）。草稿只填入、
# 不自动发送；「会改文件」提示与权限三档对应。
TASK_TEMPLATES = (
    ('info', '解释这个项目是怎么组织的', '只读',
     '请先阅读这个项目的 README 和目录结构，用通俗的话解释它是做什么的、主要代码在哪里。不要修改任何文件。'),
    ('check', '找出并修复失败的测试', '会改文件',
     '请运行项目已有的测试，找出失败的用例，定位原因并修复；修完再跑一遍测试确认全部通过。'),
    ('plus', '加一个新功能（我来描述）', '会改文件',
     '我想加一个新功能：（在这里描述）。请先给出实现计划，等我确认后再改代码，并补上测试。'),
)

# 对话区空态引导：无消息时显示，避免右侧 Agent 面板呈现为纯黑空洞。
# 文案只描述"这里将会发生什么"，不伪造任何已发生的执行。
CHAT_EMPTY_HINT = (
    '在下方输入任务描述，按 Enter 发送。\n'
    'Agent 的执行过程、工具步骤与产物会实时汇报在这里。'
)


def readonly(parent, **options):
    text = tk.Text(parent, height=6, font=FONT_UI, wrap='word', **options)
    style_editor(text)
    scroll = ttk.Scrollbar(parent, command=text.yview)
    scroll.pack(side='right', fill='y')
    text.configure(yscrollcommand=scroll.set, state='disabled')
    text.pack(fill='both', expand=True)
    return text


def replace(text, body):
    body = str(redact(body))
    if text.get('1.0', 'end-1c') == body:
        return
    position = text.yview()
    text.configure(state='normal')
    text.delete('1.0', 'end')
    text.insert('1.0', body)
    text.configure(state='disabled')
    text.yview_moveto(position[0])


class Conversation(tk.Text):
    """Shared Markdown parser + Text tags; link callbacks belong to this instance."""
    def __init__(self, parent, open_file, history_source=None):
        super().__init__(parent, height=6, wrap='word', font=FONT_UI)
        style_editor(self)
        self.configure(state='disabled', background=C['chat'], highlightthickness=0,
                       padx=10, pady=8, spacing1=3, spacing3=5)
        self.open_file = open_file
        self.history_source = history_source
        self._history_path = None
        self._history_times = defaultdict(deque)
        self.tag_configure('timestamp', foreground=C['comment'], spacing1=6)
        self.tag_configure('heading', font=FONT_SECTION, foreground=C['text'])
        self.tag_configure('user_heading', font=FONT_UI_BOLD, foreground=C['accent2'], spacing1=8)
        self.tag_configure('agent_heading', font=FONT_UI_BOLD, foreground=C['agent_accent'], spacing1=8)
        self.tag_configure('b', font=FONT_UI_BOLD)
        self.tag_configure('code', font=FONT_MONO_SM, background=C['code_bg'])
        self.tag_configure('quote', foreground=C['subtext'], lmargin1=12, lmargin2=12)
        # 空态引导：居中空态卡（图标 + 标题 + 说明，与 bottom_panel 的
        # _empty_state_card 同一视觉语言），place 悬浮在 Text 之上；
        # message() 首次写入真实内容时撤下，clear() 时重建——一次性，
        # 不与真实消息混排。
        self._empty_card = None
        # 新手层：app 注入 on_template（把草稿填进输入框）与锁定原因
        # （没模型 / 没项目时说清楚还差什么）；均可缺省，旧行为不变。
        self.on_template = None
        self._locked_reason = ''
        self._locked_action = None
        self._links = 0
        self._windows = []
        self._last_width = 0
        self._insert_empty_hint()
        self.bind('<Configure>', self._fit_cards)

    def _insert_empty_hint(self):
        """居中空态卡（仅当对话区无内容且无现存卡时）。

        不调用 self.delete()——那会走进 _clear_cards()，而 __init__ 阶段
        self._windows 尚未建立；控件此刻本就是空的，无需先删。卡以
        place 悬浮在 Text 上，Text 自身保持空与禁用，不引入文本态。
        """
        try:
            if self.get('1.0', 'end-1c').strip():
                return
            if self._empty_card is not None and self._empty_card.winfo_exists():
                return
            from ui_icons import icon_image
            box = tk.Frame(self, bg=C['chat'])
            # 内容含模板后变高：从顶部往下排，避免居中时图标被裁掉。
            box.place(relx=0.5, y=ui_px(self, 28), anchor='n')
            img = icon_image(box, 'chat', size=28, fg=C['ter'])
            if img is not None:
                icon = tk.Label(box, image=img, bg=C['chat'], bd=0)
                icon.image = img  # 防 GC
                icon.pack(pady=(0, 8))
            locked = bool(self._locked_reason)
            tk.Label(box, text='完成设置后就能开始' if locked else '和 Coding Agent 开始一个任务',
                     font=FONT_SECTION, bg=C['chat'], fg=C['subtext']).pack()
            tk.Label(box, text=self._locked_reason if locked else CHAT_EMPTY_HINT,
                     font=FONT_CAPTION, bg=C['chat'], fg=C['ter'], justify='center').pack(pady=(4, 0))
            if locked and self._locked_action is not None:
                label, callback = self._locked_action
                self.locked_button = dev_button(box, label, callback, kind='primary')
                self.locked_button.pack(pady=(ui_px(self, 10), 0))
            self._build_templates(box, enabled=not locked)
            self._empty_card = box
        except tk.TclError:
            pass

    def _build_templates(self, box, *, enabled):
        if self.on_template is None:
            return
        tk.Label(box, text='或者从这里开始：', font=FONT_CAPTION, bg=C['chat'],
                 fg=C['faint']).pack(anchor='w', pady=(ui_px(self, 14), ui_px(self, 4)))
        for icon, title, note, prompt in TASK_TEMPLATES:
            row = tk.Frame(box, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'],
                           cursor='hand2' if enabled else 'arrow',
                           padx=ui_px(self, 10), pady=ui_px(self, 7))
            row.pack(fill='x', pady=(0, ui_px(self, 6)))
            glyph = tk.Canvas(row, width=ui_px(self, 16), height=ui_px(self, 16), bg=C['surface'],
                              highlightthickness=0, bd=0)
            from ui_icons import draw_icon
            draw_icon(glyph, icon, size=ui_px(self, 16), fg=C['subtext'] if enabled else C['faint'])
            glyph.pack(side='left', padx=(0, ui_px(self, 8)))
            label = tk.Label(row, text=title, font=FONT_SMALL, bg=C['surface'],
                             fg=C['body'] if enabled else C['faint'], anchor='w')
            label.pack(side='left', fill='x', expand=True)
            tag = tk.Label(row, text=note, font=FONT_CAPTION, bg=C['surface'],
                           fg=C['warn'] if note == '会改文件' and enabled else C['faint'])
            tag.pack(side='right', padx=(ui_px(self, 12), 0))
            if not enabled:
                continue
            parts = (row, glyph, label, tag)
            for widget in parts:
                widget.bind('<Button-1>', lambda _e, text=prompt: self.on_template(text))
                widget.bind('<Enter>', lambda _e, ws=parts: [w.configure(bg=C['hover']) for w in ws])
                widget.bind('<Leave>', lambda _e, ws=parts: [w.configure(bg=C['surface']) for w in ws])

    def set_locked(self, reason, action=None):
        """未就绪（没模型 / 没项目）时空态说明还差什么，模板置灰；就绪后恢复。

        action=(按钮文字, 回调) 时在说明下给一个直达按钮。"""
        reason = reason or ''
        if (reason, action) == (self._locked_reason, self._locked_action) and self._empty_card is not None:
            return
        self._locked_reason, self._locked_action = reason, action
        if self._empty_card is not None:
            self._drop_empty_hint()
            self._insert_empty_hint()

    def _drop_empty_hint(self):
        """撤下一次性空态卡（若存在）。只销毁覆盖层，不动 Text 内容——
        避免连带清掉已插入的卡片。"""
        try:
            card = self._empty_card
            self._empty_card = None
            if card is not None and card.winfo_exists():
                card.destroy()
        except tk.TclError:
            pass

    def _fit_cards(self, event):
        if event.width == self._last_width:
            return
        self._last_width = event.width
        for widget in self._windows:
            if widget.winfo_exists() and hasattr(widget, 'fit'):
                widget.fit(max(100, event.width - 24))
                self._stretch(widget, max(100, event.width - 24))

    @staticmethod
    def _stretch(widget, width):
        """卡片铺满对话列：底部一条 1px、与卡同色的撑宽条。

        嵌在 Text 里的窗口按内容自然宽度显示，fit() 只改折行宽度，卡片会窄成一截。
        卡内部用 grid 时不能混 pack，此时保持原样。
        """
        try:
            pad = sum(int(float(v)) for v in str(widget.cget('padding') or '0').split()[:1]) * 2
        except (tk.TclError, ValueError):
            pad = 0
        try:
            strut = getattr(widget, '_forge_strut', None)
            if strut is None or not strut.winfo_exists():
                strut = tk.Frame(widget, height=1, bg=C['surface'], bd=0, highlightthickness=0)
                strut.pack(side='bottom', fill='x')
                widget._forge_strut = strut
            strut.configure(width=max(1, width - pad))
        except tk.TclError:
            pass

    def clear(self):
        self.configure(state='normal')
        self.delete('1.0', 'end')
        self._clear_cards()
        self.configure(state='disabled')
        self._insert_empty_hint()

    def delete(self, index1, index2=None):
        if index1 == '1.0' and index2 == 'end':
            self._clear_cards()
        return super().delete(index1, index2)

    def _clear_cards(self):
        for widget in self._windows:
            if widget.winfo_exists():
                widget.destroy()
        self._windows.clear()
        self._history_path = None
        self._history_times.clear()

    def insert(self, index, chars, *tags):
        # Existing recovery adapter appends whole historical messages to Text.
        match = re.fullmatch(r'\n(user|assistant)\n([\s\S]*)\n', str(chars)) if not tags else None
        if match:
            return self.message(match[1], match[2], timestamp=self._historical_time(match[1], match[2]), historical=True)
        return super().insert(index, chars, *tags)

    def _historical_time(self, role, body):
        # Read only the already-authorized recovery journal, once per restore.
        # Older logs without timestamps remain explicitly unknown.
        path = self.history_source() if self.history_source else None
        if path and path != self._history_path:
            self._history_path = path
            self._history_times.clear()
            from forge.loop import sanitise_child_output
            try:
                with Path(path).open('rb') as stream:
                    raw = stream.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    return None
                nested = 0
                messages = deque(maxlen=100)
                for line in raw.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get('type')
                    if kind == 'subagent_spawn':
                        nested += 1
                    elif kind in {'subagent_result', 'subagent_error'}:
                        nested = max(0, nested - 1)
                    elif kind == 'development_recovery':
                        messages.clear()
                        for message in event.get('history', []):
                            if isinstance(message, dict) and message.get('role') in {'user', 'assistant'}:
                                messages.append((message['role'], str(redact(message.get('content', ''))), message.get('ts')))
                    elif kind in {'user_message', 'assistant_message'} and not event.get('depth', 0) and ('depth' in event or not nested):
                        messages.append(('user' if kind == 'user_message' else 'assistant',
                            sanitise_child_output(str(redact(event.get('content', '')))), event.get('ts')))
                for name, content, ts in messages:
                    self._history_times[(name, content)].append(ts)
            except (OSError, ValueError):
                return None
        times = self._history_times.get((role, body))
        return times.popleft() if times else None

    def message(self, role, body, *, timestamp=None, historical=False):
        self.configure(state='normal')
        self._drop_empty_hint()
        if timestamp is None and not historical:
            timestamp = datetime.now().timestamp()  # actual message display time
        try:
            stamp = datetime.fromtimestamp(float(timestamp)).strftime('%H:%M')
        except (TypeError, ValueError, OverflowError, OSError):
            stamp = '时间未记录'
        self.insert('end', '\n' + stamp + '\n', 'timestamp')
        self.insert('end', '\n' + str(redact(role)) + '\n',
                    'user_heading' if role in {'你', 'user'} else 'agent_heading')
        for block in parse_blocks(str(redact(body))):
            kind, value = block['type'], block.get('text', '')
            if kind == 'hr':
                self.insert('end', '────────\n')
            elif kind in {'code', 'table'}:
                self.insert('end', value + '\n', 'code')
            else:
                prefix = {'li': '• ', 'oli': block.get('num', '') + '. ',
                          'todo': '✓ ' if block.get('done') else '○ ', 'quote': '› '}.get(kind, '')
                self.insert('end', prefix)
                for segment, tag in inline_segments(value):
                    for part in re.split(r'(\[[^\]]+\]\([^\s)]+\)|[\w./\\-]+\.[\w]+(?::\d+)?)', segment):
                        if not part:
                            continue
                        match = re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', part)
                        path = match[2] if match else part
                        if (match or re.fullmatch(r'[\w./\\-]+\.[\w]+(?::\d+)?', part)) and '://' not in path:
                            self._links += 1
                            link = 'file' + str(self._links)
                            self.tag_configure(link, foreground=C['link'], underline=True)
                            self.tag_bind(link, '<Button-1>', lambda e, p=path: self.open_file(p))
                            self.tag_bind(link, '<Enter>', lambda e: self.configure(cursor='hand2'))
                            self.tag_bind(link, '<Leave>', lambda e: self.configure(cursor='xterm'))
                            self.insert('end', match[1] if match else part, (tag or '', link))
                        else:
                            self.insert('end', part, 'heading' if kind == 'h' else 'quote' if kind == 'quote' else tag or '')
                self.insert('end', '\n')
        self.configure(state='disabled')
        self.see('end')

    def card(self, widget):
        # 计划 / 步骤 / 改动卡也算真实内容：撤下空态覆盖层，否则它会盖在卡片上。
        self._drop_empty_hint()
        self.configure(state='normal')
        self.insert('end', '\n')
        self.window_create('end', window=widget)
        self.insert('end', '\n')
        self._windows.append(widget)
        if hasattr(widget, 'fit'):
            widget.fit(max(100, self.winfo_width() - 24))
            self._stretch(widget, max(100, self.winfo_width() - 24))
        self.configure(state='disabled')


# 时间线每行的状态：图标 + 颜色 token（与权限徽标同一套语义色）。
STEP_MARKS = {
    'completed': ('check', 'ok'), 'failed': ('close', 'error'), 'cancelled': ('close', 'faint'),
    'running': ('refresh', 'accent2'), 'awaiting_approval': ('warning', 'warn'),
    'not_started': ('minus', 'faint'), 'unknown': ('info', 'warn'),
}
TIMELINE_ROWS = 8


def step_subject(row):
    """一步工具调用的「对象」：路径 / 命令 / 查询，取不到就空。"""
    args = row.get('args') or {}
    for key in ('path', 'command', 'pattern', 'query', 'url', 'name'):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace('\n', ' ')
    blocks = args.get('patches') or args.get('edits') or []
    paths = [b.get('path') for b in blocks if isinstance(b, dict) and b.get('path')]
    return '、'.join(paths[:2]) + (f' 等 {len(paths)} 个' if len(paths) > 2 else '') if paths else ''


def format_seconds(seconds):
    if seconds is None:
        return ''
    return f'{seconds * 1000:.0f}ms' if seconds < 1 else f'{seconds:.1f}s' if seconds < 60 else \
        f'{int(seconds // 60)}m{int(seconds % 60):02d}s'


def estimate_cost(runs):
    """按 forge.pricing 的账单混合单价估算花费。

    返回 (¥ 合计, 是否有价格未知的运行)。pricing 把「未知价」记为 0.0，
    这里不把它当免费：有 token 却没有单价的运行单独标记为未知。
    """
    from forge.pricing import cost_of, rate_for
    total, unknown = 0.0, False
    for run in runs:
        tokens = int(run.get('tokens') or 0)
        if not tokens:
            continue
        model = str(run.get('model') or '')
        if rate_for(model) > 0:
            total += cost_of(model, tokens)
        else:
            unknown = True
    return total, unknown


def format_cost(total, unknown):
    if not total and not unknown:
        return ''
    if not total:
        return '价格未知'
    amount = f'≈¥{total:.4f}' if total < 0.01 else f'≈¥{total:.2f}'
    return amount + (' · 部分价格未知' if unknown else '')


def format_tokens(tokens):
    if not tokens:
        return ''
    return f'{tokens / 1000:.1f}k tokens' if tokens >= 1000 else f'{tokens} tokens'


# 时间线标题里的状态计数用中文；未知状态原样显示，不吞信息。
STATUS_WORDS = {'completed': '完成', 'running': '进行中', 'failed': '失败', 'awaiting_approval': '待审批',
                'denied': '已拒绝', 'cancelled': '已取消', 'unknown': '结果未知'}


class ToolSummary(ttk.Frame):
    """执行时间线卡：标题（步数 / 状态计数 / token）+ 最近步骤 + 可展开原始结果。

    耗时是界面按事件到达时刻量的（运行时事件不带时间戳），标注为约值；
    token 只取运行时 usage 事件的真实合计，不估算花费。
    """

    def __init__(self, parent):
        super().__init__(parent, padding=8, style='Card.TFrame')
        self.summary = ttk.Label(self, text='工具步骤', font=FONT_UI_BOLD, style='Card.TLabel')
        self.summary.pack(anchor='w')
        self.timeline = tk.Frame(self, bg=C['surface'])
        self.timeline.pack(fill='x', pady=(ui_px(self, 6), ui_px(self, 2)))
        self.button = dev_button(self, '展开原始结果', self.toggle, kind='quiet')
        self.button.pack(anchor='w')
        self.detail_frame = ttk.Frame(self)
        self.detail = readonly(self.detail_frame, width=38)
        self.expanded = False
        self._width = 300
        self._signature = None

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.detail_frame.pack(fill='both', expand=True)
        else:
            self.detail_frame.pack_forget()
        self.button.configure(text='收起原始结果' if self.expanded else '展开原始结果')

    def fit(self, width):
        self._width = width
        self.summary.configure(wraplength=width)
        from tkinter import font
        char = max(1, font.Font(root=self, font=FONT_UI).measure('0'))
        self.detail.configure(width=max(10, (width - 20) // char))
        self._signature = None

    def update_actions(self, actions, *, tokens=None, clock=None, cost=''):
        clock = clock or {}
        counts = {}
        for row in actions:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        parts = [f'执行步骤 · {len(actions)} 步', ' · '.join(
            f'{STATUS_WORDS.get(k, k)} {v}' for k, v in counts.items())]
        if tokens:
            parts.append(format_tokens(tokens))
        if cost:
            parts.append(cost)
        self.summary.configure(text=' · '.join(parts))
        replace(self.detail, '\n'.join(f"{r.get('agent', 'forge')} · {r.get('tool', '')} · {r['status']}\n"
            + str(r.get('result', ''))[:1500] for r in actions[-100:]))
        self._render_timeline(actions, clock)

    def _render_timeline(self, actions, clock):
        import time
        now = time.monotonic()
        recent = actions[-TIMELINE_ROWS:]
        rows = []
        for row in recent:
            started, finished = clock.get(row.get('action_id'), (None, None))
            elapsed = (finished or now) - started if started is not None else None
            rows.append((row.get('action_id'), row.get('agent', 'forge'), row.get('tool', ''),
                         step_subject(row), row['status'],
                         format_seconds(elapsed) if row['status'] != 'running' else
                         ('进行中' if elapsed is None else f'进行中 {format_seconds(elapsed)}')))
        signature = (tuple(rows), len(actions), self._width)
        if signature == self._signature:
            return
        self._signature = signature
        for child in self.timeline.winfo_children():
            child.destroy()
        from ui_icons import draw_icon
        hidden = len(actions) - len(recent)
        if hidden:
            tk.Label(self.timeline, text=f'… 更早的 {hidden} 步在详情里', font=FONT_CAPTION,
                     bg=C['surface'], fg=C['faint'], anchor='w').pack(fill='x')
        size = ui_px(self, 12)
        from tkinter import font as tkfont
        mono, caption = tkfont.Font(root=self, font=FONT_MONO_SM), tkfont.Font(root=self, font=FONT_CAPTION)
        for _id, agent, tool, subject, status, timing in rows:
            line = tk.Frame(self.timeline, bg=C['surface'])
            line.pack(fill='x', pady=1)
            icon, tone = STEP_MARKS.get(status, ('info', 'faint'))
            mark = tk.Canvas(line, width=size, height=size, bg=C['surface'], highlightthickness=0, bd=0)
            draw_icon(mark, icon, size=size, fg=C[tone])
            mark.pack(side='left', padx=(0, ui_px(self, 6)))
            name = tool if agent in ('forge', '') else f'{agent} · {tool}'
            tk.Label(line, text=name, font=FONT_MONO_SM, bg=C['surface'], fg=C['accent2']).pack(side='left')
            if subject:
                # 按字体实测可用宽度截断，保留结尾（文件名 / 命令尾部最有信息量）。
                room = (self._width - size - mono.measure(name) - caption.measure(timing or '')
                        - ui_px(self, 28))
                text = subject
                while len(text) > 4 and caption.measure(text) > room:
                    text = text[2:]
                if text != subject:
                    text = '…' + text
                tk.Label(line, text=text, font=FONT_CAPTION, bg=C['surface'], fg=C['body'],
                         anchor='w').pack(side='left', fill='x', expand=True, padx=(ui_px(self, 6), 0))
            if timing:
                tk.Label(line, text=timing, font=FONT_CAPTION, bg=C['surface'], fg=C['faint']).pack(
                    side='right')


class DiffCard(ttk.Frame):
    def __init__(self, parent, change, *, view, open_file, revert, pending):
        super().__init__(parent, padding=8, style='Card.TFrame')
        self.change = change
        self.title = ttk.Label(self, wraplength=310, font=FONT_UI_BOLD, style='Card.TLabel')
        self.title.pack(anchor='w')
        self.state_label = ttk.Label(self, wraplength=310, style='Muted.Card.TLabel')
        self.state_label.pack(anchor='w')
        self.row = ttk.Frame(self, style='Card.TFrame')
        self.row.pack(fill='x')
        self.buttons = {}
        self._width = 260
        self.callbacks = dict(view=view, open=open_file, revert=revert, pending=pending)
        self.update_change(change)

    def update_change(self, change):
        self.change = dict(change)
        self.title.configure(text='已改动：' + change['path'])
        state = change['state']
        label = {'pending': '待审批 · 未写入', 'written': '已写入工作区', 'committed': '已提交 Git'}.get(state, state)
        counts = ('行数预览不可用' if change.get('added') is None else
                  f"+{change['added']} 行 / −{change['removed']} 行")
        if change.get('revision_changed'):
            label += ' · 当前文件版本已变化'
        self.state_label.configure(text=label + '\n' + counts)
        for button in self.buttons.values():
            button.destroy()
        self.buttons.clear()
        operations = [('view', '查看 Diff'), ('open', '打开文件')]
        if state == 'pending':
            operations.append(('pending', '处理授权 / 重试'))
        elif state == 'written' and change.get('checkpoint') and change.get('before_revision') is not None and not change.get('revision_changed'):
            operations.append(('revert', '撤销（先看 Diff）'))
        for key, label in operations:
            button = dev_button(self.row, label, lambda k=key: self.callbacks[k](self.change), kind='ghost')
            self.buttons[key] = button
        self.fit(self._width)

    def fit(self, width):
        self._width = width
        self.title.configure(wraplength=width)
        self.state_label.configure(wraplength=width)
        # Wrap real actions into rows rather than clipping Revert in narrow panes.
        columns = max(1, width // 110)
        for index, button in enumerate(self.buttons.values()):
            button.grid(row=index // columns, column=index % columns, sticky='w', padx=(0, 4), pady=2)


class ChangesSummary(ttk.Frame):
    """本次任务的改动汇总：文件数与 +/− 合计、每个文件一行，点行看 Diff。"""

    ROWS = 6

    def __init__(self, parent, *, view, review_all=None):
        super().__init__(parent, padding=8, style='Card.TFrame')
        head = tk.Frame(self, bg=C['surface'])
        head.pack(fill='x')
        self.title = tk.Label(head, text='', font=FONT_UI_BOLD, bg=C['surface'], fg=C['text'])
        self.title.pack(side='left')
        self.totals = tk.Label(head, text='', font=FONT_MONO_SM, bg=C['surface'], fg=C['subtext'])
        self.totals.pack(side='left', padx=(ui_px(self, 8), 0))
        if review_all is not None:
            self.review_button = dev_button(head, '逐个审查', review_all, kind='ghost')
            self.review_button.pack(side='right')
        self.rows = tk.Frame(self, bg=C['surface'])
        self.rows.pack(fill='x', pady=(ui_px(self, 6), 0))
        self._view = view
        self._signature = None

    def fit(self, width):
        self._signature = None

    def view_file(self, index):
        if 0 <= index < len(getattr(self, 'files', ())):
            self._view(self.files[index]['change'])

    def update_changes(self, changes):
        by_path = {}
        for change in changes:
            row = by_path.setdefault(change['path'], {'path': change['path'], 'added': 0, 'removed': 0,
                                                      'known': True, 'pending': False, 'change': change})
            if change.get('added') is None:
                row['known'] = False
            else:
                row['added'] += change['added'] or 0
                row['removed'] += change['removed'] or 0
            row['pending'] = row['pending'] or change.get('state') == 'pending'
            row['change'] = change
        files = list(by_path.values())
        self.files = files
        signature = tuple((f['path'], f['added'], f['removed'], f['known'], f['pending']) for f in files)
        if signature == self._signature:
            return
        self._signature = signature
        added = sum(f['added'] for f in files)
        removed = sum(f['removed'] for f in files)
        self.title.configure(text=f'改动 {len(files)} 个文件')
        self.totals.configure(text=f'+{added} −{removed}' + ('' if all(f['known'] for f in files) else ' · 部分行数未知'))
        for child in self.rows.winfo_children():
            child.destroy()
        for index, item in enumerate(files[:self.ROWS]):
            line = tk.Frame(self.rows, bg=C['surface'], cursor='hand2')
            line.pack(fill='x', pady=1)
            path = tk.Label(line, text=item['path'], font=FONT_MONO_SM, bg=C['surface'], fg=C['body'],
                            anchor='w', cursor='hand2')
            path.pack(side='left', fill='x', expand=True)
            parts = [line, path]
            if item['pending']:
                tag = tk.Label(line, text='待授权', font=FONT_CAPTION, bg=C['surface'], fg=C['warn'])
                tag.pack(side='right', padx=(ui_px(self, 6), 0))
                parts.append(tag)
            if item['known']:
                for text, tone in ((f"−{item['removed']}", 'diff_del'), (f"+{item['added']}", 'diff_add')):
                    label = tk.Label(line, text=text, font=FONT_MONO_SM, bg=C['surface'], fg=C[tone])
                    label.pack(side='right', padx=(ui_px(self, 6), 0))
                    parts.append(label)
            for widget in parts:
                widget.bind('<Button-1>', lambda _e, i=index: self.view_file(i))
        hidden = len(files) - self.ROWS
        if hidden > 0:
            tk.Label(self.rows, text=f'… 还有 {hidden} 个文件，点「逐个审查」查看全部', font=FONT_CAPTION,
                     bg=C['surface'], fg=C['faint'], anchor='w').pack(fill='x')


class AgentEvents(ttk.Frame):
    """Embeddable bottom-tab content; consumes the host's existing event stream."""
    def __init__(self, parent):
        super().__init__(parent)
        self.text = readonly(self)

    def append_event(self, event):
        text = json.dumps(redact(event), ensure_ascii=False, indent=2)
        self.text.configure(state='normal')
        self.text.insert('end', text[:16384] + ('\n[预览截断；完整日志见 Session]' if len(text) > 16384 else '') + '\n')
        self.text.configure(state='disabled')


class TaskInput(tk.Text):
    """An overlay placeholder, never inserted into the task sent to Runtime."""
    def __init__(self, parent, **options):
        super().__init__(parent, **options)
        style_editor(self)
        self.placeholder = tk.Label(self, text='输入任务描述 …',
            bg=self.cget('background'), fg=C['placeholder'], font=FONT_UI, justify='left')
        self.placeholder.bind('<Button-1>', lambda event: self.focus_set())
        self.bind('<<Modified>>', self._placeholder_changed, add='+')
        self._placeholder_changed()

    def _placeholder_changed(self, event=None):
        if self.get('1.0', 'end-1c'):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=6, y=6)
        if self.edit_modified():
            self.edit_modified(False)


class PlanningCard(ttk.Frame):
    LEVELS = {'none': '无 (None)', 'low': '低 (Low)', 'medium': '中等 (Medium)', 'high': '高 (High)'}

    def __init__(self, parent, view_changes):
        super().__init__(parent, padding=8, style='Card.TFrame')
        ttk.Label(self, text='任务规划', font=FONT_UI_BOLD, style='Card.TLabel').pack(anchor='w')
        self.level = ttk.Label(self, style='Muted.Card.TLabel')
        self.level.pack(anchor='w')
        self.authorization = ttk.Label(self, wraplength=280, style='Card.TLabel')
        self.authorization.pack(anchor='w')
        self.permission = ttk.Label(self, style='Muted.Card.TLabel')
        self.permission.pack(anchor='w')
        self.view = dev_button(self, '查看变更', view_changes, kind='ghost', state='disabled')
        self.view.pack(anchor='w', pady=(4, 0))
        self._view_changes = view_changes
        self._display = None

    def fit(self, width):
        self.authorization.configure(wraplength=width)

    def update_plan(self, evidence, context):
        level = self.LEVELS.get(context.get('planning'), '级别未记录')
        permission = context.get('permission')
        label = {'read-only': '只读', 'workspace-write': '工作区写'}.get(permission, '权限未记录')
        pending = any(r['status'] == 'awaiting_approval' for r in evidence.actions.values())
        denied = any(r.get('authorization') == 'deny' for r in evidence.actions.values())
        auth = ('待逐动作审批' if pending else '动作被拒绝' if denied else
                '已授权工作区写入' if permission == 'workspace-write' else
                '只读授权' if permission == 'read-only' else '未提交授权')
        if context.get('recovered'):
            auth = '历史授权记录 · 当前恢复为只读'
        enabled = bool(self._view_changes and (evidence.changes or evidence.diffs))
        display = (level, auth, label, enabled)
        if display == self._display:
            return
        self._display = display
        self.level.configure(text=level)
        self.permission.configure(text='权限 · ' + label)
        self.authorization.configure(text='授权 · ' + auth)
        self.view.configure(state='normal' if enabled else 'disabled')
        # 运行时还没上报的项不显示「未记录」占位：全空时只留一句等待说明。
        unknown = {self.level: level == '级别未记录', self.permission: label == '权限未记录',
                   self.authorization: auth == '未提交授权'}
        if all(unknown.values()):
            unknown[self.authorization] = False
            self.authorization.configure(text='等待运行时上报规划与授权')
        for widget in (self.level, self.authorization, self.permission):
            widget.pack_forget()
        for widget in (self.level, self.authorization, self.permission):
            if not unknown[widget]:
                widget.pack(anchor='w', before=self.view)


class AgentPanel(ttk.Frame):
    def __init__(self, parent, *, open_file, view_diff, revert, pending, configure_model, refresh,
                 new_conversation=None, history=None, settings=None, view_changes=None, history_source=None,
                 on_show_approvals=None):
        super().__init__(parent)
        titlebar = ttk.Frame(self, padding=(ui_px(self, 12), ui_px(self, 6), ui_px(self, 6), 0))
        titlebar.pack(fill='x')
        self.title = ttk.Label(titlebar, text='AI Agent', font=FONT_UI_BOLD, foreground=C['text'])
        self.title.pack(side='left', fill='x', expand=True)
        # P0-D1：权限常驻徽标。权限矩阵在「运行时上下文」里且默认折叠，
        # 过去 perm_badge 建在未 pack 的 status_row 上（探针实测
        # mapped=False）——用户在默认 UI 下看不到"下一个任务将以什么
        # 权限运行"。徽标放标题行，任何时刻可见；点击展开运行时上下文。
        # app._sync_perm_badge 调 set_permission() 同步。
        self._perm_chip = tk.Label(titlebar, text='只看不改', font=FONT_CAPTION,
                                   bg=C['surface2'], fg=C['subtext'],
                                   padx=7, pady=2, cursor='hand2')
        attach_tooltip(self._perm_chip,
                       'Agent 下一个任务能做什么。点击切换；高级选项在菜单底部。')
        self._perm_chip.pack(side='left', padx=(6, 2))
        # 信息密度：简洁只留对话与结果；专业显示底部面板与运行时上下文。
        self._density_chip = tk.Label(titlebar, text='专业', font=FONT_CAPTION,
                                      bg=C['bg'], fg=C['faint'], padx=6, pady=2, cursor='hand2')
        attach_tooltip(self._density_chip, '信息密度：点击在「简洁」与「专业」之间切换')
        self._density_chip.pack(side='left', padx=(2, 2))
        self._density_chip.bind('<Button-1>', lambda _e: self.on_density_toggle and self.on_density_toggle())
        self.on_density_toggle = None
        self._perm_chip.bind('<Button-1>', lambda _e: self._permission_menu())
        # app 注入：on_permission_request(value) 走既有确认流程（permission_changed）。
        self.on_permission_request = None
        self.header_buttons = {}
        for key, icon, label, callback in (
                ('new', 'plus', '新对话', new_conversation),
                ('history', 'clock', '历史', history), ('settings', 'config', '设置', settings),
                ('approvals', 'check', '审批', on_show_approvals)):
            button = IconButton(titlebar, icon=icon, icon_size=ui_px(self, 15), text='', command=callback,
                bg=C['bg'], fg=C['subtext'], bd=0, padx=ui_px(self, 5), pady=ui_px(self, 4),
                takefocus=True, highlightthickness=1, highlightbackground=C['bg'],
                state='normal' if callback else 'disabled', cursor='hand2')
            # 标题行图标：与编辑器工具图标同款「安静」反馈（无描边、无禁用色块）。
            button.set_feedback({'pressed_bg': C['control_pressed'], 'hover_border': C['bg'],
                                 'disabled_bg': C['bg'], 'disabled_border': C['bg']})
            attach_tooltip(button, label)
            button.pack(side='left', padx=1)
            self.header_buttons[key] = button
        # 只读提示条：项目已打开但 Agent 还不能改文件时，用大白话给出一键授权入口。
        self._perm_hint = tk.Frame(self, bg=C['accent_soft'], padx=ui_px(self, 12),
                                   pady=ui_px(self, 6))
        tk.Label(self._perm_hint, text='Agent 现在只能看、不能改文件', font=FONT_CAPTION,
                 bg=C['accent_soft'], fg=C['accent_text']).pack(side='left')
        allow = tk.Label(self._perm_hint, text='允许修改这个项目', font=FONT_CAPTION,
                         bg=C['accent'], fg='#FFFFFF', padx=ui_px(self, 8), pady=ui_px(self, 2),
                         cursor='hand2')
        allow.pack(side='right')
        allow.bind('<Button-1>', lambda _e: self._request_permission('workspace-write'))
        self.perm_hint_button = allow
        self._perm_hint_anchor = titlebar
        # 运行时上下文（权限矩阵 + 模型路由）—— 4-Tab 之上、标题之下
        self._runtime_context = None  # 由 app 调 mount_runtime_context 注入
        # 执行时间线的界面计时：action_id → (开始, 结束)，monotonic 秒。
        self.action_clock = {}
        self._changes_summary = None
        self.notebook = ttk.Notebook(self, style='Coding.TNotebook')
        self.notebook.pack(fill='both', expand=True)
        # Agent 状态条（v0.4 产品化）：4-Tab 上方显示 idle / running / waiting
        # / error 状态、最后动作时间。视觉上是 22px 高的细条，左侧 8px 圆点
        # + 状态文字，右侧最近时间戳。
        self._status_bar = self._build_status_bar()
        # 4 个 Tab：对话 / 任务 / 子 Agent / 完整事件
        conversation, tasks, agents, events = (ttk.Frame(self.notebook) for _ in range(4))
        self.tasks_page = tasks
        for frame, label in zip((conversation, tasks, agents, events),
                                ('对话', '任务', '子 Agent', '完整事件')):
            self.notebook.add(frame, text=label)
        self.chat = Conversation(conversation, open_file, history_source)
        sb = ttk.Scrollbar(conversation, command=self.chat.yview)
        sb.pack(side='right', fill='y')
        self.chat.configure(yscrollcommand=sb.set)
        self.chat.pack(fill='both', expand=True)
        self.tasks_toolbar = ttk.Frame(tasks)
        self.tasks_toolbar.pack(side='bottom', fill='x')
        task_body = ttk.Frame(tasks)
        task_body.pack(fill='both', expand=True)
        self.workflow = readonly(task_body)
        for tag, token in (('pending', 'subtext'), ('running', 'warn'), ('completed', 'ok')):
            self.workflow.tag_configure(tag, foreground=C[token])
        tools = ttk.Frame(agents)
        tools.pack(fill='x')
        dev_button(tools, '模型配置', lambda: configure_model(self.selected_name()),
                   kind='ghost').pack(side='left')
        dev_button(tools, '核实 Git 版本', refresh, kind='ghost').pack(side='right')
        self.tree = ttk.Treeview(agents, show='tree headings', columns=('state',), height=3, style='Coding.Treeview')
        self.tree.heading('#0', text='Agent')
        self.tree.heading('state', text='执行状态')
        self.tree.column('#0', width=180)
        self.tree.column('state', width=100)
        self.tree.pack(fill='x')
        detail = ttk.Frame(agents)
        detail.pack(fill='both', expand=True)
        self.detail = readonly(detail)
        self.detail.configure(height=3)
        self.tree.bind('<<TreeviewSelect>>', self.show_agent)
        # 完整事件 Tab：与底部 mount_events 共享同一 AgentEvents 实例
        # （避免重复构建），但通过这里的 notebook 也直接可见。
        self._events_holder = events
        self.rows = {}
        self._evidence = None
        self._summary = None
        self._cards = {}
        self._callbacks = dict(view=view_diff, open_file=open_file, revert=revert, pending=pending)
        self._last_task = None
        self.events = None
        self.task_context = {}
        self._planning_card = None
        self._view_changes = view_changes

    def mount_runtime_context(self, **kwargs):
        """app 注入 RuntimeContext 组件（权限矩阵 + 模型路由）。"""
        if self._runtime_context is not None:
            self._runtime_context.destroy()
        from .policy_widgets import RuntimeContext
        self._runtime_context = RuntimeContext(self, **kwargs)
        # 插在 titlebar 与 notebook 之间
        self._runtime_context.pack(fill='x', before=self.notebook)
        if getattr(self, '_density', 'pro') == 'simple':
            self._runtime_context.pack_forget()
        # 窄窗契约（1200x760）：默认收起，展开由用户点击——避免 chrome
        # 高度把 P0-L1 保护的底部固定组挤出栏底（实测溢出 4px）。
        if hasattr(self._runtime_context, '_toggle'):
            try:
                self._runtime_context._toggle()
            except Exception:
                pass
        return self._runtime_context

    # ── 权限徽标（P0-D1）──────────────────────────────────────
    # 设计稿：权限用大白话（只看不改 / 可改项目文件），颜色语义不变。
    _PERM_TONES = {
        'read-only': ('只看不改', 'surface2', 'subtext'),
        'workspace-write': ('可改项目文件', 'ok_soft', 'ok'),
        'danger-full-access': ('完全访问', 'warn_soft', 'warn'),
    }

    def _request_permission(self, value):
        if self.on_permission_request is not None:
            self.on_permission_request(value)

    def _permission_menu(self):
        """点击权限徽标：两档大白话 + 不支持的完全访问（置灰）+ 高级矩阵。"""
        if self.on_permission_request is None:
            self._expand_runtime_context()
            return
        menu = tk.Menu(self, tearoff=False, bg=C['surface'], fg=C['text'],
                       activebackground=C['accent_soft'], activeforeground=C['text'],
                       disabledforeground=C['faint'])
        menu.add_command(label='只看不改 —— 只读文件、回答问题',
                         command=lambda: self._request_permission('read-only'))
        menu.add_command(label='可改项目文件 —— 能改这个项目、跑测试（需确认）',
                         command=lambda: self._request_permission('workspace-write'))
        menu.add_command(label='完全访问 —— Coding 暂不支持', state='disabled')
        menu.add_separator()
        menu.add_command(label='高级：权限矩阵 / 模型路由…', command=self._expand_runtime_context)
        chip = self._perm_chip
        try:
            menu.tk_popup(chip.winfo_rootx(), chip.winfo_rooty() + chip.winfo_height())
        finally:
            menu.grab_release()

    def set_density(self, density):
        """简洁：收起运行时上下文条；专业：恢复。"""
        simple = density == 'simple'
        self._density = density
        self._density_chip.configure(text='简洁' if simple else '专业',
                                     fg=C['accent2'] if simple else C['faint'])
        ctx = self._runtime_context
        if ctx is None:
            return
        try:
            if simple and ctx.winfo_manager():
                ctx.pack_forget()
            elif not simple and not ctx.winfo_manager():
                ctx.pack(fill='x', before=self.notebook)
        except tk.TclError:
            pass

    def note_event(self, event):
        """记录工具步骤的到达时刻（运行时事件没有时间戳，耗时为界面测得的约值）。"""
        import time
        action = event.get('action_id')
        kind = event.get('type')
        if not action or kind not in {'tool_started', 'tool_completed'}:
            return
        started, finished = self.action_clock.get(action, (None, None))
        now = time.monotonic()
        if kind == 'tool_started' and started is None:
            started = now
        elif kind == 'tool_completed':
            started, finished = (started if started is not None else now), now
        self.action_clock[action] = (started, finished)

    def set_permission_hint(self, visible):
        """只读且项目已打开时显示一键授权提示条。"""
        hint = self._perm_hint
        try:
            if visible and not hint.winfo_manager():
                hint.pack(fill='x', after=self._perm_hint_anchor, pady=(ui_px(self, 6), 0))
            elif not visible and hint.winfo_manager():
                hint.pack_forget()
        except tk.TclError:
            pass

    def set_permission(self, sandbox):
        """更新标题行权限徽标（app._sync_perm_badge 调用）。

        颜色走语义 token：只读=中性灰、工作区可写=ok 绿、危险全访问=
        warn 橙——高于只读时必须"看起来不一样"，否则等于没有提示。
        """
        text, bg_token, fg_token = self._PERM_TONES.get(
            sandbox, (str(sandbox), 'surface2', 'subtext'))
        try:
            self._perm_chip.configure(text=text, bg=C[bg_token], fg=C[fg_token])
        except (tk.TclError, KeyError):
            pass

    def _expand_runtime_context(self):
        """点击权限徽标 → 展开被折叠的运行时上下文（若可用）。"""
        ctx = self._runtime_context
        if ctx is not None and getattr(ctx, '_collapsed', False):
            try:
                ctx._toggle()
            except Exception:
                pass

    def mount_events(self, notebook):
        """WorkBuddy container seam: attach once without rebuilding its layout.

        同时把同一个 AgentEvents 实例挂到 AgentPanel 的「完整事件」Tab
        （line 360 的 self._events_holder）—— 这之前是塞到底部 Notebook 的
        「变更事件」Tab，v0.2 起统一到本面板内。
        """
        if self.events is None:
            self.events = AgentEvents(self._events_holder)
            self._events_holder.notebook_inner = self._events_holder  # placeholder
            self.events.pack(fill='both', expand=True, in_=self._events_holder)
        return self.events

    def selected_name(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0], {}).get('agent') if selected else None

    # ── Agent 状态条（v0.4 产品化收紧）─────────────────────
    # 22px 高的细条：左侧 8px 状态点 + 状态文字，右侧最近动作时间戳。
    # 状态点用 fg 而不是 fill——和现有 ActivityGlyph 风格一致，圆点用 create_oval。
    # 颜色 token 复用 status_bar 的 idle / running / waiting / error 语义。
    def _build_status_bar(self):
        # 重设计：状态条去掉 surface2 色块，作为标题下方的一行副标题。
        bar = tk.Frame(self, bg=C['bg'], height=ui_px(self, 22))
        bar.pack_propagate(False)
        bar.pack(fill='x', before=self.notebook)
        # 左侧：状态点
        self._status_dot = tk.Canvas(bar, width=10, height=10, bg=C['bg'],
                                       highlightthickness=0, bd=0)
        self._status_dot.pack(side='left', padx=(ui_px(self, 12), ui_px(self, 4)))
        # 左侧：状态文字
        self._status_text = tk.Label(bar, text='就绪 · 等待任务', font=FONT_CAPTION,
                                    bg=C['bg'], fg=C['subtext'], anchor='w')
        self._status_text.pack(side='left', padx=(0, 8))
        # 右侧：最近动作时间
        self._status_last = tk.Label(bar, text='', font=FONT_CAPTION,
                                     bg=C['bg'], fg=C['ter'], anchor='e')
        self._status_last.pack(side='right', padx=(0, ui_px(self, 12)))
        # 初始化为 idle
        self.set_status_state('idle', '就绪 · 等待任务')
        return bar

    def set_status_state(self, state, text, last_action=None):
        """Agent 状态条更新。

        state ∈ {'idle', 'running', 'waiting', 'error'} —— 决定状态点颜色
        与文案前缀。last_action 可选（人类可读时间字符串）。
        """
        # 颜色 token（与 status_bar 内 StatusBar._sync_ready_status 一致）
        tokens = {
            'idle':    C['ter'],     # 灰
            'running': C['warn'],    # 橙
            'waiting': C['accent'],  # 蓝
            'error':   C['error'],   # 红
        }
        token = tokens.get(state, C['ter'])
        # 状态点（重画）
        try:
            self._status_dot.delete('dot')
            self._status_dot.create_oval(1, 1, 9, 9, outline='', fill=token, tags='dot')
        except tk.TclError:
            pass
        try:
            self._status_text.configure(text=text or '')
        except tk.TclError:
            pass
        if last_action is not None:
            try:
                self._status_last.configure(text=last_action)
            except tk.TclError:
                pass

    def show_agent(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return
        row = self.rows.get(selected[0], {})
        name, run = row.get('agent', ''), row.get('run_id', '')
        model = row.get('model') or row.get('selected_model') or '继承宿主路由；尚无 usage 证据'
        actions = [r for r in self._evidence.actions.values() if r.get('agent', 'forge') == name and r.get('run_id', '') == run]
        replace(self.detail, '\n'.join([
            '名称：' + name, '运行：' + run, '任务：' + row.get('task_id', ''),
            '任务内容：' + str(row.get('task', '')), '模型：' + str(model),
            '配置来源（当前运行）：' + str(row.get('model_source', '无来源证据')),
            '状态：' + str(row.get('status', 'unknown')),
            '工具权限上限：' + str(row.get('permission', '未提供；不推断授权')),
            '已暴露工具（仍逐动作授权）：' + ', '.join(row.get('tool_surface', [])),
            '执行历史：', *[f"{r.get('tool', '')} · {r['status']} · {r.get('authorization', '')}" for r in actions[-50:]],
            '输出：' + str(row.get('output', ''))]))

    def render(self, evidence):
        self._evidence = evidence
        lines = ['任务：' + (evidence.task_id or '尚未创建'), '任务状态：' + evidence.phase,
                 '', '计划（未提供阶段事件时不推断完成）：']
        lines += ['☐ ' + str(task) for task in evidence.plan]
        marks = {'running': '◐', 'completed': '☑', 'not_started': '☐', 'awaiting_approval': '☐'}
        lines += ['', '真实工具步骤：'] + [f"{marks.get(r['status'], '!')} {r.get('tool', '')} · {r['status']}" for r in list(evidence.actions.values())[-100:]]
        lines += ['', '测试：'] + [f"{r['status']} · {r.get('command', '')} · 退出码 {r.get('exit_code', '未提供')}" for r in evidence.tests[-20:]]
        lines += ['', f'工作区 Diff 快照：{len(evidence.diffs)}（可能包含其他来源）']
        replace(self.workflow, '\n'.join(lines))
        for number, line in enumerate(lines, 1):
            tag = {'☐': 'pending', '◐': 'running', '☑': 'completed'}.get(line[:1])
            if tag:
                self.workflow.tag_add(tag, f'{number}.0', f'{number}.end')
        if self._last_task != evidence.task_id:
            self._summary = None
            self._changes_summary = None
            self._last_task = evidence.task_id
            self._planning_card = None
        if evidence.task_id or evidence.plan:
            if self._planning_card is None or not self._planning_card.winfo_exists():
                callback = (lambda e=evidence: self._view_changes(e)) if self._view_changes else None
                self._planning_card = PlanningCard(self.chat, callback)
                self.chat.card(self._planning_card)
            # Configuration events are frozen task evidence, never the next-task controls.
            context = dict(self.task_context)
            main = next((c for c in evidence.configurations.values() if c.get('role') in {'default', 'complex', 'simple'}), {})
            if main.get('permission'):
                context['permission'] = main['permission']
            self._planning_card.update_plan(evidence, context)
        if evidence.actions:
            if self._summary is None or not self._summary.winfo_exists():
                self._summary = ToolSummary(self.chat)
                self.chat.card(self._summary)
            tokens = sum(int(r.get('tokens') or 0) for r in evidence.runs.values())
            cost = format_cost(*estimate_cost(evidence.runs.values()))
            self._summary.update_actions(list(evidence.actions.values()), tokens=tokens,
                                         clock=self.action_clock, cost=cost)
        if evidence.changes:
            if self._changes_summary is None or not self._changes_summary.winfo_exists():
                review_all = (lambda e=evidence: self._view_changes(e)) if self._view_changes else None
                self._changes_summary = ChangesSummary(self.chat, view=self._callbacks['view'],
                                                       review_all=review_all)
                self.chat.card(self._changes_summary)
            self._changes_summary.update_changes(list(evidence.changes.values()))
        for key, change in evidence.changes.items():
            card = self._cards.get(key)
            if card is None or not card.winfo_exists():
                card = DiffCard(self.chat, change, **self._callbacks)
                self.chat.card(card)
                self._cards[key] = card
            elif card.change != change:
                card.update_change(change)
        selection = self.tree.selection()
        # Stable IDs come from full name + run identity, not display names.
        import hashlib
        keys = {key: hashlib.sha256(repr(key).encode()).hexdigest() for key in evidence.runs}
        desired = set(keys.values())
        for identifier in list(self.rows):
            if identifier not in desired:
                if self.tree.exists(identifier):
                    self.tree.delete(identifier)
                self.rows.pop(identifier)
        for key, row in sorted(evidence.runs.items(), key=lambda item: item[1].get('depth', 0)):
            identifier = keys[key]
            parent = next((keys[k] for k, r in evidence.runs.items() if r.get('run_id') == row.get('parent_run_id') and k != key), '')
            if parent and not self.tree.exists(parent):
                parent = ''
            values = (row.get('status', 'unknown'),)
            if not self.tree.exists(identifier):
                self.tree.insert(parent, 'end', iid=identifier, text=row.get('agent', ''), values=values, open=True)
            else:
                self.tree.item(identifier, text=row.get('agent', ''), values=values)
                if self.tree.parent(identifier) != parent:
                    self.tree.move(identifier, parent, 'end')
            self.rows[identifier] = row
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection)
        elif self.rows:
            self.tree.selection_set(next(iter(self.rows)))
        self.show_agent()
