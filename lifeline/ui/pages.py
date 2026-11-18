"""The guided pages: what happened, choose a drive, scan options, scanning, recovery done."""
from __future__ import annotations

import os
import time
from typing import Dict

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QListWidget, QListWidgetItem,
                             QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from . import theme
from .widgets import (Collapsible, DiskMap, DriveRow, Notice, OptionCard, ScenarioCard, StatTile,
                      clear_layout, help_button, label, scroll_page)
from ..core import devices
from ..core.carver import SIGNATURES
from ..core.models import category_for, human_size

# --------------------------------------------------------------------------- scenarios
SCENARIOS = [
    ("deleted", "🗑", "I deleted files or folders by mistake",
     "Including Shift+Delete, an emptied Recycle Bin or Trash, or files deleted by an app.",
     "Lifeline looks for their names and folders first, then searches by content."),
    ("formatted", "💾", "A drive or memory card was formatted, or now asks to be formatted",
     "For USB sticks, SD cards and drives that show as RAW, empty or unreadable.",
     "Lifeline searches the whole drive for photos, documents, videos and more."),
    ("partition", "🧩", "A whole partition or drive letter disappeared",
     "Lifeline searches the disk for lost partitions and lets you browse them.", ""),
    ("trash", "♻", "Check the Recycle Bin / Trash first",
     "Takes a second. Files there are complete and keep their original location.", ""),
]


class StartPage(QWidget):
    chosen = pyqtSignal(str)
    openImage = pyqtSignal()
    openSession = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body, lay = scroll_page()
        outer.addWidget(body)
        lay.addWidget(label("What happened?", "pageTitle"))
        lay.addWidget(label("Pick the closest match. Lifeline chooses the right scan for you, and you can "
                            "always change it later.", "pageLead", wrap=True))
        lay.addSpacing(6)
        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards: Dict[str, ScenarioCard] = {}
        for i, (key, glyph, title, text, hint) in enumerate(SCENARIOS):
            card = ScenarioCard(glyph, title, text, hint)
            card.clicked.connect(lambda k=key: self.chosen.emit(k))
            grid.addWidget(card, i // 2, i % 2)
            self.cards[key] = card
        lay.addLayout(grid)
        lay.addSpacing(8)
        more = QHBoxLayout()
        img = QPushButton("Open a disk image…")
        img.setToolTip("Scan a .img/.dd/.raw/.iso copy of a drive. Also accepts whole-disk images.")
        img.clicked.connect(self.openImage)
        ses = QPushButton("Open a saved scan…")
        ses.setToolTip("Continue where you left off, without scanning again.")
        ses.clicked.connect(self.openSession)
        more.addWidget(img)
        more.addWidget(ses)
        more.addStretch(1)
        lay.addLayout(more)
        lay.addSpacing(8)
        lay.addWidget(Notice(
            "First: stop using the drive the files were on",
            "New files can land on the space your deleted files still occupy. Until you've recovered them, "
            "avoid downloads, installs and saving anything on that drive. Recover onto a different drive "
            "or a USB stick.", kind="info"))
        lay.addStretch(1)


# --------------------------------------------------------------------------- drives
class DrivePage(QWidget):
    chosen = pyqtSignal(object)        # VolumeInfo
    openImage = pyqtSignal()
    makeImage = pyqtSignal()
    restartAdmin = pyqtSignal()
    back = pyqtSignal()

    TITLES = {
        "deleted": ("Where were the files?", "Pick the drive the files or folders were on. Lifeline only reads from it."),
        "formatted": ("Which drive was formatted?", "Pick the drive or memory card. Lifeline only reads from it."),
        "partition": ("Which disk lost a partition?", "Pick the whole disk. Lifeline will search it for partitions "
                                                      "that are no longer listed."),
    }

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.scenario = "deleted"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body, lay = scroll_page()
        outer.addWidget(body)
        self.title = label("", "pageTitle")
        self.lead = label("", "pageLead", wrap=True)
        lay.addWidget(self.title)
        lay.addWidget(self.lead)
        self.notices = QVBoxLayout()
        lay.addLayout(self.notices)
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        lay.addLayout(self.list)
        opts = QHBoxLayout()
        self.show_disks = QCheckBox("Show whole disks")
        self.show_disks.setToolTip("Whole physical disks, for finding lost partitions or scanning everything.")
        self.show_disks.toggled.connect(self.refresh)
        opts.addWidget(self.show_disks)
        opts.addStretch(1)
        lay.addLayout(opts)
        more = QHBoxLayout()
        back = QPushButton("Back")
        back.clicked.connect(self.back)
        img = QPushButton("Open a disk image…")
        img.clicked.connect(self.openImage)
        mk = QPushButton("Make an image of a drive…")
        mk.setToolTip("Copy a failing or important drive to a file first, then scan the copy safely.")
        mk.clicked.connect(self.makeImage)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        more.addWidget(back)
        more.addStretch(1)
        more.addWidget(img)
        more.addWidget(mk)
        more.addWidget(refresh)
        lay.addSpacing(4)
        lay.addLayout(more)
        lay.addStretch(1)

    def set_scenario(self, scenario: str):
        self.scenario = scenario
        t, l = self.TITLES.get(scenario, self.TITLES["deleted"])
        self.title.setText(t)
        self.lead.setText(l)
        self.show_disks.blockSignals(True)
        self.show_disks.setChecked(scenario == "partition")
        self.show_disks.blockSignals(False)
        self.refresh()

    def refresh(self):
        clear_layout(self.list)
        clear_layout(self.notices)
        disks_only = self.scenario == "partition"
        vols = devices.list_volumes(include_disks=self.show_disks.isChecked())
        if disks_only and self.show_disks.isChecked():
            vols = [v for v in vols if v.is_disk] + [v for v in vols if not v.is_disk]
        if devices.needs_admin(vols):
            btn = QPushButton("Restart with administrator rights")
            btn.clicked.connect(self.restartAdmin)
            self.notices.addWidget(Notice(
                "Administrator rights needed",
                "Reading a drive directly needs administrator rights. Lifeline still never writes to it. "
                "You can also open a disk image without them.", action=btn))
        if not vols:
            self.list.addWidget(label("No drives found. You can still open a disk image.", "muted", wrap=True))
        shown_disk_header = False
        for v in vols:
            if v.is_disk and not shown_disk_header:
                self.list.addSpacing(6)
                self.list.addWidget(label("Whole disks", "sectionTitle"))
                shown_disk_header = True
            recommended = (self.scenario == "formatted" and v.removable and not v.is_disk) or \
                          (disks_only and v.is_disk and not v.is_system)
            row = DriveRow(v, recommended=recommended)
            row.clicked.connect(lambda v=v: self.chosen.emit(v))
            self.list.addWidget(row)


# --------------------------------------------------------------------------- setup
def workers_combo(current: int = 0) -> QComboBox:
    from ..core.parallel import cpu_workers
    cb = QComboBox()
    cb.addItem("Automatic", 0)
    n = cpu_workers()
    for k in sorted({1, 2, 4, 8, 16, n}):
        if k <= n:
            cb.addItem(f"{k} (all cores)" if k == n else str(k), k)
    cb.setCurrentIndex(max(0, cb.findData(current)))
    return cb


_SIG_CATEGORY = {"jpg": "Images", "png": "Images", "gif": "Images", "bmp": "Images", "tif": "Images",
                 "psd": "Images", "pdf": "Documents", "zip": "Documents", "ole": "Documents", "7z": "Archives",
                 "rar": "Archives", "mp4": "Video", "mkv": "Video", "asf": "Video", "riff": "Video",
                 "mp3": "Audio", "ogg": "Audio", "flac": "Audio", "sqlite": "Code & data", "exe": "Code & data"}


def sig_category(sig) -> str:
    return getattr(sig, "category", None) or _SIG_CATEGORY.get(sig.key) or category_for(sig.key)


class SetupPage(QWidget):
    start = pyqtSignal(dict)       # options
    back = pyqtSignal()
    partitionChanged = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.session = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body, lay = scroll_page()
        outer.addWidget(body)

        self.title = label("", "pageTitle")
        self.lead = label("", "pageLead", wrap=True)
        lay.addWidget(self.title)
        lay.addWidget(self.lead)
        prow = QHBoxLayout()
        self.part_label = label("Partition:", "sectionTitle")
        self.part_combo = QComboBox()
        self.part_combo.setMinimumWidth(380)
        self.part_combo.activated.connect(self._part_changed)
        prow.addWidget(self.part_label)
        prow.addWidget(self.part_combo)
        prow.addStretch(1)
        lay.addLayout(prow)
        self.notice_box = QVBoxLayout()
        lay.addLayout(self.notice_box)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.cards = {
            "smart": OptionCard("Smart scan", "Finds deleted files with their names and folders, then searches "
                                "free space for anything else.",
                                "The best choice in most cases. Results appear in stages.", tag="Recommended"),
            "quick": OptionCard("Quick scan", "Only reads the drive's file records: original names and folders.",
                                "Seconds to a few minutes. Best for files deleted recently."),
            "deep": OptionCard("Deep scan", "Searches every block for photos, documents, videos, music and archives.",
                               "Slower. Works after formatting or on damaged drives. Names come back as "
                               "numbers, sorted by type."),
            "partitions": OptionCard("Find lost partitions", "Searches the disk for partitions that were deleted "
                                     "or are no longer listed.", "Then lets you scan the one you need."),
        }
        for key, card in self.cards.items():
            card.clicked.connect(lambda k=key: self.select(k))
            cards.addWidget(card)
        lay.addLayout(cards)

        adv = QWidget()
        al = QVBoxLayout(adv)
        al.setContentsMargins(0, 4, 0, 0)
        al.setSpacing(8)
        trow = QHBoxLayout()
        trow.addWidget(label("File types to search for (deep scan)", "sectionTitle"))
        trow.addWidget(help_button("Deep scan recognises files by their internal structure. Fewer types make it "
                                   "slightly faster and the results less crowded."))
        trow.addStretch(1)
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        all_btn.clicked.connect(lambda: [c.setChecked(True) for c in self.type_checks.values()])
        none_btn.clicked.connect(lambda: [c.setChecked(False) for c in self.type_checks.values()])
        trow.addWidget(all_btn)
        trow.addWidget(none_btn)
        al.addLayout(trow)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        self.type_checks: Dict[str, QCheckBox] = {}
        groups: Dict[str, list] = {}
        for sig in SIGNATURES:
            groups.setdefault(sig_category(sig), []).append(sig)
        col = 0
        for cat in ("Images", "Documents", "Video", "Audio", "Archives", "Code & data", "Other"):
            if cat not in groups:
                continue
            box = QVBoxLayout()
            box.setSpacing(3)
            box.addWidget(label(cat, "muted"))
            for sig in groups[cat]:
                cb = QCheckBox(sig.label.replace("&", "&&"))
                cb.setChecked(True)
                self.type_checks[sig.key] = cb
                box.addWidget(cb)
            box.addStretch(1)
            grid.addLayout(box, col // 3, col % 3)
            col += 1
        al.addLayout(grid)
        al.addSpacing(6)
        self.free_only = QCheckBox("Deep scan only free space (faster; skips files that still exist)")
        self.free_only.setChecked(True)
        self.include_existing = QCheckBox("Also list files that still exist (useful for damaged or lost partitions)")
        al.addWidget(self.free_only)
        al.addWidget(self.include_existing)
        wrow = QHBoxLayout()
        wrow.addWidget(label("Parallel workers:"))
        self.workers = workers_combo()
        self.workers.setToolTip("How many CPU cores to use. Automatic uses fewer on spinning hard drives, where "
                                "parallel reads slow things down.")
        wrow.addWidget(self.workers)
        wrow.addStretch(1)
        al.addLayout(wrow)
        self.advanced = Collapsible("advanced options", adv)
        lay.addWidget(self.advanced)

        lay.addStretch(1)
        nav = QHBoxLayout()
        back = QPushButton("Back")
        back.clicked.connect(self.back)
        self.start_btn = QPushButton("Start scan")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._start)
        nav.addWidget(back)
        nav.addStretch(1)
        nav.addWidget(self.start_btn)
        lay.addLayout(nav)
        self.mode = "smart"

    def prepare(self, session, vol, scenario: str):
        self.session = session
        clear_layout(self.notice_box)
        self.title.setText(f"Scan {session.display_name}")
        fs = session.fs_name if session.fs else "unreadable file system"
        lead = f"{human_size(session.size)}, {fs}."
        if session.partition is not None:
            lead += f" Partition at {human_size(session.partition.offset)} into the disk."
        self.lead.setText(lead)
        self.part_combo.clear()
        parts = session.partitions
        for p in parts:
            name = f"{p.fs or 'Unknown'} · {human_size(p.length)}"
            if getattr(p, "label", ""):
                name = f"{p.label} · " + name
            name += f"  ({getattr(p, 'description', '') or p.origin})"
            self.part_combo.addItem(name, p)
        if session.partition is not None and parts:
            for i, p in enumerate(parts):
                if p.offset == session.partition.offset:
                    self.part_combo.setCurrentIndex(i)
        self.part_label.setVisible(len(parts) > 1)
        self.part_combo.setVisible(len(parts) > 1)

        if vol is not None and vol.media == "SSD" and vol.trim is not False:
            self.notice_box.addWidget(Notice(
                "This is an SSD",
                "SSDs usually erase deleted data within minutes (TRIM). Files may show up with their names but "
                "contain only zeros. Scan now, and check previews before recovering."))
        if vol is not None and vol.is_system:
            self.notice_box.addWidget(Notice(
                "This is your system drive",
                "The operating system writes to it constantly. For the best chance, close other programs, or "
                "make an image of the drive onto a USB disk and scan the image instead.", kind="info"))
        if not session.fs:
            self.notice_box.addWidget(Notice("File names can't be read on this drive", session.fs_error or "",
                                             kind="info"))
        whole_disk = (vol is not None and vol.is_disk) or bool(parts) or session.partition is not None
        self.cards["partitions"].setVisible(whole_disk or scenario == "partition")
        self.cards["quick"].setEnabled(session.can_quick)
        self.cards["smart"].setEnabled(True)
        self.free_only.setEnabled(session.can_quick)
        self.free_only.setChecked(session.can_quick and scenario != "formatted")
        lost = session.partition is not None and getattr(session.partition, "origin", "") == "Found"
        self.include_existing.setChecked(lost or scenario == "formatted")
        self.include_existing.setEnabled(session.can_quick)
        if scenario == "partition" and (vol is None or vol.is_disk):
            self.select("partitions")
        else:
            self.select("smart")

    def _part_changed(self, idx: int):
        p = self.part_combo.itemData(idx)
        if p is not None:
            self.partitionChanged.emit(p)

    def select(self, mode: str):
        if not self.cards[mode].isEnabled():
            return
        self.mode = mode
        for k, c in self.cards.items():
            c.set_selected(k == mode)
        self.advanced.setVisible(mode in ("smart", "deep", "quick"))
        self.start_btn.setText({"smart": "Start smart scan", "quick": "Start quick scan",
                                "deep": "Start deep scan", "partitions": "Search for partitions"}[mode])

    def _start(self):
        keys = [k for k, cb in self.type_checks.items() if cb.isChecked()]
        if self.mode in ("deep", "smart") and not keys:
            QMessageBox.information(self, "Choose file types", "Check at least one file type to search for.")
            return
        self.start.emit({"mode": self.mode, "keys": keys, "free_only": self.free_only.isChecked(),
                         "include_existing": self.include_existing.isChecked(),
                         "workers": self.workers.currentData() or None})


# --------------------------------------------------------------------------- scanning
def fmt_time(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)} s"
    if sec < 3600:
        return f"{int(sec // 60)} min {int(sec % 60):02d} s"
    return f"{int(sec // 3600)} h {int(sec % 3600 // 60):02d} min"


class ScanningPage(QWidget):
    stop = pyqtSignal()
    pauseToggled = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 32, 40, 26)
        lay.setSpacing(12)
        self.title = label("Scanning", "pageTitle")
        self.phase = label("", "pageLead", wrap=True)
        lay.addWidget(self.title)
        lay.addWidget(self.phase)
        lay.addSpacing(6)
        self.map = DiskMap()
        lay.addWidget(self.map)
        legend = QHBoxLayout()
        self._swatches = []
        for key, text in (("map_empty", "Not searched yet"), ("map_done", "Searched"), ("accent", "Files found")):
            sw = label("")
            sw.setFixedSize(12, 12)
            self._swatches.append((sw, key))
            legend.addWidget(sw)
            legend.addWidget(label(text, "muted"))
            legend.addSpacing(14)
        legend.addStretch(1)
        lay.addLayout(legend)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.t_found = StatTile("files found")
        self.t_elapsed = StatTile("elapsed", "0 s")
        self.t_left = StatTile("remaining (estimate)", "…")
        self.t_speed = StatTile("read speed", "…")
        for t in (self.t_found, self.t_elapsed, self.t_left, self.t_speed):
            tiles.addWidget(t)
        lay.addLayout(tiles)
        self.tip = Notice("While you wait", "You can stop at any time and look at what's been found so far. "
                          "Pausing frees up the drive if you need it briefly.", kind="info")
        lay.addWidget(self.tip)
        lay.addStretch(1)
        nav = QHBoxLayout()
        nav.addStretch(1)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._pause)
        self.stop_btn = QPushButton("Stop and show results")
        self.stop_btn.clicked.connect(self.stop)
        nav.addWidget(self.pause_btn)
        nav.addWidget(self.stop_btn)
        lay.addLayout(nav)
        self._t0 = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self._bytes = 0
        self.restyle()

    def restyle(self):
        for sw, key in self._swatches:
            sw.setStyleSheet(f"background: {theme.color(key)}; border-radius: 2px;")
        self.map.update()

    def begin(self, title: str, total_bytes: int, show_map: bool = True, can_pause: bool = True):
        self.title.setText(title)
        self.phase.setText("Starting…")
        self.t_found.set("0")
        self.t_elapsed.set("0 s")
        self.t_left.set("…")
        self.t_speed.set("…")
        self.bar.setValue(0)
        self.map.reset()
        self.map.setVisible(show_map)
        self.stop_btn.setEnabled(True)
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(False)
        self.pause_btn.setText("Pause")
        self.pause_btn.blockSignals(False)
        self.pause_btn.setVisible(can_pause)
        self._t0 = time.monotonic()
        self._paused_total = 0.0
        self._bytes = total_bytes

    def _elapsed(self) -> float:
        now = self._paused_at if self.pause_btn.isChecked() else time.monotonic()
        return now - self._t0 - self._paused_total

    def _pause(self, on: bool):
        if on:
            self._paused_at = time.monotonic()
            self.pause_btn.setText("Resume")
            self.phase.setText("Paused. Press Resume to continue.")
        else:
            self._paused_total += time.monotonic() - self._paused_at
            self.pause_btn.setText("Pause")
        self.pauseToggled.emit(on)

    def on_progress(self, frac: float, msg: str, track_map: bool):
        if self.pause_btn.isChecked():
            return
        self.bar.setValue(int(frac * 1000))
        self.phase.setText(msg)
        if track_map:
            self.map.fill_to(frac)
        elapsed = self._elapsed()
        self.t_elapsed.set(fmt_time(elapsed))
        if 0.01 < frac < 1.0 and elapsed > 3:
            self.t_left.set(fmt_time(elapsed * (1 - frac) / frac))
        if self._bytes and elapsed > 1:
            self.t_speed.set(f"{self._bytes * frac / elapsed / 2**20:,.0f} MB/s")

    def on_found(self, n: int):
        self.t_found.set(f"{n:,}")

    def stopping(self):
        self.stop_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.phase.setText("Stopping…")


# --------------------------------------------------------------------------- recovery
class RecoverPage(QWidget):
    openFolder = pyqtSignal()
    backToResults = pyqtSignal()
    startOver = pyqtSignal()
    cancel = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 32, 40, 26)
        lay.setSpacing(12)
        self.title = label("Recovering files", "pageTitle")
        self.lead = label("", "pageLead", wrap=True)
        lay.addWidget(self.title)
        lay.addWidget(self.lead)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.status = label("", "muted", wrap=True)
        lay.addWidget(self.status)
        tiles = QHBoxLayout()
        self.t_ok = StatTile("files recovered")
        self.t_size = StatTile("recovered", "0 B")
        self.t_bad = StatTile("look damaged")
        self.t_failed = StatTile("couldn't be written")
        for t in (self.t_ok, self.t_size, self.t_bad, self.t_failed):
            tiles.addWidget(t)
        self.tiles = QWidget()
        self.tiles.setLayout(tiles)
        lay.addWidget(self.tiles)
        self.problems_title = label("Files to look at", "sectionTitle")
        self.problems = QListWidget()
        lay.addWidget(self.problems_title)
        lay.addWidget(self.problems, 1)
        self.result_notice = QVBoxLayout()
        lay.addLayout(self.result_notice)
        lay.addStretch(0)
        nav = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        self.again_btn = QPushButton("Start over")
        self.again_btn.clicked.connect(self.startOver)
        self.back_btn = QPushButton("Back to results")
        self.back_btn.clicked.connect(self.backToResults)
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self.openFolder)
        nav.addWidget(self.cancel_btn)
        nav.addWidget(self.again_btn)
        nav.addStretch(1)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.open_btn)
        lay.addLayout(nav)

    def begin(self, count: int, size: int, dest: str):
        self.title.setText("Recovering files")
        self.lead.setText(f"Copying {count:,} files ({human_size(size)}) to {dest}")
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.status.setText("")
        self.tiles.setVisible(False)
        self.problems.clear()
        self.problems.setVisible(False)
        self.problems_title.setVisible(False)
        clear_layout(self.result_notice)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        for b in (self.again_btn, self.back_btn, self.open_btn):
            b.setVisible(False)

    def on_progress(self, frac: float, msg: str):
        self.bar.setValue(int(frac * 1000))
        self.status.setText(msg)

    def finish(self, res: dict):
        self.title.setText("Recovery cancelled" if res.get("cancelled") else "Your files are back")
        self.lead.setText(f"Saved to {res['dest']}")
        self.bar.setVisible(False)
        self.status.setText(f"A report of every file is saved there too: {os.path.basename(res['report'])}"
                            if res.get("report") else "")
        self.tiles.setVisible(True)
        self.t_ok.set(f"{res['recovered']:,}")
        self.t_size.set(human_size(res["bytes"]))
        self.t_bad.set(f"{len(res.get('damaged', [])):,}")
        self.t_failed.set(f"{len(res['failed']):,}")
        for it in res.get("damaged", []):
            QListWidgetItem(f"Looks damaged: {it.full_path}  —  {it.note or 'structure check failed'}", self.problems)
        for path, err in res["failed"]:
            QListWidgetItem(f"Not written: {path}  —  {err}", self.problems)
        has = self.problems.count() > 0
        self.problems.setVisible(has)
        self.problems_title.setVisible(has)
        if res.get("damaged"):
            self.result_notice.addWidget(Notice(
                "Some files may not open",
                "Their data was partly overwritten, or they were stored in pieces that deep scan can't join. "
                "Try opening them anyway: photos and documents often still open partly.", kind="warn"))
        elif res["recovered"] and not res.get("cancelled"):
            self.result_notice.addWidget(Notice("Done", "Open a few files to make sure they're what you need "
                                                "before you use the original drive again.", kind="good"))
        self.cancel_btn.setVisible(False)
        for b in (self.again_btn, self.back_btn, self.open_btn):
            b.setVisible(True)
