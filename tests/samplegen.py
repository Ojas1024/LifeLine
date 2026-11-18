"""Build small but structurally valid sample files for every carved format.

Only the standard library and PyQt6 (for real JPEG/PNG/BMP/TIFF/WebP encoders)
are used. Formats nobody ships a writer for (RAR, OLE, FLAC, ...) are built
byte by byte from their specifications.
"""
from __future__ import annotations

import io
import os
import random
import sqlite3
import struct
import tempfile
import uuid
import zipfile
import zlib
from typing import Dict, Tuple

from lifeline.core.carver import flac_crc16, crc8, ogg_crc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _noise(r: random.Random, n: int) -> bytes:
    return r.randbytes(n)


# ------------------------------------------------------------------ Qt images
_app = None


def _qt_image(fmt: str, w: int = 160, h: int = 120, seed: int = 1) -> bytes:
    global _app
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QColor, QGuiApplication, QImage
    if _app is None and QGuiApplication.instance() is None:
        from PyQt6.QtWidgets import QApplication  # a full QApplication so GUI tests can share it
        _app = QApplication([])
    r = _rng(seed)
    img = QImage(w, h, QImage.Format.Format_RGB32)
    for y in range(h):
        for x in range(w):
            img.setPixelColor(x, y, QColor((x * 3 + r.randrange(40)) % 256, (y * 2) % 256, (x ^ y) % 256))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, fmt), fmt
    return bytes(buf.data())


def jpeg() -> bytes:
    return _qt_image("JPEG")


def png() -> bytes:
    return _qt_image("PNG", seed=2)


def bmp() -> bytes:
    return _qt_image("BMP", 40, 30, seed=3)


def qt_tiff() -> bytes:
    return _qt_image("TIFF", 64, 48, seed=4)


def webp() -> bytes:
    return _qt_image("WEBP", 64, 48, seed=5)


# ------------------------------------------------------------------ GIF
def gif() -> bytes:
    r = _rng(6)
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", 16, 16, 0x80 | 0x01, 0, 0))
    out += bytes(r.randrange(256) for _ in range(12))           # 4-colour global table
    out += b"\x21\xF9\x04\x00\x0A\x00\x00\x00"                   # graphic control ext
    out += b"\x21\xFE\x06comm\x00\x3B\x00"                       # comment holding 00 3B (old search broke here)
    out += b"\x2C" + struct.pack("<HHHHB", 0, 0, 16, 16, 0) + b"\x02"
    for n in (255, 255, 40):
        out += bytes([n]) + _noise(r, n)
    out += b"\x00\x3B"
    return bytes(out)


# ------------------------------------------------------------------ TIFF (hand built, maker = NIKON)
def tiff_nef() -> bytes:
    w, h = 8, 4
    pixels = bytes(range(w * h * 3))
    entries = []
    make = b"NIKON CORPORATION\x00"
    hdr_len, n = 8, 10
    ifd_len = 2 + n * 12 + 4
    make_off = hdr_len + ifd_len
    strip_off = make_off + len(make) + 100      # gap, then pixel data last
    for tag, typ, count, val in [(256, 3, 1, w), (257, 3, 1, h), (258, 3, 1, 8), (259, 3, 1, 1),
                                 (262, 3, 1, 2), (271, 2, len(make), make_off), (273, 4, 1, strip_off),
                                 (277, 3, 1, 3), (278, 3, 1, h), (279, 4, 1, len(pixels))]:
        entries.append(struct.pack("<HHI", tag, typ, count) +
                       (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val)))
    out = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", n) + b"".join(entries) + b"\0\0\0\0"
    out += make + bytes(100) + pixels
    assert len(out) == strip_off + len(pixels)
    return out


# ------------------------------------------------------------------ PSD
def psd() -> bytes:
    ch, h, w = 3, 16, 16
    out = b"8BPS" + struct.pack(">H", 1) + bytes(6) + struct.pack(">HIIHH", ch, h, w, 8, 3)
    out += struct.pack(">I", 0) + struct.pack(">I", 12) + b"8BIM" + bytes(8) + struct.pack(">I", 0)
    out += struct.pack(">H", 0) + _noise(_rng(7), ch * h * w)
    return out


# ------------------------------------------------------------------ documents
def pdf() -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _zip(entries: Dict[str, bytes], first: Tuple[str, bytes] = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if first:
            z.writestr(zipfile.ZipInfo(first[0]), first[1])  # stored, like ODF's mimetype
        for name, data in entries.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def docx() -> bytes:
    r = _rng(8)
    return _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document>" + r.randbytes(3000).hex().encode() + b"</w:document>"})


def xlsx() -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>" * 50})


def odt() -> bytes:
    return _zip({"content.xml": b"<office:document-content/>" * 40},
                first=("mimetype", b"application/vnd.oasis.opendocument.text"))


def plain_zip() -> bytes:
    r = _rng(9)
    return _zip({"a.txt": b"hello " * 500, "b.bin": r.randbytes(5000)})


def zip64() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=True) as z:
        with z.open("big.txt", "w", force_zip64=True) as f:
            f.write(b"zip64 data " * 300)
    return buf.getvalue()


def sqlite_db() -> bytes:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    con = sqlite3.connect(path)
    con.execute("create table t (a integer, b text)")
    con.executemany("insert into t values (?, ?)", [(i, "row %d" % i * 5) for i in range(800)])
    con.commit()
    con.close()
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return data


def ole_doc() -> bytes:
    """Version-3 compound file: FAT in sector 0, directory in sector 1, WordDocument in 2..9."""
    ss = 512
    stream = _noise(_rng(10), 8 * ss)
    FREE, END, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    fat = [FATSECT, END] + list(range(3, 10)) + [END]
    fat += [FREE] * (128 - len(fat))
    head = bytearray(512)
    head[0:8] = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
    struct.pack_into("<HHHHH", head, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", head, 0x28, 0, 1, 1, 0, 4096, END, 0, END, 0)
    struct.pack_into("<109I", head, 0x4C, 0, *([FREE] * 108))

    def entry(name: str, typ: int, start: int, size: int, child=FREE) -> bytes:
        e = bytearray(128)
        n = (name + "\0").encode("utf-16-le")
        e[:len(n)] = n
        struct.pack_into("<HBB", e, 64, len(n), typ, 1)
        struct.pack_into("<III", e, 68, FREE, FREE, child)
        struct.pack_into("<IQ", e, 116, start, size)
        return bytes(e)
    directory = entry("Root Entry", 5, END, 0, child=1) + entry("WordDocument", 2, 2, len(stream))
    directory += bytes(ss - len(directory))
    return bytes(head) + struct.pack("<128I", *fat) + directory + stream


# ------------------------------------------------------------------ archives
def rar4() -> bytes:
    r = _rng(11)
    data = r.randbytes(3000)
    name = b"photo.jpg"

    def block(typ, flags, body):
        hdr = struct.pack("<BHH", typ, flags, 7 + len(body)) + body
        return struct.pack("<H", zlib.crc32(hdr) & 0xFFFF) + hdr
    out = b"Rar!\x1a\x07\x00"
    out += block(0x73, 0, bytes(6))
    fbody = struct.pack("<IIBIIBBHI", len(data), len(data), 2, zlib.crc32(data), 0, 29, 0x30, len(name), 0x20) + name
    out += block(0x74, 0x8000, fbody) + data
    out += block(0x7B, 0x4000, b"")
    return out


def _v(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def rar5() -> bytes:
    data = _noise(_rng(12), 2500)

    def header(typ, flags, fields, data_size=None):
        body = _v(typ) + _v(flags) + (_v(data_size) if data_size is not None else b"") + fields
        sized = _v(len(body)) + body
        return struct.pack("<I", zlib.crc32(sized)) + sized
    out = b"Rar!\x1a\x07\x01\x00"
    out += header(1, 0, _v(0))
    name = b"notes.txt"
    fields = _v(0) + _v(len(data)) + _v(0x20) + _v(0) + _v(0) + _v(len(name)) + name
    out += header(2, 0x02, fields, len(data)) + data
    out += header(5, 0, _v(0))
    return out


def seven_zip() -> bytes:
    packed = _noise(_rng(13), 4000)
    next_header = b"\x01\x04\x06\x00\x01\x09\x80\x00\x00"  # header with a minimal stream info shape
    start = struct.pack("<QQI", len(packed), len(next_header), zlib.crc32(next_header))
    return b"7z\xBC\xAF\x27\x1C\x00\x04" + struct.pack("<I", zlib.crc32(start)) + start + packed + next_header


# ------------------------------------------------------------------ audio/video
def _box(t: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + t + body


def mp4(brand: bytes = b"isom") -> bytes:
    r = _rng(14)
    return (_box(b"ftyp", brand + b"\0\0\x02\0" + brand + b"mp41") + _box(b"moov", _box(b"mvhd", bytes(100)))
            + _box(b"mdat", r.randbytes(6000)))


def mov() -> bytes:
    return mp4(b"qt  ")


def mkv(webm: bool = False, unknown: bool = False) -> bytes:
    def el(eid: int, body: bytes) -> bytes:
        idb = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
        n = len(body)
        size = (0x10000000 | n).to_bytes(4, "big")
        return idb + size + body
    doctype = b"webm" if webm else b"matroska"
    header = el(0x1A45DFA3, el(0x4286, b"\x01") + el(0x4282, doctype) + el(0x4287, b"\x04"))
    cluster = el(0x1F43B675, el(0xE7, b"\x00") + el(0xA3, _noise(_rng(15), 3000)))
    seg_body = el(0x1549A966, el(0x2AD7B1, b"\x0F\x42\x40")) + cluster + cluster
    if unknown:
        return header + b"\x18\x53\x80\x67" + b"\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF" + seg_body
    return header + el(0x18538067, seg_body)


def asf() -> bytes:
    def obj(guid: str, body: bytes) -> bytes:
        return uuid.UUID(guid).bytes_le + struct.pack("<Q", 24 + len(body)) + body
    stream = obj("B7DC0791-A9B7-11CF-8EE6-00C00C205365",
                 uuid.UUID("BC19EFC0-5B4D-11CF-A8FD-00805F5C442B").bytes_le + bytes(40))
    data_obj = obj("75B22636-668E-11CF-A6D9-00AA0062CE6C", bytes(16) + struct.pack("<QH", 5, 0x0101)
                   + _noise(_rng(16), 5000))
    fp_body_len = 16 + 8 + 8 + 8 + 8 + 8 + 8 + 8 + 4 + 4 + 4 + 4
    header_len = 30 + (24 + fp_body_len) + len(stream)
    total = header_len + len(data_obj)
    fileprops = obj("8CABDCA1-A947-11CF-8EE4-00C00C205365",
                    bytes(16) + struct.pack("<QQQQQQQIIII", total, 0, 5, 0, 0, 0, 0, 2, 3200, 3200, 0))
    head = uuid.UUID("75B22630-668E-11CF-A6D9-00AA0062CE6C").bytes_le + struct.pack("<QI", header_len, 2) + b"\x01\x02"
    out = head + fileprops + stream + data_obj
    assert len(out) == total
    return out


def wav() -> bytes:
    pcm = _noise(_rng(17), 8000)
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


def avi() -> bytes:
    body = b"AVI " + b"LIST" + struct.pack("<I", 4 + 64) + b"hdrl" + bytes(64) + b"LIST" + struct.pack("<I", 4 + 3000) + b"movi" + _noise(_rng(18), 3000)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mp3() -> bytes:
    r = _rng(19)
    frame_body = 417 - 4
    title = b"\x00Test song"
    tag_body = b"TIT2" + struct.pack(">I", len(title)) + b"\0\0" + title
    tag_body += bytes(64)  # padding inside the tag
    n = len(tag_body)
    syncsafe = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    out = b"ID3\x03\x00\x00" + syncsafe + tag_body
    for i in range(30):
        out += b"\xFF\xFB\x90\x00" + r.randbytes(frame_body).replace(b"\xFF", b"\xFE")
    out += b"TAG" + b"Test song".ljust(30, b"\0") + bytes(95)
    return out


def ogg(opus: bool = True) -> bytes:
    r = _rng(20)
    serial = 0x1234

    def page(flags: int, seq: int, granule: int, packets) -> bytes:
        table, body = bytearray(), b""
        for p in packets:
            n = len(p)
            while n >= 255:
                table.append(255)
                n -= 255
            table.append(n)
            body += p
        h = b"OggS" + bytes([0, flags]) + struct.pack("<qII", granule, serial, seq) + b"\0\0\0\0" + bytes([len(table)]) + bytes(table)
        crc = ogg_crc(h + body)
        return h[:22] + struct.pack("<I", crc) + h[26:] + body
    head = b"OpusHead\x01\x02\x38\x01\x80\xbb\x00\x00\x00\x00\x00" if opus else b"\x01vorbis" + bytes(23)
    out = page(2, 0, 0, [head]) + page(0, 1, 0, [b"OpusTags" + bytes(16)])
    for i in range(5):
        out += page(0, 2 + i, 960 * (i + 1), [r.randbytes(200), r.randbytes(150)])
    out += page(4, 7, 960 * 6, [r.randbytes(100)])
    return out


def flac(frames: int = 12) -> bytes:
    r = _rng(21)
    bs = 256
    total = bs * frames
    si = struct.pack(">HH", bs, bs) + (0).to_bytes(3, "big") + (2000).to_bytes(3, "big")
    # sample rate 44100 (20 bits), channels-1 = 0 (3), bps-1 = 15 (5), total samples (36)
    v = (44100 << 44) | (0 << 41) | (15 << 36) | total
    si += v.to_bytes(8, "big") + bytes(16)
    out = b"fLaC" + bytes([0x80]) + len(si).to_bytes(3, "big") + si
    for n in range(frames):
        hdr = bytes([0xFF, 0xF8, 0x89, 0x08]) + _utf8(n)   # bs code 8 (256), 44.1k, mono, 16-bit
        hdr += bytes([crc8(hdr)])
        samples = b"".join(struct.pack(">h", r.randrange(-30000, 30000)) for _ in range(bs))
        frame = hdr + b"\x02" + samples                   # VERBATIM subframe
        out += frame + struct.pack(">H", flac_crc16(frame))
    return out


def _utf8(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    return bytes([0xC0 | (n >> 6), 0x80 | (n & 0x3F)])


def pe(dll: bool = False) -> bytes:
    r = _rng(22)
    mz = bytearray(0x80)
    mz[:2] = b"MZ"
    struct.pack_into("<I", mz, 0x3C, 0x80)
    coff = struct.pack("<HHIIIHH", 0x14C, 2, 0, 0, 0, 224, 0x2102 if dll else 0x0102)
    opt = bytearray(224)
    struct.pack_into("<H", opt, 0, 0x10B)
    struct.pack_into("<I", opt, 60, 0x200)
    struct.pack_into("<I", opt, 92, 16)
    end = 0x800
    if dll:
        struct.pack_into("<II", opt, 96 + 4 * 8, 0x800, 0x100)
        end = 0x900
    sections = b""
    for name, ptr, size in ((b".text", 0x200, 0x400), (b".data", 0x600, 0x200)):
        sections += name.ljust(8, b"\0") + struct.pack("<IIIIIIHHI", size, ptr, size, ptr, 0, 0, 0, 0, 0)
    head = bytes(mz) + b"PE\0\0" + coff + bytes(opt) + sections
    out = head + bytes(0x200 - len(head)) + r.randbytes(end - 0x200)
    return out


# ------------------------------------------------------------------ registry
def all_samples() -> Dict[str, Tuple[bytes, str]]:
    """name -> (bytes, expected carved extension)."""
    return {
        "jpeg": (jpeg(), "jpg"), "png": (png(), "png"), "bmp": (bmp(), "bmp"), "gif": (gif(), "gif"),
        "qt_tiff": (qt_tiff(), "tif"), "nef": (tiff_nef(), "nef"), "webp": (webp(), "webp"), "psd": (psd(), "psd"),
        "pdf": (pdf(), "pdf"), "docx": (docx(), "docx"), "xlsx": (xlsx(), "xlsx"), "odt": (odt(), "odt"),
        "zip": (plain_zip(), "zip"), "zip64": (zip64(), "zip"), "sqlite": (sqlite_db(), "sqlite"),
        "doc": (ole_doc(), "doc"), "rar4": (rar4(), "rar"), "rar5": (rar5(), "rar"), "7z": (seven_zip(), "7z"),
        "mp4": (mp4(), "mp4"), "mov": (mov(), "mov"), "mkv": (mkv(), "mkv"), "webm_live": (mkv(True, True), "webm"),
        "wmv": (asf(), "wmv"), "wav": (wav(), "wav"), "avi": (avi(), "avi"), "mp3": (mp3(), "mp3"),
        "opus": (ogg(True), "opus"), "ogg": (ogg(False), "ogg"), "flac": (flac(), "flac"),
        "exe": (pe(False), "exe"), "dll": (pe(True), "dll"),
    }


JUNK_HEADERS = [
    b"\xFF\xD8\xFF\xE0", b"\x89PNG\r\n\x1a\n", b"GIF89a", b"%PDF-1.4\n", b"PK\x03\x04\x14\x00\x00\x00\x08\x00",
    b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00", b"7z\xBC\xAF\x27\x1C\x00\x04", b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1",
    b"\x00\x00\x00\x18ftypisom", b"\x1A\x45\xDF\xA3", b"ID3\x03\x00\x00\x00\x00\x01\x00", b"OggS\x00\x02",
    b"fLaC\x00\x00\x00\x22", b"8BPS\x00\x01\x00\x00\x00\x00\x00\x00", b"II*\x00\x08\x00\x00\x00", b"MZ\x90\x00",
    b"SQLite format 3\x00", b"RIFF\x10\x00\x00\x00WAVE", uuid.UUID("75B22630-668E-11CF-A6D9-00AA0062CE6C").bytes_le,
]


def build_image(path: str, block: int = 512, seed: int = 99, gap_blocks=(1, 12), with_junk: bool = True):
    """Write samples into an image separated by random noise. Returns
    {offset: (name, data, ext)} for each sample."""
    r = _rng(seed)
    layout = {}
    samples = all_samples()
    with open(path, "wb") as f:
        def pad_to_block():
            rem = f.tell() % block
            if rem:
                f.write(r.randbytes(block - rem))
        f.write(r.randbytes(block * 3))
        for name, (data, ext) in samples.items():
            if with_junk:
                junk = r.choice(JUNK_HEADERS)
                f.write(junk + r.randbytes(block - len(junk)))
            f.write(r.randbytes(block * r.randint(*gap_blocks)))
            layout[f.tell()] = (name, data, ext)
            f.write(data)
            pad_to_block()
        f.write(r.randbytes(block * 8))
    return layout
