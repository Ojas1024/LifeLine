"""Search across found files with a forgiving, plain query language.

Everything is optional and combines with AND:

  thesis report          words anywhere in the name or folder (any order)
  "final draft"          an exact phrase
  -backup                leave out matches
  *.docx  IMG_20??*      wildcards on the file name
  ext:jpg,png            file extensions (also: type:photos, type:documents ...)
  size:>10mb  size:1mb..50mb  size:<500kb
  modified:2024  modified:>2024-03-01  modified:2023-01..2023-06  (also created:, deleted:)
  chance:excellent       recovery chance (excellent, good, poor, unlikely)
  in:Documents           folder contains this text
  re:^IMG_\\d+           regular expression on the full path
  found:quick / found:deep / found:bin

Unknown or malformed filters fall back to plain text, so a search never errors out.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .models import CATEGORIES, RecoverableItem, Source

Predicate = Callable[[RecoverableItem], bool]

_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3,
          "t": 1024 ** 4, "tb": 1024 ** 4}

TYPE_ALIASES = {
    "photo": "Images", "photos": "Images", "image": "Images", "images": "Images", "picture": "Images",
    "pictures": "Images", "doc": "Documents", "docs": "Documents", "document": "Documents",
    "documents": "Documents", "video": "Video", "videos": "Video", "movie": "Video", "movies": "Video",
    "audio": "Audio", "music": "Audio", "sound": "Audio", "archive": "Archives", "archives": "Archives",
    "code": "Code & data", "data": "Code & data", "other": "Other",
}

SOURCE_ALIASES = {"quick": Source.QUICK, "deep": Source.DEEP, "bin": Source.TRASH, "trash": Source.TRASH,
                  "recycle": Source.TRASH, "partition": Source.PARTITION}


def parse_size(text: str) -> Optional[int]:
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-z]*)\s*", text.lower())
    if not m or m.group(2) not in _UNITS and m.group(2):
        return None
    try:
        return int(float(m.group(1)) * _UNITS.get(m.group(2) or "b", 1))
    except ValueError:
        return None


def _parse_date(text: str, end: bool = False) -> Optional[datetime]:
    text = text.strip()
    for fmt, fill_end in (("%Y-%m-%d", None), ("%Y-%m", "month"), ("%Y", "year"), ("%d/%m/%Y", None)):
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end:
            if fill_end == "year":
                return d.replace(month=12, day=31, hour=23, minute=59, second=59)
            if fill_end == "month":
                nxt = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
                return datetime.fromtimestamp(nxt.timestamp() - 1)
            return d.replace(hour=23, minute=59, second=59)
        return d
    return None


def _range(value: str, parse, parse_end=None) -> Optional[Tuple[Optional[object], Optional[object]]]:
    """'>x', '<x', 'a..b', 'x' -> (low, high) with None for an open end."""
    parse_end = parse_end or parse
    v = value.strip()
    if ".." in v:
        a, b = v.split("..", 1)
        lo = parse(a) if a else None
        hi = parse_end(b) if b else None
        return None if (a and lo is None) or (b and hi is None) else (lo, hi)
    if v[:2] in (">=", "<="):
        op, v = v[:2], v[2:]
    elif v[:1] in "<>=":
        op, v = v[:1], v[1:]
    else:
        op = "="
    x = parse(v)
    if x is None:
        return None
    if op in (">", ">="):
        return x, None
    if op in ("<", "<="):
        return None, (parse_end(v) if op == "<=" else x)
    return x, parse_end(v)


def _in_range(x, lo, hi) -> bool:
    if x is None:
        return False
    return (lo is None or x >= lo) and (hi is None or x <= hi)


def _term(tok: str) -> Optional[Predicate]:
    low = tok.lower()
    if ":" in tok and not re.match(r"^[a-zA-Z]:[\\/]", tok):
        key, _, val = tok.partition(":")
        key = key.lower()
        if not val:
            return None
        if key in ("ext", "extension"):
            exts = {e.strip().lstrip(".").lower() for e in val.split(",") if e.strip()}
            return lambda it: it.ext in exts
        if key in ("type", "kind", "category"):
            cats = {TYPE_ALIASES.get(v.strip().lower(), v.strip().title()) for v in val.split(",")}
            return lambda it: it.category in cats
        if key == "size":
            rng = _range(val, parse_size)
            if rng:
                lo, hi = rng
                if lo is not None and hi is not None and lo == hi:   # size:10mb means roughly 10 MB
                    lo, hi = int(lo * 0.95), int(hi * 1.05) + 1
                return lambda it: _in_range(it.size, lo, hi)
        if key in ("modified", "date", "created", "deleted"):
            rng = _range(val, _parse_date, lambda s: _parse_date(s, end=True))
            if rng:
                lo, hi = rng
                attr = {"modified": "modified", "date": "modified", "created": "created", "deleted": "deleted_at"}[key]
                return lambda it: _in_range(getattr(it, attr), lo, hi)
        if key == "chance":
            want = {v.strip().lower() for v in val.split(",")}
            return lambda it: it.chance.lower() in want
        if key in ("in", "folder", "path"):
            v = val.lower()
            return lambda it: v in it.folder.lower()
        if key in ("re", "regex"):
            try:
                rx = re.compile(val, re.IGNORECASE)
            except re.error:
                return None
            return lambda it: rx.search(it.full_path) is not None
        if key in ("found", "source", "from"):
            srcs = {SOURCE_ALIASES.get(v.strip().lower(), v) for v in val.split(",")}
            return lambda it: it.source in srcs
        if key == "name":
            return _term(val) if any(c in val for c in "*?[") else (lambda it: val.lower() in it.name.lower())
    if any(c in tok for c in "*?["):
        return lambda it: fnmatch.fnmatch(it.name.lower(), low)
    return lambda it: low in it.full_path.lower()


def compile_query(text: str) -> Predicate:
    """Turn a search string into a predicate. Never raises."""
    text = (text or "").strip()
    if not text:
        return lambda it: True
    # Quote-aware split that keeps backslashes (regexes and Windows paths need them).
    tokens = [t.replace('"', "") for t in re.findall(r'(?:[^\s"]*"[^"]*"?)+[^\s"]*|\S+', text)]
    preds: List[Tuple[bool, Predicate]] = []
    for tok in tokens:
        neg = tok.startswith("-") and len(tok) > 1
        body = tok[1:] if neg else tok
        p = _term(body) or (lambda it, b=body.lower(): b in it.full_path.lower())
        preds.append((neg, p))

    def match(it: RecoverableItem) -> bool:
        for neg, p in preds:
            try:
                ok = p(it)
            except Exception:
                ok = False
            if ok == neg:
                return False
        return True

    return match


def category_names() -> List[str]:
    return [*CATEGORIES.keys(), "Other"]
