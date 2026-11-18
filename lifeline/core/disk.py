"""Raw, read-only access to drives, partitions and disk image files.

Works the same on Windows (\\\\.\\D:, \\\\.\\PhysicalDrive1), Linux (/dev/sdb1)
and macOS (/dev/rdisk2s1). Raw devices on Windows and macOS only accept reads
that start on a sector boundary and cover whole sectors; DiskReader hides that,
so callers can ask for any (offset, size) and get exactly those bytes.

A failing drive returns read errors for some sectors. Instead of losing a whole
multi-megabyte read, DiskReader retries the window in 4 KB pieces and fills only
the unreadable pieces with zeros, counting them in `bad_blocks`.
"""
from __future__ import annotations

import os
import stat
import struct
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
ALIGN = 4096  # multiple of both 512e and 4Kn sector sizes


class DiskError(Exception):
    pass


class DiskReader:
    """Thread-safe, read-only, sector-aligned reader for a device or an image file.
    base_offset/length expose a window of it (a partition inside a disk)."""

    def __init__(self, path: str, base_offset: int = 0, length: Optional[int] = None):
        self.path = path
        self.base_offset = base_offset
        self.bad_blocks = 0
        self._lock = threading.Lock()
        try:
            self._f = open(path, "rb", buffering=0)
        except PermissionError as e:
            raise DiskError(access_denied_message()) from e
        except OSError as e:
            raise DiskError(f"Can't open {path}: {e.strerror or e}") from e
        self.length = length if length is not None else max(0, self._detect_length())

    def _detect_length(self) -> int:
        if is_device_path(self.path):
            return device_length(self.path, self._f) - self.base_offset
        return os.path.getsize(self.path) - self.base_offset

    def read_at(self, offset: int, size: int) -> bytes:
        """Read `size` bytes at reader-relative `offset`. Short read only at the end of the media;
        unreadable sectors in the middle come back as zeros."""
        if size <= 0 or offset < 0:
            return b""
        size = min(size, max(0, self.length - offset)) if self.length else size
        if size <= 0:
            return b""
        absolute = self.base_offset + offset
        start = absolute - (absolute % ALIGN)
        end = absolute + size
        end = end + (-end % ALIGN)
        with self._lock:
            data = self._read_window(start, end - start)
        skip = absolute - start
        return data[skip:skip + size]

    def _read_window(self, start: int, n: int) -> bytes:
        data = self._read_fully(start, n)
        if len(data) >= n or self.length == 0:
            return data
        # Short read inside the media: a bad area. Salvage it 4 KB at a time.
        media_end = self.base_offset + self.length
        if start + len(data) >= media_end:
            return data
        out = bytearray(data[:len(data) - len(data) % ALIGN])
        pos = start + len(out)
        stop = min(start + n, media_end + (-media_end % ALIGN))
        while pos < stop:
            piece = self._read_fully(pos, ALIGN)
            if len(piece) < ALIGN and pos + ALIGN <= media_end:
                self.bad_blocks += 1
                piece = piece + b"\x00" * (ALIGN - len(piece))
            out += piece
            pos += ALIGN
        return bytes(out)

    def _read_fully(self, start: int, n: int) -> bytes:
        parts, got = [], 0
        try:
            self._f.seek(start)
        except OSError:
            return b""
        while got < n:
            try:
                chunk = self._f.read(n - got)
            except OSError:
                chunk = None  # bad sector: the caller zero-fills
            if not chunk:
                break
            parts.append(chunk)
            got += len(chunk)
        return b"".join(parts)

    def sub(self, offset: int, length: int) -> "DiskReader":
        """A new reader over a window of this one (e.g. a partition of a whole disk)."""
        return DiskReader(self.path, base_offset=self.base_offset + offset, length=length)

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def access_denied_message() -> str:
    if IS_WINDOWS:
        return "Access denied. Run Lifeline as administrator to read drives directly."
    if IS_MAC:
        return ("Access denied. Restart Lifeline with administrator rights, and give it Full Disk Access "
                "in System Settings > Privacy & Security.")
    return ("Access denied. Restart Lifeline with administrator rights (it will ask for your password), "
            "or run it with sudo.")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def is_raw_volume_path(path: str) -> bool:
    """A Windows device path such as \\\\.\\D: or \\\\.\\PhysicalDrive0."""
    return path.startswith("\\\\.\\")


def is_device_path(path: str) -> bool:
    if is_raw_volume_path(path):
        return True
    if IS_WINDOWS:
        return False
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return path.startswith("/dev/")
    return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


def raw_path_for_letter(letter: str) -> str:
    return f"\\\\.\\{letter.upper()}:"


def drive_letter_of(path: str) -> Optional[str]:
    """'E:\\x' -> 'E', '\\\\.\\D:' -> 'D', otherwise None."""
    if is_raw_volume_path(path):
        rest = path[4:]
        return rest[0].upper() if len(rest) == 2 and rest[1] == ":" else None
    if not IS_WINDOWS:
        return None
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if len(drive) == 2 and drive[1] == ":":
        return drive[0].upper()
    return None


# --------------------------------------------------------------------------
# Device size
# --------------------------------------------------------------------------

def device_length(path: str, f=None) -> int:
    if IS_WINDOWS:
        return _windows_length(path)
    if IS_MAC:
        n = _mac_length(f, path)
        if n:
            return n
    # Linux block devices (and most others) report their size through SEEK_END.
    try:
        if f is not None:
            n = f.seek(0, os.SEEK_END)
            f.seek(0)
        else:
            with open(path, "rb", buffering=0) as g:
                n = g.seek(0, os.SEEK_END)
        if n:
            return n
    except OSError:
        pass
    if IS_LINUX:
        name = os.path.basename(os.path.realpath(path))
        try:
            with open(f"/sys/class/block/{name}/size") as s:
                return int(s.read()) * 512
        except (OSError, ValueError):
            pass
    raise DiskError(f"Can't tell how big {path} is.")


def _mac_length(f, path: str) -> int:
    import fcntl
    DKIOCGETBLOCKSIZE, DKIOCGETBLOCKCOUNT = 0x40046418, 0x40086419
    try:
        own = f is None
        g = open(path, "rb", buffering=0) if own else f
        try:
            bs = struct.unpack("<I", fcntl.ioctl(g.fileno(), DKIOCGETBLOCKSIZE, b"\0" * 4))[0]
            bc = struct.unpack("<Q", fcntl.ioctl(g.fileno(), DKIOCGETBLOCKCOUNT, b"\0" * 8))[0]
            return bs * bc
        finally:
            if own:
                g.close()
    except OSError:
        return 0


def _windows_length(raw_path: str) -> int:
    import ctypes
    from . import winapi
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    h = winapi.open_device(raw_path)
    if h is not None:
        try:
            out = winapi.ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 8)
            if out and len(out) == 8:
                return struct.unpack("<q", out)[0]
        finally:
            winapi.close(h)
    letter = drive_letter_of(raw_path)
    if letter:
        total = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(f"{letter}:\\", None, ctypes.byref(total), None)
        return total.value
    raise DiskError(f"Can't tell how big {raw_path} is.")


# --------------------------------------------------------------------------
# Disk imaging: copy a (possibly failing) drive to a file, ddrescue style
# --------------------------------------------------------------------------

def create_image(source: str, dest: str, progress: Callable[[float, str], None],
                 cancelled: Callable[[], bool], chunk: int = 4 * 1024 * 1024,
                 resume: bool = True, same_device: Optional[Callable[[str, str], bool]] = None) -> dict:
    """Copy `source` to `dest`.

    Pass 1 copies in big chunks and skips quickly past any chunk with read errors,
    so a failing drive gives up as much healthy data as possible before it gets worse.
    Pass 2 goes back over the skipped chunks 4 KB at a time and patches them in.
    With resume=True an existing, shorter `dest` is continued rather than restarted.
    """
    if same_device is None:
        from .devices import same_device as _same
        same_device = _same
    if same_device(source, dest):
        raise DiskError("Save the image to a different drive than the one you're copying.")
    with DiskReader(source) as reader:
        total = reader.length
        start = 0
        if resume and os.path.exists(dest):
            start = min(os.path.getsize(dest), total)
            start -= start % ALIGN
        mode = "r+b" if start else "wb"
        bad_windows: List[Tuple[int, int]] = []
        t0, pos = time.monotonic(), start
        with open(dest, mode) as out:
            out.seek(start)
            while pos < total and not cancelled():
                n = min(chunk, total - pos)
                before = reader.bad_blocks
                with reader._lock:
                    data = reader._read_fully(reader.base_offset + pos, n)
                if len(data) < n:
                    bad_windows.append((pos, n))
                    data = data + b"\x00" * (n - len(data))
                reader.bad_blocks = before
                out.write(data)
                pos += n
                rate = (pos - start) / max(time.monotonic() - t0, 1e-3)
                progress(pos / total * (0.9 if bad_windows else 1.0),
                         f"Copied {pos / 2**20:,.0f} of {total / 2**20:,.0f} MB  ({rate / 2**20:,.0f} MB/s)")
            bad = 0
            for i, (w, n) in enumerate(bad_windows):
                if cancelled():
                    break
                progress(0.9 + 0.1 * i / len(bad_windows), f"Retrying damaged area {i + 1} of {len(bad_windows)}")
                for s in range(0, n, ALIGN):
                    m = min(ALIGN, n - s)
                    with reader._lock:
                        piece = reader._read_fully(reader.base_offset + w + s, m)
                    if len(piece) < m:
                        bad += 1
                        continue
                    out.seek(w + s)
                    out.write(piece)
            out.truncate(pos)
    return {"bytes": pos, "unreadable_blocks": bad, "complete": pos >= total and not cancelled(),
            "resumed_from": start}


# --------------------------------------------------------------------------
# Back-compat helpers
# --------------------------------------------------------------------------

def find_partitions(reader: DiskReader) -> List[tuple]:
    """[(byte_offset, byte_length, description)] from the MBR/GPT of a whole-disk source."""
    from .partitions import read_partition_table
    return [(p.offset, p.length, p.description) for p in read_partition_table(reader)]
