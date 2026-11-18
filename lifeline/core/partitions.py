"""Partition tables (MBR with logical partitions, GPT) and lost-partition search.

A deleted or damaged partition table doesn't touch the partitions themselves:
each file system still starts with its boot sector, and NTFS keeps a backup
boot sector in its last sector. Searching a whole disk for those finds
partitions that no longer appear in the table, so their files can be read
again. We only look where partitions realistically start (every 1 MiB, the
old 63-sector and cylinder boundaries), which keeps the search fast.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional

from .disk import DiskReader
from .filesystems import FSError, sniff
from .models import Cancelled, Progress

MiB = 1024 * 1024
SECTOR = 512
CYLINDER = 255 * 63 * SECTOR
EXTENDED_TYPES = (0x05, 0x0F, 0x85)


@dataclass
class Partition:
    offset: int
    length: int
    fs: Optional[str]
    label: str
    origin: str          # "MBR", "GPT" or "Found"
    description: str


def _sub_reader(reader: DiskReader, offset: int, length: int) -> DiskReader:
    length = max(0, min(length, reader.length - offset))
    return DiskReader(reader.path, base_offset=reader.base_offset + offset, length=length)


def _describe(fs: Optional[str], length: int, extra: str) -> str:
    from .models import human_size
    return f"{fs or 'Unknown'} partition, {human_size(length)}" + (f" ({extra})" if extra else "")


def read_partition_table(reader: DiskReader) -> List[Partition]:
    """Partitions listed in the MBR (including logical ones) or GPT."""
    mbr = reader.read_at(0, SECTOR)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        return []
    if sniff(mbr) is not None:
        return []  # a bare volume (superfloppy), not a partition table
    parts: List[Partition] = []
    types = [mbr[0x1BE + i * 16 + 4] for i in range(4)]
    if 0xEE in types:
        for sector in (512, 4096):
            gpt = _read_gpt(reader, sector)
            if gpt:
                return gpt
    for i in range(4):
        e = mbr[0x1BE + i * 16:0x1BE + (i + 1) * 16]
        ptype = e[4]
        lba, sectors = struct.unpack_from("<II", e, 8)
        if not ptype or not sectors or ptype == 0xEE:
            continue
        if ptype in EXTENDED_TYPES:
            parts.extend(_read_ebr_chain(reader, lba))
            continue
        parts.append(_make(reader, lba * SECTOR, sectors * SECTOR, "MBR", f"Partition {i + 1}, type 0x{ptype:02X}"))
    return parts


def _read_ebr_chain(reader: DiskReader, ext_lba: int) -> List[Partition]:
    out, ebr_lba, seen, n = [], ext_lba, set(), 5
    while ebr_lba not in seen and len(seen) < 128:
        seen.add(ebr_lba)
        ebr = reader.read_at(ebr_lba * SECTOR, SECTOR)
        if len(ebr) < SECTOR or ebr[510:512] != b"\x55\xaa":
            break
        e1, e2 = ebr[0x1BE:0x1CE], ebr[0x1CE:0x1DE]
        lba, sectors = struct.unpack_from("<II", e1, 8)
        if e1[4] and sectors:
            out.append(_make(reader, (ebr_lba + lba) * SECTOR, sectors * SECTOR, "MBR",
                             f"Logical partition {n}, type 0x{e1[4]:02X}"))
            n += 1
        nxt = struct.unpack_from("<I", e2, 8)[0]
        if e2[4] not in EXTENDED_TYPES or not nxt:
            break
        ebr_lba = ext_lba + nxt
    return out


def _read_gpt(reader: DiskReader, sector: int) -> List[Partition]:
    hdr = reader.read_at(sector, 512)
    if hdr[:8] != b"EFI PART":
        return []
    entries_lba, count, esize = struct.unpack_from("<QII", hdr, 0x48)
    if not 128 <= esize <= 4096 or count > 4096:
        return []
    table = reader.read_at(entries_lba * sector, count * esize)
    parts = []
    for i in range(count):
        e = table[i * esize:(i + 1) * esize]
        if len(e) < 0x38 or e[:16] == b"\x00" * 16:
            continue
        first, last = struct.unpack_from("<QQ", e, 0x20)
        if last < first:
            continue
        name = e[0x38:0x38 + 72].decode("utf-16-le", "ignore").rstrip("\x00")
        parts.append(_make(reader, first * sector, (last - first + 1) * sector, "GPT", name or f"GPT partition {i + 1}"))
    return parts


def _make(reader: DiskReader, offset: int, length: int, origin: str, extra: str) -> Partition:
    boot = reader.read_at(offset, SECTOR)
    fs = sniff(boot)
    label = ""
    if fs:
        try:
            vol = _open_fs(reader, offset, length, fs)
            label = vol.label if vol else ""
            if vol:
                fs = vol.name
                vol.reader.close()
        except Exception:
            pass
    return Partition(offset, length, fs, label, origin, _describe(fs, length, extra))


def _open_fs(reader: DiskReader, offset: int, length: int, fs: str):
    from .exfat import ExFATVolume
    from .fat import FATVolume
    from .ntfs import NTFSVolume
    cls = {"NTFS": NTFSVolume, "exFAT": ExFATVolume}.get(fs, FATVolume)
    sub = _sub_reader(reader, offset, length)
    try:
        return cls(sub)
    except FSError:
        sub.close()
        return None


def _boot_length(boot: bytes, fs: str) -> int:
    """Volume size as the boot sector states it."""
    if fs == "NTFS":
        bps = struct.unpack_from("<H", boot, 0x0B)[0]
        return (struct.unpack_from("<Q", boot, 0x28)[0] + 1) * bps  # +1: the backup boot sector
    if fs == "exFAT":
        return struct.unpack_from("<Q", boot, 72)[0] << boot[108]
    bps = struct.unpack_from("<H", boot, 11)[0]
    total = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
    return total * bps


def _candidate_offsets(length: int) -> List[int]:
    offs = set(range(0, length, MiB))
    offs.add(63 * SECTOR)
    offs.update(range(CYLINDER + 0, length, CYLINDER))           # cylinder-aligned starts
    offs.update(o + 63 * SECTOR for o in range(0, length, CYLINDER))  # first-head-of-cylinder starts
    return sorted(o for o in offs if o < length)


def search_lost_partitions(reader: DiskReader, progress: Progress, cancelled: Cancelled) -> List[Partition]:
    """Find file systems on a whole disk, including ones missing from the partition table."""
    table = read_partition_table(reader)
    known: Dict[int, Partition] = {p.offset: p for p in table}
    found: Dict[int, Partition] = {}
    offsets = _candidate_offsets(reader.length)
    total = len(offsets)

    def consider(start: int, boot: bytes, fs: str, how: str):
        if start < 0 or start in found or start >= reader.length:
            return
        length = min(_boot_length(boot, fs), reader.length - start)
        if length <= 0:
            return
        if start in known:
            found[start] = known[start]
            return
        vol = None
        try:
            if fs == "NTFS" and how == "backup":
                from .ntfs import NTFSVolume
                sub = _sub_reader(reader, start, length)
                try:
                    vol = NTFSVolume(sub)
                except FSError:
                    sub.close()
            else:
                vol = _open_fs(reader, start, length, fs)
        except Exception:
            vol = None
        if vol is None:
            return
        extra = "found by searching" + (", from its backup boot sector" if how == "backup" else "")
        found[start] = Partition(start, length, vol.name, vol.label, "Found", _describe(vol.name, length, extra))
        vol.reader.close()

    for i, off in enumerate(offsets):
        if cancelled():
            break
        # One read covers the sector before the boundary (an NTFS backup boot sector
        # at the end of the previous partition) and the sector at it (a primary boot sector).
        lo = max(off - SECTOR, 0)
        data = reader.read_at(lo, off + SECTOR - lo)
        for at in (off, off - SECTOR):
            if at < 0 or at - lo + SECTOR > len(data):
                continue
            boot = data[at - lo:at - lo + SECTOR]
            fs = sniff(boot)
            if not fs:
                continue
            if fs == "NTFS":
                total_sectors = struct.unpack_from("<Q", boot, 0x28)[0]
                bps = struct.unpack_from("<H", boot, 0x0B)[0] or SECTOR
                consider(at, boot, fs, "primary")
                if at not in found:  # not a usable primary, so maybe the copy at a volume's end
                    consider(at - total_sectors * bps, boot, fs, "backup")
            else:
                consider(at, boot, fs, "primary")
        if i % 256 == 0:
            progress(i / max(total, 1), f"Looking for partitions: {off / (1 << 30):,.1f} GB checked")
    progress(1.0, f"Found {len(found)} partition(s)")
    for off, p in known.items():
        found.setdefault(off, p)
    return sorted(found.values(), key=lambda p: p.offset)
