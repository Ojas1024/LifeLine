"""Finding drives, checking permissions and keeping recovery off the source drive,
on Windows, Linux and macOS.

Everything here degrades gracefully: if a platform tool is missing we list fewer
details rather than fail, and people can always open a disk image instead.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from .disk import IS_LINUX, IS_MAC, IS_WINDOWS, drive_letter_of, is_device_path, raw_path_for_letter

RECOVERABLE_FS = {"ntfs", "vfat", "fat", "fat12", "fat16", "fat32", "msdos", "exfat"}


@dataclass
class VolumeInfo:
    path: str                    # what DiskReader opens: \\.\D:, /dev/sdb1, /dev/rdisk2s1
    name: str                    # short id shown to people: "D:", "sdb1", "disk2s1"
    label: str
    filesystem: str
    total: int
    free: Optional[int]
    removable: bool
    media: str                   # "SSD", "HDD" or "Unknown"
    trim: Optional[bool]
    mountpoint: str = ""
    is_disk: bool = False        # a whole physical disk (for lost partitions and imaging)
    is_system: bool = False      # the drive the operating system runs from
    model: str = ""
    letter: str = ""             # Windows drive letter
    readable: bool = True        # can we open it right now (admin rights etc.)

    @property
    def raw_path(self) -> str:
        return self.path

    @property
    def kind(self) -> str:
        """Plain-language kind: what someone would call this drive."""
        if self.is_disk:
            return "Whole disk"
        if self.removable:
            return "USB drive or memory card"
        return {"SSD": "Internal SSD", "HDD": "Internal hard drive"}.get(self.media, "Local drive")

    @property
    def title(self) -> str:
        if self.is_disk:
            return self.model or "Disk"
        if self.label:
            return self.label
        if self.is_system:
            return {"win32": "Windows", "darwin": "Macintosh HD"}.get(sys.platform, "System drive")
        return self.kind

    @property
    def fs_supported(self) -> bool:
        return self.filesystem.lower() in RECOVERABLE_FS


# ==========================================================================
# Permissions
# ==========================================================================

def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def needs_admin(vols: Optional[List[VolumeInfo]] = None) -> bool:
    """True when we probably can't read any drive without elevating."""
    if is_admin():
        return False
    if IS_WINDOWS:
        return True
    vols = vols if vols is not None else list_volumes()
    return not any(v.readable for v in vols)


def _launch_args() -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable] + sys.argv[1:]
    return [sys.executable, "-m", "lifeline"] + sys.argv[1:]


def relaunch_as_admin() -> bool:
    """Start an elevated copy of Lifeline. Returns True if the elevated copy was launched
    (the caller should then quit)."""
    args = _launch_args()
    if IS_WINDOWS:
        import ctypes
        params = " ".join(f'"{a}"' for a in args[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", args[0], params, os.getcwd(), 1)
        return rc > 32
    if IS_MAC:
        cmd = " ".join(shlex.quote(a) for a in args) + " >/dev/null 2>&1 &"
        script = f'do shell script {json.dumps(cmd)} with administrator privileges'
        try:
            return subprocess.call(["osascript", "-e", script]) == 0
        except OSError:
            return False
    if shutil.which("pkexec"):
        # pkexec clears the environment; pass through what a GUI app needs to find the display.
        keep = ["DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "QT_QPA_PLATFORM", "HOME",
                "LANG", "PYTHONPATH"]
        env = [f"{k}={os.environ[k]}" for k in keep if os.environ.get(k)]
        try:
            subprocess.Popen(["pkexec", "env", *env, *args], cwd=os.getcwd(), start_new_session=True)
            return True
        except OSError:
            return False
    return False


def elevation_hint() -> str:
    if IS_WINDOWS:
        return "Right-click Lifeline and choose Run as administrator."
    cmd = "sudo " + " ".join(shlex.quote(a) for a in _launch_args())
    if IS_MAC:
        return (f"Run it from Terminal with: {cmd}\nmacOS may also need Full Disk Access for Terminal "
                "(System Settings > Privacy & Security).")
    return f"Run it from a terminal with: sudo -E {' '.join(shlex.quote(a) for a in _launch_args())}"


# ==========================================================================
# Listing drives
# ==========================================================================

def list_volumes(include_disks: bool = False) -> List[VolumeInfo]:
    """Volumes (partitions) people recognise, optionally followed by whole physical disks."""
    try:
        if IS_WINDOWS:
            vols = _windows_volumes() + (_windows_disks() if include_disks else [])
        elif IS_MAC:
            vols = _mac_volumes(include_disks)
        elif IS_LINUX:
            vols = _linux_volumes(include_disks)
        else:
            vols = []
    except Exception:
        vols = []
    return vols


def list_disks() -> List[VolumeInfo]:
    return [v for v in list_volumes(include_disks=True) if v.is_disk]


def _readable(path: str) -> bool:
    if IS_WINDOWS:
        return True
    return os.access(path, os.R_OK)


def _free_space(mountpoint: str) -> Optional[int]:
    if not mountpoint:
        return None
    try:
        return shutil.disk_usage(mountpoint).free
    except OSError:
        return None


# --------------------------------------------------------------------------- Linux
_SKIP_LINUX_TYPES = {"loop", "rom", "zram"}


def _linux_volumes(include_disks: bool) -> List[VolumeInfo]:
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o",
                              "NAME,PATH,SIZE,FSTYPE,LABEL,MOUNTPOINT,RM,ROTA,TYPE,MODEL,PKNAME,DISC-GRAN,HOTPLUG,TRAN"],
                             capture_output=True, text=True, timeout=15).stdout
        tree = json.loads(out)["blockdevices"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return _linux_proc_partitions()
    vols, disks = [], []
    root_dev = _linux_root_device()

    def b(x) -> bool:
        return x in (True, "1", 1)

    def walk(node, parent=None):
        typ = node.get("type") or ""
        path = node.get("path") or f"/dev/{node['name']}"
        rota = b(node.get("rota"))
        removable = b(node.get("rm")) or b(node.get("hotplug")) or (node.get("tran") == "usb")
        if parent is not None:
            removable = removable or parent["removable"]
        media = "HDD" if rota else "SSD"
        if removable and parent is None and typ == "disk" and not node.get("tran"):
            media = "Unknown"
        trim = (node.get("disc-gran") or 0) > 0 if media == "SSD" else None
        info = {"removable": removable}
        children = node.get("children") or []
        if typ == "disk":
            disks.append(VolumeInfo(path=path, name=node["name"], label="", filesystem="",
                                    total=int(node.get("size") or 0), free=None, removable=removable,
                                    media=media, trim=trim, is_disk=True, model=(node.get("model") or "").strip(),
                                    readable=_readable(path),
                                    is_system=any(_contains_dev(c, root_dev) for c in children) or path == root_dev))
        fstype = (node.get("fstype") or "").lower()
        is_volume = typ in ("part", "crypt", "lvm", "md", "raid1", "raid0") or (typ == "disk" and fstype and not children)
        if typ not in _SKIP_LINUX_TYPES and is_volume and fstype not in ("swap", "crypto_luks", "lvm2_member"):
            mp = node.get("mountpoint") or ""
            vols.append(VolumeInfo(path=path, name=node["name"], label=node.get("label") or "",
                                   filesystem=_fs_display(fstype), total=int(node.get("size") or 0),
                                   free=_free_space(mp), removable=removable, media=media, trim=trim,
                                   mountpoint=mp, is_system=mp == "/", readable=_readable(path)))
        for c in children:
            walk(c, info)

    for node in tree:
        if (node.get("type") or "") in _SKIP_LINUX_TYPES and (node.get("fstype") or "").lower() not in RECOVERABLE_FS:
            continue
        walk(node)
    vols.sort(key=lambda v: (not v.fs_supported, v.is_system, v.name))
    return vols + (disks if include_disks else [])


def _contains_dev(node, dev: str) -> bool:
    if (node.get("path") or "") == dev:
        return True
    return any(_contains_dev(c, dev) for c in node.get("children") or [])


def _linux_root_device() -> str:
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "/":
                    return os.path.realpath(parts[0])
    except OSError:
        pass
    return ""


def _linux_proc_partitions() -> List[VolumeInfo]:
    vols = []
    try:
        with open("/proc/partitions") as f:
            lines = f.read().splitlines()[2:]
    except OSError:
        return vols
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or parts[3].startswith(("loop", "ram", "zram", "sr")):
            continue
        path = f"/dev/{parts[3]}"
        vols.append(VolumeInfo(path=path, name=parts[3], label="", filesystem="", total=int(parts[2]) * 1024,
                               free=None, removable=False, media="Unknown", trim=None, readable=_readable(path)))
    return vols


def _fs_display(fstype: str) -> str:
    return {"vfat": "FAT32", "ntfs": "NTFS", "exfat": "exFAT", "ext4": "ext4", "ext3": "ext3", "ext2": "ext2",
            "btrfs": "Btrfs", "xfs": "XFS", "hfsplus": "HFS+", "apfs": "APFS", "msdos": "FAT"}.get(fstype,
                                                                                                 fstype or "RAW")


# --------------------------------------------------------------------------- macOS
def _plist(args: List[str]) -> dict:
    import plistlib  # macOS only; pulls in the XML parser
    out = subprocess.run(["diskutil", *args, "-plist"], capture_output=True, timeout=20).stdout
    return plistlib.loads(out) if out else {}


def _mac_volumes(include_disks: bool) -> List[VolumeInfo]:
    try:
        listing = _plist(["list"])
    except Exception:  # diskutil missing, timed out or returned garbage
        return []
    vols, disks = [], []
    for d in listing.get("AllDisksAndPartitions", []):
        disk_id = d.get("DeviceIdentifier", "")
        try:
            dinfo = _plist(["info", disk_id])
        except Exception:
            dinfo = {}
        if dinfo.get("VirtualOrPhysical") == "Virtual" and not d.get("Partitions"):
            continue  # APFS containers: their physical store is listed separately
        removable = bool(dinfo.get("RemovableMediaOrExternalDevice") or dinfo.get("Ejectable")
                         or not dinfo.get("Internal", True))
        media = "SSD" if dinfo.get("SolidState") else ("HDD" if "SolidState" in dinfo else "Unknown")
        trim = bool(dinfo.get("TRIMSupport")) if media == "SSD" else None
        if dinfo.get("VirtualOrPhysical") != "Virtual":
            disks.append(VolumeInfo(path=f"/dev/r{disk_id}", name=disk_id, label="", filesystem="",
                                    total=int(d.get("Size") or 0), free=None, removable=removable, media=media,
                                    trim=trim, is_disk=True, model=dinfo.get("MediaName", ""),
                                    readable=_readable(f"/dev/r{disk_id}")))
        for p in d.get("Partitions", []) or []:
            pid = p.get("DeviceIdentifier", "")
            content = p.get("Content", "")
            if content in ("EFI", "Apple_Boot", "Apple_APFS_ISC", "Apple_APFS_Recovery"):
                continue
            try:
                pinfo = _plist(["info", pid])
            except Exception:
                pinfo = {}
            fs = pinfo.get("FilesystemName") or pinfo.get("FilesystemType") or content or "RAW"
            mp = p.get("MountPoint", "") or pinfo.get("MountPoint", "")
            vols.append(VolumeInfo(path=f"/dev/r{pid}", name=pid, label=p.get("VolumeName", "") or "",
                                   filesystem=_mac_fs(fs), total=int(p.get("Size") or 0),
                                   free=pinfo.get("FreeSpace") or _free_space(mp), removable=removable,
                                   media=media, trim=trim, mountpoint=mp, is_system=mp == "/",
                                   readable=_readable(f"/dev/r{pid}")))
    vols.sort(key=lambda v: (not v.fs_supported, v.is_system, v.name))
    return vols + (disks if include_disks else [])


def _mac_fs(fs: str) -> str:
    f = fs.lower()
    if "ntfs" in f or "windows_ntfs" in f:
        return "NTFS"
    if "exfat" in f:
        return "exFAT"
    if "fat32" in f or "ms-dos" in f or "msdos" in f or f == "dos_fat_32":
        return "FAT32"
    if "apfs" in f:
        return "APFS"
    if "hfs" in f or "journaled" in f:
        return "HFS+"
    return fs


# --------------------------------------------------------------------------- Windows
def _windows_volumes() -> List[VolumeInfo]:
    import ctypes
    k32 = ctypes.windll.kernel32
    vols = []
    mask = k32.GetLogicalDrives()
    sysdrive = os.environ.get("SystemDrive", "C:")[0].upper()
    admin = is_admin()
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(65 + i)
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3):  # DRIVE_REMOVABLE, DRIVE_FIXED
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(64)
        ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 64)
        total, free = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), ctypes.byref(free))
        media, trim, removable_bus = _windows_media(raw_path_for_letter(letter))
        vols.append(VolumeInfo(
            path=raw_path_for_letter(letter), name=f"{letter}:", letter=letter,
            label=label.value if ok else "", filesystem=fs.value if ok else "RAW",
            total=total.value, free=free.value, removable=dtype == 2 or removable_bus,
            media=media, trim=trim, mountpoint=root, is_system=letter == sysdrive, readable=admin))
    return vols


def _windows_media(path: str):
    """("SSD"|"HDD"|"Unknown", trim or None, is_usb) for a device path."""
    from . import winapi
    h = winapi.open_device(path)
    if h is None:
        return "Unknown", None, False
    try:
        media, trim, usb = "Unknown", None, False
        seek = winapi.storage_property(h, 7, 64)    # StorageDeviceSeekPenaltyProperty
        if seek and len(seek) >= 9:
            media = "HDD" if seek[8] else "SSD"
        tr = winapi.storage_property(h, 8, 64)      # StorageDeviceTrimProperty
        if tr and len(tr) >= 9:
            trim = bool(tr[8])
        dev = winapi.storage_property(h, 0, 1024)   # StorageDeviceProperty: BusType at 0x1C
        if dev and len(dev) >= 0x20:
            usb = int.from_bytes(dev[0x1C:0x20], "little") in (7, 12, 13)  # USB, SD, MMC
        return media, trim, usb
    finally:
        winapi.close(h)


def _windows_disks() -> List[VolumeInfo]:
    import struct
    from . import winapi
    disks = []
    for n in range(32):
        path = f"\\\\.\\PhysicalDrive{n}"
        h = winapi.open_device(path)
        if h is None:
            continue
        try:
            size_raw = winapi.ioctl(h, winapi.IOCTL_DISK_GET_LENGTH_INFO, None, 8)
            dev = winapi.storage_property(h, 0, 1024)
        finally:
            winapi.close(h)
        size = struct.unpack("<q", size_raw)[0] if size_raw and len(size_raw) == 8 else 0
        model = ""
        if dev and len(dev) >= 0x20:
            vend_off, prod_off = struct.unpack_from("<II", dev, 0x0C)
            for off in (vend_off, prod_off):
                if 0 < off < len(dev):
                    model += dev[off:dev.index(b"\0", off) if b"\0" in dev[off:] else len(dev)].decode(
                        "ascii", "ignore").strip() + " "
        media, trim, usb = _windows_media(path)
        disks.append(VolumeInfo(path=path, name=f"Disk {n}", label="", filesystem="", total=size, free=None,
                                removable=usb, media=media, trim=trim, is_disk=True, model=model.strip(),
                                readable=is_admin()))
    return disks


# ==========================================================================
# Same-drive safety check
# ==========================================================================

def same_device(source: str, dest: str) -> bool:
    """Would writing into `dest` (a folder or file path) land on the device `source` reads?
    Image files are never at risk: writing elsewhere on their drive doesn't change them."""
    if not source or not is_device_path(source):
        return False
    dest = os.path.abspath(dest)
    probe = dest
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    try:
        if IS_WINDOWS:
            return _same_windows(source, probe)
        if IS_MAC:
            return _same_mac(source, probe)
        return _same_linux(source, probe)
    except Exception:
        return False


def _same_windows(source: str, dest: str) -> bool:
    d_letter = drive_letter_of(dest)
    s_letter = drive_letter_of(source)
    if s_letter:
        return s_letter == d_letter
    if "PhysicalDrive" in source and d_letter:
        from . import winapi
        n = int(source.rsplit("PhysicalDrive", 1)[1])
        return n in winapi.disk_numbers_of_volume(d_letter)
    return False


def _linux_block_chain(devnum: int) -> List[str]:
    """Kernel names of a block device and every device under it (partition -> disk, dm -> slaves)."""
    major, minor = os.major(devnum), os.minor(devnum)
    try:
        real = os.path.realpath(f"/sys/dev/block/{major}:{minor}")
    except OSError:
        return []
    names, todo, seen = [], [real], set()
    while todo:
        p = todo.pop()
        if p in seen or not os.path.isdir(p):
            continue
        seen.add(p)
        names.append(os.path.basename(p))
        if os.path.exists(os.path.join(p, "partition")):
            todo.append(os.path.dirname(p))           # partition -> its disk
        slaves = os.path.join(p, "slaves")
        if os.path.isdir(slaves):
            todo += [os.path.realpath(os.path.join(slaves, s)) for s in os.listdir(slaves)]
    return names


def _same_linux(source: str, dest: str) -> bool:
    src = os.stat(source)
    dst_dev = os.stat(dest).st_dev
    if src.st_rdev == dst_dev:
        return True
    src_name = os.path.basename(os.path.realpath(source))
    return src_name in _linux_block_chain(dst_dev)


def _same_mac(source: str, dest: str) -> bool:
    out = subprocess.run(["df", "-P", dest], capture_output=True, text=True, timeout=10).stdout.splitlines()
    if len(out) < 2:
        return False
    dest_dev = out[1].split()[0]                         # /dev/disk3s5
    src_id = os.path.basename(source).lstrip("r")         # disk2s1 from /dev/rdisk2s1
    stores = [os.path.basename(dest_dev)]
    try:
        info = _plist(["info", dest_dev])
        stores += [s.get("APFSPhysicalStore", "") for s in info.get("APFSPhysicalStores", [])]
        if info.get("ParentWholeDisk"):
            stores.append(info["ParentWholeDisk"])
    except Exception:
        pass
    for s in filter(None, stores):
        if s == src_id or s.startswith(src_id + "s"):
            return True
    return False


def volume_for_path(path: str, vols: Optional[List[VolumeInfo]] = None) -> Optional[VolumeInfo]:
    """The listed volume a folder lives on (for showing 'this is on your USB stick')."""
    vols = vols if vols is not None else list_volumes()
    best: Optional[VolumeInfo] = None
    ap = os.path.abspath(path)
    for v in vols:
        mp = v.mountpoint
        if mp and (ap == mp.rstrip("\\/") or ap.startswith(mp.rstrip("\\/") + os.sep) or ap.startswith(mp)):
            if best is None or len(mp) > len(best.mountpoint):
                best = v
    return best


def media_for_source(path: str, vols: Optional[List[VolumeInfo]] = None) -> Dict[str, object]:
    vols = vols if vols is not None else list_volumes(include_disks=True)
    for v in vols:
        if v.path == path:
            return {"media": v.media, "trim": v.trim, "volume": v}
    return {"media": "Unknown", "trim": None, "volume": None}
