import os

import pytest

import lifeline.core.ntfs as ntfs
from imagegen import find_record, have, make_ntfs, ntfs_delete, ntfs_set_parent, read_item
from lifeline.core.disk import DiskReader
from lifeline.core.filesystems import detect_filesystem
from lifeline.core.models import Condition
from lifeline.core.ntfs import NTFSVolume, count_pieces

pytestmark = pytest.mark.skipif(not have("mkntfs", "ntfscp"), reason="needs ntfs-3g tools")
nop = (lambda f, m: None, lambda: False)


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    d = tmp_path_factory.mktemp("ntfs")
    files = {f"file{i:03d}.bin": os.urandom(3000 + i * 1500) for i in range(40)}
    files.update({"small.txt": b"tiny resident", "big.bin": os.urandom(300_000),
                  "Projects": os.urandom(5000), "inner.doc": os.urandom(70_000)})
    img = str(d / "n.img")
    make_ntfs(img, files)
    recs = {n: find_record(img, n) for n in files}
    ntfs_set_parent(img, recs["inner.doc"], recs["Projects"])
    deleted = ["small.txt", "big.bin", "inner.doc"] + [f"file{i:03d}.bin" for i in range(0, 40, 2)]
    for n in deleted:
        ntfs_delete(img, recs[n])
    ntfs_delete(img, recs["Projects"], mark_dir=True)  # a deleted folder
    return img, files, deleted


def test_quick_scan(image):
    img, files, deleted = image
    with DiskReader(img) as r:
        fs = detect_filesystem(r)
        assert isinstance(fs, NTFSVolume) and fs.label == "TESTNTFS"
        items = {i.full_path: i for i in fs.scan_deleted(*nop)}
        expected = {("Projects/" + n if n == "inner.doc" else n) for n in deleted}
        assert set(items) == expected
        for path, it in items.items():
            assert it.deleted and it.condition == Condition.GOOD
            assert read_item(r, it) == files[it.name]
        assert items["small.txt"].resident_data == b"tiny resident"


def test_parallel_matches_sequential(image, monkeypatch):
    img, _, _ = image
    monkeypatch.setattr(ntfs, "SLICE_BYTES", 8192)  # many slices from a small MFT
    with DiskReader(img) as r:
        fs = NTFSVolume(r)
        assert len(fs._mft_slices()) > 4
        key = lambda items: [(i.full_path, i.size, i.extents, i.resident_data, i.mft_record, i.condition) for i in items]
        one = fs.scan_deleted(*nop)
        many = fs.scan_deleted(*nop, workers=4)
        assert key(one) == key(many) and one
        assert key(fs.scan_deleted(*nop, include_existing=True)) == key(
            fs.scan_deleted(*nop, include_existing=True, workers=3))


def test_include_existing(image):
    img, files, deleted = image
    with DiskReader(img) as r:
        items = {i.name: i for i in NTFSVolume(r).scan_deleted(*nop, include_existing=True)}
        live = set(files) - set(deleted) - {"Projects"}
        assert live <= set(items)
        for n in live:
            assert not items[n].deleted and read_item(r, items[n]) == files[n]


def test_overwrite_detected_and_free_filter(image, tmp_path):
    img, _, _ = image
    copy = str(tmp_path / "c.img")
    with open(img, "rb") as a, open(copy, "wb") as b:
        b.write(a.read())
    with DiskReader(copy) as r:
        fs = NTFSVolume(r)
        it = {i.name: i for i in fs.scan_deleted(*nop)}["big.bin"]
        first = it.extents[0][0] // fs.cluster_size
        bm_rec = fs._read_record(6)
        runs, size = fs._merge_fragments(bm_rec.fragments)
        bm_off = fs._runs_to_extents(runs, size)[0][0]
        f = fs.free_filter()
        assert f(it.extents[0][0]) and fs.is_free(it.extents[0][0])
    with open(copy, "r+b") as fh:  # hand the first 8 clusters of big.bin to "another file"
        fh.seek(bm_off + first // 8)
        b0 = fh.read(2)
        v = int.from_bytes(b0, "little") | (0xFF << (first % 8))
        fh.seek(bm_off + first // 8)
        fh.write(v.to_bytes(2, "little"))
    with DiskReader(copy) as r:
        fs = NTFSVolume(r)
        it = {i.name: i for i in fs.scan_deleted(*nop)}["big.bin"]
        assert it.condition == Condition.PARTIAL and 0 < it.overwritten_pct < 1
        assert not fs.is_free(it.extents[0][0])


def test_backup_boot_sector(image, tmp_path):
    img, _, _ = image
    copy = str(tmp_path / "b.img")
    with open(img, "rb") as a, open(copy, "wb") as b:
        b.write(a.read())
    with open(copy, "r+b") as fh:
        fh.write(b"\x00" * 512)  # wipe the primary boot sector
    with DiskReader(copy) as r:
        fs = detect_filesystem(r)
        assert isinstance(fs, NTFSVolume) and fs.used_backup_boot
        assert any(i.name == "big.bin" for i in fs.scan_deleted(*nop))


def test_count_pieces():
    assert count_pieces([(0, 4096), (4096, 100)]) == 1
    assert count_pieces([(0, 4096), (None, 4096), (16384, 10)]) == 2
    assert count_pieces([]) == 0


def test_cancel(image):
    img, _, _ = image
    with DiskReader(img) as r:
        assert NTFSVolume(r).scan_deleted(lambda f, m: None, lambda: True) == []
