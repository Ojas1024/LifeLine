"""exFAT parser for finding deleted files (SD cards over 32 GB, modern USB drives).

exFAT describes each file with an entry set: a File entry (attributes and
timestamps), a Stream Extension (size and first cluster) and one or more File
Name entries. Deleting a file clears the "in use" bit (0x80) in each entry's
type byte and frees its clusters in the allocation bitmap. Everything else
stays. Files written in one piece carry a NoFatChain flag, so their layout is
known exactly; for the rest we follow the FAT chain if it still looks intact,
otherwise we assume one piece.
"""
from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta
from typing import List, Optional, Set, Tuple

from .disk import DiskReader
from .filesystems import FileSystem, FSError
from .models import (Cancelled, Condition, FreeSpaceFilter, OnFound, Progress, RecoverableItem,
                     Source)
from .ntfs import _count_bits, count_pieces

T_BITMAP, T_UPCASE, T_LABEL = 0x81, 0x82, 0x83
T_FILE, T_STREAM, T_NAME = 0x05, 0x40, 0x41   # with the in-use bit (0x80) cleared
IN_USE = 0x80
ATTR_DIR = 0x10
FLAG_NO_FAT_CHAIN = 0x02
MAX_DIR_BYTES = 256 * 1024 * 1024


class ExFATError(FSError):
    pass


def exfat_time(stamp: int, ten_ms: int = 0, utc_offset: int = 0) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        dt = datetime(1980 + (stamp >> 25), (stamp >> 21) & 0x0F, (stamp >> 16) & 0x1F,
                      (stamp >> 11) & 0x1F, (stamp >> 5) & 0x3F, (stamp & 0x1F) * 2)
    except ValueError:
        return None
    dt += timedelta(milliseconds=10 * min(ten_ms, 199))
    if utc_offset & 0x80:  # offset is valid: convert local time to UTC like NTFS times
        minutes = ((utc_offset & 0x7F) ^ 0x40) - 0x40
        dt -= timedelta(minutes=15 * minutes)
    return dt


class _Set:
    __slots__ = ("name", "attr", "cluster", "size", "contiguous", "created", "modified", "deleted", "offset")


def parse_entry_sets(data: bytes, base_offset: int) -> List[_Set]:
    """Decode File entry sets (live and deleted) from raw directory bytes."""
    out: List[_Set] = []
    n = len(data) // 32
    i = 0
    while i < n:
        e = data[i * 32:i * 32 + 32]
        t = e[0]
        if t == 0x00:
            break
        if t & 0x7F != T_FILE:
            i += 1
            continue
        live = bool(t & IN_USE)
        count = e[1]
        if not 2 <= count <= 18 or i + count >= n + 1:
            i += 1
            continue
        stream = data[(i + 1) * 32:(i + 2) * 32]
        if len(stream) < 32 or stream[0] != (T_STREAM | (IN_USE if live else 0)):
            i += 1
            continue
        name_len = stream[3]
        parts = []
        for k in range(2, count + 1):
            ne = data[(i + k) * 32:(i + k + 1) * 32]
            if len(ne) < 32 or ne[0] != (T_NAME | (IN_USE if live else 0)):
                break
            parts.append(ne[2:32])
        name = b"".join(parts).decode("utf-16-le", "replace")[:name_len]
        if not name or len(name) < name_len:
            i += 1
            continue
        s = _Set()
        s.name, s.deleted, s.offset = name, not live, base_offset + i * 32
        s.attr = struct.unpack_from("<H", e, 4)[0]
        create, modify = struct.unpack_from("<II", e, 8)
        s.created = exfat_time(create, e[20], e[22])
        s.modified = exfat_time(modify, e[21], e[23])
        flags = stream[1]
        s.contiguous = bool(flags & FLAG_NO_FAT_CHAIN)
        s.cluster, = struct.unpack_from("<I", stream, 20)
        s.size, = struct.unpack_from("<Q", stream, 24)
        out.append(s)
        i += count + 1
    return out


class ExFATVolume(FileSystem):
    name = "exFAT"

    def __init__(self, reader: DiskReader):
        super().__init__(reader)
        b = reader.read_at(0, 512)
        if len(b) < 512 or b[3:11] != b"EXFAT   ":
            raise ExFATError("Not an exFAT volume.")
        vol_len, fat_off, fat_len, heap_off, count, root = struct.unpack_from("<QIIIII", b, 72)
        bps_shift, spc_shift = b[108], b[109]
        if not 9 <= bps_shift <= 12 or bps_shift + spc_shift > 25 or not count or root < 2:
            raise ExFATError("The exFAT boot sector looks damaged.")
        self.bytes_per_sector = 1 << bps_shift
        self.cluster_size = 1 << (bps_shift + spc_shift)
        self.volume_size = vol_len * self.bytes_per_sector
        self.fat_offset = fat_off * self.bytes_per_sector
        self.data_start = heap_off * self.bytes_per_sector
        self.cluster_count = count
        self.root_cluster = root
        fat_bytes = min(fat_len * self.bytes_per_sector, (count + 2) * 4)
        raw = reader.read_at(self.fat_offset, fat_bytes)
        self._fat = memoryview(raw[:len(raw) - len(raw) % 4]).cast("I")
        self.label = ""
        self._bitmap: Optional[bytes] = None
        root_data = self._read_chain_bytes(root, MAX_DIR_BYTES)
        for i in range(0, len(root_data) - 31, 32):
            t = root_data[i]
            if t == 0:
                break
            if t == T_BITMAP and not root_data[i + 1] & 1:
                first, length = struct.unpack_from("<IQ", root_data, i + 20)
                self._bitmap = self._read_contiguous_or_chain(first, length)
            elif t == T_LABEL:
                n = min(root_data[i + 1], 11)
                self.label = root_data[i + 2:i + 2 + n * 2].decode("utf-16-le", "replace")
        if self._bitmap is None:
            raise ExFATError("The exFAT allocation bitmap is missing.")
        self._filter = FreeSpaceFilter(self._bitmap, self.cluster_size, self.data_start, 2)

    # ---------------------------------------------------------------- clusters
    def _next(self, c: int) -> int:
        return self._fat[c] if c < len(self._fat) else 0

    def chain(self, start: int, limit: int) -> List[int]:
        out, seen, c = [], set(), start
        while 2 <= c < self.cluster_count + 2 and c not in seen and len(out) < limit:
            seen.add(c)
            out.append(c)
            c = self._next(c)
        return out

    def cluster_offset(self, c: int) -> int:
        return self.data_start + (c - 2) * self.cluster_size

    def _read_chain_bytes(self, start: int, limit: int) -> bytes:
        clusters = self.chain(start, max(1, limit // self.cluster_size))
        return b"".join(self.reader.read_at(self.cluster_offset(c), self.cluster_size) for c in clusters)

    def _read_contiguous_or_chain(self, first: int, length: int) -> bytes:
        n = -(-length // self.cluster_size)
        chain = self.chain(first, n)
        if len(chain) == n:
            return b"".join(self.reader.read_at(self.cluster_offset(c), self.cluster_size) for c in chain)[:length]
        return self.reader.read_at(self.cluster_offset(first), length)

    def _layout(self, s: _Set) -> List[Tuple[Optional[int], int]]:
        """Where the file's bytes are. Exact for NoFatChain files; for chained files use
        the FAT if the chain is intact, otherwise assume one piece."""
        n = -(-s.size // self.cluster_size)
        if s.contiguous or n <= 1:
            return [(self.cluster_offset(s.cluster), s.size)]
        chain = self.chain(s.cluster, n)
        if len(chain) == n and (not s.deleted or self._chain_is_free(chain)):
            extents, remaining, i = [], s.size, 0
            while i < len(chain) and remaining > 0:
                j = i
                while j + 1 < len(chain) and chain[j + 1] == chain[j] + 1:
                    j += 1
                k = min((j - i + 1) * self.cluster_size, remaining)
                extents.append((self.cluster_offset(chain[i]), k))
                remaining -= k
                i = j + 1
            return extents
        return [(self.cluster_offset(s.cluster), s.size)]

    def _chain_is_free(self, chain: List[int]) -> bool:
        bm = self._bitmap
        return all(not (bm[(c - 2) // 8] >> ((c - 2) % 8)) & 1 for c in chain if (c - 2) // 8 < len(bm))

    # ---------------------------------------------------------------- free space
    def free_filter(self) -> Optional[FreeSpaceFilter]:
        return self._filter

    def is_free(self, offset: int) -> bool:
        return self._filter(offset)

    def free_regions(self) -> List[Tuple[int, int]]:
        span = 8 * self.cluster_size
        return [(self.data_start + m.start() * span, min(self.data_start + m.end() * span, self.volume_size))
                for m in re.finditer(rb"[^\xff]+", self._bitmap)]

    def _reused_fraction(self, extents) -> float:
        used = total = 0
        for off, length in extents:
            first = (off - self.data_start) // self.cluster_size
            n = -(-length // self.cluster_size)
            total += n
            used += _count_bits(self._bitmap, first, n)
        return used / total if total else 0.0

    # ---------------------------------------------------------------- scan
    def _read_dir(self, s: Optional[_Set], visited: Set[int]) -> Optional[Tuple[bytes, int]]:
        if s is None:
            if self.root_cluster in visited:
                return None
            visited.add(self.root_cluster)
            return self._read_chain_bytes(self.root_cluster, MAX_DIR_BYTES), self.cluster_offset(self.root_cluster)
        if not 2 <= s.cluster < self.cluster_count + 2 or s.cluster in visited or not s.size:
            return None
        visited.add(s.cluster)
        size = min(s.size, MAX_DIR_BYTES)
        extents = self._layout(_with_size(s, size))
        data = b"".join(self.reader.read_at(o, n) for o, n in extents)
        return data, extents[0][0]

    def scan_deleted(self, progress: Progress, cancelled: Cancelled, on_found: Optional[OnFound] = None,
                     include_existing: bool = False, workers: int = 1) -> List[RecoverableItem]:
        items: List[RecoverableItem] = []
        visited: Set[int] = set()
        stack: List[Tuple[Optional[_Set], str, bool]] = [(None, "", False)]
        dirs_done = 0
        used = max(int.from_bytes(self._bitmap, "little").bit_count(), 1)
        while stack:
            if cancelled():
                break
            entry, folder, parent_deleted = stack.pop()
            got = self._read_dir(entry, visited)
            dirs_done += 1
            if got is None:
                continue
            data, base = got
            for s in parse_entry_sets(data, base):
                deleted = s.deleted or parent_deleted
                path = f"{folder}/{s.name}" if folder else s.name
                if s.attr & ATTR_DIR:
                    stack.append((s, path, deleted))
                    continue
                if not deleted and not include_existing:
                    continue
                item = self._make_item(s, folder, deleted)
                if item is not None:
                    item.id = len(items)
                    items.append(item)
                    if on_found and len(items) % 100 == 0:
                        on_found(len(items))
            progress(min(len(visited) * 4 / used, 0.99), f"Reading folders: {dirs_done:,} so far")
        progress(1.0, f"Read {dirs_done:,} folders")
        if on_found:
            on_found(len(items))
        return items

    def _make_item(self, s: _Set, folder: str, deleted: bool) -> Optional[RecoverableItem]:
        common = dict(id=0, name=s.name, folder=folder, size=s.size, source=Source.QUICK,
                      created=s.created, modified=s.modified, mft_record=s.offset, deleted=deleted)
        if s.size == 0:
            return RecoverableItem(resident_data=b"", condition=Condition.GOOD, **common)
        if not 2 <= s.cluster < self.cluster_count + 2:
            return None
        heap_end = self.cluster_offset(self.cluster_count + 2)
        extents = [(o, min(n, heap_end - o)) for o, n in self._layout(s) if o < heap_end]
        if not extents:
            return None
        common["size"] = sum(n for _, n in extents)
        pieces = count_pieces(extents)
        note = f"Fragmented: {pieces} pieces" if pieces > 1 else ""
        if not deleted:
            return RecoverableItem(extents=extents, condition=Condition.GOOD, note=note, **common)
        pct = self._reused_fraction(extents)
        cond = Condition.GOOD if pct == 0 else Condition.OVERWRITTEN if pct >= 0.999 else Condition.PARTIAL
        return RecoverableItem(extents=extents, condition=cond, overwritten_pct=pct, note=note, **common)


def _with_size(s: _Set, size: int) -> _Set:
    t = _Set()
    for k in _Set.__slots__:
        setattr(t, k, getattr(s, k))
    t.size = size
    return t
