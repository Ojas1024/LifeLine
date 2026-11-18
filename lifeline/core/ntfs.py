"""NTFS Master File Table parser for finding deleted files ("Quick scan").

When Windows deletes a file it clears the "in use" flag on its MFT record and
frees its clusters in $Bitmap, but the record (name, parent folder, size,
timestamps and cluster runs) stays until a new file reuses it. We read every
record, keep the ones that are no longer in use, rebuild their folder paths
from the parent references, and check $Bitmap to see whether their clusters
have been handed to another file since.

Parsing a large MFT is CPU-bound, so with workers > 1 the MFT is cut into
slices that worker processes parse independently. Each slice result is small
(directory names plus the candidate file records), and merging them gives
exactly the same answer as a single pass.
"""
from __future__ import annotations

import re
import struct
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .disk import DiskReader
from .filesystems import FileSystem, FSError
from .models import (Cancelled, Condition, FreeSpaceFilter, OnFound, Progress, RecoverableItem,
                     Source)

ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_VOLUME_NAME = 0x60
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

FLAG_IN_USE = 0x01
FLAG_DIRECTORY = 0x02

ROOT_RECORD = 5
VOLUME_RECORD = 3
BITMAP_RECORD = 6
FIRST_USER_RECORD = 16  # records 0-15 are NTFS metadata files

SLICE_BYTES = 16 * 1024 * 1024  # MFT bytes per parallel work unit
_REF_MASK = 0xFFFFFFFFFFFF
_EPOCH = datetime(1601, 1, 1)


class NTFSError(FSError):
    pass


def filetime_to_dt(ft: int) -> Optional[datetime]:
    if not ft:
        return None
    try:
        return _EPOCH + timedelta(microseconds=ft // 10)
    except OverflowError:
        return None


def decode_runs(buf: bytes) -> List[Tuple[Optional[int], int]]:
    """Decode an NTFS mapping-pairs array into [(lcn or None for sparse, cluster_count)]."""
    runs, pos, lcn = [], 0, 0
    while pos < len(buf):
        header = buf[pos]
        if header == 0:
            break
        len_size, off_size = header & 0x0F, header >> 4
        pos += 1
        if len_size == 0 or pos + len_size + off_size > len(buf):
            break
        length = int.from_bytes(buf[pos:pos + len_size], "little")
        pos += len_size
        if off_size == 0:
            runs.append((None, length))
        else:
            lcn += int.from_bytes(buf[pos:pos + off_size], "little", signed=True)
            pos += off_size
            if lcn < 0:
                break
            runs.append((lcn, length))
    return runs


@dataclass
class _DataFragment:
    start_vcn: int
    runs: List[Tuple[Optional[int], int]]
    real_size: Optional[int]  # only present on the first fragment


@dataclass
class _Record:
    number: int
    in_use: bool
    is_dir: bool
    base_ref: int
    names: List[Tuple[int, str, int]]          # (namespace, name, parent_record)
    created: Optional[datetime] = None
    modified: Optional[datetime] = None
    resident: Optional[bytes] = None
    fragments: List[_DataFragment] = field(default_factory=list)
    size_hint: int = 0
    attr_list: Optional[bytes] = None           # resident $ATTRIBUTE_LIST value
    attr_list_frag: Optional[_DataFragment] = None  # non-resident $ATTRIBUTE_LIST
    volume_name: Optional[str] = None

    def best_name(self) -> Optional[Tuple[str, int]]:
        if not self.names:
            return None
        # Prefer Win32 (1) or Win32&DOS (3), then POSIX (0); DOS 8.3 names (2) last.
        order = {1: 0, 3: 0, 0: 1, 2: 2}
        ns, name, parent = min(self.names, key=lambda n: order.get(n[0], 3))
        return name, parent


# --------------------------------------------------------------------------
# Record parsing: module-level so worker processes can run it
# --------------------------------------------------------------------------

def fixup(data: bytes, record_size: int) -> Optional[bytes]:
    if len(data) < record_size or data[:4] != b"FILE":
        return None
    uso, usc = struct.unpack_from("<HH", data, 4)
    if usc < 2 or uso + usc * 2 > len(data):
        return None
    buf = bytearray(data[:record_size])
    usn = buf[uso:uso + 2]
    for i in range(1, usc):
        end = i * 512 - 2
        if end + 2 > len(buf):
            break
        if buf[end:end + 2] != usn:
            return None  # torn write: record is inconsistent
        buf[end:end + 2] = buf[uso + i * 2:uso + i * 2 + 2]
    return bytes(buf)


def parse_record(data: Optional[bytes], number: int) -> Optional[_Record]:
    if data is None:
        return None
    flags = struct.unpack_from("<H", data, 0x16)[0]
    attr_off = struct.unpack_from("<H", data, 0x14)[0]
    base_ref = struct.unpack_from("<Q", data, 0x20)[0] & _REF_MASK
    rec = _Record(number, bool(flags & FLAG_IN_USE), bool(flags & FLAG_DIRECTORY), base_ref, [])
    pos = attr_off
    while pos + 16 <= len(data):
        atype, alen = struct.unpack_from("<II", data, pos)
        if atype == ATTR_END or alen < 16 or pos + alen > len(data):
            break
        a = data[pos:pos + alen]
        non_resident, name_len = a[8], a[9]
        if not non_resident:
            if len(a) < 0x16:
                break
            vlen, voff = struct.unpack_from("<IH", a, 0x10)
            value = a[voff:voff + vlen]
            if atype == ATTR_STANDARD_INFORMATION and len(value) >= 16:
                c, m = struct.unpack_from("<QQ", value, 0)
                rec.created, rec.modified = filetime_to_dt(c), filetime_to_dt(m)
            elif atype == ATTR_FILE_NAME and len(value) >= 0x42:
                parent = struct.unpack_from("<Q", value, 0)[0] & _REF_MASK
                rec.size_hint = max(rec.size_hint, struct.unpack_from("<Q", value, 0x30)[0])
                nlen, ns = value[0x40], value[0x41]
                name = value[0x42:0x42 + nlen * 2].decode("utf-16-le", "replace")
                rec.names.append((ns, name, parent))
            elif atype == ATTR_DATA and name_len == 0:
                rec.resident = bytes(value)
            elif atype == ATTR_ATTRIBUTE_LIST:
                rec.attr_list = bytes(value)
            elif atype == ATTR_VOLUME_NAME:
                rec.volume_name = value.decode("utf-16-le", "replace")
        elif alen >= 0x40 and atype in (ATTR_DATA, ATTR_ATTRIBUTE_LIST) and (name_len == 0):
            start_vcn = struct.unpack_from("<Q", a, 0x10)[0]
            runs_off = struct.unpack_from("<H", a, 0x20)[0]
            real = struct.unpack_from("<Q", a, 0x30)[0] if start_vcn == 0 else None
            frag = _DataFragment(start_vcn, decode_runs(a[runs_off:]), real)
            if atype == ATTR_DATA:
                rec.fragments.append(frag)
            else:
                rec.attr_list_frag = frag
        pos += alen
    return rec


@dataclass
class _SliceResult:
    dirs: Dict[int, Tuple[str, int]]
    files: List[_Record]
    ext_frags: List[Tuple[int, bool, List[_DataFragment]]]  # (base record, in_use, fragments)
    records: int


def parse_slice(data: bytes, first_number: int, record_size: int, include_existing: bool) -> _SliceResult:
    """Parse consecutive MFT records and keep only what the scan needs."""
    res = _SliceResult({}, [], [], 0)
    number = first_number
    for r in range(0, len(data) - record_size + 1, record_size):
        rec = parse_record(fixup(data[r:r + record_size], record_size), number)
        number += 1
        if rec is None:
            continue
        if rec.base_ref:  # extension record of a fragmented file
            if rec.fragments and (include_existing or not rec.in_use):
                res.ext_frags.append((rec.base_ref, rec.in_use, rec.fragments))
            continue
        named = rec.best_name()
        if named is None:
            continue
        if rec.is_dir:
            res.dirs[rec.number] = named
        elif rec.number >= FIRST_USER_RECORD and (include_existing or not rec.in_use):
            rec.attr_list = rec.attr_list_frag = None  # not needed past this point
            res.files.append(rec)
    res.records = number - first_number
    return res


def _slice_worker(spec, offset: int, length: int, first_number: int, record_size: int,
                  include_existing: bool) -> _SliceResult:
    from .parallel import worker_reader
    data = worker_reader(spec).read_at(offset, length)
    return parse_slice(data, first_number, record_size, include_existing)


# --------------------------------------------------------------------------

class NTFSVolume(FileSystem):
    name = "NTFS"

    def __init__(self, reader: DiskReader, boot: Optional[bytes] = None):
        super().__init__(reader)
        boot = boot if boot is not None else reader.read_at(0, 512)
        if boot[3:11] != b"NTFS    ":
            # The primary boot sector may be damaged; NTFS keeps a copy in the last sector.
            backup = reader.read_at(reader.length - 512, 512) if reader.length > 512 * 16 else b""
            if backup[3:11] != b"NTFS    ":
                raise NTFSError("This drive isn't NTFS. Use Deep scan to search it by file signatures.")
            boot = backup
            self.used_backup_boot = True
        else:
            self.used_backup_boot = False
        self.bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0]
        spc = boot[0x0D]
        self.sectors_per_cluster = 1 << (256 - spc) if spc > 0x80 else spc
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.total_sectors = struct.unpack_from("<Q", boot, 0x28)[0]
        self.mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
        cpr = struct.unpack_from("<b", boot, 0x40)[0]
        if self.cluster_size == 0 or self.bytes_per_sector not in (512, 1024, 2048, 4096):
            raise NTFSError("The NTFS boot sector looks damaged. Try Deep scan instead.")
        self.record_size = (1 << -cpr) if cpr < 0 else cpr * self.cluster_size
        if self.record_size not in (1024, 2048, 4096):
            raise NTFSError("The NTFS boot sector looks damaged. Try Deep scan instead.")
        self.volume_size = self.total_sectors * self.bytes_per_sector
        self.total_clusters = self.volume_size // self.cluster_size

        rec0 = self._parse(self._fixup(reader.read_at(self.mft_lcn * self.cluster_size, self.record_size)), 0)
        if rec0 is None or not rec0.fragments:
            raise NTFSError("Couldn't read the Master File Table. Try Deep scan instead.")
        self.mft_runs = self._merge_fragments(rec0.fragments)[0]
        self._load_mft_attribute_list(rec0)
        self._bitmap: Optional[bytes] = None
        self.label = self._read_label()

    # ---------------------------------------------------------------- records
    def _fixup(self, data: bytes) -> Optional[bytes]:
        return fixup(data, self.record_size)

    def _parse(self, data: Optional[bytes], number: int) -> Optional[_Record]:
        return parse_record(data, number)

    def _read_record(self, number: int) -> Optional[_Record]:
        off = self._mft_record_offset(number)
        if off is None:
            return None
        return self._parse(self._fixup(self.reader.read_at(off, self.record_size)), number)

    def _load_mft_attribute_list(self, rec0: _Record):
        """A very fragmented MFT keeps the rest of its run list in extension records
        named by an $ATTRIBUTE_LIST on record 0. Pull those fragments in."""
        raw = rec0.attr_list
        if raw is None and rec0.attr_list_frag is not None:
            f = rec0.attr_list_frag
            raw = self.read_extents(self._runs_to_extents(f.runs, f.real_size or 0))
        if not raw:
            return
        wanted, pos = set(), 0
        while pos + 26 <= len(raw):
            atype, rlen = struct.unpack_from("<IH", raw, pos)
            if rlen < 26:
                break
            ref = struct.unpack_from("<Q", raw, pos + 16)[0] & _REF_MASK
            if atype == ATTR_DATA and ref != 0:
                wanted.add(ref)
            pos += rlen
        frags = list(rec0.fragments)
        pending = sorted(wanted)
        # Extension records may live in fragments we only learn about from earlier ones.
        for _ in range(len(pending) + 1):
            if not pending:
                break
            left = []
            for ref in pending:
                ext = self._read_record(ref)
                if ext is None:
                    left.append(ref)
                    continue
                frags.extend(ext.fragments)
                self.mft_runs = self._merge_fragments(frags)[0]
            if len(left) == len(pending):
                break
            pending = left

    def _read_label(self) -> str:
        try:
            rec = self._read_record(VOLUME_RECORD)
        except Exception:
            return ""
        return (rec.volume_name or "") if rec else ""

    @staticmethod
    def _merge_fragments(frags: List[_DataFragment]) -> Tuple[List[Tuple[Optional[int], int]], Optional[int]]:
        frags = sorted(frags, key=lambda f: f.start_vcn)
        runs, size, seen = [], None, set()
        for f in frags:
            if f.start_vcn in seen:  # same fragment listed twice (e.g. stale extension record)
                continue
            seen.add(f.start_vcn)
            runs.extend(f.runs)
            if f.real_size is not None:
                size = f.real_size
        return runs, size

    def _runs_to_extents(self, runs, size):
        extents, remaining = [], size
        for lcn, count in runs:
            if remaining <= 0:
                break
            n = min(count * self.cluster_size, remaining)
            extents.append((None if lcn is None else lcn * self.cluster_size, n))
            remaining -= n
        return extents

    def _mft_slices(self) -> List[Tuple[int, int, int]]:
        """[(byte_offset, length, first_record_number)] covering the whole MFT."""
        slices, number = [], 0
        per = max(SLICE_BYTES - SLICE_BYTES % self.record_size, self.record_size)
        for lcn, count in self.mft_runs:
            run_bytes = count * self.cluster_size
            if lcn is None:
                number += run_bytes // self.record_size
                continue
            for off in range(0, run_bytes, per):
                n = min(per, run_bytes - off)
                slices.append((lcn * self.cluster_size + off, n, number))
                number += n // self.record_size
        return slices

    def total_records(self) -> int:
        return sum(c for _, c in self.mft_runs) * self.cluster_size // self.record_size

    def iter_records(self, progress: Progress, cancelled: Cancelled):
        """Yield every parsed MFT record, reading the MFT in slices."""
        total = self.total_records()
        for off, length, number in self._mft_slices():
            if cancelled():
                return
            data = self.reader.read_at(off, length)
            for r in range(0, len(data) - self.record_size + 1, self.record_size):
                rec = self._parse(self._fixup(data[r:r + self.record_size]), number)
                if rec is not None:
                    yield rec
                number += 1
            progress(min(number / max(total, 1), 1.0), f"Reading file records: {number:,} of {total:,}")

    # ---------------------------------------------------------------- bitmap
    def load_bitmap(self) -> Optional[bytes]:
        if self._bitmap is not None:
            return self._bitmap
        rec = self._read_record(BITMAP_RECORD)
        if rec is None:
            return None
        if rec.resident is not None:
            self._bitmap = rec.resident
        elif rec.fragments:
            runs, size = self._merge_fragments(rec.fragments)
            self._bitmap = self.read_extents(self._runs_to_extents(runs, size or 0))
        return self._bitmap

    def _mft_record_offset(self, number: int) -> Optional[int]:
        byte = number * self.record_size
        for lcn, count in self.mft_runs:
            run_bytes = count * self.cluster_size
            if byte < run_bytes:
                return None if lcn is None else lcn * self.cluster_size + byte
            byte -= run_bytes
        return None

    def allocated_fraction(self, extents) -> float:
        """Share of a file's clusters that $Bitmap says now belong to something else."""
        bm = self.load_bitmap()
        if bm is None:
            return 0.0
        used = total = 0
        for off, length in extents:
            if off is None:
                continue
            first = off // self.cluster_size
            n = -(-length // self.cluster_size)
            total += n
            used += _count_bits(bm, first, n)
        return used / total if total else 0.0

    def free_regions(self) -> List[Tuple[int, int]]:
        """Byte ranges containing free clusters (a slight superset: 8-cluster granularity).
        Pair with is_free() for an exact per-cluster check."""
        bm = self.load_bitmap()
        if bm is None:
            return [(0, self.volume_size)]
        span = 8 * self.cluster_size
        return [(m.start() * span, min(m.end() * span, self.volume_size))
                for m in re.finditer(rb"[^\xff]+", bm)]

    def free_filter(self) -> Optional[FreeSpaceFilter]:
        bm = self.load_bitmap()
        return None if bm is None else FreeSpaceFilter(bm, self.cluster_size)

    def is_free(self, offset: int) -> bool:
        bm = self.load_bitmap()
        if bm is None:
            return True
        c = offset // self.cluster_size
        return c // 8 < len(bm) and not (bm[c // 8] >> (c % 8)) & 1

    def read_extents(self, extents, limit: Optional[int] = None) -> bytes:
        out = bytearray()
        for off, length in extents:
            if limit is not None and len(out) >= limit:
                break
            n = length if limit is None else min(length, limit - len(out))
            out += b"\x00" * n if off is None else self.reader.read_at(off, n)
        return bytes(out)

    # ---------------------------------------------------------------- scan
    def _collect(self, progress: Progress, cancelled: Cancelled, on_found: Optional[OnFound],
                 include_existing: bool, workers: int) -> List[_SliceResult]:
        slices = self._mft_slices()
        total = max(self.total_records(), 1)
        results: Dict[int, _SliceResult] = {}
        done_records = found = 0

        def account(i: int, res: _SliceResult):
            nonlocal done_records, found
            results[i] = res
            done_records += res.records
            found += len(res.files)
            progress(min(done_records / total, 1.0), f"Reading file records: {done_records:,} of {total:,}")
            if on_found:
                on_found(found)

        if workers <= 1 or len(slices) <= 1:
            for i, (off, length, first) in enumerate(slices):
                if cancelled():
                    break
                data = self.reader.read_at(off, length)
                account(i, parse_slice(data, first, self.record_size, include_existing))
        else:
            from .parallel import make_pool, reader_spec
            spec = reader_spec(self.reader)
            pool = make_pool(workers)
            try:
                futures = {}
                pending_slices = list(enumerate(slices))
                # Keep a bounded number of tasks in flight so cancel is quick.
                while pending_slices or futures:
                    while pending_slices and len(futures) < workers * 2 and not cancelled():
                        i, (off, length, first) = pending_slices.pop(0)
                        futures[pool.submit(_slice_worker, spec, off, length, first,
                                            self.record_size, include_existing)] = i
                    if cancelled():
                        break
                    if not futures:
                        break
                    done, _ = wait(list(futures), timeout=0.25, return_when=FIRST_COMPLETED)
                    for fut in done:
                        account(futures.pop(fut), fut.result())
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        return [results[i] for i in sorted(results)]

    def scan_deleted(self, progress: Progress, cancelled: Cancelled, on_found: Optional[OnFound] = None,
                     include_existing: bool = False, workers: int = 1) -> List[RecoverableItem]:
        parts = self._collect(progress, cancelled, on_found, include_existing, workers)
        dirs: Dict[int, Tuple[str, int]] = {}
        files: List[_Record] = []
        ext_frags: Dict[Tuple[int, bool], List[_DataFragment]] = {}
        for p in parts:
            dirs.update(p.dirs)
            files.extend(p.files)
            for base, in_use, frags in p.ext_frags:
                ext_frags.setdefault((base, in_use), []).extend(frags)

        progress(1.0, "Rebuilding folder paths")
        self.load_bitmap()
        path_cache: Dict[int, str] = {ROOT_RECORD: ""}

        def folder_of(parent: int) -> str:
            chain, cur = [], parent
            while cur not in path_cache and len(chain) < 128:
                entry = dirs.get(cur)
                if entry is None or cur in (c for c, _ in chain):
                    break
                chain.append((cur, entry[0]))
                cur = entry[1]
            base = path_cache.get(cur, f"Lost folders/Folder {cur}")
            for rec_no, name in reversed(chain):
                base = f"{base}/{name}" if base else name
                path_cache[rec_no] = base
            return path_cache.get(parent, base)

        items: List[RecoverableItem] = []
        for i, rec in enumerate(files):
            if cancelled():
                break
            name, parent = rec.best_name()
            frags = rec.fragments + ext_frags.get((rec.number, rec.in_use), [])
            if rec.resident is not None:
                extents, size, resident = [], len(rec.resident), rec.resident
            elif frags:
                runs, size = self._merge_fragments(frags)
                size = size if size is not None else rec.size_hint
                extents, resident = self._runs_to_extents(runs, size), None
            else:
                continue  # no data attribute left: nothing to recover
            if rec.in_use:
                pct, cond = 0.0, Condition.GOOD
            else:
                pct = self.allocated_fraction(extents) if extents else 0.0
                if resident is not None or pct == 0:
                    cond = Condition.GOOD
                elif pct >= 0.999:
                    cond = Condition.OVERWRITTEN
                else:
                    cond = Condition.PARTIAL
            pieces = count_pieces(extents)
            items.append(RecoverableItem(
                id=i, name=name, folder=folder_of(parent), size=size, source=Source.QUICK,
                extents=extents, resident_data=resident, created=rec.created, modified=rec.modified,
                condition=cond, overwritten_pct=pct, mft_record=rec.number, deleted=not rec.in_use,
                note=f"Fragmented: {pieces} pieces" if pieces > 1 else "",
            ))
        if on_found:
            on_found(len(items))
        return items


def count_pieces(extents) -> int:
    """Number of separate places on disk the data lives in (adjacent runs count once)."""
    pieces, end = 0, None
    for off, length in extents:
        if off is None:
            continue
        if off != end:
            pieces += 1
        end = off + length
    return pieces


def _count_bits(bm: bytes, first: int, n: int) -> int:
    """Count set bits for clusters [first, first+n) in an LSB-first bitmap."""
    if n <= 0:
        return 0
    lo, hi = first // 8, (first + n - 1) // 8 + 1
    chunk = bm[lo:hi]
    if not chunk:
        return n  # beyond the bitmap: treat as unavailable
    value = int.from_bytes(chunk, "little") >> (first - lo * 8)
    covered = min(n, len(chunk) * 8 - (first - lo * 8))
    return (value & ((1 << covered) - 1)).bit_count() + (n - covered)
