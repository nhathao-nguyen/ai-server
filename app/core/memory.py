"""Small, dependency-free system RAM probe for the local admin metrics."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryStatus:
    total_bytes: int | None
    available_bytes: int | None
    used_bytes: int | None
    utilization_percent: int | None
    reason: str | None = None

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "total_bytes": self.total_bytes,
            "available_bytes": self.available_bytes,
            "used_bytes": self.used_bytes,
            "utilization_percent": self.utilization_percent,
            "reason": self.reason,
        }


class _MemoryStatusEx(ctypes.Structure):
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


class SystemMemoryProbe:
    """Read current system RAM without adding a runtime dependency."""

    def read(self) -> MemoryStatus:
        if os.name == "nt":
            return self._read_windows()
        return self._read_posix()

    @staticmethod
    def _read_windows() -> MemoryStatus:
        try:
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return MemoryStatus(None, None, None, None, "global_memory_status_failed")
            total = int(status.ullTotalPhys)
            available = int(status.ullAvailPhys)
            return MemoryStatus(total, available, max(0, total - available), int(status.dwMemoryLoad))
        except (AttributeError, OSError, TypeError, ValueError):
            return MemoryStatus(None, None, None, None, "global_memory_status_unavailable")

    @staticmethod
    def _read_posix() -> MemoryStatus:
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
            total = page_size * total_pages
            available = page_size * available_pages
            used = max(0, total - available)
            utilization = round((used / total) * 100) if total else 0
            return MemoryStatus(total, available, used, utilization)
        except (AttributeError, OSError, ValueError):
            return MemoryStatus(None, None, None, None, "system_memory_unavailable")
