"""Small Tk host for a real, separately owned Chromium window."""
import threading
import tkinter as tk

from forge.browser import BrowserManager
from forge.browser_tools import BrowserLane, BrowserCancellation
from forge.config import Config
from forge.policy import Policy
from gui_theme import C, FONT_SMALL, pill_button


class BrowserPanel:
    def __init__(self, app):
        self.app = app
        self.manager = BrowserManager()
        self.window = None
        self.current = None
        self.key = None
        self.granted = {}
        self.busy = False
        self.closed = False

    def _key(self):
        return (str(self.app._active_workspace().resolve()),
                self.app._workspace_mode.value, self.app._session_id)

    def open(self):
        key = self._key()
        if self.window is not None and self.window.winfo_exists():
            if self.busy:
                self.window.lift()
                return
            self.window.destroy()
        self.key = key
        notice = ''
        try:
            self.current = self.manager.session(*key)
        except RuntimeError as exc:
            # Keep cleanup controls accessible even when all four slots are full.
            self.current = None
            notice = str(exc) + '；可关闭全部浏览器后重试。'
        window = self.window = tk.Toplevel(self.app.root)
        window.title('Forge Chromium 浏览器')
        window.geometry('700x250')
        window.configure(bg=C['bg'])
        tk.Label(window, text='应用自带 Chromium · 独立窗口与会话', bg=C['bg'], fg=C['text'],
                 font=FONT_SMALL).pack(anchor='w', padx=16, pady=(16, 8))
        self.url = tk.StringVar(value='about:blank')
        entry = tk.Entry(window, textvariable=self.url, bg=C['surface'], fg=C['text'], insertbackground=C['text'])
        entry.pack(fill='x', padx=16)
        entry.bind('<Return>', lambda _: self.navigate())
        self.enabled = tk.BooleanVar(value=key in self.granted)
        tk.Checkbutton(window, text='允许本会话的 Work Agent 读取网页；导航、点击、输入逐次确认',
            variable=self.enabled, command=self._grant, bg=C['bg'], fg=C['text'], selectcolor=C['surface']).pack(anchor='w', padx=12, pady=10)
        bar = tk.Frame(window, bg=C['bg']); bar.pack(fill='x', padx=16)
        pill_button(bar, '打开网页', self.navigate, kind='primary').pack(side='left')
        pill_button(bar, '关闭当前浏览器并撤销授权', self.revoke).pack(side='left', padx=8)
        pill_button(bar, '关闭全部浏览器', self.close_all).pack(side='left')
        self.status = tk.StringVar(value=notice or f'{key[1]} · {key[0]} · 登录资料在关闭浏览器后清除')
        tk.Label(window, textvariable=self.status, bg=C['bg'], fg=C['subtext'], wraplength=610,
                 justify='left', font=FONT_SMALL).pack(anchor='w', padx=16, pady=10)

    def _grant(self):
        if self.enabled.get() and self.key != self._key():
            self.enabled.set(False)
            self.status.set('项目、模式或聊天已切换，请重新打开浏览器面板后授权')
            return
        if self.enabled.get():
            self.granted.setdefault(self.key, threading.Event())
        else:
            token = self.granted.pop(self.key, None)
            if token is not None:
                token.set()

    def navigate(self):
        if self.busy or self.closed:
            return
        if self.key != self._key():
            self.status.set('项目、模式或聊天已切换，请重新打开浏览器面板')
            return
        try:
            # A new explicit click may create a new session; never retry an action.
            self.current = self.manager.session(*self.key)
        except RuntimeError as exc:
            self._done(str(exc) + '；请稍后重试，或关闭全部浏览器。')
            return
        session, url = self.current, self.url.get().strip()
        self.busy = True
        self.status.set('正在启动 Chromium / 加载网页…')
        def worker():
            try:
                result = session.call('open', {'url': url})
                message = '网页已在 Forge Chromium 窗口打开' if result.ok else result.error
            except Exception as exc:
                message = str(exc)
            self.app._post_ui(self._done, message)
        threading.Thread(target=worker, name='Forge-Browser-Open', daemon=True).start()

    def _done(self, message):
        self.busy = False
        if self.window is not None and self.window.winfo_exists():
            self.status.set(message)
        self.app._set_status(message)

    def revoke(self):
        token = self.granted.pop(self.key, None)
        if token is not None:
            token.set()
        self.enabled.set(False)
        if self.current is not None:
            self.current.close(wait=False)
        self.status.set('已撤销授权，浏览器正在关闭；关闭完成后可再次点击打开网页')

    def close_all(self):
        if self.busy:
            return
        self.busy = True
        self.enabled.set(False)
        for token in self.granted.values():
            token.set()
        self.granted.clear()
        self.status.set('正在关闭所有项目与聊天的浏览器…')
        manager = self.manager
        def worker():
            closed = manager.close()
            self.app._post_ui(self._reset_done, closed)
        threading.Thread(target=worker, name='Forge-Browser-Close', daemon=True).start()

    def _reset_done(self, closed):
        if self.closed:
            return
        if closed:
            self.manager = BrowserManager()
            self.current = None
        self._done('全部浏览器已关闭' if closed else '浏览器尚未退出，请稍后重试关闭')

    def lane(self, *, workspace, rows, scope, cancel):
        key = self._key()
        if key not in self.granted or key[1] != 'work' or str(workspace.resolve()) != key[0]:
            return None
        config = Config(); config.apply_patch(rows)
        session = self.manager.session(*key)
        def approve(name, args, token):
            return key in self.granted and not session.revoked.is_set() and self.app.desktop_features.confirm(name, args, token)
        lane = BrowserLane(session, home=self.app.home, workspace=workspace,
            policy=Policy.from_config(config, workspace=workspace, non_interactive=True),
            scope=scope, cancel=BrowserCancellation(cancel, self.granted[key]), approve=approve)
        return lane

    def close(self):
        self.closed = True
        for token in self.granted.values():
            token.set()
        self.granted.clear()
        return self.manager.close()
