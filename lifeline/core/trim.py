"""Spot files whose data the drive has already erased (SSD TRIM, or a secure wipe).

Deleting a file on an SSD makes the operating system send TRIM for its blocks. The SSD
then unmaps them and from that moment returns zeros for them to everyone, including
Lifeline. The file's record (name, folder, size, dates) is still there, so without this
check it would look perfectly recoverable until someone opened it.

We read up to three 4 KB samples per file (start, middle, end) through the file's real
cluster layout. Real files almost never start with 4 KB of zeros, so a zero start is a
reliable sign. Formats that do start with zeros (disk images and the like) are skipped.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from .disk import DiskReader
from .models import Cancelled, Condition, Progress, RecoverableItem

SAMPLE = 4096
# Formats that legitimately begin with a long run of zeros.
ZERO_START_OK = {"iso", "img", "ima", "dd", "raw", "bin", "dat", "vhd", "vhdx", "vmdk", "qcow2", "vdi", "dmg",
                 "swp", "sys", "001", "e01", "tib", "wim", "sparseimage", "pst", "ost", "edb", "mdf", "ldf"}


def _file_offset_to_disk(item: RecoverableItem, pos: int) -> Optional[int]:
    """Disk byte offset of byte `pos` of the file, or None if it falls in a sparse run."""
    for off, length in item.extents:
        if pos < length:
            return None if off is None else off + pos
        pos -= length
    return None


def _is_zero(b: bytes) -> bool:
    return bool(b) and not any(b)


def probe(reader: DiskReader, item: RecoverableItem) -> Optional[str]:
    """Condition.ERASED / PARTLY_ERASED, or None if the data looks present."""
    if not item.deleted or item.file_path is not None or item.resident_data is not None or not item.extents:
        return None
    if item.size <= 0 or item.ext in ZERO_START_OK or item.condition == Condition.OVERWRITTEN:
        return None
    first = _file_offset_to_disk(item, 0)
    if first is None:
        return None
    head = reader.read_at(first, min(SAMPLE, item.size))
    if not _is_zero(head):
        return None
    others = []
    for pos in (item.size // 2, max(0, item.size - SAMPLE)):
        if pos < SAMPLE:
            continue
        pos -= pos % SAMPLE
        at = _file_offset_to_disk(item, pos)
        if at is not None:
            others.append(reader.read_at(at, min(SAMPLE, item.size - pos)))
    if all(_is_zero(b) for b in others):
        return Condition.ERASED
    return Condition.PARTLY_ERASED


def mark_erased(reader: DiskReader, items: List[RecoverableItem], progress: Progress, cancelled: Cancelled,
                ssd: bool = False, workers: int = 8) -> int:
    """Probe every candidate item and update its condition. Returns how many look erased."""
    todo = [it for it in items if it.deleted and it.file_path is None and it.resident_data is None and it.extents]
    if not todo:
        return 0
    done, erased = [0], [0]
    who = "the SSD (TRIM)" if ssd else "the drive (on SSDs this is TRIM; elsewhere, a wipe)"

    def one(it: RecoverableItem):
        if cancelled():
            return
        try:
            cond = probe(reader, it)
        except Exception:
            cond = None
        if cond == Condition.ERASED:
            it.condition = cond
            it.note = f"Already erased by {who}: its data reads back as zeros. Only its name and details survive."
            erased[0] += 1
        elif cond == Condition.PARTLY_ERASED:
            it.condition = cond
            it.note = f"Part of this file was erased by {who} and reads back as zeros. The rest may still open."
            erased[0] += 1
        done[0] += 1
        if done[0] % 200 == 0 or done[0] == len(todo):
            progress(done[0] / len(todo), f"Checking which files the drive already erased: {done[0]:,} of {len(todo):,}")

    with ThreadPoolExecutor(max(1, workers)) as ex:
        list(ex.map(one, todo))
    return erased[0]
