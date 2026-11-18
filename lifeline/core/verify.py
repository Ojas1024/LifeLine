"""Check whether a recovered (or about-to-be-recovered) file is intact.

Every check returns ("ok" | "damaged" | "unknown", short plain-English detail).
Structural checks reuse the carver's format walkers on an in-memory copy; formats
with checksums (PNG, ZIP, Ogg, 7z, RAR, SQLite) get their checksums tested too.
Nothing here raises, and work is capped so a huge file can't stall the app.
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import struct
import tempfile
import zipfile
import zlib
from typing import Optional, Tuple

from .carver import Carver, _vint5, ogg_crc

MB = 1024 * 1024
CAP = 256 * MB
OK, DAMAGED, UNKNOWN = "ok", "damaged", "unknown"
Result = Tuple[str, str]

ZIP_FAMILY = {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "jar", "apk"}
# Formats whose bytes are compressed or encoded: long runs of zeros there mean lost data.
# (Uncompressed formats such as BMP or WAV can legitimately contain silence or black.)
COMPRESSED = ZIP_FAMILY | {"jpg", "jpeg", "png", "gif", "pdf", "mp3", "mp4", "mov", "m4a", "m4v", "heic", "avif",
                           "cr3", "3gp", "mkv", "webm", "ogg", "opus", "oga", "flac", "rar", "7z", "wmv", "wma",
                           "webp"}

# Carver notes that mean the file is incomplete (others, like "ZIP64", are just information).
DAMAGE_NOTES = {"No end-of-archive marker", "No end-of-stream marker", "Shorter than its stated length"}

_EXTRACTOR = {
    "jpg": "_jpeg", "jpeg": "_jpeg", "png": "_png", "gif": "_gif", "bmp": "_bmp", "psd": "_psd",
    "tif": "_tiff", "tiff": "_tiff", "cr2": "_tiff", "nef": "_tiff", "arw": "_tiff", "dng": "_tiff", "orf": "_tiff",
    "pdf": "_pdf", "doc": "_ole", "xls": "_ole", "ppt": "_ole", "msg": "_ole", "msi": "_ole", "ole": "_ole",
    "rar": "_rar", "7z": "_7z", "mp4": "_mp4", "mov": "_mp4", "m4a": "_mp4", "m4v": "_mp4", "heic": "_mp4",
    "avif": "_mp4", "cr3": "_mp4", "3gp": "_mp4", "mkv": "_mkv", "webm": "_mkv", "wmv": "_asf", "wma": "_asf",
    "asf": "_asf", "wav": "_riff", "avi": "_riff", "webp": "_riff", "mp3": "_mp3", "ogg": "_ogg", "opus": "_ogg",
    "oga": "_ogg", "flac": "_flac", "sqlite": "_sqlite", "db": "_sqlite", "exe": "_pe", "dll": "_pe",
}


class _BytesReader:
    def __init__(self, data: bytes):
        self.data = data
        self.length = len(data)

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size <= 0:
            return b""
        return self.data[offset:offset + size]


def zero_check(data: bytes, ext: str) -> Optional[Result]:
    if not data:
        return DAMAGED, "The file is empty."
    if not any(data):
        return DAMAGED, "The file contains only zeros (the drive erased it, for example SSD TRIM)."
    blocks = max(1, len(data) // 4096)
    if blocks >= 4 and ext in COMPRESSED:
        z = bytes(4096)
        empty = sum(1 for i in range(0, blocks * 4096, 4096) if data[i:i + 4096] == z)
        if empty / blocks > 0.3:
            return DAMAGED, f"About {empty / blocks:.0%} of the file is blank, so parts of it were erased."
    return None


def verify_bytes(data: bytes, ext: str, full_size: Optional[int] = None) -> Result:
    """Check a file held in memory. full_size: the real size when `data` is only the first part."""
    try:
        return _verify(data, ext.lower().lstrip("."), full_size)
    except Exception:  # a checker bug must never stop a recovery
        return UNKNOWN, "The automatic check couldn't read this file."


def verify_file(path: str, ext: Optional[str] = None) -> Result:
    try:
        ext = (ext or os.path.splitext(path)[1]).lower().lstrip(".")
        size = os.path.getsize(path)
        if ext in ZIP_FAMILY and size > CAP:
            with zipfile.ZipFile(path) as z:  # too big to test every CRC: check the directory only
                z.namelist()
            return OK, "Its table of contents is intact (contents not fully tested: very large file)."
        with open(path, "rb") as f:
            data = f.read(CAP)
        return verify_bytes(data, ext, size)
    except zipfile.BadZipFile:
        return DAMAGED, "The archive's table of contents is damaged."
    except OSError as e:
        return UNKNOWN, f"Couldn't open the file: {e.strerror or e}"
    except Exception:
        return UNKNOWN, "The automatic check couldn't read this file."


def _verify(data: bytes, ext: str, full_size: Optional[int]) -> Result:
    z = zero_check(data, ext)
    if z:
        return z
    partial = full_size is not None and full_size > len(data)
    special = {
        "png": _png, "sqlite": _sqlite, "db": _sqlite, "7z": _7z, "rar": _rar,
        "ogg": _ogg, "opus": _ogg, "oga": _ogg, "jpg": _jpeg, "jpeg": _jpeg, "pdf": _pdf,
    }
    if ext in ZIP_FAMILY:
        return _zip(data)
    if ext in special and not partial:
        res = special[ext](data)
        if res:
            return res
    name = _EXTRACTOR.get(ext)
    if not name:
        return UNKNOWN, "No automatic check for this type of file."
    if partial:
        return UNKNOWN, "Too large to check completely."
    try:
        res = getattr(Carver(_BytesReader(data)), name)(0, len(data))
    except (struct.error, ValueError, IndexError, KeyError, OverflowError):
        res = None
    if not res:
        return DAMAGED, "Its internal structure is broken, so it may not open."
    size = res[0]
    if size > len(data):
        return DAMAGED, f"It's cut short: only {len(data) / max(size, 1):.0%} of it is there."
    if len(res) > 2 and res[2] in DAMAGE_NOTES:
        return DAMAGED, f"It's incomplete ({res[2].lower()})."
    return OK, "Its structure checks out."


# ------------------------------------------------------------------ format checks
def _zip(data: bytes) -> Result:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
    except zipfile.BadZipFile:
        return DAMAGED, "The archive's table of contents is damaged."
    except (RuntimeError, NotImplementedError):
        return UNKNOWN, "It's encrypted or uses compression we can't test."
    except (zlib.error, EOFError, OSError, ValueError):
        return DAMAGED, "Part of the compressed data is damaged."
    if bad:
        return DAMAGED, f"'{bad}' inside it is damaged."
    return OK, "Every part passed its checksum."


def _png(data: bytes) -> Optional[Result]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return DAMAGED, "The PNG header is missing."
    pos = 8
    while pos + 12 <= len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        end = pos + 12 + length
        if end > len(data):
            return DAMAGED, "It's cut short."
        if zlib.crc32(data[pos + 4:pos + 8 + length]) != struct.unpack_from(">I", data, pos + 8 + length)[0]:
            return DAMAGED, f"The {ctype.decode('latin-1')} section failed its checksum."
        if ctype == b"IEND":
            return OK, "Every section passed its checksum."
        pos = end
    return DAMAGED, "It's cut short (no end marker)."


def _jpeg(data: bytes) -> Optional[Result]:
    if data[:3] != b"\xFF\xD8\xFF":
        return DAMAGED, "The JPEG header is missing."
    res = None
    try:
        res = Carver(_BytesReader(data))._jpeg(0, len(data))
    except (struct.error, ValueError, IndexError):
        pass
    if not res:
        tail = data.rstrip(b"\x00")
        if not tail.endswith(b"\xFF\xD9"):
            return DAMAGED, "The picture is cut short or its data is corrupted."
        return DAMAGED, "The picture data is corrupted."
    return OK, "The picture data is complete."


_STARTXREF = re.compile(rb"startxref\s+(\d+)\s+%%EOF")


def _pdf(data: bytes) -> Optional[Result]:
    if not data.startswith(b"%PDF-"):
        return DAMAGED, "The PDF header is missing."
    tail = data[-2048:]
    if b"%%EOF" not in tail:
        return DAMAGED, "It's cut short (no end marker)."
    m = None
    for m in _STARTXREF.finditer(tail):
        pass
    if not m:
        return DAMAGED, "Its cross-reference pointer is missing."
    off = int(m.group(1))
    target = data[off:off + 64]
    if target.startswith(b"xref") or re.match(rb"\s*\d+\s+\d+\s+obj", target):
        return OK, "Its cross-reference table is where it should be."
    return DAMAGED, "Its cross-reference table is missing or moved, so some readers may refuse it."


def _sqlite(data: bytes) -> Optional[Result]:
    if not data.startswith(b"SQLite format 3\x00") or len(data) < 100:
        return DAMAGED, "The database header is missing."
    page = struct.unpack_from(">H", data, 16)[0]
    page = 65536 if page == 1 else page
    pages = struct.unpack_from(">I", data, 28)[0]
    if page * pages > len(data):
        return DAMAGED, "It's cut short."
    if len(data) > 64 * MB:
        return OK, "Its header and size are consistent."
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
        if row and row[0] == "ok":
            return OK, "The database passed its integrity check."
        return DAMAGED, "The database failed its integrity check."
    except sqlite3.DatabaseError:
        return DAMAGED, "The database couldn't be opened."
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _7z(data: bytes) -> Optional[Result]:
    if len(data) < 32:
        return DAMAGED, "It's cut short."
    if zlib.crc32(data[12:32]) != struct.unpack_from("<I", data, 8)[0]:
        return DAMAGED, "The archive header failed its checksum."
    off, size, crc = struct.unpack_from("<QQI", data, 12)
    nh = data[32 + off:32 + off + size]
    if len(nh) < size:
        return DAMAGED, "It's cut short."
    if zlib.crc32(nh) != crc:
        return DAMAGED, "The archive's table of contents failed its checksum."
    return OK, "The archive's headers passed their checksums."


def _rar(data: bytes) -> Optional[Result]:
    if data[:7] == b"Rar!\x1a\x07\x00":
        pos = 7
        while pos + 7 <= len(data):
            crc, typ, flags, hsize = struct.unpack_from("<HBHH", data, pos)
            if hsize < 7 or pos + hsize > len(data):
                return DAMAGED, "It's cut short."
            if zlib.crc32(data[pos + 2:pos + hsize]) & 0xFFFF != crc:
                return DAMAGED, "A header inside the archive failed its checksum."
            add = 0
            if typ in (0x74, 0x7A):
                add = struct.unpack_from("<I", data, pos + 7)[0]
                if flags & 0x100:
                    add |= struct.unpack_from("<I", data, pos + 32)[0] << 32
                # stored (uncompressed, unsplit) files carry a CRC of their bytes
                if typ == 0x74 and data[pos + 25] == 0x30 and not flags & 0x07:
                    body = data[pos + hsize:pos + hsize + add]
                    if len(body) == add and zlib.crc32(body) != struct.unpack_from("<I", data, pos + 16)[0]:
                        return DAMAGED, "A file inside the archive failed its checksum."
            elif flags & 0x8000:
                add = struct.unpack_from("<I", data, pos + 7)[0]
            pos += hsize + add
            if typ == 0x7B:
                return OK, "Every header passed its checksum."
        return DAMAGED, "It's cut short (no end marker)."
    if data[:8] == b"Rar!\x1a\x07\x01\x00":
        pos = 8
        while pos + 7 <= len(data):
            crc = struct.unpack_from("<I", data, pos)[0]
            size, p = _vint5(data, pos + 4)
            if size is None or p + size > len(data):
                return DAMAGED, "It's cut short."
            if zlib.crc32(data[pos + 4:p + size]) != crc:
                return DAMAGED, "A header inside the archive failed its checksum."
            typ, q = _vint5(data, p)
            flags, q = _vint5(data, q)
            if flags & 1:
                _, q = _vint5(data, q)
            extra = 0
            if flags & 2:
                extra, q = _vint5(data, q)
            pos = p + size + (extra or 0)
            if typ == 5:
                return OK, "Every header passed its checksum."
        return DAMAGED, "It's cut short (no end marker)."
    return DAMAGED, "The RAR header is missing."


def _ogg(data: bytes) -> Optional[Result]:
    pos, pages = 0, 0
    while pos + 27 <= len(data):
        if data[pos:pos + 4] != b"OggS":
            break
        nseg = data[pos + 26]
        size = 27 + nseg + sum(data[pos + 27:pos + 27 + nseg])
        page = data[pos:pos + size]
        if len(page) < size:
            return DAMAGED, "It's cut short."
        if ogg_crc(page[:22] + b"\0\0\0\0" + page[26:]) != struct.unpack_from("<I", page, 22)[0]:
            return DAMAGED, f"Audio page {pages + 1} failed its checksum."
        pages += 1
        pos += size
    if not pages:
        return DAMAGED, "The Ogg header is missing."
    if pos < len(data) - 4096:
        return DAMAGED, "The audio breaks off partway through."
    return OK, "Every audio page passed its checksum."
