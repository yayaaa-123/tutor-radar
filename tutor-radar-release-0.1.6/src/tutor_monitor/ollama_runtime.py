"""A dedicated Ollama listener with an explicit model directory."""
import atexit
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil


PORT = 18766
ENDPOINT = f"http://127.0.0.1:{PORT}"
_process: subprocess.Popen | None = None
_directory: Path | None = None
_job = None


def resolve_directory(value: str) -> Path:
    path = Path(value.strip())
    if not path.is_absolute() or not path.drive or path.drive.startswith("\\"):
        raise ValueError("invalid_directory")
    path = path.resolve()
    if path.parent == path:
        raise ValueError("invalid_directory")
    return path


def find_ollama() -> str | None:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        executable = Path(local) / "Programs/Ollama/ollama.exe"
        if executable.is_file():
            return str(executable)
    return shutil.which("ollama.exe")


def owns_listener() -> bool:
    if _process is None or _process.poll() is not None:
        return False
    try:
        return any(item.status == psutil.CONN_LISTEN and item.laddr.ip == "127.0.0.1"
                   and item.laddr.port == PORT
                   for item in psutil.Process(_process.pid).net_connections(kind="tcp4"))
    except (psutil.Error, OSError):
        return False


def _attach_kill_job(pid: int):
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
            ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    handle = None
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        handle = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not handle or not kernel.AssignProcessToJobObject(job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        kernel.CloseHandle(job)
        raise
    finally:
        if handle:
            kernel.CloseHandle(handle)
    return kernel, job


def stop() -> None:
    global _process, _directory, _job
    if _job:
        kernel, job = _job
        kernel.CloseHandle(job)
    elif _process and _process.poll() is None:
        _process.terminate()
    if _process:
        _process.wait(timeout=5)
    _process = _directory = _job = None


atexit.register(stop)


def start(executable: str, directory: Path) -> None:
    global _process, _directory, _job
    if _directory == directory and owns_listener():
        return
    stop()
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OLLAMA_MODELS=str(directory), OLLAMA_HOST=f"127.0.0.1:{PORT}",
               OLLAMA_NO_CLOUD="1", OLLAMA_NOPRUNE="1")
    _process = subprocess.Popen([executable, "serve"], env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _job = _attach_kill_job(_process.pid)
        for _ in range(50):
            if owns_listener():
                _directory = directory
                return
            if _process.poll() is not None:
                break
            time.sleep(0.2)
        raise OSError("managed_ollama_unavailable")
    except Exception:
        stop()
        raise
