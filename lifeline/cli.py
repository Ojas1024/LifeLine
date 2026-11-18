"""Command-line interface (Windows, macOS, Linux).

  lifeline list [--disks]
  lifeline scan D: --mode smart --out E:\\Recovered
  lifeline scan /dev/sdb1 --mode quick --query "*.docx modified:2024" --out ~/Recovered
  lifeline scan disk.img --mode deep --types jpg,pdf,zip --workers 8 --save scan.lifeline
  lifeline load scan.lifeline --query "type:photos" --out ~/Recovered
  lifeline partitions /dev/sdb --search
  lifeline trash --out ~/Recovered
  lifeline image /dev/sdb ~/sdb.img
"""
from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .core import devices, disk
from .core.carver import SIGNATURES
from .core.models import human_size
from .core.query import compile_query
from .core.recovery import RecoveryError, recover
from .core.session import ScanSession

COMMANDS = ("list", "scan", "load", "partitions", "trash", "image")


def _source(arg: str) -> str:
    """'D', 'D:' -> \\\\.\\D: on Windows. Anything else is a device or image path."""
    if disk.IS_WINDOWS and len(arg) in (1, 2) and arg[0].isalpha() and arg.rstrip(":").isalpha():
        return disk.raw_path_for_letter(arg[0])
    return arg


class Bar:
    def __init__(self):
        self.last = 0.0
        self.t0 = time.monotonic()

    def __call__(self, frac: float, msg: str):
        now = time.monotonic()
        if now - self.last < 0.2 and frac < 1:
            return
        self.last = now
        width = 28
        fill = int(min(frac, 1.0) * width)
        eta = ""
        el = now - self.t0
        if 0.02 < frac < 1 and el > 3:
            left = el * (1 - frac) / frac
            eta = f" ~{int(left // 60)}m{int(left % 60):02d}s"
        sys.stderr.write(f"\r[{'#' * fill}{'.' * (width - fill)}] {frac:6.1%}{eta:>9}  {msg[:56]:<56}")
        sys.stderr.flush()
        if frac >= 1:
            sys.stderr.write("\n")


def _never():
    return False


def cmd_list(args):
    vols = devices.list_volumes(include_disks=args.disks)
    if not vols:
        print("No drives found. You can still scan a disk image file.")
    for v in vols:
        flags = [v.filesystem or "-", v.kind]
        if v.media == "SSD" and v.trim:
            flags.append("TRIM on")
        if not v.readable:
            flags.append("needs admin")
        name = f"{v.letter}:" if v.letter else v.path
        print(f"{name:<22} {(v.label or v.title)[:22]:<22} {human_size(v.total):>10}   {', '.join(flags)}")
    if devices.needs_admin(vols):
        print("\nReading drives needs administrator rights. " + devices.elevation_hint(), file=sys.stderr)
    return 0


def _open(src_arg: str, partition_index=None):
    src = _source(src_arg)
    info = devices.media_for_source(src) if disk.is_device_path(src) else {"media": "Unknown"}
    s = ScanSession(src, src_arg, media=info["media"])
    if partition_index is not None:
        from .core.partitions import read_partition_table
        parts = read_partition_table(s.disk)
        if partition_index >= len(parts):  # not in the table: number them as `partitions --search` does
            print("Searching for lost partitions…", file=sys.stderr)
            parts = s.search_partitions(Bar(), _never) or parts
        if partition_index >= len(parts):
            raise IndexError(f"There's no partition {partition_index}. See: lifeline partitions {src_arg} --search")
        s.use_partition(parts[partition_index])
    return s


def _finish(s: ScanSession, args):
    items = s.items
    if not args.include_overwritten:
        items = [i for i in items if not (i.chance == "Unlikely" and i.deleted)]
    if getattr(args, "query", None):
        pred = compile_query(args.query)
        items = [i for i in items if pred(i)]
    if getattr(args, "verify", False) and items:
        print("Checking files…", file=sys.stderr)
        s.verify(items, Bar(), _never)
    if getattr(args, "save", None):
        s.save(args.save)
        print(f"Scan saved to {args.save}", file=sys.stderr)
    print(f"\n{len(items):,} files ({human_size(sum(i.size for i in items))})")
    if args.list_only or not args.out:
        for i in items[: args.limit]:
            check = f" [{i.verified}]" if i.verified else ""
            print(f"  {i.chance:<10} {human_size(i.size):>10}  {i.full_path}{check}")
        if len(items) > args.limit:
            print(f"  … and {len(items) - args.limit:,} more (use --limit)")
        if not args.out:
            print("\nAdd --out <folder on another drive> to recover them.")
        return 0
    try:
        res = recover(s.reader, s.source_path, items, args.out, not args.flat, Bar(), _never,
                      workers=args.copy_workers, verify=args.verify_after)
    except RecoveryError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"Recovered {res['recovered']:,} files ({human_size(res['bytes'])}) to {args.out}")
    if res["damaged"]:
        print(f"{len(res['damaged']):,} look damaged (see the report).")
    if res["failed"]:
        print(f"{len(res['failed']):,} couldn't be written (see the report).")
    if res["report"]:
        print(f"Report: {res['report']}")
    return 0


def cmd_scan(args):
    try:
        s = _open(args.source, args.partition)
    except (disk.DiskError, IndexError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    keys = args.types.split(",") if args.types else [x.key for x in SIGNATURES]
    workers = args.workers or None
    print(f"Source: {s.display_name}  ·  {human_size(s.size)}  ·  {s.fs_name}"
          + (f"  ·  partition at {human_size(s.partition.offset)}" if s.partition else ""), file=sys.stderr)
    try:
        if args.mode in ("quick", "both", "smart"):
            if s.can_quick:
                print("Quick scan: reading file records…", file=sys.stderr)
                s.quick_scan(Bar(), _never, include_existing=args.include_existing, workers=workers)
            elif args.mode == "quick":
                print(f"Error: {s.fs_error}", file=sys.stderr)
                return 2
        if args.mode in ("deep", "both", "smart"):
            print("Deep scan: searching for file contents…", file=sys.stderr)
            s.deep_scan(keys, s.can_quick and not args.all_space, Bar(), _never, workers=workers)
    except KeyboardInterrupt:
        print("\nStopped. Showing what was found so far.", file=sys.stderr)
    return _finish(s, args)


def cmd_load(args):
    try:
        s = ScanSession.load(args.file, source_override=args.source)
    except disk.DiskError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return _finish(s, args)


def cmd_trash(args):
    s = ScanSession(None, "Recycle Bin")
    s.scan_trash(Bar(), _never)
    return _finish(s, args)


def cmd_partitions(args):
    try:
        s = ScanSession(_source(args.source), args.source)
    except disk.DiskError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    from .core.partitions import read_partition_table
    parts = read_partition_table(s.disk)
    if args.search:
        print("Searching for lost partitions…", file=sys.stderr)
        parts = s.search_partitions(Bar(), _never) or parts
    if not parts:
        print("No partitions found.")
    for i, p in enumerate(parts):
        print(f"[{i}] {p.origin:<5} {p.fs or 'Unknown':<7} {human_size(p.length):>10} at {p.offset:>15,}  "
              f"{p.label or p.description}")
    if parts:
        print("\nScan one with: lifeline scan <source> --partition <number>")
    return 0


def cmd_image(args):
    src = _source(args.source)
    try:
        res = disk.create_image(src, args.dest, Bar(), _never, resume=not args.restart)
    except disk.DiskError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if res["resumed_from"]:
        print(f"Continued from {human_size(res['resumed_from'])}")
    print(f"Wrote {human_size(res['bytes'])}; unreadable 4 KB blocks: {res['unreadable_blocks']}")
    return 0


def _add_output_args(p):
    p.add_argument("--query", "-q", help='search filter, e.g. "*.docx size:>1mb modified:2024 -backup"')
    p.add_argument("--match", dest="query", help=argparse.SUPPRESS)  # v1 name
    p.add_argument("--out", "-o", help="folder to recover into (must be on another drive)")
    p.add_argument("--flat", action="store_true", help="don't recreate original folders")
    p.add_argument("--include-overwritten", action="store_true", help="also list files marked Unlikely")
    p.add_argument("--list-only", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--verify", action="store_true", help="check every listed file's structure before output")
    p.add_argument("--verify-after", action="store_true", help="check each file after recovering it")
    p.add_argument("--copy-workers", type=int, default=4, help="files written at once (default 4)")
    p.add_argument("--save", help="save the results to a .lifeline file to reopen later")


def main(argv=None):
    p = argparse.ArgumentParser(prog="lifeline",
                                description="Recover deleted files from NTFS, FAT and exFAT drives, disks and images.")
    p.add_argument("--version", action="version", version=f"Lifeline {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="list drives")
    ls.add_argument("--disks", action="store_true", help="also list whole physical disks")

    sc = sub.add_parser("scan", help="scan a drive, disk or image and optionally recover")
    sc.add_argument("source", help="drive letter (D:), device (/dev/sdb1, /dev/rdisk2s1) or image file")
    sc.add_argument("--mode", choices=["smart", "quick", "deep", "both"], default="smart")
    sc.add_argument("--types", help="deep scan types, comma separated: " + ",".join(s.key for s in SIGNATURES))
    sc.add_argument("--all-space", action="store_true", help="deep scan the whole drive, not just free space")
    sc.add_argument("--include-existing", action="store_true", help="also list files that still exist")
    sc.add_argument("--partition", type=int, help="scan partition number N (see `lifeline partitions`)")
    sc.add_argument("--workers", type=int, default=0, help="parallel scan workers (default: automatic)")
    _add_output_args(sc)

    ld = sub.add_parser("load", help="reopen a saved .lifeline scan")
    ld.add_argument("file")
    ld.add_argument("--source", help="where the drive or image is now, if it moved")
    _add_output_args(ld)

    tr = sub.add_parser("trash", help="list or restore files in the Recycle Bin / Trash")
    _add_output_args(tr)

    pt = sub.add_parser("partitions", help="list partitions on a disk, optionally searching for lost ones")
    pt.add_argument("source")
    pt.add_argument("--search", action="store_true", help="search the whole disk for lost partitions")

    im = sub.add_parser("image", help="copy a whole drive into an image file (resumable)")
    im.add_argument("source")
    im.add_argument("dest")
    im.add_argument("--restart", action="store_true", help="start over instead of continuing an existing image")
    args = p.parse_args(argv)
    return {"list": cmd_list, "scan": cmd_scan, "load": cmd_load, "trash": cmd_trash,
            "partitions": cmd_partitions, "image": cmd_image}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
