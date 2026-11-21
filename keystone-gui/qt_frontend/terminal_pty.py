"""A project shell attached to an OS pseudoterminal, owned by one terminal worker."""
import codecs
from collections import deque
import os
from pathlib import Path
import signal
import subprocess

import pyte


class TerminalScreen(pyte.Screen):
    """Send newly scrolled lines once and replace only the live viewport."""

    def __init__(self, columns, rows):
        self.scrolled = deque(maxlen=4096)
        super().__init__(columns, rows)

    def line_text(self, row):
        cells = self.buffer[row]
        return ''.join(cells[i].data for i in range(max(cells, default=-1) + 1)).rstrip()

    def index(self):
        if self.margins is None and self.cursor.y == self.lines - 1:
            self.scrolled.append(self.line_text(0) + '\n')
        super().index()

    def frame(self):
        history = ''.join(self.scrolled)[-131072:]
        self.scrolled.clear()
        screen = '\n'.join(self.line_text(y) for y in range(self.lines)).rstrip('\n')
        return history, screen


class ProjectPty:
    def __init__(self, root, size):
        self.screen = TerminalScreen(*size)
        self.stream = pyte.Stream(self.screen)
        # Keep desktop credentials and the GUI's Python configuration out of child processes.
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR',
                   'HOME', 'USERPROFILE', 'LANG', 'LC_CTYPE', 'PATHEXT'}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env.update(TERM='xterm-256color', PYTHONIOENCODING='utf-8')
        if os.name == 'nt':
            from winpty import Backend, PTY
            shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            self.process = PTY(*size, backend=Backend.ConPTY)
            # Qt edits the submitted line. Let the console handle subsequent program input.
            args = ' -NoLogo -NoProfile -NoExit -Command "Remove-Module PSReadLine -ErrorAction SilentlyContinue"'
            environment = '\0'.join(f'{k}={v}' for k, v in sorted(env.items())) + '\0'
            self.process.spawn(str(shell), cmdline=args, cwd=str(root), env=environment)
        else:
            from ptyprocess import PtyProcess
            self.process = PtyProcess.spawn(['/bin/sh', '-i'], cwd=str(root), env=env,
                                            dimensions=(size[1], size[0]))
            self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def read(self):
        if os.name == 'nt':
            data = self.process.read(blocking=False)
        else:
            import select
            if not select.select([self.process.fd], [], [], 0)[0]:
                return False
            try:
                data = self.decoder.decode(self.process.read(32768))
            except EOFError:
                data = self.decoder.decode(b'', final=True)
        if data:
            self.stream.feed(data)
        return bool(data)

    def write(self, data):
        if os.name == 'nt':
            self.process.write(data)
        else:
            data = data.encode('utf-8')
            while data:
                data = data[self.process.write(data):]

    def resize(self, columns, rows):
        for row in range(max(0, self.screen.lines - rows)):
            self.screen.scrolled.append(self.screen.line_text(row) + '\n')
        self.screen.resize(lines=rows, columns=columns)
        if os.name == 'nt':
            self.process.set_size(columns, rows)
        else:
            self.process.setwinsize(rows, columns)

    def exit_code(self):
        if self.process.isalive():
            return None
        if os.name == 'nt':
            return self.process.get_exitstatus()
        return self.process.exitstatus if self.process.exitstatus is not None else -self.process.signalstatus

    def close(self):
        if os.name == 'nt':
            if self.process.isalive():
                subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                               creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=5)
        else:
            if self.process.isalive():
                # Interactive children may own a different foreground process group.
                foreground = os.tcgetpgrp(self.process.fd)
                for group in {foreground, self.process.pid}:
                    if group > 0:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            self.process.close(force=True)
