"""Common interface for the file systems Lifeline can read deleted entries from.

Every parser (NTFS, FAT12/16/32, exFAT) implements FileSystem so the session,
the CLI and the UI never need to know which one they're talking to.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .disk import DiskReader
from .models import Cancelled, FreeSpaceFilter, OnFound, Progress, RecoverableItem


class FSError(Exception):
    """The volume isn't this file system, or its metadata is too damaged to use."""


class FileSystem:
    name: str = "Unknown"         # "NTFS", "FAT12", "FAT16", "FAT32", "exFAT"
    cluster_size: int = 512
    volume_size: int = 0
    label: str = ""

    def __init__(self, reader: DiskReader):
        self.reader = reader

    def scan_deleted(self, progress: Progress, cancelled: Cancelled, on_found: Optional[OnFound] = None,
                     include_existing: bool = False, workers: int = 1) -> List[RecoverableItem]:
        """Return deleted files with names, folders and data extents.
        include_existing=True also lists live files (deleted=False): used for lost/damaged partitions."""
        raise NotImplementedError

    def free_regions(self) -> List[Tuple[int, int]]:
        """Byte ranges that contain free clusters (a superset is fine)."""
        return [(0, self.volume_size)]

    def free_filter(self) -> Optional[FreeSpaceFilter]:
        """Picklable per-offset free-cluster test, or None if unknown."""
        return None

    def is_free(self, offset: int) -> bool:
        f = self.free_filter()
        return True if f is None else f(offset)


def detect_filesystem(reader: DiskReader) -> Optional[FileSystem]:
    """Try each parser on the volume. Returns None if none recognises it."""
    from .ntfs import NTFSVolume
    candidates = [NTFSVolume]
    try:
        from .exfat import ExFATVolume
        candidates.append(ExFATVolume)
    except ImportError:
        pass
    try:
        from .fat import FATVolume
        candidates.append(FATVolume)
    except ImportError:
        pass
    for cls in candidates:
        try:
            return cls(reader)
        except FSError:
            continue
    return None


def sniff(boot: bytes) -> Optional[str]:
    """Name the file system from a boot sector alone (no further reads)."""
    if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
        return "NTFS" if boot[3:11] == b"NTFS    " else None
    if boot[3:11] == b"NTFS    ":
        return "NTFS"
    if boot[3:11] == b"EXFAT   ":
        return "exFAT"
    if boot[82:90] == b"FAT32   ":
        return "FAT32"
    if boot[54:62] in (b"FAT16   ", b"FAT12   ", b"FAT     "):
        return boot[54:59].decode().strip() if boot[54:59] != b"FAT  " else "FAT"
    return None
