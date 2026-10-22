"""Cooperating Forge processes: OS advisory locks, never scan-as-lock.

Lock files live outside the project. The OS releases them after process death.
Nested acquisition in one thread is supported (a tool may spawn another agent).
Uncooperating editors are detected by revision checks, not locked by this API.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time

_mutex = threading.RLock()
_locks = {}
_local = threading.local()


@contextmanager
def resource_lock(kind, path):
    key = kind + ':' + os.path.normcase(str(Path(path).resolve()))
    with _mutex:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, 'held', None)
        if held is None:
            held = _local.held = set()
        if key in held:
            yield
            return
        root = Path(tempfile.gettempdir()) / 'forge-runtime-locks'
        root.mkdir(exist_ok=True)
        target = root / (hashlib.sha256(key.encode()).hexdigest() + '.lock')
        with target.open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {13, 11, 36}:
                            raise
                        time.sleep(.01)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            held.add(key)
            try:
                # Windows byte-range locks also cover bytes beyond EOF. Take
                # the lock before initializing: two first openers may both see
                # an empty file, then one writes into the other's locked byte.
                if handle.seek(0, 2) == 0:
                    handle.write(b'\0')
                    handle.flush()
                yield
            finally:
                held.remove(key)
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
