"""Machine resource locks must work across checkouts and allow cancellation."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from forge.runtime_lock import named_resource_lock, LockCancelled


class NamedResourceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity = uuid.uuid4().hex

    def child(self, source, cwd):
        cwd.mkdir()
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        process = subprocess.Popen([sys.executable, '-u', '-c', source, self.identity],
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def close():
            if process.poll() is None: process.kill()
            process.communicate(timeout=5)
        self.addCleanup(close)
        output = queue.Queue()
        def read():
            for line in process.stdout: output.put(line.decode().strip())
        threading.Thread(target=read, daemon=True).start()
        return process, output

    def test_different_working_directories_share_target_and_desktop_locks(self):
        for kind in ('computer-target', 'computer-input'):
            source = f'''
import sys
from forge.runtime_lock import named_resource_lock
print('ready', flush=True)
with named_resource_lock({kind!r},sys.argv[1]):
    print('acquired', flush=True)
'''
            with named_resource_lock(kind, self.identity):
                children = [self.child(source, self.root / (kind + str(i))) for i in range(2)]
                for process, output in children:
                    self.assertEqual(output.get(timeout=5), 'ready')
                    with self.assertRaises(queue.Empty): output.get(timeout=.15)
            for process, output in children:
                self.assertEqual(output.get(timeout=5), 'acquired')
                self.assertEqual(process.wait(timeout=5), 0)

    def test_os_lock_wait_cancels_without_waiting_for_owner(self):
        process, output = self.child('''
import sys
from forge.runtime_lock import named_resource_lock
with named_resource_lock('computer-target',sys.argv[1]):
    print('held',flush=True)
    sys.stdin.readline()
''', self.root / 'holder')
        self.assertEqual(output.get(timeout=5), 'held')
        cancelled, started = threading.Event(), threading.Event()
        results = []
        def contend():
            started.set()
            try:
                with named_resource_lock('computer-target', self.identity, cancelled=cancelled):
                    results.append('entered')
            except LockCancelled: results.append('cancelled')
        worker = threading.Thread(target=contend, daemon=True)
        worker.start(); self.assertTrue(started.wait(2))
        self.assertFalse(threading.Event().wait(.1))
        cancelled.set(); worker.join(2)
        self.assertFalse(worker.is_alive()); self.assertEqual(results, ['cancelled'])
        self.assertIsNone(process.poll())
        process.stdin.write(b'\n'); process.stdin.flush()
        self.assertEqual(process.wait(timeout=5), 0)
        with named_resource_lock('computer-target', self.identity): pass

    def test_nested_acquisition_exception_and_cancel_leave_lock_usable(self):
        token = threading.Event()
        with named_resource_lock('test', self.identity):
            with self.assertRaises(ValueError):
                with named_resource_lock('test', self.identity): raise ValueError('fixture')
            token.set()
            with self.assertRaises(LockCancelled):
                with named_resource_lock('test', self.identity, cancelled=token): pass
        with named_resource_lock('test', self.identity): pass


if __name__ == '__main__': unittest.main()
