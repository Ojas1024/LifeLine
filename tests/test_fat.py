import os

import pytest

from imagegen import have, make_fat, mcopy, read_item, run
from lifeline.core.disk import DiskReader
from lifeline.core.fat import FATVolume
from lifeline.core.filesystems import detect_filesystem
from lifeline.core.models import Condition

pytestmark = pytest.mark.skipif(not have("mkfs.fat", "mcopy", "mdel"), reason="needs dosfstools + mtools")
nop = (lambda f, m: None, lambda: False)


def scan(img, **kw):
    r = DiskReader(img)
    fs = detect_filesystem(r)
    return r, fs, {i.full_path: i for i in fs.scan_deleted(*nop, **kw)}


@pytest.mark.parametrize("bits,size_kb", [(12, 1440), (16, 16384), (32, 65536)])
def test_deleted_files_and_folders(tmp_path, bits, size_kb):
    img = str(tmp_path / "f.img")
    make_fat(img, bits, size_kb)
    photo, doc, small = os.urandom(120_000 if bits != 12 else 40_000), os.urandom(33_333), b"hello\n"
    run("mmd", "-i", img, "::/Photos", "::/Photos/Trip 2024")
    mcopy(img, photo, "/Photos/Trip 2024/Beach sunset (1).jpg", str(tmp_path))
    mcopy(img, doc, "/Report final.docx", str(tmp_path))
    mcopy(img, small, "/notes.txt", str(tmp_path))
    mcopy(img, b"stay", "/Photos/keep.txt", str(tmp_path))
    run("mdel", "-i", img, "::/notes.txt", "::/Report final.docx")
    run("mdeltree", "-i", img, "::/Photos/Trip 2024")

    r, fs, items = scan(img)
    assert fs.name == f"FAT{bits}" and fs.label == "TESTFAT"
    # "notes.txt" fits 8.3, so it has no long name and its lost first letter can't be known.
    assert set(items) == {"Photos/Trip 2024/Beach sunset (1).jpg", "Report final.docx", "_otes.txt"}
    for path, data in (("Photos/Trip 2024/Beach sunset (1).jpg", photo), ("Report final.docx", doc), ("_otes.txt", small)):
        it = items[path]
        assert it.deleted and it.condition == Condition.GOOD
        assert read_item(r, it) == data
        assert it.modified is not None

    _, _, everything = scan(img, include_existing=True)
    assert not everything["Photos/keep.txt"].deleted
    assert read_item(r, everything["Photos/keep.txt"]) == b"stay"


def test_overwritten_detected(tmp_path):
    img = str(tmp_path / "f.img")
    make_fat(img, 32, 65536)
    mcopy(img, os.urandom(200_000), "/Old data.bin", str(tmp_path))
    run("mmd", "-i", img, "::/sub")
    run("mdel", "-i", img, "::/Old data.bin")
    with DiskReader(img) as r:
        free = FATVolume(r)._used.count(0) * FATVolume(r).cluster_size
    mcopy(img, os.urandom(free), "/sub/new.bin", str(tmp_path))  # fills the disk, so reuses the freed space
    _, fs, items = scan(img)
    old = items["Old data.bin"]
    assert old.condition == Condition.OVERWRITTEN and old.overwritten_pct == 1.0
    off = old.extents[0][0]
    assert not fs.is_free(off)
    assert not fs.free_regions()
    assert not fs.free_filter()(0)  # boot area is never "free space"


def test_free_space(tmp_path):
    img = str(tmp_path / "f.img")
    make_fat(img, 32, 65536)
    mcopy(img, os.urandom(100_000), "/a.bin", str(tmp_path))
    _, fs, _ = scan(img)
    assert fs.is_free(fs.data_start + (fs.cluster_count - 5) * fs.cluster_size)
    regions = fs.free_regions()
    assert regions and all(fs.data_start <= a < b <= fs.volume_size for a, b in regions)
    assert sum(b - a for a, b in regions) >= (fs.cluster_count - 300) * fs.cluster_size


def test_short_name_first_letter_recovered_from_long_name(tmp_path):
    img = str(tmp_path / "f.img")
    make_fat(img, 16, 16384)
    mcopy(img, b"x" * 5000, "/Quarterly numbers.xlsx", str(tmp_path))
    run("mdel", "-i", img, "::/Quarterly numbers.xlsx")
    _, _, items = scan(img)
    assert "Quarterly numbers.xlsx" in items


def test_not_fat(tmp_path):
    p = tmp_path / "z.img"
    p.write_bytes(b"\x00" * 1024 * 1024)
    with DiskReader(str(p)) as r:
        with pytest.raises(Exception):
            FATVolume(r)
        assert detect_filesystem(r) is None
