"""Copy recoverable items out to a destination folder, never onto the source drive.

Several files are written at once (disk reads and writes release the GIL, so
threads overlap them well). Each file can be checked after it's written, and a
CSV report lists what came back and in what state.
"""
from __future__ import annotations

import csv
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterator, List, Optional

from .devices import same_device
from .disk import DiskReader
from .models import RecoverableItem

CHUNK = 4 * 1024 * 1024
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class RecoveryError(Exception):
    pass


def safe_name(name: str) -> str:
    """A file name that's valid on Windows, macOS and Linux alike."""
    name = _BAD_CHARS.sub("_", name).rstrip(" .") or "unnamed"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    if len(name.encode("utf-8")) > 240:   # most file systems cap names at 255 bytes
        stem, ext = os.path.splitext(name)
        while len((stem + ext).encode("utf-8")) > 240 and stem:
            stem = stem[:-1]
        name = stem + ext
    return name


def same_drive(source_path: str, dest_dir: str) -> bool:
    return same_device(source_path, dest_dir)


def iter_item_data(reader: Optional[DiskReader], item: RecoverableItem, limit: Optional[int] = None,
                   chunk: int = CHUNK) -> Iterator[bytes]:
    """Yield the item's bytes in chunks (up to `limit` bytes if given)."""
    remaining = item.size if limit is None else min(item.size, limit)
    if item.file_path is not None:
        with open(item.file_path, "rb") as f:
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    return
                remaining -= len(data)
                yield data
        return
    if item.resident_data is not None:
        yield item.resident_data[:remaining]
        return
    for off, length in item.extents:
        pos = 0
        while pos < length and remaining > 0:
            n = min(chunk, length - pos, remaining)
            data = b"\x00" * n if off is None else reader.read_at(off + pos, n)
            if len(data) < n:  # unreadable area or end of media: keep the file layout intact
                data += b"\x00" * (n - len(data))
            yield data
            pos += n
            remaining -= n
        if remaining <= 0:
            return


def read_head(reader: Optional[DiskReader], item: RecoverableItem, n: int) -> bytes:
    return b"".join(iter_item_data(reader, item, limit=n))


class _Namer:
    """Unique target paths, safe across threads (two deleted 'IMG_0001.jpg' in one folder are common)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._taken = set()

    def claim(self, path: str) -> str:
        with self._lock:
            stem, ext = os.path.splitext(path)
            cand, i = path, 1
            while cand.lower() in self._taken or os.path.exists(cand):
                cand = f"{stem} ({i}){ext}"
                i += 1
            self._taken.add(cand.lower())
            return cand


def recover(reader: Optional[DiskReader], source_path: str, items: List[RecoverableItem], dest_dir: str,
            keep_structure: bool, progress: Callable[[float, str], None], cancelled: Callable[[], bool],
            workers: int = 4, verify: bool = False, on_item: Optional[Callable[[RecoverableItem, str], None]] = None
            ) -> dict:
    if same_drive(source_path, dest_dir):
        raise RecoveryError("The destination is on the drive you're recovering from. "
                            "Saving there can overwrite the files you're trying to get back. "
                            "Choose a folder on another drive or a USB stick.")
    os.makedirs(dest_dir, exist_ok=True)
    total = sum(i.size for i in items) or 1
    lock = threading.Lock()
    state = {"written": 0, "ok": 0, "last": 0.0}
    failed: List[tuple] = []
    rows: Dict[int, list] = {}
    namer = _Namer()
    checker = None
    if verify:
        from .verify import verify_file
        checker = verify_file

    def tick(n: int, name: str):
        with lock:
            state["written"] += n
            now = time.monotonic()
            if now - state["last"] > 0.1:
                state["last"] = now
                progress(min(state["written"] / total, 1.0), f"Recovering {name}")

    def one(idx: int, item: RecoverableItem):
        if cancelled():
            return
        parts = [safe_name(p) for p in item.folder.split("/") if p] if keep_structure else []
        folder = os.path.join(dest_dir, *parts)
        target = namer.claim(os.path.join(folder, safe_name(item.name)))
        verdict = ""
        try:
            os.makedirs(folder, exist_ok=True)
            with open(target, "wb") as out:
                for chunk in iter_item_data(reader, item):
                    if cancelled():
                        break
                    out.write(chunk)
                    tick(len(chunk), item.name)
            if cancelled():
                os.remove(target)  # don't leave a half-written file that looks complete
                return
            ts = None
            if item.modified and item.modified.year > 1970:
                ts = item.modified.timestamp()
            if ts:
                try:
                    os.utime(target, (ts, ts))
                except (OSError, OverflowError, ValueError):
                    pass
            if checker:
                status, detail = checker(target, item.ext)
                item.verified = status
                verdict = detail if status != "ok" else "Checked: looks intact"
            with lock:
                state["ok"] += 1
                rows[idx] = [item.full_path, target, item.size, item.condition, item.chance, item.source,
                             "Recovered", verdict]
            if on_item:
                on_item(item, target)
        except OSError as e:
            with lock:
                failed.append((item.full_path, str(e)))
                rows[idx] = [item.full_path, target, item.size, item.condition, item.chance, item.source,
                             f"Failed: {e.strerror or e}", ""]

    # Small files first: people see progress immediately and most wanted files are small.
    order = sorted(range(len(items)), key=lambda i: items[i].size)
    if workers <= 1:
        for i in order:
            one(i, items[i])
    else:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(lambda i: one(i, items[i]), order))
    progress(1.0, "Writing report")

    report = os.path.join(dest_dir, "Lifeline recovery report.csv")
    try:
        report = namer.claim(report)
        with open(report, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Original path", "Saved as", "Size (bytes)", "Condition", "Recovery chance", "Found by",
                        "Result", "Check"])
            w.writerows(rows[i] for i in sorted(rows))
    except OSError:
        report = None
    damaged = [items[i] for i in rows if items[i].verified == "damaged"]
    return {"recovered": state["ok"], "failed": failed, "bytes": state["written"], "report": report,
            "cancelled": cancelled(), "damaged": damaged, "dest": dest_dir}
