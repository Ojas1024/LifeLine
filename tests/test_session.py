"""End-to-end: session (smart scan, dedupe, save/load, verify), recovery, query, trash, CLI."""
import hashlib
import os
import struct
import subprocess
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import imagegen  # noqa: E402
import samplegen  # noqa: E402

from lifeline.core import devices  # noqa: E402
from lifeline.core.models import Condition, RecoverableItem, Source  # noqa: E402
from lifeline.core.query import compile_query, parse_size  # noqa: E402
from lifeline.core.recovery import recover, safe_name  # noqa: E402
from lifeline.core.session import ScanSession  # noqa: E402
from lifeline.core.trash import parse_trashinfo, parse_windows_i_file, _scan_xdg  # noqa: E402

pytestmark = pytest.mark.skipif(not imagegen.have("mkfs.fat", "mcopy", "mdel"), reason="needs dosfstools + mtools")

never = lambda: False  # noqa: E731
quiet = lambda f, m: None  # noqa: E731


@pytest.fixture(scope="module")
def fat_image(tmp_path_factory):
    d = tmp_path_factory.mktemp("fat")
    img = str(d / "card.img")
    os.environ["MTOOLS_SKIP_CHECK"] = "1"
    imagegen.make_fat(img, 32, 65536, "CARD")
    imagegen.run("mmd", "-i", img, "::Photos")
    files = {"Photos/Beach day.jpg": samplegen.jpeg(), "Photos/Sunset photo.png": samplegen.png(),
             "Report final.pdf": samplegen.pdf(), "Budget sheet.xlsx": samplegen.xlsx()}
    for path, data in files.items():
        imagegen.mcopy(img, data, path, str(d))
    imagegen.mcopy(img, samplegen.docx(), "Keep this.docx", str(d))
    imagegen.run("mdeltree", "-i", img, "::Photos")
    imagegen.run("mdel", "-i", img, "::Report final.pdf")
    imagegen.run("mdel", "-i", img, "::Budget sheet.xlsx")
    return img, files


def _md5(b):
    return hashlib.md5(b).hexdigest()


def test_smart_scan_dedupes_and_recovers(fat_image, tmp_path):
    img, files = fat_image
    s = ScanSession(img, "card")
    assert s.fs_name == "FAT32"
    s.smart_scan(None, quiet, never, workers=2)
    names = {i.full_path for i in s.items}
    for path in files:
        assert path in names, names
    # Deep scan only searches free space, and anything the quick scan named isn't listed twice.
    starts = [i.first_offset for i in s.items if i.first_offset is not None]
    assert len(starts) == len(set(starts))
    assert not any(i.name == "Keep this.docx" for i in s.items)
    out = tmp_path / "out"
    res = recover(s.reader, img, [i for i in s.items if i.source == Source.QUICK], str(out), True, quiet, never,
                  workers=3, verify=True)
    assert res["recovered"] == len(files) and not res["failed"]
    for path, data in files.items():
        with open(out / path, "rb") as f:
            assert _md5(f.read()) == _md5(data)
    assert os.path.exists(res["report"])
    assert all(i.verified in ("ok", "unknown") for i in s.items if i.source == Source.QUICK)
    s.close()


def test_save_and_load_roundtrip(fat_image, tmp_path):
    img, files = fat_image
    s = ScanSession(img, "card")
    s.quick_scan(quiet, never)
    path = str(tmp_path / "scan.lifeline")
    s.save(path)
    t = ScanSession.load(path)
    assert [(i.full_path, i.size, i.extents, i.modified) for i in t.items] == \
           [(i.full_path, i.size, i.extents, i.modified) for i in s.items]
    from lifeline.core.recovery import read_head
    it = next(i for i in t.items if i.name == "Beach day.jpg")
    assert read_head(t.reader, it, it.size) == files["Photos/Beach day.jpg"]
    s.close()
    t.close()


def test_verify_marks_damage(fat_image):
    img, _ = fat_image
    s = ScanSession(img, "card")
    s.quick_scan(quiet, never)
    good = [i for i in s.items if i.ext in ("jpg", "png", "pdf", "xlsx")]
    counts = s.verify(good, quiet, never)
    assert counts["ok"] == len(good)
    bad = RecoverableItem(id=99, name="zero.jpg", folder="", size=8192, source=Source.QUICK,
                          extents=[(None, 8192)], condition=Condition.GOOD)
    s.verify([bad], quiet, never)
    assert bad.verified == "damaged" and bad.chance == "Poor"
    s.close()


def test_whole_disk_image_opens_partition(tmp_path, fat_image):
    img, files = fat_image
    disk = str(tmp_path / "disk.img")
    vol = open(img, "rb").read()
    mbr = bytearray(512)
    struct.pack_into("<BBBBBBBBII", mbr, 0x1BE, 0, 0, 0, 0, 0x0C, 0, 0, 0, 2048, len(vol) // 512)
    mbr[510:512] = b"\x55\xaa"
    with open(disk, "wb") as f:
        f.write(mbr + b"\x00" * (2048 * 512 - 512) + vol)
    s = ScanSession(disk, "disk")
    assert s.partition is not None and s.partition.offset == 2048 * 512 and s.fs_name == "FAT32"
    s.quick_scan(quiet, never)
    assert "Photos/Beach day.jpg" in {i.full_path for i in s.items}
    s.use_whole_disk()
    assert s.items == [] and s.partition is None
    s.deep_scan(["jpg", "png"], False, quiet, never, workers=1)
    assert {i.ext for i in s.items} >= {"jpg", "png"}
    s.close()


def test_query_language():
    def item(name, folder="", size=100, modified=None, ext_src=Source.QUICK):
        return RecoverableItem(id=0, name=name, folder=folder, size=size, source=ext_src, modified=modified,
                               condition=Condition.GOOD)
    a = item("IMG_2041.jpg", "DCIM/Holiday", 3 * 2**20, datetime(2024, 5, 1))
    b = item("Thesis final draft.docx", "Documents/Uni", 200 * 1024, datetime(2023, 2, 1))
    c = item("backup.zip", "Documents", 50 * 2**20, datetime(2024, 8, 1), Source.DEEP)
    allx = [a, b, c]

    def q(text):
        p = compile_query(text)
        return [i.name for i in allx if p(i)]
    assert q("") == [a.name, b.name, c.name]
    assert q("thesis") == [b.name]
    assert q("*.jpg") == [a.name]
    assert q("ext:jpg,zip") == [a.name, c.name]
    assert q("type:photos") == [a.name]
    assert q("size:>1mb") == [a.name, c.name]
    assert q("size:100kb..10mb") == [a.name, b.name]
    assert q("modified:2024") == [a.name, c.name]
    assert q("modified:>2024-06-01") == [c.name]
    assert q("modified:2023-01..2023-12") == [b.name]
    assert q("in:documents -backup") == [b.name]
    assert q('"final draft"') == [b.name]
    assert q("re:^DCIM/.*\\d{4}") == [a.name]
    assert q("found:deep") == [c.name]
    assert q("size:>>bad modified:notadate re:[") == []  # malformed filters never raise
    assert parse_size("1.5mb") == int(1.5 * 2**20) and parse_size("abc") is None


def test_safe_names():
    assert safe_name('a<b>:c"d|e?f*g.txt') == "a_b__c_d_e_f_g.txt"
    assert safe_name("CON.txt") == "_CON.txt"
    assert safe_name("trailing. ") == "trailing"
    long = "é" * 300 + ".jpg"
    assert len(safe_name(long).encode()) <= 240 and safe_name(long).endswith(".jpg")


def test_trash_parsers(tmp_path):
    ft = int((datetime(2024, 3, 2, 10, 0) - datetime(1601, 1, 1)).total_seconds() * 10**7)
    path = "C:\\Users\\Ana\\Documents\\Thesis.docx"
    v2 = struct.pack("<QQQI", 2, 1234, ft, len(path) + 1) + (path + "\x00").encode("utf-16-le")
    size, when, orig = parse_windows_i_file(v2)
    assert (size, when.year, orig) == (1234, 2024, path)
    v1 = struct.pack("<QQQ", 1, 99, ft) + path.encode("utf-16-le").ljust(520, b"\x00")
    assert parse_windows_i_file(v1)[2] == path
    p, when = parse_trashinfo("[Trash Info]\nPath=/home/ana/My%20Docs/a.txt\nDeletionDate=2024-01-02T03:04:05\n")
    assert p == "/home/ana/My Docs/a.txt" and when.month == 1
    # A freedesktop trash folder with one file and one folder.
    t = tmp_path / "Trash"
    (t / "info").mkdir(parents=True)
    (t / "files" / "Album").mkdir(parents=True)
    (t / "files" / "a.txt").write_bytes(b"hello")
    (t / "files" / "Album" / "p.jpg").write_bytes(b"jpegdata")
    (t / "info" / "a.txt.trashinfo").write_text("[Trash Info]\nPath=/home/ana/a.txt\nDeletionDate=2024-01-02T03:04:05\n")
    (t / "info" / "Album.trashinfo").write_text("[Trash Info]\nPath=/home/ana/Pictures/Album\n")
    out = []
    _scan_xdg(out, never, bins=[str(t)])
    got = {i.full_path: i for i in out}
    assert set(got) == {"Recycle Bin/home/ana/a.txt", "Recycle Bin/home/ana/Pictures/Album/p.jpg"}
    assert all(i.chance == "Excellent" and i.file_path for i in out)
    res = recover(None, None, out, str(tmp_path / "rec"), True, quiet, never)
    assert res["recovered"] == 2


def test_same_device_rules(tmp_path):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 4096)
    assert not devices.same_device(str(img), str(tmp_path))  # images are never at risk
    assert not devices.same_device(None, str(tmp_path))


def test_cli_scan_and_recover(fat_image, tmp_path):
    img, files = fat_image
    out = tmp_path / "cli"
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    r = subprocess.run([sys.executable, "-m", "lifeline", "scan", img, "--mode", "quick", "--query", "type:photos",
                        "--out", str(out), "--save", str(tmp_path / "s.lifeline")],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert (out / "Photos" / "Beach day.jpg").read_bytes() == files["Photos/Beach day.jpg"]
    assert not (out / "Report final.pdf").exists()
    r = subprocess.run([sys.executable, "-m", "lifeline", "load", str(tmp_path / "s.lifeline"), "--list-only"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "Report final.pdf" in r.stdout
