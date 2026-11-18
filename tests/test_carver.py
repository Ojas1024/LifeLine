import os
import random
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lifeline.core.carver import SIGNATURES, Carver, count_by_ext  # noqa: E402
from lifeline.core.disk import DiskReader  # noqa: E402
from lifeline.core.models import FreeSpaceFilter  # noqa: E402
from lifeline.core.parallel import cpu_workers  # noqa: E402


import samplegen  # noqa: E402

KEYS = [s.key for s in SIGNATURES]
never = lambda: False  # noqa: E731
quiet = lambda *a: None  # noqa: E731


def carve(path, block, workers, regions=None, accept=None, task_size=64 * 1024 * 1024):
    with DiskReader(path) as r:
        c = Carver(r, block=block)
        items = c.scan(regions or [(0, r.length)], KEYS, quiet, never, accept=accept, workers=workers,
                       task_size=task_size)
        data = {it.extents[0][0]: r.read_at(it.extents[0][0], it.size) for it in items}
    return items, data


@pytest.fixture(scope="module", params=[512, 4096])
def image(request, tmp_path_factory):
    block = request.param
    path = str(tmp_path_factory.mktemp("carve") / f"img{block}.bin")
    layout = samplegen.build_image(path, block=block)
    return path, block, layout


def _check(items, data, layout):
    found = {it.extents[0][0]: it for it in items}
    missing = [(off, name) for off, (name, _, _) in layout.items() if off not in found]
    assert not missing, f"missed: {missing}"
    extra = [(hex(it.extents[0][0]), it.name) for it in items if it.extents[0][0] not in layout]
    assert not extra, f"false positives: {extra}"
    for off, (name, blob, ext) in layout.items():
        it = found[off]
        assert it.ext == ext, (name, it.ext, ext)
        assert data[off] == blob, f"{name}: carved {len(data[off])} bytes, expected {len(blob)}"


def test_sequential_finds_every_sample(image):
    path, block, layout = image
    items, data = carve(path, block, workers=1)
    _check(items, data, layout)


def test_parallel_matches_sequential(image):
    path, block, layout = image
    seq, _ = carve(path, block, workers=1)
    # tiny tasks so plenty of samples straddle task boundaries
    par, data = carve(path, block, workers=4, task_size=block * 16)
    assert [(i.name, i.size) for i in par] == [(i.name, i.size) for i in seq]
    _check(par, data, layout)


def test_regions_and_free_filter(image):
    path, block, layout = image
    offs = sorted(layout)
    keep = offs[::2]
    # bitmap: block-sized clusters; mark everything used except the kept samples' first cluster
    size = os.path.getsize(path)
    clusters = size // block + 1
    bm = bytearray(b"\xff" * (clusters // 8 + 1))
    for off in keep:
        c = off // block
        bm[c // 8] &= ~(1 << (c % 8))
    acc = FreeSpaceFilter(bytes(bm), block)
    for workers in (1, 3):
        items, _ = carve(path, block, workers, accept=acc, task_size=block * 32)
        assert sorted(i.extents[0][0] for i in items) == keep


def test_split_regions_equal_whole(image):
    path, block, layout = image
    size = os.path.getsize(path)
    mid = (size // 2) - (size // 2) % block
    whole, _ = carve(path, block, 1)
    split, _ = carve(path, block, 1, regions=[(0, mid), (mid, size)])
    assert [(i.name, i.size) for i in whole] == [(i.name, i.size) for i in split]


def test_cancel_returns_quickly(image):
    path, block, _ = image
    with DiskReader(path) as r:
        items = Carver(r, block).scan([(0, r.length)], KEYS, quiet, lambda: True, workers=2)
    assert items == []


def test_count_by_ext(image):
    path, block, layout = image
    items, _ = carve(path, block, 1)
    counts = count_by_ext(items)
    assert counts["rar"] == 2 and counts["zip"] == 2


def test_speed(tmp_path):
    """Throughput on 256 MB of mostly random data with a few real files (printed, not asserted)."""
    path = str(tmp_path / "speed.bin")
    r = random.Random(5)
    samples = samplegen.all_samples()
    with open(path, "wb") as f:
        for i in range(64):
            f.write(r.randbytes(4 * 1024 * 1024 - 65536))
            data = list(samples.values())[i % len(samples)][0]
            f.write(data[:65536].ljust(65536, b"\0"))
    size = os.path.getsize(path)
    times = {}
    for workers in (1, cpu_workers()):
        t = time.perf_counter()
        carve(path, 512, workers)
        times[workers] = time.perf_counter() - t
    print("\n" + ", ".join(f"workers={w}: {size / t / 1e6:,.0f} MB/s" for w, t in times.items()))
