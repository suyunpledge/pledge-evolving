"""Coding 工作台键位表：动作 → 键位 → 显示文案 → Tk 绑定序列的唯一真源。

纯逻辑模块，**不 import tkinter**——这样排序/格式化能在无窗口环境里
单测（本仓库的 GUI 测试需要真实窗口，CI 上跑不了）。

为什么需要这张表：改版前键位散在三处（app 的两个 bind、编辑器组件
里的 Ctrl+S、输入框里的 Enter/Ctrl+Enter），文案又是各写各的——
欢迎页曾列出「Ctrl+Shift+P 命令面板」「Ctrl+B 切换侧栏」两个从未
绑定过的键。用户按下去毫无反应，这是"毛坯感"最典型的来源：
提示与行为对不上。

约定（对齐 VS Code / JetBrains 等成熟产品）：

* 用 ``Mod`` 表示"平台主修饰键"——Windows/Linux 是 Ctrl，macOS 是 ⌘；
* 键位字符串用 ``+`` 连接、单字母大写：``Mod+Shift+P``；
* 显示文案不额外加空格（``Ctrl+P``，macOS 为 ``⌘P``）；
* 一个动作只占一组键，重复注册在测试里直接判失败。
"""
import sys
from dataclasses import dataclass

IS_MAC = sys.platform == 'darwin'

#: 平台主修饰键的 Tk 名字。
MOD_TK = 'Command' if IS_MAC else 'Control'

_MAC_SYMBOLS = {'Mod': '⌘', 'Shift': '⇧', 'Alt': '⌥', 'Control': '⌃',
                'Return': '↩', 'Escape': 'esc', 'Tab': '⇥', 'BackSpace': '⌫'}

_PC_NAMES = {'Mod': 'Ctrl', 'Control': 'Ctrl', 'Return': 'Enter', 'Escape': 'Esc'}

# 修饰键的书写顺序（与系统菜单/快捷键说明一致，不按用户输入顺序）。
# macOS 是 ⌃⌥⇧⌘，Windows 是 Ctrl+Alt+Shift——顺序反了会显得业余：
# 所有人都写 ⇧⌘P，没人写 ⌘⇧P。
_MAC_ORDER = {'Control': 0, 'Alt': 1, 'Shift': 2, 'Mod': 3}
_PC_ORDER = {'Mod': 0, 'Control': 1, 'Alt': 2, 'Shift': 3}
_MODIFIERS = ('Mod', 'Control', 'Alt', 'Shift')


@dataclass(frozen=True)
class Shortcut:
    """一条快捷键定义。``keys`` 是规范写法（如 ``Mod+Shift+E``）。"""

    key: str          # 动作 id（handler 注册用）
    label: str        # 动作名（UI 文案 / 提示）
    keys: str         # 规范键位
    group: str = '通用'
    scope: str = 'global'   # 'global' = 挂 toplevel；'local' = 控件内部自己绑

    @property
    def display(self):
        return display(self.keys)

    @property
    def sequence(self):
        return tk_sequence(self.keys)

    @property
    def tooltip(self):
        return f'{self.label}（{self.display}）'


SHORTCUTS = (
    Shortcut('command_palette', '命令面板', 'Mod+Shift+P', '导航'),
    Shortcut('quick_open', '快速打开文件', 'Mod+P', '导航'),
    Shortcut('save', '保存当前文档', 'Mod+S', '编辑'),
    Shortcut('save_as', '另存为', 'Mod+Shift+S', '编辑'),
    Shortcut('close_tab', '关闭当前标签', 'Mod+W', '编辑'),
    Shortcut('next_tab', '下一个标签', 'Control+Tab', '编辑'),
    Shortcut('prev_tab', '上一个标签', 'Control+Shift+Tab', '编辑'),
    Shortcut('focus_explorer', '聚焦文件树', 'Mod+Shift+E', '导航'),
    # 这里只有「按文件名过滤」这一种搜索（内容搜索未接入），所以键位
    # 不冒充 VS Code 的 Ctrl+Shift+F，用 Ctrl+F——但仅在编辑器没有
    # 抢走这个键位时才生效（编辑器内部的 Ctrl+F 是查找栏）。
    Shortcut('focus_search', '搜索文件名', 'Mod+F', '导航'),
    Shortcut('run_tests', '运行测试', 'F5', '任务'),
    # 输入框内部键位：不进全局绑定（scope='local'），但文案仍从本表
    # 派生——否则输入区又会出现一套手写的 'Shift+Enter' 字符串。
    Shortcut('send', '发送任务', 'Return', '输入', 'local'),
    Shortcut('newline', '换行', 'Shift+Return', '输入', 'local'),
    Shortcut('send_force', '直接执行', 'Mod+Return', '输入', 'local'),
)

BY_KEY = {item.key: item for item in SHORTCUTS}


def _tokens(keys):
    return [part for part in str(keys).split('+') if part]


def display(keys, *, mac=None):
    """键位 → 人读文案（``Mod+Shift+P`` → ``Ctrl+Shift+P`` / ``⇧⌘P``）。"""
    mac = IS_MAC if mac is None else mac
    tokens = _tokens(keys)
    modifiers = [token for token in tokens if token in _MODIFIERS]
    base = [token for token in tokens if token not in _MODIFIERS]
    modifiers.sort(key=(_MAC_ORDER if mac else _PC_ORDER).__getitem__)
    ordered = modifiers + base
    if mac:
        return ''.join(_MAC_SYMBOLS.get(token, token.upper() if len(token) == 1 else token)
                       for token in ordered)
    return '+'.join(_PC_NAMES.get(token, token.upper() if len(token) == 1 else token)
                    for token in ordered)


def tk_sequence(keys, *, mac=None):
    """键位 → Tk 绑定序列（``Mod+P`` → ``<Control-P>`` / ``<Command-P>``）。"""
    mac = IS_MAC if mac is None else mac
    tokens = _tokens(keys)
    shifted = 'Shift' in tokens
    # Tk 的字母 keysym 区分大小写：单按 Ctrl+P 产生的是 ``p``，只有按住
    # Shift（或开着大写锁定）才是 ``P``。绑 ``<Control-P>`` 会让 Ctrl+P
    # 毫无反应、反而被 Ctrl+Shift+P 触发——字母必须按有无 Shift 定大小写。
    parts = [('Command' if mac else 'Control') if token == 'Mod' else
             (token.upper() if shifted else token.lower()) if len(token) == 1 and token.isalpha()
             else token
             for token in tokens]
    return '<' + '-'.join(parts) + '>'


def get(key):
    """按动作 id 取定义（未知返回 None，不抛异常——调用点多为 UI 兜底）。"""
    return BY_KEY.get(key)


def hint(key, default=''):
    """动作的键位显示文案；动作不存在时返回 ``default``。"""
    item = BY_KEY.get(key)
    return item.display if item is not None else default


def tooltip(key, action=None):
    """动作的 tooltip 文案：``保存当前文档（Ctrl+S）``。"""
    item = BY_KEY.get(key)
    if item is None:
        return action or ''
    return f'{action or item.label}（{item.display}）'


def bindings():
    """全部需要挂到 toplevel 的 (Tk 序列, 动作 id)。"""
    return tuple((item.sequence, item.key) for item in SHORTCUTS
                 if item.scope == 'global')


def groups():
    """按分组聚合（未来命令面板 / 快捷键说明页的数据源）。"""
    result = {}
    for item in SHORTCUTS:
        result.setdefault(item.group, []).append(item)
    return result
