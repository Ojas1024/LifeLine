import os

from imagegen import ExFATBuilder, read_item
from lifeline.core.disk import DiskReader
from lifeline.core.exfat import ExFATVolume, exfat_time
from lifeline.core.filesystems import detect_filesystem
from lifeline.core.models import Condition

nop = (lambda f, m: None, lambda: False)


def build(tmp_path):
    b = ExFATBuilder()
    data = {
        "DCIM/100CANON/IMG_0001.JPG": os.urandom(50_000),
        "DCIM/100CANON/A long video name that needs three name entries.mov": os.urandom(30_000),
        "keep.txt": b"live file",
        "gone.bin": os.urandom(9_000),
        "frag.bin": os.urandom(20_000),
    }
    b.add_file("DCIM/100CANON/IMG_0001.JPG", data["DCIM/100CANON/IMG_0001.JPG"])
    b.add_file("DCIM/100CANON/A long video name that needs three name entries.mov",
               data["DCIM/100CANON/A long video name that needs three name entries.mov"])
    b.add_file("keep.txt", data["keep.txt"])
    b.add_file("gone.bin", data["gone.bin"], contiguous=False)
    b.add_file("frag.bin", data["frag.bin"], gap_after_first=4)   # chained, fragmented, FAT left intact
    b.delete("DCIM")
    b.delete("gone.bin", zero_chain=True)
    b.delete("frag.bin")
    img = str(tmp_path / "ex.img")
    b.build(img)
    return b, img, data


def test_deleted_entry_sets(tmp_path):
    _, img, data = build(tmp_path)
    r = DiskReader(img)
    fs = detect_filesystem(r)
    assert isinstance(fs, ExFATVolume) and fs.label == "TESTEX" and fs.cluster_size == 4096
    items = {i.full_path: i for i in fs.scan_deleted(*nop)}
    assert set(items) == set(data) - {"keep.txt"}
    for path, it in items.items():
        assert it.deleted and it.condition == Condition.GOOD
        assert read_item(r, it) == data[path], path
    assert items["frag.bin"].note == "Fragmented: 2 pieces"
    assert items["DCIM/100CANON/IMG_0001.JPG"].modified.year == 2024

    everything = {i.full_path: i for i in fs.scan_deleted(*nop, include_existing=True)}
    assert not everything["keep.txt"].deleted and read_item(r, everything["keep.txt"]) == b"live file"


def test_reused_clusters(tmp_path):
    b, img, _ = build(tmp_path)
    b.overwrite(None, "gone.bin", img)
    with DiskReader(img) as r:
        fs = ExFATVolume(r)
        it = {i.full_path: i for i in fs.scan_deleted(*nop)}["gone.bin"]
        assert it.condition == Condition.PARTIAL and 0 < it.overwritten_pct < 1
        assert not fs.is_free(it.extents[0][0])
        assert fs.is_free(it.extents[0][0] + fs.cluster_size)
        assert not fs.is_free(0)
        assert fs.free_regions()


def test_timestamp_utc_offset():
    stamp = ((2024 - 1980) << 25) | (5 << 21) | (17 << 16) | (10 << 11) | (30 << 5) | 10
    assert exfat_time(stamp, 150, 0x80 | 8).strftime("%H:%M:%S") == "08:30:21"  # UTC+2 local -> UTC
