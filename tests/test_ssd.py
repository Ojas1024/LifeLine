"""SSD/TRIM handling: erased files are labelled at scan time, and surviving copies are found."""
import os
import struct
import sys
import zlib

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import imagegen  # noqa: E402
import samplegen  # noqa: E402

from lifeline.core.models import Condition, Source  # noqa: E402
from lifeline.core.session import ScanSession  # noqa: E402
from lifeline.core import survivors  # noqa: E402

never = lambda: False  # noqa: E731
quiet = lambda f, m: None  # noqa: E731


@pytest.mark.skipif(not imagegen.have("mkfs.fat", "mcopy", "mdel"), reason="needs dosfstools + mtools")
def test_trimmed_files_are_labelled_from_the_start(tmp_path):
    os.environ["MTOOLS_SKIP_CHECK"] = "1"
    img = str(tmp_path / "ssd.img")
    imagegen.make_fat(img, 32, 65536, "SSD")
    big = samplegen.jpeg() * 1 + os.urandom(40000)          # > 3 samples worth of data
    files = {"Erased photo.jpg": big, "Half erased.pdf": samplegen.pdf() + os.urandom(30000),
             "Still fine.png": samplegen.png() + os.urandom(20000)}
    for n, d in files.items():
        imagegen.mcopy(img, d, n, str(tmp_path))
    for n in files:
        imagegen.run("mdel", "-i", img, "::" + n)
    s = ScanSession(img, "ssd")
    s.quick_scan(quiet, never)
    by = {i.name: i for i in s.items}
    # emulate TRIM: zero all of one file, and only the first 4 KB of another
    with open(img, "r+b") as f:
        for off, n in by["Erased photo.jpg"].extents:
            f.seek(off)
            f.write(b"\0" * n)
        off, _ = by["Half erased.pdf"].extents[0]
        f.seek(off)
        f.write(b"\0" * 4096)
    s.close()

    s = ScanSession(img, "ssd", media="SSD")
    s.quick_scan(quiet, never)
    by = {i.name: i for i in s.items}
    erased, half, fine = by["Erased photo.jpg"], by["Half erased.pdf"], by["Still fine.png"]
    assert erased.condition == Condition.ERASED and erased.chance == "Unlikely"
    assert "SSD" in erased.note and not erased.hidden_by_default   # visible, so people see what was lost
    assert half.condition == Condition.PARTLY_ERASED and half.chance == "Poor"
    assert fine.condition == Condition.GOOD and fine.chance == "Excellent"
    assert s.erased_count == 2
    # The bug: checking a file must not move it between chances.
    s.verify([erased, half, fine], quiet, never)
    assert (erased.chance, half.chance, fine.chance) == ("Unlikely", "Poor", "Excellent")
    s.close()


def test_vssadmin_and_tmutil_parsing():
    text = r"""vssadmin 1.1 - Volume Shadow Copy Service administrative command-line tool
Contents of shadow copy set ID: {1}
   Contained 1 shadow copies at creation time: 11/17/2026 10:00:03 AM
      Shadow Copy ID: {2}
         Original Volume: (C:)\\?\Volume{abc}\
         Shadow Copy Volume: \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy3
Contents of shadow copy set ID: {3}
   Enthaltene Schattenkopien zum Erstellungszeitpunkt: 18.11.2026 09:00:00
         Ursprungsvolume: (D:)\\?\Volume{def}\
         Schattenkopievolume: \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy7
"""
    snaps = survivors.parse_vssadmin(text)
    assert [(s.root, s.live_root, s.order) for s in snaps] == [
        ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy3\\", "C:\\", 3),
        ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7\\", "D:\\", 7)]
    assert "11/17/2026" in snaps[0].label and "18.11.2026" in snaps[1].label
    out = "Snapshots for disk /:\ncom.apple.TimeMachine.2026-11-17-101010.local\ncom.apple.TimeMachine.2026-11-18-080000.local\n"
    assert survivors.parse_tmutil(out) == ["com.apple.TimeMachine.2026-11-17-101010.local",
                                           "com.apple.TimeMachine.2026-11-18-080000.local"]


def test_files_deleted_since_a_snapshot(tmp_path):
    live = tmp_path / "live"
    old = tmp_path / "snap1"
    new = tmp_path / "snap2"
    for root, files in ((live, {"home/ana/keep.txt": b"k"}),
                        (old, {"home/ana/keep.txt": b"k", "home/ana/thesis.docx": b"OLD", "home/ana/gone/a.jpg": b"A",
                               "home/ana/.cache/junk": b"x"}),
                        (new, {"home/ana/keep.txt": b"k", "home/ana/thesis.docx": b"NEWER"})):
        for rel, data in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    snaps = [survivors.Snapshot(str(new), str(live), "Snapshot 2", 2, ("home",)),
             survivors.Snapshot(str(old), str(live), "Snapshot 1", 1, ("home",))]
    items = survivors.deleted_since(snaps)
    got = {(i.folder, i.name): open(i.file_path, "rb").read() for i in items}
    assert got == {("Restore points/Snapshot 2/home/ana", "thesis.docx"): b"NEWER",   # newest copy wins
                   ("Restore points/Snapshot 1/home/ana/gone", "a.jpg"): b"A"}         # whole folder deleted
    assert all(i.source == Source.SNAPSHOT and i.chance == "Excellent" for i in items)


def _png_with_text(key: str, value: str) -> bytes:
    def chunk(t, body):
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body))
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", key.encode() + b"\0" + value.encode()) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def test_freedesktop_thumbnails_of_deleted_pictures(tmp_path):
    thumbs = tmp_path / "thumbnails" / "large"
    thumbs.mkdir(parents=True)
    exists = tmp_path / "still_here.jpg"
    exists.write_bytes(b"x")
    (thumbs / "a.png").write_bytes(_png_with_text("Thumb::URI", "file:///home/ana/Pictures/Beach%20day.jpg"))
    (thumbs / "b.png").write_bytes(_png_with_text("Thumb::URI", exists.as_uri()))
    items = survivors.freedesktop_thumbnails([str(tmp_path / "thumbnails")])
    assert [(i.folder, i.name) for i in items] == [("Thumbnails/home/ana/Pictures", "Beach day (preview).png")]
    assert "Beach day.jpg" in items[0].note


def test_windows_thumbcache_carving(tmp_path):
    jpg, png = samplegen.jpeg(), samplegen.png()
    db = tmp_path / "thumbcache_256.db"
    # entries sit at arbitrary, unaligned offsets after a CMMM header
    db.write_bytes(b"CMMM" + os.urandom(37) + b"CMMM" + b"\x01" * 51 + jpg + b"\x00" * 13 + b"CMMM" + b"\x02" * 29
                   + png + os.urandom(100) + jpg)
    items = survivors.carve_thumbcache(str(db), min_size=1)
    datas = [i.resident_data for i in items]
    assert jpg in datas and png in datas and len(items) == 2      # the repeated JPEG is listed once
    assert all(i.source == Source.THUMBNAIL and i.chance == "Excellent" for i in items)


def test_autosaves(tmp_path):
    word = tmp_path / "Word"
    word.mkdir()
    (word / "AutoRecovery save of Thesis.asd").write_bytes(b"doc")
    items = survivors.find_autosaves([(str(word / "*.asd"), "Word")])
    assert [(i.folder, i.name) for i in items] == [("Autosaves/Word", "AutoRecovery save of Thesis.asd")]
