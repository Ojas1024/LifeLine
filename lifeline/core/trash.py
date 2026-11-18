"""Files still sitting in the Recycle Bin / Trash.

The cheapest recovery there is: the data is intact and the bin remembers where
each file came from. Many people "lose" files that are only here, so Lifeline
checks it before anything else.

Windows: <drive>:\\$Recycle.Bin\\<user SID>\\$I...(metadata) + $R...(data)
Linux:   ~/.local/share/Trash/{info,files} and <mount>/.Trash-<uid> (freedesktop spec)
macOS:   ~/.Trash and <volume>/.Trashes/<uid> (no original location is recorded)
"""
from __future__ import annotations

import os
import struct
import sys
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from .models import Condition, RecoverableItem, Source

_EPOCH = datetime(1601, 1, 1)


def _filetime(ft: int) -> Optional[datetime]:
    try:
        return _EPOCH + timedelta(microseconds=ft // 10) if ft else None
    except OverflowError:
        return None


def _mtime(path: str) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime)
    except (OSError, ValueError, OverflowError):
        return None


def _folder_from_original(original: str) -> str:
    """Turn 'C:\\Users\\Ana\\Documents' or '/home/ana/Documents' into a safe relative folder."""
    p = original.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = f"{p[0].upper()} drive{p[2:]}"
    return "Recycle Bin/" + p.strip("/") if p.strip("/") else "Recycle Bin"


def _expand(data_path: str, original: str, deleted_at: Optional[datetime], out: List[RecoverableItem],
            cancelled: Callable[[], bool]):
    """Add one bin entry; a deleted folder becomes one item per file inside it."""
    orig_dir, orig_name = os.path.split(original.replace("\\", "/"))
    if os.path.isdir(data_path) and not os.path.islink(data_path):
        for root, _dirs, files in os.walk(data_path):
            if cancelled():
                return
            rel = os.path.relpath(root, data_path).replace("\\", "/")
            sub = orig_name if rel == "." else f"{orig_name}/{rel}"
            for f in files:
                p = os.path.join(root, f)
                _add(p, f, f"{orig_dir}/{sub}", deleted_at, out)
    elif os.path.isfile(data_path):
        _add(data_path, orig_name or os.path.basename(data_path), orig_dir, deleted_at, out)


def _add(path: str, name: str, orig_dir: str, deleted_at, out: List[RecoverableItem]):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    out.append(RecoverableItem(
        id=len(out), name=name, folder=_folder_from_original(orig_dir), size=size, source=Source.TRASH,
        modified=_mtime(path), condition=Condition.GOOD, file_path=path, deleted_at=deleted_at,
        note="In the Recycle Bin: the file is complete."))


# --------------------------------------------------------------------------- Windows
def parse_windows_i_file(data: bytes) -> Optional[Tuple[int, Optional[datetime], str]]:
    """$I file -> (size, deleted_at, original path). Version 1 = Vista..8.1, 2 = Windows 10+."""
    if len(data) < 24:
        return None
    version, size, ft = struct.unpack_from("<QQQ", data, 0)
    if version == 1:
        path = data[24:24 + 520].decode("utf-16-le", "ignore").split("\x00", 1)[0]
    elif version == 2 and len(data) >= 28:
        n = struct.unpack_from("<I", data, 24)[0]
        path = data[28:28 + n * 2].decode("utf-16-le", "ignore").rstrip("\x00")
    else:
        return None
    return size, _filetime(ft), path


def _windows_bins() -> Iterable[str]:
    import string
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\$Recycle.Bin"
        try:
            sids = os.listdir(root)
        except OSError:
            continue
        for sid in sids:
            yield os.path.join(root, sid)


def _scan_windows(out, cancelled, bins=None):
    for b in bins if bins is not None else _windows_bins():
        try:
            names = os.listdir(b)
        except OSError:
            continue  # other users' bins need administrator rights
        for n in names:
            if cancelled():
                return
            if not n.upper().startswith("$I"):
                continue
            try:
                with open(os.path.join(b, n), "rb") as f:
                    meta = parse_windows_i_file(f.read(4096))
            except OSError:
                continue
            if not meta:
                continue
            _size, deleted_at, original = meta
            _expand(os.path.join(b, "$R" + n[2:]), original, deleted_at, out, cancelled)


# --------------------------------------------------------------------------- freedesktop (Linux, BSD)
def _xdg_bins() -> Iterable[str]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    yield os.path.join(home, "Trash")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    try:
        with open("/proc/self/mounts") as f:
            mounts = [line.split()[1].replace("\\040", " ") for line in f]
    except OSError:
        mounts = []
    for m in mounts:
        if m.startswith(("/proc", "/sys", "/dev", "/run", "/snap")):
            continue
        yield os.path.join(m, f".Trash-{uid}")
        yield os.path.join(m, ".Trash", str(uid))


def parse_trashinfo(text: str) -> Tuple[str, Optional[datetime]]:
    path, when = "", None
    for line in text.splitlines():
        if line.startswith("Path="):
            path = unquote(line[5:].strip())
        elif line.startswith("DeletionDate="):
            try:
                when = datetime.fromisoformat(line[13:].strip())
            except ValueError:
                pass
    return path, when


def _scan_xdg(out, cancelled, bins=None):
    seen = set()
    for b in bins if bins is not None else _xdg_bins():
        real = os.path.realpath(b)
        info_dir = os.path.join(b, "info")
        if real in seen or not os.path.isdir(info_dir):
            continue
        seen.add(real)
        top = os.path.dirname(os.path.dirname(b)) if os.path.basename(os.path.dirname(b)) == ".Trash" else \
            os.path.dirname(b)
        for n in os.listdir(info_dir):
            if cancelled():
                return
            if not n.endswith(".trashinfo"):
                continue
            try:
                with open(os.path.join(info_dir, n), encoding="utf-8", errors="replace") as f:
                    original, when = parse_trashinfo(f.read())
            except OSError:
                continue
            if original and not original.startswith("/"):
                original = os.path.join(top, original)  # per-volume bins store relative paths
            data = os.path.join(b, "files", n[:-len(".trashinfo")])
            _expand(data, original or "/" + n[:-10], when, out, cancelled)


# --------------------------------------------------------------------------- macOS
def _scan_mac(out, cancelled):
    uid = os.getuid()
    bins = [os.path.expanduser("~/.Trash")]
    try:
        bins += [os.path.join("/Volumes", v, ".Trashes", str(uid)) for v in os.listdir("/Volumes")]
    except OSError:
        pass
    for b in bins:
        try:
            names = os.listdir(b)
        except OSError:
            continue  # ~/.Trash needs Full Disk Access on recent macOS
        for n in names:
            if cancelled():
                return
            if n == ".DS_Store":
                continue
            p = os.path.join(b, n)
            _expand(p, f"/Trash/{n}", _mtime(p), out, cancelled)


def scan_trash(progress: Callable[[float, str], None] = lambda f, m: None,
               cancelled: Callable[[], bool] = lambda: False) -> List[RecoverableItem]:
    out: List[RecoverableItem] = []
    progress(0.0, "Looking in the Recycle Bin")
    if sys.platform == "win32":
        _scan_windows(out, cancelled)
    elif sys.platform == "darwin":
        _scan_mac(out, cancelled)
    else:
        _scan_xdg(out, cancelled)
    progress(1.0, f"{len(out):,} files in the Recycle Bin")
    return out
