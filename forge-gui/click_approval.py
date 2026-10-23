"""Worker data broker and Tk-owned coordinate approval. No worker Tk callbacks."""
import queue
import threading
import time
import tkinter as tk
from gui_theme import C, FONT_SMALL, pill_button


class ClickApprovals:
    def __init__(self):
        self.queue = queue.Queue(maxsize=8)
        self.stop = threading.Event()

    def request(self, target, args, frame, cancel):
        row = {'target':target, 'args':args, 'frame':frame, 'cancel':cancel,
               'done':threading.Event(), 'accepted':False, 'deadline':time.monotonic()+60}
        if self.stop.is_set(): return False
        try: self.queue.put_nowait(row)
        except queue.Full: return False
        while not row['done'].wait(.05):
            if self.stop.is_set() or cancel is not None and cancel.is_set() or time.monotonic()>row['deadline']:
                row['done'].set(); return False
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
            if row is not None and not row['done'].is_set(): self.show(row)
        self.after = self.owner.after(50,self.poll)

    def show(self, row):
        window=tk.Toplevel(self.owner); window.title('确认视觉坐标单击')
        window.configure(bg=C['bg']); window.transient(self.owner)
        args,frame=row['args'],row['frame']
        tk.Label(window,text=f"目标 {row['target']}\n截图像素 ({args['x']}, {args['y']}) · {args['reason']}",
                 bg=C['bg'],fg=C['text'],font=FONT_SMALL,wraplength=700,justify='left').pack(fill='x',padx=12,pady=10)
        factor=max(1,(frame['width']+699)//700,(frame['height']+399)//400)
        image=tk.PhotoImage(master=window,data=frame['png']).subsample(factor,factor)
        canvas=tk.Canvas(window,width=image.width(),height=image.height(),bg=C['surface'],highlightthickness=0)
        canvas.pack(padx=12,pady=6); canvas.image=image
        canvas.create_image(0,0,image=image,anchor='nw')
        x,y=args['x']/factor,args['y']/factor
        canvas.create_oval(x-9,y-9,x+9,y+9,outline=C['error'],width=2)
        canvas.create_line(x-15,y,x+15,y,fill=C['error'],width=2)
        canvas.create_line(x,y-15,x,y+15,fill=C['error'],width=2)
        bar=tk.Frame(window,bg=C['bg']);bar.pack(fill='x',padx=12,pady=10)
        check_after=None
        def resolve(accepted):
            nonlocal check_after
            if check_after is not None: window.after_cancel(check_after);check_after=None
            row['accepted']=accepted;row['done'].set()
            if window.winfo_exists(): window.destroy()
            self.active=None
        pill_button(bar,'确认单击',lambda:resolve(True),kind='primary',bg=C['bg']).pack(side='left',padx=6)
        pill_button(bar,'拒绝',lambda:resolve(False),kind='quiet',bg=C['bg']).pack(side='left',padx=6)
        window.protocol('WM_DELETE_WINDOW',lambda:resolve(False))
        self.active=(window,row,resolve)
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
            try: self.broker.queue.get_nowait()['done'].set()
            except queue.Empty: break
