"""Copies of deleted files that SSD TRIM never touched.

TRIM erases the blocks a deleted file used, and no software can bring those back. But
other copies often survive somewhere else on the computer:

* Restore points and snapshots: Windows Volume Shadow Copies ("Previous Versions"),
  macOS Time Machine local snapshots, Linux snapper / Timeshift snapshots. A file that
  is in a snapshot but no longer on the live drive comes back complete, as it was then.
* Thumbnail caches: Windows thumbcache_*.db and the freedesktop ~/.cache/thumbnails keep
  small previews of pictures, often long after the pictures themselves are deleted.
* Autosaves: Office and LibreOffice keep recovery copies of documents being edited.

Everything here reads through the normal file system, so it works on any drive type.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from .models import Cancelled, Condition, Progress, RecoverableItem, Source

NoProgress: Progress = lambda f, m: None  # noqa: E731
SKIP_DIRS = {"appdata", "library", ".cache", "cache", "caches", "node_modules", ".git", "temp", "tmp",
             "$recycle.bin", ".trash", ".trashes", "trash", ".local", ".npm", ".gradle", ".m2", ".cargo",
             ".rustup", ".venv", "venv", "__pycache__", ".conda", "anaconda3", "miniconda3", ".vscode-server"}


def _mtime(path: str) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime)
    except (OSError, ValueError, OverflowError):
        return None


# =========================================================================== snapshots
@dataclass
class Snapshot:
    root: str          # where the snapshot's files can be read
    live_root: str     # the same place on the live drive
    label: str         # shown to people: "Restore point 17 Nov 2026 10:00"
    order: float       # higher = newer
    subdirs: Tuple[str, ...] = ("",)   # where people keep their files, relative to root


def parse_vssadmin(text: str) -> List[Snapshot]:
    """`vssadmin list shadows` output (any language: we match paths, not words)."""
    snaps, when, letter = [], "", None
    for line in text.splitlines():
        m_time = re.search(r":\s*(\d{1,4}[./-]\d{1,2}[./-]\d{1,4}[^\r\n]*)$", line)
        if m_time and "\\\\?\\" not in line:
            when = m_time.group(1).strip()
        m_vol = re.search(r"\(([A-Za-z]):\)\\\\\?\\Volume", line)
        if m_vol:
            letter = m_vol.group(1).upper()
        m_snap = re.search(r"(\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy(\d+))", line)
        if m_snap and letter:
            num = int(m_snap.group(2))
            snaps.append(Snapshot(root=m_snap.group(1) + "\\", live_root=f"{letter}:\\",
                                  label=f"Restore point {when or num} ({letter}:)", order=num, subdirs=("Users",)))
    return snaps


def parse_tmutil(text: str) -> List[str]:
    return re.findall(r"(com\.apple\.TimeMachine\.\d{4}-\d{2}-\d{2}-\d{6}(?:\.local)?)", text)


def _windows_snapshots() -> List[Snapshot]:
    try:
        out = subprocess.run(["vssadmin", "list", "shadows"], capture_output=True, text=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_vssadmin(out)


def _mac_snapshots(cleanups: list) -> List[Snapshot]:
    vol = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    try:
        out = subprocess.run(["tmutil", "listlocalsnapshots", vol], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    snaps = []
    for name in parse_tmutil(out):
        mnt = tempfile.mkdtemp(prefix="lifeline-snap-")
        r = subprocess.run(["mount_apfs", "-o", "ro,nobrowse", "-s", name, vol, mnt], capture_output=True)
        if r.returncode != 0:
            os.rmdir(mnt)
            continue
        cleanups.append(lambda m=mnt: (subprocess.run(["umount", m], capture_output=True), os.rmdir(m)))
        stamp = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})", name)
        when = datetime(*map(int, stamp.groups())) if stamp else None
        snaps.append(Snapshot(root=mnt, live_root=vol,
                              label=f"Time Machine {when:%d %b %Y %H:%M}" if when else name,
                              order=when.timestamp() if when else 0, subdirs=("Users",)))
    return snaps


def _linux_snapshots(base: str = "/") -> List[Snapshot]:
    snaps = []
    patterns = [  # (glob, live root, subdirs)
        (".snapshots/*/snapshot", "", ("home", "root")),                 # snapper on /
        ("home/.snapshots/*/snapshot", "home", ("",)),                   # snapper on /home
        ("timeshift/snapshots/*/localhost", "", ("home", "root")),       # Timeshift (rsync)
        ("run/timeshift/*/backup/timeshift*/snapshots/*/localhost", "", ("home", "root")),
        ("timeshift-btrfs/snapshots/*/@home", "home", ("",)),
    ]
    for pat, live, subdirs in patterns:
        for root in glob.glob(os.path.join(base, pat)):
            name = os.path.basename(os.path.dirname(root))
            try:
                order = os.stat(root).st_mtime
            except OSError:
                order = 0
            snaps.append(Snapshot(root=root, live_root=os.path.join(base, live), order=order, subdirs=subdirs,
                                  label=f"Snapshot {name}"))
    return snaps


def list_snapshots(cleanups: Optional[list] = None) -> List[Snapshot]:
    cleanups = cleanups if cleanups is not None else []
    if sys.platform == "win32":
        snaps = _windows_snapshots()
    elif sys.platform == "darwin":
        snaps = _mac_snapshots(cleanups)
    else:
        snaps = _linux_snapshots()
    return sorted(snaps, key=lambda s: -s.order)


def deleted_since(snaps: List[Snapshot], progress: Progress = NoProgress, cancelled: Cancelled = lambda: False,
                  limit: int = 200_000) -> List[RecoverableItem]:
    """Files that are in a snapshot but no longer on the live drive. Newest snapshot wins."""
    out: List[RecoverableItem] = []
    seen = set()
    checked = [0]

    def walk(snap: Snapshot, sdir: str, ldir: str, rel: str, live_missing: bool):
        if cancelled() or len(out) >= limit:
            return
        try:
            entries = list(os.scandir(sdir))
        except OSError:
            return
        for e in entries:
            if cancelled():
                return
            lpath = os.path.join(ldir, e.name)
            rpath = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() in SKIP_DIRS:
                        continue
                    walk(snap, e.path, lpath, rpath, live_missing or not os.path.isdir(lpath))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            checked[0] += 1
            if checked[0] % 2000 == 0:
                progress(0.5, f"Comparing restore points with the drive: {checked[0]:,} files checked")
            key = rpath.lower()
            if key in seen:
                continue
            if live_missing or not os.path.lexists(lpath):
                seen.add(key)
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                folder = os.path.dirname(rpath)
                out.append(RecoverableItem(
                    id=0, name=e.name, folder=f"Restore points/{snap.label}" + (f"/{folder}" if folder else ""),
                    size=size, source=Source.SNAPSHOT, modified=_mtime(e.path), condition=Condition.GOOD,
                    file_path=e.path,
                    note=f"A complete copy from {snap.label}. It's no longer on the drive, but the restore point "
                         "kept it, so SSD TRIM didn't touch it."))

    for snap in snaps:
        for sub in snap.subdirs:
            sroot = os.path.join(snap.root, sub) if sub else snap.root
            lroot = os.path.join(snap.live_root, sub) if sub else snap.live_root
            if os.path.isdir(sroot):
                walk(snap, sroot, lroot, sub.replace("\\", "/"), not os.path.isdir(lroot))
    return out


# =========================================================================== thumbnails
def png_text(data: bytes) -> Dict[str, str]:
    """tEXt / iTXt chunks of a PNG."""
    out, pos = {}, 8
    while pos + 8 <= len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if kind == b"tEXt" and b"\0" in body:
            k, v = body.split(b"\0", 1)
            out[k.decode("latin-1")] = v.decode("latin-1")
        elif kind == b"iTXt" and b"\0" in body:
            k, rest = body.split(b"\0", 1)
            if len(rest) >= 2:
                comp, rest = rest[0], rest[2:]
                parts = rest.split(b"\0", 2)
                if len(parts) == 3:
                    txt = zlib.decompress(parts[2]) if comp else parts[2]
                    out[k.decode("latin-1")] = txt.decode("utf-8", "replace")
        elif kind == b"IEND":
            break
        pos += 12 + n
    return out


def freedesktop_thumbnails(dirs: Optional[Iterable[str]] = None, cancelled: Cancelled = lambda: False
                           ) -> List[RecoverableItem]:
    """~/.cache/thumbnails previews whose original picture no longer exists."""
    if dirs is None:
        homes = {os.path.expanduser("~")} | set(glob.glob("/home/*"))
        dirs = [os.path.join(h, ".cache", "thumbnails") for h in homes]
    out = []
    for d in dirs:
        for path in glob.glob(os.path.join(d, "*", "*.png")):
            if cancelled():
                return out
            try:
                with open(path, "rb") as f:
                    head = f.read(64 * 1024)
            except OSError:
                continue
            uri = png_text(head).get("Thumb::URI", "")
            if not uri.startswith("file://"):
                continue
            original = unquote(urlparse(uri).path)
            if os.path.exists(original):
                continue  # not lost
            stem = os.path.splitext(os.path.basename(original))[0] or "picture"
            folder = os.path.dirname(original).strip("/")
            out.append(RecoverableItem(
                id=0, name=f"{stem} (preview).png", folder="Thumbnails" + (f"/{folder}" if folder else ""),
                size=os.path.getsize(path), source=Source.THUMBNAIL, modified=_mtime(path),
                condition=Condition.GOOD, file_path=path,
                note=f"A small preview of {original}, which no longer exists. The full picture is gone, "
                     "but this preview survived."))
    return out


def _windows_thumbcaches() -> List[str]:
    roots = {os.environ.get("LOCALAPPDATA", "")}
    roots |= set(glob.glob(os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users", "*", "AppData", "Local")))
    files = []
    for r in filter(None, roots):
        files += glob.glob(os.path.join(r, "Microsoft", "Windows", "Explorer", "thumbcache_*.db"))
    # 16 and 32 px caches hold icons, and the idx file holds no pictures
    return [f for f in files if not re.search(r"thumbcache_(16|32|idx)\.db$", f, re.I)]


def carve_thumbcache(path: str, cancelled: Cancelled = lambda: False, min_size: int = 1500) -> List[RecoverableItem]:
    """Pull the JPEG/PNG/BMP previews out of a Windows thumbcache_*.db (entries aren't aligned)."""
    from .carver import Carver
    from .disk import DiskReader
    out = []
    try:
        reader = DiskReader(path)
    except Exception:
        return out
    try:
        found = Carver(reader, block=1).scan([(0, reader.length)], ["jpg", "png", "bmp"], NoProgress, cancelled,
                                             workers=1)
        seen = set()
        stem = os.path.splitext(os.path.basename(path))[0]
        user = _user_of(path)
        for i, it in enumerate(found, 1):
            if it.size < min_size:
                continue
            data = reader.read_at(it.extents[0][0], it.size)
            digest = hashlib.md5(data).digest()
            if digest in seen:
                continue
            seen.add(digest)
            out.append(RecoverableItem(
                id=0, name=f"Preview {len(seen):04d}.{it.ext}",
                folder=f"Thumbnails/Windows {user + ' ' if user else ''}{stem}", size=len(data),
                source=Source.THUMBNAIL, modified=_mtime(path), condition=Condition.GOOD, resident_data=data,
                note="A preview Windows kept of a picture (thumbnail cache). The full-size photo may be gone, "
                     "but this smaller copy survived."))
    finally:
        reader.close()
    return out


def _user_of(path: str) -> str:
    m = re.search(r"[\\/]Users[\\/]([^\\/]+)[\\/]", path)
    return m.group(1) if m else ""


# =========================================================================== autosaves
def _autosave_globs() -> List[Tuple[str, str]]:
    """(glob pattern, app name) for this platform."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        roam = {os.environ.get("APPDATA", "")} | set(glob.glob(os.path.join(
            os.environ.get("SystemDrive", "C:") + "\\", "Users", "*", "AppData", "Roaming")))
        local = {os.environ.get("LOCALAPPDATA", "")} | set(glob.glob(os.path.join(
            os.environ.get("SystemDrive", "C:") + "\\", "Users", "*", "AppData", "Local")))
        pats = []
        for r in filter(None, roam):
            pats += [(os.path.join(r, "Microsoft", "Word", "*.asd"), "Word"),
                     (os.path.join(r, "Microsoft", "Word", "*.wbk"), "Word"),
                     (os.path.join(r, "Microsoft", "Excel", "*.xar"), "Excel"),
                     (os.path.join(r, "Microsoft", "Excel", "*", "*.xar"), "Excel"),
                     (os.path.join(r, "Microsoft", "PowerPoint", "*.pp*"), "PowerPoint"),
                     (os.path.join(r, "LibreOffice", "4", "user", "backup", "*"), "LibreOffice")]
        for r in filter(None, local):
            pats += [(os.path.join(r, "Microsoft", "Office", "UnsavedFiles", "*"), "Office (unsaved)")]
        return pats
    if sys.platform == "darwin":
        c = os.path.join(home, "Library", "Containers")
        return [(os.path.join(c, "com.microsoft.Word", "Data", "Library", "Preferences", "AutoRecovery", "*"), "Word"),
                (os.path.join(c, "com.microsoft.Excel", "Data", "Library", "Application Support", "Microsoft", "*"),
                 "Excel"),
                (os.path.join(c, "com.microsoft.Powerpoint", "Data", "Library", "Preferences", "AutoRecovery", "*"),
                 "PowerPoint"),
                (os.path.join(home, "Library", "Application Support", "LibreOffice", "4", "user", "backup", "*"),
                 "LibreOffice"),
                (os.path.join(home, "Library", "Autosave Information", "*"), "macOS Autosave")]
    return [(os.path.join(home, ".config", "libreoffice", "4", "user", "backup", "*"), "LibreOffice")]


def find_autosaves(patterns: Optional[List[Tuple[str, str]]] = None) -> List[RecoverableItem]:
    out, seen = [], set()
    for pat, app in patterns if patterns is not None else _autosave_globs():
        for path in glob.glob(pat):
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            out.append(RecoverableItem(
                id=0, name=os.path.basename(path), folder=f"Autosaves/{app}", size=os.path.getsize(path),
                source=Source.AUTOSAVE, modified=_mtime(path), condition=Condition.GOOD, file_path=path,
                note=f"An automatic recovery copy kept by {app}. It can hold work that was never saved, or an "
                     "earlier version of a deleted document. Rename it to the right extension (.docx, .xlsx …) "
                     "if it doesn't open."))
    return out


# =========================================================================== everything
def find_all(progress: Progress = NoProgress, cancelled: Cancelled = lambda: False, snapshots: bool = True,
             thumbnails: bool = True, autosaves: bool = True, cleanups: Optional[list] = None
             ) -> List[RecoverableItem]:
    out: List[RecoverableItem] = []
    if autosaves:
        progress(0.02, "Looking for Office and LibreOffice autosaves")
        out += find_autosaves()
    if thumbnails and not cancelled():
        progress(0.08, "Looking in thumbnail caches")
        if sys.platform == "win32":
            caches = _windows_thumbcaches()
            for i, c in enumerate(caches):
                if cancelled():
                    break
                progress(0.08 + 0.2 * i / max(len(caches), 1), f"Reading thumbnail cache {os.path.basename(c)}")
                out += carve_thumbcache(c, cancelled)
        else:
            out += freedesktop_thumbnails(cancelled=cancelled)
    if snapshots and not cancelled():
        progress(0.3, "Looking for restore points and snapshots")
        snaps = list_snapshots(cleanups)
        if snaps:
            out += deleted_since(snaps, lambda f, m: progress(0.35 + 0.6 * f, m), cancelled)
    progress(1.0, f"Found {len(out):,} surviving copies")
    return out
