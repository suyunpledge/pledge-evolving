import sys
sys.path.insert(0, r'C:\Users\匡溯昀\pledge-evolving\forge-gui\development_mode')
import tempfile
import tkinter as tk
from pathlib import Path
from gui_theme import apply_ttk_theme
from forge.development.state import ModeState
from development_mode.app import DevelopmentWorkbench
import _tkinter, time

t = tempfile.TemporaryDirectory()
base = Path(t.name)
home = base / 'home'
state = ModeState(home)
state.switch('development')
root = tk.Tk()
root.geometry('1200x760')
root.attributes('-alpha', 0)
apply_ttk_theme(root)
app = DevelopmentWorkbench(root, state)
app.pack(fill='both', expand=True)

for _ in range(16):
    if not root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT): break

root.geometry('1200x760')
for _ in range(16):
    if not root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT): break
app.apply_layout()
for _ in range(16):
    if not root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT): break

af_top = app.agent_frame.winfo_rooty() - root.winfo_rooty()
af_h = app.agent_frame.winfo_height()
af_bot = af_top + af_h
print('agent_frame:', 'top=%d' % af_top, 'h=%d' % af_h, 'bottom=%d' % af_bot)
for c in app.agent_frame.winfo_children():
    t = c.winfo_rooty() - root.winfo_rooty()
    h = c.winfo_height()
    pi = c.pack_info()
    print('  %-18s top=%d h=%d bottom=%d expand=%s side=%s' % (
        type(c).__name__, t, h, t + h, pi.get('expand'), pi.get('side')))

# Also check what's inside agent_panel
ap = app.agent_panel
print('\nagent_panel:')
for c in ap.winfo_children():
    t = c.winfo_rooty() - root.winfo_rooty()
    h = c.winfo_height()
    print('  %-18s top=%d h=%d bottom=%d' % (type(c).__name__, t, h, t + h))

root.destroy()
t.cleanup()
