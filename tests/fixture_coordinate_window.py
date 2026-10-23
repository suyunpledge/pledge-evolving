"""Owned custom-drawn target. JSON stdin/stdout, no user application access."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from forge.computer_win32 import WindowsControlDriver


def main():
    import os
    u=c.WinDLL('user32')
    u.SetProcessDpiAwarenessContext.argtypes=[w.HANDLE]
    u.SetProcessDpiAwarenessContext(w.HANDLE(-4 if sys.argv[-1]=='aware' else -1))
    root=tk.Tk();root.title('Forge owned coordinate fixture');root.geometry('480x300+60+60')
    root.attributes('-topmost',True)  # Test fixture only: never click a covered user app.
    canvas=tk.Canvas(root,bg='white',highlightthickness=0);canvas.pack(fill='both',expand=True)
    canvas.create_rectangle(130,80,350,180,fill='#15944a',outline='black',tags='target')
    canvas.create_text(240,130,text='VERIFY',fill='white',font=('Arial',20),tags='label')
    clicks=[]
    def clicked(event):
        clicks.append([event.x,event.y])
        if 130<=event.x<=350 and 80<=event.y<=180:
            canvas.itemconfigure('target',fill='#c42c2c');canvas.itemconfigure('label',text='DONE')
    canvas.bind('<Button-1>',clicked)
    root.update_idletasks()
    u.GetParent.argtypes=[w.HWND];u.GetParent.restype=w.HWND
    hwnd=u.GetParent(root.winfo_id())
    driver=WindowsControlDriver(hwnd,os.getpid())
    u.LogicalToPhysicalPointForPerMonitorDPI.argtypes=[w.HWND,c.POINTER(w.POINT)]
    def ready():
        root.lift();root.focus_force()
        geometry=driver.geometry()
        point=w.POINT(canvas.winfo_rootx()+240,canvas.winfo_rooty()+130)
        u.LogicalToPhysicalPointForPerMonitorDPI(hwnd,c.byref(point))
        print(json.dumps({'window':int(hwnd),'process_id':os.getpid(),
            'x':point.x-geometry['rect'][0],'y':point.y-geometry['rect'][1]}),flush=True)
    root.after(200,ready)
    commands=queue.Queue()
    def read():
        try:
            for line in sys.stdin:
                commands.put(json.loads(line))
        finally:
            commands.put({'op':'close'})  # A failed/killed test parent cannot leave a window behind.
    threading.Thread(target=read,daemon=True).start()
    def poll():
        while not commands.empty():
            command=commands.get()
            if command['op']=='close': root.destroy();return
            if command['op']=='move': root.geometry('+160+160');root.update_idletasks()
            if command['op']=='change': canvas.itemconfigure('label',text='CHANGED');root.update_idletasks()
            if command['op']=='activate':root.lift();root.focus_force()
            print(json.dumps({'clicks':clicks,'text':canvas.itemcget('label','text')}),flush=True)
        root.after(10,poll)
    root.after(10,poll);root.mainloop()


if __name__=='__main__':main()
