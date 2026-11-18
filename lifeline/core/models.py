"""Data model shared by the scan engines, the recovery writer and the UI."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple

# An extent is a run of bytes on the volume: (byte_offset, length).
# byte_offset is None for a sparse run (reads back as zeros).
Extent = Tuple[Optional[int], int]

# Callback types used by every long-running engine.
Progress = Callable[[float, str], None]   # (fraction 0..1, human message)
Cancelled = Callable[[], bool]
OnFound = Callable[[int], None]           # running count of items found so far
OnBlock = Callable[[float, bool], None]   # (fraction of the volume swept, found something here)

CATEGORIES = {
    "Images": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "heif", "avif", "tif", "tiff", "raw", "cr2", "cr3",
               "nef", "arw", "dng", "orf", "rw2", "raf", "svg", "ico", "psd"},
    "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "txt", "rtf", "csv",
                  "md", "html", "htm", "xml", "json", "epub", "msg", "pages", "numbers", "key"},
    "Video": {"mp4", "mov", "avi", "mkv", "wmv", "asf", "m4v", "webm", "flv", "3gp", "mts", "m2ts"},
    "Audio": {"mp3", "wav", "flac", "m4a", "aac", "ogg", "oga", "opus", "wma", "aif", "aiff"},
    "Archives": {"zip", "7z", "rar", "tar", "gz", "bz2", "xz", "iso", "cab", "jar"},
    "Code & data": {"py", "js", "ts", "java", "c", "cpp", "h", "cs", "go", "rs", "php", "rb", "sql", "sqlite",
                    "db", "ipynb", "yml", "yaml", "ini", "cfg", "exe", "dll", "msi", "elf", "pst", "ost"},
}


def category_for(ext: str) -> str:
    ext = ext.lower()
    for name, exts in CATEGORIES.items():
        if ext in exts:
            return name
    return "Other"


class Condition:
    GOOD = "Good"                # data clusters are free and untouched
    PARTIAL = "Partly overwritten"
    OVERWRITTEN = "Overwritten"  # every cluster now belongs to another file
    UNKNOWN = "Unknown"          # carved files: structure validated, layout guessed


# Plain-language recovery chances shown to people who don't know file-system terms.
CHANCE_EXCELLENT = "Excellent"
CHANCE_GOOD = "Good"
CHANCE_POOR = "Poor"
CHANCE_NONE = "Unlikely"


class Source:
    QUICK = "Quick scan"
    DEEP = "Deep scan"
    TRASH = "Recycle Bin"
    PARTITION = "Lost partition"


@dataclass
class RecoverableItem:
    id: int
    name: str
    folder: str                 # reconstructed parent path, "/"-separated, no leading slash
    size: int
    source: str                 # one of Source.*
    extents: List[Extent] = field(default_factory=list)
    resident_data: Optional[bytes] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None
    condition: str = Condition.UNKNOWN
    overwritten_pct: float = 0.0
    mft_record: Optional[int] = None   # NTFS record number or FAT/exFAT directory-entry offset
    file_path: Optional[str] = None    # read through the normal file system (Recycle Bin / Trash)
    deleted: bool = True               # False for live files listed from a lost/damaged partition
    deleted_at: Optional[datetime] = None
    note: str = ""                     # extra detail, e.g. "Fragmented: 3 pieces" or a verify result
    verified: Optional[str] = None     # None (not checked), "ok", "damaged" or "unknown"

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lstrip(".").lower()

    @property
    def category(self) -> str:
        return category_for(self.ext)

    @property
    def full_path(self) -> str:
        return f"{self.folder}/{self.name}" if self.folder else self.name

    @property
    def first_offset(self) -> Optional[int]:
        for off, _ in self.extents:
            if off is not None:
                return off
        return None

    @property
    def chance(self) -> str:
        """A plain-language guess at how well this file will come back."""
        if self.verified == "damaged":
            return CHANCE_POOR
        if self.file_path is not None or not self.deleted:
            return CHANCE_EXCELLENT
        if self.condition == Condition.OVERWRITTEN:
            return CHANCE_NONE
        if self.condition == Condition.PARTIAL:
            return CHANCE_POOR
        if self.condition == Condition.GOOD:
            return CHANCE_EXCELLENT
        return CHANCE_EXCELLENT if self.verified == "ok" else CHANCE_GOOD


def human_size(n: Optional[int]) -> str:
    if n is None:
        return ""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


class FreeSpaceFilter:
    """Picklable "is this byte offset in a free cluster?" test built from an allocation bitmap
    (LSB-first, one bit per cluster, 1 = in use). Safe to send to worker processes."""

    def __init__(self, bitmap: bytes, cluster_size: int, data_start: int = 0, first_cluster: int = 0):
        self.bitmap = bitmap
        self.cluster_size = cluster_size
        self.data_start = data_start          # byte offset of cluster `first_cluster`
        self.first_cluster = first_cluster    # 0 for NTFS, 2 for FAT/exFAT

    def __call__(self, offset: int) -> bool:
        if offset < self.data_start:
            return False                      # FAT/exFAT metadata area: never free
        c = (offset - self.data_start) // self.cluster_size
        return c // 8 < len(self.bitmap) and not (self.bitmap[c // 8] >> (c % 8)) & 1
