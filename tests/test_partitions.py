import os
import struct
import zlib

import pytest

from imagegen import ExFATBuilder, have, make_fat, make_ntfs, mcopy
from lifeline.core.disk import DiskReader
from lifeline.core.partitions import read_partition_table, search_lost_partitions

MiB = 1024 * 1024
nop = (lambda f, m: None, lambda: False)
needs_tools = pytest.mark.skipif(not have("mkfs.fat", "mkntfs", "ntfscp", "mcopy"), reason="needs fs tools")


def _volumes(tmp_path):
    fat = str(tmp_path / "fat.img")
    make_fat(fat, 32, 40 * 1024, label="CARD")
    mcopy(fat, b"x" * 1000, "/a.txt", str(tmp_path))
    ntfs = str(tmp_path / "ntfs.img")
    make_ntfs(ntfs, {"b.txt": b"y" * 2000}, size_mb=24)
    ex = str(tmp_path / "ex.img")
    b = ExFATBuilder(size=16 * MiB)
    b.add_file("c.txt", b"z" * 100)
    b.build(ex)
    return [open(p, "rb").read() for p in (fat, ntfs, ex)]


def _disk(tmp_path, vols, gap=MiB):
    layout, pos = [], MiB
    for v in vols:
        layout.append((pos, len(v)))
        pos += len(v) + gap
    disk = bytearray(pos + MiB)
    for (off, _), v in zip(layout, vols):
        disk[off:off + len(v)] = v
    return disk, layout


def _mbr(entries):
    m = bytearray(512)
    for i, (ptype, lba, sectors) in enumerate(entries):
        e = 0x1BE + i * 16
        m[e + 4] = ptype
        struct.pack_into("<II", m, e + 8, lba, sectors)
    m[510:512] = b"\x55\xaa"
    return m


@needs_tools
def test_mbr_and_logical_partitions(tmp_path):
    vols = _volumes(tmp_path)
    disk, layout = _disk(tmp_path, vols)
    (o1, l1), (o2, l2), (o3, l3) = layout
    # partition 1 primary; 2 and 3 logical inside an extended partition
    ext_start = o2 - MiB // 2
    ext_lba = ext_start // 512
    disk[0:512] = _mbr([(0x0C, o1 // 512, l1 // 512), (0x0F, ext_lba, (len(disk) - ext_start) // 512)])
    ebr1 = _mbr([(0x07, (o2 - ext_start) // 512, l2 // 512), (0x05, (o3 - MiB // 2 - ext_start) // 512, 0)])
    struct.pack_into("<I", ebr1, 0x1CE + 12, (l3 + MiB // 2) // 512)
    disk[ext_start:ext_start + 512] = ebr1
    ebr2_at = o3 - MiB // 2
    disk[ebr2_at:ebr2_at + 512] = _mbr([(0x07, MiB // 2 // 512, l3 // 512)])
    p = tmp_path / "disk.img"
    p.write_bytes(disk)
    with DiskReader(str(p)) as r:
        parts = read_partition_table(r)
    assert [(x.offset, x.fs, x.origin) for x in parts] == [(o1, "FAT32", "MBR"), (o2, "NTFS", "MBR"), (o3, "exFAT", "MBR")]
    assert parts[0].label == "CARD" and parts[1].label == "TESTNTFS"


@needs_tools
def test_gpt(tmp_path):
    vols = _volumes(tmp_path)[:2]
    disk, layout = _disk(tmp_path, vols)
    disk[0:512] = _mbr([(0xEE, 1, len(disk) // 512 - 1)])
    entries = bytearray(128 * 128)
    for i, (off, length) in enumerate(layout):
        e = i * 128
        entries[e:e + 16] = os.urandom(16)
        entries[e + 16:e + 32] = os.urandom(16)
        struct.pack_into("<QQ", entries, e + 0x20, off // 512, (off + length) // 512 - 1)
        entries[e + 0x38:e + 0x38 + 12] = f"Part{i}".encode("utf-16-le")
    hdr = bytearray(512)
    hdr[:8] = b"EFI PART"
    struct.pack_into("<QII", hdr, 0x48, 2, 128, 128)
    struct.pack_into("<I", hdr, 0x58, zlib.crc32(entries))
    disk[512:1024] = hdr
    disk[1024:1024 + len(entries)] = entries
    p = tmp_path / "gpt.img"
    p.write_bytes(disk)
    with DiskReader(str(p)) as r:
        parts = read_partition_table(r)
    assert [(x.offset, x.length, x.fs, x.origin, x.description.endswith("(Part%d)" % i)) for i, x in enumerate(parts)] == \
        [(layout[0][0], layout[0][1], "FAT32", "GPT", True), (layout[1][0], layout[1][1], "NTFS", "GPT", True)]


@needs_tools
def test_search_finds_partitions_without_a_table(tmp_path):
    vols = _volumes(tmp_path)
    disk, layout = _disk(tmp_path, vols, gap=3 * MiB)
    # wipe the NTFS primary boot sector too: it must be found from its backup at the end
    o2 = layout[1][0]
    disk[o2:o2 + 512] = b"\x00" * 512
    p = tmp_path / "lost.img"
    p.write_bytes(disk)
    with DiskReader(str(p)) as r:
        assert read_partition_table(r) == []
        found = search_lost_partitions(r, *nop)
    assert [(x.offset, x.fs, x.origin) for x in found] == [
        (layout[0][0], "FAT32", "Found"), (layout[1][0], "NTFS", "Found"), (layout[2][0], "exFAT", "Found")]
    assert "backup boot sector" in found[1].description
    assert found[0].label == "CARD"


def test_search_cancel_and_blank(tmp_path):
    p = tmp_path / "blank.img"
    p.write_bytes(b"\x00" * 8 * MiB)
    with DiskReader(str(p)) as r:
        assert search_lost_partitions(r, *nop) == []
        assert search_lost_partitions(r, lambda f, m: None, lambda: True) == []
