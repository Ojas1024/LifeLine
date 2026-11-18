"""A scan session: one opened source plus the files found on it.

Shared by the GUI and the CLI. A source is a drive, a partition, a whole disk or
an image file. Whole disks are opened on their first readable partition, and the
session remembers the partition table so the UI can offer the others.
"""
from __future__ import annotations

import base64
import gzip
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional

from . import parallel
from .carver import SIGNATURES, Carver
from .disk import DiskError, DiskReader, is_device_path
from .filesystems import FileSystem, FSError, detect_filesystem
from .models import Cancelled, OnBlock, OnFound, Progress, RecoverableItem, Source

SESSION_VERSION = 2
NoOp = lambda *a, **k: None  # noqa: E731


def _scaled(progress: Progress, lo: float, hi: float, prefix: str = "") -> Progress:
    return lambda f, m: progress(lo + (hi - lo) * f, prefix + m)


class ScanSession:
    def __init__(self, source_path: Optional[str], display_name: Optional[str] = None, partition=None,
                 media: str = "Unknown"):
        self.source_path = source_path
        self.display_name = display_name or source_path or "Recycle Bin"
        self.media = media
        self.items: List[RecoverableItem] = []
        self.partitions: list = []
        self.partition = None
        self.disk: Optional[DiskReader] = None
        self.reader: Optional[DiskReader] = None
        self.fs: Optional[FileSystem] = None
        self.fs_error: Optional[str] = None
        if source_path is None:
            return                                   # a Recycle-Bin-only session
        self.disk = DiskReader(source_path)
        if partition is not None:
            self._use_partition(partition)
            return
        self.reader = self.disk
        self.fs = self._detect(self.disk)
        if self.fs is None:
            try:
                from .partitions import read_partition_table
                self.partitions = read_partition_table(self.disk)
            except Exception:
                self.partitions = []
            for p in self.partitions:
                if getattr(p, "fs", None):
                    self._use_partition(p)
                    if self.fs:
                        break
            if self.fs is None:
                self.reader, self.partition = self.disk, None
        if self.fs is None and not self.fs_error:
            self.fs_error = ("Lifeline can't read the file system on this drive (it may be formatted, damaged, "
                             "or a type like ext4/APFS). Deep scan can still find files by their contents.")

    def _detect(self, reader: DiskReader) -> Optional[FileSystem]:
        try:
            return detect_filesystem(reader)
        except FSError as e:
            self.fs_error = str(e)
        except Exception as e:  # a damaged volume must never stop a deep scan
            self.fs_error = f"The file system looks damaged ({e.__class__.__name__}). Deep scan still works."
        return None

    def _use_partition(self, p):
        if self.reader is not None and self.reader is not self.disk:
            self.reader.close()
        self.partition = p
        self.reader = self.disk.sub(p.offset, p.length)
        self.fs = self._detect(self.reader)

    def use_partition(self, p):
        """Switch to another partition of the same disk. Results are per partition (their
        offsets are relative to it), so the list starts empty again."""
        self.items = []
        self._use_partition(p)

    def use_whole_disk(self):
        """Scan the disk itself rather than a partition (deep scan across everything)."""
        if self.reader is not None and self.reader is not self.disk:
            self.reader.close()
        self.items, self.partition, self.reader = [], None, self.disk
        self.fs_error = None
        self.fs = self._detect(self.disk)
        if self.fs is None and not self.fs_error:
            self.fs_error = "The whole disk has no single file system. Deep scan searches all of it by content."

    # ---------------------------------------------------------------- facts
    @property
    def fs_name(self) -> str:
        return self.fs.name if self.fs else "Unknown"

    @property
    def is_ntfs(self) -> bool:
        return self.fs is not None and self.fs.name == "NTFS"

    @property
    def can_quick(self) -> bool:
        return self.fs is not None

    @property
    def ntfs_error(self) -> Optional[str]:  # older callers
        return self.fs_error

    @property
    def size(self) -> int:
        if self.fs and self.fs.volume_size:
            return self.fs.volume_size
        return self.reader.length if self.reader else 0

    @property
    def is_image(self) -> bool:
        return bool(self.source_path) and not is_device_path(self.source_path)

    def default_workers(self) -> int:
        return parallel.default_workers(self.media, self.is_image)

    # ---------------------------------------------------------------- scans
    def quick_scan(self, progress: Progress, cancelled: Cancelled, on_found: Optional[OnFound] = None,
                   include_existing: bool = False, workers: Optional[int] = None) -> List[RecoverableItem]:
        if not self.fs:
            raise FSError(self.fs_error or "Quick scan needs an NTFS, FAT or exFAT drive.")
        base = len(self.items)
        found = self.fs.scan_deleted(progress, cancelled, (lambda n: on_found(base + n)) if on_found else None,
                                     include_existing=include_existing,
                                     workers=workers or self.default_workers())
        if self.partition is not None and getattr(self.partition, "origin", "") == "Found":
            for it in found:
                it.source = Source.PARTITION
        return self._add(found)

    def deep_scan(self, keys: List[str], free_space_only: bool, progress: Progress, cancelled: Cancelled,
                  on_found: Optional[OnFound] = None, on_block: Optional[OnBlock] = None,
                  workers: Optional[int] = None) -> List[RecoverableItem]:
        accept, block = None, 512
        regions = [(0, self.reader.length)]
        if self.fs:
            filt = self.fs.free_filter() if free_space_only else None
            data_start = filt.data_start if filt else getattr(self.fs, "data_start", 0)
            block = math.gcd(self.fs.cluster_size, data_start) if data_start else self.fs.cluster_size
            block = max(512, block)
            if filt:
                accept, regions = filt, self.fs.free_regions()
        base = len(self.items)
        carver = Carver(self.reader, block=block)
        known = frozenset(it.first_offset for it in self.items
                          if it.first_offset is not None and it.source != Source.TRASH)
        found = carver.scan(regions, keys, progress, cancelled,
                            (lambda n: on_found(base + n)) if on_found else None, on_block,
                            accept=accept, workers=workers or self.default_workers(), known=known)
        return self._add(found)

    def smart_scan(self, keys: Optional[List[str]], progress: Progress, cancelled: Cancelled,
                   on_found: Optional[OnFound] = None, on_block: Optional[OnBlock] = None,
                   workers: Optional[int] = None, free_space_only: bool = True) -> List[RecoverableItem]:
        """What most people want: names and folders first (quick), then contents (deep)."""
        keys = keys or [s.key for s in SIGNATURES]
        out: List[RecoverableItem] = []
        split = 0.0
        if self.fs:
            split = 0.25
            out += self.quick_scan(_scaled(progress, 0, split, "Step 1 of 2: "), cancelled, on_found,
                                   workers=workers)
        if cancelled():
            return out
        prefix = "Step 2 of 2: " if self.fs else ""
        out += self.deep_scan(keys, free_space_only and self.fs is not None,
                              _scaled(progress, split, 1.0, prefix), cancelled, on_found, on_block, workers)
        return out

    def scan_trash(self, progress: Progress = NoOp, cancelled: Cancelled = lambda: False) -> List[RecoverableItem]:
        from .trash import scan_trash
        return self._add(scan_trash(progress, cancelled))

    def search_partitions(self, progress: Progress, cancelled: Cancelled) -> list:
        from .partitions import search_lost_partitions
        found = search_lost_partitions(self.disk, progress, cancelled)
        known = {(p.offset, p.length) for p in self.partitions}
        self.partitions += [p for p in found if (p.offset, p.length) not in known]
        self.partitions.sort(key=lambda p: p.offset)
        return found

    def _add(self, found: List[RecoverableItem]) -> List[RecoverableItem]:
        # A file found by both scans is kept once, with its real name from the quick scan.
        offset_base = self.partition.offset if self.partition is not None else 0
        taken = {(offset_base + it.first_offset) for it in self.items
                 if it.first_offset is not None and it.source != Source.TRASH}
        kept = []
        for it in found:
            off = it.first_offset
            if off is not None and it.source == Source.DEEP and (offset_base + off) in taken:
                continue
            if off is not None and it.source != Source.TRASH:
                taken.add(offset_base + off)
            kept.append(it)
        base = len(self.items)
        for i, item in enumerate(kept):
            item.id = base + i
        self.items.extend(kept)
        return kept

    # ---------------------------------------------------------------- checks
    def verify(self, items: List[RecoverableItem], progress: Progress, cancelled: Cancelled,
               workers: int = 4, cap: int = 256 * 1024 * 1024) -> dict:
        """Read each file and check its structure, without writing anything."""
        from .recovery import read_head
        from .verify import verify_bytes
        done = [0]
        counts = {"ok": 0, "damaged": 0, "unknown": 0}

        def one(it: RecoverableItem):
            if cancelled():
                return
            try:
                data = read_head(self.reader, it, min(it.size, cap))
                status, detail = verify_bytes(data, it.ext)
            except Exception as e:  # never let one file stop the batch
                status, detail = "unknown", f"Couldn't check: {e}"
            it.verified = status
            if detail and status != "ok":
                it.note = detail
            counts[status] = counts.get(status, 0) + 1
            done[0] += 1
            progress(done[0] / max(len(items), 1), f"Checked {done[0]:,} of {len(items):,} files")

        with ThreadPoolExecutor(max(1, workers)) as ex:
            list(ex.map(one, items))
        return counts

    # ---------------------------------------------------------------- save / load
    def save(self, path: str):
        def enc(it: RecoverableItem) -> dict:
            d = dict(it.__dict__)
            for k in ("created", "modified", "deleted_at"):
                d[k] = d[k].isoformat() if d[k] else None
            d["resident_data"] = base64.b64encode(it.resident_data).decode() if it.resident_data is not None else None
            d["extents"] = [list(e) for e in it.extents]
            return d

        doc = {
            "lifeline_session": SESSION_VERSION,
            "saved": datetime.now().isoformat(),
            "source_path": self.source_path,
            "display_name": self.display_name,
            "media": self.media,
            "partition": None if self.partition is None else {
                "offset": self.partition.offset, "length": self.partition.length,
                "fs": getattr(self.partition, "fs", None), "label": getattr(self.partition, "label", ""),
                "origin": getattr(self.partition, "origin", ""),
                "description": getattr(self.partition, "description", "")},
            "items": [enc(i) for i in self.items],
        }
        tmp = path + ".part"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str, source_override: Optional[str] = None) -> "ScanSession":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise DiskError(f"That isn't a Lifeline scan file ({e}).") from e
        if doc.get("lifeline_session") != SESSION_VERSION:
            raise DiskError("This scan was saved by a different version of Lifeline.")
        src = source_override or doc.get("source_path")
        part = None
        if doc.get("partition"):
            from .partitions import Partition
            p = doc["partition"]
            part = Partition(offset=p["offset"], length=p["length"], fs=p.get("fs"), label=p.get("label", ""),
                             origin=p.get("origin", ""), description=p.get("description", ""))
        if src and not os.path.exists(src):
            raise DiskError(f"The drive or image this scan came from isn't available: {src}")
        s = cls(src, doc.get("display_name"), partition=part, media=doc.get("media", "Unknown"))
        for d in doc["items"]:
            for k in ("created", "modified", "deleted_at"):
                d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
            d["resident_data"] = base64.b64decode(d["resident_data"]) if d.get("resident_data") is not None else None
            d["extents"] = [tuple(e) for e in d.get("extents", [])]
            known = RecoverableItem.__dataclass_fields__.keys()
            s.items.append(RecoverableItem(**{k: v for k, v in d.items() if k in known}))
        return s

    def close(self):
        if self.reader is not None and self.reader is not self.disk:
            self.reader.close()
        if self.disk is not None:
            self.disk.close()
