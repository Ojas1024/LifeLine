"""Build small test volumes: exFAT from scratch, NTFS/FAT via mkntfs/ntfscp/mtools,
plus helpers that emulate deletion the way Windows does it."""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List


def have(*tools: str) -> bool:
    return all(shutil.which(t) for t in tools)


def run(*cmd: str, **kw):
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, **kw)


# --------------------------------------------------------------------------- exFAT

def _dos_stamp(y=2024, mo=5, d=17, h=10, mi=30, s=20) -> int:
    return ((y - 1980) << 25) | (mo << 21) | (d << 16) | (h << 11) | (mi << 5) | (s // 2)


@dataclass
class _Node:
    name: str
    is_dir: bool
    data: bytes = b""
    contiguous: bool = True
    children: List["_Node"] = field(default_factory=list)
    cluster: int = 0
    clusters: List[int] = field(default_factory=list)
    size: int = 0
    deleted: bool = False
    zero_chain: bool = False


class ExFATBuilder:
    """Minimal exFAT writer: enough structure for a reader, not for Windows.
    512-byte sectors, 4 KiB clusters."""

    def __init__(self, size: int = 32 * 1024 * 1024, cluster: int = 4096):
        self.sector, self.cluster = 512, cluster
        self.total_sectors = size // 512
        self.fat_offset = 128
        self.heap_offset = 1024
        self.count = (self.total_sectors - self.heap_offset) // (cluster // 512)
        self.fat_length = -(-(self.count + 2) * 4 // 512)
        assert self.fat_offset + self.fat_length <= self.heap_offset
        self.root = _Node("", True)
        self.next_cluster = 5  # 2 bitmap, 3 upcase, 4 root
        self.fat: Dict[int, int] = {}
        self.used: set = set()
        self.label = "TESTEX"

    def _node(self, path: str, create_dirs=True) -> _Node:
        cur = self.root
        for part in [p for p in path.split("/") if p]:
            for c in cur.children:
                if c.name == part:
                    cur = c
                    break
            else:
                if not create_dirs:
                    raise KeyError(path)
                n = _Node(part, True)
                cur.children.append(n)
                cur = n
        return cur

    def add_file(self, path: str, data: bytes, contiguous: bool = True, gap_after_first: int = 0):
        folder, _, name = path.rpartition("/")
        parent = self._node(folder)
        node = _Node(name, False, data, contiguous)
        n = max(1, -(-len(data) // self.cluster)) if data else 0
        clusters = []
        for i in range(n):
            if i == 1 and gap_after_first:
                self.next_cluster += gap_after_first  # forces a fragmented chain
            clusters.append(self.next_cluster)
            self.next_cluster += 1
        node.clusters, node.size = clusters, len(data)
        node.cluster = clusters[0] if clusters else 0
        if gap_after_first:
            node.contiguous = False
        parent.children.append(node)
        return node

    def delete(self, path: str, zero_chain: bool = False):
        node = self._node(path, create_dirs=False)

        def mark(n: _Node):
            n.deleted = True
            n.zero_chain = zero_chain
            for c in n.children:
                mark(c)
        mark(node)

    def _layout_dirs(self, node: _Node):
        for c in node.children:
            if c.is_dir:
                c.clusters = [self.next_cluster]
                c.cluster = self.next_cluster
                self.next_cluster += 1
                self._layout_dirs(c)

    @staticmethod
    def _entry_set(n: _Node) -> bytes:
        live = 0 if n.deleted else 0x80
        name16 = n.name.encode("utf-16-le")
        parts = [name16[i:i + 30].ljust(30, b"\x00") for i in range(0, len(name16), 30)]
        count = 1 + len(parts)
        f = bytearray(32)
        f[0] = 0x05 | live
        f[1] = count
        struct.pack_into("<H", f, 4, 0x10 if n.is_dir else 0x20)
        struct.pack_into("<III", f, 8, _dos_stamp(), _dos_stamp(mi=45), _dos_stamp())
        s = bytearray(32)
        s[0] = 0x40 | live
        s[1] = 0x01 | (0x02 if (n.contiguous or n.is_dir) else 0)
        s[3] = len(n.name)
        size = len(n.data) if not n.is_dir else 4096
        struct.pack_into("<Q", s, 8, size)
        struct.pack_into("<I", s, 20, n.cluster)
        struct.pack_into("<Q", s, 24, size)
        out = bytes(f) + bytes(s)
        for p in parts:
            e = bytearray(32)
            e[0] = 0x41 | live
            e[2:32] = p
            out += bytes(e)
        return out

    def build(self, path: str):
        self._layout_dirs(self.root)
        img = bytearray(self.total_sectors * 512)
        b = bytearray(512)
        b[0:3] = b"\xEB\x76\x90"
        b[3:11] = b"EXFAT   "
        struct.pack_into("<QQIIIIII", b, 64, 0, self.total_sectors, self.fat_offset, self.fat_length,
                         self.heap_offset, self.count, 4, 0x12345678)
        b[108], b[109], b[110] = 9, 3, 1
        b[510:512] = b"\x55\xaa"
        img[0:512] = b

        fat: Dict[int, int] = {0: 0xFFFFFFF8, 1: 0xFFFFFFFF, 2: 0xFFFFFFFF, 3: 0xFFFFFFFF, 4: 0xFFFFFFFF}
        allocated = {2, 3, 4}
        bitmap = bytearray(-(-self.count // 8))

        def write_cluster_data(clusters, data):
            for i, c in enumerate(clusters):
                chunk = data[i * self.cluster:(i + 1) * self.cluster]
                off = (self.heap_offset * 512) + (c - 2) * self.cluster
                img[off:off + len(chunk)] = chunk

        def emit(node: _Node, clusters: List[int]) -> bytes:
            out = b""
            for c in node.children:
                out += self._entry_set(c)
            return out

        def walk(node: _Node, clusters: List[int]):
            data = emit(node, clusters)
            assert len(data) <= self.cluster * len(clusters)
            write_cluster_data(clusters, data)
            for c in node.children:
                if c.is_dir:
                    if not c.deleted:
                        allocated.update(c.clusters)
                        fat[c.cluster] = 0xFFFFFFFF
                    walk(c, c.clusters)
                else:
                    write_cluster_data(c.clusters, c.data)
                    if not c.deleted:
                        allocated.update(c.clusters)
                    if not c.contiguous and not (c.deleted and c.zero_chain):
                        for i, cl in enumerate(c.clusters):
                            fat[cl] = c.clusters[i + 1] if i + 1 < len(c.clusters) else 0xFFFFFFFF

        # root: label + bitmap + upcase entries, then files
        root_entries = bytearray()
        lab = bytearray(32)
        lab[0] = 0x83
        lab[1] = len(self.label)
        lab[2:2 + 2 * len(self.label)] = self.label.encode("utf-16-le")
        root_entries += lab
        bm = bytearray(32)
        bm[0] = 0x81
        struct.pack_into("<IQ", bm, 20, 2, len(bitmap))
        root_entries += bm
        up = bytearray(32)
        up[0] = 0x82
        struct.pack_into("<IQ", up, 20, 3, 128)
        root_entries += up
        root_entries += emit(self.root, [4])
        write_cluster_data([4], bytes(root_entries))
        # reuse walk for subtrees (root already written)
        for c in self.root.children:
            if c.is_dir:
                if not c.deleted:
                    allocated.add(c.cluster)
                    fat[c.cluster] = 0xFFFFFFFF
                walk(c, c.clusters)
            else:
                write_cluster_data(c.clusters, c.data)
                if not c.deleted:
                    allocated.update(c.clusters)
                if not c.contiguous and not (c.deleted and c.zero_chain):
                    for i, cl in enumerate(c.clusters):
                        fat[cl] = c.clusters[i + 1] if i + 1 < len(c.clusters) else 0xFFFFFFFF
        for c in allocated:
            bitmap[(c - 2) // 8] |= 1 << ((c - 2) % 8)
        write_cluster_data([2], bytes(bitmap))
        fat_off = self.fat_offset * 512
        for k, v in fat.items():
            struct.pack_into("<I", img, fat_off + k * 4, v)
        with open(path, "wb") as f:
            f.write(img)

    def overwrite(self, path: str, data_path: str, img_path: str):
        """Mark a deleted file's first cluster as reused (allocated) in the image's bitmap."""
        node = self._node(data_path, create_dirs=False)
        with open(img_path, "r+b") as f:
            off = self.heap_offset * 512 + 0  # bitmap is cluster 2
            c = node.clusters[0]
            f.seek(off + (c - 2) // 8)
            byte = f.read(1)[0] | (1 << ((c - 2) % 8))
            f.seek(off + (c - 2) // 8)
            f.write(bytes([byte]))


# --------------------------------------------------------------------------- NTFS

def make_ntfs(path: str, files: Dict[str, bytes], size_mb: int = 64, cluster: int = 4096):
    """Create an NTFS image and copy `files` (root-level names) into it with ntfscp."""
    with open(path, "wb") as f:
        f.truncate(size_mb * 1024 * 1024)
    run("mkntfs", "-q", "-F", "-f", "-c", str(cluster), "-L", "TESTNTFS", path)
    tmp = path + ".src"
    os.makedirs(tmp, exist_ok=True)
    for name, data in files.items():
        src = os.path.join(tmp, name)
        with open(src, "wb") as f:
            f.write(data)
        run("ntfscp", "-f", path, src, name)
    shutil.rmtree(tmp)


def ntfs_delete(img: str, record: int, mark_dir: bool = False):
    """Emulate Windows deleting a file: clear the record's in-use flag and free its clusters."""
    from lifeline.core.disk import DiskReader
    from lifeline.core.ntfs import NTFSVolume
    with DiskReader(img) as r:
        vol = NTFSVolume(r)
        rec_off = vol._mft_record_offset(record)
        rec = vol._read_record(record)
        bm_rec = vol._read_record(6)
        bm_runs, bm_size = vol._merge_fragments(bm_rec.fragments)
        bm_extents = vol._runs_to_extents(bm_runs, bm_size)
        clusters = []
        if rec.resident is None and rec.fragments:
            runs, size = vol._merge_fragments(rec.fragments)
            for off, n in vol._runs_to_extents(runs, size):
                if off is not None:
                    first = off // vol.cluster_size
                    clusters.extend(range(first, first + -(-n // vol.cluster_size)))
    with open(img, "r+b") as f:
        f.seek(rec_off + 0x16)
        flags = struct.unpack("<H", f.read(2))[0]
        flags &= ~0x01
        if mark_dir:
            flags |= 0x02
        f.seek(rec_off + 0x16)
        f.write(struct.pack("<H", flags))
        for c in clusters:
            byte_index = c // 8
            # map the bitmap byte to its location on disk
            pos = byte_index
            for off, n in bm_extents:
                if pos < n:
                    f.seek(off + pos)
                    v = f.read(1)[0] & ~(1 << (c % 8))
                    f.seek(off + pos)
                    f.write(bytes([v]))
                    break
                pos -= n


def ntfs_set_parent(img: str, record: int, parent: int):
    """Point a record's first $FILE_NAME at another parent record (for path tests)."""
    from lifeline.core.disk import DiskReader
    from lifeline.core.ntfs import NTFSVolume
    with DiskReader(img) as r:
        vol = NTFSVolume(r)
        rec_off = vol._mft_record_offset(record)
        raw = vol._fixup(r.read_at(rec_off, vol.record_size))
    pos = struct.unpack_from("<H", raw, 0x14)[0]
    while True:
        atype, alen = struct.unpack_from("<II", raw, pos)
        if atype == 0x30:
            voff = struct.unpack_from("<H", raw, pos + 0x14)[0]
            at = pos + voff
            assert at + 8 < 510, "FILE_NAME must sit in the first sector for this test helper"
            with open(img, "r+b") as f:
                f.seek(rec_off + at)
                f.write(struct.pack("<Q", parent | (1 << 48)))
            return
        if atype == 0xFFFFFFFF:
            raise ValueError("no $FILE_NAME")
        pos += alen


def find_record(img: str, name: str) -> int:
    from lifeline.core.disk import DiskReader
    from lifeline.core.ntfs import NTFSVolume
    with DiskReader(img) as r:
        vol = NTFSVolume(r)
        for rec in vol.iter_records(lambda *a: None, lambda: False):
            n = rec.best_name()
            if n and n[0] == name and rec.in_use:
                return rec.number
    raise KeyError(name)


# --------------------------------------------------------------------------- FAT

def make_fat(path: str, fat_bits: int = 32, size_kb: int = 65536, label: str = "TESTFAT"):
    if os.path.exists(path):
        os.remove(path)
    run("mkfs.fat", "-C", "-F", str(fat_bits), "-n", label, path, str(size_kb))


def mcopy(img: str, src_data: bytes, dest: str, workdir: str):
    tmp = os.path.join(workdir, "_mcopy_src")
    with open(tmp, "wb") as f:
        f.write(src_data)
    run("mcopy", "-i", img, tmp, "::" + dest)
    os.remove(tmp)


def read_item(reader, item) -> bytes:
    if item.resident_data is not None:
        return item.resident_data[:item.size]
    out = bytearray()
    for off, n in item.extents:
        out += b"\x00" * n if off is None else reader.read_at(off, n)
    return bytes(out[:item.size])
