"""Opt-in Tk components. No mounting changes to either production workspace."""
import tkinter as tk

import gui_theme as theme
from ui_icons import IconCanvas
from .contracts import WorkspaceKind, LABELS, NoticeSpec, PermissionSpec
from .tokens import color, font, px


class ModeSwitcher(tk.Frame):
    """One top-level entry. Host acknowledges a request through set_selected()."""
    def __init__(self, parent, on_request, *, selected=WorkspaceKind.WORK):
        super().__init__(parent, bg=color('surface.root'))
        self.on_request = on_request
        self.buttons = {}
        for kind in WorkspaceKind:
            button = theme.control_button(self, text=LABELS[kind], icon='chat' if kind == WorkspaceKind.WORK else 'code',
                font=font('label'), command=lambda k=kind: self.request(k),
                padx=px(self, 10), pady=px(self, 7), takefocus=True,
                icon_size=px(self, 16), highlightthickness=px(self, 1))
            button.pack(side=tk.LEFT, padx=px(self, 2))
            button.bind('<Left>', lambda e, k=kind: self._arrow(k, -1))
            button.bind('<Right>', lambda e, k=kind: self._arrow(k, 1))
            self.buttons[kind] = button
        self.set_selected(selected)

    def request(self, kind):
        kind = WorkspaceKind(kind)
        if kind != self.selected and str(self.buttons[kind].cget('state')) != tk.DISABLED:
            self.on_request(kind)  # No permissions, cancellation or automatic state copying.

    def _arrow(self, kind, direction):
        if str(self.buttons[kind].cget('state')) == tk.DISABLED:
            return 'break'
        kinds = list(WorkspaceKind)
        next_kind = kinds[(kinds.index(kind) + direction) % len(kinds)]
        self.buttons[next_kind].focus_set()
        self.request(next_kind)
        return 'break'

    def set_selected(self, kind):
        self.selected = WorkspaceKind(kind)
        for item, button in self.buttons.items():
            selected = item == self.selected
            # Shared feedback owns hover/pressed/disabled/focus behavior.
            button.configure(bg=color('state.selected') if selected else color('surface.root'),
                             fg=theme.C['accent_text'] if selected else color('text.secondary'))
            theme.button_feedback(button, kind='accent_soft' if selected else 'quiet')


class WorkspaceDeck(tk.Frame):
    """Persistent sibling surfaces. Switching changes visibility, never ownership."""
    def __init__(self, parent, factories):
        super().__init__(parent, bg=color('surface.root'))
        self.factories = {WorkspaceKind(k): v for k, v in factories.items()}
        self.frames = {}
        self.focus_targets = {}
        self.selected = None
        self._focus_root = self.winfo_toplevel()
        self._focus_token = self._focus_root.bind('<FocusIn>', self._remember_focus, add='+')
        self.bind('<Destroy>', self._destroyed, add='+')
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def _remember_focus(self, event):
        for kind, frame in self.frames.items():
            if event.widget == frame or str(event.widget).startswith(str(frame) + '.'):
                self.focus_targets[kind] = event.widget
                break

    def _destroyed(self, event):
        if event.widget == self:
            self._focus_root.unbind('<FocusIn>', self._focus_token)

    def select(self, kind):
        kind = WorkspaceKind(kind)
        if self.selected == kind:
            return self.frames[kind]
        if kind not in self.frames:
            frame = tk.Frame(self, bg=color('surface.root'))
            try:
                self.factories[kind](frame)
            except BaseException:
                frame.destroy()
                raise  # Keep the previously mounted workspace visible.
            self.frames[kind] = frame
        if self.selected is not None:
            previous = self.frames[self.selected]
            focused = self.focus_get()
            if focused is not None and (focused == previous or str(focused).startswith(str(previous) + '.')):
                self.focus_targets[self.selected] = focused
            previous.grid_remove()
        frame = self.frames[kind]
        frame.grid(row=0, column=0, sticky='nsew')
        self.selected = kind
        target = self.focus_targets.get(kind)
        if target is not None and target.winfo_exists():
            target.focus_set()
        return frame


class NoticeBanner(tk.Frame):
    """Shared visual notification. Permission decisions remain with the host."""
    def __init__(self, parent, notice: NoticeSpec, *, on_open=None):
        super().__init__(parent, bg=color('surface.panel'), padx=px(parent, 12), pady=px(parent, 8),
                         highlightthickness=1, highlightbackground=color('border.default'))
        self.notice = notice
        icon = {'info': 'info', 'ok': 'check_circle', 'warn': 'warning', 'error': 'error'}[notice.tone]
        IconCanvas(self, icon, size=px(self, 16), bg=color('surface.panel'), fg=theme.C[notice.tone]).pack(side=tk.LEFT, padx=(0, px(self, 8)))
        text = f'{LABELS[notice.workspace]} · {notice.title}\n{notice.detail}'
        label = tk.Label(self, text=text, bg=color('surface.panel'), fg=color('text.body'),
                         font=font('label'), anchor='w', justify=tk.LEFT)
        label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.bind('<Configure>', lambda e: label.configure(wraplength=max(px(self, 80), e.width - px(self, 160))))
        if on_open is not None:
            theme.control_button(self, text='查看', command=lambda: on_open(notice),
                                 font=font('label'), takefocus=True, pady=px(self, 7),
                                 padx=px(self, 12)).pack(side=tk.RIGHT)


class PermissionPrompt(tk.Frame):
    """Explicit decision intents. Only Runtime can validate and apply a grant."""
    def __init__(self, parent, request: PermissionSpec, on_decision):
        super().__init__(parent, bg=color('surface.panel'))
        self.request = request
        self.on_decision = on_decision
        detail = (f'操作：{request.operation}\n项目：{request.project or "不适用"}\n范围：{request.target}\n'
                  f'任务：{request.task_id or "手动操作"}\n请求：{request.request_id}')
        NoticeBanner(self, NoticeSpec('warn', '需要明确批准', detail, request.workspace,
                                     request.task_id)).pack(fill=tk.X)
        actions = tk.Frame(self, bg=color('surface.panel'), padx=px(self, 12), pady=px(self, 8))
        actions.pack(fill=tk.X)
        self.buttons = {}
        for intent, text in [('allow_once', '允许本次'), ('deny', '拒绝')]:
            button = theme.control_button(actions, text=text, font=font('label'),
                command=lambda value=intent: self.on_decision(self.request, value),
                bg=color('action.primary') if intent == 'allow_once' else color('surface.panel'),
                fg=color('action.primary-text') if intent == 'allow_once' else color('text.body'),
                takefocus=True, padx=px(self, 12), pady=px(self, 7))
            button.pack(side=tk.RIGHT, padx=px(self, 4))
            self.buttons[intent] = button

    def set_pending(self, pending):
        for button in self.buttons.values():
            button.configure(state=tk.NORMAL if pending else tk.DISABLED)
