"""Worker pools for the CPU-heavy parts of scanning (MFT parsing, signature carving).

Python threads can't parse in parallel (the GIL), so scans use worker processes.
Each worker opens its own read-only handle on the source, so nothing large is
copied between processes: tasks are (offset, length) ranges, results are small.

If processes can't start (some sandboxes, odd frozen builds) we fall back to
threads, which still overlap disk reads with parsing.
"""
from __future__ import annotations

import multiprocessing as mp
import os
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Callable, Optional, Tuple

from .disk import DiskReader

_PROCESSES_OK: Optional[bool] = None


def cpu_workers() -> int:
    try:
        n = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        n = os.cpu_count() or 1
    return max(1, min(n, 16))


def default_workers(media: str = "Unknown", is_image: bool = False) -> int:
    """Spinning disks hate parallel seeks; SSDs and image files on SSDs love them."""
    if media == "HDD" and not is_image:
        return min(2, cpu_workers())
    return cpu_workers()


def _ping() -> int:
    return os.getpid()


def processes_available() -> bool:
    global _PROCESSES_OK
    if _PROCESSES_OK is None:
        if os.environ.get("LIFELINE_NO_PROCESSES"):
            _PROCESSES_OK = False
        else:
            try:
                with ProcessPoolExecutor(1, mp_context=_context()) as ex:
                    ex.submit(_ping).result(timeout=30)
                _PROCESSES_OK = True
            except Exception:
                _PROCESSES_OK = False
    return _PROCESSES_OK


def _context():
    # "spawn" everywhere: fork() inside a Qt app with live threads is unsafe,
    # and spawn behaves the same on Windows, macOS and Linux.
    return mp.get_context("spawn")


def make_pool(workers: int, initializer: Optional[Callable] = None, initargs: Tuple = ()) -> Executor:
    if workers > 1 and processes_available():
        return ProcessPoolExecutor(workers, mp_context=_context(), initializer=initializer, initargs=initargs)
    if initializer:
        initializer(*initargs)
    return ThreadPoolExecutor(max(1, workers))


def reader_spec(reader: DiskReader) -> Tuple[str, int, int]:
    """What a worker needs to reopen the same source: (path, base_offset, length)."""
    return reader.path, reader.base_offset, reader.length


_worker_readers = {}


def worker_reader(spec: Tuple[str, int, int]) -> DiskReader:
    """One cached reader per source per worker process (or thread pool)."""
    r = _worker_readers.get(spec)
    if r is None:
        r = DiskReader(spec[0], base_offset=spec[1], length=spec[2])
        _worker_readers[spec] = r
    return r
