"""Signature-based file carving ("Deep scan").

Used when the file records are gone (MFT entry reused, drive formatted, or the
file system isn't one we can read). We look for known file headers at block
boundaries, then walk each format's own structure to find where the file ends.
Every extractor validates the structure it walks, so random byte matches are
dropped.

Speed: instead of testing every block boundary from Python, we gather the first
32 bytes of every block into one small buffer and let the regex engine (C code)
search that. Large volumes are split into ~64 MB tasks and carved by worker
processes (see parallel.py); the merge step makes the result identical to a
single sequential pass.

Limitation shared by all carvers: a file is assumed to be stored contiguously.
Fragmented files come back damaged; Quick scan doesn't have that problem.
"""
from __future__ import annotations

import re
import struct
import uuid
import zlib
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .models import (Cancelled, Condition, OnBlock, OnFound, Progress, RecoverableItem, Source)

MB = 1024 * 1024
GB = 1024 * MB
REC = 32          # bytes of each block start handed to the regex (longest header is 16)


@dataclass
class Signature:
    key: str
    label: str
    header: bytes             # regex source (bytes); must fit in 16 bytes
    extractor: str            # method name on Carver
    max_size: int
    category: str = "Other"
    offset: int = 0           # where `header` sits inside the file (mp4's "ftyp" is at 4)


SIGNATURES: List[Signature] = [
    Signature("jpg", "JPEG photos", rb"\xFF\xD8\xFF[\xC0-\xFE]", "_jpeg", 64 * MB, "Images"),
    Signature("png", "PNG images", rb"\x89PNG\r\n\x1a\n", "_png", 128 * MB, "Images"),
    Signature("gif", "GIF images", rb"GIF8[79]a", "_gif", 64 * MB, "Images"),
    Signature("bmp", "BMP images", rb"BM....\x00\x00\x00\x00", "_bmp", 256 * MB, "Images"),
    Signature("tif", "TIFF & camera RAW (tif, cr2, nef, arw, dng, orf)",
              rb"II\*\x00|MM\x00\*|IIR[OS]\x08\x00\x00\x00", "_tiff", 512 * MB, "Images"),
    Signature("psd", "Photoshop (psd, psb)", rb"8BPS\x00[\x01\x02]\x00{6}", "_psd", 4 * GB, "Images"),
    Signature("pdf", "PDF documents", rb"%PDF-[12]\.\d", "_pdf", 512 * MB, "Documents"),
    Signature("zip", "Office files & ZIP (docx, xlsx, pptx, odt, epub, zip, jar, apk)",
              rb"PK\x03\x04", "_zip", 4 * GB, "Documents"),
    Signature("ole", "Legacy Office & Outlook (doc, xls, ppt, msg)",
              rb"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", "_ole", 2 * GB, "Documents"),
    Signature("rar", "RAR archives", rb"Rar!\x1a\x07[\x00\x01]", "_rar", 16 * GB, "Archives"),
    Signature("7z", "7-Zip archives", rb"7z\xBC\xAF\x27\x1C", "_7z", 16 * GB, "Archives"),
    Signature("mp4", "Video & photos (mp4, mov, m4a, heic, cr3)", rb"ftyp", "_mp4", 32 * GB, "Video", 4),
    Signature("mkv", "Matroska & WebM video (mkv, webm)", rb"\x1A\x45\xDF\xA3", "_mkv", 32 * GB, "Video"),
    Signature("asf", "Windows Media (wmv, wma)",
              re.escape(uuid.UUID("75B22630-668E-11CF-A6D9-00AA0062CE6C").bytes_le), "_asf", 16 * GB, "Video"),
    Signature("riff", "WAV, AVI, WebP", rb"RIFF....(?:WAVE|AVI |WEBP)", "_riff", 4 * GB, "Audio"),
    Signature("mp3", "MP3 music (with ID3 tags)", rb"ID3[\x02-\x04]\x00", "_mp3", 512 * MB, "Audio"),
    Signature("ogg", "Ogg & Opus audio", rb"OggS\x00\x02", "_ogg", 2 * GB, "Audio"),
    Signature("flac", "FLAC audio", rb"fLaC[\x00\x80]\x00\x00\x22", "_flac", 2 * GB, "Audio"),
    Signature("sqlite", "SQLite databases", rb"SQLite format 3\x00", "_sqlite", 8 * GB, "Code & data"),
    Signature("exe", "Programs (exe, dll)", rb"MZ", "_pe", 1 * GB, "Code & data"),
]
SIG_BY_KEY = {s.key: s for s in SIGNATURES}

# (offset, size, ext, sig_key, note) -- what a scan produces before items are named
Raw = Tuple[int, int, str, str, str]


@lru_cache(maxsize=32)
def _pattern(keys: Tuple[str, ...]) -> Tuple[Tuple[str, int, "re.Pattern[bytes]"], ...]:
    # One regex per format: each then gets the engine's fast literal-prefix search.
    # (A single combined alternation is ~50x slower because no prefix can be used.)
    return tuple((k, SIG_BY_KEY[k].offset, re.compile(SIG_BY_KEY[k].header, re.DOTALL)) for k in keys)


def count_by_ext(items: Sequence[RecoverableItem]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        out[it.ext] = out.get(it.ext, 0) + 1
    return out


class _Window:
    """Buffered random access for walkers that hop through many small headers."""

    def __init__(self, reader, size: int = MB):
        self.reader, self.size = reader, size
        self.base, self.buf = -1, b""

    def get(self, pos: int, n: int) -> bytes:
        o = pos - self.base
        if self.base < 0 or o < 0 or o + n > len(self.buf):
            self.base, self.buf = pos, self.reader.read_at(pos, max(n, self.size))
            o = 0
        return self.buf[o:o + n]


# ------------------------------------------------------------------ worker side
_ACCEPT = None


def _init_worker(accept):
    global _ACCEPT
    _ACCEPT = accept


def _carve_task(spec, block: int, keys: Tuple[str, ...], start: int, end: int, chunk: int):
    from .parallel import worker_reader
    carver = Carver(worker_reader(spec), block=block)
    found, _ = carver._scan_range(start, end, _pattern(keys), _ACCEPT, lambda: False, 0, None, chunk)
    return found


# ------------------------------------------------------------------ CRC helpers
_CRC8 = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = ((_c << 1) ^ 0x07) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    _CRC8.append(_c)
_CRC16 = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x8005) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    _CRC16.append(_c)
_BITREV = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


def ogg_crc(data: bytes) -> int:
    """Ogg's CRC-32 (poly 0x04C11DB7, not reflected, init 0) computed with zlib by bit-reversing."""
    r = (~zlib.crc32(data.translate(_BITREV), 0xFFFFFFFF)) & 0xFFFFFFFF
    return int(f"{r:032b}"[::-1], 2)


def _vint5(buf: bytes, p: int) -> Tuple[Optional[int], int]:
    """RAR5 variable-length integer: (value, new position) or (None, p)."""
    v, shift = 0, 0
    for i in range(10):
        if p + i >= len(buf):
            return None, p
        b = buf[p + i]
        v |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return v, p + i + 1
    return None, p


def _ebml_id(buf: bytes, p: int) -> Tuple[Optional[int], int]:
    if p >= len(buf) or buf[p] == 0:
        return None, p
    n = 8 - buf[p].bit_length() + 1
    if n > 4 or p + n > len(buf):
        return None, p
    return int.from_bytes(buf[p:p + n], "big"), p + n


def _ebml_size(buf: bytes, p: int) -> Tuple[Optional[int], int, bool]:
    """(value, new position, unknown_size)."""
    if p >= len(buf) or buf[p] == 0:
        return None, p, False
    n = 8 - buf[p].bit_length() + 1
    if p + n > len(buf):
        return None, p, False
    v = buf[p] & (0xFF >> n)
    for b in buf[p + 1:p + n]:
        v = (v << 8) | b
    return v, p + n, v == (1 << (7 * n)) - 1


# ------------------------------------------------------------------ MP3 tables
_MP3_BITRATES = {
    (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],   # MPEG1 L1
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],      # MPEG1 L2
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],       # MPEG1 L3
    (2, 3): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],      # MPEG2 L1
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mp3_frame(h: bytes) -> Optional[Tuple[int, int]]:
    """(frame length, signature bits that must stay constant) or None."""
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    ver, layer = (h[1] >> 3) & 3, (h[1] >> 1) & 3
    bri, sri, pad = h[2] >> 4, (h[2] >> 2) & 3, (h[2] >> 1) & 1
    if ver == 1 or layer == 0 or bri in (0, 15) or sri == 3:
        return None
    br = _MP3_BITRATES[(3 if ver == 3 else 2, layer)][bri] * 1000
    sr = _MP3_RATES[ver][sri]
    if layer == 3:
        length = (12 * br // sr + pad) * 4
    elif layer == 1 and ver != 3:
        length = 72 * br // sr + pad
    else:
        length = 144 * br // sr + pad
    return length, (h[1] << 8) | (h[2] & 0x0C)


# ------------------------------------------------------------------ FLAC helpers
def flac_frame_header(buf: bytes, p: int, sample_rate_known: bool = True):
    """Parse a FLAC frame header at p. Returns (header_len, blocksize, number) or None."""
    if p + 6 > len(buf) or buf[p] != 0xFF or buf[p + 1] not in (0xF8, 0xF9):
        return None
    bsc, src = buf[p + 2] >> 4, buf[p + 2] & 0x0F
    ch, ss = buf[p + 3] >> 4, (buf[p + 3] >> 1) & 7
    if bsc == 0 or src == 15 or ch > 10 or ss == 3 or buf[p + 3] & 1:
        return None
    q = p + 4
    first = buf[q]
    if first < 0x80:
        n, number = 1, first
    else:
        n = 8 - (first ^ 0xFF).bit_length()
        if n < 2 or n > 7:
            return None
        number = first & (0xFF >> (n + 1))
        if q + n > len(buf):
            return None
        for b in buf[q + 1:q + n]:
            if b & 0xC0 != 0x80:
                return None
            number = (number << 6) | (b & 0x3F)
    q += n
    if bsc == 6:
        bs, q = (buf[q] + 1 if q < len(buf) else 0), q + 1
    elif bsc == 7:
        bs, q = int.from_bytes(buf[q:q + 2], "big") + 1, q + 2
    elif bsc == 1:
        bs = 192
    elif bsc <= 5:
        bs = 576 << (bsc - 2)
    else:
        bs = 256 << (bsc - 8)
    if src == 12:
        q += 1
    elif src in (13, 14):
        q += 2
    if q >= len(buf) or crc8(buf[p:q]) != buf[q]:
        return None
    return q + 1 - p, bs, number


def flac_crc16(data: bytes, start: int = 0) -> int:
    c = start
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC16[(c >> 8) ^ b]
    return c


_TIFF_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_ASF_FILE_PROPS = uuid.UUID("8CABDCA1-A947-11CF-8EE4-00C00C205365").bytes_le
_ASF_STREAM_PROPS = uuid.UUID("B7DC0791-A9B7-11CF-8EE6-00C00C205365").bytes_le
_ASF_VIDEO = uuid.UUID("BC19EFC0-5B4D-11CF-A8FD-00805F5C442B").bytes_le
_ASF_DATA = uuid.UUID("75B22636-668E-11CF-A6D9-00AA0062CE6C").bytes_le
_MKV_TOP = {0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1F43B675, 0x1C53BB6B, 0x1941A469, 0x1043A770,
            0x1254C367, 0xEC, 0xBF}
_MKV_CLUSTER_CHILDREN = {0xE7, 0xA3, 0xA0, 0xA7, 0xAB, 0xEC, 0xBF, 0x5854, 0xAF}


class Carver:
    def __init__(self, reader, block: int = 512):
        self.reader = reader
        self.block = block

    # ================================================================= scan
    def scan(self, regions: List[Tuple[int, int]], keys: List[str],
             progress: Progress, cancelled: Cancelled,
             on_found: Optional[OnFound] = None,
             on_block: Optional[OnBlock] = None,
             accept: Optional[Callable[[int], bool]] = None,
             chunk: int = 8 * MB, workers: int = 1, task_size: int = 64 * MB,
             known=frozenset()) -> List[RecoverableItem]:
        """`known`: start offsets already found another way (e.g. by quick scan). They're still
        returned, but not counted in on_found or marked on the map, so live counts match the results."""
        keys_t = tuple(k for k in dict.fromkeys(keys) if k in SIG_BY_KEY)
        if not keys_t:
            return []
        pattern = _pattern(keys_t)
        regions = [(s, e) for s, e in regions if e > s]
        total = sum(e - s for s, e in regions) or 1
        length = max(getattr(self.reader, "length", 0) or 0, 1)
        if workers <= 1:
            raw = self._scan_sequential(regions, pattern, accept, progress, cancelled, on_found, on_block,
                                        total, length, chunk, known)
        else:
            raw = self._scan_parallel(regions, keys_t, pattern, accept, progress, cancelled, on_found, on_block,
                                      total, length, chunk, workers, task_size, known)
        return self._to_items(raw)

    def _scan_sequential(self, regions, pattern, accept, progress, cancelled, on_found, on_block,
                         total, length, chunk, known=frozenset()) -> List[Raw]:
        found: List[Raw] = []
        state = {"done": 0, "n": 0}
        skip = 0

        def on_chunk(lo, hi, hit, new):
            state["done"] += max(0, hi - lo)
            new = [r for r in new if r[0] not in known]
            hit = bool(new)
            if on_block:
                on_block(min(hi / length, 1.0), hit)
            state["n"] += len(new)
            if on_found and hit:
                on_found(state["n"])
            progress(min(state["done"] / total, 1.0), f"Searching {state['done'] / MB:,.0f} of {total / MB:,.0f} MB")

        for s, e in regions:
            if cancelled():
                break
            got, skip = self._scan_range(s, e, pattern, accept, cancelled, skip, on_chunk, chunk)
            found.extend(got)
        return found

    def _scan_parallel(self, regions, keys_t, pattern, accept, progress, cancelled, on_found, on_block,
                       total, length, chunk, workers, task_size, known=frozenset()) -> List[Raw]:
        from .parallel import make_pool, reader_spec
        b = self.block
        step = max(b, task_size - task_size % b)
        tasks: List[Tuple[int, int]] = []
        for s, e in regions:
            pos = s
            while pos < e:
                nxt = min(e, (pos - pos % b) + step)
                tasks.append((pos, nxt))
                pos = nxt
        spec = reader_spec(self.reader)
        pool = make_pool(workers, _init_worker, (accept,))
        results: Dict[int, List[Tuple]] = {}
        pending = {}
        queue = iter(range(len(tasks)))
        emitted, done_bytes, n_found, finished_bytes = 0, 0, 0, 0
        stopped = False

        def submit_next():
            i = next(queue, None)
            if i is not None:
                s, e = tasks[i]
                pending[pool.submit(_carve_task, spec, b, keys_t, s, e, chunk)] = i

        try:
            for _ in range(2 * workers):
                submit_next()
            while pending:
                ready, _ = wait(list(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                if cancelled():
                    stopped = True
                    break
                for f in ready:
                    i = pending.pop(f)
                    s, e = tasks[i]
                    results[i] = [r + (e,) for r in f.result()]
                    finished_bytes += e - s
                    submit_next()
                if ready:  # the bar counts every finished task; the map below fills in disk order
                    progress(min(finished_bytes / total, 1.0),
                             f"Searching {finished_bytes / MB:,.0f} of {total / MB:,.0f} MB")
                # report in disk order so the live map fills left to right
                while emitted in results:
                    s, e = tasks[emitted]
                    done_bytes += e - s
                    hits = [r for r in results[emitted] if r[0] not in known]
                    n_found += len(hits)
                    if on_block:
                        on_block(min(e / length, 1.0), bool(hits))
                    if on_found and hits:
                        on_found(n_found)
                    emitted += 1
        finally:
            pool.shutdown(wait=not stopped, cancel_futures=True)
        raw = [r for i in sorted(results) for r in results[i]]
        return self._merge(raw, pattern, accept, cancelled)

    def _merge(self, raw: List[Tuple], pattern, accept, cancelled) -> List[Raw]:
        """Combine per-task results so they equal one sequential pass.

        A sequential pass never looks for headers inside a file it already found.
        A task that started in the middle of a file found by the task before it
        may have found (and skipped past) something that the sequential pass
        wouldn't have; those stretches are searched again here."""
        by_off = {r[0]: r for r in raw}
        searched = set()
        while True:
            kept, cur_end, gaps = [], 0, []
            for r in sorted(by_off.values()):
                if r[0] >= cur_end:
                    kept.append(r)
                    cur_end = r[0] + r[1]
                else:
                    g = (cur_end, min(r[0] + r[1], r[5]))
                    if g[1] > g[0] and g not in searched:
                        gaps.append(g)
            if not gaps or cancelled():
                return [r[:5] for r in kept]
            for g in gaps:
                searched.add(g)
                got, _ = self._scan_range(g[0], g[1], pattern, accept, cancelled, g[0], None)
                for f in got:
                    by_off.setdefault(f[0], f + (g[1],))

    def _scan_range(self, start: int, end: int, pattern, accept, cancelled, skip_until: int = 0,
                    on_chunk=None, chunk: int = 8 * MB) -> Tuple[List[Raw], int]:
        """Carve [start, end). Headers are only accepted at block boundaries and
        outside files already found (skip_until). Returns (found, skip_until)."""
        b = self.block
        stride = min(b, REC)
        found: List[Raw] = []
        pos = start - start % b
        while pos < end:
            if cancelled():
                break
            if skip_until > pos:
                # inside a file we already carved: jump over it without reading
                nxt = min(end, -(-skip_until // b) * b)
                if nxt > pos:
                    if on_chunk:
                        on_chunk(max(pos, start), nxt, False, [])
                    pos = nxt
                    continue
            n = min(chunk, end - pos)
            n += -n % b if pos + n < end else 0
            data = self.reader.read_at(pos, n + REC)
            lim = min(n, len(data))
            if b <= REC:
                heads = data
            else:
                heads = b"".join([data[i:i + REC] for i in range(0, lim, b)])
            hlim = -(-lim // b) * stride
            n_before = len(found)
            cands = []
            for key, shift, pat in pattern:
                for m in pat.finditer(heads, shift):
                    s = m.start() - shift
                    if s >= hlim:
                        break
                    if s % stride:
                        # an unaligned hit may cover an aligned one of the same format
                        a = s + stride - s % stride
                        if a < m.end() - shift and a < hlim and pat.match(heads, a + shift):
                            cands.append((a, key))
                        continue
                    cands.append((s, key))
            cands.sort()
            for s, key in cands:
                at = pos + (s // stride) * b
                if at < skip_until or at < start or at >= end:
                    continue
                if accept is not None and not accept(at):
                    continue
                res = self._extract(key, at)
                if res is None:
                    continue
                size, ext, note = res
                found.append((at, size, ext, key, note))
                skip_until = at + size
                if skip_until >= pos + lim:
                    break
            if on_chunk:
                on_chunk(max(pos, start), min(pos + n, end), len(found) > n_before, found[n_before:])
            if lim < n:  # end of media
                break
            pos += n
        return found, skip_until

    def _extract(self, key: str, at: int) -> Optional[Tuple[int, str, str]]:
        sig = SIG_BY_KEY[key]
        try:
            res = getattr(self, sig.extractor)(at, sig.max_size)
        except (struct.error, ValueError, IndexError, KeyError, OverflowError):
            return None
        if not res:
            return None
        size, ext = res[0], res[1]
        note = res[2] if len(res) > 2 else ""
        if size < 32 or size > sig.max_size:
            return None
        return size, ext, note

    def _to_items(self, raw: List[Raw]) -> List[RecoverableItem]:
        counts: Dict[str, int] = {}
        items = []
        for at, size, ext, _key, note in sorted(raw):
            counts[ext] = counts.get(ext, 0) + 1
            items.append(RecoverableItem(
                id=len(items), name=f"{ext.upper()}_{counts[ext]:05d}_{at:012X}.{ext}",
                folder=f"Deep scan/{ext.upper()}", size=size, source=Source.DEEP,
                extents=[(at, size)], condition=Condition.UNKNOWN, note=note,
            ))
        return items

    # ================================================================= helpers
    def _read(self, at: int, n: int) -> bytes:
        return self.reader.read_at(at, n)

    def _search_forward(self, at: int, regex: "re.Pattern[bytes]", limit: int, keep: int = 16,
                        window: int = 4 * MB):
        """First regex match at or after `at` (absolute) within `limit` bytes: (offset, match) or (-1, None)."""
        pos, stop = at, at + limit
        while pos < stop:
            data = self._read(pos, min(window, stop - pos))
            if not data:
                return -1, None
            m = regex.search(data)
            if m:
                return pos + m.start(), m
            if len(data) <= keep:
                return -1, None
            pos += len(data) - keep
        return -1, None

    def _find_forward(self, at: int, needle: bytes, limit: int, window: int = 4 * MB) -> int:
        return self._search_forward(at, re.compile(re.escape(needle)), limit, len(needle) - 1, window)[0]

    # ================================================================= images
    def _jpeg(self, at: int, limit: int):
        w = _Window(self.reader, 256 * 1024)
        pos = at + 2
        while pos < at + limit:
            seg = w.get(pos, 4)
            if len(seg) < 4 or seg[0] != 0xFF:
                return None
            marker = seg[1]
            if marker == 0xFF:
                pos += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            length = struct.unpack(">H", seg[2:4])[0]
            if length < 2:
                return None
            pos += 2 + length
            if marker == 0xDA:  # start of scan: entropy-coded data until EOI
                return self._jpeg_scan(at, pos, limit)
        return None

    def _jpeg_scan(self, at: int, pos: int, limit: int):
        """Walk entropy-coded data to the EOI marker (FF D9), stepping over marker
        segments that sit between the scans of a progressive JPEG."""
        stop = at + limit
        while pos < stop:
            data = self._read(pos, min(MB, stop - pos) + 1)
            if len(data) < 2:
                return None
            i, jumped = 0, False
            while True:
                i = data.find(b"\xFF", i)
                if i < 0 or i + 1 >= len(data):
                    break
                nxt = data[i + 1]
                if nxt == 0xD9:
                    return pos + i + 2 - at, "jpg"
                if nxt == 0x00 or nxt == 0xFF or 0xD0 <= nxt <= 0xD7:
                    i += 1
                    continue
                if 0xC0 <= nxt <= 0xFE:
                    length = struct.unpack(">H", self._read(pos + i + 2, 2))[0]
                    if length < 2:
                        return None
                    pos, jumped = pos + i + 2 + length, True
                    break
                return None  # invalid byte after FF: not a JPEG (or corrupted)
            if not jumped:
                pos += len(data) - 1
        return None

    def _png(self, at: int, limit: int):
        w = _Window(self.reader)
        pos = at + 8
        first = True
        while pos < at + limit:
            head = w.get(pos, 8)
            if len(head) < 8:
                return None
            length, ctype = struct.unpack(">I4s", head)
            if not ctype.isalpha() or length > limit:
                return None
            if first and (ctype != b"IHDR" or length != 13):
                return None
            first = False
            pos += 12 + length
            if ctype == b"IEND":
                return pos - at, "png"
        return None

    def _gif(self, at: int, limit: int):
        w = _Window(self.reader, 256 * 1024)
        head = w.get(at, 13)
        if len(head) < 13:
            return None
        width, height, flags = struct.unpack_from("<HHB", head, 6)
        if not width or not height:
            return None
        pos = at + 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
        images = 0
        stop = at + limit

        def sub_blocks(p):
            while p < stop:
                n = w.get(p, 1)
                if not n:
                    return -1
                p += 1
                if n[0] == 0:
                    return p
                p += n[0]
            return -1

        while pos < stop:
            b = w.get(pos, 1)
            if not b:
                return None
            if b[0] == 0x3B:
                return (pos + 1 - at, "gif") if images else None
            if b[0] == 0x21:
                pos = sub_blocks(pos + 2)
            elif b[0] == 0x2C:
                d = w.get(pos, 11)
                if len(d) < 11:
                    return None
                pos += 10 + (3 << ((d[9] & 7) + 1) if d[9] & 0x80 else 0)
                code = w.get(pos, 1)
                if not code or not 1 <= code[0] <= 12:
                    return None
                pos = sub_blocks(pos + 1)
                images += 1
            else:
                return None
            if pos < 0:
                return None
        return None

    def _bmp(self, at: int, limit: int):
        head = self._read(at, 30)
        size, _, data_off, dib = struct.unpack_from("<IIII", head, 2)
        w, h = struct.unpack_from("<ii", head, 18)
        planes = struct.unpack_from("<H", head, 26)[0]
        if dib not in (12, 40, 52, 56, 108, 124) or planes != 1 or not (54 <= size <= limit):
            return None
        if not (0 < w < 65536 and 0 < abs(h) < 65536) or data_off >= size:
            return None
        return size, "bmp"

    def _tiff(self, at: int, limit: int):
        w = _Window(self.reader, 256 * 1024)
        head = w.get(at, 16)
        bo = "<" if head[:2] == b"II" else ">"
        magic, first = struct.unpack(bo + "HI", head[2:8])
        if magic not in (42, 0x4F52, 0x5352) or not 8 <= first < limit:
            return None
        u16, u32 = struct.Struct(bo + "H"), struct.Struct(bo + "I")
        queue, seen = [first], set()
        max_end, tags, has_data, ifds = 8, {}, False, 0

        def values(typ, count, raw_val, voff):
            size = _TIFF_TYPES[typ] * count
            if count > 1_000_000:
                return []
            data = raw_val[:size] if size <= 4 else w.get(at + voff, size)
            if typ == 3:
                return list(struct.unpack(f"{bo}{len(data) // 2}H", data[:len(data) // 2 * 2]))
            if typ in (4, 13):
                return list(struct.unpack(f"{bo}{len(data) // 4}I", data[:len(data) // 4 * 4]))
            return [data]

        while queue and ifds < 64:
            off = queue.pop(0)
            if off in seen or off < 8 or off + 2 > limit:
                continue
            seen.add(off)
            ifds += 1
            n = u16.unpack(w.get(at + off, 2))[0]
            if not 1 <= n <= 1000:
                if off == first:
                    return None
                continue
            ent = w.get(at + off + 2, n * 12 + 4)
            if len(ent) < n * 12 + 4:
                return None
            max_end = max(max_end, off + 2 + n * 12 + 4)
            nxt = u32.unpack_from(ent, n * 12)[0]
            if nxt:
                queue.append(nxt)
            offs, counts = {}, {}
            for e in range(n):
                tag, typ, count = struct.unpack_from(bo + "HHI", ent, e * 12)
                raw_val = ent[e * 12 + 8:e * 12 + 12]
                if typ not in _TIFF_TYPES:
                    if off == first:
                        return None
                    continue
                nbytes = _TIFF_TYPES[typ] * count
                voff = u32.unpack(raw_val)[0]
                if nbytes > 4:
                    if voff + nbytes > limit:
                        if off == first and tag in (273, 279, 256, 257):
                            return None
                        continue
                    max_end = max(max_end, voff + nbytes)
                if tag in (273, 279, 324, 325, 513, 514, 330, 34665, 34853, 271, 50706, 256):
                    vals = values(typ, count, raw_val, voff)
                    tags.setdefault(tag, vals)
                    if tag in (273, 324, 513):
                        offs[tag] = vals
                    elif tag in (279, 325, 514):
                        counts[{279: 273, 325: 324, 514: 513}[tag]] = vals
                    elif tag in (330, 34665, 34853):
                        queue.extend(v for v in vals if isinstance(v, int))
            for t, ov in offs.items():
                cv = counts.get(t, [])
                for o, c in zip(ov, cv):
                    if isinstance(o, int) and isinstance(c, int) and c:
                        if o + c > limit:
                            return None
                        max_end = max(max_end, o + c)
                        has_data = True
        if 256 not in tags or not has_data:
            return None
        make = tags.get(271, [b""])[0]
        make = make.upper() if isinstance(make, bytes) else b""
        if 50706 in tags:
            ext = "dng"
        elif head[8:10] == b"CR":
            ext = "cr2"
        elif magic != 42 or make.startswith(b"OLYMPUS"):
            ext = "orf"
        elif make.startswith(b"NIKON"):
            ext = "nef"
        elif make.startswith(b"SONY"):
            ext = "arw"
        else:
            ext = "tif"
        return max_end, ext

    def _psd(self, at: int, limit: int):
        head = self._read(at, 26)
        ver, = struct.unpack_from(">H", head, 4)
        ch, h, wd, depth, mode = struct.unpack_from(">HIIHH", head, 12)
        if not 1 <= ch <= 56 or not h or not wd or depth not in (1, 8, 16, 32) or mode > 9:
            return None
        if ver == 1 and (h > 30000 or wd > 30000):
            return None
        pos = at + 26
        for wide in (False, False, ver == 2):  # colour mode data, image resources, layer & mask info
            n = 8 if wide else 4
            raw = self._read(pos, n)
            if len(raw) < n:
                return None
            pos += n + int.from_bytes(raw, "big")
            if pos - at > limit:
                return None
        comp = struct.unpack(">H", self._read(pos, 2))[0]
        pos += 2
        rows = ch * h
        if comp == 0:
            pos += rows * ((wd * depth + 7) // 8)
        elif comp == 1:
            cw = 4 if ver == 2 else 2
            table = self._read(pos, rows * cw)
            if len(table) < rows * cw:
                return None
            fmt = ">%d%s" % (rows, "I" if cw == 4 else "H")
            pos += rows * cw + sum(struct.unpack(fmt, table))
        else:
            return None  # zip-compressed image data has no stored length
        size = pos - at
        return (size, "psd") if size <= limit else None

    # ================================================================= documents
    _PDF_NEEDLES = re.compile(rb"%%EOF|%PDF-")

    def _pdf(self, at: int, limit: int):
        if b"obj" not in self._read(at, 2048):
            return None  # a real PDF starts its first object right after the header
        end, m = self._search_forward(at + 8, self._PDF_NEEDLES, limit - 8)
        if end < 0 or m.group() != b"%%EOF":
            return None  # no end, or another PDF began before this one ended
        # incremental saves append more %%EOF markers shortly after
        while True:
            nxt, m = self._search_forward(end + 5, self._PDF_NEEDLES, 256 * 1024)
            if nxt < 0 or m.group() != b"%%EOF" or nxt - at > limit:
                break
            end = nxt
        tail = self._read(end + 5, 2)
        extra = len(tail) - len(tail.lstrip(b"\r\n"))
        return end + 5 + extra - at, "pdf"

    _ZIP_NEXT = (b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06", b"PK\x06\x06", b"PK\x07\x08", b"PK\x06\x08")

    def _zip(self, at: int, limit: int):
        lfh = self._read(at, 30)
        version, flags, method = struct.unpack_from("<HHH", lfh, 4)
        csize = struct.unpack_from("<I", lfh, 18)[0]
        name_len, extra_len = struct.unpack_from("<HH", lfh, 26)
        if (version & 0xFF) > 63 or method not in (0, 8, 9, 12, 14, 93, 95, 98, 99) or not 0 < name_len < 1024:
            return None
        name = self._read(at + 30, name_len)
        if len(name) < name_len or min(name) < 0x20:
            return None
        if not flags & 0x08 and csize != 0xFFFFFFFF:
            nxt = self._read(at + 30 + name_len + extra_len + csize, 4)
            if nxt not in self._ZIP_NEXT:
                return None
        start = at + 30
        for _ in range(16):  # embedded zips have their own end records; skip those
            end = self._find_forward(start, b"PK\x05\x06", at + limit - start)
            if end < 0:
                return None
            eocd = self._read(end, 22)
            if len(eocd) < 22:
                return None
            cd_size, cd_off, comment = struct.unpack_from("<IIH", eocd, 12)
            cd_end_expected = end
            note = ""
            if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
                loc = self._read(end - 20, 20)
                if loc[:4] == b"PK\x06\x07":
                    z64 = struct.unpack_from("<Q", loc, 8)[0]
                    rec = self._read(at + z64, 56)
                    if rec[:4] == b"PK\x06\x06":
                        cd_size, cd_off = struct.unpack_from("<QQ", rec, 40)
                        cd_end_expected = at + z64
                        note = "ZIP64"
            ok = at + cd_off + cd_size == cd_end_expected and (
                cd_size == 0 or self._read(at + cd_off, 4) == b"PK\x01\x02")
            if ok:
                size = end + 22 + comment - at
                if size > limit:
                    return None
                names = self._read(at + cd_off, min(cd_size, 256 * 1024))
                first = self._read(at, 30 + name_len + 80)
                return size, self._zip_kind(names, first), note
            start = end + 4
        return None

    @staticmethod
    def _zip_kind(names: bytes, first: bytes) -> str:
        if b"mimetypeapplication/vnd.oasis.opendocument.text" in first:
            return "odt"
        if b"mimetypeapplication/vnd.oasis.opendocument.spreadsheet" in first:
            return "ods"
        if b"mimetypeapplication/vnd.oasis.opendocument.presentation" in first:
            return "odp"
        if b"mimetypeapplication/epub+zip" in first:
            return "epub"
        if b"word/" in names:
            return "docx"
        if b"xl/" in names:
            return "xlsx"
        if b"ppt/" in names:
            return "pptx"
        if b"AndroidManifest.xml" in names and b"classes" in names:
            return "apk"
        if b"META-INF/MANIFEST.MF" in names:
            return "jar"
        return "zip"

    def _ole(self, at: int, limit: int):
        head = self._read(at, 512)
        minor, major, bom, shift = struct.unpack_from("<HHHH", head, 0x18)
        if bom != 0xFFFE or (major, shift) not in ((3, 9), (4, 12)):
            return None
        ss = 1 << shift
        n_fat, first_dir = struct.unpack_from("<II", head, 0x2C)
        first_difat, n_difat = struct.unpack_from("<II", head, 0x44)
        if not 0 < n_fat <= limit // (ss * ss // 4) + 1:
            return None
        fat_sectors = [s for s in struct.unpack_from("<109I", head, 0x4C) if s < 0xFFFFFFFA]
        d, hops = first_difat, 0
        while d < 0xFFFFFFFA and hops < n_difat and len(fat_sectors) < n_fat:
            raw = self._read(at + (d + 1) * ss, ss)
            if len(raw) < ss:
                return None
            vals = struct.unpack(f"<{ss // 4}I", raw)
            fat_sectors += [s for s in vals[:-1] if s < 0xFFFFFFFA]
            d, hops = vals[-1], hops + 1
        fat_sectors = fat_sectors[:n_fat]
        if len(fat_sectors) < n_fat:
            return None
        per = ss // 4
        max_used = -1
        fat: List[int] = []
        w = _Window(self.reader)
        for i, s in enumerate(fat_sectors):
            if (s + 2) * ss > limit:
                return None
            raw = w.get(at + (s + 1) * ss, ss)
            if len(raw) < ss:
                return None
            vals = struct.unpack(f"<{per}I", raw)
            if len(fat) < 4_000_000:
                fat.extend(vals)
            for j in range(per - 1, -1, -1):
                if vals[j] != 0xFFFFFFFF:
                    max_used = max(max_used, i * per + j)
                    break
        if max_used < 0:
            return None
        size = (max_used + 2) * ss
        if size > limit:
            return None
        # name the file by the streams in its root directory
        names, root_clsid = set(), b""
        s, hops = first_dir, 0
        while s < len(fat) and hops < 16:
            raw = self._read(at + (s + 1) * ss, ss)
            for e in range(0, len(raw) - 127, 128):
                n = struct.unpack_from("<H", raw, e + 64)[0]
                if 2 <= n <= 64:
                    names.add(raw[e:e + n - 2].decode("utf-16-le", "ignore"))
                if hops == 0 and e == 0:
                    root_clsid = raw[80:96]
            s, hops = fat[s], hops + 1
        if "Root Entry" not in names:
            return None
        if "WordDocument" in names:
            ext = "doc"
        elif "Workbook" in names or "Book" in names:
            ext = "xls"
        elif "PowerPoint Document" in names:
            ext = "ppt"
        elif any(n.startswith("__substg1.0_") for n in names):
            ext = "msg"
        elif root_clsid == uuid.UUID("000C1084-0000-0000-C000-000000000046").bytes_le:
            ext = "msi"
        else:
            ext = "ole"
        return size, ext

    # ================================================================= archives
    def _rar(self, at: int, limit: int):
        sig = self._read(at, 8)
        return self._rar5(at, limit) if sig[6] == 1 else self._rar4(at, limit)

    def _rar4(self, at: int, limit: int):
        w = _Window(self.reader)
        pos, files, main = at + 7, 0, False
        while pos < at + limit:
            h = w.get(pos, 7)
            if len(h) < 7:
                break
            crc, typ, flags, hsize = struct.unpack("<HBHH", h)
            if not 0x73 <= typ <= 0x7B or hsize < 7:
                break
            hdr = w.get(pos, hsize)
            if len(hdr) < hsize or zlib.crc32(hdr[2:]) & 0xFFFF != crc:
                break
            add = 0
            if typ in (0x74, 0x7A):
                if hsize < 32:
                    break
                add = struct.unpack_from("<I", hdr, 7)[0]
                if flags & 0x100 and hsize >= 36:
                    add |= struct.unpack_from("<I", hdr, 32)[0] << 32
                files += typ == 0x74
            elif flags & 0x8000:
                add = struct.unpack_from("<I", hdr, 7)[0]
            main |= typ == 0x73
            pos += hsize + add
            if typ == 0x7B:
                return pos - at, "rar"
        if main and files and pos - at <= limit:
            return pos - at, "rar", "No end-of-archive marker"
        return None

    def _rar5(self, at: int, limit: int):
        w = _Window(self.reader)
        pos, n = at + 8, 0
        while pos < at + limit:
            h = w.get(pos, 4 + 3)
            if len(h) < 7:
                return None
            crc = struct.unpack_from("<I", h)[0]
            size, p = _vint5(h, 4)
            if size is None or not 2 <= size <= 2 * MB:
                return None
            body = w.get(pos + 4, p - 4 + size)
            if len(body) < p - 4 + size or zlib.crc32(body) != crc:
                return None
            q = p - 4
            typ, q = _vint5(body, q)
            flags, q = _vint5(body, q)
            if typ is None or flags is None or not 1 <= typ <= 5 or (n == 0 and typ not in (1, 4)):
                return None
            if flags & 1:
                _, q = _vint5(body, q)
            data = 0
            if flags & 2:
                data, q = _vint5(body, q)
                if data is None:
                    return None
            pos += 4 + len(body) + data
            n += 1
            if typ == 5:
                return pos - at, "rar"
        return None

    def _7z(self, at: int, limit: int):
        head = self._read(at, 32)
        major = head[6]
        start_crc = struct.unpack_from("<I", head, 8)[0]
        next_off, next_size = struct.unpack_from("<QQ", head, 12)
        size = 32 + next_off + next_size
        if major != 0 or zlib.crc32(head[12:32]) != start_crc or not 32 < size <= limit:
            return None
        return size, "7z"

    # ================================================================= audio / video
    _MP4_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"pnot", b"udta",
                  b"meta", b"moof", b"mfra", b"sidx", b"styp", b"pdin", b"iinf", b"iloc", b"idat"}
    _MP4_BRANDS = {b"qt  ": "mov", b"M4A ": "m4a", b"M4B ": "m4a", b"M4V ": "m4v", b"M4VH": "m4v",
                   b"heic": "heic", b"heix": "heic", b"mif1": "heic", b"msf1": "heic", b"hevc": "heic",
                   b"avif": "avif", b"avis": "avif", b"crx ": "cr3", b"3gp4": "3gp", b"3gp5": "3gp",
                   b"3gp6": "3gp", b"3g2a": "3gp"}

    def _mp4(self, at: int, limit: int):
        brand = self._read(at + 8, 4)
        first = struct.unpack(">I", self._read(at, 4))[0]
        if not 16 <= first <= 1024 or not brand.isascii():
            return None
        pos, seen = at, set()
        while pos < at + limit:
            head = self._read(pos, 16)
            if len(head) < 8:
                break
            size, btype = struct.unpack(">I4s", head[:8])
            if btype not in self._MP4_BOXES:
                break
            if size == 1:
                size = struct.unpack(">Q", head[8:16])[0]
            elif size == 0:
                return None  # "extends to end of file": can't bound it
            if size < 8:
                return None
            seen.add(btype)
            pos += size
        if b"ftyp" not in seen or not ({b"moov", b"mdat", b"meta", b"moof"} & seen) or pos - at > limit:
            return None
        return pos - at, self._MP4_BRANDS.get(brand, "mp4")

    def _mkv(self, at: int, limit: int):
        head = self._read(at, 64)
        _, p = _ebml_id(head, 0)
        hsize, p, unknown = _ebml_size(head, p)
        if hsize is None or unknown or hsize > 256:
            return None
        body = self._read(at + p, hsize)
        doctype, q = None, 0
        while q < len(body):
            eid, q2 = _ebml_id(body, q)
            esz, q3, unk = _ebml_size(body, q2)
            if eid is None or esz is None or unk:
                return None
            if eid == 0x4282:
                doctype = body[q3:q3 + esz].rstrip(b"\x00")
            q = q3 + esz
        if doctype not in (b"matroska", b"webm"):
            return None
        ext = "webm" if doctype == b"webm" else "mkv"
        pos = at + p + hsize
        seg = self._read(pos, 12)
        sid, q = _ebml_id(seg, 0)
        ssize, q, unknown = _ebml_size(seg, q)
        if sid != 0x18538067 or ssize is None:
            return None
        pos += q
        if not unknown:
            end = pos + ssize - at
            return (end, ext) if end <= limit else None
        # live-recorded file: walk top-level elements until something that isn't one
        w = _Window(self.reader)
        stop = at + limit
        in_cluster = False
        while pos < stop:
            h = w.get(pos, 12)
            eid, q = _ebml_id(h, 0)
            esz, q, unk = _ebml_size(h, q)
            if eid is None or esz is None:
                break
            if eid in _MKV_TOP or (in_cluster and eid in _MKV_CLUSTER_CHILDREN):
                if unk:
                    if eid != 0x1F43B675:
                        break
                    in_cluster = True  # children follow directly
                    pos += q
                    continue
                if eid == 0x1F43B675:
                    in_cluster = False
                pos += q + esz
            else:
                break
        end = pos - at
        return (end, ext, "Recorded without a final size") if 64 < end <= limit else None

    _ASF_HEADER = uuid.UUID("75B22630-668E-11CF-A6D9-00AA0062CE6C").bytes_le

    def _asf(self, at: int, limit: int):
        head = self._read(at, 30)
        hsize, nobj = struct.unpack_from("<QI", head, 16)
        if head[28:30] != b"\x01\x02" or not 30 < hsize <= 16 * MB or not 0 < nobj < 1000:
            return None
        hdr = self._read(at, hsize)
        if len(hdr) < hsize:
            return None
        pos, file_size, video = 30, 0, False
        for _ in range(nobj):
            if pos + 24 > hsize:
                return None
            guid, osize = hdr[pos:pos + 16], struct.unpack_from("<Q", hdr, pos + 16)[0]
            if osize < 24 or pos + osize > hsize:
                return None
            if guid == _ASF_FILE_PROPS:
                file_size = struct.unpack_from("<Q", hdr, pos + 40)[0]
            elif guid == _ASF_STREAM_PROPS and hdr[pos + 24:pos + 40] == _ASF_VIDEO:
                video = True
            pos += osize
        ext = "wmv" if video else "wma"
        if hsize < file_size <= limit:
            return file_size, ext
        data = self._read(at + hsize, 24)
        if data[:16] != _ASF_DATA:
            return None
        size = hsize + struct.unpack_from("<Q", data, 16)[0]
        return (size, ext, "Size taken from the data section") if size <= limit else None

    def _riff(self, at: int, limit: int):
        head = self._read(at, 12)
        size = struct.unpack_from("<I", head, 4)[0] + 8
        kind = head[8:12]
        if not 12 < size <= limit:
            return None
        return size, {b"WAVE": "wav", b"AVI ": "avi", b"WEBP": "webp"}[kind]

    def _mp3(self, at: int, limit: int):
        w = _Window(self.reader)
        head = w.get(at, 10)
        if head[5] & 0x0F or any(b & 0x80 for b in head[6:10]):
            return None
        tag = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        pos = at + 10 + tag + (10 if head[5] & 0x10 else 0)
        # some encoders pad between the tag and the first frame
        gap = w.get(pos, 4096)
        i = gap.find(b"\xFF")
        if i < 0 or any(gap[:i]):
            return None
        pos += i
        frames, sig, stop = 0, None, at + limit
        while pos < stop:
            fr = mp3_frame(w.get(pos, 4))
            if fr is None or (sig is not None and fr[1] != sig):
                break
            sig = fr[1]
            pos += fr[0]
            frames += 1
        if frames < 4:
            return None
        if w.get(pos, 3) == b"TAG":
            pos += 128
        return (pos - at, "mp3") if pos - at <= limit else None

    def _ogg(self, at: int, limit: int):
        w = _Window(self.reader)
        pos, stop = at, at + limit
        open_streams, pages, ext = set(), 0, "ogg"
        while pos < stop:
            h = w.get(pos, 27)
            if len(h) < 27 or h[:4] != b"OggS" or h[4] != 0 or h[5] & ~0x07:
                break
            flags, serial, nseg = h[5], struct.unpack_from("<I", h, 14)[0], h[26]
            table = w.get(pos + 27, nseg)
            if len(table) < nseg:
                break
            size = 27 + nseg + sum(table)
            if pages == 0:
                page = w.get(pos, size)
                if not flags & 2 or len(page) < size or \
                        ogg_crc(page[:22] + b"\0\0\0\0" + page[26:]) != struct.unpack_from("<I", h, 22)[0]:
                    return None
                body = page[27 + nseg:]
                if body.startswith(b"OpusHead"):
                    ext = "opus"
                elif body.startswith(b"\x7fFLAC"):
                    ext = "oga"
            if flags & 2:
                open_streams.add(serial)
            elif serial not in open_streams:
                break
            pages += 1
            pos += size
            if flags & 4:
                open_streams.discard(serial)
                if not open_streams:
                    return pos - at, ext
        if pages >= 3 and pos - at <= limit:
            return pos - at, ext, "No end-of-stream marker"
        return None

    def _flac(self, at: int, limit: int):
        w = _Window(self.reader)
        pos, total, max_frame, last = at + 4, 0, 0, False
        first = True
        while not last:
            h = w.get(pos, 4)
            if len(h) < 4:
                return None
            last, btype, blen = bool(h[0] & 0x80), h[0] & 0x7F, int.from_bytes(h[1:4], "big")
            if btype == 127 or (first and (btype != 0 or blen != 34)):
                return None
            if first:
                si = w.get(pos + 4, 34)
                max_frame = int.from_bytes(si[7:10], "big")
                total = int.from_bytes(si[13:18], "big") & 0xFFFFFFFFF
                first = False
            pos += 4 + blen
            if pos - at > limit:
                return None
        stop = at + limit
        samples, frames, expect, variable = 0, 0, 0, None
        search = max(max_frame, 64 * 1024) + 16
        while pos < stop:
            buf = w.get(pos, search + 32)
            hdr = flac_frame_header(buf, 0)
            if hdr is None:
                break
            hlen, bs, number = hdr
            var = buf[1] == 0xF9
            if variable is None:
                variable = var
            if var != variable or number != (samples if variable else expect):
                break
            frames += 1
            samples += bs
            expect += 1
            if total and samples >= total:
                end = self._flac_frame_end(buf, hlen)
                return (pos + end - at, "flac") if end else None
            # the next frame header: same sync bytes, valid CRC-8, next frame number
            i, nxt = hlen, None
            while True:
                i = buf.find(buf[:2], i)
                if i < 0:
                    break
                h2 = flac_frame_header(buf, i)
                if h2 and h2[2] == (samples if variable else expect):
                    nxt = i
                    break
                i += 1
            if nxt is None:
                end = self._flac_frame_end(buf, hlen)
                if not end:
                    break
                return pos + end - at, "flac", "Shorter than its stated length"
            pos += nxt
        return None

    @staticmethod
    def _flac_frame_end(buf: bytes, hlen: int) -> int:
        """End of the last frame: the first spot where the running CRC-16 matches the next 2 bytes."""
        c = flac_crc16(buf[:hlen + 1])
        for i in range(hlen + 1, len(buf) - 1):
            if c == (buf[i] << 8 | buf[i + 1]):
                return i + 2
            c = ((c << 8) & 0xFFFF) ^ _CRC16[(c >> 8) ^ buf[i]]
        return 0

    # ================================================================= data
    def _sqlite(self, at: int, limit: int):
        head = self._read(at, 100)
        page = struct.unpack_from(">H", head, 16)[0]
        page = 65536 if page == 1 else page
        pages = struct.unpack_from(">I", head, 28)[0]
        if page < 512 or page & (page - 1) or not pages:
            return None
        if head[18] not in (1, 2) or head[19] not in (1, 2) or head[21:24] != b"\x40\x20\x20":
            return None
        size = page * pages
        return (size, "sqlite") if size <= limit else None

    def _pe(self, at: int, limit: int):
        mz = self._read(at, 64)
        lfanew = struct.unpack_from("<I", mz, 0x3C)[0]
        if not 64 <= lfanew <= 4096:
            return None
        pe = self._read(at + lfanew, 24 + 240)
        if pe[:4] != b"PE\x00\x00":
            return None
        nsec, = struct.unpack_from("<H", pe, 6)
        opt_size, chars = struct.unpack_from("<HH", pe, 20)
        if not 1 <= nsec <= 96 or opt_size < 96:
            return None
        magic, = struct.unpack_from("<H", pe, 24)
        if magic not in (0x10B, 0x20B):
            return None
        opt = self._read(at + lfanew + 24, opt_size)
        size = struct.unpack_from("<I", opt, 60)[0]  # SizeOfHeaders
        dd = 96 if magic == 0x10B else 112
        cert_off = cert_len = 0
        if opt_size >= dd + 5 * 8:
            cert_off, cert_len = struct.unpack_from("<II", opt, dd + 4 * 8)
        sec = self._read(at + lfanew + 24 + opt_size, nsec * 40)
        if len(sec) < nsec * 40:
            return None
        for i in range(nsec):
            raw_size, raw_ptr = struct.unpack_from("<II", sec, i * 40 + 16)
            if raw_size:
                size = max(size, raw_ptr + raw_size)
        if cert_off and cert_len and cert_off + cert_len <= limit:
            size = max(size, cert_off + cert_len)  # the signature sits after any appended data
        if not 512 <= size <= limit:
            return None
        return size, "dll" if chars & 0x2000 else "exe"
