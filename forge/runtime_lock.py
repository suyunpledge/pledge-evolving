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


class LockCancelled(RuntimeError):
    """Cancelled while waiting; the protected operation has not started."""


@contextmanager
def cancellable_lock(lock, cancelled=None):
    if cancelled is None:
        lock.acquire()
    else:
        while True:
            if cancelled.is_set(): raise LockCancelled('Cancelled while waiting for a resource')
            if lock.acquire(timeout=.05): break
    try:
        if cancelled is not None and cancelled.is_set():
            raise LockCancelled('Cancelled before using a resource')
        yield
    finally:
        lock.release()


@contextmanager
def named_resource_lock(kind, identity, *, cancelled=None):
    """Machine resource identity, independent of cwd (unlike filesystem paths)."""
    with _resource_lock('named:' + kind + ':' + str(identity), cancelled):
        yield


@contextmanager
def resource_lock(kind, path):
    key = kind + ':' + os.path.normcase(str(Path(path).resolve()))
    with _resource_lock(key):
        yield


@contextmanager
def _resource_lock(key, cancelled=None):
    with _mutex:
        lock = _locks.setdefault(key, threading.RLock())
    with cancellable_lock(lock, cancelled):
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
                    if cancelled is not None and cancelled.is_set():
                        raise LockCancelled('Cancelled while waiting for another process')
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
                if cancelled is None:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                else:
                    while True:
                        if cancelled.is_set(): raise LockCancelled('Cancelled while waiting for another process')
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(.01)
            held.add(key)
            try:
                if cancelled is not None and cancelled.is_set():
                    raise LockCancelled('Cancelled before using a resource')
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
