"""Thin ctypes wrappers for the few Windows device calls Lifeline needs. Import only on Windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

INVALID = wintypes.HANDLE(-1).value
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C


def open_device(path: str):
    """Open a device for IOCTLs only (no read access needed, works unelevated for many queries)."""
    h = k32.CreateFileW(path, 0, 0x1 | 0x2, None, 3, 0, None)  # share R/W, OPEN_EXISTING
    if h is None or h == INVALID:
        return None
    return h


def close(h):
    k32.CloseHandle(h)


def ioctl(handle, code: int, in_buf, out_size: int) -> Optional[bytes]:
    out = ctypes.create_string_buffer(out_size)
    returned = wintypes.DWORD(0)
    in_ptr = ctypes.cast(in_buf, wintypes.LPVOID) if in_buf is not None else None
    in_len = ctypes.sizeof(in_buf) if in_buf is not None else 0
    ok = k32.DeviceIoControl(handle, code, in_ptr, in_len, out, out_size, ctypes.byref(returned), None)
    return out.raw[:returned.value] if ok else None


def storage_property(handle, prop_id: int, size: int = 1024) -> Optional[bytes]:
    q = (ctypes.c_uint32 * 3)(prop_id, 0, 0)  # PropertyId, PropertyStandardQuery
    return ioctl(handle, IOCTL_STORAGE_QUERY_PROPERTY, q, size)


def disk_numbers_of_volume(letter: str):
    """Physical disk numbers a drive letter lives on (more than one for spanned volumes)."""
    import struct
    h = open_device(f"\\\\.\\{letter}:")
    if h is None:
        return []
    try:
        out = ioctl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 8 + 24 * 8)
        if not out or len(out) < 8:
            return []
        n = struct.unpack_from("<I", out, 0)[0]
        return [struct.unpack_from("<I", out, 8 + 24 * i)[0] for i in range(n) if 8 + 24 * i + 4 <= len(out)]
    finally:
        close(h)
