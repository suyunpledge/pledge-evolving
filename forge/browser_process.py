"""Lifetime fence for the browser helper, not an OS permission sandbox."""
import ctypes
import os
import signal


class BrowserLifetime:
    def __init__(self, process):
        self.process, self.job = process, None
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        kernel = self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, w.HANDLE(int(process._handle)))):
            error = ctypes.get_last_error()
            kernel.CloseHandle(job)
            raise ctypes.WinError(error)
        self.job = job

    def close(self):
        if self.job is not None:
            self.kernel.TerminateJobObject(self.job, 1)
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def worker_streams():
    """pythonw / a windowed frozen EXE may set Python stdio to None.

    Reopen only the anonymous pipe handles supplied by the owning Popen.
    No named pipe, TCP port or publicly attachable debugging endpoint is used.
    """
    import sys
    if os.name != 'nt' or sys.stdin is not None and sys.stdout is not None:
        return sys.stdin, sys.stdout
    import msvcrt
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [w.DWORD]
    kernel.GetStdHandle.restype = w.HANDLE
    kernel.GetFileType.argtypes = [w.HANDLE]
    def pipe(number, mode, flags):
        handle = kernel.GetStdHandle(number)
        if kernel.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            raise RuntimeError('Browser worker requires parent-owned pipes')
        descriptor = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
        return os.fdopen(descriptor, mode, encoding='utf-8')
    return pipe(-10, 'r', os.O_RDONLY), pipe(-11, 'w', os.O_WRONLY)
