"""Worker data broker and Tk-owned coordinate approval. No worker Tk callbacks."""
import queue
import threading
import time
import tkinter as tk
from gui_theme import C, FONT_SMALL, pill_button


POINTER_LABELS={'click':'单击','double_click':'双击','right_click':'右键单击',
                'scroll_up':'向上滚动','scroll_down':'向下滚动'}


def describe(args):
    """(window title, summary line, confirm button) for one pending input approval."""
    if 'text' in args:
        text=args['text']
        note='；含换行，每个换行会按一次回车，可能直接发送或提交' if '\n' in text else ''
        return '确认键盘输入', f'输入 {len(text)} 个字符到窗口当前焦点{note}', '确认输入'
    if 'key' in args:
        repeat=args.get('repeat',1)
        combo='+'.join(part.capitalize() if len(part)>1 else part.upper() for part in args['key'].split('+'))
        return '确认按键', f"按键 {combo}" + (f' × {repeat}' if repeat>1 else ''), '确认按键'
    label=POINTER_LABELS.get(args.get('action','click'),'单击')
    if args.get('action','').startswith('scroll'): label+=f" {args.get('amount',3)} 格"
    return f'确认视觉坐标{label}', f"{label} · 截图像素 ({args['x']}, {args['y']})", f'确认{label}'


class ClickApprovals:
    def __init__(self):
        self.queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()

    def resolve(self, row, accepted):
        """First decision wins; an expired/cancelled prompt can never be accepted."""
        with row['lock']:
            if row['done'].is_set(): return
            row['accepted'] = bool(accepted and not self.stop.is_set()
                and not (row['cancel'] is not None and row['cancel'].is_set())
                and time.monotonic() < row['deadline'])
            row['done'].set()

    def request(self, target, args, frame, cancel):
        row = {'target':target, 'args':args, 'frame':frame, 'cancel':cancel,
               'done':threading.Event(), 'lock':threading.Lock(),
               'accepted':False, 'deadline':time.monotonic()+60}
        if self.stop.is_set(): return False
        try: self.queue.put_nowait(row)
        except queue.Full: return False
        while not row['done'].wait(.05):
            if self.stop.is_set() or cancel is not None and cancel.is_set() or time.monotonic()>row['deadline']:
                self.resolve(row,False); return False
        return row['accepted'] and not self.stop.is_set() and not (cancel is not None and cancel.is_set())


class ApprovalView:
    def __init__(self, owner, broker):
        self.owner, self.broker, self.active = owner, broker, None
        self.after = owner.after(50,self.poll)

    def poll(self):
        self.after = None
        if self.broker.stop.is_set(): return
        if self.active is None:
            try: row=self.broker.queue.get_nowait()
            except queue.Empty: row=None
            if row is not None and not row['done'].is_set():
                try: self.show(row)
                except Exception:
                    # Bad image/Tk failure must reject this prompt, not stop the
                    # entire approval pump and strand all subsequent requests.
                    self.broker.resolve(row,False)
                    if self.active: self.active[2](False)
        self.after = self.owner.after(50,self.poll)

    def show(self, row):
        args=row['args']
        title,summary,confirm=describe(args)
        window=tk.Toplevel(self.owner); window.title(title)
        check_after=None
        def resolve(accepted):
            nonlocal check_after
            if check_after is not None: window.after_cancel(check_after);check_after=None
            self.broker.resolve(row,accepted)
            if window.winfo_exists(): window.destroy()
            self.active=None
        self.active=(window,row,resolve)
        window.configure(bg=C['bg']); window.transient(self.owner)
        frame=row['frame']
        tk.Label(window,text=f"目标 {row['target']}\n{summary} · {args['reason']}",
                 bg=C['bg'],fg=C['text'],font=FONT_SMALL,wraplength=700,justify='left').pack(fill='x',padx=12,pady=10)
        if 'text' in args:
            # The exact text, read-only: what is shown is what will be typed.
            box=tk.Text(window,height=min(8,args['text'].count('\n')+2),width=80,wrap='char',
                        bg=C['input_bg'],fg=C['text'],font=FONT_SMALL,relief='flat')
            box.insert('1.0',args['text']); box.configure(state='disabled')
            box.pack(fill='x',padx=12)
        factor=max(1,(frame['width']+699)//700,(frame['height']+399)//400)
        image=tk.PhotoImage(master=window,data=frame['png']).subsample(factor,factor)
        canvas=tk.Canvas(window,width=image.width(),height=image.height(),bg=C['surface'],highlightthickness=0)
        canvas.pack(padx=12,pady=6); canvas.image=image
        canvas.create_image(0,0,image=image,anchor='nw')
        if 'x' in args:
            x,y=args['x']/factor,args['y']/factor
            canvas.create_oval(x-9,y-9,x+9,y+9,outline=C['error'],width=2)
            canvas.create_line(x-15,y,x+15,y,fill=C['error'],width=2)
            canvas.create_line(x,y-15,x,y+15,fill=C['error'],width=2)
        bar=tk.Frame(window,bg=C['bg']);bar.pack(fill='x',padx=12,pady=10)
        pill_button(bar,confirm,lambda:resolve(True),kind='primary',bg=C['bg']).pack(side='left',padx=6)
        pill_button(bar,'拒绝',lambda:resolve(False),kind='quiet',bg=C['bg']).pack(side='left',padx=6)
        window.protocol('WM_DELETE_WINDOW',lambda:resolve(False))
        def check():
            nonlocal check_after
            check_after=None
            if row['done'].is_set() or self.broker.stop.is_set() or (
                    row['cancel'] is not None and row['cancel'].is_set()) or time.monotonic()>row['deadline']:
                resolve(False)
            elif window.winfo_exists(): check_after=window.after(100,check)
        check_after=window.after(100,check)

    def close(self):
        self.broker.stop.set()
        if self.after is not None: self.owner.after_cancel(self.after); self.after=None
        if self.active:
            self.active[2](False)
        while not self.broker.queue.empty():
            try: self.broker.resolve(self.broker.queue.get_nowait(),False)
            except queue.Empty: break
