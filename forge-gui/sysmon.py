"""System resource sampling for the Forge desktop GUI.

Zero third-party dependencies. Uses ctypes against the Windows API where
available, falling back to /proc on Linux and graceful None returns when
metrics cannot be obtained.

Public surface (must match the contract verbatim):

    class SysMon:
        def __init__(self, callback, interval=2.0): ...
        def start(self) -> None: ...
        def stop(self) -> None: ...
        def sample(self) -> dict: ...

    sample() -> {
        "cpu": float | None,
        "ram_pct": float | None,
        "ram_used_gb": float | None,
        "ram_total_gb": float | None,
        "gpu": float | None,
        "gpu_name": str | None,
    }
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Optional


_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Windows: CPU via GetSystemTimes
# ---------------------------------------------------------------------------

if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GetSystemTimes = _kernel32.GetSystemTimes
    _GetSystemTimes.argtypes = [
        ctypes.POINTER(wt.FILETIME),
        ctypes.POINTER(wt.FILETIME),
        ctypes.POINTER(wt.FILETIME),
    ]
    _GetSystemTimes.restype = wt.BOOL

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    _GlobalMemoryStatusEx = _kernel32.GlobalMemoryStatusEx
    _GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    _GlobalMemoryStatusEx.restype = wt.BOOL


# ---------------------------------------------------------------------------
# CPU sampling
# ---------------------------------------------------------------------------

# Holds the previous GetSystemTimes snapshot used to compute busy time
# between successive samples. Lives at module scope so each SysMon
# instance sees a shared, monotonic baseline (and so that the *first*
# sample returns None since there is no previous tick to diff against).
_CPU_STATE = {
    "windows_initialized": False,
    "windows_prev_idle": 0,
    "windows_prev_kernel": 0,
    "windows_prev_user": 0,
    "linux_prev_total": 0,
    "linux_prev_idle": 0,
}
_CPU_LOCK = threading.Lock()


def _filetime_to_int(ft) -> int:
    # FILETIME is a 64-bit value split into two 32-bit halves.
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _read_cpu_windows() -> Optional[float]:
    """Return CPU busy percent via two successive GetSystemTimes calls.

    The first call after import has no prior baseline, so we cache
    it and return None. Subsequent calls diff against the cached
    snapshot to produce a 0..100 value.
    """
    idle_t = wt.FILETIME()
    kernel_t = wt.FILETIME()
    user_t = wt.FILETIME()
    ok = _GetSystemTimes(ctypes.byref(idle_t), ctypes.byref(kernel_t), ctypes.byref(user_t))
    if not ok:
        return None
    idle = _filetime_to_int(idle_t)
    kernel = _filetime_to_int(kernel_t)
    user = _filetime_to_int(user_t)

    with _CPU_LOCK:
        if not _CPU_STATE["windows_initialized"]:
            _CPU_STATE["windows_prev_idle"] = idle
            _CPU_STATE["windows_prev_kernel"] = kernel
            _CPU_STATE["windows_prev_user"] = user
            _CPU_STATE["windows_initialized"] = True
            return None

        prev_idle = _CPU_STATE["windows_prev_idle"]
        prev_kernel = _CPU_STATE["windows_prev_kernel"]
        prev_user = _CPU_STATE["windows_prev_user"]
        _CPU_STATE["windows_prev_idle"] = idle
        _CPU_STATE["windows_prev_kernel"] = kernel
        _CPU_STATE["windows_prev_user"] = user

    # Kernel time includes idle time on Windows.
    idle_delta = idle - prev_idle
    kernel_delta = kernel - prev_kernel
    user_delta = user - prev_user
    total = kernel_delta + user_delta
    if total <= 0:
        # No time has passed or counters wrapped; report 0% rather than None.
        return 0.0
    busy = total - idle_delta
    if busy < 0:
        busy = 0
    return max(0.0, min(100.0, busy * 100.0 / total))


def _read_cpu_linux() -> Optional[float]:
    """Return CPU busy percent via /proc/stat deltas (Linux)."""
    try:
        with open("/proc/stat", "r", encoding="ascii") as f:
            line = f.readline()  # "cpu  ..."
    except OSError:
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    nums = []
    for p in parts[1:]:
        try:
            nums.append(int(p))
        except ValueError:
            break
    if not nums:
        return None
    # /proc/stat on Linux: user nice system idle iowait irq softirq steal guest guest_nice
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)

    with _CPU_LOCK:
        prev_total = _CPU_STATE["linux_prev_total"]
        prev_idle = _CPU_STATE["linux_prev_idle"]
        _CPU_STATE["linux_prev_total"] = total
        _CPU_STATE["linux_prev_idle"] = idle

    if prev_total == 0:
        return None
    total_d = total - prev_total
    idle_d = idle - prev_idle
    if total_d <= 0:
        return 0.0
    busy = total_d - idle_d
    return max(0.0, min(100.0, busy * 100.0 / total_d))


def _read_cpu() -> Optional[float]:
    if _IS_WINDOWS:
        return _read_cpu_windows()
    if sys.platform.startswith("linux"):
        return _read_cpu_linux()
    # macOS / other: no zero-deps reliable implementation.
    return None


# ---------------------------------------------------------------------------
# RAM sampling
# ---------------------------------------------------------------------------

def _read_ram_windows() -> Dict[str, Optional[float]]:
    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ok = _GlobalMemoryStatusEx(ctypes.byref(st))
    if not ok:
        return {"ram_pct": None, "ram_used_gb": None, "ram_total_gb": None}
    pct = float(st.dwMemoryLoad)
    total = st.ullTotalPhys
    avail = st.ullAvailPhys
    used = (total - avail) if total >= avail else 0
    GB = 1024.0 ** 3
    return {
        "ram_pct": max(0.0, min(100.0, pct)),
        "ram_used_gb": round(used / GB, 2),
        "ram_total_gb": round(total / GB, 2),
    }


def _read_ram_linux() -> Dict[str, Optional[float]]:
    info: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                toks = rest.split()
                if not toks:
                    continue
                try:
                    info[key] = int(toks[0])  # in kB
                except ValueError:
                    continue
    except OSError:
        return {"ram_pct": None, "ram_used_gb": None, "ram_total_gb": None}

    total_kb = info.get("MemTotal")
    avail_kb = info.get("MemAvailable")
    if total_kb is None:
        return {"ram_pct": None, "ram_used_gb": None, "ram_total_gb": None}
    if avail_kb is None:
        free_kb = info.get("MemFree", 0)
        buffers_kb = info.get("Buffers", 0)
        cached_kb = info.get("Cached", 0)
        avail_kb = free_kb + buffers_kb + cached_kb
    used_kb = max(total_kb - avail_kb, 0)
    pct = (used_kb * 100.0 / total_kb) if total_kb > 0 else 0.0
    return {
        "ram_pct": max(0.0, min(100.0, pct)),
        "ram_used_gb": round(used_kb / (1024.0 * 1024.0), 2),
        "ram_total_gb": round(total_kb / (1024.0 * 1024.0), 2),
    }


def _read_ram() -> Dict[str, Optional[float]]:
    if _IS_WINDOWS:
        return _read_ram_windows()
    if sys.platform.startswith("linux"):
        return _read_ram_linux()
    # macOS: no zero-deps reliable implementation.
    return {"ram_pct": None, "ram_used_gb": None, "ram_total_gb": None}


# ---------------------------------------------------------------------------
# GPU sampling via nvidia-smi (only on Windows path; throttled)
# ---------------------------------------------------------------------------

# Module-level state so that if nvidia-smi fails once (binary missing,
# no NVIDIA card, etc.) we stop trying forever instead of hanging the
# sampling thread on every call.
_GPU_STATE = {
    "disabled": False,
    "available": False,
    "name": None,
    "sampler_calls": 0,
    "lock": threading.Lock(),
}


def _query_gpu_windows() -> Optional[Dict[str, Optional[object]]]:
    """Run nvidia-smi with a 2s timeout. Never raises."""
    with _GPU_STATE["lock"]:
        if _GPU_STATE["disabled"]:
            return None
        _GPU_STATE["sampler_calls"] += 1
        # Only attempt on the 1st call and every 5th thereafter.
        n = _GPU_STATE["sampler_calls"]
        if n != 1 and n % 5 != 0:
            # Return the last cached reading (may still be None).
            if _GPU_STATE["available"]:
                return {"gpu": None, "gpu_name": _GPU_STATE["name"]}
            return None

    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            shell=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        with _GPU_STATE["lock"]:
            _GPU_STATE["disabled"] = True
        return None

    if proc.returncode != 0:
        with _GPU_STATE["lock"]:
            _GPU_STATE["disabled"] = True
        return None

    line = (proc.stdout or "").strip().splitlines()
    if not line:
        with _GPU_STATE["lock"]:
            _GPU_STATE["disabled"] = True
        return None
    first = line[0]
    # CSV: "<util>, <name>"  e.g. "28, NVIDIA GeForce RTX 4090"
    if "," in first:
        util_str, name = first.split(",", 1)
    else:
        util_str, name = first.strip(), ""
    try:
        util = float(util_str.strip())
    except ValueError:
        with _GPU_STATE["lock"]:
            _GPU_STATE["disabled"] = True
        return None
    name = name.strip() or None
    with _GPU_STATE["lock"]:
        _GPU_STATE["available"] = True
        _GPU_STATE["name"] = name
    return {"gpu": max(0.0, min(100.0, util)), "gpu_name": name}


def _read_gpu() -> Dict[str, Optional[object]]:
    if not _IS_WINDOWS:
        return {"gpu": None, "gpu_name": None}
    return _query_gpu_windows() or {"gpu": None, "gpu_name": None}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

Callback = Callable[[Dict[str, Optional[object]]], None]


class SysMon:
    """Background sampler that emits resource snapshots to *callback*.

    The sampling loop runs on a daemon thread; ``stop()`` wakes it via
    a ``threading.Event`` and joins it. ``start()`` and ``stop()`` are
    idempotent.
    """

    def __init__(self, callback: Callback, interval: float = 2.0) -> None:
        self._callback = callback
        self._interval = float(interval) if interval and interval > 0 else 2.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_lock = threading.Lock()
        self._stopped_lock = threading.Lock()

    # ---- sampling ----

    def sample(self) -> Dict[str, Optional[object]]:
        """Synchronously take one snapshot and return it."""
        payload: Dict[str, Optional[object]] = {"cpu": None}
        try:
            payload["cpu"] = _read_cpu()
        except Exception:
            payload["cpu"] = None
        try:
            payload.update(_read_ram())
        except Exception:
            payload["ram_pct"] = None
            payload["ram_used_gb"] = None
            payload["ram_total_gb"] = None
        try:
            payload.update(_read_gpu())
        except Exception:
            payload["gpu"] = None
            payload["gpu_name"] = None
        # Defensive shape guarantee.
        return {
            "cpu": payload.get("cpu"),
            "ram_pct": payload.get("ram_pct"),
            "ram_used_gb": payload.get("ram_used_gb"),
            "ram_total_gb": payload.get("ram_total_gb"),
            "gpu": payload.get("gpu"),
            "gpu_name": payload.get("gpu_name"),
        }

    # ---- lifecycle ----

    def start(self) -> None:
        with self._started_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            t = threading.Thread(
                target=self._run, name="SysMon-sampler", daemon=True
            )
            self._thread = t
            t.start()

    def stop(self) -> None:
        with self._stopped_lock:
            t = self._thread
            if t is None:
                return
            self._stop_event.set()
        # Join outside the lock so the worker thread can observe the event
        # and exit even if it was mid-callback.
        t.join(timeout=max(self._interval * 2.0, 5.0))
        self._thread = None

    # ---- internals ----

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self.sample()
            except Exception:
                # sample() already swallows per-section errors, but be
                # paranoid: never let an exception kill the loop.
                payload = {
                    "cpu": None,
                    "ram_pct": None,
                    "ram_used_gb": None,
                    "ram_total_gb": None,
                    "gpu": None,
                    "gpu_name": None,
                }
            if self._stop_event.is_set():
                break
            try:
                self._callback(payload)
            except Exception:
                # Callback failures must NOT kill the sampler thread.
                pass
            # Sleep in small slices so stop() is responsive.
            end_at = time.monotonic() + self._interval
            while not self._stop_event.is_set():
                remaining = end_at - time.monotonic()
                if remaining <= 0:
                    break
                self._stop_event.wait(timeout=min(remaining, 0.1))


# ---------------------------------------------------------------------------
# Self-test entry point
# ---------------------------------------------------------------------------

def _selftest() -> int:
    print("[selftest] sync sample x3 (0.5s apart)")
    for i in range(3):
        snap = SysMon(lambda _p: None).sample()
        print(f"  sample[{i}] = {json.dumps(snap, ensure_ascii=False)}")
        if i < 2:
            time.sleep(0.5)

    received: list = []
    done = threading.Event()

    def cb(payload):
        received.append(payload)
        if len(received) >= 3:
            done.set()

    print("[selftest] thread sample x3 @ 0.5s")
    sm = SysMon(cb, interval=0.5)
    sm.start()
    done.wait(timeout=10.0)
    sm.stop()
    for i, p in enumerate(received):
        print(f"  cb[{i}] = {json.dumps(p, ensure_ascii=False)}")
    print("thread ok")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("sysmon.py: import as a module, or run with --selftest")