import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lifeline.core.verify import verify_bytes, verify_file  # noqa: E402

import samplegen  # noqa: E402

SAMPLES = samplegen.all_samples()
EXT = {"qt_tiff": "tif"}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_pristine_samples_are_ok(name):
    data, ext = SAMPLES[name]
    status, detail = verify_bytes(data, ext)
    assert status == "ok", (name, detail)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_truncated_is_damaged(name):
    data, ext = SAMPLES[name]
    status, detail = verify_bytes(data[:len(data) * 2 // 3], ext)
    assert status == "damaged", (name, detail)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_zeroed_is_damaged(name):
    data, ext = SAMPLES[name]
    status, detail = verify_bytes(bytes(len(data)), ext)
    assert status == "damaged" and "zeros" in detail


# formats whose checksums cover the body, so a single flipped byte is caught
@pytest.mark.parametrize("name,where", [("png", 0.5), ("docx", 0.4), ("zip", 0.5), ("opus", 0.5),
                                        ("ogg", 0.6), ("rar4", 30), ("rar4", 0.5), ("rar5", 20), ("7z", -3)])
def test_bit_flip_caught_by_checksum(name, where):
    data, ext = SAMPLES[name]
    b = bytearray(data)
    i = int(len(b) * where) if isinstance(where, float) else where
    b[i] ^= 0x10
    status, detail = verify_bytes(bytes(b), ext)
    assert status == "damaged", (name, detail)


def test_mostly_blank_compressed_file_is_damaged():
    data, _ = SAMPLES["mp4"]
    big = data[:1000] + bytes(64 * 1024) + data[1000:]
    assert verify_bytes(big, "jpg")[0] == "damaged"


def test_unknown_type():
    assert verify_bytes(b"hello world", "xyz")[0] == "unknown"


def test_never_raises_on_garbage():
    for ext in ["jpg", "png", "zip", "pdf", "sqlite", "7z", "rar", "ogg", "mp4", "tif", "doc", "flac", "exe"]:
        for blob in (b"\x01", os.urandom(5000), b"PK\x03\x04" + os.urandom(100)):
            status, _ = verify_bytes(blob, ext)
            assert status in ("ok", "damaged", "unknown")


def test_verify_file(tmp_path):
    data, ext = SAMPLES["sqlite"]
    p = tmp_path / "x.sqlite"
    p.write_bytes(data)
    assert verify_file(str(p))[0] == "ok"
    p.write_bytes(data[:4096])
    assert verify_file(str(p))[0] == "damaged"
    assert verify_file(str(tmp_path / "missing.jpg"))[0] == "unknown"
