"""Dialogs: make an image, settings, help, choose a found partition."""
from __future__ import annotations

import os
from typing import List, Optional

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QHeaderView, QPushButton, QSpinBox, QTableWidget,
                             QTableWidgetItem, QTextBrowser, QVBoxLayout)

from .widgets import label
from ..core import devices
from ..core.models import human_size


def settings() -> QSettings:
    return QSettings("Lifeline", "Lifeline File Recovery")


class ImageDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Make an image of a drive")
        lay = QVBoxLayout(self)
        lay.addWidget(label("Copies the whole drive, deleted data included, into one file on another drive. Scan "
                            "the copy instead of the drive: nothing can overwrite your files any more, and a "
                            "failing drive is read only once.", wrap=True))
        lay.addWidget(label("Damaged areas are skipped first and retried at the end. If the copy is interrupted, "
                            "choosing the same file again continues where it stopped.", "muted", wrap=True))
        form = QFormLayout()
        self.src = QComboBox()
        for v in devices.list_volumes(include_disks=True):
            name = v.title + (f" ({v.letter}:)" if v.letter else f" ({v.name})")
            self.src.addItem(f"{name}  ·  {human_size(v.total)}  ·  {v.kind}", v)
        self.dest = QPushButton("Choose where to save…")
        self.dest.clicked.connect(self._pick)
        self.dest_path = ""
        form.addRow("Drive", self.src)
        form.addRow("Save image to", self.dest)
        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = btns.addButton("Make image", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setEnabled(False)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.resize(600, 240)

    def _pick(self):
        v = self.src.currentData()
        name = f"{(v.letter or v.name) if v else 'drive'} image.img".replace("/", "_")
        path, _ = QFileDialog.getSaveFileName(self, "Save image as", os.path.join(os.path.expanduser("~"), name),
                                              "Disk image (*.img)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.dest_path = path
            self.dest.setText(path)
            self.ok.setEnabled(True)

    def _accept(self):
        v = self.src.currentData()
        if v and devices.same_device(v.path, self.dest_path):
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Choose another drive",
                                "Save the image to a different drive than the one you're copying.")
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        s = settings()
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.theme = QComboBox()
        for t, v in (("Follow system", "system"), ("Light", "light"), ("Dark", "dark")):
            self.theme.addItem(t, v)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.value("theme", "system"))))
        from .pages import workers_combo
        self.workers = workers_combo(int(s.value("workers", 0)))
        self.verify = QCheckBox("Check each file after recovering it")
        self.verify.setChecked(s.value("verify", True, type=bool))
        self.rec_workers = QSpinBox()
        self.rec_workers.setRange(1, 16)
        self.rec_workers.setValue(int(s.value("recover_workers", 4)))
        form.addRow("Appearance", self.theme)
        form.addRow("Scan workers", self.workers)
        form.addRow("Files copied at once", self.rec_workers)
        form.addRow("", self.verify)
        lay.addLayout(form)
        lay.addWidget(label("Automatic uses every CPU core for SSDs and image files, and fewer for spinning hard "
                            "drives where parallel reads slow things down.", "muted", wrap=True))
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.resize(460, 220)

    def _save(self):
        s = settings()
        s.setValue("theme", self.theme.currentData())
        s.setValue("workers", self.workers.currentData())
        s.setValue("verify", self.verify.isChecked())
        s.setValue("recover_workers", self.rec_workers.value())
        self.accept()


HELP_HTML = """
<h2>Getting your files back</h2>
<ol>
<li><b>Stop using the drive</b> the files were on. Every new file saved there can land on top of them.</li>
<li><b>Pick what happened</b> and the drive. Lifeline picks the right scan.</li>
<li><b>Tick the files</b> you want. Use the search box, the type list and the gallery to find them.</li>
<li><b>Recover to a different drive</b>, such as a USB stick or external disk.</li>
</ol>

<h3>What do the chances mean?</h3>
<p><b style="color:#2E7A4C">Excellent</b>: its space hasn't been reused; it should come back whole.<br>
<b style="color:#2E7A4C">Good</b>: found by its contents and its structure checks out.<br>
<b style="color:#B9821B">Poor</b>: part of its space now holds other data; it may be damaged.<br>
<b style="color:#B0392C">Unlikely</b>: another file took all of its space. Hidden unless you ask for it.</p>

<h3>Quick, deep and smart scans</h3>
<p><b>Quick scan</b> reads the drive's file records (NTFS, FAT32, exFAT), so files keep their names, folders and
dates. <b>Deep scan</b> searches every block for known file types; it works after formatting and on any file system,
but names are lost and fragmented files may come back damaged. <b>Smart scan</b> does both, quick first.</p>

<h3>My files show up but are empty or full of zeros</h3>
<p>That's normally an SSD with TRIM: the drive erased the deleted blocks itself. No software can undo that.</p>

<h3>The drive is clicking, very slow, or giving errors</h3>
<p>Use <b>Make an image of a drive</b> first and scan the image. The drive is then read once, damaged areas are
skipped and retried at the end, and an interrupted copy can continue later.</p>

<h3>Search tips</h3>
<p><code>*.docx</code>, <code>holiday 2023</code>, <code>ext:jpg,png</code>, <code>type:photos</code>,
<code>size:&gt;5mb</code>, <code>modified:2024</code>, <code>in:Documents</code>, <code>-backup</code>,
<code>"exact phrase"</code>.</p>

<h3>Keyboard</h3>
<p><b>Ctrl+F</b> search · <b>Space</b> tick selected files · <b>Ctrl+R</b> recover ticked files ·
<b>Ctrl+S</b> save scan · <b>Ctrl+O</b> open a disk image · <b>F1</b> this help</p>

<h3>Administrator rights</h3>
<p>Reading a drive directly needs them on every operating system. Lifeline still only reads: it never writes to the
drive it scans, and it refuses to recover files onto it.</p>
"""


class HelpDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Lifeline help")
        lay = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(HELP_HTML)
        lay.addWidget(tb)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.resize(640, 640)


class PartitionsDialog(QDialog):
    """Pick one of the partitions found on a disk."""

    def __init__(self, parent, parts: List, found_count: int):
        super().__init__(parent)
        self.setWindowTitle("Partitions on this disk")
        lay = QVBoxLayout(self)
        if found_count:
            what = "a partition that isn't" if found_count == 1 else f"{found_count} partitions that aren't"
            lay.addWidget(label(f"Lifeline found {what} in the partition table. "
                                "Pick one to scan. Lost partitions usually still have their files.", wrap=True))
        else:
            lay.addWidget(label("No lost partitions turned up. These are the partitions the disk lists. "
                                "You can also deep scan the whole disk.", wrap=True))
        self.table = QTableWidget(len(parts), 5)
        self.table.setHorizontalHeaderLabels(["Name", "File system", "Size", "Starts at", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.parts = parts
        for r, p in enumerate(parts):
            vals = [p.label or p.description or "(no name)", p.fs or "Unknown", human_size(p.length),
                    human_size(p.offset), "Lost" if p.origin == "Found" else f"Listed ({p.origin})"]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lost = [i for i, p in enumerate(parts) if p.origin == "Found"]
        self.table.selectRow(lost[0] if lost else 0)
        self.table.doubleClicked.connect(self.accept)
        lay.addWidget(self.table)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.whole = btns.addButton("Deep scan the whole disk", QDialogButtonBox.ButtonRole.ActionRole)
        ok = btns.addButton("Scan this partition", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        self.choice: Optional[object] = None
        self.whole_disk = False
        self.whole.clicked.connect(self._whole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.resize(720, 360)

    def _whole(self):
        self.whole_disk = True
        self.accept()

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.parts[rows[0].row()] if rows else None
