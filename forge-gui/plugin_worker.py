"""Private JSON-only worker and process-tree lifetime boundary; NOT a sandbox."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import threading


class ProcessBoundary:
    """Attach before releasing the worker's stdin gate; close kills descendants."""
    def __init__(self, process):
        self.process = process
        self.job = None
        self._close_lock = threading.Lock()
        if os.name != "nt":
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel = kernel
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        # KILL_ON_JOB_CLOSE + job-wide committed memory ceiling (512 MiB).
        limits.basic.flags = 0x2000 | 0x200
        limits.job_memory = 512 * 1024 * 1024
        if (not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(job, w.HANDLE(int(process._handle)))):
            error = ctypes.get_last_error()
            kernel.CloseHandle(job)
            raise ctypes.WinError(error)
        self.job = job

    def close(self):
        with self._close_lock:
            self._close()

    def _close(self):
        if self.job is not None:
            self.kernel.TerminateJobObject(self.job, 1)
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)


def worker_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    request_path, output_path = map(Path, argv)
    # No untrusted import before parent assigns the process to its lifetime boundary.
    if sys.stdin.buffer.read(1) != b"1":
        return 2
    from plugin_runtime import _validate_tool_spec, _cap_result, MAX_TOOLS_PER_PLUGIN
    from plugin_market import parse_manifest, MANIFEST_NAME
    import importlib.util
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        directory = Path(request["directory"])
        plugin = parse_manifest(json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8")))
        os.chdir(directory)
        sys.dont_write_bytecode = True
        # Sibling imports are confined to this worker's module namespace.
        sys.path.insert(0, str(directory))
        entry = directory / "plugin.py"
        if not entry.is_file():
            raise FileNotFoundError(f"{directory} 里没有 plugin.py")
        spec = importlib.util.spec_from_file_location(f"forge_plugin_{plugin.id}", entry)
        module = importlib.util.module_from_spec(spec)
        # Compile the trusted copied bytes directly: never reuse stale .pyc files.
        exec(compile(entry.read_bytes(), str(entry), "exec"), module.__dict__)
        register = getattr(module, "register", None)
        if not callable(register):
            raise AttributeError("插件缺少模块级 register() 函数")
        raw = register()
        if not isinstance(raw, list):
            raise TypeError("register() 必须返回 list")
        if len(raw) > 1024:
            raise ValueError("register() 工具数量超过硬上限")
        tools = [_validate_tool_spec(item, plugin, i) for i, item in enumerate(raw)]
        if len({tool["name"] for tool in tools}) != len(tools):
            raise ValueError("重复注册工具名")
        metadata = [{k: t[k] for k in ("name", "description", "parameters")}
                    for t in tools[:MAX_TOOLS_PER_PLUGIN]]
        if request["action"] == "load":
            value = {"tools": metadata, "overflow": max(0, len(tools)-MAX_TOOLS_PER_PLUGIN)}
        else:
            if metadata != request["metadata"]:
                raise PermissionError("register() 工具声明发生变化，请重新加载并确认插件")
            tool = next((t for t in tools[:MAX_TOOLS_PER_PLUGIN] if t["name"] == request["name"]), None)
            if tool is None:
                raise KeyError("工具不存在")
            value = _cap_result(tool["execute"](request["arguments"]))
        packet = {"ok": True, "value": value}
    except BaseException as exc:
        packet = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:4000]}
        if isinstance(exc, PermissionError):
            packet["schema_changed"] = True
    # Only JSON crosses the boundary. No pickle or plugin objects in the host.
    text = json.dumps(packet, ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > 2_000_000:
        text = '{"ok":false,"error":"插件输出超过传输上限"}'
    output_path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main())
