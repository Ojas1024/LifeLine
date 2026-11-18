"""FAT12/16/32 parser for finding deleted files (USB sticks, SD cards, cameras).

Deleting a file on FAT overwrites the first byte of its directory entry with
0xE5 and zeroes its cluster chain in the FAT. Everything else in the entry
survives: size, first cluster, timestamps, and the long-file-name entries in
front of it. With the chain gone we assume the file was stored in one piece
(true for most files written to a camera card or a fresh stick), and check the
FAT to see whether any of those clusters have been given to a new file since.

The lost first character of the 8.3 name is recovered from the long-name
checksum when long-name entries survive.
"""
from __future__ import annotations

import re
import struct
from array import array
from datetime import datetime
from typing import List, Optional, Set, Tuple

from .disk import DiskReader
from .filesystems import FileSystem, FSError
from .models import (Cancelled, Condition, FreeSpaceFilter, OnFound, Progress, RecoverableItem,
                     Source)

DELETED = 0xE5
ATTR_VOLUME = 0x08
ATTR_DIR = 0x10
ATTR_LFN = 0x0F
MAX_DIR_CLUSTERS = 4096   # a directory bigger than this is almost certainly a corrupt chain
MAX_DELETED_DIR_CLUSTERS = 256


class FATError(FSError):
    pass


def dos_datetime(date: int, time: int, tenths: int = 0) -> Optional[datetime]:
    if not date:
        return None
    try:
        return datetime(1980 + (date >> 9), (date >> 5) & 0x0F, date & 0x1F,
                        time >> 11, (time >> 5) & 0x3F, min((time & 0x1F) * 2 + tenths // 100, 59))
    except ValueError:
        return None


def lfn_checksum(short: bytes) -> int:
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def short_name_text(raw: bytes, case_flags: int = 0) -> str:
    base = raw[:8].decode("cp437", "replace").rstrip()
    ext = raw[8:11].decode("cp437", "replace").rstrip()
    # Windows NT stores all-lowercase 8.3 names without a long name, using these two flag bits.
    if case_flags & 0x08:
        base = base.lower()
    if case_flags & 0x10:
        ext = ext.lower()
    return f"{base}.{ext}" if ext else base


def _lfn_chars(e: bytes) -> bytes:
    return e[1:11] + e[14:26] + e[28:32]


class _Entry:
    __slots__ = ("name", "attr", "cluster", "size", "created", "modified", "deleted", "offset")

    def __init__(self, name, attr, cluster, size, created, modified, deleted, offset):
        self.name, self.attr, self.cluster, self.size = name, attr, cluster, size
        self.created, self.modified, self.deleted, self.offset = created, modified, deleted, offset


def parse_dir_entries(data: bytes, base_offset: int) -> List[_Entry]:
    """Turn raw directory bytes into entries (live and deleted), long names joined."""
    out: List[_Entry] = []
    lfn: List[bytes] = []          # parts in on-disk order (last part first)
    lfn_sum: Optional[int] = None
    for pos in range(0, len(data) - 31, 32):
        e = data[pos:pos + 32]
        first = e[0]
        if first == 0x00:
            break  # end of directory
        attr = e[11]
        if attr == ATTR_LFN and e[26:28] == b"\x00\x00":
            if lfn_sum is not None and e[13] != lfn_sum:
                lfn = []
            lfn.append(_lfn_chars(e))
            lfn_sum = e[13]
            continue
        parts, chk = lfn, lfn_sum
        lfn, lfn_sum = [], None
        if attr & ATTR_VOLUME or e[0:2] == b". " or e[0:3] == b".. " or first == 0x2E:
            continue
        deleted = first == DELETED
        short = bytearray(e[:11])
        if short[0] == 0x05:
            short[0] = 0xE5  # 0x05 stands in for a real 0xE5 first character
        name = None
        if parts and chk is not None:
            if deleted:
                # Find the original first character: the one that makes the checksum match.
                for c in range(0x20, 0x100):
                    short[0] = c
                    if lfn_checksum(bytes(short)) == chk:
                        break
                else:
                    short[0] = ord("_")
            if lfn_checksum(bytes(short)) == chk:
                raw = b"".join(reversed(parts))
                name = raw.decode("utf-16-le", "replace").split("\x00")[0].rstrip("￿")
        elif deleted:
            short[0] = ord("_")
        if not name:
            name = short_name_text(bytes(short), e[12])
        if not name.strip(" ."):
            continue
        hi, ctime, cdate = struct.unpack_from("<HHH", e, 20)[0], *struct.unpack_from("<HH", e, 14)
        mtime, mdate, lo, size = struct.unpack_from("<HHHI", e, 22)
        created = dos_datetime(cdate, ctime, e[13])
        out.append(_Entry(name, attr, (hi << 16) | lo, size, created, dos_datetime(mdate, mtime),
                          deleted, base_offset + pos))
    return out


class FATVolume(FileSystem):
    def __init__(self, reader: DiskReader):
        super().__init__(reader)
        b = reader.read_at(0, 512)
        if len(b) < 512 or b[0] not in (0xEB, 0xE9) or b[3:11] in (b"NTFS    ", b"EXFAT   "):
            raise FATError("Not a FAT volume.")
        bps, spc, reserved, nfats, root_entries, total16, _media, fatsz16 = struct.unpack_from("<HBHBHHBH", b, 11)
        total32, fatsz32 = struct.unpack_from("<II", b, 32)
        if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1) or not reserved or nfats not in (1, 2):
            raise FATError("Not a FAT volume.")
        fatsz = fatsz16 or fatsz32
        total = total16 or total32
        if not fatsz or not total:
            raise FATError("Not a FAT volume.")
        self.bytes_per_sector = bps
        self.cluster_size = bps * spc
        root_sectors = (root_entries * 32 + bps - 1) // bps
        self.fat_offset = reserved * bps
        self.root_offset = (reserved + nfats * fatsz) * bps
        self.root_size = root_sectors * bps
        self.data_start = self.root_offset + self.root_size
        self.cluster_count = (total * bps - self.data_start) // self.cluster_size
        if self.cluster_count <= 0:
            raise FATError("Not a FAT volume.")
        if self.cluster_count < 4085:
            self.bits = 12
        elif self.cluster_count < 65525:
            self.bits = 16
        else:
            self.bits = 32
        if self.bits == 32 and (fatsz16 or root_entries):
            raise FATError("Inconsistent FAT boot sector.")
        self.name = f"FAT{self.bits}"
        self.volume_size = total * bps
        self.root_cluster = struct.unpack_from("<I", b, 44)[0] if self.bits == 32 else 0
        label_off = 71 if self.bits == 32 else 43
        self.label = b[label_off:label_off + 11].decode("cp437", "replace").strip()
        if self.label.upper() == "NO NAME":
            self.label = ""
        self.eoc = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.bits]
        self._fat = self._load_fat(fatsz * bps)
        self._used = self._used_flags()   # one byte per cluster number: 1 = allocated
        self._bitmap: Optional[bytes] = None
        self._filter: Optional[FreeSpaceFilter] = None

    # ---------------------------------------------------------------- FAT
    def _load_fat(self, size: int) -> array:
        need = {12: (self.cluster_count + 2) * 3 // 2 + 2, 16: (self.cluster_count + 2) * 2,
                32: (self.cluster_count + 2) * 4}[self.bits]
        raw = self.reader.read_at(self.fat_offset, min(size, need + 4))
        if self.bits == 12:
            fat = array("I")
            for n in range(self.cluster_count + 2):
                i = n * 3 // 2
                if i + 1 >= len(raw):
                    fat.append(0)
                    continue
                v = raw[i] | raw[i + 1] << 8
                fat.append(v >> 4 if n & 1 else v & 0xFFF)
            return fat
        code = "H" if self.bits == 16 else "I"
        fat = array(code)
        width = fat.itemsize
        raw = raw[:len(raw) - len(raw) % width]
        fat.frombytes(raw)
        if struct.pack("=H", 1) != struct.pack("<H", 1):
            fat.byteswap()
        return fat

    def _entry(self, n: int) -> int:
        if n >= len(self._fat):
            return 0
        v = self._fat[n]
        return v & 0x0FFFFFFF if self.bits == 32 else v

    def _used_flags(self) -> bytes:
        """bytes indexed by cluster number, 1 where the FAT entry is non-zero. Built with
        slicing and big-int ORs so a 32 GB card doesn't need a per-cluster Python loop."""
        n = self.cluster_count + 2
        if self.bits == 12:
            flags = bytes(1 if self._entry(i) else 0 for i in range(n))
        else:
            raw = self._fat.tobytes() if struct.pack("=H", 1) == struct.pack("<H", 1) else None
            if raw is None:
                flags = bytes(1 if self._entry(i) else 0 for i in range(min(n, len(self._fat))))
            else:
                width = self._fat.itemsize
                count = len(raw) // width
                acc = 0
                mask_top = bytes(range(16)) * 16  # keep low nibble only (FAT32 top 4 bits are reserved)
                for k in range(width):
                    col = raw[k::width]
                    if self.bits == 32 and k == 3:
                        col = col.translate(mask_top)
                    acc |= int.from_bytes(col, "little")
                merged = acc.to_bytes(count, "little")
                flags = merged.translate(bytes([0] + [1] * 255))
            flags = flags[:n]
        flags = b"\x01\x01" + flags[2:]
        return flags + b"\x01" * (n - len(flags))

    def chain(self, start: int, limit: int = 1 << 24) -> List[int]:
        out, seen, c = [], set(), start
        while 2 <= c < self.cluster_count + 2 and c not in seen and len(out) < limit:
            seen.add(c)
            out.append(c)
            c = self._entry(c)
            if c >= self.eoc or c in (0, 1) or c == self.eoc - 1:
                break
        return out

    def cluster_offset(self, c: int) -> int:
        return self.data_start + (c - 2) * self.cluster_size

    def _clusters_to_extents(self, clusters: List[int], size: int) -> List[Tuple[Optional[int], int]]:
        extents, remaining, i = [], size, 0
        while i < len(clusters) and remaining > 0:
            j = i
            while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1:
                j += 1
            n = min((j - i + 1) * self.cluster_size, remaining)
            extents.append((self.cluster_offset(clusters[i]), n))
            remaining -= n
            i = j + 1
        return extents

    # ---------------------------------------------------------------- free space
    def _packed_bitmap(self) -> bytes:
        """LSB-first bitmap of clusters 2.. (1 = in use), for FreeSpaceFilter."""
        if self._bitmap is None:
            flags = self._used[2:]
            flags += b"\x01" * (-len(flags) % 8)
            acc = 0
            for k in range(8):
                acc |= int.from_bytes(flags[k::8].translate(bytes([0, 1 << k]) + bytes(254)), "little")
            self._bitmap = acc.to_bytes(len(flags) // 8, "little")
        return self._bitmap

    def free_filter(self) -> Optional[FreeSpaceFilter]:
        if self._filter is None:
            self._filter = FreeSpaceFilter(self._packed_bitmap(), self.cluster_size, self.data_start, 2)
        return self._filter

    def is_free(self, offset: int) -> bool:
        return self.free_filter()(offset)

    def free_regions(self) -> List[Tuple[int, int]]:
        bm = self._packed_bitmap()
        span = 8 * self.cluster_size
        return [(self.data_start + m.start() * span, min(self.data_start + m.end() * span, self.volume_size))
                for m in re.finditer(rb"[^\xff]+", bm)]

    def _reused_fraction(self, first: int, n: int) -> float:
        if n <= 0:
            return 0.0
        hi = self.cluster_count + 2
        inside = self._used[first:min(first + n, hi)]
        used = inside.count(1) + (n - len(inside))
        return used / n

    # ---------------------------------------------------------------- directories
    def _read_dir(self, cluster: int, deleted: bool, visited: Set[int]) -> Optional[Tuple[bytes, int]]:
        """Bytes of a directory (and its volume offset). For deleted folders the chain is
        gone, so read forward cluster by cluster while it still looks like that folder."""
        if cluster == 0 and self.bits != 32:
            return self.reader.read_at(self.root_offset, self.root_size), self.root_offset
        if not 2 <= cluster < self.cluster_count + 2 or cluster in visited:
            return None
        if not deleted and self._entry(cluster) != 0:
            clusters = self.chain(cluster, MAX_DIR_CLUSTERS)
        else:
            clusters = [cluster]
        visited.update(clusters)
        start = self.cluster_offset(clusters[0])
        if len(clusters) == 1 and deleted:
            first = self.reader.read_at(start, self.cluster_size)
            # A real folder begins with its "." entry pointing at itself.
            dot_cluster = struct.unpack_from("<H", first, 20)[0] << 16 | struct.unpack_from("<H", first, 26)[0]
            if first[:11] != b".          " or dot_cluster != cluster:
                return None
            parts = [first]
            c = cluster
            while not _has_end(parts[-1]) and len(parts) < MAX_DELETED_DIR_CLUSTERS:
                c += 1
                if c >= self.cluster_count + 2 or self._used[c] or c in visited:
                    break
                nxt = self.reader.read_at(self.cluster_offset(c), self.cluster_size)
                if not _looks_like_dir(nxt):
                    break
                visited.add(c)
                parts.append(nxt)
            return b"".join(parts), start
        # Entry offsets are only used for reporting, so the first cluster's offset is enough.
        data = b"".join(self.reader.read_at(self.cluster_offset(c), self.cluster_size) for c in clusters)
        return data, start

    # ---------------------------------------------------------------- scan
    def scan_deleted(self, progress: Progress, cancelled: Cancelled, on_found: Optional[OnFound] = None,
                     include_existing: bool = False, workers: int = 1) -> List[RecoverableItem]:
        items: List[RecoverableItem] = []
        visited: Set[int] = set()
        stack: List[Tuple[int, str, bool]] = [(self.root_cluster, "", False)]
        dirs_done = 0
        used_estimate = max(self.cluster_count - self._used.count(0), 1)
        while stack:
            if cancelled():
                break
            cluster, folder, parent_deleted = stack.pop()
            got = self._read_dir(cluster, parent_deleted, visited)
            dirs_done += 1
            if got is None:
                continue
            data, base = got
            for e in parse_dir_entries(data, base):
                is_deleted = e.deleted or parent_deleted
                path = f"{folder}/{e.name}" if folder else e.name
                if e.attr & ATTR_DIR:
                    if e.cluster and e.cluster != cluster:
                        stack.append((e.cluster, path, is_deleted))
                    continue
                if not is_deleted and not include_existing:
                    continue
                item = self._make_item(e, folder, is_deleted)
                if item is not None:
                    item.id = len(items)
                    items.append(item)
                    if on_found and len(items) % 100 == 0:
                        on_found(len(items))
            progress(min(len(visited) / used_estimate, 0.99), f"Reading folders: {dirs_done:,} so far")
        progress(1.0, f"Read {dirs_done:,} folders")
        if on_found:
            on_found(len(items))
        return items

    def _make_item(self, e: _Entry, folder: str, deleted: bool) -> Optional[RecoverableItem]:
        size = e.size
        common = dict(id=0, name=e.name, folder=folder, size=size, source=Source.QUICK,
                      created=e.created, modified=e.modified, mft_record=e.offset, deleted=deleted)
        if size == 0 or e.cluster == 0:
            if size:
                return None  # size but no data location: nothing to read
            return RecoverableItem(resident_data=b"", condition=Condition.GOOD, **common)
        if not 2 <= e.cluster < self.cluster_count + 2:
            return None
        n = -(-size // self.cluster_size)
        if not deleted:
            clusters = self.chain(e.cluster, n + 1)
            extents = self._clusters_to_extents(clusters, size)
            pieces = len(extents)
            return RecoverableItem(extents=extents, condition=Condition.GOOD,
                                   note=f"Fragmented: {pieces} pieces" if pieces > 1 else "", **common)
        if e.cluster + n > self.cluster_count + 2:
            n_avail = self.cluster_count + 2 - e.cluster
            size = min(size, n_avail * self.cluster_size)
            common["size"] = size
        extents = [(self.cluster_offset(e.cluster), size)]
        pct = self._reused_fraction(e.cluster, n)
        cond = Condition.GOOD if pct == 0 else Condition.OVERWRITTEN if pct >= 0.999 else Condition.PARTIAL
        return RecoverableItem(extents=extents, condition=cond, overwritten_pct=pct, **common)


def _has_end(data: bytes) -> bool:
    return any(data[i] == 0 for i in range(0, len(data) - 31, 32))


def _looks_like_dir(data: bytes) -> bool:
    """Every 32-byte slot is an end marker, a long-name piece, or a plausible short entry."""
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        if e[0] == 0:
            return True
        attr = e[11]
        if attr == ATTR_LFN:
            continue
        if attr & 0xC0 or e[0] < 0x20 and e[0] != 0x05:
            return False
    return True
