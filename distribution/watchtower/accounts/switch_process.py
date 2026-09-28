"""Read-only Windows process proofs for an existing account-switch pane.

Watchtower's foreground projection selects recognized agents, not arbitrary
children. Inspect the OS tree directly instead. No command lines, environments,
credentials or process mutation are involved.
"""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ntpath
import os
import re

_WINDOWS = os.name == "nt"
_MAX_PROCESSES = 65536
_MAX_PYTHON_PROCESSES = 4
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)?)?w?\.exe\Z", re.IGNORECASE)
_SHELLS = {"powershell.exe", "pwsh.exe"}


@dataclass(frozen=True)
class _Entry:
    pid: int
    parent_pid: int
    name: str


def _valid_pid(pid):
    return type(pid) is int and 0 < pid <= 0xFFFFFFFF


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _snapshot():
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = _kernel()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("Process snapshot unavailable")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(handle, ctypes.byref(entry)):
            raise OSError("Process snapshot unavailable")
        result = {}
        while True:
            pid = int(entry.th32ProcessID)
            if pid in result or len(result) >= _MAX_PROCESSES:
                raise OSError("Process snapshot is ambiguous or too large")
            result[pid] = _Entry(pid, int(entry.th32ParentProcessID), entry.szExeFile.lower())
            if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise OSError("Process snapshot is incomplete")
                return result
    finally:
        kernel.CloseHandle(handle)


class _Process:
    def __init__(self, pid):
        kernel = self.kernel = _kernel()
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not self.handle:
            raise OSError("Process identity unavailable")
        try:
            size = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(self.handle, 0, image, ctypes.byref(size)):
                raise OSError("Process identity unavailable")
            self.name = ntpath.basename(image.value).lower()
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                raise OSError("Process generation unavailable")
            self.created = (created.dwHighDateTime << 32) | created.dwLowDateTime
            if not self.alive():
                raise OSError("Process exited")
        except BaseException:
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise

    def alive(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 258  # WAIT_TIMEOUT

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.kernel.CloseHandle(self.handle)


def shell_has_no_children(shell_pid):
    """Require a live PowerShell root with no OS child, including non-agents."""
    if not _WINDOWS or not _valid_pid(shell_pid):
        return False
    try:
        with _Process(shell_pid) as shell:
            if shell.name not in _SHELLS:
                return False
            snapshot = _snapshot()
            entry = snapshot.get(shell_pid)
            # Any descendant chain begins with a direct child. Do not ignore
            # Python wrappers, background utilities, or unrecognized commands.
            return bool(entry and entry.name == shell.name and shell.alive()
                        and not any(item.parent_pid == shell_pid for item in snapshot.values()))
    except (OSError, ValueError, OverflowError):
        return False


def runner_belongs_to_shell(shell_pid):
    """Prove this Python runner is below the exact live pane PowerShell.

    Windows venv launchers may add Python-to-Python parents. Accept at most four
    Python processes, hold each generation alive, and reject every other wrapper.
    """
    runner_pid = os.getpid()
    if not _WINDOWS or not _valid_pid(shell_pid) or shell_pid == runner_pid:
        return False
    try:
        first = _snapshot()
        chain = []
        current = runner_pid
        while current != shell_pid:
            entry = first.get(current)
            if (not entry or current in chain or len(chain) >= _MAX_PYTHON_PROCESSES
                    or not _PYTHON.fullmatch(entry.name)):
                return False
            chain.append(current)
            current = entry.parent_pid
        chain.append(shell_pid)
        if not first.get(shell_pid) or first[shell_pid].name not in _SHELLS:
            return False
        with ExitStack() as stack:
            held = {pid: stack.enter_context(_Process(pid)) for pid in chain}
            if any(held[pid].name != first[pid].name for pid in chain):
                return False
            # Parent PID reuse must not turn a younger unrelated process into
            # an accepted ancestor of this already-running child.
            if any(held[parent].created > held[child].created
                   for child, parent in zip(chain, chain[1:])):
                return False
            second = _snapshot()
            if any(second.get(pid) != first[pid] for pid in chain):
                return False
            # Reject parallel work under the pane, allowing only the current
            # Python chain and this runner's transient metadata-query children.
            for child, parent in zip(chain, chain[1:]):
                if any(item.parent_pid == parent and item.pid != child for item in second.values()):
                    return False
            return all(process.alive() for process in held.values())
    except (OSError, ValueError, OverflowError):
        return False
