"""Owned text-entry target for real keyboard input. JSON stdin/stdout, no user application access."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
import queue
import sys
import threading
import tkinter as tk


def main():
    u=c.WinDLL('user32')
    root=tk.Tk();root.title('revon owned keyboard fixture');root.geometry('420x120+80+80')
    root.attributes('-topmost',True)  # Test fixture only: keystrokes are refused unless this is in front.
    entry=tk.Entry(root,font=('Arial',16),insertofftime=0);entry.pack(fill='x',padx=12,pady=30)
    submitted=[]
    entry.bind('<Return>',lambda _:submitted.append(entry.get()))
    root.update_idletasks()
    u.GetParent.argtypes=[w.HWND];u.GetParent.restype=w.HWND
    hwnd=u.GetParent(root.winfo_id())
    def ready():
        center = [(entry.winfo_x() + entry.winfo_width()/2) / root.winfo_width(),
                  (entry.winfo_y() + entry.winfo_height()/2) / root.winfo_height()]
        print(json.dumps({'window':int(hwnd),'process_id':os.getpid(),'field_center':center}),flush=True)
    root.after(200,ready)
    commands=queue.Queue()
    def read():
        try:
            for line in sys.stdin: commands.put(json.loads(line))
        finally:
            commands.put({'op':'close'})  # A failed/killed test parent cannot leave a window behind.
    threading.Thread(target=read,daemon=True).start()
    def poll():
        while not commands.empty():
            command=commands.get()
            if command['op']=='close': root.destroy();return
            print(json.dumps({'typed':entry.get(),'submitted':submitted}),flush=True)
        root.after(10,poll)
    root.after(10,poll);root.mainloop()


if __name__=='__main__':main()
